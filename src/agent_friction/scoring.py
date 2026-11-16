"""Scoring, time-lost estimation and confidence. Every constant is documented
in docs/methodology.md; nothing here depends on other hotspots in the report,
so a score means the same thing in every run.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from agent_friction.model import Finding
from agent_friction.signals import (
    COMMAND_FAILURE,
    EDIT_THRASH,
    HUMAN_REWORK,
    NO_CONVERGENCE,
    PATH_MISS,
    PROBABLE_CORRECTION,
    READ_LOOP,
    RETRY_LOOP,
    SEARCH_MISS,
    SEVERITY,
)

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
#: Signals that routinely accompany productive work (many edits in one sitting, re-reading
#: a file, a search that finds nothing, a follow-up that merely sounds like a correction).
#: A target with nothing but these is a lead at most: its score is capped, it gets no
#: recommendation, and it cannot fail a build on its own.
WEAK_ALONE_SIGNALS = frozenset({READ_LOOP, EDIT_THRASH, SEARCH_MISS, PROBABLE_CORRECTION})
WEAK_ONLY_SCORE_CAP = 19.0
#: The same signals are too weak to justify "high" confidence on their own.
WEAK_SIGNALS = WEAK_ALONE_SIGNALS
#: Signals computed from other findings. Next to their source they are the same evidence
#: counted again, so they do not add a distinct signal type for confidence.
DERIVED_SIGNALS = {RETRY_LOOP: COMMAND_FAILURE, NO_CONVERGENCE: COMMAND_FAILURE}
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
        per_session[finding.unit] = per_session.get(finding.unit, 0.0) + contribution
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
    """``high``: 3+ transcript sessions and 2+ signal types, weak ones not counted.
    ``medium``: 2+ transcript sessions. ``low``: otherwise.

    Git evidence never raises confidence: agent commits are not counted as
    sessions here and ``human-rework`` is not counted as a signal type.
    """
    sessions = len({s for s in session_ids if not s.startswith(GIT_SESSION_PREFIX)})
    kinds = set(signal_ids) - {HUMAN_REWORK}
    for derived, source in DERIVED_SIGNALS.items():
        if source in kinds:
            kinds.discard(derived)  # one phenomenon, not two kinds of evidence
    # Weak kinds never corroborate: many edits next to a check that will not pass are
    # one stall seen twice, not two kinds of evidence.
    if sessions >= HIGH_MIN_SESSIONS and len(kinds - WEAK_SIGNALS) >= HIGH_MIN_SIGNALS:
        return "high"
    if sessions >= MEDIUM_MIN_SESSIONS and kinds:
        return "medium"
    return "low"


def claimed(finding: Finding) -> list[tuple[float, float]]:
    """Every stretch of time a finding claims."""
    first = [finding.interval] if finding.interval is not None else []
    return [*first, *finding.intervals]


def lost_seconds(findings: Iterable[Finding]) -> float:
    """Length of the union of the findings' intervals, merged per stream."""
    by_stream: dict[str, list[tuple[float, float]]] = {}
    for finding in findings:
        for interval in claimed(finding):
            by_stream.setdefault(finding.stream, []).append(interval)
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
    if count == 1:
        return f"{count} {noun}"
    return f"{count} {noun}es" if noun.endswith(("s", "sh", "ch", "x")) else f"{count} {noun}s"


#: How each signal is counted in a sentence: (one, several). Written out, because a
#: phrase cannot be pluralized by rule ("stretches that never went green").
SIGNAL_NOUNS: dict[str, tuple[str, str]] = {
    "user-correction": ("user correction", "user corrections"),
    "retry-loop": ("retry loop", "retry loops"),
    "tool-rejected": ("declined tool call", "declined tool calls"),
    "user-interrupt": ("user interrupt", "user interrupts"),
    "edit-miss": ("edit miss", "edit misses"),
    "edit-revert": ("reverted edit", "reverted edits"),
    "command-failure": ("command failure", "command failures"),
    "human-rework": ("human rework commit", "human rework commits"),
    "edit-thrash": ("edit thrash episode", "edit thrash episodes"),
    "permission-denied": ("permission denial", "permission denials"),
    "stale-edit": ("stale edit", "stale edits"),
    "path-miss": ("path miss", "path misses"),
    "search-miss": ("search miss", "search misses"),
    "read-loop": ("read loop", "read loops"),
    "probable-correction": ("probable correction", "probable corrections"),
    "file-too-large": (
        "refused read of a too-large file",
        "refused reads of a too-large file",
    ),
    "no-convergence": (
        "edit-and-run stretch that never went green",
        "edit-and-run stretches that never went green",
    ),
}


