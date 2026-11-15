"""Terminal renderer: a compact, skimmable friction map for a TTY or a CI log."""

from __future__ import annotations

import textwrap
from typing import Any

from agent_friction.report._common import (
    collect_recommendations,
    fmt_minutes,
    plural,
    signal_titles,
    top_signals,
)


class Style:
    """ANSI styling that collapses to plain text when color is off."""

    def __init__(self, color: bool) -> None:
        self.color = color

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)

    def heat(self, score: float, text: str) -> str:
        # Bands follow the reading guide: <20 incident, 20-45 pattern, >45 reliable struggle.
        if score >= 45:
            return self.red(text)
        if score >= 20:
            return self.yellow(text)
        return self.green(text)


def bar(score: float, width: int = 10) -> str:
    """A fixed-width bar for a 0-100 score, using eighth blocks for resolution."""
    score = max(0.0, min(100.0, score))
    eighths = round(score / 100 * width * 8)
    full, rest = divmod(eighths, 8)
    partial = " ▏▎▍▌▋▊▉"[rest] if rest else ""
    return ("█" * full + partial).ljust(width)


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else "…" + text[-(width - 1) :]


def _hotspot_rows(
    items: list[dict[str, Any]], st: Style, titles: dict[str, str], width: int
) -> list[str]:
    lines: list[str] = []
    path_w = max(24, min(48, width - 62))
    header = (
        f"  {'score':<16} {'path':<{path_w}} {'sess':>4} {'events':>6} {'lost':>7}  top signals"
    )
    lines.append(st.dim(header))
    for h in items:
        score = float(h.get("score", 0))
        sig = ", ".join(f"{sid}×{n}" for sid, n in top_signals(h, 2))
        conf = h.get("confidence", "low")
        conf_mark = {"high": "●", "medium": "◐", "low": "○"}.get(conf, "○")
        lines.append(
            "  "
            + st.heat(score, f"{bar(score)} {score:>5.1f}")
            + f" {_truncate(str(h.get('path', '')), path_w):<{path_w}}"
            + f" {h.get('sessions', 0):>4} {h.get('events', 0):>6}"
            + f" {fmt_minutes(float(h.get('est_minutes_lost', 0))):>7}  "
            + st.dim(f"{conf_mark} ")
            + sig
        )
    return lines


def render_terminal(
    result: dict[str, Any], *, color: bool = False, top: int = 10, width: int = 100
) -> str:
    """Render the full friction map summary."""
    st = Style(color)
    titles = signal_titles(result)
    totals = result.get("totals", {})
    repo = result.get("repo", {})
    window = result.get("window", {})
    out: list[str] = []

    out.append(st.bold(f"agent-friction · {repo.get('name', '?')}"))
    span = ""
    if window.get("start") or window.get("end"):
        span = f" · {str(window.get('start') or '…')[:10]} → {str(window.get('end') or '…')[:10]}"
    out.append(
        st.dim(
            f"{plural(int(totals.get('sessions', 0)), 'session')}"
            f" · {plural(int(totals.get('tool_calls', 0)), 'tool call')}"
            f" · {plural(int(totals.get('friction_events', 0)), 'friction event')}"
            f" · ~{fmt_minutes(float(totals.get('est_minutes_lost', 0)))} lost"
            f" · {plural(int(totals.get('corrections', 0)), 'human correction')}{span}"
        )
    )
    out.append("")

    hotspots = list(result.get("hotspots", []))[:top]
    commands = list(result.get("commands", []))[:top]
    if not hotspots and not commands and not result.get("directories"):
        out.append(st.green("No recurring friction found. Agents seem to have a smooth ride here."))
        return "\n".join(out) + "\n"

    if hotspots:
        out.append(st.bold("Files where agents struggle"))
        out.extend(_hotspot_rows(hotspots, st, titles, width))
        out.append("")
    if commands:
        out.append(st.bold("Commands that keep failing"))
        out.extend(_hotspot_rows(commands, st, titles, width))
        out.append("")
    directories = list(result.get("directories", []))[:top]
    if directories:
        out.append(st.bold("Directories where agents search in vain"))
        out.extend(_hotspot_rows(directories, st, titles, width))
        out.append("")

    recs = collect_recommendations(result, limit=5)
    if recs:
        out.append(st.bold("What to fix first"))
        for i, (h, rec) in enumerate(recs, 1):
            out.append(f"  {i}. {st.cyan(str(h.get('path')))}: {rec.get('title', '')}")
            detail = str(rec.get("detail", "")).strip()
            if detail:
                for line in textwrap.wrap(detail, width=max(40, width - 7)):
                    out.append(st.dim(f"     {line}"))
        out.append("")

    out.append(
        st.dim(
            "● high ◐ medium ○ low confidence · "
            "drill in: agent-friction show <path> · full map: --format html -o map.html"
        )
    )
    out.append(
        st.dim(
            "Per-row time can overlap (one stall can involve a file and a command); "
            "the total counts each minute once."
        )
    )
    skipped = sum(int(s.get("skipped_lines", 0)) for s in result.get("sources", []))
    if skipped:
        out.append(
            st.yellow(
                f"{plural(skipped, 'transcript line')} could not be parsed and "
                "were skipped (see sources in --format json)."
            )
        )
    return "\n".join(out) + "\n"


