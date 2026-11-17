"""Keep the README honest: its sample output and intra-repo links must match reality."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
USAGE = (ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
BLOB = "https://github.com/nurettincoban/agent-friction/blob/main/"


def _sample_block() -> list[str]:
    block = USAGE.split("```console", 1)[1].split("```", 1)[0]
    return [line for line in block.splitlines() if line.strip() and "..." not in line]


def test_usage_sample_matches_the_generated_example() -> None:
    example = (ROOT / "examples" / "shopfront.txt").read_text(encoding="utf-8")
    normalized = {re.sub(r"\s+", " ", line).strip() for line in example.splitlines()}
    for line in _sample_block():
        if line.startswith("$ "):
            continue
        squashed = re.sub(r"\s+", " ", line).strip()
        assert squashed in normalized or any(squashed in full for full in normalized), (
            f"docs/usage.md sample line not in examples/shopfront.txt: {line!r}. "
            "Re-paste it from the regenerated example."
        )


def _anchors(markdown: str) -> set[str]:
    out = set()
    for heading in re.findall(r"^#+\s+(.+)$", markdown, flags=re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        out.add(slug)
    return out


def test_intra_repo_links_resolve() -> None:
    docs = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md"))]
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            absolute = target.startswith(BLOB) or target.startswith(
                BLOB.replace("/blob/", "/tree/")
            )
            if absolute:
                target = target.split("/main/", 1)[1]
            elif re.match(r"^[a-z]+:", target) or target.startswith("#"):
                continue
            path, _, anchor = target.partition("#")
            resolved = (
                (ROOT / path)
                if absolute or doc.name == "README.md" or "/" in path
                else doc.parent / path
            )
            if not resolved.exists():
                resolved = doc.parent / path
            assert resolved.exists(), f"{doc.name}: broken link {target}"
            if anchor and resolved.suffix == ".md":
                anchors = _anchors(resolved.read_text(encoding="utf-8"))
                assert anchor in anchors, f"{doc.name}: missing anchor #{anchor} in {path}"


def test_site_demo_is_the_generated_example() -> None:
    demo = (ROOT / "docs" / "demo" / "index.html").read_text(encoding="utf-8")
    example = (ROOT / "examples" / "shopfront.html").read_text(encoding="utf-8")
    assert demo == example, "docs/demo/index.html is stale: run `make examples`"


def test_site_sample_output_matches_the_generated_example() -> None:
    site = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    block = re.split(r"<pre[^>]*>", site, maxsplit=1)[1].split("</pre>", 1)[0]
    text = re.sub(r"<[^>]+>", "", block).replace("&amp;", "&")
    example = (ROOT / "examples" / "shopfront.txt").read_text(encoding="utf-8")
    squashed_example = re.sub(r"\s+", " ", example)
    for line in text.splitlines():
        squashed = re.sub(r"\s+", " ", line).strip()
        if not squashed or squashed.startswith("$ "):
            continue
        assert squashed in squashed_example, f"site sample line not in the example: {line!r}"
