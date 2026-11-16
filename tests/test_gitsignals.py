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


BASE = "".join(f"line {n}\n" for n in range(1, 11))


def lines(text: str, **changes: str) -> str:
    """``text`` with 1-based lines replaced, e.g. ``lines(BASE, l3="x")``."""
    out = text.splitlines()
    for key, value in changes.items():
        out[int(key[1:]) - 1] = value
    return "\n".join(out) + "\n"


def test_rework_detection_end_to_end(repo: Repo) -> None:
    files = ("a.py", "b.py", "dir/c d.py", "e.py", "old.py")
    repo.commit(1, "initial", dict.fromkeys(files, BASE))
    by_agent = lines(BASE, l3="agent 3", l4="agent 4")
    agent1 = repo.commit(
        2, f"feat: agent work\n\nbody line\n\n{CLAUDE}", dict.fromkeys(files[:4], by_agent)
    )
    # rewrites a line the agent wrote: rework, whatever the subject says
    human1 = repo.commit(5, "tidy the handler", {"a.py": lines(by_agent, l4="human 4")})
    # touches other lines and appends: a human continuing work, not rework
    repo.commit(5, "add retries", {"b.py": lines(by_agent, l9="human 9") + "appended\n"})
    # touches other lines but says it is a fix: counted on the subject alone
    human3 = repo.commit(6, "fix: handle empty carts", {"dir/c d.py": lines(by_agent, l8="h")})
    repo.commit(7, "more human work", {"a.py": lines(by_agent, l3="human 3", l4="human 4")})
    repo.commit(8, f"agent again\n\n{CLAUDE}", {"b.py": by_agent})  # agent after human
    repo.commit(30, "revert the agent", {"e.py": BASE})  # outside the 14-day window
    agent2 = repo.commit(31, "aider: tweak", {"old.py": lines(BASE, l1="agent 1")})
    human2 = repo.commit(31, "same-day tweak", {"old.py": lines(BASE, l1="human 1"), "new.py": "1"})

    history = gitsignals.read_history(repo.path)
    assert history.available and len(history.commits) == 10 and history.agent_commits == 3
    assert history.commits[1].files == dict.fromkeys(files[:4], (2, 2))
    assert history.commits[1].subject == "feat: agent work"

    reader = gitsignals.HunkReader(repo.path)
    found = gitsignals.find_rework(history.commits, hunks=reader)
    assert [(f.targets[0].key, f.session_id, f.detail["variant"]) for f in found] == [
        ("a.py", f"git:{agent1[:12]}", "overlap"),
        ("dir/c d.py", f"git:{agent1[:12]}", "subject"),
        ("old.py", f"git:{agent2[:12]}", "overlap"),
    ]
    first = found[0]
    assert first.signal == signals.HUMAN_REWORK and first.source == "git" and first.line == 0
    assert first.detail == {
        "agent_commit": agent1[:8],
        "human_commit": human1[:8],
        "subject": "tidy the handler",
        "variant": "overlap",
    }
    assert "3 days later human commit" in first.summary
    assert "rewrote lines that commit had written (1 hunk) (+1/-1)" in first.summary
    assert first.excerpt == "tidy the handler"
    assert found[1].detail["human_commit"] == human3[:8]
    assert "is described as a fix or revert" in found[1].summary
    assert found[2].detail["human_commit"] == human2[:8]
    assert reader.calls <= 2 * 5  # only candidate pairs are read, each commit once

    wide = gitsignals.find_rework(history.commits, window_days=60, hunks=reader)
    assert sorted(f.targets[0].key for f in wide) == ["a.py", "dir/c d.py", "e.py", "old.py"]
    narrow = gitsignals.find_rework(history.commits, window_days=0, hunks=reader)
    assert [f.targets[0].key for f in narrow] == ["old.py"]
    # without line ranges only the commit subject can qualify a candidate
    blind = gitsignals.find_rework(history.commits)
    assert [f.targets[0].key for f in blind] == ["dir/c d.py"]


def test_parse_hunks() -> None:
    patch = (
        "diff --git a/src/a.py b/src/a.py\nindex 1..2 100644\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -3,2 +3,4 @@ def f():\n-x\n-y\n+a\n+b\n+c\n+d\n"
        "@@ -9,0 +12 @@\n+new\n@@ -20 +22,0 @@\n-gone\n"
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-1\n-2\n"
        "diff --git a/bin.dat b/bin.dat\nBinary files a/bin.dat and b/bin.dat differ\n"
        "@@ not a hunk header\n"
    )
    assert gitsignals.parse_hunks(patch) == {
        "src/a.py": ([(3, 5), (9, 9), (20, 21)], [(3, 7), (12, 13), (22, 22)]),
        "gone.py": ([(1, 3)], [(0, 0)]),
    }
    assert gitsignals.parse_hunks("") == {}


