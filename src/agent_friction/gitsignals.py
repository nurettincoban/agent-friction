"""Human-rework detection from git history.

A file counts as reworked when a commit attributed to a coding agent changed
it and the *next* commit touching that file is a human one made within the
rework window. Only the current branch's history is read.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from agent_friction import model
from agent_friction.adapters.common import parse_ts
from agent_friction.model import Finding, Target
from agent_friction.signals import HUMAN_REWORK

DEFAULT_REWORK_WINDOW_DAYS = 14
#: A human commit whose subject announces new work ("feat: ...", "add ...", "implement
#: ..."), and how soon after the agent's commit it must come to count as rework at all.
FEATURE_SUBJECT_RE = re.compile(
    r"^\s*(?:feat(?:ure)?(?:\([^)]*\))?!?:|(?:add|adds|added|adding|implement\w*|introduc\w+"
    r"|support|new)\b)",
    re.I,
)
FEATURE_REWORK_WINDOW = timedelta(days=2)
DEFAULT_MAX_COMMITS = 5000
GIT_TIMEOUT_SECONDS = 60

_AGENTS = r"claude|anthropic|copilot|codex|cursor|aider|devin|openhands|gemini|jules|windsurf"
#: Regexes (case-insensitive, multiline) matched against
#: ``"author: <name> <email>\n<full commit message>"``. One match marks an agent commit.
DEFAULT_AGENT_PATTERNS: tuple[str, ...] = (
    rf"^co-authored-by:.*\b(?:{_AGENTS})\b",
    r"generated with \[?claude code",
    r"^aider: ",
    r"^author: .*\(aider\)",
    rf"^author: .*\b(?:{_AGENTS})\b.*(?:\[bot\]|noreply@anthropic\.com|agent@)",
)

#: Automation that commits on its own (formatters, dependency updates). Matched like the
#: agent patterns. Such a commit is neither the agent's work nor a human's reaction to it.
DEFAULT_BOT_PATTERNS: tuple[str, ...] = (
    r"^author: .*\[bot\]",
    r"^author: (?:dependabot|renovate|pre-commit-ci|github-actions|mergify|semantic-release"
    r"|allcontributors|snyk|greenkeeper|imgbot|restyled|deepsource|sourcery|codecov)\b",
    r"^author: .*<(?:bot@renovateapp\.com|action@github\.com|actions@github\.com"
    r"|[^>]*\bbot@[^>]*|[^>]*@bots?\.[^>]*)>",
)

_RS = "\x1e"
_FS = "\x1f"
_FORMAT = f"{_RS}%H{_FS}%aI{_FS}%an{_FS}%ae{_FS}%B{_FS}"


@dataclass
class Commit:
    sha: str
    date: datetime
    author: str
    message: str
    is_agent: bool
    #: Committed by automation that is not a coding agent (CI formatter, dependency bot).
    is_bot: bool = False
    files: dict[str, tuple[int, int]] = field(default_factory=dict)  # path -> (added, deleted)

    @property
    def subject(self) -> str:
        return self.message.strip().split("\n", 1)[0]

    @property
    def session_id(self) -> str:
        """Agent commits stand in for sessions in git-based evidence."""
        return f"git:{self.sha[:12]}"


@dataclass
class GitHistory:
    available: bool
    reason: str = ""
    commits: list[Commit] = field(default_factory=list)  # oldest first

    @property
    def agent_commits(self) -> int:
        return sum(1 for c in self.commits if c.is_agent)


def compile_agent_patterns(patterns: Sequence[str] | None = None) -> list[re.Pattern[str]]:
    source = DEFAULT_AGENT_PATTERNS if patterns is None else patterns
    return [re.compile(p, re.I | re.M) for p in source]


def is_agent_commit(author: str, message: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    text = f"author: {author}\n{message}"
    return any(p.search(text) for p in patterns)


def compile_bot_patterns(extra: Sequence[str] = ()) -> list[re.Pattern[str]]:
    return [re.compile(p, re.I | re.M) for p in (*DEFAULT_BOT_PATTERNS, *extra)]


def parse_log(
    output: str,
    patterns: Sequence[re.Pattern[str]],
    bot_patterns: Sequence[re.Pattern[str]] | None = None,
) -> list[Commit]:
    """Parse ``git log --numstat`` output produced with our record/field separators."""
    bots = compile_bot_patterns() if bot_patterns is None else bot_patterns
    commits: list[Commit] = []
    for record in output.split(_RS)[1:]:
        fields = record.split(_FS)
        if len(fields) < 6:
            continue
        sha, date_raw, name, email, message = fields[:5]
        date = parse_ts(date_raw.strip())
        if date is None or not sha.strip():
            continue
        author = f"{name} <{email}>"
        commit = Commit(
            sha=sha.strip(),
            date=date,
            author=author,
            message=message,
            is_agent=is_agent_commit(author, message, patterns),
        )
        commit.is_bot = not commit.is_agent and any(p.search(f"author: {author}") for p in bots)
        for line in _FS.join(fields[5:]).splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not parts[2]:
                continue
            added = int(parts[0]) if parts[0].isdigit() else 0
            deleted = int(parts[1]) if parts[1].isdigit() else 0
            commit.files[parts[2]] = (added, deleted)
        commits.append(commit)
    commits.reverse()  # git prints newest first
    commits.sort(key=lambda c: c.date)  # stable: keeps topological order on ties
    return commits


def read_history(
    repo: Path,
    max_commits: int = DEFAULT_MAX_COMMITS,
    agent_patterns: Sequence[str] | None = None,
    bot_patterns: Sequence[str] = (),
) -> GitHistory:
    """Read commit history for ``repo``. Never raises: failures set ``available=False``."""
    command = [
        "git",
        "-C",
        str(repo),
        "-c",
        "core.quotePath=false",
        "log",
        "--no-merges",
        "--no-renames",
        "--numstat",
        "--relative",
        f"--max-count={max_commits}",
        f"--format={_FORMAT}",
    ]
    env = {**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except FileNotFoundError:
        return GitHistory(False, "git executable not found")
    except (OSError, subprocess.SubprocessError) as exc:
        return GitHistory(False, f"git log failed: {type(exc).__name__}")
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        return GitHistory(False, lines[-1] if lines else "git log failed")
    commits = parse_log(
        proc.stdout, compile_agent_patterns(agent_patterns), compile_bot_patterns(bot_patterns)
    )
    return GitHistory(True, "", commits)


def ignored_directories(repo: Path, directories: Sequence[str]) -> set[str]:
    """Those of the repo-relative ``directories`` that ``.gitignore`` rules match.

    One ``git check-ignore`` call for all of them. Never raises: without git, or
    outside a repository, nothing is ignored.
    """
    wanted = sorted({d.strip("/") for d in directories if d.strip("/")})
    if not wanted:
        return set()
    command = ["git", "-C", str(repo), "check-ignore", "--stdin", "-z"]
    env = {**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = subprocess.run(
            command,
            input="".join(f"{d}/\0" for d in wanted),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return set()
    if proc.returncode not in (0, 1):
        return set()
    return {d.strip("/") for d in proc.stdout.split("\0") if d.strip("/")}


def removal(repo: Path, rel: str) -> tuple[str | None, str | None]:
    """When ``rel`` left the tree, and what it was renamed to: ``(date, new path)``.

    The date is that of the last commit that touched ``rel`` (ISO 8601), if that
    commit deleted or renamed it. ``(None, None)`` where git knows nothing of the
    kind: no git, not a repository, a path that was never tracked, a file that is
    only missing from the working tree. Never raises.
    """

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "-c", "core.quotepath=off", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
                env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    last = (git("log", "-1", "--format=%H%x09%cI", "--", rel) or "").strip()
    sha, _, date = last.partition("\t")
    if not sha or not date:
        return None, None
    if date.endswith("Z"):  # newer git writes UTC as "Z"; keep one form across versions
        date = date[:-1] + "+00:00"
    shown = git("show", "-M", "--relative", "--name-status", "--format=", sha) or ""
    for line in shown.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[0].startswith("R") and fields[1] == rel:
            return date, fields[2]
        if len(fields) == 2 and fields[0] == "D" and fields[1] == rel:
            return date, None
    return None, None


def is_untracked(repo: Path, rel: str) -> bool:
    """True if git knows ``repo`` and does not track ``rel`` (ignored, or never added).

    False for a tracked path, and also where nothing can be said: no git, not a
    repository. Never raises.
    """
    command = ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", rel]
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return proc.returncode == 1 and "did not match any file" in proc.stderr


Range = tuple[int, int]  # half-open line range [start, end)

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
#: Subjects that say the commit repairs something. Checked only on a human commit that
#: directly follows an agent commit on the same file.
FIX_SUBJECT_RE = re.compile(
    r"^(?:revert|fix(?:up)?!?|hotfix|bugfix|undo|rollback|roll back|repair|correct)\b"
    r"|\b(?:reverts?|reverted|fix(?:es|ed)?|undo|regression|broke|broken|oops|mistake)\b",
    re.I,
)
#: Commits whose patches are read to compare line ranges, at most (newest first).
MAX_PATCH_COMMITS = 400


def parse_hunks(patch: str) -> dict[str, tuple[list[Range], list[Range]]]:
    """Per file: the old-side and new-side line ranges of every hunk of a ``-U0`` patch.

    A pure insertion has an empty old range ``(n, n)`` placed *after* line n; a
    pure deletion has an empty new range.
    """
    files: dict[str, tuple[list[Range], list[Range]]] = {}
    current: tuple[list[Range], list[Range]] | None = None
    old_name = ""
    for line in patch.splitlines():
        if line.startswith("--- "):
            old_name = line[4:].strip()
        elif line.startswith("+++ "):
            name = line[4:].strip()
            name = old_name if name == "/dev/null" else name
            name = name[2:] if name[:2] in ("a/", "b/") else name
            current = files.setdefault(name, ([], []))
        elif current is not None and line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if match:
                old_start, new_start = int(match.group(1)), int(match.group(3))
                old_len = int(match.group(2)) if match.group(2) is not None else 1
                new_len = int(match.group(4)) if match.group(4) is not None else 1
                current[0].append((old_start, old_start + old_len))
                current[1].append((new_start, new_start + new_len))
        elif line.startswith("diff --git "):
            current = None
    return files


#: A line that states a version and nothing else: ``"version": "1.4.0",`` in package.json,
#: ``version = "1.4.0"`` in pyproject.toml or Cargo.toml, ``__version__ = "1.4.0"``.
VERSION_LINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const\s+|let\s+|var\s+)?[\"']?(?:__version__|version|VERSION"
    r"|appVersion|versionName|versionCode|[A-Z_]*VERSION)[\"']?\s*(?::\s*\w+\s*)?[:=]\s*"
    r"[\"']?v?\d+(?:\.\d+)*[\w.+-]*[\"']?\s*[,;]?\s*$"
)


def version_only_files(patch: str) -> frozenset[str]:
    """Files of a patch in which nothing changed but a version string.

    Every removed and every added line of the file states a version
    (``VERSION_LINE_RE``), and as many were added as removed.
    """
    lines: dict[str, list[str]] = {}
    current: list[str] | None = None
    old_name = ""
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("--- "):
            old_name = line[4:].strip()
        elif line.startswith("+++ "):
            name = line[4:].strip()
            name = old_name if name == "/dev/null" else name
            current = lines.setdefault(name[2:] if name[:2] in ("a/", "b/") else name, [])
        elif current is not None and line[:1] in ("+", "-"):
            current.append(line)
    return frozenset(
        name
        for name, changed in lines.items()
        if changed
        and all(VERSION_LINE_RE.match(line[1:]) for line in changed)
        and sum(line[0] == "+" for line in changed) * 2 == len(changed)
    )


#: A version or a commit hash inside a line: ``0.15.2``, ``v1.4.0-rc1``, ``9f3c2ab``.
_PIN_TOKEN_RE = re.compile(
    r"\bv?\d+(?:\.\d+)+[\w.+-]*|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b"
    r"|\bsha(?:256|512)[:-][0-9A-Za-z+/=]{20,}"
)


def pin_only_hunks(patch: str) -> dict[str, tuple[list[Range], list[Range]]]:
    """Per file: the hunks of a ``-U0`` patch that change nothing but a version or a hash.

    Such a hunk removes as many lines as it adds, and each removed line equals the
    added one in its place once versions (``1.4.0``) and hashes (7 to 40 hex
    digits) are blanked: ``rev: 9f3c2ab`` -> ``rev: 51d07e1``, ``ruff==0.15.1`` ->
    ``ruff==0.15.2``. The same shape as ``parse_hunks``.
    """
    files: dict[str, tuple[list[Range], list[Range]]] = {}
    current: tuple[list[Range], list[Range]] | None = None
    old_name = ""
    header: tuple[Range, Range] | None = None
    removed: list[str] = []
    added: list[str] = []

    def close() -> None:
        if current is not None and header is not None and removed and len(removed) == len(added):
            blank = [
                (_PIN_TOKEN_RE.sub("\0", a), _PIN_TOKEN_RE.sub("\0", b))
                for a, b in zip(removed, added, strict=True)
            ]
            if all(a == b and "\0" in a for a, b in blank):
                current[0].append(header[0])
                current[1].append(header[1])

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            close()
            current, header = None, None
        elif line.startswith("--- ") and header is None:
            old_name = line[4:].strip()
        elif line.startswith("+++ ") and header is None:
            name = line[4:].strip()
            name = old_name if name == "/dev/null" else name
            current = files.setdefault(name[2:] if name[:2] in ("a/", "b/") else name, ([], []))
        elif current is not None and line.startswith("@@"):
            close()
            match = _HUNK_RE.match(line)
            header = None
            if match:
                old_start, new_start = int(match.group(1)), int(match.group(3))
                old_len = int(match.group(2)) if match.group(2) is not None else 1
                new_len = int(match.group(4)) if match.group(4) is not None else 1
                header = ((old_start, old_start + old_len), (new_start, new_start + new_len))
            removed, added = [], []
        elif header is not None and line.startswith("-"):
            removed.append(line[1:])
        elif header is not None and line.startswith("+"):
            added.append(line[1:])
    close()
    return {name: ranges for name, ranges in files.items() if ranges[0]}


def _overlaps(a: Range, b: Range) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def without_pin_bumps(
    agent_new: Sequence[Range],
    human_old: Sequence[Range],
    agent_pins: Sequence[Range],
    human_pins: Sequence[Range],
) -> list[Range]:
    """``human_old`` without the hunks that only move a pin the agent had only moved.

    A human hunk is left out when it changes nothing but a version or hash and
    every line of the agent's that it touches was itself such a change: the next
    bump of a pin is the pin being bumped again, not the agent's work being redone.
    """
    kept: list[Range] = []
    for old in human_old:
        touched = [new for new in agent_new if _overlaps(new, old)]
        routine = old in human_pins and touched and all(new in agent_pins for new in touched)
        if not routine:
            kept.append(old)
    return kept


class HunkReader:
    """Reads and caches the changed line ranges of single commits (``git show -U0``)."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls = 0
        self._cache: dict[str, dict[str, tuple[list[Range], list[Range]]]] = {}
        self._bumps: dict[str, frozenset[str]] = {}
        self._pins: dict[str, dict[str, tuple[list[Range], list[Range]]]] = {}

    def pins(self, sha: str) -> dict[str, tuple[list[Range], list[Range]]]:
        """Hunks of ``sha`` that change only a version or a hash (``pin_only_hunks``)."""
        self(sha)
        return self._pins.get(sha, {})

    def version_only(self, sha: str) -> frozenset[str]:
        """Files of ``sha`` in which only a version string changed (see above)."""
        self(sha)
        return self._bumps.get(sha, frozenset())

    def __call__(self, sha: str) -> dict[str, tuple[list[Range], list[Range]]]:
        if sha not in self._cache:
            self._cache[sha] = self._read(sha) if self.calls < MAX_PATCH_COMMITS else {}
        return self._cache[sha]

    def _read(self, sha: str) -> dict[str, tuple[list[Range], list[Range]]]:
        self.calls += 1
        command = ["git", "-C", str(self.repo), "-c", "core.quotePath=false", "show"]
        command += ["--unified=0", "--no-renames", "--relative", "--format=", "--no-color"]
        command += ["--no-ext-diff", sha]
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
                env={**os.environ, "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"},
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if proc.returncode != 0:
            return {}
        self._bumps[sha] = version_only_files(proc.stdout)
        self._pins[sha] = pin_only_hunks(proc.stdout)
        return parse_hunks(proc.stdout)


