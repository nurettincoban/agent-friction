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


def parse_log(output: str, patterns: Sequence[re.Pattern[str]]) -> list[Commit]:
    """Parse ``git log --numstat`` output produced with our record/field separators."""
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
    return GitHistory(True, "", parse_log(proc.stdout, compile_agent_patterns(agent_patterns)))


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


class HunkReader:
    """Reads and caches the changed line ranges of single commits (``git show -U0``)."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.calls = 0
        self._cache: dict[str, dict[str, tuple[list[Range], list[Range]]]] = {}

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
        return parse_hunks(proc.stdout) if proc.returncode == 0 else {}


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
    candidates: list[tuple[Commit, Commit, str]] = []
    for commit in commits:
        for path in sorted(commit.files):
            previous = last_touch.get(path)
            last_touch[path] = commit
            if previous is None or not previous.is_agent or commit.is_agent:
                continue
            if timedelta(0) <= commit.date - previous.date <= window:
                candidates.append((previous, commit, path))
    findings: list[Finding] = []
    for previous, commit, path in reversed(candidates):  # newest first: they get the patch budget
        overlap = 0
        if hunks is not None:
            agent_new = hunks(previous.sha).get(path, ([], []))[1]
            human_old = hunks(commit.sha).get(path, ([], []))[0]
            overlap = reworked_lines(agent_new, human_old)
        by_subject = bool(FIX_SUBJECT_RE.search(commit.subject))
        if not overlap and not by_subject:
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
