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
