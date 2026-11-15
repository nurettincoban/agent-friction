from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_friction import gitsignals, signals

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

CLAUDE = "Co-Authored-By: Claude <noreply@anthropic.com>"


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(
        self, *args: str, date: str | None = None, author: str = "Dana Dev <dana@corp.test>"
    ) -> str:
        name, _, email = author.partition(" <")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email.rstrip(">"),
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email.rstrip(">"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        out = subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return out.stdout

    def commit(self, day: int, message: str, files: dict[str, str], **kw: str) -> str:
        for rel, content in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, date=f"2026-10-{day:02d}T12:00:00+00:00", **kw)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def test_agent_commit_patterns() -> None:
    patterns = gitsignals.compile_agent_patterns()

    def agent(author: str, message: str) -> bool:
        return gitsignals.is_agent_commit(author, message, patterns)

    human = "Dana Dev <dana@corp.test>"
    assert agent(human, f"fix\n\n{CLAUDE}")
    assert agent(human, "fix\n\nco-authored-by: Cursor Agent <cursoragent@cursor.com>")
    assert agent(
        human, "fix\n\nCo-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>"
    )
    assert agent(human, "fix\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    assert agent(human, "aider: rename the helper")
    assert agent("Dana Dev (aider) <dana@corp.test>", "rename")
    assert agent("copilot-swe-agent[bot] <1+copilot[bot]@users.noreply.github.com>", "fix")
    assert agent("Claude <noreply@anthropic.com>", "fix")
    assert not agent(human, "fix the claude integration docs")
    assert not agent(human, "fix\n\nCo-authored-by: Sam Smith <sam@corp.test>")
    assert not agent("dependabot[bot] <dependabot@github.com>", "bump x")
    custom = gitsignals.compile_agent_patterns([r"^\[bot\]"])
    assert gitsignals.is_agent_commit(human, "[bot] regenerate", custom)
    assert not gitsignals.is_agent_commit(human, f"fix\n\n{CLAUDE}", custom)


def test_rework_detection_end_to_end(repo: Repo) -> None:
    repo.commit(1, "initial", {"a.py": "1\n", "b.py": "1\n", "dir/c d.py": "1\n", "old.py": "1\n"})
    agent1 = repo.commit(
        2,
        f"feat: agent work\n\nbody line\n\n{CLAUDE}",
        {"a.py": "1\n2\n", "b.py": "2\n", "dir/c d.py": "2\n"},
    )
    human1 = repo.commit(5, "fix what the agent did", {"a.py": "1\n2\n3\n"})
    repo.commit(6, "more human work", {"a.py": "4\n"})  # follows a human commit: not rework
    repo.commit(7, f"agent again\n\n{CLAUDE}", {"b.py": "3\n"})  # agent after agent: not rework
    repo.commit(30, "late human change", {"b.py": "4\n", "dir/c d.py": "3\n"})  # outside 14 days
    agent2 = repo.commit(31, "aider: tweak", {"old.py": "2\n"})
    human2 = repo.commit(31, "same-day fixup", {"old.py": "3\n", "new.py": "1\n"})

    history = gitsignals.read_history(repo.path)
    assert history.available and len(history.commits) == 8 and history.agent_commits == 3
    assert history.commits[1].files == {"a.py": (1, 0), "b.py": (1, 1), "dir/c d.py": (1, 1)}
    assert history.commits[1].subject == "feat: agent work"

    found = gitsignals.find_rework(history.commits)
    assert [(f.targets[0].key, f.session_id) for f in found] == [
        ("a.py", f"git:{agent1[:12]}"),
        ("old.py", f"git:{agent2[:12]}"),
    ]
    first = found[0]
    assert first.signal == signals.HUMAN_REWORK and first.source == "git" and first.line == 0
    assert first.detail == {
        "agent_commit": agent1[:8],
        "human_commit": human1[:8],
        "subject": "fix what the agent did",
    }
    assert "3 days later (+1/-0)" in first.summary and first.excerpt == "fix what the agent did"
    assert found[1].detail["human_commit"] == human2[:8]

    wide = gitsignals.find_rework(history.commits, window_days=60)
    assert sorted(f.targets[0].key for f in wide) == ["a.py", "b.py", "dir/c d.py", "old.py"]
    assert gitsignals.find_rework(history.commits, window_days=0)[0].targets[0].key == "old.py"


def test_history_is_relative_to_a_subdirectory(repo: Repo) -> None:
    repo.commit(1, f"agent\n\n{CLAUDE}", {"pkg/a.py": "1\n", "top.py": "1\n"})
    repo.commit(2, "human", {"pkg/a.py": "2\n", "top.py": "2\n"})
    history = gitsignals.read_history(repo.path / "pkg")
    assert [sorted(c.files) for c in history.commits] == [["a.py"], ["a.py"]]


def test_degrades_gracefully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    not_a_repo = gitsignals.read_history(tmp_path)
    assert not not_a_repo.available and not_a_repo.reason and not_a_repo.commits == []

    empty = Repo(tmp_path / "empty")
    assert not gitsignals.read_history(empty.path).available  # no commits yet

    monkeypatch.setenv("PATH", str(tmp_path / "no-bin"))
    missing = gitsignals.read_history(tmp_path)
    assert not missing.available and "not found" in missing.reason


def test_parse_log_tolerates_junk() -> None:
    patterns = gitsignals.compile_agent_patterns()
    rs, fs = "\x1e", "\x1f"
    good = (
        f"{rs}abc123{fs}2026-10-01T12:00:00+02:00{fs}A{fs}a@b.c{fs}subject\n\nbody{fs}\n\n"
        "3\t1\tsrc/a.py\n-\t-\tbin.dat\nnot a numstat line\n"
    )
    junk = f"{rs}short{fs}record" + f"{rs}{fs}bad-date{fs}A{fs}a@b.c{fs}m{fs}\n"
    (commit,) = gitsignals.parse_log("preamble" + junk + good, patterns)
    assert commit.sha == "abc123" and commit.files == {"src/a.py": (3, 1), "bin.dat": (0, 0)}
    assert gitsignals.parse_log("", patterns) == []