def render_detail(
    result: dict[str, Any], item: dict[str, Any], *, color: bool = False, width: int = 100
) -> str:
    """Render one hotspot (file or command) with its full evidence trail."""
    st = Style(color)
    titles = signal_titles(result)
    score = float(item.get("score", 0))
    out: list[str] = []
    kind = {"command": "command", "dir": "directory"}.get(str(item.get("kind")), "file")
    out.append(st.bold(f"{kind}: {item.get('path')}"))
    out.append(
        st.heat(score, f"{bar(score)} {score:.1f}/100")
        + st.dim(
            f"  · {plural(int(item.get('sessions', 0)), 'session')}"
            f" · {plural(int(item.get('events', 0)), 'event')}"
            f" · ~{fmt_minutes(float(item.get('est_minutes_lost', 0)))} lost"
            f" · {item.get('confidence', 'low')} confidence"
        )
    )
    why = str(item.get("why", "")).strip()
    if why:
        out.extend(textwrap.wrap(why, width=width))
    out.append("")

    out.append(st.bold("Signals"))
    for sid, n in top_signals(item, limit=99):
        out.append(f"  {n:>4} × {titles.get(sid, sid)} {st.dim('(' + sid + ')')}")
    out.append("")

    recs = item.get("recommendations", [])
    if recs:
        out.append(st.bold("Recommendations"))
        for rec in recs:
            out.append(f"  • {rec.get('title', '')}")
            for line in textwrap.wrap(str(rec.get("detail", "")), width=max(40, width - 6)):
                out.append(st.dim(f"    {line}"))
        out.append("")

    evidence = item.get("evidence", [])
    if evidence:
        out.append(st.bold("Evidence"))
        for ev in evidence:
            ts = str(ev.get("timestamp", ""))[:16].replace("T", " ")
            sess = str(ev.get("session_id", ""))[:8]
            out.append(
                f"  {st.cyan(ts)} {st.dim('session ' + sess)}  "
                f"{titles.get(str(ev.get('signal')), ev.get('signal'))}: {ev.get('summary', '')}"
            )
            excerpt = ev.get("excerpt")
            if excerpt:
                for line in textwrap.wrap(str(excerpt), width=max(40, width - 8)):
                    out.append(st.dim(f"      │ {line}"))
            src = ev.get("source")
            if src:
                out.append(st.dim(f"      ↳ {src}:{ev.get('line', '?')}"))
        out.append("")
    return "\n".join(out)


def render_dir_detail(
    result: dict[str, Any], node: dict[str, Any], *, color: bool = False, top: int = 10
) -> str:
    """Render a directory: its rolled-up totals and its hottest descendants."""
    st = Style(color)
    score = float(node.get("score", 0))
    out = [
        st.bold(f"directory: {node.get('path') or '.'}/ (everything inside, rolled up)"),
        st.heat(score, f"{bar(score)} {score:.1f}/100")
        + st.dim(
            f"  · {plural(int(node.get('events', 0)), 'event')}"
            f" · ~{fmt_minutes(float(node.get('est_minutes_lost', 0)))} lost"
        ),
        "",
    ]
    prefix = (str(node.get("path", "")) + "/") if node.get("path") else ""
    inside = [h for h in result.get("hotspots", []) if str(h.get("path", "")).startswith(prefix)]
    if inside:
        out.append(st.bold("Hottest files inside"))
        out.extend(_hotspot_rows(inside[:top], st, signal_titles(result), 100))
    return "\n".join(out) + "\n"