Hunks = Callable[[str], dict[str, tuple[list[Range], list[Range]]]]


def reworked_lines(agent_new: Sequence[Range], human_old: Sequence[Range]) -> int:
    """How many human hunks changed, deleted or split lines the agent commit had written.

    Valid because the human commit is the next one to touch the file: its
    "old" side is exactly the agent's "new" side. Appending after the agent's
    last line, or editing elsewhere in the file, does not count.
    """
    hits = 0
    for old_start, old_end in human_old:
        for new_start, new_end in agent_new:
            if new_end <= new_start:
                continue  # the agent only deleted here
            if old_end > old_start:  # human changed/deleted lines
                overlap = old_start < new_end and new_start < old_end
            else:  # human inserted after line old_start: inside the agent's block?
                overlap = new_start <= old_start < new_end - 1
            if overlap:
                hits += 1
                break
    return hits


#: A human commit touching more files than this may be a repo-wide sweep.
SWEEP_MIN_FILES = 20
SWEEP_SUBJECT_RE = re.compile(
    r"\b(?:re-?format\w*|format(?:ting|ted)?|prettier|black|ruff format|gofmt|rustfmt|isort"
    r"|lint(?:ing)?|style|whitespace|line endings|eol|license headers?|copyright|codemod"
    r"|rename|bump|mass|bulk|sweep)\b",
    re.I,
)


