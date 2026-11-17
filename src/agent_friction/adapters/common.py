"""Helpers shared by transcript adapters."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from agent_friction import model
from agent_friction.model import Event

#: Stored tool output is clipped to this many characters (head and tail are kept).
MAX_OUTPUT_CHARS = 4000

_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


#: Timestamps before this are placeholders (0, 1970-01-01), not when something happened.
EARLIEST_YEAR = 2000


def parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (or epoch seconds). Naive values are taken as UTC.

    Epoch numbers may be seconds, milliseconds, microseconds or nanoseconds (told
    apart by their size), given as a number or as a string of digits. Anything
    unparsable, earlier than ``EARLIEST_YEAR`` or more than a year ahead of today
    counts as missing: such a value is a placeholder or a unit mix-up, and one of
    them would stretch a session over decades.
    """
    parsed = _parse_ts(value)
    if parsed is None or parsed.year < EARLIEST_YEAR:
        return None
    return parsed if parsed.year <= datetime.now(timezone.utc).year + 1 else None


_EPOCH_RE = re.compile(r"^\d{9,19}(?:\.\d+)?$")


def _parse_ts(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and _EPOCH_RE.match(value.strip()):
        value = float(value)
    if isinstance(value, (int, float)):
        try:
            # Heuristic: 1e11 seconds is the year 5138. Larger values count in
            # milliseconds, microseconds or nanoseconds.
            seconds = float(value)
            for limit, unit in ((1e17, 1e9), (1e14, 1e6), (1e11, 1e3)):
                if seconds > limit:
                    seconds /= unit
                    break
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    match = _TS_RE.match(value.strip())
    if not match:
        return None
    date, clock, fraction, zone = match.groups()
    micro = (fraction or "0")[:6].ljust(6, "0")
    if zone in (None, "Z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = f"{zone[:3]}:{zone[3:]}"
    try:
        return datetime.fromisoformat(f"{date}T{clock}.{micro}{zone}")
    except ValueError:
        return None


#: Characters of written text kept in memory per call (see ``Event.new_text``).
NEW_TEXT_LIMIT = 200_000


def new_text(*parts: object) -> str:
    return "\n".join(p for p in parts if isinstance(p, str))[:NEW_TEXT_LIMIT]


#: Tools with which an agent asks the human to choose or confirm, in any harness.
QUESTION_TOOL_RE = re.compile(
    r"ask.?(?:user|human|followup|question)|user.?(?:question|input)|request.?(?:input|decision)"
    r"|exit.?plan|confirm",
    re.I,
)


#: A closing courtesy, not a question that a "no" could answer: "Anything else?",
#: "Let me know if you'd like changes?", "How does that look?".
_CLOSER_RE = re.compile(
    r"\b(?:anything (?:else|more|further)|let me know|what(?:'s| is| would you like)? next"
    r"|what else|what would you like|what do you think|how (?:does|do|did) (?:that|this|it|they)"
    r"|how(?:'s| is) that|any (?:questions|thoughts|feedback|concerns|other|further|more)"
    r"|(?:does|do|did) (?:that|this|it|these|they) (?:look|work|help|make sense|seem|feel)"
    r"|make sense|sound good|look good|looks? (?:right|ok|okay)|happy with|feel free)\b",
    re.I,
)
#: An offer to do something or a request to choose, which a "no" answers: "Should I
#: also rename it?", "Do you want tests?", "Which module?", "A or B?".
_OFFER_RE = re.compile(
    r"\b(?:should (?:i|we)|shall (?:i|we)|(?:do|did) you (?:want|need|prefer|mean)"
    r"|would you (?:like|prefer|rather)|want me to|like me to|(?:can|may|could|might) i\b"
    r"|is it (?:ok|okay|fine|alright)|(?:ok|okay|fine|alright) (?:if|to|with)"
    r"|(?:go ahead|proceed|continue)\b|which\b|or\b|(?:are|were) you\b"
    r"|is (?:that|this|it) (?:the|what)"
    r"|ready (?:to|for)|permission|confirm)",
    re.I,
)


def asks_question(text: str) -> bool:
    """Does agent prose end by offering something or asking the human to choose?

    "Want me to refactor util.py? It would take a few minutes." asks, although
    it does not end in a question mark; a "No, leave it" after it is an answer.
    "Done. Anything else?" and "Let me know if that works?" are closing
    courtesies: what the human says next is about the work, not about them.
    Only this one bit is kept of the text.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paragraphs:
        return False
    sentences = re.split(r"(?<=[.!?])\s+|\n", paragraphs[-1])
    questions = [s for s in sentences if s.rstrip(" *_)\"'").endswith("?")]
    return any(_OFFER_RE.search(q) is not None and _CLOSER_RE.search(q) is None for q in questions)


def edit_pairs(
    pairs: list[tuple[str, str]],
) -> tuple[tuple[tuple[str, str], ...], frozenset[tuple[str, str]]]:
    """``Event.edits`` and ``Event.insertions`` for (old, new) replacement texts."""
    hashed = tuple((digest(old), digest(new)) for old, new in pairs)
    added = frozenset(
        (digest(old), digest(new))
        for old, new in pairs
        if old.strip() and old != new and old in new
    )
    return hashed, added


def digest(text: str) -> str:
    """Short stable fingerprint used to compare edit strings without storing them."""
    return hashlib.sha1(text.encode("utf-8", "replace"), usedforsecurity=False).hexdigest()[:16]


def clip_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + "\n…\n" + text[-(limit - head) :]


#: Statuses of a background command that say how it ended (not "running", not "killed":
#: stopping a server the agent started is not a failure of the server).
_FINISHED = frozenset({"completed", "failed", "error", "exited", "done", "success"})
_FAILED = frozenset({"failed", "error"})
OUTCOME_SUFFIX = "#outcome"


def pair_background(events: list[Event]) -> list[Event]:
    """Give commands launched in the background the outcome that was reported later.

    The acknowledgement of a launch says nothing about how the command ended, so it
    is marked ``background`` (outcome unknown). When the outcome arrives
    (``BACKGROUND_OUTCOME``: a poll of the task, a notification), it becomes a
    call and a result of the launched command at that point of the transcript, to be
    judged like a foreground run. Outcomes of tasks that no shell call launched, and
    of tasks that were killed or are still running, are dropped.
    """
    calls: dict[str, Event] = {}
    launched: dict[str, Event] = {}
    reported: dict[str, Event] = {}
    out: list[Event] = []
    for event in events:
        if event.kind == model.TOOL_CALL:
            calls[event.call_id] = event
            if event.background and event.op == model.OP_SHELL:
                launched[event.call_id] = event
        elif event.kind == model.TOOL_RESULT:
            call = calls.get(event.call_id)
            launch = call is not None and call.op == model.OP_SHELL
            if launch and call is not None and (call.background or event.background):
                event.background = call.background = True
                launched[event.call_id] = call
                if event.task_id:
                    launched[event.task_id] = call
            else:
                event.background = False
        elif event.kind == model.BACKGROUND_OUTCOME:
            poll = calls.get(event.call_id)
            task = event.task_id or (poll.task_id if poll is not None else "")
            started = launched.get(task)
            status = event.task_status.lower()
            if started is None or (status not in _FINISHED and event.exit_code is None):
                continue
            if status in ("killed", "stopped", "running", "cancelled", "canceled"):
                continue
            earlier = reported.get(started.call_id)
            if earlier is not None:
                if not earlier.output and event.output:
                    earlier.output = event.output  # the notification came before the log
                continue
            code = event.exit_code
            failed = bool(code) or (status in _FAILED and code is None) or not event.ok
            call_id = started.call_id + OUTCOME_SUFFIX
            out.append(
                Event(
                    kind=model.TOOL_CALL,
                    line=event.line,
                    ts=event.ts,
                    cwd=started.cwd,
                    call_id=call_id,
                    op=model.OP_SHELL,
                    tool=started.tool,
                    command=started.command,
                    derived=True,
                )
            )
            result = Event(
                kind=model.TOOL_RESULT,
                line=event.line,
                ts=event.ts,
                cwd=started.cwd,
                call_id=call_id,
                ok=not failed,
                error=model.ERR_EXIT if failed else "",
                exit_code=code if code is not None else (1 if failed else 0),
                output=event.output,
            )
            reported[started.call_id] = result
            out.append(result)
            continue
        out.append(event)
    return out


_AMBIGUOUS_RE = re.compile(r"Found (\d+) matches of the string to replace", re.I)
_EXIT_RE = re.compile(r"^\s*(?:<tool_use_error>)?\s*(?:Error: )?Exit code (\d+)", re.I)
_REJECTED_PREFIXES = (
    "The user doesn't want to proceed with this tool use",
    "The user doesn't want to take this action right now",
)
_REASON_RE = re.compile(r"the user said:\s*(.+)", re.I | re.S)
_TAG_RE = re.compile(r"</?tool_use_error>")


def strip_error_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


#: The harness refused to start the call: a sibling in a parallel batch failed, or the
#: arguments did not validate. Nothing ran, so nothing failed.
_NOT_RUN_RE = re.compile(
    r"\s*(?:Error: )?(?:Cancelled: |InputValidationError\b|No such tool available\b"
    r"|Tool \S+ not found\b)",
    re.I,
)
_TOO_LARGE_RE = re.compile(
    r"exceeds (?:the )?maximum allowed (?:tokens|size)|File content \([^)]*\) exceeds", re.I
)
#: The harness's own refusal of a command pattern ("Blocked: sleep 30 followed by ...").
_BLOCKED_RE = re.compile(r"\s*(?:Error: )?Blocked: ")
#: User-role lines that carry hook output, not something the human typed.
HOOK_FEEDBACK_RE = re.compile(
    r"(?:[\w:.-]+ )?hook (?:feedback|blocking error|error|additional context|output)\b"
    r"|(?:Pre|Post)ToolUse:|(?:Stop|SubagentStop|SessionStart|UserPromptSubmit|PreCompact"
    r"|Notification) hook\b",
    re.I,
)


def classify_error(text: str) -> tuple[str, int | None]:
    """Map a failed tool result's text to an error class and the "N matches" count.

    The strings are the ones Claude Code's built-in tools emit. Other agents can
    either reuse them or set ``error`` explicitly in the generic format.
    """
    lowered = text.lower()
    if _NOT_RUN_RE.match(text):
        return model.ERR_NOT_RUN, None
    if _TOO_LARGE_RE.search(text):
        return model.ERR_TOO_LARGE, None
    if "string to replace not found" in lowered or "string not found in file" in lowered:
        return model.ERR_EDIT_NOT_FOUND, None
    ambiguous = _AMBIGUOUS_RE.search(text)
    if ambiguous:
        return model.ERR_EDIT_AMBIGUOUS, int(ambiguous.group(1))
    if "has not been read yet" in lowered:
        return model.ERR_NOT_READ, None
    if "modified since read" in lowered or "unexpectedly modified" in lowered:
        return model.ERR_STALE, None
    if (
        "file does not exist" in lowered
        or "no such file or directory" in lowered
        or "enoent" in lowered
    ):
        return model.ERR_MISSING, None
    return model.ERR_OTHER, None


def rejection_reason(text: str) -> str | None:
    """The user's reason if ``text`` is a declined-tool result, ``""`` if none given."""
    stripped = strip_error_tags(text)
    if not stripped.startswith(_REJECTED_PREFIXES):
        return None
    match = _REASON_RE.search(stripped)
    return match.group(1).strip() if match else ""


#: Results the harness writes when a tool call was refused before it ran, without a
#: human declining it: missing permission, a deny rule, a hook, a safety check.
_DENIED_RE = re.compile(
    r"\s*(?:Error: )?(?:"
    r"Claude requested permissions? to [^\n]{1,300}, but you haven't granted it yet"
    r"|Permission to use \S+(?: with command [^\n]*)? has been (?:auto-)?denied"
    r"|Permission for this (?:command|tool|action|tool use) (?:was|has been) denied"
    r"|\S* ?(?:PreToolUse|PermissionRequest):\S* hook (?:error|blocking error|denied)"
    r"|[^\n]{0,80}\bhook (?:blocking error|returned blocking error|blocked this)"
    r"|This command requires approval"
    r"|(?:The )?(?:command|tool use|action) was blocked (?:by|for)\b"
    r")",
    re.I,
)
_HOOK_RE = re.compile(r"\bhook\b", re.I)
_CLASSIFIER_RE = re.compile(r"\bdenied by the (?:Claude Code )?auto[- ]mode classifier\b", re.I)
_SAFETY_CHECK_RE = re.compile(r"\bdenied by a built-in (?:Claude Code )?safety check\b", re.I)


def denial(text: str) -> str | None:
    """``"hook"`` or ``"permission"`` if ``text`` is a policy denial, else ``None``."""
    stripped = strip_error_tags(text)
    if _BLOCKED_RE.match(stripped):
        return "blocked"
    match = _DENIED_RE.match(stripped)
    if match is None:
        return None
    if _HOOK_RE.search(match.group(0)):
        return "hook"
    # Who refused, where the harness says so in the sentence that refuses: the auto-mode
    # classifier and the built-in safety check are not something a permission rule changes.
    said = stripped[: match.end() + 120].split("\n", 1)[0]
    if _CLASSIFIER_RE.search(said):
        return "classifier"
    if _SAFETY_CHECK_RE.search(said):
        return "safety"
    return "permission"


def exit_code(text: str) -> int | None:
    match = _EXIT_RE.match(text)
    return int(match.group(1)) if match else None


_EMPTY_SEARCH_PREFIXES = ("no matches found", "no files found")


def is_empty_search(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped == "" or stripped.startswith(_EMPTY_SEARCH_PREFIXES)
