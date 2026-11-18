"""Markdown renderer: paste into an issue, a PR comment, or a team wiki."""

from __future__ import annotations

import re
from typing import Any

from agent_friction.report._common import (
    AGENT_TOOL_HEADING,
    LEADS_HEADING,
    PASTE_HEADING,
    PASTE_NOTE,
    TOKENS_NOTE,
    agent_tool_rules,
    collect_recommendations,
    cost_line,
    dropped_note,
    events_phrase,
    fmt_lost,
    gone_note,
    grouped_unattributed,
    headline_lost,
    headline_tokens,
    is_thin,
    paste_deny,
    paste_lines,
    paste_settings_json,
    plural,
    repo_recommendations,
    signal_titles,
    thin_note,
    tokens_phrase,
    tool_said,
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
            f"| {i} | {_cell(_code(h.get('path', '')))}{gone_note(h)}"
            f" | {float(h.get('score', 0)):.1f}"
            f" | {h.get('sessions', 0)} | {h.get('events', 0)}"
            f" | {fmt_lost(float(h.get('est_minutes_lost', 0)))} | {_cell(sig)}"
            f" | {h.get('confidence', 'low')} |"
        )
    return lines


def _events(rec: dict[str, Any]) -> str:
    n = rec.get("events")
    return plural(n, "event") if isinstance(n, int) else "few events"


def _agent_tool_section(result: dict[str, Any]) -> list[str]:
    rows = agent_tool_rules(result)
    if not rows:
        return []
    out = [f"## {AGENT_TOOL_HEADING}", ""]
    for heading, text in rows:
        out.append(f"- **{_html_escape(heading)}.** {_html_escape(text)}")
    return [*out, ""]


def _details(h: dict[str, Any], titles: dict[str, str]) -> list[str]:
    out = [
        "<details>",
        f"<summary><code>{_html_escape(str(h.get('path', '')))}</code> · score "
        f"{float(h.get('score', 0)):.1f} · {plural(int(h.get('sessions', 0)), 'session')}"
        + (f" · {_html_escape(t)}" if (t := tokens_phrase(h.get("tokens_lost"))) else "")
        + "</summary>",
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
            thin = f" _(thin evidence: {_events(rec)})_" if is_thin(rec) else ""
            out.append(f"- **{title}**{thin} {detail}".rstrip())
        out.append("")
    ev = h.get("evidence", [])
    if ev:
        out.append("**Evidence**")
        out.append("")
        for e in ev:
            ts = str(e.get("timestamp", ""))[:16].replace("T", " ")
            ts = f"{ts} UTC" if ts else ts
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


def _repo_section(result: dict[str, Any]) -> list[str]:
    repo_recs = repo_recommendations(result)
    if not repo_recs:
        return []
    lines = ["## Across the repository", ""]
    for rec in repo_recs:
        title = _html_escape(str(rec.get("title", "")))
        lines.append(f"- **{title}** {_html_escape(str(rec.get('detail', '')))}")
    return [*lines, ""]


def _unattributed(result: dict[str, Any]) -> list[str]:
    heading = unattributed_heading(result)
    if heading is None:
        return []
    lines = [f"## {heading}", ""]
    for ev, n in grouped_unattributed(result):
        when = str(ev.get("timestamp") or "")[:16].replace("T", " ")
        when = f"{when} UTC" if when else when
        where = ""
        if ev.get("source"):
            where = " · " + _code(f"{ev['source']}:{ev.get('line', '?')}")
        lines.append(
            f"- `{when}` {_code(ev.get('signal', '?'))}: "
            f"{_html_escape(str(ev.get('summary', '')))}{where}" + (f" (×{n})" if n > 1 else "")
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


def _paste_section(result: dict[str, Any]) -> list[str]:
    """ "Ready to paste": the AGENTS.md lines and the deny rules, in code blocks."""
    lines = paste_lines(result)
    deny = paste_deny(result)
    if not lines and not deny:
        return []
    out = [f"## {PASTE_HEADING}", "", f"_{PASTE_NOTE}_", ""]
    if lines:
        body = "\n".join(lines)
        fence = "`" * max(3, max((len(m) for m in re.findall(r"`+", body)), default=0) + 1)
        out += ["AGENTS.md (or CLAUDE.md):", "", f"{fence}markdown", body, fence, ""]
    if deny:
        out += [
            "`.claude/settings.json` (merge into what is there):",
            "",
            "```json",
            paste_settings_json(result),
            "```",
            "",
            *(f"- {_code(rule)}: replayed, {_escape_prose(what)}." for rule, what in deny),
            "",
        ]
    return out


def render_markdown(result: dict[str, Any], *, top: int = 10) -> str:
    titles = signal_titles(result)
    totals = result.get("totals", {})
    repo = result.get("repo", {})
    out = [
        f"# Agent friction map: {repo.get('name', '?')}",
        "",
        f"{plural(int(totals.get('sessions', 0)), 'agent session')} analysed · "
        f"{events_phrase(totals)} · "
        f"{headline_lost(totals)} · "
        f"{plural(int(totals.get('corrections', 0)), 'human correction')} · "
        f"{plural(int(totals.get('files_affected', 0)), 'file')} affected",
        "",
    ]
    if headline_tokens(result):
        out += [f"Tokens: {headline_tokens(result)}. {TOKENS_NOTE}", ""]
    hotspots = list(result.get("hotspots", []))[:top]
    commands = list(result.get("commands", []))[:top]
    if not hotspots and not commands and not result.get("directories"):
        if int(totals.get("friction_events", 0)):
            out += ["No hotspot: no file, directory, or command collected friction.", ""]
        else:
            out += ["No recurring friction found.", ""]
        out += _agent_tool_section(result)
        out += _unattributed(result)
        out += _repo_section(result)
        note = dropped_note(result)
        if note:
            out += [f"_{note}_", ""]
        return "\n".join(out)
    recs = collect_recommendations(result, limit=5)
    if recs:
        leads = all(is_thin(rec) for _, rec in recs)
        out += [f"## {LEADS_HEADING if leads else 'What to fix first'}", ""]
        for i, (h, rec) in enumerate(recs, 1):
            title = _html_escape(str(rec.get("title", "")))
            detail = _html_escape(str(rec.get("detail", "")))
            said = tool_said(h, rec)
            if said:
                detail += " " + _html_escape(said)
            thin = " _(thin evidence)_" if is_thin(rec) else ""
            mark = "-" if leads else f"{i}."
            cost = cost_line(h)
            if cost:
                detail += " " + _html_escape(cost)
            out.append(f"{mark} {_code(h.get('path'))}: **{title}**{thin} {detail}")
        out.append("")
        said = thin_note(result, recs)
        if said:
            out += [f"_{said}_", ""]
    out += _paste_section(result)
    note = dropped_note(result)
    if note:
        out += [f"_{note}_", ""]
    out += _agent_tool_section(result)
    out += _repo_section(result)

    if hotspots:
        out += ["## Files where agents struggle", "", *_table(hotspots, "File"), ""]
    if commands:
        out += ["## Commands that keep failing", "", *_table(commands, "Command"), ""]
    directories = list(result.get("directories", []))[:top]
    if directories:
        out += [
            "## Directories where agents search in vain",
            "",
            "Only friction in the directory itself (searches that found nothing there); "
            "`agent-friction show <dir>` rolls up everything inside it.",
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
