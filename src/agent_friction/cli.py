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
from pathlib import Path, PurePosixPath, PureWindowsPath
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
from agent_friction.report._common import (
    all_items,
    command_candidates,
    find_dir,
    find_target,
    plural,
)
from agent_friction.report.terminal import render_detail, render_dir_detail

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_GATE = 0, 1, 2, 3


class UsageError(AgentFrictionError):
    """A problem with the arguments rather than the data (exit code 2)."""


_REL = re.compile(r"^(\d+)\s*([hdwm])$")
_FORMAT_ALIASES = {"md": "markdown", "txt": "terminal", "text": "terminal", "htm": "html"}


def _format_name(value: str) -> str:
    """Accept the file-extension spellings too: `-f md` is `-f markdown`."""
    return _FORMAT_ALIASES.get(value.lower(), value.lower())


_EXT_FORMAT = {
    ".html": "html",
    ".htm": "html",
    ".md": "markdown",
    ".json": "json",
    ".txt": "terminal",
}


_REPO_HELP = "repo root (default: the git repository containing the current directory)"


def _absolute_path(value: str) -> str:
    # Aliases are usually recorded on another machine, so accept either OS's form.
    if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise argparse.ArgumentTypeError(f"{value!r} is not an absolute path")
    return value


def _find_repo_root(start: Path) -> Path | None:
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def _resolve_repo(args: argparse.Namespace, err: TextIO) -> None:
    """Fill in ``args.repo``: explicit path, else the enclosing git repository, else ``.``."""
    pos = getattr(args, "repo_pos", None)
    if pos is not None:
        if args.repo is not None:
            raise UsageError("give the repo either as an argument or with --repo, not both")
        args.repo = pos
    if args.repo is not None:
        if not args.repo.exists():
            raise AgentFrictionError(f"{args.repo}: directory does not exist")
        return
    cwd = Path.cwd()
    root = _find_repo_root(cwd)
    if root is not None and root != cwd and getattr(args, "command", None) != "show":
        # `show` is run again and again from wherever you are; the note is for maps.
        err.write(
            f"agent-friction: using the repository root {root} "
            "(pass . to map only this directory)\n"
        )
    elif root is None and getattr(args, "from_json", None) is None:
        err.write(
            f"agent-friction: note: {cwd} is not inside a git repository; mapping it as the "
            "repository (pass the repo path to choose another)\n"
        )
    args.repo = Path() if root is None or root == cwd else root


