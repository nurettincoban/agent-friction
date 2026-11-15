"""Small formatting helpers shared by the renderers."""

from __future__ import annotations

from typing import Any


def fmt_minutes(minutes: float) -> str:
    """Render a duration in minutes compactly: ``42s``, ``7m``, ``2h 05m``."""
    if minutes <= 0:
        return "0m"
    if minutes < 1:
        return f"{round(minutes * 60)}s"
    if minutes < 60:
        return f"{round(minutes)}m"
    hours, mins = divmod(round(minutes), 60)
    return f"{hours}h {mins:02d}m"


def signal_titles(result: dict[str, Any]) -> dict[str, str]:
    """Map signal id -> human title using the catalog embedded in the result."""
    return {s["id"]: s.get("title", s["id"]) for s in result.get("signals", [])}


def top_signals(hotspot: dict[str, Any], limit: int = 3) -> list[tuple[str, int]]:
    """Return the hotspot's most frequent signals, most frequent first, ties by id."""
    items = sorted(hotspot.get("signals", {}).items(), key=lambda kv: (-kv[1], kv[0]))
    return items[:limit]


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def collect_recommendations(
    result: dict[str, Any], limit: int = 5
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Highest-impact recommendations first: (hotspot, recommendation) pairs.

    Items backed by medium or high confidence come first, then by score; each
    contributes its first (most relevant) recommendation.
    """
    seen: set[str] = set()
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    ranked = sorted(
        all_items(result),
        key=lambda h: (
            h.get("confidence") == "low",
            -float(h.get("score", 0)),
            h.get("kind", ""),
            h.get("path", ""),
        ),
    )
    for hotspot in ranked:
        for rec in hotspot.get("recommendations", [])[:1]:
            key = f"{hotspot.get('path')}::{rec.get('id')}"
            if key in seen:
                continue
            seen.add(key)
            out.append((hotspot, rec))
        if len(out) >= limit:
            break
    return out


def all_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ranked item: file hotspots, commands, and directory-scoped hotspots."""
    return [
        *result.get("hotspots", []),
        *result.get("commands", []),
        *result.get("directories", []),
    ]


def find_target(result: dict[str, Any], target: str) -> dict[str, Any] | None:
    """Find a hotspot or command by exact path/command, then by unique suffix match."""
    pool = all_items(result)
    norm = target.strip().rstrip("/").replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    for h in pool:
        if h.get("path") == norm:
            return h
    suffix = [h for h in pool if str(h.get("path", "")).endswith("/" + norm)]
    if len(suffix) == 1:
        return suffix[0]
    # Last resort: the user typed a raw command line ("PYTEST -x", "npm test -- foo").
    from agent_friction.attribution import normalize_command

    key = normalize_command(target.strip().lower()) if target.strip() else ""
    for h in pool:
        if h.get("kind") == "command" and h.get("path") == key:
            return h
    return None


def find_dir(result: dict[str, Any], target: str) -> dict[str, Any] | None:
    """Find a directory node in the result tree by repo-relative path."""
    norm = target.strip().strip("/").replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]

    def walk(node: dict[str, Any]) -> dict[str, Any] | None:
        if node.get("path", "") == norm and node.get("children"):
            return node
        child: dict[str, Any]
        for child in node.get("children", []):
            found = walk(child)
            if found is not None:
                return found
        return None

    tree: dict[str, Any] | None = result.get("tree")
    return walk(tree) if isinstance(tree, dict) else None
