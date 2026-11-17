"""Scoring, time-lost estimation and confidence. Every constant is documented
in docs/methodology.md; nothing here depends on other hotspots in the report,
so a score means the same thing in every run.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from agent_friction.model import Finding
from agent_friction.signals import (
    COMMAND_FAILURE,
    EDIT_MISS,
    EDIT_THRASH,
    HUMAN_REWORK,
    NO_CONVERGENCE,
    PATH_MISS,
    PERMISSION_DENIED,
    PROBABLE_CORRECTION,
    READ_LOOP,
    RETRY_LOOP,
    SEARCH_MISS,
    SEVERITY,
    STALE_EDIT,
    TOOL_FAILURE,
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
WEAK_ALONE_SIGNALS = frozenset(
    {READ_LOOP, EDIT_THRASH, SEARCH_MISS, PROBABLE_CORRECTION, TOOL_FAILURE}
)
WEAK_ONLY_SCORE_CAP = 19.0
#: The same signals are too weak to justify "high" confidence on their own.
WEAK_SIGNALS = WEAK_ALONE_SIGNALS
#: Signals computed from other findings. Next to their source they are the same evidence
#: counted again, so they do not add a distinct signal type for confidence.
DERIVED_SIGNALS = {RETRY_LOOP: COMMAND_FAILURE, NO_CONVERGENCE: COMMAND_FAILURE}
#: Mechanical signals that happen at some rate wherever agents edit files. A file is
#: singled out for one of them only if it shows clearly more than the repository's rate.
BASELINE_SIGNALS = frozenset({EDIT_MISS, STALE_EDIT})
#: The rate assumed where the repository's own data shows less (or too little to say):
#: one visit in twenty. A *visit* is one session (episode) touching one file.
BASELINE_FLOOR = 0.05
#: Visits to other files needed before the repository's own rate is used instead.
BASELINE_MIN_VISITS = 20
#: False discovery rate accepted among the files called "above the baseline".
BASELINE_ALPHA = 0.05
#: Marks a finding that is no more than the background rate (``Finding.detail``).
WITHIN_BASELINE = "within_baseline"
#: Finding detail: the file stands out, but only against the fixed floor (`Baseline.sparse`).
NO_COMPARISON = "no_comparison"
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


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    below = sum(math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k))
    return max(0.0, 1.0 - below)


def poisson_tail(k: int, mean: float) -> float:
    """P(X >= k) for X ~ Poisson(mean)."""
    if k <= 0:
        return 1.0
    if mean <= 0.0:
        return 0.0
    term = below = math.exp(-mean)
    for i in range(1, k):
        term *= mean / i
        below += term
    return max(0.0, 1.0 - below)


@dataclass(frozen=True)
class Baseline:
    """How often one mechanical signal happens across the repository's files."""

    visits: int  # sessions x files touched, summed over files
    hit_visits: int  # ... of which those with at least one such event
    events: int
    files: int  # files with at least one such event
    above: tuple[str, ...]  # files clearly above the rate, most significant first
    #: Files judged against ``BASELINE_FLOOR`` alone: the other files had too few visits
    #: to give a rate, so "more than the rest of the repository" cannot be said of them.
    sparse: tuple[str, ...] = ()

    @property
    def rate(self) -> float:
        return round(self.hit_visits / self.visits, 3) if self.visits else 0.0


