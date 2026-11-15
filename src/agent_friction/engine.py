"""Public entry point: :func:`analyze`.

Pipeline: locate transcripts -> parse into normalized streams -> detect
findings (plus git rework) -> attribute to files, directories and commands ->
score -> recommend -> emit a deterministic, JSON-serializable dict whose
schema is documented in docs/schema.md.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_friction import __version__, gitsignals, model, recommend, redact, scoring, signals
from agent_friction.adapters import claude_code, parse_transcript
from agent_friction.attribution import PathResolver, public_key
from agent_friction.discovery import (
    claude_config_dirs,
    discover_repo_transcripts,
    expand_transcript_paths,
    repo_roots,
)
from agent_friction.errors import AgentFrictionError, NoTranscriptsError
from agent_friction.model import Finding, ParsedFile, Stream

__all__ = [
    "SCHEMA_VERSION",
    "AgentFrictionError",
    "AnalyzeOptions",
    "NoTranscriptsError",
    "analyze",
    "list_transcripts",
]

SCHEMA_VERSION = 1
TOOL_NAME = "agent-friction"

Item = tuple[Finding, float]


@dataclass
class AnalyzeOptions:
    """Inputs for :func:`analyze`. Only ``repo`` is required."""

    repo: Path
    #: Transcript files or directories. ``None`` auto-discovers Claude Code transcripts.
    transcripts: Sequence[Path] | None = None
    since: datetime | None = None
    until: datetime | None = None
    include_git: bool = True
    #: ``False`` removes every verbatim transcript quote from the result.
    include_excerpts: bool = True
    max_evidence: int = 5
    max_hotspots: int = 50
    #: Fixed "now" for ``generated_at`` (deterministic output in tests).
    now: datetime | None = None
    # ---- additions to the base contract ----
    #: Maximum characters per excerpt.
    excerpt_chars: int = redact.DEFAULT_EXCERPT_CHARS
    #: Other absolute paths that are this repo (e.g. where it lived when recorded).
    repo_aliases: Sequence[Path | str] = field(default_factory=tuple)
    rework_window_days: int = gitsignals.DEFAULT_REWORK_WINDOW_DAYS
    #: Regexes marking a commit as agent-authored; ``None`` uses the defaults.
    agent_commit_patterns: Sequence[str] | None = None
    #: Extra regexes (matched against ``"author: <name> <email>"``) for automation whose
    #: commits are never human rework, in addition to ``gitsignals.DEFAULT_BOT_PATTERNS``.
    bot_commit_patterns: Sequence[str] = field(default_factory=tuple)
    git_max_commits: int = gitsignals.DEFAULT_MAX_COMMITS
    #: With no transcripts at all, analyse git history alone instead of raising (needs
    #: ``include_git`` and a readable git history).
    allow_no_transcripts: bool = False


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _in_window(ts: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    if ts is None:
        return True
    return not ((since is not None and ts < since) or (until is not None and ts > until))


def _apply_window(
    stream: Stream, since: datetime | None, until: datetime | None
) -> list[model.Event]:
    """Drop events outside the window. Events without a timestamp inherit the previous one.

    Returns the events *before* the window: they are not counted, but what the
    agent did there is the context for judging the first events inside it.
    """
    if since is None and until is None:
        return []
    kept, before = [], []
    last: datetime | None = None
    for event in stream.events:
        last = event.ts or last
        if _in_window(last, since, until):
            kept.append(event)
        elif since is not None and last is not None and last < since:
            before.append(event)
    stream.events = kept
    return before


def _load(
    files: Sequence[Path], since: datetime | None, until: datetime | None
) -> tuple[list[ParsedFile], dict[str, list[model.Event]]]:
    """Parsed files cut to the window, and each stream's events from before it (by key)."""
    seen: dict[str, str] = {}
    parsed: list[ParsedFile] = []
    context: dict[str, list[model.Event]] = {}
    for path in files:
        try:
            item = parse_transcript(path, seen)
        except OSError:
            continue
        for stream in item.streams:
            before = _apply_window(stream, since, until)
            if before and stream.events:
                context[stream.key] = before
        item.streams = [s for s in item.streams if s.events]
        parsed.append(item)
    return parsed, context


#: A commit up to this long after a session's last event still belongs to that session.
COMMIT_SESSION_SLACK = timedelta(minutes=10)