#: Subjects of routine upkeep: a version bump, a release, a pin or vendored copy being
#: moved along. Such a commit follows the previous bump, whoever made that one, and says
#: nothing about its quality: "Bump 0.15.2", "Release 1.4.0", "Update conformance suite
#: commit hash", "chore(deps): bump serde", "Pin ecosystem-analyzer", "v2.1.0".
_CONVENTIONAL = r"(?:\[[^\]]*\]\s*)?(?:(?:chore|build|ci|deps?|release|meta)(?:\([^)]*\))?!?:\s*)?"
ROUTINE_SUBJECT_RE = re.compile(
    r"^\s*" + _CONVENTIONAL + r"(?:"
    r"(?:bump|bumps|bumped|bumping|pin|pins|pinned|unpin|vendor|re-?vendor|release|releasing"
    r"|prepare (?:for )?(?:the )?release|cut (?:a |the )?release)\b"
    r"|(?:update|updates|updated|upgrade|upgrades|upgraded|sync|refresh|roll|advance|move)\b"
    r".{0,80}\b(?:pins?|hash(?:es)?|references?|refs?|versions?|shas?|revisions?|revs?"
    r"|lock ?files?|submodules?|changelog|vendored)\b"
    r"|v?\d+\.\d+(?:\.\d+)?[\w.+-]*\s*$"
    r")"
    r"|^\s*(?:chore|build)\((?:release|deps(?:-dev)?)\)!?:",
    re.I,
)


