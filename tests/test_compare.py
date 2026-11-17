from __future__ import annotations

from agent_friction.compare import compare, render_compare
from tests.sample_result import sample_result


def _statuses(before: dict, after: dict) -> dict[str, str]:  # type: ignore[type-arg]
    return {c.path: c.status for c in compare(before, after)}


def test_identical_is_unchanged() -> None:
    s = _statuses(sample_result(), sample_result())
    assert set(s.values()) == {"unchanged"}


def test_improved_regressed_new_resolved() -> None:
    before, after = sample_result(), sample_result()
    after["hotspots"][0]["events"] = 2  # schema.py 14 -> 2
    after["hotspots"][1]["events"] = 20  # client.py 6 -> 20
    after["hotspots"].pop(2)  # README resolved
    after["commands"].append({**after["commands"][0], "path": "npm run lint", "events": 3})
    s = _statuses(before, after)
    assert s["src/shop/generated/schema.py"] == "improved"
    assert s["src/shop/api/client.py"] == "regressed"
    assert s["README.md"] == "unchanged"  # 1 event -> 0 is below min_events
    assert s["npm run lint"] == "new"
    assert s["pytest"] == "unchanged"


def test_rates_normalize_for_session_count() -> None:
    before, after = sample_result(), sample_result()
    # twice the sessions and twice the events: same rate, so unchanged
    after["totals"]["sessions"] = 12
    for h in after["hotspots"] + after["commands"]:
        h["events"] *= 2
    assert set(_statuses(before, after).values()) == {"unchanged"}


def test_small_absolute_changes_are_noise() -> None:
    before, after = sample_result(), sample_result()
    before["totals"]["sessions"] = after["totals"]["sessions"] = 1000
    # README 1 event -> 1 event at different session counts must not flip
    after["totals"]["sessions"] = 600
    s = _statuses(before, after)
    assert s["README.md"] == "unchanged"


def test_render_compare_text_and_markdown() -> None:
    before, after = sample_result(), sample_result()
    after["hotspots"][0]["events"] = 1
    changes = compare(before, after)
    text = render_compare(before, after, changes)
    assert "Improved (1 target)" in text and "schema.py" in text
    md = render_compare(before, after, changes, markdown=True)
    assert md.startswith("# Friction comparison") and "## Improved (1 target)" in md
    same = render_compare(before, before, compare(before, before))
    assert "No meaningful change." in same


def test_resolved_needs_enough_events() -> None:
    before, after = sample_result(), sample_result()
    after["hotspots"].pop(1)  # client.py had 6 events
    assert _statuses(before, after)["src/shop/api/client.py"] == "resolved"
    assert {c.path: c.status for c in compare(before, after, min_events=10)}[
        "src/shop/api/client.py"
    ] == "unchanged"


def test_exposure_separates_resolved_from_inactive() -> None:
    before, after = sample_result(), sample_result()
    after["hotspots"] = [h for h in after["hotspots"] if "client.py" not in h["path"]]
    after["hotspots"] = [h for h in after["hotspots"] if "schema.py" not in h["path"]]
    after["exposure"] = {"file": {"src/shop/api/client.py": 5}, "command": {}, "dir": {}}
    s = _statuses(before, after)
    assert s["src/shop/api/client.py"] == "resolved"  # touched 5 times, no friction
    assert s["src/shop/generated/schema.py"] == "inactive"  # never touched afterwards


def test_rates_use_exposure_not_window_size() -> None:
    before, after = sample_result(), sample_result()
    # Same events, but the file was touched in 10x more sessions afterwards.
    before["hotspots"][0]["exposure_sessions"] = 5
    after["hotspots"][0]["exposure_sessions"] = 50
    after["totals"]["sessions"] = 6
    changes = {c.path: c for c in compare(before, after)}
    c = changes["src/shop/generated/schema.py"]
    assert c.status == "improved"
    assert c.before_rate == 28.0 and c.after_rate == 2.8
    assert c.to_dict()["after_exposure_sessions"] == 50


