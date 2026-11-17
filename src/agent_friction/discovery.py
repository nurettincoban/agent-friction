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
from agent_friction.model import load_json_line, open_transcript

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

    ``$CLAUDE_CONFIG_DIR`` (one directory) wins when set. Otherwise ``~/.claude``
    and the XDG location ``~/.config/claude`` are both checked.
    """
    environ = os.environ if env is None else env
    configured = environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return [Path(configured).expanduser()]
    base = home if home is not None else Path.home()
    xdg = environ.get("XDG_CONFIG_HOME", "").strip()
    return [base / ".claude", (Path(xdg) if xdg else base / ".config") / "claude"]


def _git(repo: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_worktrees(repo: Path) -> list[Path]:
    """Where ``repo`` lives in each git worktree (empty when git is unavailable).

    For a repository root these are the worktree roots, including entries git
    calls prunable (the directory is gone, the record is not): their sessions
    are still this repository's.

    When ``repo`` is a directory *inside* a git repository (one package of a
    monorepo), the worktrees are those of the enclosing repository, and only
    the same directory inside each of them is ``repo``: ``<worktree>/<prefix>``,
    where it exists. The worktree roots themselves are never returned then, or
    the whole enclosing repository would be read as this directory.
    """
    listing = _git(repo, "worktree", "list", "--porcelain")
    shown = _git(repo, "rev-parse", "--show-prefix")
    if listing is None or shown is None:
        return []
    prefix = shown.strip().strip("/")
    found: list[Path] = []
    for line in listing.splitlines():
        if not line.startswith("worktree ") or not line[len("worktree ") :].strip():
            continue
        root = Path(line[len("worktree ") :])
        if not prefix:
            found.append(root)
            continue
        with contextlib.suppress(OSError):
            if (root / prefix).is_dir():
                found.append(root / prefix)
    return found


def repo_name(repo: Path) -> str:
    """What the repository at ``repo`` is called.

    The directory's name, except in a linked git worktree: `shop-fix-123/` is the
    repository `shop`, and a scan from there must name it as a scan of the main
    worktree does, or two results for one repository would look like two.
    """
    name = repo.name or "repo"
    listing = _git(repo, "worktree", "list", "--porcelain")
    shown = _git(repo, "rev-parse", "--show-prefix")
    if not listing or shown is None or shown.strip().strip("/"):
        return name  # not git, or a directory inside a repository: its own name
    first = listing.split("\n\n", 1)[0].splitlines()
    main = next((line[len("worktree ") :].strip() for line in first if line[:9] == "worktree "), "")
    if not main:
        return name
    with contextlib.suppress(OSError):
        if Path(main).resolve() == repo.resolve():
            return name
    found = Path(main).name
    if "bare" in first and found.endswith(".git"):
        found = found[: -len(".git")]
    return found or name


def removed_sibling_sessions(
    repo: Path, config_dirs: Sequence[Path] | None = None
) -> list[tuple[str, int]]:
    """Session files recorded in a vanished directory beside ``repo`` named after it.

    ``(directory, session files)`` for Claude Code project directories whose
    sessions ran in ``<parent of repo>/<repo name><-._ anything>`` when that
    directory no longer exists: most likely a git worktree that was removed
    (``git worktree remove`` leaves no trace to ask git about). Such sessions
    are not read by a scan; passing the directory as a repo alias includes
    them. A guess from names alone: a deleted unrelated checkout called
    ``<repo>-old`` looks the same.
    """
    root = repo.absolute()
    encoded = encode_cwd(root)
    resolver = PathResolver(repo, repo_roots(repo)[1:])
    found: dict[str, int] = {}
    for config in config_dirs if config_dirs is not None else claude_config_dirs():
        projects = config / "projects"
        try:
            entries = sorted(e for e in projects.iterdir() if e.is_dir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(encoded + "-"):
                continue
            for session in _session_files(entry):
                cwd = _file_cwd(session)
                if cwd is None or resolver.contains(cwd):
                    continue  # a worktree git still knows is read like the repository
                here = Path(cwd)
                beside = here.parent == root.parent and re.match(
                    re.escape(root.name) + r"[-._]", here.name
                )
                with contextlib.suppress(OSError):
                    if beside and not here.exists():
                        found[cwd] = found.get(cwd, 0) + 1
    return sorted(found.items(), key=lambda pair: (-pair[1], pair[0]))


def enclosing_repositories(repo: Path) -> list[Path]:
    """The git repository ``repo`` is a subdirectory of, as each of its worktrees.

    Empty when ``repo`` is the repository's root, is in no repository, or git is
    unavailable: there is then nothing "elsewhere in the same repository".
    """
    shown = _git(repo, "rev-parse", "--show-prefix")
    top = _git(repo, "rev-parse", "--show-toplevel")
    if shown is None or top is None or not shown.strip().strip("/") or not top.strip():
        return []
    found = [Path(top.strip())]
    for line in (_git(repo, "worktree", "list", "--porcelain") or "").splitlines():
        root = line[len("worktree ") :].strip() if line.startswith("worktree ") else ""
        if root and Path(root) not in found:
            found.append(Path(root))
    return found


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
        with open_transcript(path) as handle:
            for raw in islice(handle, CWD_PROBE_LINES):
                try:
                    obj = load_json_line(raw)
                except (ValueError, RecursionError):
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


def _ancestors(roots: Sequence[Path]) -> list[Path]:
    seen: list[Path] = []
    for root in roots:
        for parent in root.parents:
            if parent not in seen and parent not in roots:
                seen.append(parent)
    return seen


def discover_project_dirs(
    repo: Path,
    config_dirs: Sequence[Path] | None = None,
    roots: Sequence[Path] | None = None,
    include_ancestors: bool = True,
) -> list[Path]:
    """Claude Code project directories that may hold sessions about ``repo``.

    A directory matches when its name is the encoding of a repo root (exact) or
    starts with it followed by ``-`` (a subdirectory). The encoding is lossy
    (``/a/b-c`` and ``/a/b/c`` collide), so a candidate is kept only if at least
    one of its transcripts records a ``cwd`` inside the repo. If no cwd can be
    read at all, only exact name matches are kept.

    With ``include_ancestors`` the directories of the repo's parent directories
    are returned as well: starting the agent in ``~`` or a monorepo root and
    working in the repo from there is common. Which of *their* sessions are
    relevant is decided per file by :func:`discover_repo_transcripts`.
    """
    all_roots = list(roots) if roots is not None else repo_roots(repo)
    resolver = PathResolver(repo, all_roots[1:] if all_roots else ())
    encoded = [encode_cwd(root) for root in all_roots]
    above = {encode_cwd(parent) for parent in _ancestors(all_roots)} if include_ancestors else set()
    found: list[Path] = []
    for config in config_dirs if config_dirs is not None else claude_config_dirs():
        projects = config / "projects"
        if not projects.is_dir():
            continue
        for entry in sorted(projects.iterdir()):
            if not entry.is_dir() or entry in found:
                continue
            if entry.name in above:
                found.append(entry)
                continue
            exact = entry.name in encoded
            if not exact and not any(entry.name.startswith(e + "-") for e in encoded):
                # ... or the repository under another name: sessions started through a
                # symbolic link are filed under the link's spelling. One transcript
                # says which directory that was; the file system says where it leads.
                first = next(iter(_session_files(entry)), None)
                if first is not None and resolver.adopt(_file_cwd(first)):
                    found.append(entry)
                continue
            cwds = [c for c in map(_file_cwd, _session_files(entry)) if c is not None]
            if any(resolver.contains(c) for c in cwds) if cwds else exact:
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


def _mentions(path: Path, needles: Sequence[bytes]) -> bool:
    """True if the file contains any of the byte strings (a cheap relevance pre-filter)."""
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return any(needle in data for needle in needles)


def _needles(roots: Sequence[Path]) -> list[bytes]:
    """Repo roots as they appear inside JSON transcripts (plain and backslash-escaped)."""
    needles: list[bytes] = []
    for root in roots:
        for text in (str(root), json.dumps(str(root))[1:-1]):
            encoded = text.encode("utf-8", "replace")
            if encoded not in needles:
                needles.append(encoded)
    return needles


def discover_repo_transcripts(repo: Path, roots: Sequence[Path] | None = None) -> list[Path]:
    """Transcripts for ``repo`` from the Claude Code config directories.

    Each session is judged by the ``cwd`` recorded at its top:

    * inside the repo (or a worktree): included;
    * a parent directory of the repo: included only if the session, or one of
      its subagents, mentions the repo's path somewhere (the engine then keeps
      just the events that resolve into the repo);
    * anywhere else (possible when two paths encode to the same directory
      name): left out together with its subagent transcripts.
    """
    all_roots = list(roots) if roots is not None else repo_roots(repo)
    resolver = PathResolver(repo, all_roots[1:] if all_roots else ())
    above = PathResolver.posix_set(_ancestors(all_roots))
    needles = _needles(all_roots)
    files: list[Path] = []
    for directory in discover_project_dirs(repo, roots=all_roots):
        for session in _session_files(directory):
            group = [session, *expand_transcript_paths([directory / session.stem])]
            cwd = _file_cwd(session)
            inside = cwd is None or resolver.adopt(cwd)
            if inside or (
                PathResolver.normalize(cwd or "") in above
                and any(_mentions(f, needles) for f in group)
            ):
                files.extend(group)
    return sorted(set(files))


def discover_transcripts(repo: Path) -> list[Path]:
    """All Claude Code transcript files (sessions and subagents) for ``repo``, sorted."""
    return discover_repo_transcripts(repo)
