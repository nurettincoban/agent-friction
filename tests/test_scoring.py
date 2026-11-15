from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from agent_friction import scoring, signals
from agent_friction.model import Finding


def finding(
    signal: str = signals.EDIT_MISS,
    session: str = "s1",
    weight: float = 1.0,
    interval: tuple[float, float] | None = None,
    stream: str = "st",
) -> Finding:
    return Finding(
        signal=signal,
        session_id=session,
        source="x.jsonl",
        line=1,
        ts=datetime(2026, 11, 2, tzinfo=timezone.utc),
        summary="",
        weight=weight,
        interval=interval,
        stream=stream,
    )


def score(findings: list[Finding]) -> float:
    return scoring.score_items([(f, 1.0) for f in findings]).score


def test_score_matches_the_documented_formula() -> None:
    items = [
        (finding(signals.EDIT_MISS, "a"), 1.0),  # 2.0
        (finding(signals.USER_CORRECTION, "a", weight=0.5), 0.5),  # 4.0 * 0.5 * 0.5 = 1.0
        (finding(signals.SEARCH_MISS, "b"), 1.0),  # 1.0
    ]
    result = scoring.score_items(items, exposure_sessions=4)
    points = [math.log2(1 + 3.0), math.log2(1 + 1.0)]
    mean = sum(points) / 2
    severity = 1 - math.exp(-mean / scoring.SEVERITY_SCALE)
    recurrence = math.log2(1 + 2) / math.log2(1 + scoring.RECURRENCE_FULL_SESSIONS)
    consistency = scoring.CONSISTENCY_FLOOR + (1 - scoring.CONSISTENCY_FLOOR) * 2 / (
        4 + scoring.EXPOSURE_PRIOR_SESSIONS
    )
    assert result.intensity == pytest.approx(sum(points), abs=1e-3)
    assert result.mean_session_points == pytest.approx(mean, abs=1e-3)
    assert result.severity == pytest.approx(severity, abs=1e-3)
    assert result.recurrence == pytest.approx(recurrence, abs=1e-3)
    assert result.consistency == pytest.approx(consistency, abs=1e-3)
    assert result.score == round(100 * severity * recurrence * consistency, 1)
    assert (result.friction_sessions, result.exposure_sessions, result.friction_rate) == (
        2,
        4,
        0.5,
    )
    assert [(s.session_id, s.weighted_events, s.points) for s in result.sessions] == [
        ("a", 3.0, 2.0),
        ("b", 1.0, 1.0),
    ]


def test_exposure_defaults_to_and_is_never_below_the_friction_sessions() -> None:
    items = [(finding(session=f"s{i}"), 1.0) for i in range(3)]
    assert scoring.score_items(items).exposure_sessions == 3
    assert scoring.score_items(items, exposure_sessions=1).exposure_sessions == 3
    assert scoring.score_items(items).friction_rate == 1.0
    empty = scoring.score_items([], exposure_sessions=9)
    assert (empty.score, empty.exposure_sessions, empty.friction_rate) == (0.0, 9, 0.0)


def test_consistent_friction_beats_rare_friction_in_a_busy_place() -> None:
    """5 bad sessions out of 5 visits outrank 10 bad sessions out of 200 visits."""

    def ranked(friction: int, exposure: int) -> float:
        items = [(finding(session=f"s{i}"), 1.0) for i in range(friction)]
        return scoring.score_items(items, exposure_sessions=exposure).score

    assert ranked(5, 5) > ranked(10, 200)
    assert ranked(3, 3) > ranked(3, 30)
    # more exposure with the same friction never raises the score
    scores = [ranked(4, exposure) for exposure in (4, 8, 16, 64, 1000)]
    assert scores == sorted(scores, reverse=True)
    assert scores[-1] > 0


def test_scores_do_not_saturate_with_volume() -> None:
    """Thousands of sessions must still be distinguishable by how bad each one is."""

    def at_scale(per_session: int) -> float:
        items = [
            (finding(signals.READ_LOOP if per_session == 1 else signals.EDIT_MISS, f"s{i}"), 1.0)
            for i in range(2000)
            for _ in range(per_session)
        ]
        return scoring.score_items(items).score

    mild, bad = at_scale(1), at_scale(4)
    assert mild < 40 < bad < 100


