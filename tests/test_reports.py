from __future__ import annotations

import json
import re

from agent_friction.report import render_html, render_json, render_markdown, render_terminal
from agent_friction.report._common import (
    collect_recommendations,
    find_dir,
    find_target,
    fmt_minutes,
)
from agent_friction.report.terminal import bar, render_detail, render_dir_detail
from tests.sample_result import sample_result


def test_fmt_minutes() -> None:
    assert fmt_minutes(0) == "0s"
    assert fmt_minutes(0.5) == "30s"
    assert fmt_minutes(7.4) == "7m"
    assert fmt_minutes(125) == "2h 05m"
    assert fmt_minutes(2.5) == "3m"  # half up, like the HTML report
    assert fmt_minutes(0.999) == "1m"


def test_bar_bounds() -> None:
    assert bar(0) == " " * 10
    assert bar(100) == "█" * 10
    assert bar(150) == "█" * 10
    assert len(bar(37.5)) == 10


def test_terminal_plain_has_no_ansi() -> None:
    text = render_terminal(sample_result(), color=False)
    assert "\033[" not in text
    assert "src/shop/generated/schema.py" in text
    assert "Commands that keep failing" in text
    assert "What to fix first" in text
    assert "6 sessions" in text


def test_terminal_color() -> None:
    assert "\033[" in render_terminal(sample_result(), color=True)


def test_terminal_respects_top() -> None:
    text = render_terminal(sample_result(), top=1)
    assert "schema.py" in text
    assert "client.py" not in text.split("What to fix first")[0]


def test_terminal_empty() -> None:
    r = sample_result()
    r["hotspots"], r["commands"] = [], []
    r["totals"]["friction_events"] = 0
    assert "No recurring friction" in render_terminal(r)


def test_detail_and_dir_detail() -> None:
    r = sample_result()
    item = find_target(r, "src/shop/generated/schema.py")
    assert item is not None
    text = render_detail(r, item)
    assert "Edit missed" in text and "old_string not found" in text
    assert "a.jsonl:42" in text
    node = find_dir(r, "src/shop")
    assert node is not None
    assert "schema.py" in render_dir_detail(r, node)


def test_find_target_suffix_and_normalization() -> None:
    r = sample_result()
    assert find_target(r, "./src/shop/api/client.py") is not None
    assert find_target(r, "api/client.py") is not None
    assert find_target(r, "pytest") is not None
    assert find_target(r, "nope.py") is None
    assert find_dir(r, "src/shop/") is not None
    assert find_dir(r, "README.md") is None  # a leaf, not a directory


def test_recommendations_ranked_by_score() -> None:
    recs = collect_recommendations(sample_result())
    assert [h["path"] for h, _ in recs] == [
        "src/shop/generated/schema.py",
        "pytest",
        "src/shop/api/client.py",
    ]


def test_markdown_escapes_tables_and_html() -> None:
    md = render_markdown(sample_result())
    assert md.startswith("# Agent friction map: shop")
    assert "| 1 | `src/shop/generated/schema.py` | 82.4 |" in md
    assert "<script>" not in md.split("## Evidence")[0]
    assert "## Signals" in md
    # zero-count signals are not listed
    assert "search-miss" not in md


def test_json_roundtrip_and_deterministic() -> None:
    r = sample_result()
    a, b = render_json(r), render_json(sample_result())
    assert a == b
    assert json.loads(a) == r


def test_html_is_self_contained_and_safe() -> None:
    html = render_html(sample_result())
    assert html.startswith("<!doctype html>")
    # no network fetches
    assert not re.search(r"""(src|href)=["']https?://""", html)
    # the untrusted excerpt cannot close the data script early
    data = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert "<" not in data and ">" not in data
    assert "innerHTML" not in html
    embedded = json.loads(data)
    assert embedded["hotspots"][0]["evidence"][0]["excerpt"].startswith("<script>")


def test_markdown_escapes_html_in_free_text() -> None:
    r = sample_result()
    r["hotspots"][1]["recommendations"][0]["detail"] = 'The human said: "<img src=x onerror=1>"'
    md = render_markdown(r)
    assert "<img" not in md and "&lt;img" in md


def test_directory_only_results_are_not_reported_as_smooth() -> None:
    r = sample_result()
    directory = {**r["hotspots"][0], "path": "src/shop", "kind": "dir"}
    r["hotspots"], r["commands"], r["directories"] = [], [], [directory]
    assert "No recurring friction" not in render_terminal(r)
    assert "No recurring friction" not in render_markdown(r)
    assert "search in vain" in render_terminal(r)


def test_skipped_lines_are_mentioned() -> None:
    r = sample_result()
    r["sources"][0]["skipped_lines"] = 3
    assert "3 transcript lines could not be parsed" in render_terminal(r)