def count_signal(signal_id: str, count: int) -> str:
    """``"3 edit misses"``: a signal counted in words."""
    one, several = SIGNAL_NOUNS[signal_id]
    return f"{count} {one if count == 1 else several}"


def explain(
    signal_counts: Sequence[tuple[str, int]],
    sessions: int,
    exposure: int,
    level: str,
    agent_commits: int = 0,
    capped: bool = False,
    episodes: int = 0,
) -> str:
    """One-sentence reason for a hotspot's score and confidence.

    ``sessions`` and ``exposure`` count transcript sessions; ``agent_commits``
    are git commits with rework that no transcript session accounts for.
    """
    events = sum(count for _, count in signal_counts)
    top = ", ".join(count_signal(sid, count) for sid, count in signal_counts[:3])
    more = len(signal_counts) - 3
    if more > 0:
        top += f", {more} more signal type{'s' if more > 1 else ''}"
    if level == "high":
        reason = "it recurs across sessions with several kinds of evidence"
    elif level == "medium":
        reason = "it recurs across sessions"
    else:
        reason = "there is too little independent evidence so far"
    if exposure <= 0:
        where = ""
    elif exposure == 1:
        where = "the only session that touched it"
    elif sessions >= exposure:
        where = f"all {exposure} sessions that touched it"
    else:
        where = f"{sessions} of the {exposure} sessions that touched it"
    if episodes > max(sessions, 1) and where:
        # one long-running session id that was picked up again and again
        where += f" (in {episodes} separate episodes of work)"
        reason = reason.replace("across sessions", "across episodes of work")
    if agent_commits:
        commits = _plural(agent_commits, "agent commit")
        where = f"{where} and {commits}" if where else commits
    text = (
        f"{_plural(events, 'friction event')} in {where} ({top}); "
        f"{level} confidence because {reason}."
    )
    if capped:
        text += (
            f" Only weak signals, which also accompany productive work: the score is capped "
            f"at {WEAK_ONLY_SCORE_CAP:g} and nothing is recommended."
        )
    return text


def is_weak(finding: Finding) -> bool:
    """Evidence that is routine on its own: a weak signal, or an *ordinary* command failure.

    A command that merely exits non-zero (a red test, a probe that answers
    "no", an ad-hoc snippet that raises) is what commands do. Only setup and
    invocation errors, retry loops and human reactions make a command a hotspot.
    """
    if finding.signal == COMMAND_FAILURE:
        detail = finding.detail
        if "fixed_by_setup" in detail or "recurring_red" in detail or "standing_test" in detail:
            # it took a setup step to pass, or it stays red session after session
            return False
        # an ordinary failure, or a mistyped name that the agent then spelled correctly
        return detail.get("variant") == "ordinary" or "typo_of" in detail
    if finding.signal == NO_CONVERGENCE:
        return finding.detail.get("uncertain") == "1"  # its end was not seen
    if finding.signal == PATH_MISS:
        return finding.detail.get("probe") == "1"  # looking for an optional README, .env, ...
    return finding.signal in WEAK_ALONE_SIGNALS


def wanted_hit(finding: Finding) -> bool:
    """Did the agent behind this search miss go on looking, and not for leftovers?"""
    return "wanted" in finding.detail and "hygiene" not in finding.detail


def weak_only(findings: Iterable[Finding]) -> bool:
    """True if there is evidence and all of it is of the weak-alone kind.

    One thing lifts a search miss out of that class: the *same* pattern missed
    in two or more sessions, each time by an agent that then went on looking
    (``wanted``). A name that agents keep looking for is not a one-off probe
    before creating something new, and not a check that no debug statement is
    left.
    """
    found = list(findings)
    stuck = {f.unit for f in found if f.signal == NO_CONVERGENCE and not is_weak(f)}
    if not found or not all(is_weak(f) or f.signal == NO_CONVERGENCE for f in found):
        return False
    if len(stuck) >= MEDIUM_MIN_SESSIONS:
        return False  # one stuck session can be a hard task; the same check twice is a pattern
    sessions_by_pattern: dict[str, set[str]] = {}
    for finding in found:
        pattern = finding.detail.get("pattern", "").casefold()
        if finding.signal == SEARCH_MISS and pattern and wanted_hit(finding):
            sessions_by_pattern.setdefault(pattern, set()).add(finding.unit)
    return not any(len(s) >= MEDIUM_MIN_SESSIONS for s in sessions_by_pattern.values())
