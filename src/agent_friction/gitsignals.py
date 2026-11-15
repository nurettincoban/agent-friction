"""Human-rework detection from git history.

A file counts as reworked when a commit attributed to a coding agent changed
it and the *next* commit touching that file is a human one made within the
rework window. Only the current branch's history is read.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Sequence
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


def find_rework(
    commits: Sequence[Commit], window_days: int = DEFAULT_REWORK_WINDOW_DAYS
) -> list[Finding]:
    """Emit one ``human-rework`` finding per (human commit, file) that follows an agent commit."""
    window = timedelta(days=window_days)
    last_touch: dict[str, Commit] = {}
    findings: list[Finding] = []
    for commit in commits:
        for path, (added, deleted) in sorted(commit.files.items()):
            previous = last_touch.get(path)
            last_touch[path] = commit
            if previous is None or not previous.is_agent or commit.is_agent:
                continue
            gap = commit.date - previous.date
            if gap < timedelta(0) or gap > window:
                continue
            hours = gap.total_seconds() / 3600
            after = f"{hours:.0f} h" if hours < 48 else f"{gap.days} days"
            findings.append(
                Finding(
                    signal=HUMAN_REWORK,
                    session_id=f"git:{previous.sha[:12]}",
                    source="git",
                    line=0,
                    ts=commit.date,
                    summary=(
                        f"Agent commit {previous.sha[:8]} changed {path}; human commit "
                        f"{commit.sha[:8]} changed it again {after} later (+{added}/-{deleted})"
                    ),
                    excerpt=commit.subject,
                    targets=[Target(model.FILE, path)],
                    stream="git",
                    detail={
                        "agent_commit": previous.sha[:8],
                        "human_commit": commit.sha[:8],
                        "subject": commit.subject,
                    },
                )
            )
    return findings
