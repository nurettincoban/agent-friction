"""Terminal renderer: a compact, skimmable friction map for a TTY or a CI log."""

from __future__ import annotations

import textwrap
from typing import Any

from agent_friction.report._common import (
    all_items,
    collect_recommendations,
    dropped_note,
    events_phrase,
    fmt_lost,
    fmt_minutes,
    headline_lost,
    plural,
    repo_recommendations,
    signal_titles,
    top_signals,
    unattributed_heading,
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


def _in_repo_calls(totals: dict[str, Any]) -> int:
    """Tool calls inside the repo; older results only have the all-calls count."""
    value = totals.get("tool_calls_in_repo", totals.get("tool_calls", 0))
    return int(value or 0)


def _utc_label(timestamp: object) -> str:
    """Evidence times are UTC; say so, since a reader assumes local time."""
    text = str(timestamp or "")[:16].replace("T", " ")
    return f"{text} UTC" if text else ""


def _lost_phrase(minutes: float) -> str:
    return f"~{fmt_minutes(minutes)} lost" if minutes > 0 else "no time estimate"


def bar(score: float, width: int = 10) -> str:
    """A fixed-width bar for a 0-100 score, using eighth blocks for resolution."""
    score = max(0.0, min(100.0, score))
    eighths = round(score / 100 * width * 8)
    full, rest = divmod(eighths, 8)
    partial = " ▏▎▍▌▋▊▉"[rest] if rest else ""
    return ("█" * full + partial).ljust(width)


def _truncate(text: str, width: int) -> str:
    """Elide the middle, so long sibling paths keep both their top directory and file name."""
    if len(text) <= width:
        return text
    head = (width - 1) // 3
    return text[:head] + "…" + text[-(width - 1 - head) :]


def _unattributed_rows(result: dict[str, Any], st: Style, width: int, limit: int = 5) -> list[str]:
    heading = unattributed_heading(result)
    if heading is None:
        return []
    lines = (
        ["", st.bold(heading)]
        if not result.get("hotspots") and not result.get("commands")
        else [st.bold(heading)]
    )
    items = list(result.get("unattributed") or [])
    for ev in items[:limit]:
        when = _utc_label(ev.get("timestamp"))
        text = f"{ev.get('signal', '?')} · {ev.get('summary', '')}"
        room = max(40, width - 20)
        text = text if len(text) <= room else text[: room - 1] + "…"
        lines.append(f"  {st.dim(when)} {text}")
        if ev.get("source"):
            lines.append(st.dim(f"      ↳ {ev['source']}:{ev.get('line', '?')}"))
    if len(items) > limit:
        lines.append(st.dim(f"  … {len(items) - limit} more in --format json (unattributed)"))
    lines.append("")
    return lines


def _repo_rows(result: dict[str, Any], st: Style, width: int) -> list[str]:
    items = repo_recommendations(result)
    if not items:
        return []
    lines = [st.bold("Across the repository")]
    for rec in items:
        lines.append(f"  • {rec.get('title', '')}")
        for line in textwrap.wrap(str(rec.get("detail", "")), width=max(40, width - 6)):
            lines.append(st.dim(f"    {line}"))
    lines.append("")
    return lines


def _hotspot_rows(
    items: list[dict[str, Any]],
    st: Style,
    titles: dict[str, str],
    width: int,
    label: str = "path",
) -> list[str]:
    lines: list[str] = []
    path_w = max(24, min(48, width - 62))
    header = f"  {'score':<16} {label:<{path_w}} {'sess':>4} {'events':>6} {'lost':>7}  top signals"
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
            + f" {fmt_lost(float(h.get('est_minutes_lost', 0))):>7}  "
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
    sessions = int(totals.get("sessions", 0))
    git_on = bool(result.get("settings", {}).get("git", True))
    if sessions:
        who = plural(sessions, "session")
    else:
        who = "git history only, no agent sessions" if git_on else "no agent sessions"
    out.append(
        st.dim(
            f"{who}"
            f" · {plural(_in_repo_calls(totals), 'tool call')}"
            f" · {events_phrase(totals)}"
            f" · {headline_lost(totals)}"
            f" · {plural(int(totals.get('corrections', 0)), 'human correction')}{span}"
        )
    )
    out.append("")

    hotspots = list(result.get("hotspots", []))[:top]
    commands = list(result.get("commands", []))[:top]
    if not hotspots and not commands and not result.get("directories"):
        calls = _in_repo_calls(totals)
        if int(totals.get("friction_events", 0)):
            out.append(st.green("No hotspot: no file, directory, or command collected friction."))
        elif not sessions:
            out.append(
                st.green("No recurring friction found in the git history.")
                if git_on
                else st.green(
                    "No recurring friction: no agent session touched this repository "
                    "in this window."
                )
            )
        else:
            out.append(
                st.green(f"No recurring friction found in {plural(calls, 'tool call')}.")
                + (st.dim(" That is a small sample; check again later.") if calls < 50 else "")
            )
        out.extend(_unattributed_rows(result, st, width))
        out.extend(_repo_rows(result, st, width))
        note = dropped_note(result)
        if note:
            out.append(st.dim(note))
        return "\n".join(out) + "\n"

    if hotspots:
        out.append(st.bold("Files where agents struggle"))
        out.extend(_hotspot_rows(hotspots, st, titles, width))
        out.append("")
    if commands:
        out.append(st.bold("Commands that keep failing"))
        out.extend(_hotspot_rows(commands, st, titles, width, "command"))
        out.append("")
    directories = list(result.get("directories", []))[:top]
    if directories:
        out.append(
            st.bold("Directories where agents search in vain")
            + st.dim(" (the directory itself; show <dir> rolls up everything inside)")
        )
        out.extend(_hotspot_rows(directories, st, titles, width, "directory"))
        out.append("")

    out.extend(_unattributed_rows(result, st, width))
    out.extend(_repo_rows(result, st, width))

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
            "n/a means the evidence carries no time estimate; "
            "the total counts each minute once."
        )
    )
    note = dropped_note(result)
    if note:
        out.append(st.dim(note))
    third = int(totals.get("third_party_events", 0) or 0)
    if third:
        out.append(
            st.dim(
                f"Also left out: {plural(third, 'event')} in dependencies or build output "
                "(node_modules, .venv, git-ignored directories)."
            )
        )
    if any(int(h.get("agent_commits", 0)) for h in all_items(result)):
        out.append(st.dim("For git evidence, sess counts the agent commits involved."))
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
            f" · {_lost_phrase(float(item.get('est_minutes_lost', 0)))}"
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
            ts = _utc_label(ev.get("timestamp"))
            sess = str(ev.get("session_id", ""))[:8]
            out.append(
                f"  {st.cyan(ts)} {st.dim('session ' + sess)}  "
                f"{titles.get(str(ev.get('signal')), ev.get('signal'))}: {ev.get('summary', '')}"
                + (st.yellow(" (unresolved at end of session)") if ev.get("trailing") else "")
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
        st.dim(
            "The score is computed afresh over all evidence inside, so it can exceed any one "
            "file's; compare it with other directories, not with files."
        ),
        st.heat(score, f"{bar(score)} {score:.1f}/100")
        + st.dim(
            f"  · {plural(int(node.get('events', 0)), 'event')}"
            f" · {_lost_phrase(float(node.get('est_minutes_lost', 0)))}"
        ),
        "",
    ]
    prefix = (str(node.get("path", "")) + "/") if node.get("path") else ""
    inside = [h for h in result.get("hotspots", []) if str(h.get("path", "")).startswith(prefix)]
    if inside:
        out.append(st.bold("Hottest files inside"))
        out.extend(_hotspot_rows(inside[:top], st, signal_titles(result), 100))
    return "\n".join(out) + "\n"
