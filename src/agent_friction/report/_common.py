"""Small formatting helpers shared by the renderers."""

from __future__ import annotations

import math
import re
from typing import Any

from agent_friction.recommend import THIN_EVIDENCE_EVENTS
from agent_friction.signals import OUTPUT_SIGNALS


def gone_note(h: dict[str, Any]) -> str:
    """ " (renamed)" / " (not in tree)" for a file hotspot that is no longer in the tree."""
    f = h.get("file")
    if h.get("kind") != "file" or not isinstance(f, dict) or f.get("exists") is not False:
        return ""
    return " (renamed)" if f.get("renamed_to") else " (not in tree)"


#: How "What to fix first" names advice that is about the repository, not one target.
REPO_LABEL = "Across the repository"


def repo_recommendations(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Advice about the repository as a whole (a background rate), not one target; not
    the items that go to "What to fix first" (``rank: "first"``), which are shown there."""
    return [
        r
        for r in result.get("recommendations") or []
        if isinstance(r, dict) and r.get("rank") != "first"
    ]


def repo_fix_first(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Repository-level advice that belongs in "What to fix first" (a habit of agents
    here that recurs across sessions and files, such as reporting work done unchecked)."""
    return [
        r
        for r in result.get("recommendations") or []
        if isinstance(r, dict) and r.get("rank") == "first"
    ]


def events_phrase(totals: dict[str, Any]) -> str:
    """ "23 friction events (+11 weak)": the headline counts what can be acted on.

    Weak signals (an empty search, a re-read) are what productive work also
    produces; on a large repository they can be half of all events, so they are
    named beside the count, not in it.
    """
    events = int(totals.get("friction_events", 0) or 0)
    strong = totals.get("strong_events")
    if isinstance(strong, int) and 0 <= strong < events:
        return f"{plural(strong, 'friction event')} (+{events - strong} weak)"
    return plural(events, "friction event")


def fmt_lost(minutes: float) -> str:
    """A row's time lost; "n/a" when its evidence carries no time estimate at all."""
    return fmt_minutes(minutes) if minutes > 0 else "n/a"


def headline_lost(totals: dict[str, Any]) -> str:
    """The headline's time lost: "no time estimate" when no evidence carried any time."""
    minutes = float(totals.get("est_minutes_lost", 0) or 0)
    return f"~{fmt_minutes(minutes)} lost" if minutes > 0 else "no time estimate"


def fmt_count(n: int) -> str:
    """A token count compactly: ``950``, ``6.2k``, ``79k``, ``1.2M``."""
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= size * 0.9995:
            value = n / size
            text = f"{value:.1f}" if value < 9.95 else f"{value:.0f}"
            return text.removesuffix(".0") + unit
    return str(n)


def _token_parts(tokens: object) -> tuple[int, int] | None:
    """(tokens the model was given fresh or wrote, tokens read from the prompt cache)."""
    if not isinstance(tokens, dict) or not int(tokens.get("responses", 0) or 0):
        return None
    fresh = sum(int(tokens.get(k, 0) or 0) for k in ("input", "cache_write", "output"))
    return fresh, int(tokens.get("cache_read", 0) or 0)


def tokens_phrase(tokens: object) -> str:
    """ "6.2k tokens (+79k cache reads)" for a ``tokens_lost`` tally, "" when it has none.

    Cache reads are named apart: they are most of every count and cost a fraction of
    the rest, so one sum would mostly measure how long the conversation already was.
    """
    parts = _token_parts(tokens)
    if parts is None:
        return ""
    fresh, cached = parts
    return f"{fmt_count(fresh)} tokens" + (f" (+{fmt_count(cached)} cache reads)" if cached else "")


#: What the token counts are, said wherever they are shown in full.
TOKENS_NOTE = (
    "Token counts are the model's usage as the transcripts recorded it, in the responses "
    "written inside the stretches counted as time lost (for a failed run alone, the response "
    "that read its output); which stretches belong to which problem is "
    "agent-friction's assignment, not something the transcripts say. Cache reads are shown "
    "apart: they are re-reads of the conversation so far, cheaper per token than the rest."
)


def cost_line(item: dict[str, Any]) -> str:
    """ "Cost so far: 6.2k tokens (+79k cache reads) and ~2m, over 3 sessions." for one
    item: "" when the scan has no token usage for it."""
    tokens = tokens_phrase(item.get("tokens_lost"))
    if not tokens:
        return ""
    minutes = float(item.get("est_minutes_lost", 0) or 0)
    lost = f" and ~{fmt_minutes(minutes)}" if minutes > 0 else ""
    sessions = int(item.get("sessions", 0) or 0)
    return (
        f"Cost so far: {tokens}{lost} in the agent turns counted against it, over "
        f"{plural(sessions, 'session')}."
    )


def headline_tokens(totals: dict[str, Any]) -> str:
    """ "20k (+304k cache reads) in the turns counted as lost, of 391k (+7.5M cache reads)
    in all sessions": "" without usage."""
    lost, total = _token_parts(totals.get("tokens_lost")), _token_parts(totals.get("tokens_total"))
    if lost is None or total is None:
        return ""
    return (
        f"{fmt_count(lost[0])} (+{fmt_count(lost[1])} cache reads) in the turns counted as "
        f"lost, of {fmt_count(total[0])} (+{fmt_count(total[1])} cache reads) in all sessions"
    )


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


def code_span(text: str) -> str:
    """``text`` as an inline code span, fenced with more backticks than it contains."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if longest else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def tool_said(hotspot: dict[str, Any], rec: dict[str, Any]) -> str:
    """ "The tool said: `…` (4 of 4 failures, 4 sessions)": the error line that keeps
    coming back in the output behind this advice, or "" when there is none to show."""
    if not OUTPUT_SIGNALS.intersection(rec.get("signals") or ()):
        return ""
    said = [s for s in hotspot.get("signatures") or [] if isinstance(s, dict) and s.get("text")]
    if not said:
        return ""
    top = said[0]
    text = str(top["text"])
    if text in str(rec.get("detail", "")):
        return ""
    events, sessions = int(top.get("events", 0)), int(top.get("sessions", 0))
    # out of the events that have tool output, not the corrections beside them
    total = int(hotspot.get("output_events") or 0) or events
    quoted = code_span(text)
    if events <= 1:
        return f"The tool said: {quoted}."
    where = f", {plural(sessions, 'session')}" if sessions > 1 else ""
    if top.get("variants"):
        where += "; the numbers in it differ between events"
    return f"The tool said: {quoted} ({events} of {total} events{where})."


def is_thin(rec: dict[str, Any]) -> bool:
    """Advice that rests on too few events to act on (``"evidence": "thin"``)."""
    return rec.get("evidence") == "thin"


def collect_recommendations(
    result: dict[str, Any], limit: int = 5
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Highest-impact recommendations first: (hotspot, recommendation) pairs.

    Items backed by medium or high confidence come first, then by score; each
    contributes its first (most relevant) recommendation. Advice on thin evidence
    (a handful of events) is left out while there is any other; with none other, it
    is what there is, and each one says so.
    """
    ranked = sorted(
        all_items(result),
        key=lambda h: (
            h.get("confidence") == "low",
            -float(h.get("score", 0)),
            h.get("kind", ""),
            h.get("path", ""),
        ),
    )
    repo = [({"path": REPO_LABEL, "kind": "repo"}, rec) for rec in repo_fix_first(result)]
    strong = repo + _first_recs(ranked, limit, thin=False)
    return strong[:limit] or _first_recs(ranked, limit, thin=True)


def is_agent_tool(rec: dict[str, Any]) -> bool:
    """Advice about a rule of the agent tool itself (``origin: "agent-tool"``)."""
    return rec.get("origin") == "agent-tool"


def _first_recs(
    ranked: list[dict[str, Any]], limit: int, *, thin: bool
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each hotspot's first recommendation of the asked strength; advice about the
    repository before advice about the agent tool's own rules."""
    picked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for hotspot in ranked:
        recs = [r for r in hotspot.get("recommendations", []) if isinstance(r, dict)]
        picked.extend((hotspot, rec) for rec in [r for r in recs if is_thin(r) == thin][:1])
    picked.sort(key=lambda pair: is_agent_tool(pair[1]))
    return picked[:limit]


def paste_candidates(result: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Advice that names text to paste (``agents_md``, ``deny``), strongest item first:
    not on thin evidence, not already written down or contradicted there, and not about
    the agent tool's own rules."""
    ranked = sorted(
        all_items(result),
        key=lambda h: (
            h.get("confidence") == "low",
            -float(h.get("score", 0)),
            h.get("kind", ""),
            h.get("path", ""),
        ),
    )
    return [
        (hotspot, rec)
        for hotspot in ranked
        for rec in hotspot.get("recommendations", [])
        if isinstance(rec, dict)
        and (rec.get("agents_md") or rec.get("deny"))
        and not is_thin(rec)
        and not is_agent_tool(rec)
        and rec.get("documented_as") not in ("instruction", "conflict")
    ]


def thin_count(result: dict[str, Any]) -> int:
    """Pieces of advice anywhere in the result that rest on thin evidence."""
    return sum(
        is_thin(r)
        for h in all_items(result)
        for r in h.get("recommendations", [])
        if isinstance(r, dict)
    )


def thin_note(result: dict[str, Any], shown: list[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    """The line under "What to fix first" about advice left out for thin evidence."""
    if any(is_thin(rec) for _, rec in shown):
        return (
            f"Each of these rests on fewer than {THIN_EVIDENCE_EVENTS} events: a lead to "
            "look at, not yet a reason to change the repository."
        )
    n = thin_count(result)
    if not n:
        return ""
    verb, be = ("rests", "is") if n == 1 else ("rest", "are")
    return (
        f"{plural(n, 'more suggestion')} {verb} on fewer than {THIN_EVIDENCE_EVENTS} events "
        f"each and {be} left out here. Each item's own details list them, marked "
        '"thin evidence" (in the terminal: `agent-friction show <path>`).'
    )


def agent_tool_rules(result: dict[str, Any]) -> list[tuple[str, str]]:
    """(heading, text) for each command the agent tool refused by its own rules.

    Reported apart from the hotspots: a rule of the agent tool (Claude Code refusing a
    foreground `sleep`) is the same in every repository, so it counts in no score.
    """
    out: list[tuple[str, str]] = []
    for row in result.get("agent_tool") or []:
        if not isinstance(row, dict):
            continue
        command = row.get("command") or "a call"
        minutes = float(row.get("est_minutes_lost", 0) or 0)
        lost = f", ~{fmt_minutes(minutes)} of agent time" if minutes > 0 else ""
        heading = (
            f"{command}: refused {plural(int(row.get('events', 0) or 0), 'time')} in "
            f"{plural(int(row.get('sessions', 0) or 0), 'session')}{lost}"
        )
        recs = [r for r in row.get("recommendations") or [] if isinstance(r, dict)]
        text = (
            str(recs[0].get("detail", ""))
            if recs
            else ("Refused by a rule of the agent tool itself, not by anything in this repository.")
        )
        out.append((heading, text))
    return out


AGENT_TOOL_HEADING = "The agent tool's own rules (not counted above)"
#: The heading of "What to fix first" when nothing there rests on enough events.
LEADS_HEADING = "Nothing to fix yet: leads to watch"


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
        nearby = min(outside, int(totals.get("events_elsewhere_in_repo", 0) or 0))
        far = outside - nearby
        places: list[str] = []
        if nearby:
            places.append(
                f"{plural(nearby, 'declined, denied, or failed call')} that acted elsewhere in "
                "the repository, outside the scanned directory"
            )
        if far:
            what = "call" if nearby else "declined, denied, or failed call"
            places.append(f"{plural(far, what)} that acted outside this repository")
        parts.append(f"{'Also not' if parts else 'Not'} counted: {' and '.join(places)}.")
    untimed = int(totals.get("untimed_sessions_excluded", 0) or 0)
    if untimed > 0:
        parts.append(
            f"{'Also not' if parts else 'Not'} counted: {plural(untimed, 'session')} with no "
            "timestamps, which cannot be placed in this time window."
        )
    return " ".join(parts) or None


def grouped_unattributed(result: dict[str, Any]) -> list[tuple[dict[str, Any], int]]:
    """Unattributed evidence with identical lines collapsed: (first event, how many)."""
    out: list[tuple[dict[str, Any], int]] = []
    index: dict[tuple[str, str], int] = {}
    for ev in result.get("unattributed") or []:
        key = (str(ev.get("signal", "")), str(ev.get("summary", "")))
        if key in index:
            first, n = out[index[key]]
            out[index[key]] = (first, n + 1)
        else:
            index[key] = len(out)
            out.append((ev, 1))
    return out


def unattributed_heading(result: dict[str, Any]) -> str | None:
    """Heading for friction events that no file, directory, or command could own."""
    count = int(result.get("totals", {}).get("unattributed_events", 0) or 0)
    items = result.get("unattributed") or []
    if count <= 0 and not items:
        return None
    count = max(count, len(items))
    shown = f", {len(items)} shown" if 0 < len(items) < count else ""
    return f"Not tied to a file or command ({plural(count, 'event')}{shown})"


PASTE_HEADING = "Ready to paste"
PASTE_NOTE = (
    "The lines the advice above names, filled in from the evidence. Check each against "
    "the repository before you commit it; nothing here has been applied."
)


def paste_lines(result: dict[str, Any]) -> list[str]:
    """The AGENTS.md / CLAUDE.md lines to paste, as Markdown list items."""
    paste = result.get("paste") or {}
    out: list[str] = []
    for item in paste.get("agents_md") or []:
        line = str(item.get("line", "")).strip() if isinstance(item, dict) else ""
        if line:
            out.append("- " + line[:1].upper() + line[1:])
    return out


def paste_deny(result: dict[str, Any]) -> list[tuple[str, str]]:
    """(rule, what replaying it over these sessions shows) for each deny rule to paste."""
    paste = result.get("paste") or {}
    out: list[tuple[str, str]] = []
    for item in paste.get("deny") or []:
        if not isinstance(item, dict) or not item.get("rule"):
            continue
        calls = int(item.get("would_refuse", 0) or 0)
        targets = [t for t in item.get("refused_targets") or [] if isinstance(t, dict)]
        if calls:
            what = f"would have refused {plural(calls, 'call')} in " + plural(
                int(item.get("sessions", 0) or 0), "session"
            )
            if len(targets) == 1:
                what += f", all on {targets[0].get('target')}"
            elif targets:
                what += ", on " + ", ".join(
                    f"{t.get('target')} ({t.get('calls')})" for t in targets[:3]
                )
        else:
            what = "refuses no call these sessions made"
        others = int(item.get("other_agent_sessions", 0) or 0)
        if others:
            what += (
                f"; Claude Code settings do not cover the {plural(others, 'session')} of "
                "another agent that also did this"
            )
        if str(item["rule"]).startswith("Bash("):
            what += (
                "; it stops the command as typed, not every way to do the same, so keep the "
                "line in AGENTS.md too"
            )
        out.append((str(item["rule"]), what))
    return out


def paste_settings_json(result: dict[str, Any]) -> str:
    """The deny rules as a ``.claude/settings.json`` fragment, or ``""``."""
    import json

    rules = [rule for rule, _ in paste_deny(result)]
    if not rules:
        return ""
    return json.dumps({"permissions": {"deny": rules}}, indent=2)
