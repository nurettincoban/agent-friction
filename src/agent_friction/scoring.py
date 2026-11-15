"""Scoring, time-lost estimation and confidence. Every constant is documented
in docs/methodology.md; nothing here depends on other hotspots in the report,
so a score means the same thing in every run.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agent_friction.model import Finding
from agent_friction.signals import BY_ID, EDIT_THRASH, HUMAN_REWORK, READ_LOOP, SEVERITY

#: Mean session points at which the severity factor reaches 1 - 1/e (about 0.63).
SEVERITY_SCALE = 1.5
#: Friction sessions at which the recurrence factor reaches 1 (log2(1 + 7) = 3 bits).
RECURRENCE_FULL_SESSIONS = 7
#: The consistency factor runs from this floor (rarely) to 1 (every time).
CONSISTENCY_FLOOR = 0.4
#: Clean pseudo-sessions added to the exposure, so 1 of 1 is not "always".
EXPOSURE_PRIOR_SESSIONS = 1
#: Sessions needed for "medium" and "high" confidence.
MEDIUM_MIN_SESSIONS = 2
HIGH_MIN_SESSIONS = 3
#: Distinct signal types needed for "high" confidence.
HIGH_MIN_SIGNALS = 2
#: Signals too weak to justify "high" confidence on their own.
WEAK_SIGNALS = frozenset({READ_LOOP, EDIT_THRASH})
#: Session ids with this prefix are agent commits (git), not transcript sessions.
GIT_SESSION_PREFIX = "git:"

Item = tuple[Finding, float]  # (finding, share of it attributed to this hotspot)


@dataclass(frozen=True)
class SessionPoints:
    session_id: str
    weighted_events: float
    points: float


@dataclass(frozen=True)
class Score:
    score: float
    intensity: float  # sum of session points
    mean_session_points: float
    severity: float  # how bad it is when it happens, 0..1
    recurrence: float  # how many sessions it happened in, 0..1 (log scale)
    consistency: float  # how reliably it happens when the agent goes there, 0.4..1
    friction_sessions: int
    exposure_sessions: int
    sessions: tuple[SessionPoints, ...]

    @property
    def friction_rate(self) -> float:
        if not self.exposure_sessions:
            return 0.0
        return round(self.friction_sessions / self.exposure_sessions, 2)


def session_points(weighted_events: float) -> float:
    """Diminishing returns inside one session: doubling the friction adds one point."""
    return math.log2(1.0 + weighted_events)


def score_items(items: Iterable[Item], exposure_sessions: int | None = None) -> Score:
    """Score a hotspot from its findings and the number of sessions that went there.

    1. Each finding contributes ``severity x weight x share`` to its session; a
       session's sum is squashed with ``log2(1 + sum)`` into *session points*.
    2. ``severity   = 1 - exp(-mean session points / 1.5)``
    3. ``recurrence = min(1, log2(1 + friction sessions) / 3)``
    4. ``consistency = 0.4 + 0.6 x friction sessions / (exposure sessions + 1)``
    5. ``score = 100 x severity x recurrence x consistency``

    ``exposure_sessions`` defaults to the number of friction sessions and is
    never taken to be smaller than that.
    """
    per_session: dict[str, float] = {}
    for finding, share in items:
        contribution = SEVERITY[finding.signal] * finding.weight * share
        per_session[finding.session_id] = per_session.get(finding.session_id, 0.0) + contribution
    sessions = tuple(
        SessionPoints(sid, round(raw, 3), round(session_points(raw), 3))
        for sid, raw in sorted(per_session.items())
    )
    count = len(per_session)
    exposure = max(exposure_sessions or 0, count)
    if not count:
        return Score(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, exposure, ())
    intensity = sum(session_points(raw) for raw in per_session.values())
    mean = intensity / count
    severity = 1.0 - math.exp(-mean / SEVERITY_SCALE)
    recurrence = min(1.0, math.log2(1 + count) / math.log2(1 + RECURRENCE_FULL_SESSIONS))
    rate = count / (exposure + EXPOSURE_PRIOR_SESSIONS)
    consistency = CONSISTENCY_FLOOR + (1.0 - CONSISTENCY_FLOOR) * rate
    return Score(
        score=round(100.0 * severity * recurrence * consistency, 1),
        intensity=round(intensity, 3),
        mean_session_points=round(mean, 3),
        severity=round(severity, 3),
        recurrence=round(recurrence, 3),
        consistency=round(consistency, 3),
        friction_sessions=count,
        exposure_sessions=exposure,
        sessions=sessions,
    )


def confidence(session_ids: Iterable[str], signal_ids: Iterable[str]) -> str:
    """``high``: 3+ transcript sessions and 2+ signal types (not only weak ones).
    ``medium``: 2+ transcript sessions. ``low``: otherwise.

    Git evidence never raises confidence: agent commits are not counted as
    sessions here and ``human-rework`` is not counted as a signal type.
    """
    sessions = len({s for s in session_ids if not s.startswith(GIT_SESSION_PREFIX)})
    kinds = set(signal_ids) - {HUMAN_REWORK}
    if (
        sessions >= HIGH_MIN_SESSIONS
        and len(kinds) >= HIGH_MIN_SIGNALS
        and not kinds <= WEAK_SIGNALS
    ):
        return "high"
    if sessions >= MEDIUM_MIN_SESSIONS and kinds:
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


def explain(
    signal_counts: Sequence[tuple[str, int]], sessions: int, exposure: int, level: str
) -> str:
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
        reason = "there is too little independent evidence so far"
    if exposure <= 1:
        where = "the only session that touched it"
    elif sessions >= exposure:
        where = f"all {exposure} sessions that touched it"
    else:
        where = f"{sessions} of the {exposure} sessions that touched it"
    return (
        f"{_plural(events, 'friction event')} in {where} ({top}); "
        f"{level} confidence because {reason}."
    )