def baseline(counts: Mapping[str, tuple[int, int, int]]) -> Baseline:
    """Which files show clearly more of a mechanical signal than the rest.

    ``counts`` maps a file to ``(visits, visits with the signal, events)``; files
    without the signal belong in it too (they are most of the denominator).

    For each file with events, the rate of the *other* files is the null
    hypothesis (never less than ``BASELINE_FLOOR``; the floor alone when the
    others add up to fewer than ``BASELINE_MIN_VISITS`` visits). Two tails are
    computed under it: binomial, for this many visits with the signal among the
    file's visits, and Poisson, for this many events (a burst of forty misses in
    one session is one visit). The smaller, doubled for having had two tries, is
    the file's p-value. Files are then selected by the Benjamini-Hochberg
    procedure at ``BASELINE_ALPHA``, so that looking at many files does not by
    itself produce outliers.
    """
    visits = sum(v for v, _, _ in counts.values())
    hits = sum(h for _, h, _ in counts.values())
    events = sum(e for _, _, e in counts.values())
    tested: list[tuple[float, str]] = []
    sparse: list[str] = []
    for name, (own_visits, own_hits, own_events) in counts.items():
        if not own_events:
            continue
        others = visits - own_visits
        hit_rate = event_rate = BASELINE_FLOOR
        if others >= BASELINE_MIN_VISITS:
            hit_rate = max(BASELINE_FLOOR, (hits - own_hits) / others)
            event_rate = max(BASELINE_FLOOR, (events - own_events) / others)
        else:
            sparse.append(name)
        n = max(own_visits, own_hits)
        by_visits = binomial_tail(own_hits, n, min(hit_rate, 1.0))
        by_events = poisson_tail(own_events, n * event_rate)
        tested.append((min(1.0, 2.0 * min(by_visits, by_events)), name))
    tested.sort()
    passed = 0
    for rank, (p_value, _) in enumerate(tested, 1):
        if p_value <= BASELINE_ALPHA * rank / len(tested):
            passed = rank
    return Baseline(
        visits=visits,
        hit_visits=hits,
        events=events,
        files=len(tested),
        above=tuple(name for _, name in tested[:passed]),
        sparse=tuple(sorted(sparse)),
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
    "tool-failure": ("tool failure", "tool failures"),
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
    ordinary: int = 0,
    guardrail: int = 0,
    rewritten: int = 0,
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
    held = (
        f" {guardrail} of the events {'is a guardrail' if guardrail == 1 else 'are guardrails'}"
        f" doing {'its' if guardrail == 1 else 'their'} job (a hook or a deny rule refused "
        f"the call and the agent went another way)"
        if guardrail
        else ""
    )
    if capped and rewritten:
        text += (
            f" {'The event is' if rewritten == 1 else 'The events are'} a pre-commit hook "
            "rewriting files, after which the commit went through: routine each time, a "
            f"habit across sessions. The score is capped at {WEAK_ONLY_SCORE_CAP:g}."
        )
    elif capped and guardrail:
        text += (
            f"{held}; with nothing else here but weak signals the score is capped at "
            f"{WEAK_ONLY_SCORE_CAP:g}."
        )
    elif capped:
        text += (
            f" Only weak signals, which also accompany productive work: the score is capped "
            f"at {WEAK_ONLY_SCORE_CAP:g} and nothing is recommended."
        )

    elif ordinary:
        kind = "is an ordinary failure" if ordinary == 1 else "are ordinary failures"
        text += (
            f" {ordinary} of the events {kind}"
            f" (a red test, a probe that answers no): they do not count towards confidence, "
            f"and in sessions with nothing else they cannot lift the score past "
            f"{WEAK_ONLY_SCORE_CAP:g}."
        )
    if guardrail and not capped:
        text += f"{held}: that does not count towards confidence."
    return text


def is_ordinary_failure(finding: Finding) -> bool:
    """A command failure that is routine on its own (see `is_weak`)."""
    return finding.signal == COMMAND_FAILURE and is_weak(finding)


def is_guardrail_hit(finding: Finding) -> bool:
    """A call that a hook or a deny rule refused, after which the agent went another way."""
    return finding.signal == PERMISSION_DENIED and finding.detail.get("guardrail") == "1"


def is_hook_habit(finding: Finding) -> bool:
    """A commit that a pre-commit hook stopped to rewrite files, session after session."""
    return finding.signal == COMMAND_FAILURE and finding.detail.get("hook_rewrote") == "recurring"


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
        if detail.get("push_rejected") == "recurring" or detail.get("no_upstream") == "recurring":
            return False  # pushing without fetching first (or without `-u`), every session
        # an ordinary failure, or a mistyped name that the agent then spelled correctly
        return detail.get("variant") == "ordinary" or "typo_of" in detail
    if finding.signal == NO_CONVERGENCE:
        return finding.detail.get("uncertain") == "1"  # its end was not seen
    if finding.signal in BASELINE_SIGNALS and finding.detail.get(WITHIN_BASELINE) == "1":
        return True  # no more of it than anywhere else in this repository
    if finding.signal == STALE_EDIT:
        # a new file's write refused by the harness's read-first rule
        return finding.detail.get("variant") == "unread-write"
    if is_guardrail_hit(finding):
        return True  # the setup meant this call to be refused, and it was
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