def test_markdown_code_spans_survive_backticks() -> None:
    from agent_friction.report.markdown import _code, _html_escape

    assert _code("a`b") == "``a`b``"
    assert _code("``x") == "``` ``x ```"
    assert _html_escape("a <b> `<c>` ``d`<e>`` <f>") == "a &lt;b&gt; `<c>` ``d`<e>`` &lt;f&gt;"
    assert _html_escape("odd ` <x>") == "odd ` &lt;x&gt;"


def test_header_without_sessions_says_whether_git_was_read() -> None:
    from agent_friction.report.terminal import render_terminal

    empty = {"totals": {"sessions": 0}, "repo": {"name": "x"}, "hotspots": [], "commands": []}
    with_git = render_terminal({**empty, "settings": {"git": True}}, color=False)
    without = render_terminal({**empty, "settings": {"git": False}}, color=False)
    assert "git history only" in with_git and "git history only" not in without


def test_unattributed_events_are_listed_in_every_report() -> None:
    from agent_friction.report.html import render_html
    from agent_friction.report.markdown import render_markdown
    from agent_friction.report.terminal import render_terminal

    ev = {
        "signal": "user-interrupt",
        "summary": "the human interrupted `pytest`",
        "session_id": "abcdef1234",
        "source": "claude-code:shop/s1.jsonl",
        "line": 12,
        "timestamp": "2026-11-05T16:00:00Z",
        "excerpt": None,
        "est_seconds_lost": 50.0,
    }
    result = {
        "totals": {"sessions": 3, "friction_events": 3, "unattributed_events": 3},
        "repo": {"name": "shop"},
        "hotspots": [],
        "commands": [],
        "unattributed": [ev],
    }
    text = render_terminal(result, color=False)
    assert "No recurring friction" not in text
    assert "Not tied to a file or command (3 events, 1 shown)" in text
    assert "s1.jsonl:12" in text
    md = render_markdown(result)
    assert "## Not tied to a file or command" in md and "s1.jsonl:12" in md
    assert '"unattributed"' in render_html(result)
    quiet = {**result, "totals": {"sessions": 3}, "unattributed": []}
    assert "Not tied" not in render_terminal(quiet, color=False)


def test_markdown_quotes_cannot_render_images_or_mention_people() -> None:
    from agent_friction.report.markdown import _html_escape

    out = _html_escape("![x](http://e/i.png) cc @octocat `@kept`")
    assert "![" not in out and "@octocat" not in out and "`@kept`" in out


def test_rows_without_a_time_estimate_say_so() -> None:
    from agent_friction.report._common import fmt_lost

    assert fmt_lost(0) == "n/a" and fmt_lost(2) == "2m"
    r = sample_result()
    r["hotspots"][0]["est_minutes_lost"] = 0
    item = find_target(r, "src/shop/generated/schema.py")
    assert item is not None
    assert "no time estimate" in render_detail(r, item)
    assert "n/a" in render_terminal(r) and "| n/a |" in render_markdown(r)


def test_trailing_red_runs_are_marked_unresolved() -> None:
    r = sample_result()
    r["hotspots"][0]["evidence"][0]["trailing"] = True
    item = find_target(r, "src/shop/generated/schema.py")
    assert item is not None
    assert "unresolved at end of session" in render_detail(r, item)
    assert "unresolved at end of session" in render_markdown(r)
    assert "e.trailing" in render_html(r)


def test_git_only_empty_state_and_utc_labels() -> None:
    empty = {"totals": {"sessions": 0}, "repo": {"name": "x"}, "hotspots": [], "commands": []}
    text = render_terminal({**empty, "settings": {"git": True}})
    assert "found in the git history" in text and "tool calls" not in text.split("\n", 2)[2]
    r = sample_result()
    item = find_target(r, "src/shop/generated/schema.py")
    assert item is not None
    assert " UTC" in render_detail(r, item) and " UTC" in render_markdown(r)


def test_headline_counts_weak_events_apart() -> None:
    from agent_friction.report._common import events_phrase

    assert (
        events_phrase({"friction_events": 34, "strong_events": 23})
        == "34 friction events (11 weak)"
    )
    assert events_phrase({"friction_events": 3, "strong_events": 3}) == "3 friction events"
    assert events_phrase({"friction_events": 1}) == "1 friction event"


def test_repository_wide_advice_is_shown_once() -> None:
    r = sample_result()
    r["recommendations"] = [
        {
            "id": "note-the-background-rate",
            "title": "Edit misses run at 8% of file visits across the repository",
            "detail": "No file is clearly above that rate.",
        }
    ]
    assert "Across the repository" in render_terminal(r)
    assert "## Across the repository" in render_markdown(r)
    assert "R.recommendations" in render_html(r)
    r["hotspots"], r["commands"] = [], []
    assert "Edit misses run at 8%" in render_terminal(r)