def is_routine(commit: Commit) -> bool:
    """A human commit whose subject says it is upkeep (see ``ROUTINE_SUBJECT_RE``).

    Not when the subject also says it fixes or reverts something: "Bump serde to
    fix the build the last bump broke" is a repair.
    """
    if commit.is_agent or FIX_SUBJECT_RE.search(commit.subject):
        return False
    return ROUTINE_SUBJECT_RE.search(commit.subject) is not None


#: Subjects that say a tool rewrote the files: a formatter or linter run, at any size.
MECHANICAL_SUBJECT_RE = re.compile(
    r"^\s*(?:style|format|fmt|lint)(?:\([^)]*\))?!?:"
    r"|\b(?:run|ran|apply|applied|via|with) (?:black|prettier|ruff(?: format)?|gofmt|rustfmt"
    r"|isort|eslint --fix|clang-format|autopep8|yapf|biome|dprint)\b"
    r"|\b(?:re-?format\w*|auto-?format\w*|gofmt|rustfmt|ruff format|clang-format)\b"
    r"|^\s*(?:black|prettier|formatting|lint(?:ing)?|whitespace)\s*$",
    re.I,
)


def is_sweep(commit: Commit) -> bool:
    """A human commit that reformats or mechanically rewrites the whole repo.

    More than ``SWEEP_MIN_FILES`` files, not announced as a fix, and either
    named as a mechanical change or replacing about as many lines as it adds
    in total (within 20 %). Such a commit touches the agent's lines like
    everybody else's and says nothing about them.
    """
    if commit.is_agent:
        return False
    if MECHANICAL_SUBJECT_RE.search(commit.subject):
        return True  # `style: run black` over five files is a formatter all the same
    if len(commit.files) <= SWEEP_MIN_FILES:
        return False
    if FIX_SUBJECT_RE.search(commit.subject):
        return False
    added = sum(a for a, _ in commit.files.values())
    deleted = sum(d for _, d in commit.files.values())
    balanced = min(added, deleted) > 0 and min(added, deleted) / max(added, deleted) >= 0.8
    return balanced or SWEEP_SUBJECT_RE.search(commit.subject) is not None


