"""Transcript adapters: turn agent-specific logs into normalized streams."""

from __future__ import annotations

import json
from itertools import islice
from pathlib import Path

from agent_friction.adapters import claude_code, generic
from agent_friction.model import ParsedFile

#: Lines inspected when sniffing a file's format.
SNIFF_LINES = 25

__all__ = ["claude_code", "detect_format", "generic", "parse_transcript"]


def detect_format(path: Path) -> str:
    """Return ``"generic"`` or ``"claude-code"`` by looking at the first lines.

    A line with string ``kind`` and a ``session`` key is the generic format.
    Anything else is treated as Claude Code, whose parser ignores unknown lines.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for raw in islice(handle, SNIFF_LINES):
                try:
                    obj = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if isinstance(obj.get("kind"), str) and "session" in obj:
                    return generic.KIND
                if "type" in obj and ("sessionId" in obj or "uuid" in obj):
                    return claude_code.KIND
    except OSError:
        pass
    return claude_code.KIND


def parse_transcript(path: Path, seen_uuids: dict[str, str] | None = None) -> ParsedFile:
    """Parse a transcript file in whichever supported format it is in."""
    if detect_format(path) == generic.KIND:
        return generic.parse_file(path, seen_uuids)
    return claude_code.parse_file(path, seen_uuids)
