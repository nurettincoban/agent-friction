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
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_friction import __version__, gitsignals, model, recommend, redact, scoring, signals
from agent_friction.adapters import claude_code, parse_transcript
from agent_friction.attribution import (
    PUBLIC_HASH_DIGITS,
    PathResolver,
    public_key,
    public_tool,
)
from agent_friction.discovery import (
    claude_config_dirs,
    discover_repo_transcripts,
    expand_transcript_paths,
    repo_roots,
)
from agent_friction.errors import AgentFrictionError, NoTranscriptsError
from agent_friction.guidance import Guidance
from agent_friction.model import Finding, ParsedFile, Stream, Target

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
    #: With no transcripts at all, analyse git history alone instead of raising (needs
    #: ``include_git`` and a readable git history).
    allow_no_transcripts: bool = False


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
) -> list[model.Event]:
    """Drop events outside the window. Events without a timestamp inherit the previous one.

    Returns the events *before* the window: they are not counted, but what the
    agent did there is the context for judging the first events inside it.
    """
    if since is None and until is None:
        return []
    kept, before = [], []
    last: datetime | None = None
    for event in stream.events:
        last = event.ts or last
        if _in_window(last, since, until):
            kept.append(event)
        elif since is not None and last is not None and last < since:
            before.append(event)
    stream.events = kept
    return before


def _load(
    files: Sequence[Path],
    since: datetime | None,
    until: datetime | None,
    unreadable: list[tuple[Path, str]] | None = None,
) -> tuple[list[ParsedFile], dict[str, list[model.Event]]]:
    """Parsed files cut to the window, and each stream's events from before it (by key).

    Files that cannot be read are skipped and noted in ``unreadable`` with the reason.
    """
    seen: dict[str, str] = {}
    parsed: list[ParsedFile] = []
    context: dict[str, list[model.Event]] = {}
    for path in files:
        try:
            item = parse_transcript(path, seen)
        except OSError as exc:
            if unreadable is not None:
                unreadable.append((path, read_error(exc)))
            continue
        for stream in item.streams:
            before = _apply_window(stream, since, until)
            if before and stream.events:
                context[stream.key] = before
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


#: A human prompt after this much silence starts a new episode of work in the session.
EPISODE_IDLE_SECONDS = 2 * 3600.0


