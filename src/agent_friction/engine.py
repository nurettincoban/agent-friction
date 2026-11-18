"""Public entry point: :func:`analyze`.

Pipeline: locate transcripts -> parse into normalized streams -> detect
findings (plus git rework) -> attribute to files, directories and commands ->
score -> recommend -> emit a deterministic, JSON-serializable dict whose
schema is documented in docs/schema.md.
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import json
import os
import posixpath
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_friction import __version__, gitsignals, model, recommend, redact, scoring, signals
from agent_friction.adapters import claude_code, parse_transcript
from agent_friction.attribution import (
    PUBLIC_HASH_DIGITS,
    PathResolver,
    is_inline_code,
    is_windows_path,
    public_key,
    public_tool,
    to_posix,
)
from agent_friction.discovery import (
    claude_config_dirs,
    discover_repo_transcripts,
    enclosing_repositories,
    expand_transcript_paths,
    repo_name,
    repo_roots,
)
from agent_friction.errors import AgentFrictionError, BadTranscriptError, NoTranscriptsError
from agent_friction.guidance import (
    Guidance,
    deny_rules,
    parse_deny_rule,
    rule_denies,
    typed_pieces,
)
from agent_friction.model import Finding, ParsedFile, Stream, Target
from agent_friction.report._common import paste_candidates

__all__ = [
    "SCHEMA_VERSION",
    "AgentFrictionError",
    "AnalyzeOptions",
    "NoTranscriptsError",
    "analyze",
    "list_transcripts",
]

SCHEMA_VERSION = 1
#: Where the generic format is described; docs/ is not part of the installed package.
SCHEMA_URL = "https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md"
TOOL_NAME = "agent-friction"

Item = tuple[Finding, float]


@dataclass
class AnalyzeOptions:
    """Inputs for :func:`analyze`. Only ``repo`` is required."""

    repo: Path
    #: Transcript files or directories. ``None`` auto-discovers Claude Code transcripts.
    transcripts: Sequence[Path] | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_git: bool = True
    #: ``False`` removes every verbatim transcript quote from the result.
    include_excerpts: bool = True
    max_evidence: int = 5
    max_hotspots: int | None = None  # None: every hotspot
    #: Fixed "now" for ``generated_at`` (deterministic output in tests).
    now: datetime | None = None
    # ---- additions to the base contract ----
    #: Maximum characters per excerpt.
    excerpt_chars: int = redact.DEFAULT_EXCERPT_CHARS
    #: Other absolute paths that are this repo (e.g. where it lived when recorded).
    repo_aliases: Sequence[Path | str] = field(default_factory=tuple)
    rework_window_days: int = gitsignals.DEFAULT_REWORK_WINDOW_DAYS
    #: Regexes marking a commit as agent-authored; ``None`` uses the defaults.
    agent_commit_patterns: Sequence[str] | None = None
    #: Extra regexes (matched against ``"author: <name> <email>"``) for automation whose
    #: commits are never human rework, in addition to ``gitsignals.DEFAULT_BOT_PATTERNS``.
    bot_commit_patterns: Sequence[str] = field(default_factory=tuple)
    git_max_commits: int = gitsignals.DEFAULT_MAX_COMMITS
    #: With no transcripts at all, do not raise ``NoTranscriptsError``: analyse the git
    #: history alone, and where there is none either (no repository, no commits,
    #: ``include_git=False``) return an empty, valid result. A transcript path that
    #: does not exist, or a file that is no transcript, is still an error.
    allow_no_transcripts: bool = False

    def __post_init__(self) -> None:
        # Callers of the library pass strings: `AnalyzeOptions(repo="shop")`.
        self.repo = Path(self.repo)
        given = self.transcripts
        if isinstance(given, str | os.PathLike):
            given = [given]  # one path, not a sequence of its characters
        if given is not None and not all(isinstance(path, Path) for path in given):
            self.transcripts = [Path(path) for path in given]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _in_window(ts: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    if ts is None:
        return True
    return not ((since is not None and ts < since) or (until is not None and ts >= until))


def _apply_window(
    stream: Stream, since: datetime | None, until: datetime | None
) -> tuple[list[model.Event], list[model.Event]]:
    """Drop events outside the window. Events without a timestamp inherit the previous one.

    Returns the events *before* and *after* the window: they are not counted, but
    what the agent did before is the context for judging the first events inside
    it, and what it did after says how the last ones ended (a red run that was
    fixed, a command that recovered).
    """
    if since is None and until is None:
        return [], []
    kept: list[model.Event] = []
    before: list[model.Event] = []
    after: list[model.Event] = []
    last: datetime | None = None
    for event in stream.events:
        last = event.ts or last
        if _in_window(last, since, until):
            kept.append(event)
        elif since is not None and last is not None and last < since:
            before.append(event)
        else:
            after.append(event)
    stream.events = kept
    return before, after


def _load(
    files: Sequence[Path],
    since: datetime | None,
    until: datetime | None,
    unreadable: list[tuple[Path, str]] | None = None,
    untimed: list[Stream] | None = None,
) -> tuple[list[ParsedFile], dict[str, tuple[list[model.Event], list[model.Event]]]]:
    """Parsed files cut to the window, and each stream's events outside it (by key).

    Files that cannot be read are skipped and noted in ``unreadable`` with the reason.
    """
    seen: dict[str, str] = {}
    parsed: list[ParsedFile] = []
    context: dict[str, tuple[list[model.Event], list[model.Event]]] = {}
    for path in files:
        try:
            item = parse_transcript(path, seen)
        except OSError as exc:
            if unreadable is not None:
                unreadable.append((path, read_error(exc)))
            continue
        bounded = since is not None or until is not None
        for stream in item.streams:
            if bounded and stream.events and all(e.ts is None for e in stream.events):
                # Nobody can say when this session happened. Inside a window it would be
                # inside every window, and a before/after pair would count it twice.
                if untimed is not None:
                    untimed.append(dataclasses.replace(stream))
                stream.events = []
                continue
            before, after = _apply_window(stream, since, until)
            if (before or after) and stream.events:
                context[stream.key] = (before, after)
        item.streams = [s for s in item.streams if s.events]
        parsed.append(item)
    return parsed, context


def read_error(exc: OSError) -> str:
    """Why a file could not be read, in fixed words (never the path)."""
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, FileNotFoundError):
        return "file not found"
    if isinstance(exc, IsADirectoryError):
        return "is a directory"
    return "could not be read"


#: A prompt (in a headless session: any activity) after this much silence starts a new
#: episode of work in the session.
EPISODE_IDLE_SECONDS = 2 * 3600.0


def episode_starts(streams: Sequence[Stream]) -> dict[str, list[float]]:
    """When the second and later episodes of each session begin (epoch seconds).

    A session id can live for days (``claude --continue``): ten tasks on ten days
    are ten independent occasions for the same friction, not one. A new episode
    starts at a human prompt that follows ``EPISODE_IDLE_SECONDS`` or more without
    any event in the session. In a session nobody types into after its opening
    prompt (a headless run resumed the next day, a container restarted) there is no
    such prompt to go by: there the agent's next message or call after that much
    silence starts one. A tool result never does: it is a long command finishing.
    Subagent streams never start one either: they belong to the episode of the agent
    that drives them.
    """
    prompts: dict[str, int] = {}
    for stream in streams:
        if not stream.sidechain:
            typed = sum(1 for e in stream.events if e.kind == model.USER_MESSAGE)
            prompts[stream.session_id] = prompts.get(stream.session_id, 0) + typed
    by_session: dict[str, list[tuple[float, bool]]] = {}
    for stream in streams:
        marks = by_session.setdefault(stream.session_id, [])
        headless = prompts.get(stream.session_id, 0) <= 1
        for event in stream.events:
            if event.ts is not None:
                prompt = not stream.sidechain and (
                    event.kind == model.USER_MESSAGE
                    or (headless and event.kind != model.TOOL_RESULT)
                )
                marks.append((event.ts.timestamp(), prompt))
    starts: dict[str, list[float]] = {}
    for session, marks in by_session.items():
        marks.sort()
        previous: float | None = None
        for stamp, prompt in marks:
            if prompt and previous is not None and stamp - previous >= EPISODE_IDLE_SECONDS:
                starts.setdefault(session, []).append(stamp)
            previous = stamp
    return starts


#: A commit up to this long after a session's last event still belongs to that session.
COMMIT_SESSION_SLACK = timedelta(minutes=10)


def _session_spans(streams: Sequence[Stream]) -> list[tuple[datetime, datetime, str]]:
    """First and last timestamp of every transcript session, sorted by start."""
    spans: dict[str, tuple[datetime, datetime]] = {}
    for stream in streams:
        stamps = [e.ts for e in stream.events if e.ts is not None]
        if not stamps:
            continue
        low, high = min(stamps), max(stamps)
        if stream.session_id in spans:
            low = min(low, spans[stream.session_id][0])
            high = max(high, spans[stream.session_id][1])
        spans[stream.session_id] = (low, high)
    return sorted((low, high, sid) for sid, (low, high) in spans.items())


def _session_at(
    spans: Sequence[tuple[datetime, datetime, str]], when: datetime | None
) -> str | None:
    """The transcript session during which (or just after which) ``when`` falls."""
    if when is None:
        return None
    for low, high, session_id in spans:
        if low <= when <= high + COMMIT_SESSION_SLACK:
            return session_id
    return None


def _unclaim_single_session_stretches(findings: Sequence[Finding]) -> None:
    """A ``no-convergence`` event claims time only where the pattern recurs.

    One stuck session can be a hard task: it is a capped lead and costs nothing
    in the totals. From two sessions on the same command or file, it counts.
    """
    sessions: dict[tuple[str, str], set[str]] = {}
    stuck = [f for f in findings if f.signal == signals.NO_CONVERGENCE]
    for finding in stuck:
        if finding.detail.get("uncertain"):
            continue
        for target in finding.targets:
            sessions.setdefault((target.kind, target.key), set()).add(finding.unit)
    for finding in stuck:
        recurring = any(
            len(sessions.get((t.kind, t.key), ())) >= scoring.MEDIUM_MIN_SESSIONS
            for t in finding.targets
        )
        if not recurring:
            finding.intervals = ()


#: Directory names that hold other people's code or build output, wherever they occur.
THIRD_PARTY_DIRS = frozenset(
    {
        "node_modules",
        "bower_components",
        "site-packages",
        "dist-packages",
        "vendor",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".gradle",
        ".next",
        ".nuxt",
        ".eggs",
    }
)
#: Names that usually hold build output but can be first-party (`src/build/`): third-party
#: unless git says the directory is not ignored.
BUILD_DIRS = frozenset({"target", "dist", "build"})
#: A test command that fails and passes on a plain re-run in this many sessions is flaky.
FLAKY_MIN_SESSIONS = 3
#: Sessions in which another runner must pass, untouched, after a command went red.
RUNNER_MIN_SESSIONS = 3
#: Sessions in which a command passed unchanged with a variable set in front.
ENV_MIN_SESSIONS = 2
#: Episodes in which the same test must be red, unresolved, to be a standing failure.
STANDING_TEST_MIN_SESSIONS = 3
#: A command the human stops in this many sessions is a row of its own.
INTERRUPTED_COMMAND_MIN_SESSIONS = 3
#: The share of an interrupt that goes to the command that was stopped.
INTERRUPTED_COMMAND_SHARE = 0.3
#: Below this score a hotspot seen in a single session is a lead: it gets no advice.
SINGLE_SESSION_ADVICE_MIN_SCORE = 20.0
#: ... except what rests on a human's own words (a correction, a stated reason).
HUMAN_STATED_RULES = frozenset({"write-down-the-rule", "state-the-boundary"})
#: Evidence items listed for events that belong to no hotspot (``unattributed``).
MAX_UNATTRIBUTED_EVIDENCE = 20
#: How deep, and through how many directories, a workspace package is looked for by name.
WORKSPACE_DEPTH = 3
WORKSPACE_MAX_DIRS = 5000
#: Existing make targets or package scripts listed next to "this target does not exist".
MAX_KNOWN_TARGETS = 12
MAX_TARGETS_FILE_BYTES = 512_000
_JUST_RECIPE_RE = re.compile(r"(?m)^@?([A-Za-z_][\w-]*)(?:[ \t]+[^:=\n]*)?:(?!=)")
_MAKE_TARGET_RE = re.compile(r"(?m)^([A-Za-z0-9_][\w-]*)[ \t]*:(?![=:])")

#: Sessions in which the same command must stay red for its failures to count in full,
#: and how often it must have failed in each. One red run when a session ends is routine.
RECURRING_RED_MIN_SESSIONS = 3
RECURRING_RED_MIN_RUNS = 2


def _command_keys(finding: Finding) -> list[str]:
    return [t.key for t in finding.targets if t.kind == model.COMMAND]


#: Details that say the passing run was not the failing line again (another spelling, a
#: variable in front, a setup step): the cause is known, and it is not flakiness.
_EXPLAINED = frozenset(
    {"works_with", "works_as", "works_in", "own_module", "recovered_by", "fixed_by_setup"}
)


def _plain_rerun(finding: Finding) -> bool:
    detail = finding.detail
    return detail.get("variant") == "ordinary" and not _EXPLAINED & detail.keys()


def _settle_command_fixes(findings: list[Finding]) -> list[Finding]:
    """Decide, across sessions, what a single session cannot.

    * A red run that went green after some command which is not a known setup
      step (``fixed_by_command``): once is a coincidence or a one-off script and
      is dropped as the development loop; the same pair in two or more sessions
      is a setup step under another name and is kept in full.
    * A red run of a test command that passed on a plain re-run with nothing changed
      (``rerun_green``): from ``FLAKY_MIN_SESSIONS`` sessions on, the test is flaky
      (variant ``flaky``), which is worth fixing; below that it stays an ordinary
      failure.
    * An ordinary failure of a command that never went green in its session
      (``never_green``) after failing ``RECURRING_RED_MIN_RUNS`` times or more:
      from ``RECURRING_RED_MIN_SESSIONS`` such sessions on it is a pattern, not a
      red test: it is no longer weak and claims its own run time.

    Returns the findings that were dropped.
    """
    pairs: dict[tuple[str, str], set[str]] = {}
    for finding in findings:
        # An ad-hoc snippet (`python <inline>`) is a different program every time: it is
        # never the step that fixes a command, however often "it" ran before a green run.
        if is_inline_code(finding.detail.get("fixed_by_command", "")):
            del finding.detail["fixed_by_command"]
        if is_inline_code(finding.detail.get("fixed_by_setup", "")):
            del finding.detail["fixed_by_setup"]
    pending = [f for f in findings if "fixed_by_command" in f.detail]
    for finding in pending:
        for key in _command_keys(finding):
            pair = (key, finding.detail["fixed_by_command"])
            pairs.setdefault(pair, set()).add(finding.unit)
    reruns: dict[str, set[str]] = {}
    plain = [f for f in findings if "rerun_green" in f.detail and _plain_rerun(f)]
    for finding in plain:
        for key in _command_keys(finding):
            reruns.setdefault(key, set()).add(finding.unit)
    for finding in plain:
        if any(len(reruns.get(key, ())) >= FLAKY_MIN_SESSIONS for key in _command_keys(finding)):
            finding.detail["variant"] = "flaky"
            finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
            finding.weight = 1.0
            finding.interval = finding.interval or finding.run
            finding.summary = finding.summary.replace(
                "ordinary failure", "it passed on a plain re-run, with nothing changed"
            )
    # Another runner for the same job passed with nothing changed (``green_as``): in
    # ``RUNNER_MIN_SESSIONS`` sessions or more, the failing command is the wrong one here.
    # The same command with a variable set in front passed, nothing changed
    # (``green_with``: `TZ=UTC pytest` after a red `pytest`): in ``ENV_MIN_SESSIONS``
    # sessions, counting those where another runner passed unchanged, the bare command
    # is missing what that variable gives it.
    prefixed = [f for f in findings if "green_with" in f.detail]
    envs: dict[str, set[str]] = {}
    for finding in [*prefixed, *(f for f in findings if "green_as" in f.detail)]:
        for key in _command_keys(finding):
            envs.setdefault(key, set()).add(finding.unit)
    for finding in prefixed:
        form = finding.detail.pop("green_with")
        if finding.detail.get("variant") != "ordinary" or not any(
            len(envs.get(key, ())) >= ENV_MIN_SESSIONS for key in _command_keys(finding)
        ):
            continue
        finding.detail["works_with"] = form
        finding.detail["variant"] = "invocation"
        finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
        finding.weight = 1.0
        finding.interval = finding.interval or finding.run
        finding.summary = finding.summary.replace(
            "ordinary failure", f"`{form}` then passed, with nothing changed"
        )
    env_keys = {key for key, units in envs.items() if len(units) >= ENV_MIN_SESSIONS}
    runners: dict[tuple[str, str], set[str]] = {}
    swapped = [f for f in findings if "green_as" in f.detail]
    for finding in swapped:
        for key in _command_keys(finding):
            runners.setdefault((key, finding.detail["green_as"]), set()).add(finding.unit)
    for finding in swapped:
        other = finding.detail.pop("green_as")
        keys = _command_keys(finding)
        if finding.detail.get("variant") != "ordinary" or not any(
            len(runners.get((key, other), ())) >= RUNNER_MIN_SESSIONS
            # ... or fewer, when the same command also passed with a variable set
            or (key in env_keys and any("works_with" in f.detail for f in prefixed))
            for key in keys
        ):
            continue
        finding.detail["recovered_by"] = other
        finding.detail["variant"] = "invocation"
        finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
        finding.weight = 1.0
        finding.interval = finding.interval or finding.run
        finding.summary = finding.summary.replace(
            "ordinary failure", f"`{other}` then passed, with nothing changed"
        )
    # Red where the agent first ran it, green unchanged in a subdirectory (``green_in``):
    # from ``RUNNER_MIN_SESSIONS`` sessions on, that is where the command has to be run.
    places: dict[tuple[str, str], set[str]] = {}
    moved = [f for f in findings if "green_in" in f.detail]
    for finding in moved:
        for key in _command_keys(finding):
            places.setdefault((key, finding.detail["green_in"]), set()).add(finding.unit)
    for finding in moved:
        place = finding.detail.pop("green_in")
        if finding.detail.get("variant") != "ordinary" or not any(
            len(places.get((key, place), ())) >= RUNNER_MIN_SESSIONS
            for key in _command_keys(finding)
        ):
            continue
        finding.detail["works_in"] = place
        finding.detail["variant"] = "invocation"
        finding.detail.pop("own_module", None)
        finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
        finding.weight = 1.0
        finding.interval = finding.interval or finding.run
        finding.summary = finding.summary.replace(
            "ordinary failure", f"it then passed unchanged in `{place}`"
        )
    # The same test red in ``STANDING_TEST_MIN_SESSIONS`` episodes or more, none of which
    # made it pass: a standing failure. It is no longer "a red test during development".
    red_tests: dict[tuple[str, str], set[str]] = {}
    on_arrival: set[tuple[str, str]] = set()  # red before the session changed anything
    naming = [
        f
        for f in findings
        if f.signal == signals.COMMAND_FAILURE
        and "failed_tests" in f.detail
        and f.detail.get("variant") == "ordinary"
        # A test that went green after a setup step was not left red: something in the
        # session did make it pass, and that step is the advice (`fixed_by_setup`).
        and "fixed_by_setup" not in f.detail
        and "fixed_by_command" not in f.detail
        # ... and one that passed on an untouched re-run is perhaps flaky, never
        # "red session after session, and nothing made it pass".
        and "rerun_green" not in f.detail
    ]
    for finding in naming:
        for key in _command_keys(finding):
            for test in finding.detail["failed_tests"].split("\n"):
                red_tests.setdefault((key, test), set()).add(finding.unit)
                if "after_edit" not in finding.detail:
                    on_arrival.add((key, test))
    for finding in naming:
        tests = finding.detail["failed_tests"].split("\n")
        files = finding.detail.get("failed_test_files", "").split("\n")
        for test, rel in zip(tests, files, strict=False):
            # ... unless every red run came after the agent's own change in its session:
            # then the agent may have broken it each time, and "mark it as known to
            # fail" would be the wrong advice.
            if not any(
                len(red_tests.get((key, test), ())) >= STANDING_TEST_MIN_SESSIONS
                and (key, test) in on_arrival
                for key in _command_keys(finding)
            ):
                continue
            finding.detail.setdefault("standing_test", test)
            finding.weight = 1.0
            finding.interval = finding.interval or finding.run
            if rel and all(t.key != rel for t in finding.targets):
                finding.targets.append(Target(model.FILE, rel))
        if "standing_test" in finding.detail:
            finding.summary = finding.summary.replace(
                "ordinary failure", "a test that is red session after session"
            )
    # A push turned down because the remote had moved on: routine once, and a habit worth
    # a line of guidance from ``RECURRING_RED_MIN_SESSIONS`` sessions on.
    # So is a first push of a new branch without `-u`, put right with `--set-upstream`.
    dropped: list[Finding] = []
    for habit in ("push_rejected", "no_upstream", "hook_rewrote"):
        pushes: dict[str, set[str]] = {}
        turned_down = [
            f
            for f in findings
            if habit in f.detail and (habit == "push_rejected" or "push_recovered" in f.detail)
        ]
        for finding in turned_down:
            for key in _command_keys(finding):
                pushes.setdefault(key, set()).add(finding.unit)
        for finding in turned_down:
            if any(
                len(pushes.get(key, ())) >= RECURRING_RED_MIN_SESSIONS
                for key in _command_keys(finding)
            ):
                finding.detail[habit] = "recurring"
                if habit == "hook_rewrote":
                    finding.summary = re.sub(
                        r" failed \(.*$",
                        " was stopped by a pre-commit hook that rewrote files; it went "
                        "through on the next attempt",
                        finding.summary,
                        count=1,
                    )
        # ... and one that was put right at once, and is no habit, was the loop working.
        dropped += [
            f
            for f in turned_down
            if f.detail.get(habit) != "recurring" and "push_recovered" in f.detail
        ]
    for finding in pending:
        fix = finding.detail.pop("fixed_by_command")
        recurs = any(
            len(pairs.get((key, fix), ())) >= scoring.MEDIUM_MIN_SESSIONS
            for key in _command_keys(finding)
        )
        if recurs:
            finding.detail["fixed_by_setup"] = fix
            finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
            finding.weight = 1.0
            finding.interval = finding.interval or finding.run
            finding.summary += "; it passed after another command, with no file edited"
        else:
            dropped.append(finding)
    gone = {id(f) for f in dropped}
    findings[:] = [f for f in findings if id(f) not in gone]

    red: dict[str, set[str]] = {}
    stuck = [
        f
        for f in findings
        if f.signal == signals.COMMAND_FAILURE
        and f.detail.get("never_green")
        and f.detail.get("variant") == "ordinary"
        and "inline" not in f.detail
    ]
    runs: dict[tuple[str, str], int] = {}
    for finding in stuck:
        for key in _command_keys(finding):
            runs[key, finding.unit] = runs.get((key, finding.unit), 0) + 1
    for (key, session), count in runs.items():
        if count >= RECURRING_RED_MIN_RUNS:
            red.setdefault(key, set()).add(session)
    for finding in stuck:
        if any(
            len(red.get(key, ())) >= RECURRING_RED_MIN_SESSIONS and finding.unit in red[key]
            for key in _command_keys(finding)
        ):
            finding.detail["recurring_red"] = "1"
            finding.interval = finding.interval or finding.run
    # A module of the repository itself that cannot be imported, and the agent gives up
    # there: once, perhaps the code was broken. Ending session after session, it is how
    # the command is run here that is wrong.
    stranded = [
        f
        for f in findings
        if f.signal == signals.COMMAND_FAILURE
        and "own_module" in f.detail
        and f.detail.get("never_green")
        and f.detail.get("variant") == "ordinary"
    ]
    ended: dict[str, set[str]] = {}
    for finding in stranded:
        for key in _command_keys(finding):
            ended.setdefault(key, set()).add(finding.unit)
    for finding in stranded:
        if not any(
            len(ended.get(key, ())) >= RECURRING_RED_MIN_SESSIONS for key in _command_keys(finding)
        ):
            continue
        del finding.detail["own_module"]
        finding.detail["variant"] = "invocation"
        finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
        finding.weight = 1.0
        finding.interval = finding.interval or finding.run
        finding.summary = finding.summary.replace("ordinary failure", "setup/invocation error")
    return dropped


def _attribute_interrupted_commands(findings: Sequence[Finding]) -> None:
    """A command the human stops session after session becomes a (light) command row.

    One interrupt is about the work in progress. The same command stopped in
    three sessions is about the command: it is too slow, or should not be run.
    """
    by_key: dict[str, list[Finding]] = {}
    for finding in findings:
        key = finding.detail.get("interrupted", "")
        if finding.signal == signals.USER_INTERRUPT and key:
            by_key.setdefault(key, []).append(finding)
    for key, found in by_key.items():
        if len({f.unit for f in found}) < INTERRUPTED_COMMAND_MIN_SESSIONS:
            continue
        for finding in found:
            finding.targets = [
                *finding.targets,
                Target(model.COMMAND, key, INTERRUPTED_COMMAND_SHARE),
            ]


def _is_git(finding: Finding) -> bool:
    return finding.signal == signals.HUMAN_REWORK


def _beside_ordinary(items: Sequence[Item]) -> list[Item]:
    """The evidence left when ordinary command failures are set aside (all of it if none)."""
    return [
        (f, share)
        for f, share in items
        if not scoring.is_ordinary_failure(f) and not scoring.is_guardrail_hit(f)
    ]


def _score(items: Sequence[Item], exposed: set[str]) -> scoring.Score:
    """Score a target. Git evidence may raise a score, never lower it.

    A rework finding adds a friction "session" (the agent commit) that can pull
    the mean down, so the score from transcripts alone is the floor.

    Ordinary command failures count in full only in episodes that have other
    evidence too. In episodes of their own they may lift the score no further than
    the cap for weak evidence: eight red test runs in other sessions next to one
    real error are one real error.
    """
    sessions = exposed | {f.unit for f, _ in items}
    full = scoring.score_items(items, len(sessions))
    if scoring.weak_only(f for f, _ in items):
        # Nothing but signals that also accompany productive work: a lead, never an alarm.
        return dataclasses.replace(full, score=min(full.score, scoring.WEAK_ONLY_SCORE_CAP))
    rest = _beside_ordinary(items)
    if len(rest) < len(items):
        beside = {f.unit for f, _ in rest}
        kept = [(f, share) for f, share in items if f.unit in beside]
        ceiling = max(scoring.score_items(kept, len(sessions)).score, scoring.WEAK_ONLY_SCORE_CAP)
        if full.score > ceiling:
            full = dataclasses.replace(full, score=ceiling)
    transcript_items = [(f, share) for f, share in items if not _is_git(f)]
    if len(transcript_items) == len(items) or not transcript_items:
        return full
    floor = scoring.score_items(
        transcript_items, len(exposed | {f.unit for f, _ in transcript_items})
    )
    return full if full.score >= floor.score else floor


def list_transcripts(options: AnalyzeOptions) -> list[Path]:
    """The transcript files ``analyze(options)`` would read, in the order it reads them.

    Raises ``NoTranscriptsError`` exactly when ``analyze`` would: nothing found,
    or an explicitly given file that is not a JSONL transcript.
    """
    return _Analysis(options)._transcript_files()


_FILE_OPS = (model.OP_READ, model.OP_EDIT, model.OP_WRITE)
#: A command key with the directory it ran in: ``npm test (web/)``.
_LOCATED_KEY_RE = re.compile(r"^(.+) \((.+)/\)$")
#: Details of a command failure that hold another command's key (or its directory).
_COMMAND_DETAILS = (
    "recovered_by",
    "fixed_by_setup",
    "works_with",
    "works_in",
    "green_in",
    "typo_of",
)


def _session_root(cwds: list[str], files: list[str], too_wide: Callable[[str], bool]) -> str:
    """The recorded working directory that looks most like the session's repository root."""

    def fold(path: str) -> str:
        return posixpath.normpath(to_posix(path)).rstrip("/").casefold() or "/"

    def above(parent: str, child: str) -> bool:
        return child == parent or child.startswith(parent.rstrip("/") + "/")

    usual = max(dict.fromkeys(cwds), key=cwds.count)
    distinct = list(dict.fromkeys(cwds))
    narrow = [c for c in distinct if not too_wide(c)] or distinct
    absolute = [fold(f) for f in files if f.startswith(("/", "~")) or is_windows_path(f)]
    holding = [c for c in narrow if absolute and all(above(fold(c), f) for f in absolute)]
    if holding:
        return max(holding, key=lambda c: len(fold(c)))  # the deepest that holds every file
    if absolute:
        # no recorded directory holds them all (`cd web` came first): the directory the
        # files share, if the session worked beneath it
        shared = posixpath.commonpath(absolute) if len({f[:1] for f in absolute}) == 1 else ""
        spelled = posixpath.normpath(to_posix(usual))
        cut = "/".join(spelled.split("/")[: len(shared.split("/"))]) if shared else ""
        if cut and fold(cut) == shared and above(shared, fold(usual)) and not too_wide(cut):
            return cut
    over = [c for c in narrow if above(fold(c), fold(usual))]
    return min(over, key=lambda c: len(fold(c))) if over else usual


