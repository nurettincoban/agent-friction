"""Helpers shared by transcript adapters."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from agent_friction import model

#: Stored tool output is clipped to this many characters (head and tail are kept).
MAX_OUTPUT_CHARS = 4000

_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (or epoch seconds). Naive values are taken as UTC."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            # Heuristic: values this large are epoch milliseconds.
            seconds = value / 1000 if value > 1e11 else value
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


def asks_question(text: str) -> bool:
    """Does agent prose put a question to the human in its final paragraph?

    "Want me to refactor util.py? It would take a few minutes." asks, although
    it does not end in a question mark. Only this one bit is kept of the text.
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return bool(paragraphs) and "?" in paragraphs[-1]


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


def classify_error(text: str) -> tuple[str, int | None]:
    """Map a failed tool result's text to an error class and the "N matches" count.

    The strings are the ones Claude Code's built-in tools emit. Other agents can
    either reuse them or set ``error`` explicitly in the generic format.
    """
    lowered = text.lower()
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


def denial(text: str) -> str | None:
    """``"hook"`` or ``"permission"`` if ``text`` is a policy denial, else ``None``."""
    stripped = strip_error_tags(text)
    match = _DENIED_RE.match(stripped)
    if match is None:
        return None
    return "hook" if _HOOK_RE.search(match.group(0)) else "permission"


def exit_code(text: str) -> int | None:
    match = _EXIT_RE.match(text)
    return int(match.group(1)) if match else None


_EMPTY_SEARCH_PREFIXES = ("no matches found", "no files found")


def is_empty_search(text: str) -> bool:
    stripped = text.strip().lower()
    return stripped == "" or stripped.startswith(_EMPTY_SEARCH_PREFIXES)