def test_recurrence_beats_a_single_session_burst() -> None:
    burst = [finding(session="one") for _ in range(12)]
    recurring = [finding(session=f"s{i}") for i in range(4)]
    assert score(recurring) > score(burst)
    # even three times as many events in one session do not catch up
    assert score(recurring) > score(burst * 3)


def test_one_pathological_session_has_diminishing_returns() -> None:
    ten = score([finding() for _ in range(10)])
    thousand = score([finding() for _ in range(1000)])
    assert thousand < 3 * ten
    assert thousand < 100


def test_score_is_monotonic_and_bounded() -> None:
    assert score([]) == 0.0
    previous = 0.0
    for n in range(1, 30):
        current = score([finding(session=f"s{i}") for i in range(n)])
        assert previous <= current < 100.0
        assert current > previous or n > scoring.RECURRENCE_FULL_SESSIONS
        previous = current


def test_severity_and_weight_matter() -> None:
    assert score([finding(signals.USER_CORRECTION)]) > score([finding(signals.EDIT_MISS)])
    assert score([finding(signals.EDIT_MISS)]) > score([finding(signals.READ_LOOP)])
    assert score([finding(weight=1.0)]) > score([finding(weight=0.3)])


def test_score_ignores_input_order() -> None:
    items = [finding(session=f"s{i % 3}", signal=sig) for i, sig in enumerate(signals.BY_ID)]
    assert scoring.score_items([(f, 1.0) for f in items]) == scoring.score_items(
        [(f, 1.0) for f in reversed(items)]
    )


@pytest.mark.parametrize(
    ("sessions", "kinds", "expected"),
    [
        (1, [signals.EDIT_MISS, signals.USER_CORRECTION, signals.SEARCH_MISS], "low"),
        (2, [signals.EDIT_MISS], "medium"),
        (5, [signals.EDIT_MISS], "medium"),
        (3, [signals.EDIT_MISS, signals.USER_CORRECTION], "high"),
        (9, [signals.READ_LOOP, signals.EDIT_THRASH], "medium"),
        (3, [signals.READ_LOOP, signals.EDIT_MISS], "high"),
        # git evidence corroborates, it never raises confidence on its own
        (3, [signals.EDIT_MISS, signals.HUMAN_REWORK], "medium"),
        (9, [signals.HUMAN_REWORK], "low"),
    ],
)
def test_confidence(sessions: int, kinds: list[str], expected: str) -> None:
    assert scoring.confidence([f"s{i}" for i in range(sessions)], kinds) == expected


def test_agent_commits_are_not_sessions_for_confidence() -> None:
    kinds = [signals.EDIT_MISS, signals.USER_CORRECTION]
    assert scoring.confidence(["s1", "git:aaa", "git:bbb"], kinds) == "low"
    assert scoring.confidence(["s1", "s2", "git:aaa"], kinds) == "medium"


def test_lost_time_is_the_union_of_intervals_per_stream() -> None:
    found = [
        finding(interval=(0, 60)),
        finding(interval=(30, 90)),  # overlaps the first: union is 0..90
        finding(interval=(200, 230)),
        finding(interval=None),
        finding(interval=(0, 60), stream="other"),  # another stream is never merged
    ]
    assert scoring.lost_seconds(found) == 90 + 30 + 60
    assert scoring.lost_minutes(found) == 3.0
    assert scoring.lost_minutes([]) == 0.0


def test_explain_sentence() -> None:
    counts = [(signals.EDIT_MISS, 5), (signals.USER_CORRECTION, 1)]
    assert scoring.explain(counts, 3, 4, "high") == (
        "6 friction events in 3 of the 4 sessions that touched it "
        "(5 edit miss, 1 user correction); "
        "high confidence because it recurs across sessions with several kinds of evidence."
    )
    assert scoring.explain(counts, 3, 3, "medium").startswith(
        "6 friction events in all 3 sessions that touched it ("
    )
    assert scoring.explain([(signals.EDIT_MISS, 1)], 1, 1, "low").startswith(
        "1 friction event in the only session that touched it (1 edit miss); low confidence"
    )
