"""Command-line interface.

Exit codes: 0 success, 1 runtime error (for example no transcripts found),
2 usage error, 3 a ``--fail-on`` / ``--fail-on-regression`` gate tripped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

from agent_friction import __version__
from agent_friction.compare import compare, render_compare
from agent_friction.errors import AgentFrictionError
from agent_friction.report import (
    FORMATS,
    render_html,
    render_json,
    render_markdown,
    render_terminal,
)
from agent_friction.report._common import all_items, find_dir, find_target
from agent_friction.report.terminal import render_detail, render_dir_detail

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_GATE = 0, 1, 2, 3

_REL = re.compile(r"^(\d+)\s*([hdwm])$")
_EXT_FORMAT = {".html": "html", ".htm": "html", ".md": "markdown", ".json": "json"}


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """Parse ``30d`` / ``2w`` / ``12h`` / ``3m`` (months ≈ 30d) or an ISO date/datetime."""
    now = now or datetime.now(timezone.utc)
    m = _REL.match(value.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"h": n / 24, "d": n, "w": 7 * n, "m": 30 * n}[unit]
        return now - timedelta(days=days)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid time {value!r}: use e.g. 30d, 2w, 12h, 2026-01-31 or 2026-01-31T09:00"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return n


def _score(value: str) -> float:
    try:
        n = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a score 0-100, got {value!r}") from None
    if not 0 <= n <= 100:
        raise argparse.ArgumentTypeError(f"expected a score 0-100, got {value!r}")
    return n


def _use_color(choice: str, stream: TextIO) -> bool:
    if choice == "always":
        return True
    if choice == "never" or os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _add_input_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("input")
    g.add_argument(
        "-t",
        "--transcripts",
        action="append",
        type=Path,
        metavar="PATH",
        help="transcript file or directory to read (repeatable). "
        "Default: auto-discover Claude Code sessions for the repo.",
    )
    g.add_argument(
        "--since",
        type=parse_when,
        metavar="WHEN",
        help="only sessions after WHEN (30d, 2026-01-31)",
    )
    g.add_argument("--until", type=parse_when, metavar="WHEN", help="only sessions before WHEN")
    g.add_argument("--no-git", action="store_true", help="skip git history (human-rework signal)")
    g.add_argument(
        "--repo-alias",
        action="append",
        default=[],
        metavar="PATH",
        help="another absolute path where this repo lived when sessions were recorded "
        "(another machine, an old checkout); repeatable",
    )
    g.add_argument(
        "--rework-days",
        type=_positive_int,
        default=14,
        metavar="N",
        help="human edits within N days of an agent commit count as rework (default 14)",
    )
    g.add_argument(
        "--no-excerpts",
        action="store_true",
        help="omit transcript excerpts from evidence (safer for sharing)",
    )
    g.add_argument(
        "--allow-empty",
        action="store_true",
        help="produce a report even when no session touched the repo (default: exit 1)",
    )
    g.add_argument(
        "--max-evidence",
        type=_positive_int,
        default=5,
        metavar="N",
        help="evidence items kept per hotspot (default 5)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-friction",
        description="Map where coding agents lose time, make mistakes, and need human "
        "correction in your codebase, with the evidence to decide what to fix.",
        epilog="Examples:\n"
        "  agent-friction scan                      # friction map for the current repo\n"
        "  agent-friction scan --since 30d -o map.html\n"
        "  agent-friction show src/api/client.py   # evidence for one file\n"
        "  agent-friction scan -f json -o before.json  # ...fix things, later:\n"
        "  agent-friction compare before.json after.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    scan = sub.add_parser("scan", help="analyse agent sessions and print the friction map")
    scan.add_argument("repo", nargs="?", type=Path, default=Path(), help="repo root (default .)")
    _add_input_options(scan)
    out = scan.add_argument_group("output")
    out.add_argument(
        "-f",
        "--format",
        choices=FORMATS,
        help="output format (default: terminal, or inferred from --output extension)",
    )
    out.add_argument("-o", "--output", type=Path, metavar="FILE", help="write to FILE")
    out.add_argument(
        "--top", type=_positive_int, default=10, metavar="N", help="rows per table (default 10)"
    )
    out.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto", help="terminal colors"
    )
    out.add_argument(
        "--fail-on",
        type=_score,
        metavar="SCORE",
        help="exit 3 if any hotspot scores at least SCORE (for CI gates)",
    )

    show = sub.add_parser("show", help="show the evidence behind one file, directory, or command")
    show.add_argument("target", help="repo-relative file or directory, or a normalized command")
    show.add_argument("--repo", type=Path, default=Path(), help="repo root (default .)")
    show.add_argument(
        "--from",
        dest="from_json",
        type=Path,
        metavar="JSON",
        help="read a saved `scan -f json` result instead of scanning",
    )
    show.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    _add_input_options(show)

    cmp_ = sub.add_parser("compare", help="compare two saved scans: did friction go down?")
    cmp_.add_argument("before", type=Path, help="earlier `scan -f json` result")
    cmp_.add_argument("after", type=Path, help="later `scan -f json` result")
    cmp_.add_argument(
        "-f", "--format", choices=("terminal", "markdown", "json"), default="terminal"
    )
    cmp_.add_argument(
        "--threshold",
        type=float,
        default=0.25,
        metavar="FRACTION",
        help="relative change in events/session that counts as a change (default 0.25)",
    )
    cmp_.add_argument(
        "--min-events",
        type=_positive_int,
        default=2,
        metavar="N",
        help="ignore hotspots with fewer than N events on both sides (default 2)",
    )
    cmp_.add_argument(
        "--min-sessions",
        type=_positive_int,
        default=3,
        metavar="N",
        help="below N sessions on either side, report but never trip the gate (default 3)",
    )
    cmp_.add_argument(
        "--allow-repo-mismatch",
        action="store_true",
        help="compare scans whose repository names differ (e.g. after a rename)",
    )
    cmp_.add_argument(
        "--fail-on-regression", action="store_true", help="exit 3 if anything regressed or is new"
    )

    src = sub.add_parser("sources", help="list the transcripts that would be analysed")
    src.add_argument("repo", nargs="?", type=Path, default=Path(), help="repo root (default .)")
    return parser


def _analyze(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    from agent_friction.engine import AnalyzeOptions, analyze

    if not repo.is_dir():
        raise AgentFrictionError(f"{repo}: not a directory (pass the repository root)")
    result = analyze(
        AnalyzeOptions(
            repo=repo,
            transcripts=args.transcripts,
            since=args.since,
            until=args.until,
            include_git=not args.no_git,
            include_excerpts=not args.no_excerpts,
            max_evidence=args.max_evidence,
            repo_aliases=tuple(args.repo_alias),
            rework_window_days=args.rework_days,
        )
    )
    if int(result.get("totals", {}).get("sessions", 0)) == 0 and not args.allow_empty:
        window = " in the selected time window" if (args.since or args.until) else ""
        raise AgentFrictionError(
            f"found transcripts, but no agent session touched {repo.resolve()}{window}, "
            "so there is nothing to analyse. Check the repo path, --since/--until, or "
            "--repo-alias (if sessions were recorded at another path). "
            "Pass --allow-empty to produce an empty report anyway."
        )
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AgentFrictionError(f"{path}: no such file") from None
    except json.JSONDecodeError as exc:
        raise AgentFrictionError(f"{path}: not valid JSON ({exc})") from None
    if not isinstance(data, dict) or "schema_version" not in data:
        raise AgentFrictionError(
            f"{path}: not an agent-friction result (make one with `scan -f json`)"
        )
    if data["schema_version"] == 1 and not (
        isinstance(data.get("totals"), dict)
        and isinstance(data["totals"].get("sessions"), int)
        and all(isinstance(data.get(k), list) for k in ("hotspots", "commands"))
    ):
        raise AgentFrictionError(
            f"{path}: not an agent-friction result (make one with `scan -f json`)"
        )
    if data["schema_version"] != 1:
        raise AgentFrictionError(
            f"{path}: schema_version {data['schema_version']} is not supported by "
            f"agent-friction {__version__}"
        )
    return data


def _write(text: str, output: Path | None, stdout: TextIO) -> None:
    if output is None:
        stdout.write(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def _cmd_scan(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    fmt = args.format
    if fmt is None and args.output is not None:
        fmt = _EXT_FORMAT.get(args.output.suffix.lower())
        if fmt is None:
            stderr.write(
                f"agent-friction: can't tell the format from {args.output.name!r}; "
                "use a .html, .md or .json extension, or pass --format\n"
            )
            return EXIT_USAGE
    fmt = fmt or "terminal"
    result = _analyze(args, args.repo)
    if fmt == "json":
        text = render_json(result)
    elif fmt == "markdown":
        text = render_markdown(result, top=args.top)
    elif fmt == "html":
        text = render_html(result)
    else:
        color = args.output is None and _use_color(args.color, stdout)
        text = render_terminal(result, color=color, top=args.top)
    _write(text, args.output, stdout)
    if args.output is not None:
        stderr.write(f"wrote {fmt} report to {args.output}\n")

    if args.fail_on is not None:
        over = [h for h in all_items(result) if float(h.get("score", 0)) >= args.fail_on]
        if over:
            names = ", ".join(str(h["path"]) for h in over[:5])
            stderr.write(
                f"agent-friction: {len(over)} hotspot(s) at or above {args.fail_on:g}: {names}\n"
            )
            return EXIT_GATE
    return EXIT_OK


def _cmd_show(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    if args.from_json is not None:
        result = _load_json(args.from_json)
    else:
        args.max_evidence = max(args.max_evidence, 20)
        result = _analyze(args, args.repo)
    color = _use_color(args.color, stdout)
    item = find_target(result, args.target)
    node = find_dir(result, args.target)
    if item is not None and item.get("kind") != "dir":
        stdout.write(render_detail(result, item, color=color))
        return EXIT_OK
    if node is not None or item is not None:
        # A directory: its rolled-up subtree first, then friction attributed to the
        # directory itself (for example searches that came back empty there).
        if node is not None:
            stdout.write(render_dir_detail(result, node, color=color))
        if item is not None:
            stdout.write("\n" + render_detail(result, item, color=color))
        return EXIT_OK
    stderr.write(
        f"agent-friction: no friction recorded for {args.target!r}. "
        "Run `agent-friction scan` to list hotspots.\n"
    )
    return EXIT_ERROR


def _cmd_compare(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    before, after = _load_json(args.before), _load_json(args.after)
    if not 0 < args.threshold < 10:
        stderr.write("agent-friction: --threshold must be between 0 and 10\n")
        return EXIT_USAGE
    names = (before.get("repo", {}).get("name"), after.get("repo", {}).get("name"))
    if names[0] != names[1] and not args.allow_repo_mismatch:
        raise AgentFrictionError(
            f"these scans are of different repositories ({names[0]!r} vs {names[1]!r}). "
            "Pass --allow-repo-mismatch if the repository was renamed."
        )
    changes = compare(before, after, threshold=args.threshold, min_events=args.min_events)
    sessions = (int(before["totals"]["sessions"]), int(after["totals"]["sessions"]))
    low_sample = min(sessions) < args.min_sessions
    if low_sample:
        stderr.write(
            f"agent-friction: low sample ({sessions[0]} vs {sessions[1]} sessions, "
            f"--min-sessions {args.min_sessions}); treat this comparison as anecdotal"
            + (", so the regression gate is not applied" if args.fail_on_regression else "")
            + "\n"
        )
    if args.format == "json":
        payload = [c.to_dict() for c in changes]
        stdout.write(json.dumps({"changes": payload}, indent=2, sort_keys=True) + "\n")
    else:
        stdout.write(render_compare(before, after, changes, markdown=args.format == "markdown"))
    if (
        args.fail_on_regression
        and not low_sample
        and any(c.status in ("regressed", "new") for c in changes)
    ):
        return EXIT_GATE
    return EXIT_OK


def _cmd_sources(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    from agent_friction.discovery import discover_transcripts

    found = discover_transcripts(args.repo)
    if not found:
        stderr.write(
            f"agent-friction: no Claude Code transcripts found for {args.repo.resolve()}. "
            "Run this from the repository you use the agent in, set CLAUDE_CONFIG_DIR if your "
            "config lives elsewhere, or pass transcript paths to `scan --transcripts`.\n"
        )
        return EXIT_ERROR
    for path in found:
        stdout.write(f"{path}\n")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if args.command is None:
        parser.print_help(out)
        return EXIT_USAGE
    handlers = {
        "scan": _cmd_scan,
        "show": _cmd_show,
        "compare": _cmd_compare,
        "sources": _cmd_sources,
    }
    try:
        return handlers[args.command](args, out, err)
    except AgentFrictionError as exc:
        err.write(f"agent-friction: {exc}\n")
        return EXIT_ERROR
    except BrokenPipeError:  # e.g. `agent-friction scan | head`
        return EXIT_OK
    except KeyboardInterrupt:
        err.write("interrupted\n")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
