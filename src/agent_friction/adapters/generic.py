"""Adapter for the generic normalized JSONL event format (docs/schema.md).

One JSON object per line. Convert transcripts from any agent into this shape
and agent-friction analyses them with the same detectors as Claude Code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_friction import model
from agent_friction.adapters import common
from agent_friction.model import Event, ParsedFile, Stream

KIND = "generic"

#: Optional: only whether the text ends in a question is used (never the text itself).
ASSISTANT_MESSAGE = "assistant_message"
KINDS = (model.TOOL_CALL, model.TOOL_RESULT, model.USER_MESSAGE, model.INTERRUPT)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _event(obj: dict[str, Any], line: int) -> Event | None:
    kind = obj.get("kind")
    if kind not in KINDS:
        return None
    event = Event(kind=kind, line=line, ts=common.parse_ts(obj.get("ts")), cwd=_str(obj.get("cwd")))
    if kind in (model.TOOL_CALL, model.TOOL_RESULT):
        call_id = obj.get("id")
        if not isinstance(call_id, (str, int)) or isinstance(call_id, bool) or call_id == "":
            return None
        event.call_id = str(call_id)
    if kind == model.TOOL_CALL:
        op = obj.get("op")
        event.op = op if op in model.OPS else model.OP_OTHER
        event.tool = _str(obj.get("tool")) or event.op
        event.path = _str(obj.get("path"))
        event.command = _str(obj.get("command"))
        event.pattern = _str(obj.get("pattern"))
        event.read_range = str(obj.get("range", ""))
        old, new = obj.get("old"), obj.get("new")
        if isinstance(old, str) and isinstance(new, str):
            event.edits, event.insertions = common.edit_pairs([(old, new)])
        content = obj.get("content")
        if isinstance(content, str):
            event.content_digest = common.digest(content)
        event.new_text = common.new_text(new, content)
        event.old_text = common.new_text(old)
    elif kind == model.TOOL_RESULT:
        output = obj.get("output")
        text = output if isinstance(output, str) else ""
        code = obj.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            event.exit_code = code
        declared = obj.get("error")
        ok = obj.get("ok")
        failed = ok is False or (ok is None and (bool(declared) or bool(event.exit_code)))
        if failed:
            event.ok = False
            if declared in model.ERROR_CLASSES:
                event.error = str(declared)
            elif event.exit_code:
                event.error = model.ERR_EXIT
            elif common.denial(text) is not None:
                event.error, event.reason = model.ERR_DENIED, common.denial(text) or ""
            else:
                event.error, event.matches = common.classify_error(text)
        else:
            matches = obj.get("matches")
            if isinstance(matches, int) and not isinstance(matches, bool):
                event.empty = matches == 0
            elif obj.get("empty") is True:
                event.empty = True
            elif isinstance(output, str):
                event.empty = common.is_empty_search(text)
        event.reason = _str(obj.get("reason")) or ""
        if event.error == model.ERR_DENIED and not event.reason:
            event.reason = common.denial(text) or "permission"
        event.output = common.clip_output(text)
    elif kind == model.USER_MESSAGE:
        message = obj.get("text")
        if not isinstance(message, str) or not message.strip():
            return None
        event.text = message.strip()
    return event


def parse_file(path: Path, seen: dict[str, str] | None = None) -> ParsedFile:
    """Parse one generic-format ``.jsonl`` file (it may hold several sessions).

    ``seen`` de-duplicates across files: a line that an earlier file already
    contributed (same session, timestamp, kind and payload, i.e. the same
    text) is skipped, so a copied or re-exported log does not double its
    events. Repeated lines within one file are kept.
    """
    earlier = seen if seen is not None else {}
    mine: set[str] = set()
    streams: dict[tuple[str, bool], Stream] = {}
    parsed = ParsedFile(path=path, kind=KIND, streams=[])
    asking: dict[tuple[str, bool], bool] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except (ValueError, RecursionError):
                parsed.skipped_lines += 1
                continue
            if not isinstance(obj, dict):
                parsed.skipped_lines += 1
                continue
            fingerprint = "generic:" + common.digest(json.dumps(obj, sort_keys=True))
            if earlier.get(fingerprint, str(path)) != str(path):
                continue  # the same event, already read from another file
            mine.add(fingerprint)
            session = obj.get("session")
            if obj.get("kind") == ASSISTANT_MESSAGE and isinstance(session, (str, int)):
                text = obj.get("text")
                key = (str(session), obj.get("subagent") is True)
                asking[key] = isinstance(text, str) and common.asks_question(text)
                if key in streams:  # the time only: the agent was active
                    marker = Event(kind=model.AGENT_MESSAGE, line=number)
                    marker.ts = common.parse_ts(obj.get("ts"))
                    streams[key].events.append(marker)
                continue
            if obj.get("kind") not in KINDS:
                parsed.ignored_lines += 1
                continue
            event = _event(obj, number)
            if event is None or not isinstance(session, (str, int)) or session == "":
                parsed.skipped_lines += 1
                continue
            key = (str(session), obj.get("subagent") is True)
            if key not in streams:
                streams[key] = Stream(session_id=key[0], source=path, kind=KIND, sidechain=key[1])
            if event.kind == model.USER_MESSAGE:
                event.after_question = asking.pop(key, False)
            elif event.kind == model.TOOL_CALL:
                if common.QUESTION_TOOL_RE.search(event.tool):
                    asking[key] = True
                else:
                    asking.pop(key, None)
            streams[key].events.append(event)
    for fingerprint in mine:
        earlier.setdefault(fingerprint, str(path))
    parsed.streams = list(streams.values())
    return parsed
