"""Small formatting helpers shared by the renderers."""

from __future__ import annotations

import math
import re
from typing import Any


def gone_note(h: dict[str, Any]) -> str:
    """ " (renamed)" / " (not in tree)" for a file hotspot that is no longer in the tree."""
    f = h.get("file")
    if h.get("kind") != "file" or not isinstance(f, dict) or f.get("exists") is not False:
        return ""
    return " (renamed)" if f.get("renamed_to") else " (not in tree)"


def repo_recommendations(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Advice about the repository as a whole (a background rate), not one target."""
    return [r for r in result.get("recommendations") or [] if isinstance(r, dict)]


def events_phrase(totals: dict[str, Any]) -> str:
    """ "34 friction events (11 weak)": weak signals alone are leads, not findings."""
    events = int(totals.get("friction_events", 0) or 0)
    text = plural(events, "friction event")
    strong = totals.get("strong_events")
    if isinstance(strong, int) and 0 <= strong < events:
        text += f" ({events - strong} weak)"
    return text


def fmt_lost(minutes: float) -> str:
    """A row's time lost; "n/a" when its evidence carries no time estimate at all."""
    return fmt_minutes(minutes) if minutes > 0 else "n/a"


def headline_lost(totals: dict[str, Any]) -> str:
    """The headline's time lost: "no time estimate" when no evidence carried any time."""
    minutes = float(totals.get("est_minutes_lost", 0) or 0)
    return f"~{fmt_minutes(minutes)} lost" if minutes > 0 else "no time estimate"


def fmt_minutes(minutes: float) -> str:
    """Render a duration in minutes compactly: ``42s``, ``7m``, ``2h 05m``."""
    # Round half up (not Python's half-to-even) so this matches the HTML report.
    if minutes <= 0:
        return "0s"
    secs = math.floor(minutes * 60 + 0.5)
    if secs < 60:
        return f"{secs}s"
    whole = math.floor(minutes + 0.5)
    if whole < 60:
        return f"{whole}m"
    hours, mins = divmod(whole, 60)
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
    from agent_friction.attribution import normalize_command, public_key

    raw = target.strip()
    # Try the line as typed and lower-cased ("PYTEST -x"); a --no-excerpts scan stores
    # pseudonymized keys, so also try those and let the owner type the real name.
    keys = {k for line in {raw, raw.lower()} if line for k in (normalize_command(line),) if k}
    keys |= {public_key(k) for k in keys}
    for h in pool:
        if h.get("kind") == "command" and h.get("path") in keys:
            return h
    # "npm test" for a command only run in a directory, keyed "npm test (packages/web/)".
    qualified = command_candidates(result, target)
    return qualified[0] if len(qualified) == 1 else None


_DIR_SUFFIX = re.compile(r" \([^()]*/\)$")


def command_candidates(result: dict[str, Any], target: str) -> list[dict[str, Any]]:
    """Command rows whose key, without its ``(dir/)`` suffix, is the command typed."""
    from agent_friction.attribution import normalize_command, public_key

    raw = target.strip()
    keys = {k for line in {raw, raw.lower()} if line for k in (normalize_command(line),) if k}
    keys |= {public_key(k) for k in keys}
    keys |= {raw}
    return [
        h
        for h in all_items(result)
        if h.get("kind") == "command"
        and _DIR_SUFFIX.search(str(h.get("path", "")))
        and _DIR_SUFFIX.sub("", str(h.get("path", ""))) in keys
    ]


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


def dropped_note(result: dict[str, Any]) -> str | None:
    """One line on red runs left out as normal work, so the headline floor is auditable."""
    totals = result.get("totals", {})
    runs = int(totals.get("dropped_red_runs", 0) or 0)
    outside = int(totals.get("events_outside_repo", 0) or 0)
    parts: list[str] = []
    if runs > 0:
        minutes = float(totals.get("dropped_minutes", 0) or 0)
        parts.append(
            f"Not counted: {plural(runs, 'failed run')} of normal work (the agent fixed the "
            "failure, or was only waiting and polled until it passed; "
            f"~{fmt_minutes(minutes)} of run time)."
        )
    if outside > 0:
        calls = plural(outside, "declined, denied, or failed call")
        parts.append(
            f"{'Also not' if parts else 'Not'} counted: {calls} that acted outside this repository."
        )
    untimed = int(totals.get("untimed_sessions_excluded", 0) or 0)
    if untimed > 0:
        parts.append(
            f"{'Also not' if parts else 'Not'} counted: {plural(untimed, 'session')} with no "
            "timestamps, which cannot be placed in this time window."
        )
    return " ".join(parts) or None


def unattributed_heading(result: dict[str, Any]) -> str | None:
    """Heading for friction events that no file, directory, or command could own."""
    count = int(result.get("totals", {}).get("unattributed_events", 0) or 0)
    items = result.get("unattributed") or []
    if count <= 0 and not items:
        return None
    count = max(count, len(items))
    shown = f", {len(items)} shown" if 0 < len(items) < count else ""
    return f"Not tied to a file or command ({plural(count, 'event')}{shown})"