@pytest.mark.parametrize(
    ("agent_new", "human_old", "hits"),
    [
        ([(10, 20)], [(12, 13)], 1),  # changed a line the agent wrote
        ([(10, 20)], [(5, 11)], 1),  # a change that reaches into the agent's block
        ([(10, 20)], [(20, 22)], 0),  # the lines right after it
        ([(10, 20)], [(1, 10)], 0),  # the lines right before it
        ([(10, 20)], [(14, 14)], 1),  # inserted in the middle of the agent's block
        ([(10, 20)], [(19, 19)], 0),  # appended after the agent's last line
        ([(10, 20)], [(9, 9)], 0),  # inserted just above the agent's block
        ([(10, 10)], [(8, 12)], 0),  # the agent only deleted lines there
        ([(10, 20), (40, 45)], [(12, 13), (41, 42), (70, 71)], 2),
        ([], [(1, 5)], 0),
    ],
)
def test_reworked_lines(
    agent_new: list[tuple[int, int]], human_old: list[tuple[int, int]], hits: int
) -> None:
    assert gitsignals.reworked_lines(agent_new, human_old) == hits


@pytest.mark.parametrize(
    ("subject", "is_fix"),
    [
        ("fix: handle empty carts", True),
        ('Revert "feat: agent work"', True),
        ("fixes the regression in checkout", True),
        ("oops, wrong default", True),
        ("add retries to the client", False),
        ("prefix the log lines", False),  # "fix" inside another word
        ("refactor fixtures", False),
        ("bump version", False),
    ],
)
def test_fix_subjects(subject: str, is_fix: bool) -> None:
    assert bool(gitsignals.FIX_SUBJECT_RE.search(subject)) is is_fix


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


def test_bot_commits_are_not_human_rework(repo: Repo) -> None:
    repo.commit(1, "initial", {"a.py": BASE, "b.py": BASE, "c.py": BASE})
    by_agent = lines(BASE, l3="agent 3", l4="agent 4")
    repo.commit(2, f"feat\n\n{CLAUDE}", dict.fromkeys(("a.py", "b.py", "c.py"), by_agent))
    formatted = lines(by_agent, l4="agent  4")
    bot = "github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
    repo.commit(3, "style: auto-format", {"a.py": formatted}, author=bot)
    repo.commit(3, "bump", {"b.py": formatted}, author="dependabot <support@dependabot.com>")
    repo.commit(3, "format", {"c.py": formatted}, author="Format Robot <fmt@corp.test>")
    human = repo.commit(4, "fix: the handler", {"a.py": lines(formatted, l3="human 3")})

    history = gitsignals.read_history(repo.path)
    assert [c.is_bot for c in history.commits] == [False, False, True, True, False, False]
    found = gitsignals.find_rework(history.commits, hunks=gitsignals.HunkReader(repo.path))
    # the bots are skipped; the human fix after them still counts against the agent commit
    assert sorted(f.targets[0].key for f in found) == ["a.py", "c.py"]
    assert human[:8] in "".join(str(f.detail) + f.summary for f in found)

    history = gitsignals.read_history(repo.path, bot_patterns=[r"^author: Format Robot"])
    found = gitsignals.find_rework(history.commits, hunks=gitsignals.HunkReader(repo.path))
    assert [f.targets[0].key for f in found] == ["a.py"]


def test_default_bot_patterns() -> None:
    bots = gitsignals.compile_bot_patterns()

    def bot(author: str) -> bool:
        return any(p.search(f"author: {author}") for p in bots)

    assert bot("renovate[bot] <29139614+renovate[bot]@users.noreply.github.com>")
    assert bot("pre-commit-ci[bot] <66853113+pre-commit-ci[bot]@users.noreply.github.com>")
    assert bot("dependabot <49699333+dependabot@users.noreply.github.com>")
    assert bot("GitHub Actions <actions@github.com>")
    assert not bot("Dana Dev <dana@corp.test>") and not bot("Abbot Lee <abbot@corp.test>")


def test_a_repo_wide_reformat_is_not_rework(repo: Repo) -> None:
    names = [f"src/m{i}.py" for i in range(25)]
    repo.commit(1, "initial", dict.fromkeys(names, BASE))
    by_agent = lines(BASE, l3="agent 3", l4="agent 4")
    repo.commit(2, f"feat\n\n{CLAUDE}", {names[0]: by_agent, names[1]: by_agent})
    # every file reformatted by a human, including the agent's lines
    swept = {n: lines(by_agent if n in names[:2] else BASE, l4="x  4") for n in names}
    repo.commit(3, "apply the new formatter", swept)
    human = repo.commit(4, "tidy", {names[1]: lines(swept[names[1]], l3="human 3")})
    history = gitsignals.read_history(repo.path)
    assert [gitsignals.is_sweep(c) for c in history.commits] == [False, False, True, False]
    found = gitsignals.find_rework(history.commits, hunks=gitsignals.HunkReader(repo.path))
    assert [(f.targets[0].key, f.detail["human_commit"]) for f in found] == [(names[1], human[:8])]

    # a fix that happens to touch many files is still a human reacting
    repo.commit(5, f"more\n\n{CLAUDE}", {names[2]: by_agent})
    repo.commit(6, "fix: broken imports everywhere", {n: lines(BASE, l3="fixed") for n in names})
    history = gitsignals.read_history(repo.path)
    assert not gitsignals.is_sweep(history.commits[-1])
