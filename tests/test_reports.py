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
    assert fmt_minutes(0) == "0m"
    assert fmt_minutes(0.5) == "30s"
    assert fmt_minutes(7.4) == "7m"
    assert fmt_minutes(125) == "2h 05m"


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
