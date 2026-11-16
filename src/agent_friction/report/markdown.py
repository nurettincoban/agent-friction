"""Markdown renderer: paste into an issue, a PR comment, or a team wiki."""

from __future__ import annotations

import re
from typing import Any

from agent_friction.report._common import (
    collect_recommendations,
    dropped_note,
    fmt_lost,
    fmt_minutes,
    plural,
    signal_titles,
    top_signals,
    unattributed_heading,
)


def _cell(text: object) -> str:
    """Escape text for a Markdown table cell."""
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _code(text: object) -> str:
    """Inline code that survives backticks in the content (CommonMark code spans)."""
    s = str(text).replace("\n", " ")
    longest = max((len(run) for run in re.findall(r"`+", s)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def _table(items: list[dict[str, Any]], label: str) -> list[str]:
    lines = [
        f"| # | {label} | Score | Sessions | Events | Est. time lost | Top signals | Confidence |",
        "|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for i, h in enumerate(items, 1):
        sig = ", ".join(f"{_code(sid)}×{n}" for sid, n in top_signals(h, 3))
        lines.append(
            f"| {i} | {_cell(_code(h.get('path', '')))} | {float(h.get('score', 0)):.1f}"
            f" | {h.get('sessions', 0)} | {h.get('events', 0)}"
            f" | {fmt_lost(float(h.get('est_minutes_lost', 0)))} | {_cell(sig)}"
            f" | {h.get('confidence', 'low')} |"
        )
    return lines


def _details(h: dict[str, Any], titles: dict[str, str]) -> list[str]:
    out = [
        "<details>",
        f"<summary><code>{_html_escape(str(h.get('path', '')))}</code> · score "
        f"{float(h.get('score', 0)):.1f} · {plural(int(h.get('sessions', 0)), 'session')}"
        "</summary>",
        "",
    ]
    why = str(h.get("why", "")).strip()
    if why:
        out += [_html_escape(why), ""]
    recs = h.get("recommendations", [])
    if recs:
        out.append("**Recommendations**")
        out.append("")
        for rec in recs:
            title, detail = (
                _html_escape(str(rec.get("title", ""))),
                _html_escape(str(rec.get("detail", ""))),
            )
            out.append(f"- **{title}** {detail}".rstrip())
        out.append("")
    ev = h.get("evidence", [])
    if ev:
        out.append("**Evidence**")
        out.append("")
        for e in ev:
            ts = str(e.get("timestamp", ""))[:16].replace("T", " ")
            sid = str(e.get("signal", ""))
            out.append(
                f"- `{ts}` session `{str(e.get('session_id', ''))[:8]}` · "
                f"{titles.get(sid, sid)}: {_html_escape(str(e.get('summary', '')))}"
                + (" (unresolved at end of session)" if e.get("trailing") else "")
            )
            if e.get("excerpt"):
                quoted = _html_escape(str(e["excerpt"]).replace("\n", " "))
                out.append(f"  > {quoted}")
        out.append("")
    out += ["</details>", ""]
    return out


def _unattributed(result: dict[str, Any]) -> list[str]:
    heading = unattributed_heading(result)
    if heading is None:
        return []
    lines = [f"## {heading}", ""]
    for ev in result.get("unattributed") or []:
        when = str(ev.get("timestamp") or "")[:16].replace("T", " ")
        where = ""
        if ev.get("source"):
            where = " · " + _code(f"{ev['source']}:{ev.get('line', '?')}")
        lines.append(
            f"- `{when}` {_code(ev.get('signal', '?'))}: "
            f"{_html_escape(str(ev.get('summary', '')))}{where}"
        )
    lines.append("")
    return lines


def _html_escape(s: str) -> str:
    """Escape HTML in prose, leaving `code spans` alone (GitHub shows them literally)."""
    out: list[str] = []
    last = 0
    for m in _CODE_SPAN.finditer(s):
        out.append(_escape_prose(s[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_escape_prose(s[last:]))
    return "".join(out)


_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)


def _escape_prose(s: str) -> str:
    # Quoted transcript text must not render images or links, or notify people, when the
    # report is pasted into an issue or PR: escape link brackets and break @mentions.
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("[", "\\[").replace("]", "\\]")
    return re.sub(r"@(?=[A-Za-z0-9])", "@\u200b", s)


def render_markdown(result: dict[str, Any], *, top: int = 10) -> str:
    titles = signal_titles(result)
    totals = result.get("totals", {})
    repo = result.get("repo", {})
    out = [
        f"# Agent friction map: {repo.get('name', '?')}",
        "",
        f"{plural(int(totals.get('sessions', 0)), 'agent session')} analysed · "
        f"{plural(int(totals.get('friction_events', 0)), 'friction event')} · "
        f"~{fmt_minutes(float(totals.get('est_minutes_lost', 0)))} lost · "
        f"{plural(int(totals.get('corrections', 0)), 'human correction')} · "
        f"{plural(int(totals.get('files_affected', 0)), 'file')} affected",
        "",
    ]
    hotspots = list(result.get("hotspots", []))[:top]
    commands = list(result.get("commands", []))[:top]
    if not hotspots and not commands and not result.get("directories"):
        if int(totals.get("friction_events", 0)):
            out += ["No hotspot: no file, directory, or command collected friction.", ""]
        else:
            out += ["No recurring friction found.", ""]
        out += _unattributed(result)
        note = dropped_note(result)
        if note:
            out += [f"_{note}_", ""]
        return "\n".join(out)
    note = dropped_note(result)
    if note:
        out += [f"_{note}_", ""]

    recs = collect_recommendations(result, limit=5)
    if recs:
        out += ["## What to fix first", ""]
        for i, (h, rec) in enumerate(recs, 1):
            title = _html_escape(str(rec.get("title", "")))
            detail = _html_escape(str(rec.get("detail", "")))
            out.append(f"{i}. {_code(h.get('path'))}: **{title}** {detail}")
        out.append("")

    if hotspots:
        out += ["## Files where agents struggle", "", *_table(hotspots, "File"), ""]
    if commands:
        out += ["## Commands that keep failing", "", *_table(commands, "Command"), ""]
    directories = list(result.get("directories", []))[:top]
    if directories:
        out += [
            "## Directories where agents search in vain",
            "",
            *_table(directories, "Directory"),
            "",
        ]

    out += ["## Evidence", ""]
    for h in [*hotspots, *commands, *directories]:
        out += _details(h, titles)
    out += _unattributed(result)

    used = [s for s in result.get("signals", []) if s.get("count")]
    if used:
        out += ["## Signals", "", "| Signal | Count | Meaning |", "|---|---:|---|"]
        for s in used:
            out.append(
                f"| {_code(s['id'])} {_cell(s.get('title', ''))} | {s.get('count', 0)}"
                f" | {_cell(s.get('description', ''))} |"
            )
        out.append("")
    tool = result.get("tool", {})
    out.append(
        f"<sub>Generated by agent-friction {tool.get('version', '')} at "
        f"{result.get('generated_at', '')}.</sub>"
    )
    out.append("")
    return "\n".join(out)