def find_rework(
    commits: Sequence[Commit],
    window_days: int = DEFAULT_REWORK_WINDOW_DAYS,
    hunks: Hunks | None = None,
) -> list[Finding]:
    """Emit ``human-rework`` findings.

    A candidate is a human commit that is the next commit to touch a file after
    an agent commit, within the window. It counts only if it (a) changes lines
    the agent commit wrote (needs ``hunks``), or (b) says in its subject that
    it fixes or reverts something. A human simply continuing work elsewhere in
    the file is not rework.
    """
    window = timedelta(days=window_days)
    last_touch: dict[str, Commit] = {}
    reformatted: set[str] = set()  # files a formatter rewrote since their last agent commit
    candidates: list[tuple[Commit, Commit, str, bool]] = []
    for commit in commits:
        if commit.is_bot or is_sweep(commit):
            # An auto-formatter, dependency bot or repo-wide reformat is no rework. But
            # once a formatter has rewritten a file, line numbers no longer say whose
            # lines a later commit changes.
            if not commit.is_agent and MECHANICAL_SUBJECT_RE.search(commit.subject):
                reformatted.update(commit.files)
            continue
        routine = is_routine(commit)
        for path in sorted(commit.files):
            previous = last_touch.get(path)
            last_touch[path] = commit
            if commit.is_agent:
                reformatted.discard(path)
            if previous is None or not previous.is_agent or commit.is_agent or routine:
                continue  # upkeep (a bump, a release) follows the last bump: no rework
            if timedelta(0) <= commit.date - previous.date <= window:
                candidates.append((previous, commit, path, path in reformatted))
    findings: list[Finding] = []
    # newest first: they get the patch budget
    for previous, commit, path, blurred in reversed(candidates):
        overlap = 0
        if hunks is not None and not blurred:
            agent_new = hunks(previous.sha).get(path, ([], []))[1]
            human_old = hunks(commit.sha).get(path, ([], []))[0]
            if isinstance(hunks, HunkReader):
                human_old = without_pin_bumps(
                    agent_new,
                    human_old,
                    hunks.pins(previous.sha).get(path, ([], []))[1],
                    hunks.pins(commit.sha).get(path, ([], []))[0],
                )
            overlap = reworked_lines(agent_new, human_old)
        by_subject = bool(FIX_SUBJECT_RE.search(commit.subject))
        if not overlap and not by_subject:
            continue
        if isinstance(hunks, HunkReader) and path in hunks.version_only(commit.sha):
            continue  # a release bumping the version the agent's commit had set: no rework
        new_work = not by_subject and FEATURE_SUBJECT_RE.search(commit.subject) is not None
        if new_work and commit.date - previous.date > FEATURE_REWORK_WINDOW:
            # "feat: add pagination" a week later, touching lines the agent wrote, is the
            # project moving on. A commit that announces new work counts only if it
            # follows the agent's commit closely.
            continue
        added, deleted = commit.files[path]
        gap = commit.date - previous.date
        hours = gap.total_seconds() / 3600
        after = f"{hours:.0f} h" if hours < 48 else f"{gap.days} days"
        how = (
            f"rewrote lines that commit had written ({overlap} hunk{'s' if overlap != 1 else ''})"
            if overlap
            else "is described as a fix or revert"
        )
        findings.append(
            Finding(
                signal=HUMAN_REWORK,
                session_id=previous.session_id,
                source="git",
                line=0,
                ts=commit.date,
                summary=(
                    f"Agent commit {previous.sha[:8]} changed {path}; {after} later human "
                    f"commit {commit.sha[:8]} {how} (+{added}/-{deleted})"
                ),
                excerpt=commit.subject,
                targets=[Target(model.FILE, path)],
                stream="git",
                detail={
                    "agent_commit": previous.sha[:8],
                    "human_commit": commit.sha[:8],
                    "subject": commit.subject,
                    "variant": "overlap" if overlap else "subject",
                },
            )
        )
    findings.reverse()
    return findings
