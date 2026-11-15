from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_friction import __version__, signals
from agent_friction.discovery import encode_cwd
from agent_friction.engine import AnalyzeOptions, NoTranscriptsError, analyze
from agent_friction.errors import AgentFrictionError
from tests import builders, fixture_story
from tests.builders import Session

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
SCHEMA = "src/shopfront/api/schema_gen.py"
SERVICE = "src/shopfront/checkout/service.py"


def story(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "repo": fixture_story.REPO,
        "transcripts": [fixture_story.TRANSCRIPTS],
        "repo_aliases": [fixture_story.RECORDED_AT],
        "include_git": False,
        "now": NOW,
    }
    options.update(overrides)
    return analyze(AnalyzeOptions(**options))


@pytest.fixture(scope="module")
def result() -> dict[str, Any]:
    return story()


def entry(items: list[dict[str, Any]], path: str) -> dict[str, Any]:
    return next(item for item in items if item["path"] == path)


def test_fixture_files_are_up_to_date(tmp_path: Path) -> None:
    """tests/fixtures/transcripts must match what tests/fixture_story.py generates."""
    regenerated = fixture_story.write_story(tmp_path)
    committed = sorted(p for p in fixture_story.TRANSCRIPTS.rglob("*.jsonl"))
    assert sorted(p.relative_to(tmp_path) for p in regenerated) == [
        p.relative_to(fixture_story.TRANSCRIPTS) for p in committed
    ]
    for path in regenerated:
        twin = fixture_story.TRANSCRIPTS / path.relative_to(tmp_path)
        assert path.read_text() == twin.read_text(), f"stale fixture: {twin}"


def test_contract_shape(result: dict[str, Any]) -> None:
    assert json.loads(json.dumps(result)) == result
    assert result["schema_version"] == 1
    assert result["tool"] == {"name": "agent-friction", "version": __version__}
    assert result["generated_at"] == "2026-11-15T12:00:00Z"
    assert result["repo"]["name"] == "shopfront"
    assert result["window"]["start"] == "2026-10-26T10:00:00Z"
    assert result["window"]["end"].startswith("2026-11-12T11:")
    assert set(result["totals"]) >= {
        "sessions",
        "tool_calls",
        "friction_events",
        "est_minutes_lost",
        "corrections",
        "files_affected",
    }
    assert [s["id"] for s in result["signals"]] == [s.id for s in signals.CATALOG]
    assert sum(s["count"] for s in result["signals"]) == result["totals"]["friction_events"]
    hotspot_keys = {
        "path", "kind", "score", "sessions", "events", "est_minutes_lost", "signals",
        "confidence", "why", "recommendations", "evidence",
    }  # fmt: skip
    evidence_keys = {"signal", "session_id", "timestamp", "summary", "excerpt", "source", "line"}
    for section, kind in (("hotspots", "file"), ("commands", "command"), ("directories", "dir")):
        assert result[section], section
        scores = [(-h["score"], -h["events"], h["path"]) for h in result[section]]
        assert scores == sorted(scores)
        for hotspot in result[section]:
            assert hotspot_keys <= set(hotspot) and hotspot["kind"] == kind
            assert 0 < hotspot["score"] <= 100 and hotspot["score"] == round(hotspot["score"], 1)
            assert hotspot["confidence"] in ("low", "medium", "high")
            assert sum(hotspot["signals"].values()) == hotspot["events"]
            assert 1 <= len(hotspot["evidence"]) <= 5
            for evidence in hotspot["evidence"]:
                assert set(evidence) == evidence_keys
                assert evidence["line"] >= 1 and evidence["timestamp"].endswith("Z")
            for rec in hotspot["recommendations"]:
                assert {"id", "title", "detail"} <= set(rec)


