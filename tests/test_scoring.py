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
        # weak kinds never corroborate: one strong kind plus a weak one is still one kind
        (3, [signals.READ_LOOP, signals.EDIT_MISS], "medium"),
        (3, [signals.EDIT_THRASH, signals.NO_CONVERGENCE], "medium"),
        (3, [signals.EDIT_THRASH, signals.EDIT_MISS, signals.EDIT_REVERT], "high"),
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
        "(5 edit misses, 1 user correction); "
        "high confidence because it recurs across sessions with several kinds of evidence."
    )
    assert scoring.explain(counts, 3, 3, "medium").startswith(
        "6 friction events in all 3 sessions that touched it ("
    )
    assert scoring.explain([(signals.EDIT_MISS, 1)], 1, 1, "low").startswith(
        "1 friction event in the only session that touched it (1 edit miss); low confidence"
    )
    with_git = scoring.explain([(signals.EDIT_MISS, 1), (signals.HUMAN_REWORK, 3)], 1, 1, "low", 3)
    assert with_git.startswith(
        "4 friction events in the only session that touched it and 3 agent commits "
        "(1 edit miss, 3 human rework commits);"
    )
    only_git = scoring.explain([(signals.HUMAN_REWORK, 1)], 0, 0, "low", 1)
    assert only_git.startswith("1 friction event in 1 agent commit (1 human rework commit);")
    assert set(scoring.SIGNAL_NOUNS) == set(signals.BY_ID)


def test_a_derived_signal_is_not_a_second_kind_of_evidence() -> None:
    sessions = ["a", "b", "c", "d"]
    loop_and_failures = [signals.COMMAND_FAILURE, signals.RETRY_LOOP]
    assert scoring.confidence(sessions, loop_and_failures) == "medium"
    assert scoring.confidence(sessions, [*loop_and_failures, signals.TOOL_REJECTED]) == "high"
    # a retry loop next to unrelated evidence is still its own signal
    assert scoring.confidence(sessions, [signals.RETRY_LOOP, signals.USER_CORRECTION]) == "high"


def test_weak_only_evidence() -> None:
    def miss(session: str, pattern: str, **detail: str) -> Finding:
        found = finding(signals.SEARCH_MISS, session)
        found.detail.update(pattern=pattern, **detail)
        return found

    assert not scoring.weak_only([])
    assert scoring.weak_only([finding(signals.EDIT_THRASH), finding(signals.READ_LOOP)])
    assert scoring.weak_only([finding(signals.PROBABLE_CORRECTION)])
    assert not scoring.weak_only([finding(signals.READ_LOOP), finding(signals.EDIT_MISS)])
    assert scoring.weak_only([miss("s1", "feature_1"), miss("s2", "feature_2")])
    assert scoring.weak_only([miss("s1", "Policy"), miss("s1", "Policy")])
    # the same name missed in two sessions is no longer a one-off probe
    again = [miss("s1", "Policy", wanted="1"), miss("s2", "policy", wanted="1")]
    assert not scoring.weak_only(again)
    # ... if both went on looking for it, and it is not a search for leftovers
    assert scoring.weak_only([miss("s1", "Policy"), miss("s2", "policy", wanted="1")])
    assert scoring.weak_only(
        [miss("s1", "TODO", wanted="1", hygiene="1"), miss("s2", "TODO", wanted="1", hygiene="1")]
    )
    counts = [(signals.READ_LOOP, 5)]
    assert "capped at 19" in scoring.explain(counts, 5, 5, "medium", capped=True)
    assert "capped" not in scoring.explain(counts, 5, 5, "medium")


def test_ordinary_command_failures_are_weak_on_their_own() -> None:
    def failure(variant: str, session: str = "s1") -> Finding:
        found = finding(signals.COMMAND_FAILURE, session)
        found.detail["variant"] = variant
        return found

    assert scoring.is_weak(failure("ordinary")) and not scoring.is_weak(failure("invocation"))
    assert scoring.weak_only([failure("ordinary", f"s{i}") for i in range(10)])
    assert not scoring.weak_only([failure("ordinary"), failure("invocation")])
    assert not scoring.weak_only([failure("ordinary"), finding(signals.RETRY_LOOP)])


@pytest.mark.parametrize("definition", signals.CATALOG, ids=lambda d: d.id)
def test_every_signal_can_be_counted_in_words(definition: signals.SignalDef) -> None:
    one, several = scoring.SIGNAL_NOUNS[definition.id]
    assert scoring.count_signal(definition.id, 1) == f"1 {one}"
    assert scoring.count_signal(definition.id, 2) == f"2 {several}"
    # the noun of the phrase carries the plural, not its last word
    assert not several.endswith(("greens", "files"))
    why = scoring.explain([(definition.id, 2)], 2, 2, "medium")
    assert f"(2 {several})" in why


