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
    result = scoring.score_items(items)
    intensity = math.log2(1 + 3.0) + math.log2(1 + 1.0)
    assert result.intensity == pytest.approx(intensity, abs=1e-3)
    assert result.score == round(100 * (1 - math.exp(-intensity / scoring.SCORE_SCALE)), 1)
    assert [(s.session_id, s.weighted_events, s.points) for s in result.sessions] == [
        ("a", 3.0, 2.0),
        ("b", 1.0, 1.0),
    ]


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
        assert previous < current <= 100.0
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
    ],
)
def test_confidence(sessions: int, kinds: list[str], expected: str) -> None:
    assert scoring.confidence(sessions, kinds) == expected


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
    text = scoring.explain([(signals.EDIT_MISS, 5), (signals.USER_CORRECTION, 1)], 3, "high")
    assert text == (
        "6 friction events in 3 sessions (5 edit miss, 1 user correction); "
        "high confidence because it recurs across sessions with several kinds of evidence."
    )
    assert scoring.explain([(signals.EDIT_MISS, 1)], 1, "low").startswith(
        "1 friction event in 1 session (1 edit miss); low confidence"
    )