def session_directories(
    options: AnalyzeOptions, limit: int = 5, *, mask_home: bool = True
) -> list[tuple[str, int]]:
    """Where the sessions that did *not* touch the repository ran: ``(directory, sessions)``.

    For "found transcripts, but no agent session touched this repository": the
    root directories of the sessions that were set aside, most frequent
    first (then by name), at most ``limit``, with the home directory shown as
    ``~``. If one of them is where the repository used to live, it is what
    ``repo_aliases`` (``--repo-alias``) wants. The time window is not applied.

    A session's root is a guess at its repository root, since that is what an
    alias must name: of the working directories the session recorded, the
    deepest one that holds every file it read or edited; with no such file, the
    shortest one above the directory it spent most of its events in (an agent
    starts at the root and ``cd``s down). The home directory and ``/`` are passed
    over when the session recorded anything below them.
    ``mask_home=False`` gives the paths as recorded, for a message printed on the
    machine running the scan (a ``~`` would name the wrong home there).
    """
    analysis = _Analysis(options)
    seen: dict[str, str] = {}
    sessions: dict[str, set[str]] = {}
    for path in analysis._transcript_files():
        try:
            item = parse_transcript(path, seen)
        except (OSError, ValueError):
            continue
        for stream in item.streams:
            if analysis._in_repo(stream):
                continue
            cwds = [event.cwd for event in stream.events if event.cwd]
            if not cwds:
                continue
            common = _session_root(
                cwds,
                [e.path for e in stream.events if e.path and e.op in _FILE_OPS],
                lambda c: analysis.display_path(c) in ("~", "/"),
            )
            folded = posixpath.normpath(to_posix(common))
            # Mask the home in the recorded spelling: a Windows path folded to
            # C:/Users/... first would come out as "C:~/...".
            shown = posixpath.normpath(analysis.display_path(common)) if mask_home else folded
            sessions.setdefault(shown, set()).add(stream.session_id)
    ranked = sorted(sessions.items(), key=lambda pair: (-len(pair[1]), pair[0]))
    return [(directory, len(ids)) for directory, ids in ranked[:limit]]


