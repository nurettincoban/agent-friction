"""Normalized data model shared by adapters, detectors and the engine.

Adapters turn agent-specific transcripts into :class:`Stream` objects made of
:class:`Event` records. Detectors turn streams into :class:`Finding` records.
Everything downstream (scoring, recommendations, output) only sees findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Event kinds
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
USER_MESSAGE = "user_message"
INTERRUPT = "interrupt"

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
ERR_INTERRUPTED = "interrupted"
ERR_EXIT = "exit"
ERR_OTHER = "error"
ERROR_CLASSES = (
    ERR_EDIT_NOT_FOUND,
    ERR_EDIT_AMBIGUOUS,
    ERR_NOT_READ,
    ERR_STALE,
    ERR_MISSING,
    ERR_REJECTED,
    ERR_INTERRUPTED,
    ERR_EXIT,
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
    content_digest: str = ""  # digest of full content for writes
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
    stream: str = ""  # intervals are only merged within one stream
    detail: dict[str, str] = field(default_factory=dict)
    fid: int = -1