def test_sources_and_totals(result: dict[str, Any]) -> None:
    claude, generic = result["sources"]
    assert (claude["kind"], claude["sessions"], claude["files"]) == ("claude-code", 5, 6)
    assert claude["path"].endswith("transcripts/-home-dev-shopfront")
    assert (claude["skipped_lines"], claude["ignored_lines"]) == (0, 10)
    assert (generic["kind"], generic["sessions"]) == ("generic", 1)
    totals = result["totals"]
    assert totals["sessions"] == 6  # the subagent transcript belongs to its parent session
    assert totals["corrections"] == 5
    assert totals["files_affected"] == len(result["hotspots"]) == 6
    assert totals["est_minutes_lost"] > 0


def test_story_generated_schema_is_the_top_file(result: dict[str, Any]) -> None:
    top = result["hotspots"][0]
    assert top["path"] == SCHEMA and top["confidence"] == "high" and top["sessions"] == 4
    assert top["signals"]["edit-miss"] == 6 and top["signals"]["user-correction"] == 3
    assert top["file"] == {"exists": True, "lines": 1031, "generated": True}
    recs = {r["id"]: r["detail"] for r in top["recommendations"]}
    assert list(recs)[:2] == ["edit-the-source-not-the-output", "split-or-anchor-large-file"]
    assert "stop hand-editing src/shopfront/api/schema_gen.py" in recs["write-down-the-rule"]
    assert top["why"].startswith("12 friction events in 4 sessions (6 edit miss, 3 user correction")
    assert top["score_detail"]["sessions"][0].keys() == {"session_id", "weighted_events", "points"}


def test_story_deprecated_api_corrections_land_on_the_service(result: dict[str, Any]) -> None:
    service = entry(result["hotspots"], SERVICE)
    assert service["sessions"] == 3 and service["confidence"] == "high"
    assert service["signals"] == {"user-correction": 3, "edit-revert": 2, "edit-thrash": 1}
    rule = next(r for r in service["recommendations"] if r["id"] == "write-down-the-rule")
    assert "I told you: PaymentClient only" in rule["detail"]
    quotes = [e["excerpt"] for e in service["evidence"] if e["signal"] == "user-correction"]
    assert any("legacy_charge" in q for q in quotes)
    # legacy.py is named by the human but was never touched by the agent: not a hotspot
    assert "src/shopfront/payments/legacy.py" not in [h["path"] for h in result["hotspots"]]


def test_story_pytest_should_be_make_test(result: dict[str, Any]) -> None:
    top = result["commands"][0]
    assert (top["path"], top["kind"], top["confidence"]) == ("pytest", "command", "high")
    assert top["signals"] == {"command-failure": 7, "retry-loop": 1} and top["sessions"] == 4
    first = top["recommendations"][0]
    assert first["id"] == "use-the-working-command"
    assert "5 times the next successful command was `make test`" in first["detail"]
    assert any("No module named 'shopfront'" in (e["excerpt"] or "") for e in top["evidence"])
    make = entry(result["commands"], "make test")
    assert make["score"] < top["score"] / 4  # one legitimate test failure barely registers


def test_story_search_misses_roll_up_to_the_directory(result: dict[str, Any]) -> None:
    (directory,) = result["directories"]
    assert (directory["path"], directory["kind"]) == ("src/shopfront", "dir")
    assert directory["signals"] == {"search-miss": 5} and directory["sessions"] == 2
    assert "`RefundPolicy` (2x)" in directory["recommendations"][0]["detail"]
    sources = {e["source"] for e in directory["evidence"]}
    assert any(s.endswith("subagents/agent-a1f3c9.jsonl") for s in sources)


