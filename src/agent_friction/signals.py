"""Signal catalog and transcript detectors.

Each detector looks at one :class:`~agent_friction.model.Stream` and emits
:class:`~agent_friction.model.Finding` records. Every threshold is a named
constant here and is explained in docs/signals.md.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from agent_friction import model
from agent_friction.attribution import (
    INSPECTION_PROGRAMS,
    ParsedCommand,
    PathResolver,
    parse_command,
    path_candidates,
)
from agent_friction.model import Event, Finding, Stream, Target


@dataclass(frozen=True)
class SignalDef:
    id: str
    title: str
    description: str
    why: str
    attribution: str
    severity: float


EDIT_MISS = "edit-miss"
STALE_EDIT = "stale-edit"
PATH_MISS = "path-miss"
EDIT_THRASH = "edit-thrash"
EDIT_REVERT = "edit-revert"
READ_LOOP = "read-loop"
SEARCH_MISS = "search-miss"
COMMAND_FAILURE = "command-failure"
RETRY_LOOP = "retry-loop"
TOOL_REJECTED = "tool-rejected"
USER_INTERRUPT = "user-interrupt"
USER_CORRECTION = "user-correction"
HUMAN_REWORK = "human-rework"

# ---- thresholds (see docs/signals.md) -------------------------------------------------
#: Successful edits to one file within one user turn that count as thrashing.
EDIT_THRASH_MIN_EDITS = 6
#: Identical reads (same file, same range) with no edit in between that count as a loop.
READ_LOOP_MIN_READS = 3
#: Consecutive failures of one normalized command that count as a retry loop.
RETRY_LOOP_MIN_FAILURES = 3
#: Weight of a command failure that does not look like a setup/invocation problem.
ORDINARY_FAILURE_WEIGHT = 0.3
#: Shell calls after a failure in which a different successful command counts as its fix.
RECOVERY_LOOKAHEAD = 5
#: Per-event cap for the time-lost estimate, in seconds.
TIME_CAP_SECONDS = 300.0
#: Only the start of a user message is inspected for correction phrases.
CORRECTION_HEAD_CHARS = 280
#: A correction with no named file is split over at most this many recently edited files.
CORRECTION_MAX_FILES = 5
#: Characters of a user message scanned for file names.
CORRECTION_SCAN_CHARS = 2000
#: Files named in a failing command that are attributed, at most.
COMMAND_MAX_FILES = 5
CORRECTION_STRONG = 1.0
CORRECTION_TWO_WEAK = 0.75
CORRECTION_ONE_WEAK = 0.5

CATALOG: tuple[SignalDef, ...] = (
    SignalDef(
        USER_CORRECTION,
        "User correction",
        "A human prompt that follows agent work and reads as a correction "
        '("no, …", "I said …", "revert that", "still failing", "don\'t …").',
        "Human attention is the most expensive resource in the loop; a correction means the "
        "agent's work on these files had to be redirected by hand.",
        "Files the agent touched in the preceding turn that the message names; otherwise "
        f"split evenly over the files it edited in that turn (the {CORRECTION_MAX_FILES} most "
        "recent).",
        4.0,
    ),
    SignalDef(
        RETRY_LOOP,
        "Retry loop",
        f"The same normalized shell command failed {RETRY_LOOP_MIN_FAILURES} or more times "
        "in a row in one session without succeeding in between.",
        "The failure output did not tell the agent what to do, so it kept guessing.",
        "The normalized command.",
        3.0,
    ),
    SignalDef(
        TOOL_REJECTED,
        "Tool call declined",
        "The human declined a tool call the agent proposed.",
        "The agent wanted to do something a human considered wrong or unsafe here.",
        "The file the tool targeted, or the normalized command for shell calls.",
        2.5,
    ),
    SignalDef(
        USER_INTERRUPT,
        "User interrupt",
        "The human interrupted the agent mid-turn (not counted when it merely accompanies "
        "a declined tool call).",
        "The human saw the agent going wrong and stopped it.",
        "The target of the agent's most recent tool call in that turn.",
        2.5,
    ),
    SignalDef(
        HUMAN_REWORK,
        "Human rework (git)",
        "A commit attributed to an agent changed a file and the next commit touching that "
        "file, within the rework window, was made by a human.",
        "Agent output on this file needed a human follow-up soon after it landed.",
        "The file, from git history.",
        2.5,
    ),
    SignalDef(
        EDIT_MISS,
        "Edit miss",
        "An edit failed because the text to replace was not found in the file, or matched "
        "several places.",
        "The agent's picture of the file was wrong, or the file has repeated blocks that "
        "are hard to target; each miss costs a re-read and a retry.",
        "The file being edited.",
        2.0,
    ),
    SignalDef(
        EDIT_REVERT,
        "Edit reverted",
        "An edit exactly undid an earlier successful edit to the same file in the same "
        "session (or a write restored an earlier version of the file).",
        "Work on this file went in a circle.",
        "The file being edited.",
        2.0,
    ),
    SignalDef(
        COMMAND_FAILURE,
        "Command failure",
        "A shell command exited non-zero. Failures that look like setup or invocation "
        "problems (command not found, missing module, bad arguments, …) count fully; other "
        f"failures, such as a test that legitimately fails, count at {ORDINARY_FAILURE_WEIGHT}. "
        "Read-only inspection commands (ls, grep, …) are ignored.",
        "Agents guess how to build, test and run things; a wrong guess burns a round trip "
        "every session until the right command is written down.",
        "The normalized command. Ordinary failures are also attributed to repo files named "
        "on the command line; setup/invocation errors are not (the files are not the cause).",
        2.0,
    ),
    SignalDef(
        EDIT_THRASH,
        "Edit thrash",
        f"One file received {EDIT_THRASH_MIN_EDITS} or more successful edits within a single "
        "user turn.",
        "Many small edits to one file in one go usually means trial and error rather than "
        "a plan. Weak signal on its own.",
        "The file being edited.",
        1.5,
    ),
    SignalDef(
        STALE_EDIT,
        "Stale edit",
        "An edit or write was refused because the file had not been read first, or changed "
        "on disk after it was read.",
        "Something else (formatter, code generator, watcher, another agent) rewrites this "
        "file, or the agent edits it blind.",
        "The file being edited.",
        1.0,
    ),
    SignalDef(
        PATH_MISS,
        "Path miss",
        "The agent tried to read or edit a path that does not exist.",
        "The agent expected something to live here; the layout or naming misled it.",
        "The non-existent path (so repeated wrong guesses add up).",
        1.0,
    ),
    SignalDef(
        SEARCH_MISS,
        "Search miss",
        "A Grep or Glob search returned nothing.",
        "The agent looked for a name that is not there; repeated misses in one place "
        "point at vocabulary the codebase does not use.",
        "The file or directory that was searched.",
        1.0,
    ),
    SignalDef(
        READ_LOOP,
        "Read loop",
        f"The same range of a file was read {READ_LOOP_MIN_READS} or more times in one "
        "session with no edit to it in between.",
        "The agent keeps going back to this file without making progress. Weak signal.",
        "The file being read.",
        0.5,
    ),
)

BY_ID: dict[str, SignalDef] = {s.id: s for s in CATALOG}
SEVERITY: dict[str, float] = {s.id: s.severity for s in CATALOG}

#: Output patterns that mark a command failure as a setup/invocation problem.
INVOCATION_ERROR_RE = re.compile(
    r"command not found|not recognized as an? (?:internal|name)|no such file or directory"
    r"|modulenotfounderror|no module named|cannot find module|importerror"
    r"|permission denied|unrecognized (?:argument|option)|unknown (?:option|flag|command|argument)"
    r"|invalid (?:option|choice)|^usage: |no rule to make target|not a git repository"
    r"|missing script|enoent|eacces|connection refused|could not connect|timed out"
    r"|executable file not found|is not installed|no such (?:command|option)"
    r"|command timed out|could not find a version|not found in path",
    re.I | re.M,
)
#: Exit codes the shell itself uses for "cannot execute" and "not found".
INVOCATION_EXIT_CODES = frozenset({126, 127})

_STRONG_CORRECTION: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        (
            "no, …",
            r"^(?:no|nope|nah)\b(?![,!. ]+(?:problem|worries|rush|thanks|thank you|need\b))",
        ),
        (
            "that's not/wrong",
            r"\b(?:that|this|it)(?:'s| is| was) (?:not|wrong|incorrect)\b",
        ),
        (
            "you broke/forgot/…",
            r"\byou (?:broke|removed|deleted|forgot|missed|ignored|misunderstood"
            r"|didn'?t|did not|shouldn'?t|should not|weren'?t supposed)\b",
        ),
        (
            "revert/undo",
            r"\b(?:revert|undo|roll ?back) (?:that|this|it|your|those|these|all|the)\b",
        ),
        (
            "still failing",
            r"\bstill (?:failing|fails|broken|not working|doesn'?t work|does not work"
            r"|wrong|the same|errors?|crash(?:es|ing)?)\b",
        ),
        (
            "I said/told you",
            r"\bi (?:said|told you|asked (?:you )?(?:for|to|not)|meant|didn'?t ask"
            r"|did not ask|never asked|already (?:said|told))\b",
        ),
        ("not what I asked", r"\bnot what i (?:asked|wanted|meant|said)\b"),
        ("why did you", r"\bwhy (?:did|are|would|do) you\b"),
    )
)
_WEAK_CORRECTION: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        ("actually, …", r"^actually\b"),
        ("don't/stop/never", r"\b(?:don'?t|do not|never|stop)\b"),
        ("instead", r"\binstead\b"),
        ("wrong/incorrect", r"\b(?:wrong|incorrect)\b"),
        ("should/shouldn't", r"\b(?:should(?:n'?t| not)|should (?:be|use|have))\b"),
    )
)


def correction_confidence(text: str) -> tuple[float, list[str]]:
    """Confidence in [0, 1] that ``text`` is a correction, and the matched phrase labels.

    Any strong phrase gives 1.0. Otherwise one weak phrase gives 0.5 and two or
    more distinct weak phrases give 0.75. No match gives 0.
    """
    head = text.strip()[:CORRECTION_HEAD_CHARS].replace("\u2019", "'")
    strong = [label for label, pattern in _STRONG_CORRECTION if pattern.search(head)]
    if strong:
        return CORRECTION_STRONG, strong
    weak = [label for label, pattern in _WEAK_CORRECTION if pattern.search(head)]
    if len(weak) >= 2:
        return CORRECTION_TWO_WEAK, weak
    if weak:
        return CORRECTION_ONE_WEAK, weak
    return 0.0, []


@dataclass
class DetectContext:
    """What detectors need to know about the repository."""

    resolver: PathResolver
    #: True if a repo-relative path is (or looks like) a file rather than a directory.
    is_file: Callable[[str], bool]
    #: True if a repo-relative path is a file known from disk or from any transcript.
    is_known_file: Callable[[str], bool]


@dataclass
class _Action:
    call: Event
    result: Event | None = None
    file: str | None = None  # repo-relative path for read/edit/write
    cmd: ParsedCommand | None = None  # shell calls made inside the repo
    next_any: float | None = None  # time of the next transcript event after the call


def _epoch(event: Event | None) -> float | None:
    return event.ts.timestamp() if event is not None and event.ts is not None else None


def _capped(start: float | None, end: float | None) -> tuple[float, float] | None:
    """Interval [start, end] limited to TIME_CAP_SECONDS from its start."""
    if start is None or end is None or end <= start:
        return None
    return (start, min(end, start + TIME_CAP_SECONDS))


def _capped_back(start: float | None, end: float | None) -> tuple[float, float] | None:
    """Interval [start, end] limited to the last TIME_CAP_SECONDS before its end."""
    if start is None or end is None or end <= start:
        return None
    return (max(start, end - TIME_CAP_SECONDS), end)


def _scope(call: Event, ctx: DetectContext) -> str | None:
    """Repo-relative file or directory a search was scoped to (``""`` is the repo root)."""
    base = ctx.resolver.resolve(call.path or ".", call.cwd)
    pattern = call.pattern or ""
    if call.tool == "Glob" or (call.op == model.OP_SEARCH and call.tool.lower() == "glob"):
        literal: list[str] = []
        for part in pattern.replace("\\", "/").split("/")[:-1]:
            if any(ch in part for ch in "*?[{"):
                break
            literal.append(part)
        prefix = "/".join(literal)
        if pattern.startswith("/"):
            return ctx.resolver.resolve(prefix or "/", call.cwd)
        if prefix and base is not None:
            joined = posixpath.normpath(posixpath.join(base, prefix))
            return None if joined.startswith("..") else ("" if joined == "." else joined)
    return base


@dataclass
class _State:
    """Mutable per-stream detector state."""

    turn: list[_Action] = field(default_factory=list)
    prompts: int = 0
    turn_edits: dict[str, int] = field(default_factory=dict)
    thrash: dict[str, Finding] = field(default_factory=dict)
    reads: dict[tuple[str, str], int] = field(default_factory=dict)
    read_findings: dict[tuple[str, str], Finding] = field(default_factory=dict)
    edit_history: dict[str, dict[tuple[str, str], float | None]] = field(default_factory=dict)
    write_history: dict[str, list[str]] = field(default_factory=dict)
    write_times: dict[tuple[str, str], float | None] = field(default_factory=dict)
    fail_runs: dict[str, int] = field(default_factory=dict)
    retry: dict[str, Finding] = field(default_factory=dict)
    pending: list[tuple[Finding, str, int]] = field(default_factory=list)
    open_edits: dict[str, list[Finding]] = field(default_factory=dict)


class _Detector:
    def __init__(self, stream: Stream, ctx: DetectContext) -> None:
        self.stream = stream
        self.ctx = ctx
        self.findings: list[Finding] = []
        self.state = _State()

    # ---- helpers ---------------------------------------------------------------------

    def _emit(
        self,
        signal: str,
        event: Event,
        summary: str,
        targets: list[Target],
        *,
        excerpt: str = "",
        weight: float = 1.0,
        interval: tuple[float, float] | None = None,
        detail: dict[str, str] | None = None,
    ) -> Finding:
        finding = Finding(
            signal=signal,
            session_id=self.stream.session_id,
            source=str(self.stream.source),
            line=event.line,
            ts=event.ts,
            summary=summary,
            excerpt=excerpt,
            targets=targets,
            weight=weight,
            interval=interval,
            stream=self.stream.key,
            detail=detail or {},
        )
        self.findings.append(finding)
        return finding

    def _action_targets(self, action: _Action) -> list[Target]:
        if action.file is not None:
            return [Target(model.FILE, action.file)]
        if action.cmd is not None and action.cmd.key:
            return [Target(model.COMMAND, action.cmd.key)]
        return []

    def _round_trip(self, action: _Action) -> tuple[float, float] | None:
        """From a call until the next transcript event: the cost of one wasted round trip."""
        end = action.next_any if action.next_any is not None else _epoch(action.result)
        return _capped(_epoch(action.call), end)

    def _recover(self, finding: Finding, action: _Action) -> None:
        """Extend a failure's interval up to the successful ``action`` that resolved it."""
        if finding.interval is None:
            return
        end = _epoch(action.result) or _epoch(action.call)
        extended = _capped(finding.interval[0], end)
        if extended is not None and extended[1] > finding.interval[1]:
            finding.interval = extended

    # ---- pairing ---------------------------------------------------------------------

    def _items(self) -> list[_Action | Event]:
        items: list[_Action | Event] = []
        by_id: dict[str, _Action] = {}
        resolver = self.ctx.resolver
        for event in self.stream.events:
            if event.kind == model.TOOL_CALL:
                action = _Action(call=event)
                if event.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE):
                    rel = resolver.resolve(event.path, event.cwd)
                    action.file = rel or None
                elif event.op == model.OP_SHELL and event.command and resolver.contains(event.cwd):
                    parsed = parse_command(event.command)
                    if parsed.key:
                        action.cmd = parsed
                by_id[event.call_id] = action
                items.append(action)
            elif event.kind == model.TOOL_RESULT:
                paired = by_id.get(event.call_id)
                if paired is not None and paired.result is None:
                    paired.result = event
            else:
                items.append(event)
        next_any: float | None = None
        for item in reversed(items):
            if isinstance(item, _Action):
                item.next_any = next_any
                next_any = _epoch(item.call) or next_any
            else:
                next_any = _epoch(item) or next_any
        return items

    # ---- main loop -------------------------------------------------------------------

    def run(self) -> list[Finding]:
        state = self.state
        for item in self._items():
            if isinstance(item, _Action):
                self._on_action(item)
                state.turn.append(item)
            elif item.kind == model.INTERRUPT:
                self._on_interrupt(item)
            elif item.kind == model.USER_MESSAGE:
                if not self.stream.sidechain:
                    if not item.synthetic and state.prompts > 0 and state.turn:
                        self._on_prompt(item)
                    state.prompts += 1
                state.turn = []
                state.turn_edits = {}
                state.thrash = {}
                state.pending = []
                state.open_edits = {}
        return self.findings

    def _on_action(self, action: _Action) -> None:
        result = action.result
        if result is None:
            return
        op = action.call.op
        if result.error == model.ERR_REJECTED:
            self._on_rejected(action, result)
        elif result.error == model.ERR_INTERRUPTED:
            return
        elif op in (model.OP_EDIT, model.OP_WRITE):
            self._on_edit(action, result)
        elif op == model.OP_READ:
            self._on_read(action, result)
        elif op == model.OP_SEARCH:
            self._on_search(action, result)
        elif op == model.OP_SHELL:
            self._on_shell(action, result)

    # ---- file tools ------------------------------------------------------------------

    def _on_edit(self, action: _Action, result: Event) -> None:
        path = action.file
        if path is None:
            return
        call = action.call
        targets = [Target(model.FILE, path)]
        interval = self._round_trip(action)
        before = len(self.findings)
        if not result.ok:
            if result.error == model.ERR_EDIT_NOT_FOUND:
                self._emit(
                    EDIT_MISS,
                    result,
                    f"{call.tool} on {path} failed: the text to replace was not found",
                    targets,
                    excerpt=result.output,
                    interval=interval,
                    detail={"variant": "not-found"},
                )
            elif result.error == model.ERR_EDIT_AMBIGUOUS:
                count = result.matches or 0
                self._emit(
                    EDIT_MISS,
                    result,
                    f"{call.tool} on {path} failed: the text to replace matched "
                    f"{count or 'several'} places",
                    targets,
                    excerpt=result.output,
                    interval=interval,
                    detail={"variant": "ambiguous", "matches": str(count)},
                )
            elif result.error in (model.ERR_NOT_READ, model.ERR_STALE):
                stale = result.error == model.ERR_STALE
                reason = (
                    "the file changed on disk after it was read"
                    if stale
                    else "the file had not been read first"
                )
                self._emit(
                    STALE_EDIT,
                    result,
                    f"{call.tool} on {path} refused: {reason}",
                    targets,
                    excerpt=result.output,
                    interval=interval,
                    detail={"variant": "modified" if stale else "not-read"},
                )
            elif result.error == model.ERR_MISSING:
                self._path_miss(action, result)
                return
            self.state.open_edits.setdefault(path, []).extend(self.findings[before:])
            return
        for finding in self.state.open_edits.pop(path, []):
            self._recover(finding, action)
        self._on_edit_success(action, result, path)

    def _on_edit_success(self, action: _Action, result: Event, path: str) -> None:
        state = self.state
        call = action.call
        now = _epoch(call)
        targets = [Target(model.FILE, path)]
        # Any successful change resets read-loop tracking for the file.
        for key in [k for k in state.reads if k[0] == path]:
            del state.reads[key]
            state.read_findings.pop(key, None)
        # edit-thrash
        count = state.turn_edits.get(path, 0) + 1
        state.turn_edits[path] = count
        if count == EDIT_THRASH_MIN_EDITS:
            state.thrash[path] = self._emit(EDIT_THRASH, result, "", targets)
        if path in state.thrash:
            state.thrash[path].summary = f"{count} successful edits to {path} within one user turn"
            state.thrash[path].detail["count"] = str(count)
        # edit-revert
        reverted_at: float | None = None
        reverted = False
        history = state.edit_history.setdefault(path, {})
        for old, new in call.edits:
            if old == new:
                continue
            if (new, old) in history:
                reverted = True
                reverted_at = history.pop((new, old))
            else:
                history[(old, new)] = now
        if call.op == model.OP_WRITE and call.content_digest:
            versions = state.write_history.setdefault(path, [])
            if versions and versions[-1] != call.content_digest and call.content_digest in versions:
                reverted = True
                reverted_at = state.write_times.get((path, versions[-1]))
            versions.append(call.content_digest)
            state.write_times[(path, call.content_digest)] = now
        if reverted:
            self._emit(
                EDIT_REVERT,
                result,
                f"{call.tool} on {path} undid an earlier edit from the same session",
                targets,
                interval=_capped_back(reverted_at, now),
            )

    def _path_miss(self, action: _Action, result: Event) -> None:
        if action.file is None:
            return
        self._emit(
            PATH_MISS,
            result,
            f"{action.call.tool} targeted {action.file}, which does not exist",
            [Target(model.FILE, action.file)],
            excerpt=result.output,
            interval=self._round_trip(action),
        )

    def _on_read(self, action: _Action, result: Event) -> None:
        path = action.file
        if path is None:
            return
        if not result.ok:
            if result.error == model.ERR_MISSING:
                self._path_miss(action, result)
            return
        state = self.state
        key = (path, action.call.read_range)
        count = state.reads.get(key, 0) + 1
        state.reads[key] = count
        if count == READ_LOOP_MIN_READS:
            state.read_findings[key] = self._emit(
                READ_LOOP, action.call, "", [Target(model.FILE, path)]
            )
        if key in state.read_findings:
            finding = state.read_findings[key]
            finding.summary = f"{path} was read {count} times with no edit in between"
            finding.detail["count"] = str(count)

    def _on_search(self, action: _Action, result: Event) -> None:
        if not (result.ok and result.empty):
            return
        call = action.call
        scope = _scope(call, self.ctx)
        if scope is None:
            return
        is_file = bool(scope) and self.ctx.is_file(scope)
        where = scope or "the repo root"
        self._emit(
            SEARCH_MISS,
            result,
            f"{call.tool} search in {where} returned nothing",
            [Target(model.FILE if is_file else model.DIR, scope or ".")],
            excerpt=f"pattern: {call.pattern}" if call.pattern else "",
            interval=self._round_trip(action),
            detail={"pattern": call.pattern or "", "scope": scope or "."},
        )

    # ---- shell -----------------------------------------------------------------------

    def _on_shell(self, action: _Action, result: Event) -> None:
        cmd = action.cmd
        if cmd is None:
            return
        state = self.state
        key = cmd.key
        state.pending = [(f, k, n - 1) for f, k, n in state.pending if n > 1]
        if result.ok:
            state.fail_runs[key] = 0
            state.retry.pop(key, None)
            if cmd.program not in INSPECTION_PROGRAMS:
                for finding, failed_key, _ in state.pending:
                    if failed_key != key:
                        finding.detail["recovered_by"] = key
                    self._recover(finding, action)
                state.pending = []
            return
        code = result.exit_code
        if cmd.program in INSPECTION_PROGRAMS or (
            code == 1 and cmd.last_program in INSPECTION_PROGRAMS
        ):
            return
        call = action.call
        invocation = code in INVOCATION_EXIT_CODES or bool(
            INVOCATION_ERROR_RE.search(result.output)
        )
        targets = [Target(model.COMMAND, key)]
        # A setup/invocation error says nothing about the files on the command line.
        for candidate in () if invocation else path_candidates(cmd.tokens):
            rel = self.ctx.resolver.resolve(candidate, call.cwd)
            if rel and self.ctx.is_known_file(rel) and Target(model.FILE, rel) not in targets:
                targets.append(Target(model.FILE, rel))
            if len(targets) > COMMAND_MAX_FILES:
                break
        status = f"exit {code}" if code is not None else "error"
        kind = "setup/invocation error" if invocation else "ordinary failure"
        finding = self._emit(
            COMMAND_FAILURE,
            result,
            f"`{key}` failed ({status}; {kind})",
            targets,
            excerpt=f"$ {call.command}\n{result.output}",
            weight=1.0 if invocation else ORDINARY_FAILURE_WEIGHT,
            # An ordinary failure (a red test) is information, not lost time.
            interval=self._round_trip(action) if invocation else None,
            detail={
                "variant": "invocation" if invocation else "ordinary",
                "command": call.command or "",
            },
        )
        state.pending.append((finding, key, RECOVERY_LOOKAHEAD))
        run = state.fail_runs.get(key, 0) + 1
        state.fail_runs[key] = run
        if run == RETRY_LOOP_MIN_FAILURES:
            state.retry[key] = self._emit(RETRY_LOOP, result, "", [Target(model.COMMAND, key)])
        if key in state.retry:
            state.retry[key].summary = f"`{key}` failed {run} times in a row"
            state.retry[key].detail["count"] = str(run)

    # ---- human signals ---------------------------------------------------------------

    def _on_rejected(self, action: _Action, result: Event) -> None:
        call = action.call
        targets = self._action_targets(action)
        if call.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE) and not targets:
            return  # a file outside the repo
        if call.op == model.OP_SHELL and not targets:
            return
        what = targets[0].key if targets else call.tool
        what = f"`{what}`" if targets and targets[0].kind == model.COMMAND else what
        excerpt_parts = []
        if call.op == model.OP_SHELL and call.command:
            excerpt_parts.append(f"$ {call.command}")
        if result.reason:
            excerpt_parts.append(f"user said: {result.reason}")
        self._emit(
            TOOL_REJECTED,
            result,
            f"The user declined {call.tool} on {what}" + (" and said why" if result.reason else ""),
            targets,
            excerpt="\n".join(excerpt_parts),
            interval=self._round_trip(action),
            detail={"reason": result.reason, "command": call.command or ""},
        )

    def _turn_bounds(self) -> tuple[float | None, float | None]:
        turn = self.state.turn
        start = next((s for s in (_epoch(a.call) for a in turn) if s is not None), None)
        stamps = [s for a in turn for s in (_epoch(a.call), _epoch(a.result)) if s is not None]
        return start, max(stamps) if stamps else None

    def _on_interrupt(self, event: Event) -> None:
        turn = self.state.turn
        if not turn:
            return
        last = turn[-1]
        if last.result is not None and last.result.error == model.ERR_REJECTED:
            return  # already counted as tool-rejected
        targets = self._action_targets(last)
        if not targets:
            if last.call.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE, model.OP_SHELL):
                return  # the agent was working outside the repo
            what = f"running {last.call.tool}"
        elif targets[0].kind == model.COMMAND:
            what = f"running `{targets[0].key}`"
        else:
            what = f"working on {targets[0].key}"
        start, _ = self._turn_bounds()
        self._emit(
            USER_INTERRUPT,
            event,
            f"The user interrupted the agent while it was {what}",
            targets,
            interval=_capped_back(start, _epoch(event)),
        )

    def _on_prompt(self, event: Event) -> None:
        confidence, labels = correction_confidence(event.text)
        if confidence <= 0:
            return
        touched: list[str] = []  # most recent first
        edited: list[str] = []
        for action in reversed(self.state.turn):
            if action.file is None:
                continue
            if action.file not in touched:
                touched.append(action.file)
            if action.call.op in (model.OP_EDIT, model.OP_WRITE) and action.file not in edited:
                edited.append(action.file)
        named: list[str] = []
        for candidate in path_candidates(event.text[:CORRECTION_SCAN_CHARS].split()):
            rel = self.ctx.resolver.resolve(candidate, event.cwd)
            hits = [rel] if rel in touched else []
            if not hits and "/" not in candidate and "\\" not in candidate:
                by_name = [f for f in touched if f.rsplit("/", 1)[-1] == candidate]
                hits = by_name if len(by_name) == 1 else []
            named.extend(h for h in hits if h not in named)
        targets: list[Target]
        if named:
            targets = [Target(model.FILE, rel) for rel in named[:CORRECTION_MAX_FILES]]
            how = "names " + ", ".join(t.key for t in targets)
        elif edited:
            edited = edited[:CORRECTION_MAX_FILES]
            targets = [Target(model.FILE, rel, 1.0 / len(edited)) for rel in edited]
            how = "after edits to " + ", ".join(edited)
        else:
            targets = []
            how = "no file edited in the preceding turn, so not attributed to a path"
        start, end = self._turn_bounds()
        matched = ", ".join(f'"{label}"' for label in labels)
        self._emit(
            USER_CORRECTION,
            event,
            f"User correction (confidence {confidence:g}; matched {matched}); {how}",
            targets,
            excerpt=event.text,
            weight=confidence,
            interval=_capped_back(start, end),
            detail={"quote": event.text, "confidence": f"{confidence:g}"},
        )


def detect(stream: Stream, ctx: DetectContext) -> list[Finding]:
    """Run every transcript detector over one stream."""
    return _Detector(stream, ctx).run()
