"""Locate Claude Code transcripts for a repository.

Claude Code stores one directory per working directory under
``<config>/projects/``, named after the cwd with every non-alphanumeric
character replaced by ``-``. We look for the repo itself, its subdirectories
and its git worktrees.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from itertools import islice
from pathlib import Path

from agent_friction.attribution import PathResolver

_ENCODE_RE = re.compile(r"[^A-Za-z0-9]")
#: Lines read from a transcript when checking which cwd a project dir belongs to.
CWD_PROBE_LINES = 50
GIT_TIMEOUT_SECONDS = 10


def encode_cwd(path: Path | str) -> str:
    """Claude Code's project directory name for a working directory."""
    return _ENCODE_RE.sub("-", str(path))


def claude_config_dirs(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> list[Path]:
    """Claude Code config directories, most specific first.

    ``$CLAUDE_CONFIG_DIR`` wins when set (several directories may be given,
    separated by the platform path separator). Otherwise ``~/.claude`` and the
    XDG location ``~/.config/claude`` are both checked.
    """
    environ = os.environ if env is None else env
    configured = environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return [Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]
    base = home if home is not None else Path.home()
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    return [base / ".claude", (Path(xdg) if xdg else base / ".config") / "claude"]


def git_worktrees(repo: Path) -> list[Path]:
    """Roots of all git worktrees of ``repo`` (empty when git is unavailable)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [
        Path(line[len("worktree ") :])
        for line in proc.stdout.splitlines()
        if line.startswith("worktree ") and line[len("worktree ") :].strip()
    ]


def repo_roots(repo: Path, include_worktrees: bool = True) -> list[Path]:
    """Absolute directories that count as the repo: itself, its realpath, its worktrees."""
    roots: list[Path] = []
    candidates = [repo.absolute()]
    with contextlib.suppress(OSError):
        candidates.append(repo.resolve())
    if include_worktrees:
        candidates.extend(git_worktrees(repo))
    for candidate in candidates:
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _file_cwd(path: Path) -> str | None:
    """First ``cwd`` recorded near the top of a transcript, if any."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for raw in islice(handle, CWD_PROBE_LINES):
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("cwd"), str) and obj["cwd"]:
                    return str(obj["cwd"])
    except OSError:
        pass
    return None


def _session_files(project_dir: Path) -> list[Path]:
    try:
        return sorted(p for p in project_dir.glob("*.jsonl") if p.is_file())
    except OSError:
        return []


def discover_project_dirs(
    repo: Path,
    config_dirs: Sequence[Path] | None = None,
    roots: Sequence[Path] | None = None,
) -> list[Path]:
    """Claude Code project directories that belong to ``repo``.

    A directory matches when its name is the encoding of a repo root (exact) or
    starts with it followed by ``-`` (a subdirectory). The encoding is lossy
    (``/a/b-c`` and ``/a/b/c`` collide), so a candidate is kept only if at least
    one of its transcripts records a ``cwd`` inside the repo. If no cwd can be
    read at all, only exact name matches are kept.
    """
    all_roots = list(roots) if roots is not None else repo_roots(repo)
    resolver = PathResolver(repo, all_roots[1:] if all_roots else ())
    encoded = [encode_cwd(root) for root in all_roots]
    found: list[Path] = []
    for config in config_dirs if config_dirs is not None else claude_config_dirs():
        projects = config / "projects"
        if not projects.is_dir():
            continue
        for entry in sorted(projects.iterdir()):
            if not entry.is_dir():
                continue
            exact = entry.name in encoded
            if not exact and not any(entry.name.startswith(e + "-") for e in encoded):
                continue
            cwds = [c for c in map(_file_cwd, _session_files(entry)) if c is not None]
            belongs = any(resolver.contains(c) for c in cwds) if cwds else exact
            if belongs and entry not in found:
                found.append(entry)
    return found


def expand_transcript_paths(paths: Iterable[Path]) -> list[Path]:
    """Expand files and directories into a sorted, de-duplicated list of ``.jsonl`` files."""
    files: set[Path] = set()
    for path in paths:
        path = Path(path).expanduser()
        if path.is_dir():
            files.update(p for p in path.rglob("*.jsonl") if p.is_file())
        elif path.is_file():
            files.add(path)
    return sorted(files)


def discover_repo_transcripts(repo: Path, roots: Sequence[Path] | None = None) -> list[Path]:
    """Transcripts for ``repo`` from the Claude Code config directories.

    Within a matching project directory, a session whose recorded ``cwd`` lies
    outside the repo (possible when two paths encode to the same name) is left
    out together with its subagent transcripts.
    """
    all_roots = list(roots) if roots is not None else repo_roots(repo)
    resolver = PathResolver(repo, all_roots[1:] if all_roots else ())
    files: list[Path] = []
    for directory in discover_project_dirs(repo, roots=all_roots):
        for session in _session_files(directory):
            cwd = _file_cwd(session)
            if cwd is not None and not resolver.contains(cwd):
                continue
            files.append(session)
            files.extend(expand_transcript_paths([directory / session.stem]))
    return sorted(set(files))


def discover_transcripts(repo: Path) -> list[Path]:
    """All Claude Code transcript files (sessions and subagents) for ``repo``, sorted."""
    return discover_repo_transcripts(repo)
