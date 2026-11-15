"""Regenerate examples/ from the synthetic fixture story (run via `make examples`).

CI runs this and fails if examples/ changes, so the published example always
matches what the current code produces.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_friction.engine import AnalyzeOptions, analyze
from agent_friction.report import render_html, render_json, render_markdown, render_terminal

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
OUT = ROOT / "examples"


def walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every dict nested anywhere in a JSON-like value."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def main() -> None:
    result = analyze(
        AnalyzeOptions(
            repo=FIXTURES / "shopfront",
            transcripts=[FIXTURES / "transcripts"],
            repo_aliases=["/home/dev/shopfront"],
            include_git=False,
            now=datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc),
        )
    )
    # Paths depend on where the checkout lives; make them stable across machines.
    result["repo"]["root"] = "tests/fixtures/shopfront"
    marker = "tests/fixtures/"
    for item in walk(result):
        for key in ("source", "path"):
            value = item.get(key)
            if isinstance(value, str) and marker in value:
                item[key] = marker + value.split(marker, 1)[1]
    OUT.mkdir(exist_ok=True)
    (OUT / "shopfront.json").write_text(render_json(result), encoding="utf-8")
    (OUT / "shopfront.md").write_text(render_markdown(result), encoding="utf-8")
    (OUT / "shopfront.html").write_text(render_html(result), encoding="utf-8")
    (OUT / "shopfront.txt").write_text(render_terminal(result), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}/shopfront.{{json,md,html,txt}}")


if __name__ == "__main__":
    main()