def test_tree_rolls_files_up_into_directories(result: dict[str, Any]) -> None:
    tree = result["tree"]
    assert (tree["name"], tree["path"], tree["kind"]) == ("shopfront", "", "dir")

    def walk(node: dict[str, Any]) -> list[dict[str, Any]]:
        return [node] + [n for child in node["children"] for n in walk(child)]

    nodes = {n["path"]: n for n in walk(tree)}
    leaves = sorted(p for p, n in nodes.items() if n["kind"] == "file")
    assert leaves == sorted(h["path"] for h in result["hotspots"])
    assert (
        nodes[SCHEMA]["score"] == result["hotspots"][0]["score"] and not nodes[SCHEMA]["children"]
    )
    package = nodes["src/shopfront"]
    assert package["kind"] == "dir" and package["events"] > nodes["src/shopfront/api"]["events"]
    assert package["score"] >= max(c["score"] for c in package["children"])
    for node in nodes.values():
        ordered = [(-c["score"], c["path"]) for c in node["children"]]
        assert ordered == sorted(ordered)
        assert node["est_minutes_lost"] <= tree["est_minutes_lost"]
    # a correction split over two files counts once in their common parent
    assert tree["events"] <= result["totals"]["friction_events"]


def test_output_is_deterministic_and_redacted(result: dict[str, Any]) -> None:
    assert json.dumps(story(), sort_keys=True) == json.dumps(result, sort_keys=True)
    text = json.dumps(result)
    assert "sf_stg_9f8e7d6c" not in text and "Bearer [REDACTED:token]" in text
    assert "/home/dev" not in text and "~/shopfront/tests/conftest.py" in text
    assert "Find the Order schema" not in text  # thinking blocks never leave the parser
    for hotspot in result["hotspots"] + result["commands"] + result["directories"]:
        for evidence in hotspot["evidence"]:
            assert evidence["excerpt"] is None or len(evidence["excerpt"]) <= 240


def test_no_excerpts_removes_all_verbatim_text() -> None:
    """With include_excerpts=False nothing a human typed, searched for or saw printed remains.

    What does remain is structural: repo paths, tool names, counts and normalized
    command names (the identity of a command hotspot).
    """
    bare = story(include_excerpts=False)
    text = json.dumps(bare)
    for verbatim in (
        "RefundPolicy",  # search patterns
        "DiscountPolicy",
        "*policy*",
        "API_VERSION",
        "PaymentClient",  # the human's corrections
        "legacy_charge",
        "deprecated",
        "hand-editing",
        "never delete",  # the reason given when declining a tool
        "ModuleNotFoundError",  # command output
        "conftest",
        "Bearer",  # full command lines
        "staging.shopfront.example",
        "migrations/versions",
    ):
        assert verbatim not in text, verbatim
    for section in ("hotspots", "commands", "directories"):
        assert all(e["excerpt"] is None for h in bare[section] for e in h["evidence"])
    assert bare["hotspots"][0]["recommendations"]  # advice is still given, without quotes
    assert bare["commands"][0]["recommendations"][0]["id"] == "use-the-working-command"
    assert bare["totals"] == story()["totals"]  # only the wording changes, never the numbers


def test_only_unambiguous_corrections_are_quoted(result: dict[str, Any]) -> None:
    text = json.dumps([h["recommendations"] for h in result["hotspots"]])
    assert "I told you: PaymentClient only" in text
    # the "probable" correction (two weaker cues) counts, but is not put in anyone's mouth
    assert "Don't call legacy_charge() here" not in text
    probable = [
        e
        for h in story(max_evidence=50)["hotspots"]
        for e in h["evidence"]
        if e["signal"] == "user-correction" and "confidence 0.4" in e["summary"]
    ]
    assert probable and all("Don't call legacy_charge" in e["excerpt"] for e in probable)