def test_set_of_labelled_signals_is_the_catalog() -> None:
    assert set(scoring.SIGNAL_NOUNS) == {s.id for s in signals.CATALOG}
    assert scoring.count_signal("no-convergence", 3) == (
        "3 edit-and-run stretches that never went green"
    )


# ---- round 25 ------------------------------------------------------------------------


def test_tails() -> None:
    assert scoring.binomial_tail(0, 5, 0.1) == 1.0 and scoring.binomial_tail(6, 5, 0.1) == 0.0
    assert scoring.binomial_tail(1, 1, 0.05) == pytest.approx(0.05)
    assert scoring.binomial_tail(2, 2, 0.05) == pytest.approx(0.0025)
    assert scoring.binomial_tail(3, 30, 0.05) == pytest.approx(0.188, abs=0.001)
    assert scoring.poisson_tail(0, 2.0) == 1.0 and scoring.poisson_tail(1, 0.0) == 0.0
    assert scoring.poisson_tail(1, 0.05) == pytest.approx(0.0488, abs=0.0001)
    assert scoring.poisson_tail(3, 1.5) == pytest.approx(0.191, abs=0.001)


def test_a_file_is_above_the_baseline_only_when_it_clearly_is() -> None:
    # (visits, visits with the signal, events)
    assert scoring.baseline({"a": (30, 3, 3)}).above == ()  # 3 of 30 is the assumed 5%
    assert scoring.baseline({"a": (4, 2, 2)}).above == ("a",)
    assert scoring.baseline({"a": (1, 1, 1)}).above == ()  # once, in the one visit
    assert scoring.baseline({"a": (1, 1, 40)}).above == ("a",)  # a burst is not background
    # the same 3 of 10 stands out in a quiet repository and not in a noisy one
    quiet = {f"f{i}": (20, 0, 0) for i in range(10)} | {"a": (10, 3, 3)}
    noisy = {f"f{i}": (20, 3, 3) for i in range(10)} | {"a": (10, 3, 3)}
    assert scoring.baseline(quiet).above == ("a",)
    base = scoring.baseline(noisy)
    assert base.above == () and (base.visits, base.hit_visits, base.events) == (210, 33, 33)
    assert base.files == 11 and base.rate == 0.157
    # ... and a file far above a noisy repository's rate still does
    noisy["a"] = (10, 9, 14)
    assert scoring.baseline(noisy).above == ("a",)
    # many files tested: one modest p-value among them is what chance produces
    crowd = {f"f{i}": (25, 2, 2) for i in range(30)} | {"a": (8, 3, 3)}
    assert scoring.baseline(crowd).above == ()
    assert scoring.baseline({}).above == () and scoring.baseline({}).rate == 0.0


def test_a_finding_within_the_baseline_is_weak() -> None:
    miss = finding(signals.EDIT_MISS)
    assert not scoring.is_weak(miss)
    miss.detail[scoring.WITHIN_BASELINE] = "1"
    assert scoring.is_weak(miss) and scoring.weak_only([miss])
    correction = finding(signals.USER_CORRECTION)
    correction.detail[scoring.WITHIN_BASELINE] = "1"  # not a mechanical signal: no effect
    assert not scoring.is_weak(correction)


def test_baseline_says_which_files_had_nothing_to_be_compared_with() -> None:
    # one file, every visit a miss: judged against the floor alone
    alone = scoring.baseline({"only.py": (3, 3, 6)})
    assert alone.above == ("only.py",) and alone.sparse == ("only.py",)
    # the same file next to 30 quiet visits elsewhere has a rest to stand out from
    busy = scoring.baseline({"only.py": (3, 3, 6), **{f"f{i}.py": (3, 0, 0) for i in range(10)}})
    assert busy.above == ("only.py",) and busy.sparse == ()


def test_ordinary_failures_are_named_in_the_explanation() -> None:
    counts = [(signals.COMMAND_FAILURE, 5)]
    text = scoring.explain(counts, 4, 4, "low", ordinary=4)
    assert text.endswith(
        "4 of the events are ordinary failures (a red test, a probe that answers no): they do "
        "not count towards confidence, and in sessions with nothing else they cannot lift the "
        "score past 19."
    )
    assert "1 of the events is an ordinary failure" in scoring.explain(
        counts, 4, 4, "low", 0, False, 0, 1
    )
    assert "ordinary" not in scoring.explain(counts, 4, 4, "low")
    # a capped row says the stronger thing instead
    assert "ordinary failures" not in scoring.explain(counts, 4, 4, "low", capped=True, ordinary=4)
    red = finding(signals.COMMAND_FAILURE)
    red.detail["variant"] = "ordinary"
    assert scoring.is_ordinary_failure(red)
    red.detail["recurring_red"] = "1"
    assert not scoring.is_ordinary_failure(red)
    assert not scoring.is_ordinary_failure(finding(signals.EDIT_THRASH))
