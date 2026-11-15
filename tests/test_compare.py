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
    assert "Improved (1)" in text and "schema.py" in text
    md = render_compare(before, after, changes, markdown=True)
    assert md.startswith("# Friction comparison") and "## Improved (1)" in md
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