def _regex(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regex {value!r}: {exc}") from None
    return value


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """Parse ``30d`` / ``2w`` / ``12h`` / ``3m`` (months ≈ 30d) or an ISO date/datetime."""
    now = now or datetime.now(timezone.utc)
    m = _REL.match(value.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"h": n / 24, "d": n, "w": 7 * n, "m": 30 * n}[unit]
        try:
            return now - timedelta(days=days)
        except OverflowError:
            raise argparse.ArgumentTypeError(f"{value!r} reaches too far back") from None
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
        help="only events from WHEN on (30d, 2w, 12h, 3m = three months, or 2026-01-31; "
        "dates and times without a zone are UTC); a session that straddles it is cut",
    )
    g.add_argument(
        "--until", type=parse_when, metavar="WHEN", help="only events before WHEN (same forms, UTC)"
    )
    g.add_argument("--no-git", action="store_true", help="skip git history (human-rework signal)")
    g.add_argument(
        "--repo-alias",
        action="append",
        default=[],
        type=_absolute_path,
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
        "--agent-commit",
        action="append",
        default=[],
        type=_regex,
        metavar="REGEX",
        help="also treat commits whose author line or message matches REGEX as written by "
        "an agent (added to the built-in trailers); repeatable",
    )
    g.add_argument(
        "--bot-author",
        action="append",
        default=[],
        type=_regex,
        metavar="REGEX",
        help='treat commits whose author ("Name <email>") matches REGEX as automation, '
        "not human rework; repeatable",
    )
    g.add_argument(
        "--no-excerpts",
        action="store_true",
        help="omit transcript excerpts from evidence (safer for sharing)",
    )
    g.add_argument(
        "--allow-empty",
        action="store_true",
        help="produce a report even when no session touched the repo, or from git history "
        "alone when there are no transcripts (default: exit 1)",
    )
    g.add_argument(
        "--max-evidence",
        type=_positive_int,
        default=None,
        metavar="N",
        help="evidence items kept per hotspot (default 5; 20 for show)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-friction",
        allow_abbrev=False,
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

    scan = sub.add_parser(
        "scan", allow_abbrev=False, help="analyse agent sessions and print the friction map"
    )
    scan.add_argument("repo", nargs="?", type=Path, help=_REPO_HELP)
    _add_input_options(scan)
    out = scan.add_argument_group("output")
    out.add_argument(
        "-f",
        "--format",
        type=_format_name,
        choices=FORMATS,
        help="output format (default: terminal, or inferred from --output extension)",
    )
    out.add_argument("-o", "--output", type=Path, metavar="FILE", help="write to FILE")
    out.add_argument(
        "--top",
        type=_positive_int,
        default=10,
        metavar="N",
        help="rows per table in terminal and Markdown output (default 10); JSON and "
        "HTML always include every hotspot",
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

    show = sub.add_parser(
        "show", allow_abbrev=False, help="show the evidence behind one file, directory, or command"
    )
    show.add_argument("target", help="repo-relative file or directory, or a normalized command")
    show.add_argument("repo_pos", nargs="?", type=Path, metavar="repo", help=_REPO_HELP)
    show.add_argument("--repo", type=Path, help=argparse.SUPPRESS)
    show.add_argument(
        "--from",
        dest="from_json",
        type=Path,
        metavar="JSON",
        help="read a saved `scan -f json` result instead of scanning",
    )
    show.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    show.add_argument(
        "-f",
        "--format",
        choices=("text", "json"),
        default="text",
        help="text (default), or json: the matching hotspot and directory entries",
    )
    show.add_argument("-o", "--output", type=Path, help="write to FILE instead of stdout")
    _add_input_options(show)

    cmp_ = sub.add_parser(
        "compare", allow_abbrev=False, help="compare two saved scans: did friction go down?"
    )
    cmp_.add_argument("before", type=Path, help="earlier `scan -f json` result")
    cmp_.add_argument("after", type=Path, help="later `scan -f json` result")
    cmp_.add_argument(
        "-f",
        "--format",
        type=_format_name,
        choices=("terminal", "markdown", "json"),
        help="output format (default: terminal, or inferred from --output extension)",
    )
    cmp_.add_argument("-o", "--output", type=Path, metavar="FILE", help="write to FILE")
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
        help="below N sessions overall on either side, report but never trip the gate; "
        "a target touched in fewer than N sessions on either side is 'too little data' "
        "(default 3)",
    )
    cmp_.add_argument(
        "--allow-repo-mismatch",
        action="store_true",
        help="compare scans whose repository names differ (e.g. after a rename)",
    )
    cmp_.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    cmp_.add_argument(
        "--allow-overlap",
        action="store_true",
        help="compare scans whose time windows overlap (the same sessions on both sides "
        "dilute or hide any change)",
    )
    cmp_.add_argument(
        "--fail-on-regression", action="store_true", help="exit 3 if anything regressed or is new"
    )

    src = sub.add_parser(
        "sources", allow_abbrev=False, help="list the transcripts that would be analysed"
    )
    src.add_argument("repo", nargs="?", type=Path, help=_REPO_HELP)
    src.add_argument(
        "-t",
        "--transcripts",
        action="append",
        type=Path,
        metavar="PATH",
        help="transcript file or directory (repeatable), as for `scan`",
    )
    src.add_argument(
        "--repo-alias",
        action="append",
        default=[],
        type=_absolute_path,
        metavar="PATH",
        help="another absolute path where this repo lived, as for `scan`",
    )
    src.add_argument("--no-git", action="store_true", help=argparse.SUPPRESS)
    return parser


def _note_removed_worktrees(repo: Path, err: TextIO) -> None:
    """Sessions recorded in a vanished sibling (a removed worktree?) are not read: say so."""
    from agent_friction.discovery import removed_sibling_sessions

    try:
        found = removed_sibling_sessions(repo.absolute())
    except OSError:
        return
    for directory, count in found[:3]:
        err.write(
            f"agent-friction: note: {plural(count, 'session')} ran in {directory}, which no "
            f"longer exists (a removed worktree?); pass --repo-alias {directory} to include them\n"
        )


def _analyze(args: argparse.Namespace, repo: Path, err: TextIO | None = None) -> dict[str, Any]:
    from agent_friction.engine import AnalyzeOptions, NoTranscriptsError, analyze
    from agent_friction.gitsignals import DEFAULT_AGENT_PATTERNS

    if not repo.is_dir():
        raise AgentFrictionError(f"{repo}: not a directory (pass the repository root)")
    for given in args.transcripts or []:
        # Checked even with --allow-empty: a mistyped path in CI would otherwise pass every
        # gate with a git-only report.
        if not given.expanduser().exists():
            raise AgentFrictionError(f"transcript path does not exist: {given}")
    options = AnalyzeOptions(
        repo=repo,
        transcripts=args.transcripts,
        since=args.since,
        until=args.until,
        include_git=not args.no_git,
        include_excerpts=not args.no_excerpts,
        max_evidence=args.max_evidence if args.max_evidence is not None else 5,
        repo_aliases=tuple(args.repo_alias),
        rework_window_days=args.rework_days,
        agent_commit_patterns=(
            [*DEFAULT_AGENT_PATTERNS, *args.agent_commit] if args.agent_commit else None
        ),
        bot_commit_patterns=[f"^author: .*(?:{p})" for p in args.bot_author],
        allow_no_transcripts=args.allow_empty,
        # Every hotspot goes into the result: JSON, HTML, compare and show need the whole
        # list; --top only trims the terminal and Markdown views.
        max_hotspots=None,
    )
    try:
        result = analyze(options)
    except NoTranscriptsError as exc:
        if args.no_git:
            raise
        raise NoTranscriptsError(
            f"{exc} For a map from the git history alone, run again with --allow-empty."
        ) from None
    if err is not None and args.transcripts is None and not args.repo_alias:
        _note_removed_worktrees(repo, err)
    if int(result.get("totals", {}).get("sessions", 0)) == 0 and not args.allow_empty:
        from agent_friction.engine import session_directories

        window = " in the selected time window" if (args.since or args.until) else ""
        where = ""
        try:
            seen = session_directories(options, limit=3, mask_home=False)
        except AgentFrictionError:
            seen = []
        if seen:
            listed = ", ".join(f"{d} ({plural(n, 'session')})" for d, n in seen)
            where = (
                f" The sessions in these transcripts ran in {listed}; if one of those is this "
                f"repository on another machine, pass --repo-alias {seen[0][0]}."
            )
        raise AgentFrictionError(
            f"found transcripts, but no agent session touched {repo.resolve()}{window}, "
            "so there is nothing to analyse. Check the repo path, --since/--until, or "
            "--repo-alias (if sessions were recorded at another path)." + where + " "
            "Pass --allow-empty to produce an empty report anyway."
        )
    return result


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _row_problem(data: dict[str, Any]) -> str | None:
    """The first field compare and show rely on that is missing or has the wrong type."""
    for key in ("hotspots", "commands", "directories"):
        rows = data.get(key)
        if rows is None and key == "directories":
            continue
        if not isinstance(rows, list):
            return f"{key} is not a list"
        for i, row in enumerate(rows):
            where = f"{key}[{i}]"
            if not isinstance(row, dict):
                return f"{where} is not an object"
            if not isinstance(row.get("path"), str):
                return f"{where}.path is missing or not a string"
            if not _is_int(row.get("events")):
                return f"{where}.events is missing or not an integer"
            for field in ("sessions", "exposure_sessions", "episodes"):
                if field in row and not _is_int(row[field]):
                    return f"{where}.{field} is not an integer"
            for field in ("score", "est_minutes_lost"):
                value = row.get(field, 0)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return f"{where}.{field} is not a number"
            if not isinstance(row.get("recommendations", []), list):
                return f"{where}.recommendations is not a list"
    exposure = data.get("exposure")
    if exposure is not None:
        if not isinstance(exposure, dict):
            return "exposure is not an object"
        for kind, table in exposure.items():
            if not isinstance(table, dict) or not all(_is_int(v) for v in table.values()):
                return f"exposure.{kind} is not a map of integers"
    return None


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_dir():  # Windows reports reading a directory as "Permission denied"
        raise AgentFrictionError(f"{path}: is a directory, expected a `scan -f json` file")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise AgentFrictionError(f"{path}: no such file") from None
    except IsADirectoryError:
        raise AgentFrictionError(
            f"{path}: is a directory, expected a `scan -f json` file"
        ) from None
    except OSError as exc:
        raise AgentFrictionError(f"{path}: cannot read ({exc.strerror or exc})") from None
    # PowerShell 5's `>` writes UTF-16; accept it rather than failing on the BOM.
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    try:
        data = json.loads(raw.decode(encoding))
    except UnicodeDecodeError:
        raise AgentFrictionError(
            f"{path}: not UTF-8 or UTF-16 text; write it with `scan -f json -o FILE`"
        ) from None
    except json.JSONDecodeError as exc:
        raise AgentFrictionError(f"{path}: not valid JSON ({exc})") from None
    except RecursionError:
        raise AgentFrictionError(f"{path}: not valid JSON (nested too deeply)") from None
    if not isinstance(data, dict) or "schema_version" not in data:
        raise AgentFrictionError(
            f"{path}: not an agent-friction result (make one with `scan -f json`)"
        )
    if data["schema_version"] == 1 and not (
        isinstance(data.get("totals"), dict)
        and _is_int(data["totals"].get("sessions"))
        and all(isinstance(data.get(k), list) for k in ("hotspots", "commands"))
    ):
        raise AgentFrictionError(
            f"{path}: not an agent-friction result (make one with `scan -f json`)"
        )
    if data["schema_version"] == 1:
        problem = _row_problem(data)
        if problem:
            raise AgentFrictionError(f"{path}: not a valid scan: {problem}")
    if data["schema_version"] != 1:
        raise AgentFrictionError(
            f"{path}: schema_version {data['schema_version']} is not supported by "
            f"agent-friction {__version__}"
        )
    return data


def _write(text: str, output: Path | None, stdout: TextIO) -> None:
    if output is None:
        try:
            stdout.write(text)
            stdout.flush()
        except BrokenPipeError:
            raise
        except OSError as exc:  # e.g. a full disk behind a redirect
            raise AgentFrictionError(f"cannot write the report: {exc.strerror or exc}") from None
        return
    if not output.parent.is_dir():
        # Exit 1 like any other file that cannot be written (read-only, full disk).
        raise AgentFrictionError(f"cannot write {output}: {output.parent} does not exist")
    try:
        output.write_text(text, encoding="utf-8", errors="replace")
    except OSError as exc:  # read-only dir, full disk, file locked by another program
        raise AgentFrictionError(f"cannot write {output}: {exc.strerror or exc}") from None


def _utf8_safe(stream: TextIO) -> TextIO:
    """Make a text stream able to print the report's box-drawing glyphs.

    Redirected output on Windows defaults to cp1252, which cannot encode them. A
    file or pipe is switched to UTF-8; an interactive console that cannot show a
    glyph gets a replacement character instead of a crash.
    """
    encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
    reconfigure = getattr(stream, "reconfigure", None)
    if encoding in ("utf8", "utf8sig") or reconfigure is None:
        return stream
    try:
        if getattr(stream, "isatty", lambda: False)():
            reconfigure(errors="replace")
        else:
            reconfigure(encoding="utf-8")
    except (ValueError, OSError):  # pragma: no cover - stream already written to
        reconfigure(errors="replace")
    return stream


def _warn_unreadable(result: dict[str, Any], stderr: TextIO) -> None:
    for src in result.get("sources", []):
        if src.get("kind") == "unreadable":
            why = src.get("error", "unreadable")
            stderr.write(f"agent-friction: skipped {src.get('path', '?')}: {why}\n")


def _cmd_scan(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    if args.output is not None and str(args.output) == "-":
        args.output = None  # `-o -` means standard output, as usual
    fmt = args.format
    if args.output is not None and args.output.is_dir():
        stderr.write(f"agent-friction: {args.output} is a directory; pass a file name\n")
        return EXIT_USAGE
    if fmt is None and args.output is not None:
        fmt = _EXT_FORMAT.get(args.output.suffix.lower())
        if fmt is None:
            stderr.write(
                f"agent-friction: can't tell the format from {args.output.name!r}; "
                "use a .html, .md, .json or .txt extension, or pass --format\n"
            )
            return EXIT_USAGE
    fmt = fmt or "terminal"
    result = _analyze(args, args.repo, stderr)
    _warn_unreadable(result, stderr)
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


def _target_in_repo(target: str, repo: Path | None) -> str:
    """Turn an absolute or cwd-relative path inside the repo into a repo-relative one."""
    if repo is None:
        return target
    candidate = Path(target).expanduser()
    if not candidate.is_absolute():
        if not candidate.exists():
            return target  # a repo-relative path, a suffix, or a command
        candidate = Path.cwd() / candidate
    try:
        rel = candidate.resolve().relative_to(repo.resolve())
    except (OSError, ValueError):
        return target
    return rel.as_posix() if rel.parts else target


# Flags that choose what a scan reads; a saved scan (--from) was already made without them.
_SCAN_ONLY = (
    ("transcripts", "-t/--transcripts"),
    ("since", "--since"),
    ("until", "--until"),
    ("no_git", "--no-git"),
    ("repo_alias", "--repo-alias"),
    ("agent_commit", "--agent-commit"),
    ("bot_author", "--bot-author"),
    ("allow_empty", "--allow-empty"),
)


def _check_from_json_flags(args: argparse.Namespace) -> None:
    given = [flag for dest, flag in _SCAN_ONLY if getattr(args, dest, None)]
    if getattr(args, "rework_days", 14) != 14:
        given.append("--rework-days")
    if given:
        raise UsageError(
            f"{', '.join(given)} {'is' if len(given) == 1 else 'are'} not valid with --from: "
            "the saved scan was already made; pass them to `scan` instead"
        )


def _cmd_show(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    if not args.target.strip():
        raise UsageError("show needs a target: a file, a directory, or a command")
    if args.from_json is not None:
        _check_from_json_flags(args)
        result = _load_json(args.from_json)
        if args.no_excerpts and (result.get("settings") or {}).get("excerpts", True):
            raise UsageError(
                f"{args.from_json} was scanned with transcript text; --no-excerpts cannot remove "
                "it afterwards. Make the saved scan with `scan --no-excerpts -f json`"
            )
        if args.max_evidence is not None:
            for h in all_items(result):
                h["evidence"] = list(h.get("evidence") or [])[: args.max_evidence]
    else:
        if args.max_evidence is None:
            args.max_evidence = 20
        result = _analyze(args, args.repo, stderr)
    output = None if args.output is None or str(args.output) == "-" else args.output
    color = output is None and args.format == "text" and _use_color(args.color, stdout)
    target = _target_in_repo(args.target, getattr(args, "repo", None))
    item = find_target(result, target)
    node = find_dir(result, target)
    if item is None and node is None:
        several = command_candidates(result, target)
        if several:
            listed = ", ".join(repr(str(h.get("path"))) for h in several[:8])
            stderr.write(
                f"agent-friction: {args.target!r} ran in several directories; name one: {listed}\n"
            )
            return EXIT_USAGE
        stderr.write(
            f"agent-friction: no friction recorded for {args.target!r}. "
            "Run `agent-friction scan` to list hotspots.\n"
        )
        return EXIT_ERROR
    if args.format == "json":
        doc = {
            "schema_version": result.get("schema_version", 1),
            "target": target,
            "hotspot": item,
            "directory": node,
        }
        _write(json.dumps(doc, indent=2, sort_keys=True) + "\n", output, stdout)
        return EXIT_OK
    if item is not None and item.get("kind") != "dir":
        text = render_detail(result, item, color=color)
    else:
        # A directory: its rolled-up subtree first, then friction attributed to the
        # directory itself (for example searches that came back empty there).
        text = render_dir_detail(result, node, color=color) if node is not None else ""
        if item is not None:
            text += (
                "\nFriction attributed to the directory itself "
                "(for example searches in it that found nothing):\n\n"
            ) + render_detail(result, item, color=color)
    _write(text, output, stdout)
    return EXIT_OK


def _event_span(result: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    """The first and last event a scan saw, from its ``window``."""
    window = result.get("window") or {}

    def when(*keys: str) -> datetime | None:
        for key in keys:
            value = window.get(key)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return None

    return when("first_event", "start"), when("last_event", "end")


def _check_windows(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Refuse scans whose windows overlap or are swapped: the verdicts would be wrong."""
    (b_first, b_last), (a_first, a_last) = _event_span(before), _event_span(after)
    if None in (b_first, b_last, a_first, a_last):
        return
    assert b_first and b_last and a_first and a_last
    if a_last < b_first:
        raise AgentFrictionError(
            "the 'after' scan ends before the 'before' scan starts: the arguments look "
            "swapped. Pass the older scan first: compare BEFORE.json AFTER.json"
        )
    if a_first <= b_last:
        cut = (b_last + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        raise AgentFrictionError(
            f"the scans overlap: 'after' starts at {a_first:%Y-%m-%d %H:%M} UTC, before "
            f"'before' ends at {b_last:%Y-%m-%d %H:%M} UTC, so the same sessions are on "
            f"both sides. Re-run the later scan with --since {cut} (or both with a shared "
            "--until/--since date), or pass --allow-overlap."
        )


def _cmd_compare(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    before, after = _load_json(args.before), _load_json(args.after)
    if not 0 < args.threshold < 10:
        stderr.write("agent-friction: --threshold must be between 0 and 10\n")
        return EXIT_USAGE
    modes = (before.get("settings", {}).get("key_mode"), after.get("settings", {}).get("key_mode"))
    if modes[0] != modes[1] and None not in modes:
        raise AgentFrictionError(
            f"one scan names commands in {modes[0]!r} mode and the other in {modes[1]!r} mode "
            "(was only one made with --no-excerpts?), so their command hotspots can't be "
            "matched. Re-run both scans with the same options."
        )
    names = (before.get("repo", {}).get("name"), after.get("repo", {}).get("name"))
    if names[0] != names[1] and not args.allow_repo_mismatch:
        raise AgentFrictionError(
            f"these scans are of different repositories ({names[0]!r} vs {names[1]!r}). "
            "Pass --allow-repo-mismatch if the repository was renamed."
        )
    if not args.allow_overlap:
        _check_windows(before, after)
    roots = (before.get("repo", {}).get("root"), after.get("repo", {}).get("root"))
    if roots[0] != roots[1] and None not in roots:
        stderr.write(
            f"agent-friction: note: the scans were made from different checkouts "
            f"({roots[0]} vs {roots[1]}); paths are compared relative to each root\n"
        )
    changes = compare(
        before,
        after,
        threshold=args.threshold,
        min_events=args.min_events,
        min_exposure=args.min_sessions,
    )
    sessions = (int(before["totals"]["sessions"]), int(after["totals"]["sessions"]))
    low_sample = min(sessions) < args.min_sessions
    if low_sample:
        stderr.write(
            f"agent-friction: low sample ({sessions[0]} vs {sessions[1]} sessions, "
            f"--min-sessions {args.min_sessions}); treat this comparison as anecdotal"
            + (", so the regression gate is not applied" if args.fail_on_regression else "")
            + "\n"
        )
    output = None if args.output is None or str(args.output) == "-" else args.output
    fmt = args.format
    if fmt is None and output is not None:
        fmt = {".md": "markdown", ".json": "json"}.get(output.suffix.lower())
    if fmt == "json":
        payload = [c.to_dict() for c in changes]
        doc = {
            "schema_version": 1,
            "tool": {"name": "agent-friction", "version": __version__},
            "before": {
                "repo": before.get("repo"),
                "window": before.get("window"),
                "sessions": sessions[0],
            },
            "after": {
                "repo": after.get("repo"),
                "window": after.get("window"),
                "sessions": sessions[1],
            },
            "low_sample": low_sample,
            "changes": payload,
        }
        text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    else:
        color = output is None and fmt in (None, "terminal") and _use_color(args.color, stdout)
        text = render_compare(before, after, changes, markdown=fmt == "markdown", color=color)
    _write(text, output, stdout)
    tripped = [c for c in changes if c.status in ("regressed", "new")]
    if args.fail_on_regression and not low_sample and tripped:
        listed = ", ".join(f"{c.path} ({c.status})" for c in tripped[:5])
        more = f" and {len(tripped) - 5} more" if len(tripped) > 5 else ""
        print(
            f"agent-friction: --fail-on-regression: {len(tripped)} "
            f"target{'s' if len(tripped) != 1 else ''} regressed or new: {listed}{more}",
            file=stderr,
        )
        return EXIT_GATE
    return EXIT_OK


def _cmd_sources(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    from agent_friction.engine import AnalyzeOptions, inspect_sources

    if not args.repo.is_dir():
        raise AgentFrictionError(f"{args.repo}: not a directory (pass the repository root)")
    report = inspect_sources(
        AnalyzeOptions(
            repo=args.repo,
            transcripts=args.transcripts,
            repo_aliases=tuple(args.repo_alias),
            include_git=False,
        )
    )
    unreadable = 0
    for entry in report:
        path = entry["path"]
        events = int(entry.get("events", 0))
        skipped = int(entry.get("skipped_lines", 0))
        dupes = int(entry.get("duplicate_lines", 0))
        encoding = str(entry.get("encoding") or "")
        extra = [] if encoding in ("", "utf-8") else [f"read as {encoding}"]
        if entry.get("error") or (Path(path).is_file() and not os.access(path, os.R_OK)):
            unreadable += 1
            note = f"  (unreadable: {entry.get('error') or 'permission denied'})"
        elif events == 0 and dupes:
            note = f"  (nothing new: all {plural(dupes, 'line')} repeat an earlier file)"
        elif events == 0:
            unreadable += 1
            note = "  (no events: empty, binary, or not a transcript)"
        else:
            parts = [plural(events, "event")]
            if skipped:
                parts.append(f"{plural(skipped, 'line')} skipped")
            if dupes:
                parts.append(f"{plural(dupes, 'line')} already read from an earlier file")
            note = f"  ({', '.join(parts + extra)})"
        stdout.write(f"{path}{note}\n")
    if unreadable:
        stderr.write(f"agent-friction: {plural(unreadable, 'file')} contained nothing to analyse\n")
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None
) -> int:
    out = _utf8_safe(stdout or sys.stdout)
    err = _utf8_safe(stderr or sys.stderr)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_USAGE
    if args.command is None:
        parser.print_help(out)
        return EXIT_USAGE
    since, until = getattr(args, "since", None), getattr(args, "until", None)
    if since is not None and until is not None and since >= until:
        err.write(
            f"agent-friction: --since ({since:%Y-%m-%d %H:%M}) must be before "
            f"--until ({until:%Y-%m-%d %H:%M})\n"
        )
        return EXIT_USAGE
    handlers = {
        "scan": _cmd_scan,
        "show": _cmd_show,
        "compare": _cmd_compare,
        "sources": _cmd_sources,
    }
    try:
        if hasattr(args, "repo"):
            _resolve_repo(args, err)
        return handlers[args.command](args, out, err)
    except UsageError as exc:
        err.write(f"agent-friction: {exc}\n")
        return EXIT_USAGE
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