def _session_spans(streams: Sequence[Stream]) -> list[tuple[datetime, datetime, str]]:
    """First and last timestamp of every transcript session, sorted by start."""
    spans: dict[str, tuple[datetime, datetime]] = {}
    for stream in streams:
        stamps = [e.ts for e in stream.events if e.ts is not None]
        if not stamps:
            continue
        low, high = min(stamps), max(stamps)
        if stream.session_id in spans:
            low = min(low, spans[stream.session_id][0])
            high = max(high, spans[stream.session_id][1])
        spans[stream.session_id] = (low, high)
    return sorted((low, high, sid) for sid, (low, high) in spans.items())


def _session_at(
    spans: Sequence[tuple[datetime, datetime, str]], when: datetime | None
) -> str | None:
    """The transcript session during which (or just after which) ``when`` falls."""
    if when is None:
        return None
    for low, high, session_id in spans:
        if low <= when <= high + COMMIT_SESSION_SLACK:
            return session_id
    return None


def _is_git(finding: Finding) -> bool:
    return finding.signal == signals.HUMAN_REWORK


def _score(items: Sequence[Item], exposed: set[str]) -> scoring.Score:
    """Score a target. Git evidence may raise a score, never lower it.

    A rework finding adds a friction "session" (the agent commit) that can pull
    the mean down, so the score from transcripts alone is the floor.
    """
    sessions = exposed | {f.session_id for f, _ in items}
    full = scoring.score_items(items, len(sessions))
    if scoring.weak_only(f for f, _ in items):
        # Nothing but signals that also accompany productive work: a lead, never an alarm.
        return dataclasses.replace(full, score=min(full.score, scoring.WEAK_ONLY_SCORE_CAP))
    transcript_items = [(f, share) for f, share in items if not _is_git(f)]
    if len(transcript_items) == len(items) or not transcript_items:
        return full
    floor = scoring.score_items(
        transcript_items, len(exposed | {f.session_id for f, _ in transcript_items})
    )
    return full if full.score >= floor.score else floor


def list_transcripts(options: AnalyzeOptions) -> list[Path]:
    """The transcript files ``analyze(options)`` would read, in the order it reads them.

    Raises ``NoTranscriptsError`` exactly when ``analyze`` would: nothing found,
    or an explicitly given file that is not a JSONL transcript.
    """
    return _Analysis(options)._transcript_files()


def inspect_transcripts(options: AnalyzeOptions) -> list[tuple[Path, int, int]]:
    """``(file, events, skipped_lines)`` for every transcript ``analyze`` would read.

    ``events`` is what the parser understood of the file (tool calls, results,
    human messages), before any filtering by repository or time window: 0
    means the file is empty, binary, or not a transcript.
    """
    seen: dict[str, str] = {}
    report: list[tuple[Path, int, int]] = []
    for path in list_transcripts(options):
        try:
            item = parse_transcript(path, seen)
        except (OSError, ValueError):
            report.append((path, 0, 0))
            continue
        report.append((path, sum(len(s.events) for s in item.streams), item.skipped_lines))
    return report


