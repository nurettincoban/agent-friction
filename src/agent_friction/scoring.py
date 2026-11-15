"""Scoring, time-lost estimation and confidence. Every constant is documented
in docs/methodology.md; nothing here depends on other hotspots in the report,
so a score means the same thing in every run.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agent_friction.model import Finding
from agent_friction.signals import BY_ID, EDIT_THRASH, READ_LOOP, SEVERITY

#: Intensity at which the score reaches 1 - 1/e, about 63 out of 100.
SCORE_SCALE = 10.0
#: Sessions needed for "medium" and "high" confidence.
MEDIUM_MIN_SESSIONS = 2
HIGH_MIN_SESSIONS = 3
#: Distinct signal types needed for "high" confidence.
HIGH_MIN_SIGNALS = 2
#: Signals too weak to justify "high" confidence on their own.
WEAK_SIGNALS = frozenset({READ_LOOP, EDIT_THRASH})

Item = tuple[Finding, float]  # (finding, share of it attributed to this hotspot)


@dataclass(frozen=True)
class SessionPoints:
    session_id: str
    weighted_events: float
    points: float


@dataclass(frozen=True)
class Score:
    score: float
    intensity: float
    sessions: tuple[SessionPoints, ...]


def session_points(weighted_events: float) -> float:
    """Diminishing returns inside one session: doubling the friction adds one point."""
    return math.log2(1.0 + weighted_events)


def score_items(items: Iterable[Item]) -> Score:
    """Score a hotspot from its findings.

    1. Each finding contributes ``severity x weight x share`` to its session.
    2. Each session's sum is squashed with ``log2(1 + sum)``.
    3. Session points are added up (recurrence across sessions is linear).
    4. ``score = 100 x (1 - exp(-intensity / SCORE_SCALE))``.
    """
    per_session: dict[str, float] = {}
    for finding, share in items:
        contribution = SEVERITY[finding.signal] * finding.weight * share
        per_session[finding.session_id] = per_session.get(finding.session_id, 0.0) + contribution
    sessions = tuple(
        SessionPoints(sid, round(raw, 3), round(session_points(raw), 3))
        for sid, raw in sorted(per_session.items())
    )
    intensity = sum(session_points(raw) for raw in per_session.values())
    score = 100.0 * (1.0 - math.exp(-intensity / SCORE_SCALE))
    return Score(round(score, 1), round(intensity, 3), sessions)


def confidence(sessions: int, signal_ids: Iterable[str]) -> str:
    """``high``: 3+ sessions and 2+ signal types (not only weak ones).
    ``medium``: 2+ sessions. ``low``: a single session."""
    signals = set(signal_ids)
    if (
        sessions >= HIGH_MIN_SESSIONS
        and len(signals) >= HIGH_MIN_SIGNALS
        and not signals <= WEAK_SIGNALS
    ):
        return "high"
    if sessions >= MEDIUM_MIN_SESSIONS:
        return "medium"
    return "low"


def lost_seconds(findings: Iterable[Finding]) -> float:
    """Length of the union of the findings' intervals, merged per stream."""
    by_stream: dict[str, list[tuple[float, float]]] = {}
    for finding in findings:
        if finding.interval is not None:
            by_stream.setdefault(finding.stream, []).append(finding.interval)
    total = 0.0
    for intervals in by_stream.values():
        intervals.sort()
        start, end = intervals[0]
        for nxt_start, nxt_end in intervals[1:]:
            if nxt_start > end:
                total += end - start
                start, end = nxt_start, nxt_end
            else:
                end = max(end, nxt_end)
        total += end - start
    return total


def lost_minutes(findings: Iterable[Finding]) -> float:
    return round(lost_seconds(findings) / 60.0, 1)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def explain(signal_counts: Sequence[tuple[str, int]], sessions: int, level: str) -> str:
    """One-sentence reason for a hotspot's score and confidence."""
    events = sum(count for _, count in signal_counts)
    top = ", ".join(f"{count} {BY_ID[sid].title.lower()}" for sid, count in signal_counts[:3])
    more = len(signal_counts) - 3
    if more > 0:
        top += f", {more} more signal type{'s' if more > 1 else ''}"
    if level == "high":
        reason = "it recurs across sessions with several kinds of evidence"
    elif level == "medium":
        reason = "it recurs across sessions"
    else:
        reason = "it was seen in a single session so far"
    return (
        f"{_plural(events, 'friction event')} in {_plural(sessions, 'session')} ({top}); "
        f"{level} confidence because {reason}."
    )
