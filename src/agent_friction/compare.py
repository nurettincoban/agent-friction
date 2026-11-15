"""Compare two analyses to check whether a fix actually reduced friction.

Raw event counts are not comparable between two windows, so the comparison uses
*events per 10 exposed sessions*: for each file, command, or directory, only the
sessions in which the agent actually touched it count (its exposure). That
separates "the friction went away" from "nobody worked there any more".

A hotspot counts as improved or regressed only when that rate moves by at least
``threshold`` (relative) and the after event count differs by at least
``min_events`` from what the before rate predicts for the after exposure.
Hotspots with fewer than ``min_events`` events on both sides are always
"unchanged", and a verdict about a target touched in fewer than ``min_exposure``
sessions on either side becomes "too-little-data", which keeps small-sample
noise out of the verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_friction.report._common import all_items, fmt_minutes, plural

STATUSES = ("regressed", "new", "improved", "resolved", "inactive", "too-little-data", "unchanged")


@dataclass(frozen=True)
class Change:
    kind: str
    path: str
    status: str  # one of STATUSES
    before_events: int
    after_events: int
    before_exposure: int
    after_exposure: int
    before_score: float
    after_score: float

    @property
    def before_rate(self) -> float | None:
        return _rate(self.before_events, self.before_exposure)

    @property
    def after_rate(self) -> float | None:
        return _rate(self.after_events, self.after_exposure)

    @property
    def delta_pct(self) -> float | None:
        b, a = self.before_rate, self.after_rate
        if not b or a is None:
            return None
        return (a - b) / b * 100

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "status": self.status,
            "before_events": self.before_events,
            "after_events": self.after_events,
            "before_exposure_sessions": self.before_exposure,
            "after_exposure_sessions": self.after_exposure,
            "before_events_per_10_sessions": self.before_rate,
            "after_events_per_10_sessions": self.after_rate,
            "before_score": self.before_score,
            "after_score": self.after_score,
        }


def _rate(events: int, exposure: int) -> float | None:
    """Events per 10 exposed sessions, or None when the target was not touched."""
    if exposure <= 0:
        return None
    return round(events / exposure * 10, 2)


def _exposure(result: dict[str, Any], kind: str, path: str, item: dict[str, Any] | None) -> int:
    """Sessions in which the agent touched this target in this result's window.

    Prefers the hotspot's own ``exposure_sessions``, then the result's top-level
    ``exposure`` map. Results without exposure data fall back to all sessions.
    """
    if item is not None and isinstance(item.get("exposure_sessions"), int):
        return int(item["exposure_sessions"])
    exposure = result.get("exposure")
    if isinstance(exposure, dict):
        table = exposure.get(kind)
        return int(table.get(path, 0)) if isinstance(table, dict) else 0
    return int(result.get("totals", {}).get("sessions", 0))


def compare(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    threshold: float = 0.25,
    min_events: int = 2,
    min_exposure: int = 1,
) -> list[Change]:
    """Classify every hotspot, command, and directory that appears in either analysis."""

    def index(result: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
        return {(str(h.get("kind", "file")), str(h.get("path", ""))): h for h in all_items(result)}

    bi, ai = index(before), index(after)
    changes: list[Change] = []
    for key in sorted(set(bi) | set(ai)):
        kind, path = key
        b, a = bi.get(key), ai.get(key)
        b_events = int(b["events"]) if b else 0
        a_events = int(a["events"]) if a else 0
        b_exp = _exposure(before, kind, path, b)
        a_exp = _exposure(after, kind, path, a)
        # A hotspot was necessarily touched in every session it has friction in.
        if b is not None:
            b_exp = max(b_exp, int(b.get("sessions", 0)), 1)
        if a is not None:
            a_exp = max(a_exp, int(a.get("sessions", 0)), 1)
        br, ar = _rate(b_events, b_exp), _rate(a_events, a_exp)

        if max(a_events, b_events) < min_events:
            status = "unchanged"
        elif a is None and a_exp == 0:
            status = "inactive"  # nobody touched it afterwards: no evidence either way
        elif b is None:
            status = "new"
        elif a is None:
            status = "resolved"
        elif (
            br is not None
            and ar is not None
            and abs(ar - br) >= threshold * br
            # The after count must differ from what the before rate predicts for the
            # after exposure by at least min_events, so tiny samples can't flip.
            and abs(a_events - br * a_exp / 10) >= min_events
        ):
            status = "improved" if ar < br else "regressed"
        else:
            status = "unchanged"
        if status in ("regressed", "new", "improved", "resolved"):
            needed = (a_exp,) if status == "new" else (b_exp, a_exp)
            if min(needed) < min_exposure:
                status = "too-little-data"
        changes.append(
            Change(
                kind=kind,
                path=path,
                status=status,
                before_events=b_events,
                after_events=a_events,
                before_exposure=b_exp,
                after_exposure=a_exp,
                before_score=float(b.get("score", 0)) if b else 0.0,
                after_score=float(a.get("score", 0)) if a else 0.0,
            )
        )
    order = {s: i for i, s in enumerate(STATUSES)}
    changes.sort(key=lambda c: (order[c.status], -max(c.before_score, c.after_score), c.path))
    return changes


def summary_line(before: dict[str, Any], after: dict[str, Any]) -> str:
    bt, at = before.get("totals", {}), after.get("totals", {})
    bs, as_ = int(bt.get("sessions", 0)), int(at.get("sessions", 0))
    b_per = float(bt.get("est_minutes_lost", 0)) / max(bs, 1)
    a_per = float(at.get("est_minutes_lost", 0)) / max(as_, 1)
    return (
        f"before: {plural(bs, 'session')}, ~{fmt_minutes(b_per)} lost per session · "
        f"after: {plural(as_, 'session')}, ~{fmt_minutes(a_per)} lost per session"
    )


def _fmt_rate(rate: float | None) -> str:
    return "-" if rate is None else f"{rate:.1f}"


def render_compare(
    before: dict[str, Any], after: dict[str, Any], changes: list[Change], *, markdown: bool = False
) -> str:
    """Human-readable comparison, as plain text or Markdown."""
    groups = [
        ("regressed", "Regressed"),
        ("new", "New friction"),
        ("improved", "Improved"),
        ("resolved", "Resolved (touched afterwards, no friction)"),
        ("inactive", "No activity afterwards (no evidence either way)"),
        ("too-little-data", "Too few sessions touched it to tell"),
    ]
    out: list[str] = ["# Friction comparison" if markdown else "agent-friction compare", ""]
    out += [summary_line(before, after), ""]
    any_change = False
    for status, label in groups:
        rows = [c for c in changes if c.status == status]
        if not rows:
            continue
        any_change = True
        out.append(f"## {label} ({len(rows)})" if markdown else f"{label} ({len(rows)})")
        for c in rows:
            suffix = "/" if c.kind == "dir" else ""
            name = f"$ {c.path}" if c.kind == "command" else c.path + suffix
            pct = c.delta_pct
            delta = f" ({pct:+.0f}%)" if pct is not None else ""
            body = (
                f"{_fmt_rate(c.before_rate)} → {_fmt_rate(c.after_rate)} events per 10 sessions"
                f" that touched it{delta} ({c.before_events}/{c.before_exposure} →"
                f" {c.after_events}/{c.after_exposure}), score {c.before_score:.1f} →"
                f" {c.after_score:.1f}"
            )
            out.append(f"- `{name}`: {body}" if markdown else f"  {name}: {body}")
        out.append("")
    if not any_change:
        out += ["No meaningful change.", ""]
    if any(
        (c.status == "regressed" and c.after_score < c.before_score)
        or (c.status == "improved" and c.after_score > c.before_score)
        for c in changes
    ):
        out += [
            "Verdicts follow the event rate in the sessions that touched each target, not the "
            "score, which also weighs severity and recurrence, so the two can move apart.",
            "",
        ]
    return "\n".join(out)