def _looks_like_jsonl(path: Path) -> bool:
    """True if one of the first lines of ``path`` is a JSON object."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(JSONL_PROBE_LINES), handle, strict=False):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    if isinstance(json.loads(line), dict):
                        return True
                except ValueError:
                    continue
    except OSError:
        return False
    return False


#: Lines of an explicitly given file that are checked for a JSON object.
JSONL_PROBE_LINES = 200


def _source_root(item: ParsedFile) -> Path:
    """Claude Code files are grouped by project directory; other files stand alone."""
    if item.kind != claude_code.KIND:
        return item.path
    if item.path.parent.name == "subagents":
        return item.path.parent.parent.parent
    return item.path.parent


class _Analysis:
    def __init__(self, options: AnalyzeOptions) -> None:
        self.options = options
        self.repo = options.repo.expanduser().absolute()
        self.since = _utc(options.since)
        self.until = _utc(options.until)
        roots = repo_roots(self.repo)
        self.roots = roots
        self.resolver = PathResolver(
            self.repo, [*roots[1:], str(options.repo), *options.repo_aliases]
        )
        self.touched: set[str] = set()
        self._is_file_cache: dict[str, bool | None] = {}
        self._facts: dict[str, recommend.FileFacts] = {}
        self.outside: dict[Path, set[str]] = {}
        self.skipped_events: dict[str, int] = {}
        self.source_labels: dict[str, str] = {}
        self.exposure: dict[tuple[str, str], set[str]] = {}
        self.git_used = False

    # ---- repository knowledge --------------------------------------------------------

    def _disk_is_file(self, rel: str) -> bool | None:
        """True/False for an existing file/directory, ``None`` if the path is absent."""
        if rel not in self._is_file_cache:
            path = self.repo / rel
            try:
                self._is_file_cache[rel] = (
                    True if path.is_file() else (False if path.is_dir() else None)
                )
            except OSError:
                self._is_file_cache[rel] = None
        return self._is_file_cache[rel]

    def first_party(self, top: str) -> bool:
        """Is ``top`` a top-level module or package of this repository?"""
        candidates = (top, f"{top}.py", f"src/{top}", f"src/{top}.py", f"lib/{top}")
        if any(self._disk_is_file(rel) is not None for rel in candidates):
            return True
        prefixes = (f"{top}/", f"src/{top}/", f"lib/{top}/")
        return any(rel.startswith(prefixes) or rel in candidates for rel in self.touched)

    def is_file(self, rel: str) -> bool:
        on_disk = self._disk_is_file(rel)
        if on_disk is not None:
            return on_disk
        return rel in self.touched or "." in rel.rsplit("/", 1)[-1].lstrip(".")

    def is_known_file(self, rel: str) -> bool:
        return rel in self.touched or self._disk_is_file(rel) is True

    def facts(self, rel: str) -> recommend.FileFacts:
        if rel not in self._facts:
            self._facts[rel] = recommend.file_facts(self.repo, rel)
        return self._facts[rel]

    # ---- text helpers ----------------------------------------------------------------

    def clean(self, text: str) -> str:
        return redact.one_line(text, 2 * self.options.excerpt_chars)

    def quote(self, text: str, limit: int) -> str:
        return redact.one_line(text, min(limit, max(self.options.excerpt_chars, 1)))

    def display_path(self, path: Path | str) -> str:
        """Home shown as ``~`` and forward slashes on every platform."""
        return redact.redact_home(str(path)).replace("\\", "/")

    def source_group(self, item: ParsedFile) -> str:
        """How a transcript source is named in the result.

        A Claude Code project directory is named after the absolute path of the
        working directory (user name, client, project), so it is never shown:
        the repository's own name stands in for it.
        """
        name = self.repo.name or "repo"
        if item.kind == claude_code.KIND:
            return f"{claude_code.KIND}:{name}"
        if not self.options.include_excerpts:
            return f"{item.kind}:{item.path.name}"
        return self.display_path(item.path)

    def source_label(self, item: ParsedFile) -> str:
        """One transcript file, as cited by evidence (with a line number next to it)."""
        if item.kind != claude_code.KIND:
            return self.source_group(item)
        try:
            inside = item.path.relative_to(_source_root(item)).as_posix()
        except ValueError:
            inside = item.path.name
        return f"{self.source_group(item)}/{inside}"

    def cited_source(self, finding: Finding) -> str | None:
        """Where a finding can be looked up, or ``None`` when paths are to be left out."""
        if finding.source not in self.source_labels:
            return finding.source  # "git"
        return self.source_labels[finding.source] if self.options.include_excerpts else None

    # ---- pipeline --------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        options = self.options
        missing: NoTranscriptsError | None = None
        try:
            files = self._transcript_files()
        except NoTranscriptsError as exc:
            if not (options.allow_no_transcripts and options.include_git):
                raise
            missing, files = exc, []
        parsed, context = _load(files, self.since, self.until)
        for item in parsed:
            # Sessions that never worked in this repo are not this repo's sessions.
            relevant = [s for s in item.streams if self._in_repo(s)]
            self.outside[item.path] = {s.session_id for s in item.streams} - {
                s.session_id for s in relevant
            }
            item.streams = relevant
        streams = [s for item in parsed for s in item.streams]
        self.source_labels = {str(item.path): self.source_label(item) for item in parsed}
        for stream in streams:
            for event in stream.events:
                if event.kind == model.TOOL_CALL and event.op in (
                    model.OP_READ,
                    model.OP_EDIT,
                    model.OP_WRITE,
                ):
                    rel = self.resolver.resolve(event.path, event.cwd)
                    if rel:
                        self.touched.add(rel)

        ctx = signals.DetectContext(
            self.resolver,
            self.is_file,
            self.is_known_file,
            # Without excerpts no project-specific name may survive in a command key.
            command_key=str if options.include_excerpts else public_key,
            first_party=self.first_party,
        )
        findings: list[Finding] = []
        exposure: dict[tuple[str, str], set[str]] = {}
        for stream in streams:
            detection = signals.detect_stream(stream, ctx)
            before = context.get(stream.key)
            if before:
                # Judge the window with what happened before it in view (the edit a
                # correction refers to, the red run an edit answers), but count and
                # expose only what lies inside it.
                first = stream.events[0].line
                whole = dataclasses.replace(stream, events=[*before, *stream.events])
                full = signals.detect_stream(whole, ctx)
                detection = dataclasses.replace(
                    detection,
                    findings=[f for f in full.findings if f.line >= first],
                    skipped_events=full.skipped_events,
                )
            findings.extend(detection.findings)
            source_key = str(stream.source)
            self.skipped_events[source_key] = (
                self.skipped_events.get(source_key, 0) + detection.skipped_events
            )
            for kind, key in detection.exposed:
                self._expose(exposure, kind, key, stream.session_id)

        sources = self._sources(parsed)
        if options.include_git:
            history = gitsignals.read_history(
                self.repo,
                options.git_max_commits,
                options.agent_commit_patterns,
                options.bot_commit_patterns,
            )
            source: dict[str, Any] = {
                "kind": "git",
                "path": self.display_path(self.repo),
                "sessions": 0,
                "skipped_lines": 0,
                "skipped_events": 0,
                "ignored_lines": 0,
                "available": history.available,
            }
            if history.available:
                rework = [
                    f
                    for f in gitsignals.find_rework(
                        history.commits,
                        options.rework_window_days,
                        gitsignals.HunkReader(self.repo),
                    )
                    if _in_window(f.ts, self.since, self.until)
                ]
                # A commit made during a transcript session is that session's work, not a
                # separate one. Agent commits never add exposure on their own.
                committed = {c.session_id: c.date for c in history.commits if c.is_agent}
                spans = _session_spans(streams)
                for finding in rework:
                    owner = _session_at(spans, committed.get(finding.session_id))
                    if owner is not None:
                        finding.detail["agent_commit_session"] = finding.session_id
                        finding.session_id = owner
                findings.extend(rework)
                source["sessions"] = sum(
                    1
                    for c in history.commits
                    if c.is_agent and _in_window(c.date, self.since, self.until)
                )
            else:
                source["note"] = history.reason
            sources.append(source)
            self.git_used = history.available
        if missing is not None and not self.git_used:
            raise missing  # neither transcripts nor git history: nothing to analyse

        findings.sort(key=_finding_order)
        for index, finding in enumerate(findings):
            finding.fid = index
        for finding in findings:  # friction implies exposure, whatever the detector saw
            for target in finding.targets:
                self._expose(exposure, target.kind, target.key, finding.session_id)
        self.exposure = exposure
        return self._emit(streams, findings, sources)

    def _expose(
        self, exposure: dict[tuple[str, str], set[str]], kind: str, key: str, session: str
    ) -> None:
        if kind == model.COMMAND:
            key = self.clean(key)
        exposure.setdefault((kind, key), set()).add(session)

    def _in_repo(self, stream: Stream) -> bool:
        resolver = self.resolver
        return any(
            resolver.contains(event.cwd)
            or resolver.resolve(event.path, event.cwd) is not None
            or (event.command is not None and signals.runs_inside(resolver, event))
            for event in stream.events
        )

    def _transcript_files(self) -> list[Path]:
        options = self.options
        if options.transcripts is not None:
            for given in options.transcripts:
                path = Path(given).expanduser()
                if not path.exists():
                    raise NoTranscriptsError(f"Transcript path does not exist: {given}")
                if path.is_file() and not _looks_like_jsonl(path):
                    raise NoTranscriptsError(
                        f"{given} is not a JSONL transcript: none of its first "
                        f"{JSONL_PROBE_LINES} lines is a JSON object. Pass Claude Code "
                        "session files (*.jsonl), directories that contain them, or files in "
                        "the generic format described in docs/schema.md."
                    )
            files = expand_transcript_paths(options.transcripts)
            if not files:
                listed = ", ".join(str(p) for p in options.transcripts) or "(none given)"
                raise NoTranscriptsError(
                    f"No .jsonl transcript files found in: {listed}. Pass transcript files "
                    "or directories that contain them."
                )
            return files
        files = discover_repo_transcripts(self.repo, self.roots)
        if not files:
            searched = ", ".join(str(c / "projects") for c in claude_config_dirs())
            raise NoTranscriptsError(
                f"No Claude Code transcripts found for {self.repo}. Looked in: {searched}. "
                "Transcripts are stored per working directory, so pass the path of the "
                "repository the agent worked in, set "
                "CLAUDE_CONFIG_DIR if your config lives elsewhere, or pass transcript files or "
                "directories explicitly. Logs "
                "from other agents can be converted to the generic JSONL format described "
                "in docs/schema.md."
            )
        return files

    def _sources(self, parsed: Sequence[ParsedFile]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        sessions: dict[tuple[str, str], set[str]] = {}
        outside: dict[tuple[str, str], set[str]] = {}
        for item in parsed:
            key = (item.kind, self.source_group(item))
            entry = grouped.setdefault(
                key,
                {
                    "kind": key[0],
                    "path": key[1],
                    "sessions": 0,
                    "skipped_lines": 0,
                    "skipped_events": 0,
                    "ignored_lines": 0,
                    "files": 0,
                    "sessions_outside_repo": 0,
                },
            )
            entry["skipped_lines"] += item.skipped_lines
            entry["skipped_events"] += self.skipped_events.get(str(item.path), 0)
            entry["ignored_lines"] += item.ignored_lines
            entry["files"] += 1
            sessions.setdefault(key, set()).update(s.session_id for s in item.streams)
            outside.setdefault(key, set()).update(self.outside.get(item.path, ()))
        for key, entry in grouped.items():
            entry["sessions"] = len(sessions[key])
            entry["sessions_outside_repo"] = len(outside[key] - sessions[key])
        return [grouped[key] for key in sorted(grouped)]

    # ---- output ----------------------------------------------------------------------

    def _emit(
        self, streams: Sequence[Stream], findings: Sequence[Finding], sources: list[dict[str, Any]]
    ) -> dict[str, Any]:
        options = self.options
        groups: dict[tuple[str, str], list[Item]] = {}
        for finding in findings:
            for target in finding.targets:
                key = target.key
                if target.kind == model.COMMAND:
                    key = self.clean(key)
                groups.setdefault((target.kind, key), []).append((finding, target.share))

        by_kind: dict[str, list[dict[str, Any]]] = {
            model.FILE: [],
            model.DIR: [],
            model.COMMAND: [],
        }
        for (kind, key), items in groups.items():
            by_kind[kind].append(self._entry(kind, key, items))
        for entries in by_kind.values():
            entries.sort(key=lambda e: (-e["score"], -e["events"], e["path"]))
        limit = max(options.max_hotspots, 0)
        shown: dict[str, list[dict[str, Any]]] = {}
        for kind, entries in by_kind.items():
            shown[kind] = entries[:limit]
            for entry in shown[kind]:
                self._decorate(entry, groups[(kind, entry["path"])])

        stamps = [e.ts for s in streams for e in s.events if e.ts is not None]
        stamps.extend(f.ts for f in findings if f.ts is not None)
        first, last = (min(stamps), max(stamps)) if stamps else (None, None)
        counts: dict[str, int] = {}
        for finding in findings:
            counts[finding.signal] = counts.get(finding.signal, 0) + 1
        now = _utc(options.now) or datetime.now(timezone.utc)
        return {
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": TOOL_NAME, "version": __version__},
            "generated_at": _iso(now),
            "repo": {"root": self.display_path(self.repo), "name": self.repo.name or "repo"},
            # What kind of scan this is; reports made with different settings do not compare.
            "settings": {
                "excerpts": options.include_excerpts,
                "git": self.git_used,
                "key_mode": "plain" if options.include_excerpts else "pseudonymized",
            },
            "window": {
                "start": _iso(self.since or first),
                "end": _iso(self.until or last),
                "since": _iso(self.since),
                "until": _iso(self.until),
                "first_event": _iso(first),
                "last_event": _iso(last),
            },
            "sources": sources,
            "totals": {
                "sessions": len({s.session_id for s in streams}),
                "tool_calls": sum(
                    1 for s in streams for e in s.events if e.kind == model.TOOL_CALL
                ),
                "friction_events": len(findings),
                "est_minutes_lost": scoring.lost_minutes(findings),
                # Corroborated corrections only; "probable" ones are listed as evidence.
                "corrections": counts.get(signals.USER_CORRECTION, 0),
                "probable_corrections": counts.get(signals.PROBABLE_CORRECTION, 0),
                "files_affected": len(by_kind[model.FILE]),
                "commands_affected": len(by_kind[model.COMMAND]),
                "directories_affected": len(by_kind[model.DIR]),
                "unattributed_events": sum(1 for f in findings if not f.targets),
            },
            "signals": [
                {
                    "id": s.id,
                    "title": s.title,
                    "description": s.description,
                    "count": counts.get(s.id, 0),
                    "severity": s.severity,
                    "why": s.why,
                    "attribution": s.attribution,
                }
                for s in signals.CATALOG
            ],
            "hotspots": shown[model.FILE],
            "commands": shown[model.COMMAND],
            "directories": shown[model.DIR],
            "exposure": {
                name: {
                    key: len(sessions)
                    for (kind, key), sessions in sorted(self.exposure.items())
                    if kind == wanted
                }
                for name, wanted in (
                    ("file", model.FILE),
                    ("command", model.COMMAND),
                    ("dir", model.DIR),
                )
            },
            "tree": self._tree(groups),
        }

    def _entry(self, kind: str, key: str, items: Sequence[Item]) -> dict[str, Any]:
        """The cheap part of a hotspot (everything needed for ranking)."""
        found = [f for f, _ in items]
        result = _score(items, self.exposure.get((kind, key), set()))
        signal_counts: dict[str, int] = {}
        for finding in found:
            signal_counts[finding.signal] = signal_counts.get(finding.signal, 0) + 1
        ranked = sorted(signal_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        sessions = len(result.sessions)
        commits = sum(s.session_id.startswith(scoring.GIT_SESSION_PREFIX) for s in result.sessions)
        level = scoring.confidence((s.session_id for s in result.sessions), signal_counts)
        return {
            "path": key,
            "kind": kind,
            "score": result.score,
            "sessions": sessions,
            "agent_commits": commits,
            "exposure_sessions": result.exposure_sessions,
            "friction_rate": result.friction_rate,
            "events": len(found),
            "est_minutes_lost": scoring.lost_minutes(found),
            "signals": dict(ranked),
            "confidence": level,
            "why": scoring.explain(
                ranked,
                sessions - commits,
                result.exposure_sessions - commits,
                level,
                commits,
                capped=scoring.weak_only(found),
            ),
            "recommendations": [],
            "evidence": [],
            "score_detail": {
                "capped": scoring.weak_only(found),
                "severity": result.severity,
                "recurrence": result.recurrence,
                "consistency": result.consistency,
                "mean_session_points": result.mean_session_points,
                "intensity": result.intensity,
                "sessions": [
                    {
                        "session_id": s.session_id,
                        "weighted_events": s.weighted_events,
                        "points": s.points,
                    }
                    for s in result.sessions
                ],
            },
        }

    def _decorate(self, entry: dict[str, Any], items: Sequence[Item]) -> None:
        """Add recommendations, evidence and file facts to a hotspot that will be shown."""
        options = self.options
        kind, key = entry["kind"], entry["path"]
        found = [f for f, _ in items]
        facts = self.facts(key) if kind == model.FILE else None
        quote = self.quote if options.include_excerpts else None
        shares = {f.fid: share for f, share in items}
        entry["recommendations"] = recommend.recommend(kind, key, found, facts, quote, shares)
        entry["evidence"] = [self._evidence(f) for f in _pick_evidence(found, options.max_evidence)]
        if facts is not None:
            entry["file"] = facts.as_dict()

    def _evidence(self, finding: Finding) -> dict[str, Any]:
        options = self.options
        text: str | None = None
        if options.include_excerpts and finding.excerpt.strip():
            text = redact.excerpt(finding.excerpt, options.excerpt_chars)
        return {
            "signal": finding.signal,
            "session_id": finding.session_id,
            "timestamp": _iso(finding.ts),
            "summary": self.clean(finding.summary),
            "excerpt": text,
            "source": self.cited_source(finding),
            "line": finding.line,
        }

    def _tree(self, groups: dict[tuple[str, str], list[Item]]) -> dict[str, Any]:
        direct: dict[str, list[Item]] = {"": []}
        children: dict[str, set[str]] = {"": set()}
        for (kind, key), items in groups.items():
            if kind == model.COMMAND:
                continue
            path = "" if key == "." else key
            direct.setdefault(path, []).extend(items)
            children.setdefault(path, set())
            while path:
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                children.setdefault(parent, set()).add(path)
                direct.setdefault(parent, [])
                path = parent
        dir_keys = {("" if k == "." else k) for (kind, k) in groups if kind == model.DIR}
        # Sessions that went anywhere beneath each directory, friction or not.
        beneath: dict[str, set[str]] = {}
        for (kind, key), sessions in self.exposure.items():
            if kind == model.COMMAND:
                continue
            path = "" if key == "." else key
            while True:
                beneath.setdefault(path, set()).update(sessions)
                if not path:
                    break
                path = path.rsplit("/", 1)[0] if "/" in path else ""

        def build(path: str) -> tuple[dict[str, Any], dict[int, Item]]:
            merged: dict[int, Item] = {}

            def absorb(items: Sequence[Item]) -> None:
                for finding, share in items:
                    previous = merged.get(finding.fid)
                    total = share + (previous[1] if previous else 0.0)
                    merged[finding.fid] = (finding, min(total, 1.0))

            absorb(direct[path])
            nodes = []
            for child in sorted(children[path]):
                node, child_items = build(child)
                nodes.append(node)
                absorb(list(child_items.values()))
            nodes.sort(key=lambda n: (-n["score"], n["path"]))
            found = [f for f, _ in merged.values()]
            is_dir = bool(nodes) or path in dir_keys or path == ""
            result = _score(list(merged.values()), beneath.get(path, set()))
            node = {
                "name": path.rsplit("/", 1)[-1] if path else (self.repo.name or "repo"),
                "path": path,
                "kind": model.DIR if is_dir else model.FILE,
                "score": result.score,
                "events": len(found),
                "sessions": result.friction_sessions,
                "exposure_sessions": result.exposure_sessions,
                "friction_rate": result.friction_rate,
                "est_minutes_lost": scoring.lost_minutes(found),
                "children": nodes,
            }
            return node, merged

        return build("")[0]


def _finding_order(finding: Finding) -> tuple[float, str, int, str]:
    stamp = finding.ts.timestamp() if finding.ts is not None else float("-inf")
    return (stamp, finding.source, finding.line, finding.signal)


def _pick_evidence(findings: Sequence[Finding], limit: int) -> list[Finding]:
    """Choose up to ``limit`` findings, favouring severe signals and distinct sessions.

    Signals are visited round-robin from most to least severe. Within a signal
    the heaviest finding from a session not shown yet goes first, newest first.
    The result is returned in chronological order.
    """
    if limit <= 0:
        return []
    queues: dict[str, list[Finding]] = {}
    for finding in findings:
        queues.setdefault(finding.signal, []).append(finding)
    order = sorted(queues, key=lambda sid: (-signals.SEVERITY[sid], sid))
    for queue in queues.values():
        queue.sort(key=lambda f: (-f.weight, [-x for x in _finding_order(f)[:1]], f.fid))
    chosen: list[Finding] = []
    seen_sessions: set[str] = set()
    while len(chosen) < limit and any(queues[sid] for sid in order):
        for sid in order:
            queue = queues[sid]
            if not queue or len(chosen) >= limit:
                continue
            index = next((i for i, f in enumerate(queue) if f.session_id not in seen_sessions), 0)
            pick = queue.pop(index)
            chosen.append(pick)
            seen_sessions.add(pick.session_id)
    return sorted(chosen, key=_finding_order)


def _sanitized(value: Any) -> Any:
    """Strip terminal control and bidi characters from every string of a result.

    Paths, command keys and messages come from transcripts; none of it may be
    able to move a cursor, retitle a terminal or reorder text in a report.
    """
    if isinstance(value, str):
        return redact.strip_controls(value)
    if isinstance(value, dict):
        return {_sanitized(k): _sanitized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitized(v) for v in value]
    return value


def analyze(options: AnalyzeOptions) -> dict[str, Any]:
    """Analyse agent transcripts (and git history) for ``options.repo``.

    Raises :class:`NoTranscriptsError` when no transcript files can be found.
    """
    result: dict[str, Any] = _sanitized(_Analysis(options).run())
    return result
