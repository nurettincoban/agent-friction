from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_friction import discovery
from agent_friction.discovery import discover_transcripts, encode_cwd
from agent_friction.engine import AnalyzeOptions, analyze
from tests import builders
from tests.builders import Session


def test_encode_cwd_replaces_every_non_alphanumeric_character() -> None:
    assert encode_cwd("/home/user") == "-home-user"
    assert encode_cwd("/Users/me/My Proj_v2.1") == "-Users-me-My-Proj-v2-1"
    assert encode_cwd(Path("/a/b-c/.d")) == "-a-b-c--d"
    assert encode_cwd(r"C:\Users\me\repo") == "C--Users-me-repo"


def test_config_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert discovery.claude_config_dirs({}, home) == [home / ".claude", home / ".config" / "claude"]
    assert discovery.claude_config_dirs({"XDG_CONFIG_HOME": "/xdg"}, home)[1] == Path("/xdg/claude")
    assert discovery.claude_config_dirs({"CLAUDE_CONFIG_DIR": "/custom/cfg"}, home) == [
        Path("/custom/cfg")
    ]
    # one directory, taken literally (no splitting on ":" or ";")
    odd = "/cfg:with;separators"
    assert discovery.claude_config_dirs({"CLAUDE_CONFIG_DIR": odd}, home) == [Path(odd)]


def project(config: Path, cwd: Path | str, name: str = "sess") -> Path:
    directory = config / "projects" / encode_cwd(cwd)
    return Session(name, cwd=str(cwd)).user("hi").save(directory)


def test_discovers_repo_subdirectories_and_subagents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "work" / "my.repo"
    (repo / "packages" / "api").mkdir(parents=True)
    config = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))

    root_session = project(config, repo, "root")
    sub_session = project(config, repo / "packages" / "api", "sub")
    agent = Session("root", cwd=str(repo), sidechain=True).user("task")
    agent_file = agent.save(root_session.parent / "root" / "subagents", "agent-1")
    (root_session.parent / "root" / "notes.json").write_text("{}")  # not a transcript

    # Same encoded prefix, different directory: must be rejected via the recorded cwd.
    project(config, tmp_path / "work" / "my.repo-other", "sibling")
    # Encodes exactly like the repo: lands in the same directory, filtered per file.
    collision = project(config, tmp_path / "work" / "my-repo", "collision")
    assert collision.parent == root_session.parent
    project(config, tmp_path / "unrelated", "unrelated")

    assert discover_transcripts(repo) == sorted([root_session, agent_file, sub_session])


def test_sessions_started_in_a_parent_directory_are_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    repo = home / "code" / "app"
    repo.mkdir(parents=True)
    config = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    home_dir = config / "projects" / encode_cwd(home)

    # Started in ~, works on files of the repo by absolute path.
    relevant = Session("from-home", cwd=str(home)).user("fix app")
    relevant.edit(str(repo / "a.py"), error=builders.EDIT_NOT_FOUND)
    relevant_file = relevant.save(home_dir)
    # Started in ~/code; only its subagent ever names the repo.
    parent = Session("via-subagent", cwd=str(home / "code")).user("delegate")
    parent_file = parent.save(config / "projects" / encode_cwd(home / "code"))
    sub = Session("via-subagent", cwd=str(home / "code"), sidechain=True).user("task")
    sub.read(str(repo / "b.py"))
    sub_file = sub.save(parent_file.parent / "via-subagent" / "subagents", "agent-1")
    # Started in ~ but about something else entirely: not picked up.
    Session("unrelated", cwd=str(home)).user("tidy my dotfiles").read(str(home / ".bashrc")).save(
        home_dir
    )

    assert discover_transcripts(repo) == sorted([relevant_file, parent_file, sub_file])
    assert discovery.discover_project_dirs(repo, [config], include_ancestors=False) == []

    out = analyze(AnalyzeOptions(repo=repo, include_git=False))
    assert out["totals"]["sessions"] == 2
    assert [(h["path"], h["signals"]) for h in out["hotspots"]] == [("a.py", {"edit-miss": 1})]


def test_exact_match_is_kept_when_no_cwd_can_be_read(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "cfg"
    exact = config / "projects" / encode_cwd(repo)
    prefixed = config / "projects" / (encode_cwd(repo) + "-sub")
    for directory in (exact, prefixed):
        directory.mkdir(parents=True)
        (directory / "s.jsonl").write_text('{"type": "summary"}\nnot json\n')
    assert discovery.discover_project_dirs(repo, [config]) == [exact]


def test_default_location_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    repo = home / "code" / "app"
    repo.mkdir(parents=True)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    expected = project(home / ".claude", repo)
    assert discover_transcripts(repo) == [expected]
    assert discover_transcripts(home / "code" / "other") == []


def test_missing_config_dir_yields_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "does-not-exist"))
    assert discover_transcripts(tmp_path) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_worktrees_are_discovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def git(*args: str) -> None:
        identity = ["-c", "user.name=T", "-c", "user.email=t@example.test"]
        subprocess.run(
            ["git", "-C", str(repo), *identity, *args], check=True, capture_output=True, env=env
        )

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "init")
    worktree = tmp_path / "elsewhere" / "feature-x"
    git("worktree", "add", "-q", str(worktree), "-b", "feature-x")

    assert worktree.resolve() in [p.resolve() for p in discovery.git_worktrees(repo)]
    config = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    in_worktree = project(config, worktree.resolve(), "wt")
    assert discover_transcripts(repo) == [in_worktree]
    assert discovery.git_worktrees(tmp_path / "elsewhere") == []


def test_expand_transcript_paths(tmp_path: Path) -> None:
    a = tmp_path / "d" / "a.jsonl"
    b = tmp_path / "d" / "deep" / "b.jsonl"
    b.parent.mkdir(parents=True)
    for path in (a, b, tmp_path / "d" / "c.txt"):
        path.write_text("{}\n")
    assert discovery.expand_transcript_paths([tmp_path / "d", a, tmp_path / "nope"]) == [a, b]
