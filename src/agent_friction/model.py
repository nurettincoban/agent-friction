"""Normalized data model shared by adapters, detectors and the engine.

Adapters turn agent-specific transcripts into :class:`Stream` objects made of
:class:`Event` records. Detectors turn streams into :class:`Finding` records.
Everything downstream (scoring, recommendations, output) only sees findings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# Event kinds
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
USER_MESSAGE = "user_message"
INTERRUPT = "interrupt"
#: The agent wrote prose. Only the time is kept (the text never leaves the parser): it
#: marks agent activity, so that waiting for a human is not mistaken for lost time.
AGENT_MESSAGE = "agent_message"

# Canonical tool operations
OP_READ = "read"
OP_EDIT = "edit"
OP_WRITE = "write"
OP_SHELL = "shell"
OP_SEARCH = "search"
OP_OTHER = "other"
OPS = (OP_READ, OP_EDIT, OP_WRITE, OP_SHELL, OP_SEARCH, OP_OTHER)

# Result error classes (``Event.error``)
ERR_EDIT_NOT_FOUND = "edit_not_found"
ERR_EDIT_AMBIGUOUS = "edit_ambiguous"
ERR_NOT_READ = "not_read"
ERR_STALE = "stale"
ERR_MISSING = "missing"
ERR_REJECTED = "rejected"
#: Refused by permission settings, a hook or a safety check: no human was asked.
ERR_DENIED = "denied"
ERR_INTERRUPTED = "interrupted"
ERR_EXIT = "exit"
#: The harness never started the call (cancelled sibling, malformed input).
ERR_NOT_RUN = "not_run"
#: A read refused because the file is bigger than the harness will return in one piece.
ERR_TOO_LARGE = "too_large"
ERR_OTHER = "error"
ERROR_CLASSES = (
    ERR_EDIT_NOT_FOUND,
    ERR_EDIT_AMBIGUOUS,
    ERR_NOT_READ,
    ERR_STALE,
    ERR_MISSING,
    ERR_REJECTED,
    ERR_DENIED,
    ERR_INTERRUPTED,
    ERR_EXIT,
    ERR_NOT_RUN,
    ERR_TOO_LARGE,
    ERR_OTHER,
)

# Target kinds
FILE = "file"
DIR = "dir"
COMMAND = "command"


@dataclass
class Event:
    """One normalized transcript event. Unused fields keep their defaults."""

    kind: str
    line: int
    ts: datetime | None = None
    cwd: str | None = None
    # tool_call
    call_id: str = ""
    op: str = OP_OTHER
    tool: str = ""
    path: str | None = None
    command: str | None = None
    pattern: str | None = None
    read_range: str = ""
    edits: tuple[tuple[str, str], ...] = ()  # (digest(old), digest(new)) pairs
    #: Those of ``edits`` that only added text (the old text survives inside the new).
    insertions: frozenset[tuple[str, str]] = frozenset()
    content_digest: str = ""  # digest of full content for writes
    #: Text an edit or write put into the file (capped). Held in memory only, to see whether
    #: an earlier empty search was a check before creating something; never emitted.
    new_text: str = ""
    #: Text an edit replaced (capped), kept the same way: a later search for it that finds
    #: nothing confirms the removal and is not a miss.
    old_text: str = ""
    # tool_result
    ok: bool = True
    error: str = ""
    exit_code: int | None = None
    output: str = ""
    empty: bool = False  # a search that returned nothing
    matches: int | None = None  # N in "Found N matches"
    reason: str = ""  # the user's stated reason when rejecting a tool
    # user_message
    text: str = ""
    #: The result is the harness's own error message, not output of the tool.
    harness_error: bool = False
    synthetic: bool = False  # slash command etc.: a turn boundary, never a correction
    after_question: bool = False  # the agent's last words before this message were a question


@dataclass
class Stream:
    """An ordered run of events from one transcript file and one (sub)agent."""

    session_id: str
    source: Path
    kind: str  # "claude-code" | "generic"
    sidechain: bool = False  # subagent stream: its "user" turns are not human
    events: list[Event] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}|{self.session_id}|{int(self.sidechain)}"


@dataclass
class ParsedFile:
    path: Path
    kind: str
    streams: list[Stream]
    skipped_lines: int = 0  # malformed or structurally invalid lines
    ignored_lines: int = 0  # well-formed lines of a type we do not analyse


@dataclass(frozen=True)
class Target:
    """Where a finding is attributed. ``share`` splits one finding over several paths."""

    kind: str  # FILE | DIR | COMMAND
    key: str
    share: float = 1.0


@dataclass
class Finding:
    """One detected friction event."""

    signal: str
    session_id: str
    source: str  # transcript path, or "git"
    line: int
    ts: datetime | None
    summary: str  # structural description; never contains transcript free text
    excerpt: str = ""  # raw transcript text; redacted and truncated on output
    targets: list[Target] = field(default_factory=list)
    weight: float = 1.0  # per-event multiplier in (0, 1], see docs/methodology.md
    interval: tuple[float, float] | None = None  # epoch seconds, for time-lost
    #: Further pieces of claimed time, for an event made of several separate stretches.
    intervals: tuple[tuple[float, float], ...] = ()
    stream: str = ""  # intervals are only merged within one stream
    #: When the call behind the event ran (epoch seconds), whether or not it claims time.
    run: tuple[float, float] | None = None
    #: The episode of work it happened in (see docs/methodology.md); "" = the session.
    episode: str = ""

    @property
    def unit(self) -> str:
        """What recurrence is counted over: the episode, or the session if none is set."""
        return self.episode or self.session_id

    detail: dict[str, str] = field(default_factory=dict)
    fid: int = -1


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _whole(value: Any) -> Any:
    if isinstance(value, str):
        return _SURROGATE_RE.sub("\ufffd", value)
    if isinstance(value, list):
        return [_whole(item) for item in value]
    if isinstance(value, dict):
        return {_whole(key): _whole(item) for key, item in value.items()}
    return value


def load_json_line(raw: str) -> Any:
    """``json.loads`` for a transcript line, with nothing in it that cannot be written out.

    JSON may escape half of a surrogate pair (``"\\ud83d"``, a truncated emoji).
    Python reads that as a lone surrogate, which no UTF-8 writer accepts. Every
    such character becomes U+FFFD here, at the boundary, in keys and values.
    """
    obj = json.loads(raw)
    return _whole(obj) if "\\u" in raw else obj
