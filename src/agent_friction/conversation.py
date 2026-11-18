"""What the humans said, session by session: the raw material for a diagnosis.

The detectors decide which human messages are corrections, and they miss phrasing they
do not know ("Thanks. One thing though…"). This module decides nothing: it lists every
message a human typed after the agent had started working, every reason typed into a
refusal and every interruption, each with what the agent had just done. A reader (a
person, or the session that runs ``/agent-friction:scan``) can then judge them all,
including the ones no rule recognised.

Only built when excerpts are on: it is transcript text through and through.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from agent_friction import model
from agent_friction.model import Event, Stream

#: Characters kept of a human message, and of a session's opening request.
TURN_CHARS = 400
OPENING_CHARS = 280
#: Agent actions shown before each human turn, at most (the most recent ones).
ACTIONS_BEFORE = 3
#: Characters of a command or path in an action line.
ACTION_CHARS = 120
#: Turns listed for the whole scan, at most (newest sessions win when cut).
MAX_TURNS = 200


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _action(
    call: Event,
    result: Event | None,
    clip: Callable[[str, int], str],
    where: Callable[[Event], str],
) -> str:
    """One line: what the call did and how it ended."""
    tool = call.tool or call.op
    subject = call.command or where(call) or call.pattern or ""
    line = f"{tool} {clip(subject, ACTION_CHARS)}".strip()
    if result is None:
        return line + " (no result)"
    if result.ok:
        return line + (" → nothing found" if result.empty else " → ok")
    if result.error == model.ERR_REJECTED:
        return line + " → declined by the human"
    if result.error == model.ERR_INTERRUPTED:
        return line + " → interrupted"
    if result.exit_code is not None:
        return line + f" → exit {result.exit_code}"
    return line + f" → failed ({result.error or 'error'})"


def session_turns(
    streams: Sequence[Stream],
    clip: Callable[[str, int], str],
    label: Callable[[Stream], str | None],
    where: Callable[[Event], str] = lambda call: call.path or "",
) -> list[dict[str, Any]]:
    """Per main session: the opening request and every later human turn, in order."""
    sessions: list[dict[str, Any]] = []
    for stream in streams:
        if stream.sidechain:
            continue  # a subagent's "user" turns are its parent's prompts, not a human's
        calls: dict[str, Event] = {}
        results: dict[str, Event] = {}
        recent: list[str] = []  # call ids since the last human turn
        opening: Event | None = None
        turns: list[dict[str, Any]] = []
        for event in stream.events:
            if event.kind == model.TOOL_CALL and not event.derived:
                calls[event.call_id] = event
                recent.append(event.call_id)
            elif event.kind == model.TOOL_RESULT:
                results[event.call_id] = event
                if event.error == model.ERR_REJECTED and event.reason.strip():
                    call = calls.get(event.call_id)
                    turns.append(
                        {
                            "kind": "declined",
                            "line": event.line,
                            "timestamp": _iso(event.ts),
                            "text": clip(event.reason, TURN_CHARS),
                            "after": [_action(call, event, clip, where)] if call else [],
                        }
                    )
                    recent = []
            elif event.kind in (model.USER_MESSAGE, model.INTERRUPT):
                if event.kind == model.USER_MESSAGE and (event.synthetic or not event.text):
                    continue
                first = opening is None and not turns and not recent
                if first and event.kind == model.USER_MESSAGE:
                    opening = event
                    continue
                after = [
                    _action(calls[cid], results.get(cid), clip, where)
                    for cid in recent[-ACTIONS_BEFORE:]
                    if cid in calls
                ]
                turns.append(
                    {
                        "kind": "interrupt" if event.kind == model.INTERRUPT else "message",
                        "line": event.line,
                        "timestamp": _iso(event.ts),
                        "text": clip(event.text, TURN_CHARS),
                        "after": after,
                        **({"after_done_claim": True} if event.after_claim else {}),
                        **({"after_question": True} if event.after_question else {}),
                    }
                )
                recent = []
        if opening is None and not turns:
            continue
        stamps = [e.ts for e in stream.events if e.ts is not None]
        sessions.append(
            {
                "session_id": stream.session_id,
                "source": label(stream),
                "started": _iso(min(stamps)) if stamps else None,
                "opening": clip(opening.text, OPENING_CHARS) if opening else "",
                "tool_calls": len(calls),
                "turns": turns,
            }
        )
    sessions.sort(key=lambda s: (s["started"] or "", s["session_id"]))
    total = 0
    kept: list[dict[str, Any]] = []
    for session in reversed(sessions):  # newest first when the budget runs out
        if total + len(session["turns"]) > MAX_TURNS:
            had = len(session["turns"])
            room = max(0, MAX_TURNS - total)
            session = {**session, "turns": session["turns"][had - room :] if room else []}
            session["cut"] = had - len(session["turns"])  # turns left out
        total += len(session["turns"])
        kept.append(session)
    return list(reversed(kept))
