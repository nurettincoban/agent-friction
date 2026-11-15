"""Compare two analyses to check whether a fix actually reduced friction.

Raw event counts are not comparable when the two windows contain a different
number of sessions, so the comparison uses *events per 10 sessions* for each
hotspot. A hotspot counts as improved or regressed only when that rate moves by
at least ``threshold`` (relative) and by at least one whole event in absolute
terms, which keeps small-sample noise out of the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_friction.report._common import all_items, fmt_minutes


@dataclass(frozen=True)
class Change:
    kind: str
    path: str
    status: str  # "improved" | "regressed" | "new" | "resolved" | "unchanged"
    before_rate: float
    after_rate: float
    before_score: float
    after_score: float

    @property
    def delta_pct(self) -> float | None:
        if self.before_rate == 0:
            return None
        return (self.after_rate - self.before_rate) / self.before_rate * 100


def _rate(events: int, sessions: int) -> float:
    return events / max(sessions, 1) * 10


def compare(
    before: dict[str, Any], after: dict[str, Any], *, threshold: float = 0.25
) -> list[Change]:
    """Classify every hotspot and command that appears in either analysis."""
    bs = int(before.get("totals", {}).get("sessions", 0))
    as_ = int(after.get("totals", {}).get("sessions", 0))

    def index(result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        pool = all_items(result)
        return {(str(h.get("kind", "file")), str(h.get("path", ""))): h for h in pool}

    bi, ai = index(before), index(after)
    changes: list[Change] = []
    for key in sorted(set(bi) | set(ai)):
        b, a = bi.get(key), ai.get(key)
        br = _rate(int(b["events"]), bs) if b else 0.0
        ar = _rate(int(a["events"]), as_) if a else 0.0
        b_events = int(b["events"]) if b else 0
        a_events = int(a["events"]) if a else 0
        if b is None:
            status = "new"
        elif a is None:
            status = "resolved"
        elif abs(ar - br) >= threshold * max(br, 1e-9) and abs(a_events - b_events) >= 1:
            status = "improved" if ar < br else "regressed"
        else:
            status = "unchanged"
        changes.append(
            Change(
                kind=key[0],
                path=key[1],
                status=status,
                before_rate=round(br, 2),
                after_rate=round(ar, 2),
                before_score=float(b.get("score", 0)) if b else 0.0,
                after_score=float(a.get("score", 0)) if a else 0.0,
            )
        )
    order = {"regressed": 0, "new": 1, "improved": 2, "resolved": 3, "unchanged": 4}
    changes.sort(key=lambda c: (order[c.status], -max(c.before_score, c.after_score), c.path))
    return changes


def summary_line(before: dict[str, Any], after: dict[str, Any]) -> str:
    bt, at = before.get("totals", {}), after.get("totals", {})
    bs, as_ = int(bt.get("sessions", 0)), int(at.get("sessions", 0))
    b_per = float(bt.get("est_minutes_lost", 0)) / max(bs, 1)
    a_per = float(at.get("est_minutes_lost", 0)) / max(as_, 1)
    return (
        f"before: {bs} sessions, ~{fmt_minutes(b_per)} lost per session · "
        f"after: {as_} sessions, ~{fmt_minutes(a_per)} lost per session"
    )


def render_compare(
    before: dict[str, Any], after: dict[str, Any], changes: list[Change], *, markdown: bool = False
) -> str:
    """Human-readable comparison, as plain text or Markdown."""
    head = summary_line(before, after)
    groups = [
        ("regressed", "Regressed"),
        ("new", "New friction"),
        ("improved", "Improved"),
        ("resolved", "Resolved"),
    ]
    out: list[str] = []
    out.append("# Friction comparison" if markdown else "agent-friction compare")
    out.append("")
    out.append(head)
    out.append("")
    any_change = False
    for status, label in groups:
        rows = [c for c in changes if c.status == status]
        if not rows:
            continue
        any_change = True
        out.append(f"## {label} ({len(rows)})" if markdown else f"{label} ({len(rows)})")
        for c in rows:
            name = f"$ {c.path}" if c.kind == "command" else c.path
            pct = c.delta_pct
            delta = f" ({pct:+.0f}%)" if pct is not None else ""
            line = (
                f"{name}: {c.before_rate:.1f} → {c.after_rate:.1f} events/10 sessions{delta},"
                f" score {c.before_score:.1f} → {c.after_score:.1f}"
            )
            out.append(f"- `{name}`: {line.split(': ', 1)[1]}" if markdown else f"  {line}")
        out.append("")
    if not any_change:
        out.append("No meaningful change.")
        out.append("")
    return "\n".join(out)