def inspect_sources(options: AnalyzeOptions) -> list[dict[str, Any]]:
    """What each transcript file ``analyze`` would read held, one entry per file.

    ``path``, ``events`` (what the parser understood, before any filtering by
    repository or time window), ``skipped_lines``, ``duplicate_lines`` (lines
    that an earlier file had already contributed: a copy or a re-export of a
    log adds nothing, and is not "empty") and ``encoding``. A file that could
    not be read has ``error``.
    """
    seen: dict[str, str] = {}
    report: list[dict[str, Any]] = []
    for path in list_transcripts(options):
        entry: dict[str, Any] = {
            "path": path,
            "events": 0,
            "skipped_lines": 0,
            "duplicate_lines": 0,
            "encoding": "utf-8",
        }
        try:
            entry["encoding"] = model.text_encoding(path)
            item = parse_transcript(path, seen)
        except (OSError, ValueError) as exc:
            entry["error"] = read_error(exc) if isinstance(exc, OSError) else "not readable"
        else:
            entry["events"] = sum(len(s.events) for s in item.streams)
            entry["skipped_lines"] = item.skipped_lines
            entry["duplicate_lines"] = item.duplicate_lines
        report.append(entry)
    return report


def inspect_transcripts(options: AnalyzeOptions) -> list[tuple[Path, int, int]]:
    """``(file, events, skipped_lines)`` for every transcript ``analyze`` would read.

    ``events`` is what the parser understood of the file (tool calls, results,
    human messages), before any filtering by repository or time window: 0
    means the file is empty, binary, or not a transcript.
    """
    seen: dict[str, str] = {}
    report: list[tuple[Path, int, int]] = []
    for path in list_transcripts(options):
        try:
            item = parse_transcript(path, seen)
        except (OSError, ValueError):
            report.append((path, 0, 0))
            continue
        report.append((path, sum(len(s.events) for s in item.streams), item.skipped_lines))
    return report