def test_windows_transcripts_resolve_on_any_platform(tmp_path: Path) -> None:
    recorded = "C:\\Users\\dev\\shop"
    s = Session("win", cwd=recorded).user("go")
    s.edit(recorded + "\\src\\a.py", error=builders.EDIT_NOT_FOUND)
    s.edit("src\\a.py", error=builders.EDIT_NOT_FOUND)
    s.edit("c:/Users/dev/shop/src/a.py", error=builders.EDIT_NOT_FOUND)
    s.edit("C:\\Users\\dev\\elsewhere\\b.py", error=builders.EDIT_NOT_FOUND)
    s.bash("npm test", "sh: 1: jest: not found", exit_code=127)
    repo = tmp_path / "shop"
    repo.mkdir()
    out = analyze(
        AnalyzeOptions(
            repo=repo,
            transcripts=[s.save(tmp_path / "t")],
            repo_aliases=[recorded],
            include_git=False,
        )
    )
    assert [(h["path"], h["events"]) for h in out["hotspots"]] == [("src/a.py", 3)]
    assert [c["path"] for c in out["commands"]] == ["npm test"]
    assert "C:" not in json.dumps(out["hotspots"]) and "\\\\" not in json.dumps(out["sources"])


def test_limits(result: dict[str, Any]) -> None:
    small = story(max_hotspots=2, max_evidence=1, excerpt_chars=40)
    assert [h["path"] for h in small["hotspots"]] == [h["path"] for h in result["hotspots"][:2]]
    assert len(small["commands"]) == 2
    assert small["totals"]["files_affected"] == 6  # totals are not truncated
    assert all(len(h["evidence"]) == 1 for h in small["hotspots"])
    assert all(len(e["excerpt"] or "") <= 40 for h in small["commands"] for e in h["evidence"])
    assert small["tree"] == result["tree"]
    none = story(max_hotspots=0, max_evidence=0)
    assert none["hotspots"] == [] and none["commands"] == []


def test_window_filters_events() -> None:
    since = datetime(2026, 11, 1, tzinfo=timezone.utc)
    late = story(since=since)
    assert late["totals"]["sessions"] == 4
    assert late["window"]["start"] == late["window"]["since"] == "2026-11-01T00:00:00Z"
    assert late["window"]["first_event"] == "2026-11-03T09:00:00Z"
    early = story(until=datetime(2026, 10, 31))  # naive datetimes are read as UTC
    assert early["totals"]["sessions"] == 2 and early["window"]["until"] == "2026-10-31T00:00:00Z"
    assert late["totals"]["friction_events"] + early["totals"]["friction_events"] == 36
    nothing = story(since=datetime(2027, 1, 1))
    assert nothing["totals"]["sessions"] == 0 and nothing["hotspots"] == []
    assert nothing["window"]["first_event"] is None and nothing["tree"]["children"] == []


def test_no_transcripts_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(NoTranscriptsError, match="No Claude Code transcripts found") as info:
        analyze(AnalyzeOptions(repo=tmp_path))
    assert isinstance(info.value, AgentFrictionError) and "CLAUDE_CONFIG_DIR" in str(info.value)
    with pytest.raises(NoTranscriptsError, match=r"No \.jsonl transcript files"):
        analyze(AnalyzeOptions(repo=tmp_path, transcripts=[tmp_path / "missing"]))


def test_auto_discovery_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    config = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    s = Session("auto", cwd=str(repo)).user("go")
    s.edit(str(repo / "src" / "a.py"), error=builders.EDIT_NOT_FOUND)
    s.edit("src/a.py", error=builders.EDIT_NOT_FOUND)  # relative to the session cwd
    s.edit("/etc/hosts", error=builders.EDIT_NOT_FOUND)  # outside the repo: dropped
    s.raw("{broken")
    s.save(config / "projects" / encode_cwd(repo))
    out = analyze(AnalyzeOptions(repo=repo, now=NOW))
    (hotspot,) = out["hotspots"]
    assert (hotspot["path"], hotspot["events"], hotspot["confidence"]) == ("src/a.py", 2, "low")
    assert out["sources"][0]["skipped_lines"] == 1
    assert out["sources"][-1] == {
        "kind": "git", "path": out["repo"]["root"], "sessions": 0, "skipped_lines": 0,
        "ignored_lines": 0, "available": False, "note": out["sources"][-1]["note"],
    }  # fmt: skip