def test_not_counted_note_reports_calls_outside_the_repository() -> None:
    from agent_friction.report._common import dropped_note

    assert dropped_note({"totals": {}}) is None
    only = dropped_note({"totals": {"events_outside_repo": 1}})
    assert only == (
        "Not counted: 1 declined, denied, or failed call that acted outside this repository."
    )
    both = dropped_note(
        {"totals": {"dropped_red_runs": 2, "dropped_minutes": 1.0, "events_outside_repo": 3}}
    )
    assert both is not None and both.startswith("Not counted: 2 failed runs")
    assert "Also not counted: 3 declined, denied, or failed calls" in both
    result = sample_result()
    result["totals"]["events_outside_repo"] = 4
    for text in (render_terminal(result), render_markdown(result)):
        assert "4 declined, denied, or failed calls that acted outside" in text
    assert "events_outside_repo" in render_html(result)


def test_headline_says_no_time_estimate_when_nothing_was_timed() -> None:
    result = sample_result()
    result["totals"]["est_minutes_lost"] = 0
    assert "no time estimate" in render_terminal(result)
    assert "no time estimate" in render_markdown(result)
    assert "~0s lost" not in render_terminal(result) + render_markdown(result)


def test_long_paths_are_elided_in_the_middle() -> None:
    from agent_friction.report.terminal import _truncate

    a = _truncate("src/pkg/very_long_module_name_for_layout_testing_1.py", 30)
    b = _truncate("lib/pkg/very_long_module_name_for_layout_testing_2.py", 30)
    assert len(a) == 30 and "…" in a and a.endswith("_1.py") and a.startswith("src/")
    assert a != b
    assert _truncate("short.py", 30) == "short.py"


def test_not_counted_note_reports_untimed_sessions() -> None:
    from agent_friction.report._common import dropped_note

    note = dropped_note({"totals": {"untimed_sessions_excluded": 2}})
    assert note == (
        "Not counted: 2 sessions with no timestamps, which cannot be placed in this time window."
    )


def test_find_target_matches_a_command_run_in_one_directory() -> None:
    from agent_friction.report._common import command_candidates, find_target

    row = {"kind": "command", "path": "npm test (packages/web/)", "score": 60.9}
    other = {"kind": "command", "path": "npm test (packages/api/)", "score": 20.0}
    one = {"hotspots": [], "commands": [row], "directories": []}
    assert find_target(one, "npm test") is row
    both = {"hotspots": [], "commands": [row, other], "directories": []}
    assert find_target(both, "npm test") is None
    assert command_candidates(both, "npm test") == [row, other]
    assert find_target(both, "npm test (packages/api/)") is other
    assert command_candidates(both, "pytest") == []


def test_files_no_longer_in_the_tree_are_marked() -> None:
    from agent_friction.report._common import gone_note

    result = sample_result()
    h = result["hotspots"][0]
    assert gone_note(h) == ""
    h["file"] = {**h.get("file", {}), "exists": False, "renamed_to": "src/new.py"}
    assert gone_note(h) == " (renamed)"
    assert " (renamed)" in render_markdown(result)
    assert " (renamed)" in render_detail(result, h)
    h["file"]["renamed_to"] = None
    assert gone_note(h) == " (not in tree)"
    assert gone_note({"kind": "command", "path": "pytest"}) == ""


def test_identical_unattributed_lines_are_collapsed() -> None:
    result = sample_result()
    ev = {
        "signal": "tool-failure",
        "summary": "WebFetch failed",
        "timestamp": "2026-11-01T10:00:00Z",
    }
    result["unattributed"] = [dict(ev) for _ in range(5)]
    result["totals"]["unattributed_events"] = 5
    term = render_terminal(result)
    assert term.count("WebFetch failed") == 1 and "(×5)" in term
    md = render_markdown(result)
    assert md.count("WebFetch failed") == 1 and "(×5)" in md


def test_terminal_table_fits_a_narrow_terminal() -> None:
    result = sample_result()
    wide = render_terminal(result, width=100)
    narrow = render_terminal(result, width=60)
    assert "█" in wide
    rows = [line for line in narrow.splitlines() if re.match(r"\s+\d+\.\d ", line)]
    assert rows
    assert all(len(line) <= 60 for line in rows)
    assert "█" not in narrow


def test_not_counted_note_separates_calls_elsewhere_in_the_repository() -> None:
    from agent_friction.report._common import dropped_note

    package = dropped_note({"totals": {"events_outside_repo": 3, "events_elsewhere_in_repo": 3}})
    assert package == (
        "Not counted: 3 declined, denied, or failed calls that acted elsewhere in the "
        "repository, outside the scanned directory."
    )
    mixed = dropped_note({"totals": {"events_outside_repo": 3, "events_elsewhere_in_repo": 1}})
    assert mixed is not None
    assert "1 declined, denied, or failed call that acted elsewhere in the repository" in mixed
    assert "and 2 calls that acted outside this repository." in mixed
    assert "events_elsewhere_in_repo" in render_html(sample_result())