def _encoding_note(path: Path) -> str:
    """What to say about the encoding of a file in which no JSON was found."""
    try:
        encoding = model.text_encoding(path)
        with path.open("rb") as handle:
            binary = b"\x00" in handle.read(4096)
    except OSError:
        return ""
    if encoding != "utf-8":
        return f" (it was read as {encoding.upper()}, judging by its first bytes)"
    if binary:
        return (
            " (it contains NUL bytes: it is binary, or in an encoding other than UTF-8, "
            "UTF-16 or UTF-32, which are the ones that are read)"
        )
    return ""


def _looks_like_jsonl(path: Path) -> bool:
    """True if one of the first lines of ``path`` is a JSON object."""
    try:
        with model.open_transcript(path) as handle:
            for _, line in zip(range(JSONL_PROBE_LINES), handle, strict=False):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    if isinstance(json.loads(line), dict):
                        return True
                except (ValueError, RecursionError):
                    continue
    except OSError:
        return False
    return False


#: Lines of an explicitly given file that are checked for a JSON object.
JSONL_PROBE_LINES = 200


def _source_root(item: ParsedFile) -> Path:
    """Claude Code files are grouped by project directory; other files stand alone."""
    if item.kind != claude_code.KIND:
        return item.path
    if item.path.parent.name == "subagents":
        return item.path.parent.parent.parent
    return item.path.parent