def test_targets_touched_too_rarely_are_too_little_data() -> None:
    before, after = sample_result(), sample_result()
    for side in (before, after):
        side["hotspots"][1]["exposure_sessions"] = side["hotspots"][1]["sessions"] = 1
    after["hotspots"][1]["events"] = 30
    assert _statuses(before, after)["src/shop/api/client.py"] == "regressed"
    changes = {c.path: c.status for c in compare(before, after, min_exposure=3)}
    assert changes["src/shop/api/client.py"] == "too-little-data"


def test_score_moving_against_the_verdict_is_explained() -> None:
    before, after = sample_result(), sample_result()
    after["hotspots"][1]["events"] = 20
    after["hotspots"][1]["score"] = 1.0
    text = render_compare(before, after, compare(before, after))
    assert "not the score" in text
    plain = render_compare(before, before, compare(before, before))
    assert "not the score" not in plain


def _replaced_pair(replacement_runs: int, replacement_events: int = 0) -> tuple[dict, dict]:  # type: ignore[type-arg]
    before, after = sample_result(), sample_result()
    cmd = {
        **before["commands"][0],
        "path": "npm test",
        "events": 8,
        "sessions": 4,
        "exposure_sessions": 4,
        "recommendations": [
            {"id": "use-the-working-command", "title": "Tell agents to use `npm run test:unit`"}
        ],
    }
    before["commands"] = [cmd]
    after["commands"] = []
    after["exposure"] = {"command": {"npm run test:unit": replacement_runs}}
    if replacement_events:
        after["commands"] = [
            {
                **cmd,
                "path": "npm run test:unit",
                "events": replacement_events,
                "sessions": 1,
                "exposure_sessions": replacement_runs,
                "recommendations": [],
            }
        ]
    return before, after


def test_a_command_replaced_by_the_recommended_one_counts_as_fixed() -> None:
    before, after = _replaced_pair(4)
    (change,) = [c for c in compare(before, after) if c.path == "npm test"]
    assert change.status == "replaced" and change.replaced_by == "npm run test:unit"
    assert change.to_dict()["replacement_sessions"] == 4
    text = render_compare(before, after, compare(before, after))
    assert "Replaced (agents ran the recommended command instead)" in text
    assert "`npm run test:unit` ran in 4 sessions with 0 friction events" in text


def test_a_replacement_that_fails_as_often_is_not_a_fix() -> None:
    before, after = _replaced_pair(4, replacement_events=8)
    assert _statuses(before, after)["npm test"] == "inactive"
    before, after = _replaced_pair(0)
    assert _statuses(before, after)["npm test"] == "inactive"
    text = render_compare(before, after, compare(before, after))
    assert "Not touched afterwards" in text and "steers agents away" in text


def test_structured_replacement_field_wins_over_the_title() -> None:
    before, after = _replaced_pair(3)
    before["commands"][0]["recommendations"][0]["replacement"] = "npm run test:unit"
    before["commands"][0]["recommendations"][0]["title"] = "something else"
    assert _statuses(before, after)["npm test"] == "replaced"


def test_a_guardrail_that_holds_counts_as_enforced() -> None:
    before, after = sample_result(), sample_result()
    pytest_after = next(c for c in after["commands"] if c["path"] == "pytest")
    pytest_after["guardrail_hits"] = pytest_after["events"]
    (change,) = [c for c in compare(before, after) if c.path == "pytest"]
    assert change.status == "enforced"
    assert change.to_dict()["guardrail_hits"] == pytest_after["events"]
    text = render_compare(before, after, compare(before, after))
    assert "Enforced (a guardrail now refuses it and agents go another way)" in text
    assert "by a hook or deny rule" in text and "0 other friction events" in text


def test_guardrail_hits_with_other_friction_left_are_not_enforced() -> None:
    before, after = sample_result(), sample_result()
    pytest_after = next(c for c in after["commands"] if c["path"] == "pytest")
    pytest_after["guardrail_hits"] = 1
    assert _statuses(before, after)["pytest"] != "enforced"
    # Guardrail refusals before and after, and nothing else: no change to report.
    before, after = sample_result(), sample_result()
    for result in (before, after):
        cmd = next(c for c in result["commands"] if c["path"] == "pytest")
        cmd["guardrail_hits"] = cmd["events"]
    assert _statuses(before, after)["pytest"] == "unchanged"