def test_unattributed_and_empty_sessions(tmp_path: Path) -> None:
    s = Session("solo").user("look around")
    s.read("/repo/a.py").user("no, I meant the other service")
    out = analyze(
        AnalyzeOptions(repo=Path("/repo"), transcripts=[s.save(tmp_path)], include_git=False)
    )
    assert out["totals"]["friction_events"] == out["totals"]["unattributed_events"] == 1
    assert out["hotspots"] == [] and out["tree"]["events"] == 0 and out["tree"]["score"] == 0.0
    datetime.fromisoformat(out["generated_at"].replace("Z", "+00:00"))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_rework_is_merged_into_file_hotspots(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()

    def commit(day: int, message: str, content: str) -> None:
        (repo / "a.py").write_text(content)
        stamp = f"2026-11-{day:02d}T12:00:00+00:00"
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        identity = ["-c", "user.name=Dana", "-c", "user.email=dana@corp.test"]
        for args in (["add", "-A"], ["commit", "-q", "-m", message]):
            subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, env=env)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    for day in (1, 5):
        commit(day, "agent change\n\nCo-Authored-By: Claude <noreply@anthropic.com>", f"v{day}\n")
        commit(day + 1, "human fix-up", f"v{day} fixed\n")
    s = Session("t", cwd=str(repo), start=datetime(2026, 11, 3, tzinfo=timezone.utc)).user("go")
    s.edit(str(repo / "a.py"), error=builders.EDIT_NOT_FOUND)
    options = AnalyzeOptions(repo=repo, transcripts=[s.save(tmp_path / "t")], now=NOW)
    out = analyze(options)
    (hotspot,) = out["hotspots"]
    assert hotspot["signals"] == {"human-rework": 2, "edit-miss": 1}
    assert hotspot["sessions"] == 3 and hotspot["confidence"] == "high"
    assert "learn-from-rework" in [r["id"] for r in hotspot["recommendations"]]
    rework = next(e for e in hotspot["evidence"] if e["signal"] == "human-rework")
    assert (rework["source"], rework["line"], rework["excerpt"]) == ("git", 0, "human fix-up")
    assert rework["session_id"].startswith("git:")
    assert out["sources"][-1]["kind"] == "git" and out["sources"][-1]["sessions"] == 2
    assert out["totals"]["sessions"] == 1  # git commits are not transcript sessions

    options.since = datetime(2026, 11, 4, tzinfo=timezone.utc)
    assert analyze(options)["hotspots"][0]["signals"] == {"human-rework": 1}
    options.include_git = False
    options.since = None
    assert analyze(options)["hotspots"][0]["signals"] == {"edit-miss": 1}


def test_sessions_from_other_repositories_are_left_out(tmp_path: Path) -> None:
    ours = Session("ours", cwd="/repo").user("go")
    ours.bash("make test", "No rule to make target 'test'", exit_code=2)
    theirs = Session("theirs", cwd="/other/project").user("go")
    theirs.bash("make test", "No rule to make target 'test'", exit_code=2)
    theirs.edit("/other/project/a.py", error=builders.EDIT_NOT_FOUND)
    visitor = Session("visitor", cwd="/other/project").user("go")
    visitor.edit("/repo/a.py", error=builders.EDIT_NOT_FOUND)  # reaches into our repo
    files = [s.save(tmp_path) for s in (ours, theirs, visitor)]
    out = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=files, include_git=False))
    assert out["totals"]["sessions"] == 2 and out["totals"]["tool_calls"] == 2
    assert out["sources"][0]["sessions"] == 2 and out["sources"][0]["sessions_outside_repo"] == 1
    assert [(c["path"], c["events"]) for c in out["commands"]] == [("make test", 1)]
    assert [h["path"] for h in out["hotspots"]] == ["a.py"]