class _Analysis:
    def __init__(self, options: AnalyzeOptions) -> None:
        self.options = options
        # `..` and `sub/..` are folded once, here: the name and the root shown are those
        # of the directory itself. Symbolic links are kept (their targets are roots too).
        self.repo = Path(os.path.normpath(options.repo.expanduser().absolute()))
        self.since = _utc(options.since)
        self.until = _utc(options.until)
        roots = repo_roots(self.repo)
        #: The repository's name: the main worktree's, also when scanned from a linked one.
        self.name = repo_name(self.repo)
        self.roots = roots
        #: Calls the agent tool refused by its own rules (``scoring.is_agent_tool_rule``).
        self.tool_rules: list[Finding] = []
        self.resolver = PathResolver(
            self.repo, [*roots[1:], str(options.repo), *options.repo_aliases]
        )
        self.touched: set[str] = set()
        self._is_file_cache: dict[str, bool | None] = {}
        self._facts: dict[str, recommend.FileFacts] = {}
        # With an alias the sessions were recorded where this copy does not live: what
        # lies above the copy is not what those agents loaded.
        self.guidance = Guidance(self.repo, parents=not options.repo_aliases)
        self.repo_advice: list[dict[str, object]] = []
        self.outside: dict[Path, set[str]] = {}
        self.skipped_events: dict[str, int] = {}
        self.source_labels: dict[str, str] = {}
        self.exposure: dict[tuple[str, str], set[str]] = {}  # by episode of work
        self.episodes: dict[str, list[float]] = {}
        self.unit_session: dict[str, str] = {}
        self.git_used = False
        self.dropped: list[tuple[int, float, str]] = []
        self.events_outside_repo = 0
        self.events_elsewhere_in_repo = 0
        self._dirs_by_name: dict[str, list[str]] | None = None
        self.untimed_sessions_excluded = 0
        self.unreadable: list[tuple[Path, str]] = []
        self.third_party_events = 0
        self._ignored: set[str] | None = None
        self.existed: set[str] = set()  # paths (and their parents) the agent really used

    # ---- repository knowledge --------------------------------------------------------

    def _disk_is_file(self, rel: str) -> bool | None:
        """True/False for an existing file/directory, ``None`` if the path is absent."""
        if rel not in self._is_file_cache:
            path = self.repo / rel
            try:
                self._is_file_cache[rel] = (
                    True if path.is_file() else (False if path.is_dir() else None)
                )
            except OSError:
                self._is_file_cache[rel] = None
        return self._is_file_cache[rel]

    def workspace_dir(self, name: str) -> str | None:
        """The one directory called ``name`` in the repository (``packages/web``), if any.

        Looked for ``WORKSPACE_DEPTH`` levels down, past hidden, third-party and build
        directories. More than one match is no answer.
        """
        if self._dirs_by_name is None:
            index: dict[str, list[str]] = {}
            root = str(self.repo)
            seen = 0
            try:
                for current, names, _ in os.walk(root):
                    rel = os.path.relpath(current, root).replace(os.sep, "/")
                    depth = 0 if rel == "." else rel.count("/") + 1
                    names[:] = sorted(
                        n
                        for n in names
                        if not n.startswith(".")
                        and n not in THIRD_PARTY_DIRS
                        and n not in BUILD_DIRS
                        and depth < WORKSPACE_DEPTH
                    )
                    for child in names:
                        index.setdefault(child, []).append(
                            child if rel == "." else f"{rel}/{child}"
                        )
                    seen += len(names)
                    if seen > WORKSPACE_MAX_DIRS:
                        break
            except OSError:
                pass
            self._dirs_by_name = index
        found = self._dirs_by_name.get(name.rsplit("/", 1)[-1], [])
        found = [rel for rel in found if rel == name or rel.endswith("/" + name)]
        return found[0] if len(found) == 1 else None

    def first_party(self, top: str) -> bool:
        """Is ``top`` a top-level module or package of this repository?"""
        candidates = (top, f"{top}.py", f"src/{top}", f"src/{top}.py", f"lib/{top}")
        if any(self._disk_is_file(rel) is not None for rel in candidates):
            return True
        prefixes = (f"{top}/", f"src/{top}/", f"lib/{top}/")
        return any(rel.startswith(prefixes) or rel in candidates for rel in self.touched)

    def is_file(self, rel: str) -> bool:
        on_disk = self._disk_is_file(rel)
        if on_disk is not None:
            return on_disk
        return rel in self.touched or "." in rel.rsplit("/", 1)[-1].lstrip(".")

    def public_command(self, key: str) -> str:
        """A command key without excerpts: project-specific names hashed, repo paths kept.

        Repo paths are shown everywhere else in such a report, so a script of the
        repository (``scripts/deploy.sh``) stays readable in a command key too.
        So is the repository's own name, which every report carries in its title: a
        command called like the project (``shopfront serve``) keeps that word.
        """
        own = re.sub(r"[-_.]", "-", self.repo.name).casefold()

        def in_repo(word: str) -> bool:
            if own and re.sub(r"[-_.]", "-", word).casefold() == own:
                return True
            rel = word[2:] if word.startswith("./") else word
            return (
                "/" in word and not word.startswith(("/", "~")) and self._disk_is_file(rel) is True
            )

        return public_key(key, in_repo)

    def unit(self, session_id: str, ts: datetime | None) -> str:
        """The episode of work an event belongs to: the session id, or ``<id>#<n>``."""
        starts = self.episodes.get(session_id)
        if not starts or ts is None:
            return session_id
        number = bisect.bisect_right(starts, ts.timestamp())
        if not number:
            return session_id
        unit = f"{session_id}#{number + 1}"
        self.unit_session[unit] = session_id
        return unit

    def sessions_of(self, units: Iterable[str]) -> set[str]:
        return {self.unit_session.get(unit, unit) for unit in units}

    def third_party(self, rel: str, directory: bool = False) -> bool:
        """Is ``rel`` other people's code or build output (``node_modules/``, ``.venv/``)?

        Decided by directory names, and, where git is in use, by ``.gitignore``: a
        path inside an ignored directory is not the repository's own code.
        """
        parts = rel.split("/")
        if any(part in THIRD_PARTY_DIRS or part.endswith(".egg-info") for part in parts):
            return True
        ancestors = {"/".join(parts[:depth]) for depth in range(1, len(parts))}
        if directory or self._disk_is_file(rel) is False:
            ancestors.add(rel)
        if self._ignored is not None:
            return bool(ancestors & self._ignored)
        return any(part in BUILD_DIRS for part in (parts if directory else parts[:-1]))

    def _drop_third_party(
        self, findings: list[Finding], exposure: dict[tuple[str, str], set[str]]
    ) -> list[Finding]:
        """Take files and directories that are not the repository's own out of the picture."""
        seen = {(t.kind, t.key) for f in findings for t in f.targets if t.kind != model.COMMAND}
        seen |= {(kind, key) for kind, key in exposure if kind != model.COMMAND}
        paths = {key for _, key in seen}
        if self.options.include_git and paths:
            directories = {"/".join(p.split("/")[:d]) for p in paths for d in range(1, 12)}
            if (self.repo / ".git").exists():
                self._ignored = gitsignals.ignored_directories(self.repo, sorted(directories))
        outside = {key for kind, key in seen if self.third_party(key, kind == model.DIR)}
        if not outside:
            return findings
        for kind, key in list(exposure):
            if kind != model.COMMAND and key in outside:
                del exposure[(kind, key)]
        kept: list[Finding] = []
        for finding in findings:
            own = [t for t in finding.targets if t.kind == model.COMMAND or t.key not in outside]
            if finding.targets and not own:
                self.third_party_events += 1
                continue
            finding.targets = own
            kept.append(finding)
        return kept

    def public_path(self, rel: str) -> str:
        """``rel`` as it may be shown without excerpts.

        Repo paths are shown in such a report; a path the agent only guessed is not a
        repo path but text the agent wrote. From the first component that is neither
        on disk nor known to have existed (a successful read, edit or write went
        through it), every component becomes ``<path:hash>``.
        """
        if rel in ("", "."):
            return rel
        parts = rel.split("/")
        shown: list[str] = []
        real = True
        for depth, part in enumerate(parts, 1):
            prefix = "/".join(parts[:depth])
            real = real and (prefix in self.existed or self._disk_is_file(prefix) is not None)
            digest = hashlib.sha256(part.encode()).hexdigest()[:PUBLIC_HASH_DIGITS]
            shown.append(part if real else f"<path:{digest}>")
        return "/".join(shown)

    def _public_target(self, kind: str, key: str) -> str:
        if kind != model.COMMAND:
            return self.public_path(key)
        # A command key names the directory it was pointed at (`npm test (web/)`,
        # also through `make -C`, `npm --prefix`, `pushd`). A command that failed
        # proves nothing about that directory: it is shown only if it is there now
        # or a file under it was really read or written.
        located = _LOCATED_KEY_RE.match(key)
        if located is None:
            return key
        return f"{located.group(1)} ({self.public_path(located.group(2))}/)"

    def _hide_guessed_paths(
        self, findings: Sequence[Finding], exposure: dict[tuple[str, str], set[str]]
    ) -> None:
        """Without excerpts, no path that exists only in the agent's guess is shown."""
        for finding in findings:
            for index, target in enumerate(finding.targets):
                shown = self._public_target(target.kind, target.key)
                if shown != target.key:
                    finding.summary = finding.summary.replace(target.key, shown)
                    finding.targets[index] = Target(target.kind, shown, target.share)
            for name in _COMMAND_DETAILS:  # other commands the event names, as data
                value = finding.detail.get(name)
                if value and name in ("works_in", "green_in"):
                    finding.detail[name] = self.public_path(value.rstrip("/")) + "/"
                elif value:
                    finding.detail[name] = " && ".join(
                        self._public_target(model.COMMAND, part) for part in value.split(" && ")
                    )
        for kind, key in list(exposure):
            shown = self._public_target(kind, key)
            if shown != key:
                exposure.setdefault((kind, shown), set()).update(exposure.pop((kind, key)))

    def in_repo_call(self, event: model.Event) -> bool:
        """Did this tool call act inside the repository (not on a neighbouring one)?"""
        resolver = self.resolver
        if event.op == model.OP_SHELL:
            return signals.runs_inside(resolver, event)
        if event.path is not None:
            return resolver.resolve(event.path, event.cwd) is not None
        return resolver.contains(event.cwd) if event.cwd else True

    def is_known_file(self, rel: str) -> bool:
        return rel in self.touched or self._disk_is_file(rel) is True

    def facts(self, rel: str) -> recommend.FileFacts:
        if rel not in self._facts:
            # `build/`, `gen/`, `out/` hold build output only where git does not track
            # them: one question to git per such path, and only for those.
            untracked = recommend.in_output_directory(rel) and gitsignals.is_untracked(
                self.repo, rel
            )
            self._facts[rel] = recommend.file_facts(self.repo, rel, untracked)
        return self._facts[rel]

    # ---- text helpers ----------------------------------------------------------------

    def clean(self, text: str) -> str:
        return redact.one_line(text, 2 * self.options.excerpt_chars)

    def quote(self, text: str, limit: int) -> str:
        return redact.one_line(text, min(limit, max(self.options.excerpt_chars, 1)))

    def _denied_by_rule(self) -> Callable[[str, Sequence[str], str], bool]:
        """Does a ``permissions.deny`` rule of the repository's settings refuse a call?"""
        rules = deny_rules(self.repo)
        return lambda tool, commands, rel: rule_denies(rules, tool, commands, rel)

    def _enclosing(self) -> Callable[[str], bool]:
        """Is an absolute path in the git repository the scanned directory is part of?"""
        tops = [to_posix(str(top)) for top in enclosing_repositories(self.repo)]

        def inside(absolute: str) -> bool:
            return any(absolute == top or absolute.startswith(top + "/") for top in tops)

        return inside

    def _delegated(self, streams: Sequence[Stream]) -> Callable[[str, float, float], list[str]]:
        """Which repo files the subagents of a session changed between two moments.

        A subagent's edits are in a stream of their own. For the agent that launched
        it they are what its Task call did, and a correction that follows is about
        them as much as about its own edits.
        """
        changes: dict[str, list[tuple[float, str]]] = {}
        for stream in streams:
            if not stream.sidechain:
                continue
            calls: dict[str, model.Event] = {}
            for event in stream.events:
                if event.kind == model.TOOL_CALL and event.op in (model.OP_EDIT, model.OP_WRITE):
                    calls[event.call_id] = event
                elif event.kind == model.TOOL_RESULT and event.ok and event.call_id in calls:
                    call = calls.pop(event.call_id)
                    when = event.ts or call.ts
                    rel = self.resolver.resolve(call.path, call.cwd) if call.path else None
                    if rel and when is not None:
                        changes.setdefault(stream.session_id, []).append((when.timestamp(), rel))

        def between(session: str, start: float, end: float) -> list[str]:
            found = [rel for at, rel in changes.get(session, ()) if start <= at <= end]
            return list(dict.fromkeys(found))

        return between

    def display_path(self, path: Path | str) -> str:
        """Home shown as ``~`` and forward slashes on every platform."""
        return redact.redact_home(str(path)).replace("\\", "/")

    def source_group(self, item: ParsedFile) -> str:
        """How a transcript source is named in the result.

        A Claude Code project directory is named after the absolute path of the
        working directory (user name, client, project), so it is never shown:
        the repository's own name stands in for it.
        """
        name = self.name
        if item.kind == claude_code.KIND:
            return f"{claude_code.KIND}:{name}"
        if not self.options.include_excerpts:
            return f"{item.kind}:{item.path.name}"
        shown = self.display_path(item.path)
        if not Path(shown).is_absolute() and not re.match(r"^[A-Za-z]:/", shown):
            return shown  # given relatively, or under the home directory (`~/...`)
        # An absolute path says where on this machine the logs live (user, client,
        # project). Name the file by where it is in what was passed instead.
        path = item.path.absolute()
        for given in [Path.cwd(), *(Path(g).expanduser() for g in self.options.transcripts or ())]:
            root = given.absolute()
            if root.is_dir() and path.is_relative_to(root):
                inside = path.relative_to(root).as_posix()
                return inside if root == Path.cwd() else f"{root.name}/{inside}"
        return item.path.name

    def source_label(self, item: ParsedFile) -> str:
        """One transcript file, as cited by evidence (with a line number next to it)."""
        if item.kind != claude_code.KIND:
            return self.source_group(item)
        try:
            inside = item.path.relative_to(_source_root(item)).as_posix()
        except ValueError:
            inside = item.path.name
        return f"{self.source_group(item)}/{inside}"

    def cited_source(self, finding: Finding) -> str | None:
        """Where a finding can be looked up, or ``None`` when paths are to be left out."""
        if finding.source not in self.source_labels:
            return finding.source  # "git"
        return self.source_labels[finding.source] if self.options.include_excerpts else None

    # ---- pipeline --------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        options = self.options
        missing: NoTranscriptsError | None = None
        try:
            files = self._transcript_files()
        except BadTranscriptError:
            raise  # a mistyped path is a mistake, not an empty scan
        except NoTranscriptsError as exc:
            if not options.allow_no_transcripts:
                raise
            missing, files = exc, []
        untimed: list[Stream] = []
        parsed, context = _load(files, self.since, self.until, self.unreadable, untimed)
        self.untimed_sessions_excluded = len({s.session_id for s in untimed if self._in_repo(s)})
        for item in parsed:
            # Sessions that never worked in this repo are not this repo's sessions.
            relevant = [s for s in item.streams if self._in_repo(s)]
            self.outside[item.path] = {s.session_id for s in item.streams} - {
                s.session_id for s in relevant
            }
            item.streams = relevant
        streams = [s for item in parsed for s in item.streams]
        self.source_labels = {str(item.path): self.source_label(item) for item in parsed}
        for stream in streams:
            asked: dict[str, str] = {}
            for event in stream.events:
                if event.kind == model.TOOL_CALL and event.op in (
                    model.OP_READ,
                    model.OP_EDIT,
                    model.OP_WRITE,
                ):
                    rel = self.resolver.resolve(event.path, event.cwd)
                    if rel:
                        self.touched.add(rel)
                        asked[event.call_id] = rel
                elif event.kind == model.TOOL_RESULT and event.ok and event.call_id in asked:
                    parts = asked[event.call_id].split("/")
                    self.existed.update("/".join(parts[:d]) for d in range(1, len(parts) + 1))

        self.episodes = episode_starts(streams)
        ctx = signals.DetectContext(
            self.resolver,
            self.is_file,
            self.is_known_file,
            # Without excerpts no project-specific name may survive in a command key.
            command_key=str if options.include_excerpts else self.public_command,
            tool_name=str if options.include_excerpts else public_tool,
            first_party=self.first_party,
            unit=self.unit,
            exists=lambda rel: self._disk_is_file(rel) is not None,
            workspace_dir=self.workspace_dir,
            delegated=self._delegated(streams),
            enclosing=self._enclosing(),
            denied_by_rule=self._denied_by_rule(),
        )
        findings: list[Finding] = []
        dropped: list[tuple[int, float, str]] = []
        exposure: dict[tuple[str, str], set[str]] = {}
        for stream in streams:
            detection = signals.detect_stream(stream, ctx)
            outside = context.get(stream.key)
            if outside:
                # Judge the window with what happened before it in view (the edit a
                # correction refers to, the red run an edit answers) and what happened
                # after it (the fix, the recovery), but count and expose only what lies
                # inside it.
                before, after = outside
                inside = {event.line for event in stream.events}
                whole = dataclasses.replace(stream, events=[*before, *stream.events, *after])
                full = signals.detect_stream(whole, ctx)
                detection = dataclasses.replace(
                    detection,
                    findings=[f for f in full.findings if f.line in inside],
                    skipped_events=full.skipped_events,
                    dropped=[d for d in full.dropped if d[0] in inside],
                    outside=[line for line in full.outside if line in inside],
                    nearby=[line for line in full.nearby if line in inside],
                )
            self.events_outside_repo += len(detection.outside)
            self.events_elsewhere_in_repo += len(detection.nearby)
            findings.extend(detection.findings)
            dropped.extend(detection.dropped)
            source_key = str(stream.source)
            self.skipped_events[source_key] = (
                self.skipped_events.get(source_key, 0) + detection.skipped_events
            )
            for kind, key, unit in detection.exposed_in:
                self._expose(exposure, kind, key, unit)

        sources = self._sources(parsed)
        if options.include_git:
            history = gitsignals.read_history(
                self.repo,
                options.git_max_commits,
                options.agent_commit_patterns,
                options.bot_commit_patterns,
            )
            source: dict[str, Any] = {
                "kind": "git",
                "path": self.display_path(self.repo),
                "sessions": 0,
                "skipped_lines": 0,
                "skipped_events": 0,
                "ignored_lines": 0,
                "available": history.available,
            }
            if history.available:
                candidates = [
                    f
                    for f in gitsignals.find_rework(
                        history.commits,
                        options.rework_window_days,
                        gitsignals.HunkReader(self.repo),
                    )
                    if _in_window(f.ts, self.since, self.until)
                    # ... and a file that has since been renamed away or deleted is not
                    # a place anyone can act on.
                    and all(self.facts(t.key).exists for t in f.targets)
                ]
                # A lockfile or a compiled artefact (by its name: `uv.lock`, `*_pb2.py`,
                # `*.min.js`) is rewritten by tools as a matter of course: a human
                # `chore: bump` after an agent commit is not rework. Set aside, and
                # counted. A "generated" header alone never sets rework aside: people do
                # rewrite such files by hand, and that is worth knowing.
                rework = [
                    f
                    for f in candidates
                    if not any(
                        self.facts(t.key).generated_by == recommend.GENERATED_BY_NAME
                        for t in f.targets
                    )
                ]
                source["rework_set_aside"] = len(candidates) - len(rework)
                # A commit made during a transcript session is that session's work, not a
                # separate one. Agent commits never add exposure on their own.
                committed = {c.session_id: c.date for c in history.commits if c.is_agent}
                spans = _session_spans(streams)
                for finding in rework:
                    owner = _session_at(spans, committed.get(finding.session_id))
                    if owner is not None:
                        finding.detail["agent_commit_session"] = finding.session_id
                        finding.session_id = owner
                findings.extend(rework)
                source["sessions"] = sum(
                    1
                    for c in history.commits
                    if c.is_agent and _in_window(c.date, self.since, self.until)
                )
            else:
                source["note"] = history.reason
            sources.append(source)
            self.git_used = history.available
        # Neither transcripts nor git history: with ``allow_no_transcripts`` that is an
        # empty result (no sessions, empty lists), not an error.
        del missing

        for finding in findings:
            finding.episode = self.unit(finding.session_id, finding.ts)
        findings = self._drop_third_party(findings, exposure)
        findings = _recurring_tool_failures(findings)
        if not options.include_excerpts:
            self._hide_guessed_paths(findings, exposure)
        _attribute_interrupted_commands(findings)
        for one_off in _settle_command_fixes(findings):
            low, high = one_off.run or (0.0, 0.0)
            dropped.append((one_off.line, max(0.0, high - low), signals.DROPPED_ONE_OFF))
        self.dropped = dropped
        _unclaim_single_session_stretches(findings)
        # The agent tool's own rules are the same in every repository: reported apart,
        # counted nowhere else (scores, totals, the map, comparisons).
        self.tool_rules = [f for f in findings if scoring.is_agent_tool_rule(f)]
        findings = [f for f in findings if not scoring.is_agent_tool_rule(f)]
        findings.sort(key=_finding_order)
        self.tool_rules.sort(key=_finding_order)
        for index, finding in enumerate([*findings, *self.tool_rules]):
            finding.fid = index
        for finding in findings:  # friction implies exposure, whatever the detector saw
            for target in finding.targets:
                self._expose(exposure, target.kind, target.key, finding.unit)
        self.exposure = exposure
        self.repo_advice = self._baselines(findings)
        result = self._emit(streams, findings, sources)
        result["paste"] = self._paste(result, streams)
        return result

    def _paste(self, result: dict[str, Any], streams: Sequence[Stream]) -> dict[str, Any]:
        """The fixes as text to paste: the AGENTS.md lines and the permission rules the
        advice names, strongest first. Each deny rule is replayed against every call in
        these sessions, so that what it would have refused is a count, not a guess."""
        lines: list[dict[str, Any]] = []
        deny: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item, rec in paste_candidates(result):
            line = str(rec.get("agents_md") or "")
            if line and line not in seen:
                seen.add(line)
                lines.append({"line": line, "target": item.get("path"), "rule": rec.get("id")})
            rule = str(rec.get("deny") or "")
            if rule and rule not in seen:
                seen.add(rule)
                deny.append(
                    {"rule": rule, "target": item.get("path"), **self._replay(rule, streams)}
                )
        return {"agents_md": lines, "deny": deny}

    def _replay(self, rule: str, streams: Sequence[Stream]) -> dict[str, Any]:
        """What ``rule`` would have refused in these sessions: calls, sessions, and which
        files or commands (``refused_targets``). Sessions of agents the rule does not
        govern (anything but Claude Code) are counted apart."""
        rules = parse_deny_rule(rule)
        calls = 0
        sessions: set[str] = set()
        targets: Counter[str] = Counter()
        other_agents: set[str] = set()
        for stream in streams:
            for event in stream.events:
                if event.kind != model.TOOL_CALL or event.derived:
                    continue
                rel = self.resolver.resolve(event.path, event.cwd) if event.path else None
                command = event.command or ""
                # As Claude Code matches a Bash rule: the line and its typed parts,
                # wrappers kept, so nothing is counted that the rule would let through.
                lines = [command, *typed_pieces(command)] if command else [command]
                if not rule_denies(rules, event.tool or "", lines, rel or ""):
                    continue
                if stream.kind != claude_code.KIND:
                    other_agents.add(stream.session_id)
                    continue
                calls += 1
                sessions.add(stream.session_id)
                step = next(
                    (one for one in lines[1:] if rule_denies(rules, event.tool or "", [one])),
                    command,
                )
                targets[rel or redact.redact(step)[:80] or event.tool] += 1
        return {
            "would_refuse": calls,
            "sessions": len(sessions),
            "refused_targets": [
                {"target": key, "calls": n}
                for key, n in sorted(targets.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
            "other_agent_sessions": len(other_agents),
        }

    def _baselines(self, findings: Sequence[Finding]) -> list[dict[str, object]]:
        """Mark mechanical events that are no more than the repository's background rate.

        See ``scoring.baseline``. A generated file is exempt: there, one failed edit
        is already somebody editing output by hand. Returns the repository-level
        items (``recommend.repo_wide``).
        """
        advice: list[dict[str, object]] = []
        visits = {
            key: len(units) for (kind, key), units in self.exposure.items() if kind == model.FILE
        }
        for signal in sorted(scoring.BASELINE_SIGNALS):
            mine: dict[str, list[Finding]] = {}
            for finding in findings:
                if finding.signal != signal or scoring.is_weak(finding):
                    continue
                for target in finding.targets:
                    if target.kind == model.FILE:
                        mine.setdefault(target.key, []).append(finding)
            if not mine:
                continue
            counts = {
                key: (
                    total,
                    len({f.unit for f in mine.get(key, ())}),
                    len(mine.get(key, ())),
                )
                for key, total in visits.items()
            }
            base = scoring.baseline(counts)
            above = set(base.above)
            for key, found in mine.items():
                if key in above and key in base.sparse:
                    for finding in found:
                        finding.detail[scoring.NO_COMPARISON] = "1"
                if key in above or self.facts(key).generated:
                    continue
                for finding in found:
                    finding.detail[scoring.WITHIN_BASELINE] = "1"
            every = [f for found in mine.values() for f in found]
            item = recommend.repo_wide(
                signal,
                base,
                len(self.sessions_of(f.unit for f in every)),
                len({f.fid for f in every if f.detail.get("rewritten")}),
            )
            if item is not None:
                advice.append(item)
        return advice

    def _expose(
        self, exposure: dict[tuple[str, str], set[str]], kind: str, key: str, session: str
    ) -> None:
        if kind == model.COMMAND:
            key = self.clean(key)
        exposure.setdefault((kind, key), set()).add(session)

    def _in_repo(self, stream: Stream) -> bool:
        resolver = self.resolver
        # A session started through a symbolic link to the repository recorded the
        # link's spelling: on this machine that directory is the repo.
        for cwd in {event.cwd for event in stream.events if event.cwd}:
            resolver.adopt(cwd)
        # Where nothing says where the session ran (no cwd recorded), only a tool call
        # places it here; a session of prose alone touched no repository.
        return any(
            (resolver.contains(event.cwd) if event.cwd else event.kind == model.TOOL_CALL)
            or resolver.resolve(event.path, event.cwd) is not None
            or (event.command is not None and signals.runs_inside(resolver, event))
            for event in stream.events
        )

    def _transcript_files(self) -> list[Path]:
        options = self.options
        if options.transcripts is not None:
            for given in options.transcripts:
                path = Path(given).expanduser()
                if not path.exists():
                    raise BadTranscriptError(f"Transcript path does not exist: {given}")
                if path.is_file() and os.access(path, os.R_OK) and not _looks_like_jsonl(path):
                    raise BadTranscriptError(
                        f"{given} is not a JSONL transcript: none of its first "
                        f"{JSONL_PROBE_LINES} lines is a JSON object{_encoding_note(path)}. "
                        "Pass Claude Code "
                        "session files (*.jsonl), directories that contain them, or files in "
                        f"the generic format described in {SCHEMA_URL}"
                    )
            files = expand_transcript_paths(options.transcripts)
            if not files:
                listed = ", ".join(str(p) for p in options.transcripts) or "(none given)"
                raise NoTranscriptsError(
                    f"No .jsonl transcript files found in: {listed}. Pass transcript files "
                    "or directories that contain them."
                )
            return files
        # An alias is where the repository lived when the sessions were recorded: its
        # project directories are this repository's too.
        aliases = [Path(a) for a in self.options.repo_aliases if Path(a) not in self.roots]
        files = discover_repo_transcripts(self.repo, [*self.roots, *aliases])
        if not files:
            searched = ", ".join(str(c / "projects") for c in claude_config_dirs())
            raise NoTranscriptsError(
                f"No Claude Code transcripts found for {self.repo}. Looked in: {searched}. "
                "Transcripts are stored per working directory, so pass the path of the "
                "repository the agent worked in, set "
                "CLAUDE_CONFIG_DIR if your config lives elsewhere, or pass transcript files or "
                "directories explicitly. Logs "
                "from other agents can be converted to the generic JSONL format described "
                f"in {SCHEMA_URL}"
            )
        return files

    def _sources(self, parsed: Sequence[ParsedFile]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        sessions: dict[tuple[str, str], set[str]] = {}
        outside: dict[tuple[str, str], set[str]] = {}
        for item in parsed:
            key = (item.kind, self.source_group(item))
            entry = grouped.setdefault(
                key,
                {
                    "kind": key[0],
                    "path": key[1],
                    "sessions": 0,
                    "skipped_lines": 0,
                    "skipped_events": 0,
                    "ignored_lines": 0,
                    "duplicate_lines": 0,
                    "files": 0,
                    "sessions_outside_repo": 0,
                },
            )
            entry["duplicate_lines"] += item.duplicate_lines
            entry["skipped_lines"] += item.skipped_lines
            entry["skipped_events"] += self.skipped_events.get(str(item.path), 0)
            entry["ignored_lines"] += item.ignored_lines
            entry["files"] += 1
            sessions.setdefault(key, set()).update(s.session_id for s in item.streams)
            outside.setdefault(key, set()).update(self.outside.get(item.path, ()))
        for key, entry in grouped.items():
            entry["sessions"] = len(sessions[key])
            entry["sessions_outside_repo"] = len(outside[key] - sessions[key])
        listed = [grouped[key] for key in sorted(grouped)]
        # A transcript that could not be read is said so, not passed over in silence. Only
        # its file name is shown: the directory may name a user or a project.
        for path, reason in sorted(self.unreadable):
            listed.append(
                {
                    "kind": "unreadable",
                    "path": path.name,
                    "sessions": 0,
                    "skipped_lines": 0,
                    "skipped_events": 0,
                    "ignored_lines": 0,
                    "duplicate_lines": 0,
                    "files": 1,
                    "sessions_outside_repo": 0,
                    "error": reason,
                }
            )
        return listed

    # ---- output ----------------------------------------------------------------------

    def _emit(
        self, streams: Sequence[Stream], findings: Sequence[Finding], sources: list[dict[str, Any]]
    ) -> dict[str, Any]:
        options = self.options
        groups: dict[tuple[str, str], list[Item]] = {}
        for finding in findings:
            for target in finding.targets:
                key = target.key
                if target.kind == model.COMMAND:
                    key = self.clean(key)
                groups.setdefault((target.kind, key), []).append((finding, target.share))

        by_kind: dict[str, list[dict[str, Any]]] = {
            model.FILE: [],
            model.DIR: [],
            model.COMMAND: [],
        }
        for (kind, key), items in groups.items():
            by_kind[kind].append(self._entry(kind, key, items))
        self._families(by_kind[model.COMMAND], groups)
        for entries in by_kind.values():
            entries.sort(key=lambda e: (-e["score"], -e["events"], e["path"]))
        limit = None if options.max_hotspots is None else max(options.max_hotspots, 0)
        shown: dict[str, list[dict[str, Any]]] = {}
        for kind, entries in by_kind.items():
            shown[kind] = entries[:limit]
            for entry in shown[kind]:
                self._decorate(entry, groups[(kind, entry["path"])])

        stamps = [e.ts for s in streams for e in s.events if e.ts is not None]
        stamps.extend(f.ts for f in findings if f.ts is not None)
        first, last = (min(stamps), max(stamps)) if stamps else (None, None)
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.signal] = counts.get(finding.signal, 0) + 1
        now = _utc(options.now) or datetime.now(timezone.utc)
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": TOOL_NAME, "version": __version__},
            "generated_at": _iso(now),
            "repo": {"root": self.display_path(self.repo), "name": self.name},
            # What kind of scan this is; reports made with different settings do not compare.
            "settings": {
                "excerpts": options.include_excerpts,
                "git": self.git_used,
                "key_mode": "plain" if options.include_excerpts else "pseudonymized",
            },
            "window": {
                "start": _iso(self.since or first),
                "end": _iso(self.until or last),
                "since": _iso(self.since),
                "until": _iso(self.until),
                "first_event": _iso(first),
                "last_event": _iso(last),
            },
            "sources": sources,
            "totals": {
                "sessions": len({s.session_id for s in streams}),
                "episodes": len({self.unit(s.session_id, e.ts) for s in streams for e in s.events}),
                "tool_calls": sum(
                    1
                    for s in streams
                    for e in s.events
                    if e.kind == model.TOOL_CALL and not e.derived
                ),
                # ... of which those that acted inside this repository: a session started
                # in a parent directory also works on its neighbours
                "tool_calls_in_repo": sum(
                    1
                    for s in streams
                    for e in s.events
                    if e.kind == model.TOOL_CALL and not e.derived and self.in_repo_call(e)
                ),
                # Sessions read and set aside because nothing in them touched this
                # repository (the sum of the sources' counts).
                "sessions_outside_repo": sum(s.get("sessions_outside_repo", 0) for s in sources),
                # Sessions without a single timestamp, left out because `since` or `until`
                # was given: they cannot be placed inside a window (0 without a window).
                "untimed_sessions_excluded": self.untimed_sessions_excluded,
                "friction_events": len(findings),
                # ... of which those that are friction on their own: not the kinds that
                # also accompany productive work (see ``scoring.is_weak``).
                "strong_events": sum(1 for f in findings if not scoring.is_weak(f)),
                "est_minutes_lost": scoring.lost_minutes(findings),
                # Red runs that were seen and deliberately not reported: the development
                # loop (red, change, green), waiting, one-off fixes. Not friction.
                "dropped_red_runs": len(self.dropped),
                "dropped_minutes": round(sum(d[1] for d in self.dropped) / 60.0, 1),
                # Corroborated corrections only; "probable" ones are listed as evidence.
                "corrections": counts.get(signals.USER_CORRECTION, 0),
                "probable_corrections": counts.get(signals.PROBABLE_CORRECTION, 0),
                "files_affected": len(by_kind[model.FILE]),
                "commands_affected": len(by_kind[model.COMMAND]),
                "directories_affected": len(by_kind[model.DIR]),
                "unattributed_events": sum(1 for f in findings if not f.targets),
                "third_party_events": self.third_party_events,
                # Failures of tools that are neither file tools nor the shell (MCP tools,
                # web fetches, sub-agents), listed per tool under ``tool_failures``.
                "tool_failures": counts.get(signals.TOOL_FAILURE, 0),
                # Calls that were refused or failed while acting outside the repository
                # (`cd ../other && ...`, a file under /tmp): seen, and not counted.
                "events_outside_repo": self.events_outside_repo,
                "events_elsewhere_in_repo": self.events_elsewhere_in_repo,
                # Calls the agent tool refused by its own rules: listed under
                # ``agent_tool``, in none of the counts above.
                "agent_tool_events": len(self.tool_rules),
                "agent_tool_minutes": scoring.lost_minutes(self.tool_rules),
            },
            "signals": [
                {
                    "id": s.id,
                    "title": s.title,
                    "description": s.description,
                    "count": counts.get(s.id, 0),
                    "severity": s.severity,
                    "why": s.why,
                    "attribution": s.attribution,
                }
                for s in signals.CATALOG
            ],
            "hotspots": shown[model.FILE],
            "commands": shown[model.COMMAND],
            "directories": shown[model.DIR],
            "exposure": {
                name: {
                    key: len(self.sessions_of(sessions))
                    for (kind, key), sessions in sorted(self.exposure.items())
                    if kind == wanted
                }
                for name, wanted in (
                    ("file", model.FILE),
                    ("command", model.COMMAND),
                    ("dir", model.DIR),
                )
            },
            # Advice about the repository as a whole: a mechanical signal spread evenly
            # over many files is one fact, not a pattern in each of them.
            "recommendations": self.repo_advice,
            "tool_failures": _tool_failures(findings),
            "tree": self._tree(groups),
            # Events that count in the totals but belong to no file, command or
            # directory (an interrupt with no tool call to pin it on, a declined tool
            # without a path): shown, so that no headline number is without evidence.
            "unattributed": [
                self._evidence(f)
                for f in _pick_evidence(
                    [f for f in findings if not f.targets], MAX_UNATTRIBUTED_EVIDENCE
                )
            ],
            "agent_tool": self._agent_tool_rules(),
        }

    def _agent_tool_rules(self) -> list[dict[str, Any]]:
        """The agent tool's own refusals, per command: what agents tried, how often,
        and what the refusal says to do instead. Not friction of this repository."""
        groups: dict[str, list[Finding]] = {}
        for finding in self.tool_rules:
            target = next((t for t in finding.targets if t.kind == model.COMMAND), None)
            key = self.clean(target.key) if target is not None else ""
            groups.setdefault(key, []).append(finding)
        quote = self.quote if self.options.include_excerpts else None
        ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        return [
            {
                "command": key or None,
                "events": len(found),
                "sessions": len({f.session_id for f in found}),
                "est_minutes_lost": scoring.lost_minutes(found),
                "recommendations": recommend.recommend(model.COMMAND, key, found, quote=quote)
                if key
                else [],
                "evidence": [
                    self._evidence(f) for f in _pick_evidence(found, self.options.max_evidence)
                ],
            }
            for key, found in ordered
        ]

    def _entry(self, kind: str, key: str, items: Sequence[Item]) -> dict[str, Any]:
        """The cheap part of a hotspot (everything needed for ranking)."""
        found = [f for f, _ in items]
        result = _score(items, self.exposure.get((kind, key), set()))
        signal_counts: dict[str, int] = {}
        for finding in found:
            signal_counts[finding.signal] = signal_counts.get(finding.signal, 0) + 1
        ranked = sorted(signal_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        units = {s.session_id for s in result.sessions}
        friction = self.sessions_of(units)
        sessions = len(friction)
        commits = sum(s.startswith(scoring.GIT_SESSION_PREFIX) for s in friction)
        exposed = len(friction | self.sessions_of(self.exposure.get((kind, key), set())))
        level = scoring.confidence((s.session_id for s in result.sessions), signal_counts)
        capped = scoring.weak_only(found)
        rest = [f for f, _ in _beside_ordinary(items)]
        guarded = sum(scoring.is_guardrail_hit(f) for f in found)
        ordinary = len(found) - len(rest)
        if capped:
            level = "low"  # evidence that is routine on its own is a lead, never a pattern
        elif ordinary and rest:
            # ... and next to real evidence it corroborates nothing: the sessions and the
            # kinds of evidence that count are those of the rest
            level = scoring.confidence({f.unit for f in rest}, {f.signal for f in rest})
        limited = (
            not capped
            and ordinary > 0
            and result.score < scoring.score_items(items, result.exposure_sessions).score
        )
        return {
            "path": key,
            "kind": kind,
            "score": result.score,
            "sessions": sessions,
            "episodes": len(units),
            "agent_commits": commits,
            "exposure_sessions": exposed,
            "exposure_episodes": result.exposure_sessions,
            "friction_rate": round(sessions / exposed, 2) if exposed else 0.0,
            "events": len(found),
            "est_minutes_lost": scoring.lost_minutes(found),
            "signals": dict(ranked),
            # refusals by a hook or a deny rule after which the agent went another way
            "guardrail_hits": guarded,
            "confidence": level,
            "why": scoring.explain(
                ranked,
                sessions - commits,
                exposed - commits,
                level,
                commits,
                capped=capped,
                episodes=len(units) - commits,
                ordinary=ordinary - guarded if limited else 0,
                guardrail=guarded,
                rewritten=sum(scoring.is_hook_habit(f) for f in found) if capped else 0,
            ),
            "recommendations": [],
            "evidence": [],
            "score_detail": {
                "capped": capped,
                "ordinary_limited": limited,
                "severity": result.severity,
                "recurrence": result.recurrence,
                "consistency": result.consistency,
                "mean_session_points": result.mean_session_points,
                "intensity": result.intensity,
                "sessions": [
                    {
                        "session_id": self.unit_session.get(s.session_id, s.session_id),
                        "episode": s.session_id,
                        "weighted_events": s.weighted_events,
                        "points": s.points,
                    }
                    for s in result.sessions
                ],
            },
        }

    def _families(
        self, entries: Sequence[dict[str, Any]], groups: Mapping[tuple[str, str], Sequence[Item]]
    ) -> None:
        """Tie together one command that is listed once per directory it was run in.

        ``pytest``, ``pytest (src/)`` and ``pytest (tests/)`` stay three rows (where a
        command runs is part of how it is run), and each row says what the three add
        up to: distinct events and distinct sessions.
        """
        members: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            located = _LOCATED_KEY_RE.match(entry["path"])
            members.setdefault(located.group(1) if located else entry["path"], []).append(entry)
        for base, same in members.items():
            found = {f.fid: f for entry in same for f, _ in groups[(model.COMMAND, entry["path"])]}
            family = {
                "command": base,
                "members": sorted(entry["path"] for entry in same),
                "events": len(found),
                "sessions": len(self.sessions_of(f.unit for f in found.values())),
            }
            for entry in same:
                entry["family"] = family if len(same) > 1 else None

    def _known_targets(self, key: str, found: Sequence[Finding]) -> tuple[str, ...]:
        """The make targets or package scripts that exist where a missing one was asked for."""
        holder = next((f.detail["no_target_in"] for f in found if "no_target_in" in f.detail), "")
        if not holder:
            return ()
        located = re.search(r" \((.+)/\)$", key)
        base = self.repo / located.group(1) if located else self.repo
        names: list[str] = []
        try:
            if holder == signals.TARGETS_IN_MAKEFILE:
                for name in ("GNUmakefile", "makefile", "Makefile"):
                    path = base / name
                    if path.is_file() and path.stat().st_size <= MAX_TARGETS_FILE_BYTES:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        names = _MAKE_TARGET_RE.findall(text)
                        break
            elif holder == signals.TARGETS_IN_JUSTFILE:
                for name in ("justfile", "Justfile", ".justfile"):
                    path = base / name
                    if path.is_file() and path.stat().st_size <= MAX_TARGETS_FILE_BYTES:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        names = _JUST_RECIPE_RE.findall(text)
                        break
            else:
                path = base / "package.json"
                if path.is_file() and path.stat().st_size <= MAX_TARGETS_FILE_BYTES:
                    scripts = json.loads(path.read_text(encoding="utf-8")).get("scripts")
                    names = [str(name) for name in scripts] if isinstance(scripts, dict) else []
        except (OSError, ValueError, AttributeError):
            return ()
        # the one that was asked for comes first, so that a long list cannot cut it off
        asked = {f.detail.get("no_target", "") for f in found}
        names = [n for n in names if n in asked] + [n for n in names if n not in asked]
        return tuple(dict.fromkeys(names))[:MAX_KNOWN_TARGETS]

    def _decorate(self, entry: dict[str, Any], items: Sequence[Item]) -> None:
        """Add recommendations, evidence and file facts to a hotspot that will be shown."""
        options = self.options
        kind, key = entry["kind"], entry["path"]
        found = [f for f, _ in items]
        facts = self.facts(key) if kind == model.FILE else None
        if facts is not None and not facts.exists and options.include_git:
            # Gone from the tree: ask git when, and whether it lives on under another name.
            removed_at, renamed_to = gitsignals.removal(self.repo, key)
            facts = dataclasses.replace(facts, removed_at=removed_at, renamed_to=renamed_to)
            self._facts[key] = facts
        quote = self.quote if options.include_excerpts else None
        shares = {f.fid: share for f, share in items}
        entry["recommendations"] = recommend.recommend(
            kind,
            key,
            found,
            facts,
            quote,
            shares,
            self.guidance,
            self._known_targets(key, found) if kind == model.COMMAND else (),
        )
        entry["evidence"] = [self._evidence(f) for f in _pick_evidence(found, options.max_evidence)]
        transcript_sessions = entry["episodes"] - entry["agent_commits"]
        if transcript_sessions == 1 and entry["score"] < SINGLE_SESSION_ADVICE_MIN_SCORE:
            # One session and a low score is a lead to look at, not grounds for telling
            # anybody to change their repository.
            # What a human said in so many words is passed on, as the question it is.
            entry["recommendations"] = [
                r for r in entry["recommendations"] if r["id"] in HUMAN_STATED_RULES
            ]
        if facts is not None:
            entry["file"] = facts.as_dict()

    def _evidence(self, finding: Finding) -> dict[str, Any]:
        options = self.options
        text: str | None = None
        if options.include_excerpts and finding.excerpt.strip():
            text = redact.excerpt(finding.excerpt, options.excerpt_chars)
        return {
            # the last thing that happened in its session: not known to be resolved
            **({"trailing": True} if finding.detail.get("trailing") else {}),
            "signal": finding.signal,
            "session_id": finding.session_id,
            "timestamp": _iso(finding.ts),
            "summary": self.clean(finding.summary),
            "excerpt": text,
            "source": self.cited_source(finding),
            # a transcript line; for git evidence, the short hash of the human's commit;
            # nothing when the transcript itself is not named (a line of what?)
            "line": (
                finding.detail.get("human_commit", 0)
                if _is_git(finding)
                else finding.line
                if self.cited_source(finding) is not None
                else None
            ),
            # what this event alone claims; a hotspot's minutes are the union, not the sum
            "est_seconds_lost": round(
                sum(end - start for start, end in scoring.claimed(finding)), 1
            ),
        }

    def _tree(self, groups: dict[tuple[str, str], list[Item]]) -> dict[str, Any]:
        direct: dict[str, list[Item]] = {"": []}
        children: dict[str, set[str]] = {"": set()}
        for (kind, key), items in groups.items():
            if kind == model.COMMAND:
                continue
            path = "" if key == "." else key
            direct.setdefault(path, []).extend(items)
            children.setdefault(path, set())
            while path:
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                children.setdefault(parent, set()).add(path)
                direct.setdefault(parent, [])
                path = parent
        dir_keys = {("" if k == "." else k) for (kind, k) in groups if kind == model.DIR}
        # Sessions that went anywhere beneath each directory, friction or not.
        beneath: dict[str, set[str]] = {}
        for (kind, key), sessions in self.exposure.items():
            if kind == model.COMMAND:
                continue
            path = "" if key == "." else key
            while True:
                beneath.setdefault(path, set()).update(sessions)
                if not path:
                    break
                path = path.rsplit("/", 1)[0] if "/" in path else ""

        def build(path: str) -> tuple[dict[str, Any], dict[int, Item]]:
            merged: dict[int, Item] = {}

            def absorb(items: Sequence[Item]) -> None:
                for finding, share in items:
                    previous = merged.get(finding.fid)
                    total = share + (previous[1] if previous else 0.0)
                    merged[finding.fid] = (finding, min(total, 1.0))

            absorb(direct[path])
            nodes = []
            for child in sorted(children[path]):
                node, child_items = build(child)
                nodes.append(node)
                absorb(list(child_items.values()))
            nodes.sort(key=lambda n: (-n["score"], n["path"]))
            found = [f for f, _ in merged.values()]
            is_dir = bool(nodes) or path in dir_keys or path == ""
            result = _score(list(merged.values()), beneath.get(path, set()))
            friction = self.sessions_of(s.session_id for s in result.sessions)
            exposed = len(friction | self.sessions_of(beneath.get(path, set())))
            node = {
                "name": path.rsplit("/", 1)[-1] if path else self.name,
                "path": path,
                "kind": model.DIR if is_dir else model.FILE,
                "score": result.score,
                "events": len(found),
                "sessions": len(friction),
                "exposure_sessions": exposed,
                "friction_rate": round(len(friction) / exposed, 2) if exposed else 0.0,
                "est_minutes_lost": scoring.lost_minutes(found),
                "children": nodes,
            }
            return node, merged

        return build("")[0]


def _finding_order(finding: Finding) -> tuple[float, str, int, str]:
    stamp = finding.ts.timestamp() if finding.ts is not None else float("-inf")
    return (stamp, finding.source, finding.line, finding.signal)


def _recurring_tool_failures(findings: list[Finding]) -> list[Finding]:
    """Keep the failures of a tool only if it failed more than once in the scan."""
    seen = Counter(f.detail.get("tool", "") for f in findings if f.signal == signals.TOOL_FAILURE)
    return [
        f
        for f in findings
        if f.signal != signals.TOOL_FAILURE
        or seen[f.detail.get("tool", "")] >= signals.TOOL_FAILURE_MIN_EVENTS
    ]


def _tool_failures(findings: Sequence[Finding]) -> list[dict[str, object]]:
    """Per tool: how often it returned an error, and in how many sessions."""
    by_tool: dict[str, list[Finding]] = {}
    for finding in findings:
        if finding.signal == signals.TOOL_FAILURE:
            by_tool.setdefault(finding.detail.get("tool", ""), []).append(finding)
    rows = [
        {"tool": tool, "events": len(found), "sessions": len({f.session_id for f in found})}
        for tool, found in by_tool.items()
    ]
    return sorted(rows, key=lambda row: (-int(str(row["events"])), str(row["tool"])))


def _pick_evidence(findings: Sequence[Finding], limit: int) -> list[Finding]:
    """Choose up to ``limit`` findings, favouring severe signals and distinct sessions.

    Signals are visited round-robin from most to least severe. Within a signal
    the heaviest finding from a session not shown yet goes first, newest first.
    The result is returned in chronological order.
    """
    if limit <= 0:
        return []
    queues: dict[str, list[Finding]] = {}
    for finding in findings:
        queues.setdefault(finding.signal, []).append(finding)
    order = sorted(queues, key=lambda sid: (-signals.SEVERITY[sid], sid))
    for queue in queues.values():
        queue.sort(key=lambda f: (-f.weight, [-x for x in _finding_order(f)[:1]], f.fid))
    chosen: list[Finding] = []
    seen_sessions: set[str] = set()
    while len(chosen) < limit and any(queues[sid] for sid in order):
        for sid in order:
            queue = queues[sid]
            if not queue or len(chosen) >= limit:
                continue
            index = next((i for i, f in enumerate(queue) if f.session_id not in seen_sessions), 0)
            pick = queue.pop(index)
            chosen.append(pick)
            seen_sessions.add(pick.session_id)
    return sorted(chosen, key=_finding_order)


def _sanitized(value: Any) -> Any:
    """Strip terminal control and bidi characters from every string of a result.

    Paths, command keys and messages come from transcripts; none of it may be
    able to move a cursor, retitle a terminal or reorder text in a report.
    """
    if isinstance(value, str):
        return redact.strip_controls(value)
    if isinstance(value, dict):
        return {_sanitized(k): _sanitized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitized(v) for v in value]
    return value


def analyze(options: AnalyzeOptions) -> dict[str, Any]:
    """Analyse agent transcripts (and git history) for ``options.repo``.

    Raises :class:`NoTranscriptsError` when no transcript files can be found.
    """
    result: dict[str, Any] = _sanitized(_Analysis(options).run())
    return result