def episode_starts(streams: Sequence[Stream]) -> dict[str, list[float]]:
    """When the second and later episodes of each session begin (epoch seconds).

    A session id can live for days (``claude --continue``): ten tasks on ten days
    are ten independent occasions for the same friction, not one. A new episode
    starts at a human prompt that follows ``EPISODE_IDLE_SECONDS`` or more without
    any event in the session. Subagent streams never start one: their prompts are
    written by the agent that drives them, and they belong to its episode.
    """
    by_session: dict[str, list[tuple[float, bool]]] = {}
    for stream in streams:
        marks = by_session.setdefault(stream.session_id, [])
        for event in stream.events:
            if event.ts is not None:
                prompt = event.kind == model.USER_MESSAGE and not stream.sidechain
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
    runners: dict[tuple[str, str], set[str]] = {}
    swapped = [f for f in findings if "green_as" in f.detail]
    for finding in swapped:
        for key in _command_keys(finding):
            runners.setdefault((key, finding.detail["green_as"]), set()).add(finding.unit)
    for finding in swapped:
        other = finding.detail.pop("green_as")
        keys = _command_keys(finding)
        if finding.detail.get("variant") != "ordinary" or not any(
            len(runners.get((key, other), ())) >= RUNNER_MIN_SESSIONS for key in keys
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
    # The same test red in ``STANDING_TEST_MIN_SESSIONS`` episodes or more, none of which
    # made it pass: a standing failure. It is no longer "a red test during development".
    red_tests: dict[tuple[str, str], set[str]] = {}
    naming = [
        f
        for f in findings
        if f.signal == signals.COMMAND_FAILURE
        and "failed_tests" in f.detail
        and f.detail.get("variant") == "ordinary"
    ]
    for finding in naming:
        for key in _command_keys(finding):
            for test in finding.detail["failed_tests"].split("\n"):
                red_tests.setdefault((key, test), set()).add(finding.unit)
    for finding in naming:
        tests = finding.detail["failed_tests"].split("\n")
        files = finding.detail.get("failed_test_files", "").split("\n")
        for test, rel in zip(tests, files, strict=False):
            if not any(
                len(red_tests.get((key, test), ())) >= STANDING_TEST_MIN_SESSIONS
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
    dropped: list[Finding] = []
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


def _score(items: Sequence[Item], exposed: set[str]) -> scoring.Score:
    """Score a target. Git evidence may raise a score, never lower it.

    A rework finding adds a friction "session" (the agent commit) that can pull
    the mean down, so the score from transcripts alone is the floor.
    """
    sessions = exposed | {f.unit for f, _ in items}
    full = scoring.score_items(items, len(sessions))
    if scoring.weak_only(f for f, _ in items):
        # Nothing but signals that also accompany productive work: a lead, never an alarm.
        return dataclasses.replace(full, score=min(full.score, scoring.WEAK_ONLY_SCORE_CAP))
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
        self.repo = options.repo.expanduser().absolute()
        self.since = _utc(options.since)
        self.until = _utc(options.until)
        roots = repo_roots(self.repo)
        self.roots = roots
        self.resolver = PathResolver(
            self.repo, [*roots[1:], str(options.repo), *options.repo_aliases]
        )
        self.touched: set[str] = set()
        self._is_file_cache: dict[str, bool | None] = {}
        self._facts: dict[str, recommend.FileFacts] = {}
        self.guidance = Guidance(self.repo)
        self.outside: dict[Path, set[str]] = {}
        self.skipped_events: dict[str, int] = {}
        self.source_labels: dict[str, str] = {}
        self.exposure: dict[tuple[str, str], set[str]] = {}  # by episode of work
        self.episodes: dict[str, list[float]] = {}
        self.unit_session: dict[str, str] = {}
        self.git_used = False
        self.dropped: list[tuple[int, float, str]] = []
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
        """

        def in_repo(word: str) -> bool:
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
        # A command key names the directory it ran in (`npm test (web/)`). That the
        # command ran there shows the directory existed: it is not a guess.
        return key if kind == model.COMMAND else self.public_path(key)

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
            self._facts[rel] = recommend.file_facts(self.repo, rel)
        return self._facts[rel]

    # ---- text helpers ----------------------------------------------------------------

    def clean(self, text: str) -> str:
        return redact.one_line(text, 2 * self.options.excerpt_chars)

    def quote(self, text: str, limit: int) -> str:
        return redact.one_line(text, min(limit, max(self.options.excerpt_chars, 1)))

    def display_path(self, path: Path | str) -> str:
        """Home shown as ``~`` and forward slashes on every platform."""
        return redact.redact_home(str(path)).replace("\\", "/")

    def source_group(self, item: ParsedFile) -> str:
        """How a transcript source is named in the result.

        A Claude Code project directory is named after the absolute path of the
        working directory (user name, client, project), so it is never shown:
        the repository's own name stands in for it.
        """
        name = self.repo.name or "repo"
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
        except NoTranscriptsError as exc:
            if not (options.allow_no_transcripts and options.include_git):
                raise
            missing, files = exc, []
        parsed, context = _load(files, self.since, self.until, self.unreadable)
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
        )
        findings: list[Finding] = []
        dropped: list[tuple[int, float, str]] = []
        exposure: dict[tuple[str, str], set[str]] = {}
        for stream in streams:
            detection = signals.detect_stream(stream, ctx)
            before = context.get(stream.key)
            if before:
                # Judge the window with what happened before it in view (the edit a
                # correction refers to, the red run an edit answers), but count and
                # expose only what lies inside it.
                first = stream.events[0].line
                whole = dataclasses.replace(stream, events=[*before, *stream.events])
                full = signals.detect_stream(whole, ctx)
                detection = dataclasses.replace(
                    detection,
                    findings=[f for f in full.findings if f.line >= first],
                    skipped_events=full.skipped_events,
                    dropped=[d for d in full.dropped if d[0] >= first],
                )
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
                rework = [
                    f
                    for f in gitsignals.find_rework(
                        history.commits,
                        options.rework_window_days,
                        gitsignals.HunkReader(self.repo),
                    )
                    if _in_window(f.ts, self.since, self.until)
                    # A lockfile or generated file is rewritten by tools as a matter of
                    # course: a human `chore: bump` after an agent commit is not rework.
                    and not any(self.facts(t.key).generated for t in f.targets)
                ]
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
        if missing is not None and not self.git_used:
            raise missing  # neither transcripts nor git history: nothing to analyse

        for finding in findings:
            finding.episode = self.unit(finding.session_id, finding.ts)
        findings = self._drop_third_party(findings, exposure)
        if not options.include_excerpts:
            self._hide_guessed_paths(findings, exposure)
        _attribute_interrupted_commands(findings)
        for one_off in _settle_command_fixes(findings):
            low, high = one_off.run or (0.0, 0.0)
            dropped.append((one_off.line, max(0.0, high - low), signals.DROPPED_ONE_OFF))
        self.dropped = dropped
        _unclaim_single_session_stretches(findings)
        findings.sort(key=_finding_order)
        for index, finding in enumerate(findings):
            finding.fid = index
        for finding in findings:  # friction implies exposure, whatever the detector saw
            for target in finding.targets:
                self._expose(exposure, target.kind, target.key, finding.unit)
        self.exposure = exposure
        return self._emit(streams, findings, sources)

    def _expose(
        self, exposure: dict[tuple[str, str], set[str]], kind: str, key: str, session: str
    ) -> None:
        if kind == model.COMMAND:
            key = self.clean(key)
        exposure.setdefault((kind, key), set()).add(session)

    def _in_repo(self, stream: Stream) -> bool:
        resolver = self.resolver
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
                    raise NoTranscriptsError(f"Transcript path does not exist: {given}")
                if path.is_file() and os.access(path, os.R_OK) and not _looks_like_jsonl(path):
                    raise NoTranscriptsError(
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
        files = discover_repo_transcripts(self.repo, self.roots)
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
            "repo": {"root": self.display_path(self.repo), "name": self.repo.name or "repo"},
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
                "friction_events": len(findings),
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
        }

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
        if capped:
            level = "low"  # evidence that is routine on its own is a lead, never a pattern
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
            "confidence": level,
            "why": scoring.explain(
                ranked,
                sessions - commits,
                exposed - commits,
                level,
                commits,
                capped=capped,
                episodes=len(units) - commits,
            ),
            "recommendations": [],
            "evidence": [],
            "score_detail": {
                "capped": capped,
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

    def _decorate(self, entry: dict[str, Any], items: Sequence[Item]) -> None:
        """Add recommendations, evidence and file facts to a hotspot that will be shown."""
        options = self.options
        kind, key = entry["kind"], entry["path"]
        found = [f for f, _ in items]
        facts = self.facts(key) if kind == model.FILE else None
        quote = self.quote if options.include_excerpts else None
        shares = {f.fid: share for f, share in items}
        entry["recommendations"] = recommend.recommend(
            kind, key, found, facts, quote, shares, self.guidance
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
            # a transcript line; for git evidence, the short hash of the human's commit
            "line": finding.detail.get("human_commit", 0) if _is_git(finding) else finding.line,
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
                "name": path.rsplit("/", 1)[-1] if path else (self.repo.name or "repo"),
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
