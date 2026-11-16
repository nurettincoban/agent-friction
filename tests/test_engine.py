from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_friction import __version__, signals
from agent_friction.attribution import public_key
from agent_friction.discovery import encode_cwd
from agent_friction.engine import (
    AnalyzeOptions,
    NoTranscriptsError,
    analyze,
    inspect_transcripts,
    list_transcripts,
)
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
    evidence_keys.add("est_seconds_lost")
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
    # the project directory encodes an absolute path (user, client): it is never shown
    assert claude["path"] == "claude-code:shopfront" and "home-dev" not in json.dumps(result)
    assert (claude["skipped_lines"], claude["ignored_lines"]) == (0, 10)
    assert (generic["kind"], generic["sessions"]) == ("generic", 1)
    totals = result["totals"]
    assert totals["sessions"] == 6  # the subagent transcript belongs to its parent session
    # four corroborated corrections; "Don't call legacy_charge() ..." is only probable
    assert (totals["corrections"], totals["probable_corrections"]) == (4, 1)
    assert all(source["skipped_events"] == 0 for source in result["sources"])
    assert totals["files_affected"] == len(result["hotspots"]) == 5
    assert totals["est_minutes_lost"] > 0


def test_story_generated_schema_is_the_top_file(result: dict[str, Any]) -> None:
    top = result["hotspots"][0]
    assert top["path"] == SCHEMA and top["confidence"] == "high" and top["sessions"] == 4
    assert top["signals"]["edit-miss"] == 6 and top["signals"]["user-correction"] == 2
    assert top["file"] == {"exists": True, "lines": 1031, "generated": True}
    recs = {r["id"]: r["detail"] for r in top["recommendations"]}
    assert list(recs)[:2] == ["edit-the-source-not-the-output", "split-or-anchor-large-file"]
    assert "stop hand-editing src/shopfront/api/schema_gen.py" in recs["write-down-the-rule"]
    assert top["why"].startswith(
        "12 friction events in all 4 sessions that touched it (6 edit misses, 2 user corrections"
    )
    assert (top["exposure_sessions"], top["friction_rate"]) == (4, 1.0)
    detail = top["score_detail"]
    assert detail["sessions"][0].keys() == {"session_id", "weighted_events", "points"}
    product = 100 * detail["severity"] * detail["recurrence"] * detail["consistency"]
    assert top["score"] == pytest.approx(product, abs=0.2)


def test_story_deprecated_api_corrections_land_on_the_service(result: dict[str, Any]) -> None:
    service = entry(result["hotspots"], SERVICE)
    assert service["sessions"] == 3 and service["confidence"] == "high"
    assert service["signals"] == {
        "user-correction": 2,
        "edit-revert": 2,
        "edit-thrash": 1,
        "probable-correction": 1,
    }
    rule = next(r for r in service["recommendations"] if r["id"] == "write-down-the-rule")
    assert "I told you: PaymentClient only" in rule["detail"]
    quotes = [e["excerpt"] for e in service["evidence"] if e["signal"] == "user-correction"]
    assert any("legacy_charge" in q for q in quotes)
    # legacy.py is named by the human but was never touched by the agent: not a hotspot
    assert "src/shopfront/payments/legacy.py" not in [h["path"] for h in result["hotspots"]]


def test_story_pytest_should_be_make_test(result: dict[str, Any]) -> None:
    top = result["commands"][0]
    # one kind of evidence, however often: medium. (The runs differ in their arguments,
    # so they are not a retry loop either.)
    assert (top["path"], top["kind"], top["confidence"]) == ("pytest", "command", "medium")
    assert top["signals"] == {"command-failure": 7} and top["sessions"] == 4
    first = top["recommendations"][0]
    assert first["id"] == "use-the-working-command"
    assert "5 times the agent then got the same job done with `make test`" in first["detail"]
    assert top["why"].startswith(
        "7 friction events in all 4 sessions that touched it (7 command failures); medium"
    )
    assert any("No module named 'shopfront'" in (e["excerpt"] or "") for e in top["evidence"])
    # `make test` failed once on a real test failure that the agent then fixed: that is the
    # normal red/green loop and it is not reported at all
    assert "make test" not in [c["path"] for c in result["commands"]]
    assert result["exposure"]["command"]["make test"] == 6


def test_exposure_covers_everything_the_agent_touched(result: dict[str, Any]) -> None:
    exposure = result["exposure"]
    assert list(exposure) == ["file", "command", "dir"]
    for values in exposure.values():
        assert list(values) == sorted(values) and all(n >= 1 for n in values.values())
    for kind, section in (("file", "hotspots"), ("command", "commands"), ("dir", "directories")):
        for item in result[section]:
            assert exposure[kind][item["path"]] == item["exposure_sessions"] >= item["sessions"]
            assert item["friction_rate"] == round(item["sessions"] / item["exposure_sessions"], 2)
    # targets the agent used without any friction are listed too
    assert exposure["command"]["make codegen"] == 2
    assert "make codegen" not in [c["path"] for c in result["commands"]]
    # read-only inspection (cat, ls, git status) is not exposure
    assert not {"cat", "ls", "git status", "grep"} & set(exposure["command"])
    # ... as are files: the payments client was read and edited, and nobody had to correct it
    assert exposure["file"]["src/shopfront/payments/client.py"] == 2
    assert "src/shopfront/payments/client.py" not in [h["path"] for h in result["hotspots"]]
    refunds = entry(result["hotspots"], "tests/test_refunds.py")
    assert (refunds["sessions"], refunds["exposure_sessions"], refunds["friction_rate"]) == (
        1, 1, 1.0,
    )  # fmt: skip


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
        assert node["exposure_sessions"] >= node["sessions"] >= 1
        assert node["friction_rate"] == round(node["sessions"] / node["exposure_sessions"], 2)
    for hotspot in result["hotspots"]:
        leaf = nodes[hotspot["path"]]
        assert leaf["exposure_sessions"] == hotspot["exposure_sessions"]
    payments = nodes["src/shopfront/payments"]
    assert (payments["sessions"], payments["exposure_sessions"]) == (1, 2)
    assert tree["exposure_sessions"] == result["totals"]["sessions"]
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


def test_no_excerpts_keeps_no_argument_of_an_unknown_tool(tmp_path: Path) -> None:
    """Command keys are the one thing derived from a command line that survives."""
    secrets = ("hunter2pass", "acmecorp-merger-codename", "prod-eu-9", "bob", "v9.9.9-internal")
    out = tmp_path / "t"
    for n in range(3):
        s = Session(f"s{n}", cwd="/repo").user("go")
        s.bash("./run hunter2pass", "run: bad credentials", exit_code=1)
        s.bash("mytool acmecorp-merger-codename --region prod-eu-9", "boom", exit_code=1)
        s.bash("FOO=hunter2pass deployctl --user bob push prod-eu-9", "denied", exit_code=1)
        s.bash("docker run registry.corp/acmecorp-merger-codename", "no such image", exit_code=125)
        s.bash("scripts/release.sh v9.9.9-internal", "tag exists", exit_code=1)
        s.reject("Bash", {"command": "ssh prod-eu-9 'rm -rf /srv/acmecorp-merger-codename'"})
        s.reject("Bash", {"command": "mytool hunter2pass"}, "never pass hunter2pass on argv")
        s.save(out)
    bare = analyze(
        AnalyzeOptions(
            repo=Path("/repo"),
            transcripts=[out],
            include_git=False,
            now=NOW,
            include_excerpts=False,
        )
    )
    keys = {c["path"] for c in bare["commands"]}
    # well-known programs keep their name; every project-specific name is a stable hash
    assert {k for k in keys if not k.startswith("<")} == {"docker run", "ssh"}
    hashed = sorted(k for k in keys if k.startswith("<"))
    assert len(hashed) == 4 and all(re.fullmatch(r"<tool:[0-9a-f]{6}>", k) for k in hashed)
    assert public_key("mytool") in hashed and public_key("scripts/release.sh") in hashed
    for word in ("mytool", "deployctl", "release", "scripts"):
        assert word not in json.dumps(bare), word
    assert set(bare["exposure"]["command"]) >= keys
    text = json.dumps(bare)
    for secret in secrets:
        assert secret not in text, secret
    with_excerpts = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[out], now=NOW))
    assert {c["path"] for c in with_excerpts["commands"]} == {
        "run",
        "mytool",
        "deployctl",
        "docker run",
        "scripts/release.sh",
        "ssh",
    }
    full = json.dumps(with_excerpts)
    assert "acmecorp-merger-codename" in full  # with excerpts the command line is evidence
    assert "hunter2pass" in full  # (an opaque argument is not something redaction can spot)


def test_healthy_tdd_sessions_produce_no_hotspots(tmp_path: Path) -> None:
    """Five sessions of edit / run tests / red / edit / green are not friction."""
    out = tmp_path / "t"
    for n in range(5):
        s = Session(f"h{n}", cwd="/repo").user(f"implement feature {n} with tests")
        s.grep("old_helper", "/repo/src", found=True)
        for step in range(4):
            s.edit("/repo/src/feat.py", f"a{step}", f"b{step}")
            s.bash("pytest tests/test_feat.py -x", "FAILED test_x\n1 failed", exit_code=1)
        s.edit("/repo/src/feat.py", "z", "y")
        s.bash("pytest tests/test_feat.py -x", "1 passed")
        s.user("Great. No. 2 on the list next: add docs to src/feat.py")
        s.edit("/repo/src/feat.py", "q", "r")
        s.user("Undo support is the next feature, please add it")
        s.edit("/repo/src/feat.py", "q2", "r2")
        s.user("thanks!")
        s.save(out)
    result = analyze(
        AnalyzeOptions(repo=Path("/repo"), transcripts=[out], include_git=False, now=NOW)
    )
    assert result["totals"]["sessions"] == 5
    assert result["totals"]["friction_events"] == result["totals"]["corrections"] == 0
    assert result["hotspots"] == result["commands"] == result["directories"] == []
    assert result["exposure"] == {
        "file": {"src/feat.py": 5},
        "command": {"pytest": 5},
        "dir": {"src": 5},
    }


def test_only_unambiguous_corrections_are_quoted(result: dict[str, Any]) -> None:
    text = json.dumps([h["recommendations"] for h in result["hotspots"]])
    assert "I told you: PaymentClient only" in text
    # the "probable" correction (two weaker cues) counts, but is not put in anyone's mouth
    assert "Don't call legacy_charge() here" not in text
    probable = [
        e
        for h in story(max_evidence=50)["hotspots"]
        for e in h["evidence"]
        if e["signal"] == "probable-correction" and "confidence 0.4" in e["summary"]
    ]
    assert probable and all(e["excerpt"] is None for e in probable)
    assert "Don't call legacy_charge" not in json.dumps(story(max_evidence=50))


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
    assert small["totals"]["files_affected"] == 5  # totals are not truncated
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
    total = story()["totals"]["friction_events"]
    assert late["totals"]["friction_events"] + early["totals"]["friction_events"] == total == 34
    nothing = story(since=datetime(2027, 1, 1))
    assert nothing["totals"]["sessions"] == 0 and nothing["hotspots"] == []
    assert nothing["window"]["first_event"] is None and nothing["tree"]["children"] == []


def test_no_transcripts_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(NoTranscriptsError, match="No Claude Code transcripts found") as info:
        analyze(AnalyzeOptions(repo=tmp_path))
    assert isinstance(info.value, AgentFrictionError) and "CLAUDE_CONFIG_DIR" in str(info.value)
    with pytest.raises(NoTranscriptsError, match="Transcript path does not exist"):
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
        "skipped_events": 0, "ignored_lines": 0, "available": False,
        "note": out["sources"][-1]["note"],
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
    # the two agent commits count as places the agent went, but git evidence alone never
    # raises confidence: one transcript session is still "low"
    assert hotspot["sessions"] == hotspot["exposure_sessions"] == 3
    assert hotspot["confidence"] == "low"
    assert "learn-from-rework" in [r["id"] for r in hotspot["recommendations"]]
    rework = next(e for e in hotspot["evidence"] if e["signal"] == "human-rework")
    assert (rework["source"], rework["excerpt"]) == ("git", "human fix-up")
    assert re.fullmatch(r"[0-9a-f]{8}", rework["line"])  # the human commit, not "line 0"
    assert rework["line"] in rework["summary"]
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


def test_misleading_replacements_are_not_recommended(tmp_path: Path) -> None:
    out = tmp_path / "t"
    for n in range(3):
        s = Session(f"i{n}", cwd="/repo").user("run the tests")
        s.bash("pytest", "bash: pytest: command not found", exit_code=127)
        s.bash("pip install pytest", "Successfully installed pytest-8")
        s.bash("pytest", "3 passed")
        s.bash("npm test", "sh: 1: jest: not found", exit_code=127)
        s.bash("git add -A && git commit -m wip", "[main 1a2b3c4] wip")
        s.save(out)
    result = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[out], include_git=False))
    recs = {c["path"]: c["recommendations"] for c in result["commands"]}
    assert [r["id"] for r in recs["pytest"]] == ["document-the-setup-step"]
    assert "3 times it worked after the agent ran `pip install`" in recs["pytest"][0]["detail"]
    assert [r["id"] for r in recs["npm test"]] == ["document-the-command"]
    assert "use `" not in json.dumps(recs)


def test_new_task_prompts_do_not_make_a_hotspot(tmp_path: Path) -> None:
    out = tmp_path / "t"
    prompts = (
        "Fix the bug where the app crashes again after login",
        "The customer says it's not working. Investigate the billing module.",
        "revert the migration tool design doc to draft status",
    )
    for n, text in enumerate(prompts):
        s = Session(f"n{n}", cwd="/repo").user("add the feature")
        s.edit("/repo/src/a.py").say("Done.").tick(600).user(text)
        s.save(out)
    result = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[out], include_git=False))
    assert result["totals"]["friction_events"] == 0 and result["hotspots"] == []


def test_list_transcripts_matches_what_analyze_reads(tmp_path: Path) -> None:
    out = tmp_path / "t"
    first = Session("a", cwd="/repo").user("go").edit("/repo/a.py").save(out)
    second = Session("b", cwd="/repo").user("go").edit("/repo/a.py").save(out / "nested")
    (out / "notes.txt").write_text("not a transcript")
    options = AnalyzeOptions(repo=Path("/repo"), transcripts=[out], include_git=False)
    assert list_transcripts(options) == sorted([first, second])
    assert sum(src["files"] for src in analyze(options)["sources"]) == 2

    prose = tmp_path / "notes.md"
    prose.write_text("# notes\n\nnothing to see\n")
    for bad, message in ((prose, "is not a JSONL transcript"), (tmp_path / "nope", "not exist")):
        options = AnalyzeOptions(repo=Path("/repo"), transcripts=[first, bad])
        with pytest.raises(NoTranscriptsError, match=message):
            list_transcripts(options)
        with pytest.raises(NoTranscriptsError, match=message):
            analyze(options)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NoTranscriptsError, match=r"No \.jsonl transcript files"):
        list_transcripts(AnalyzeOptions(repo=Path("/repo"), transcripts=[empty]))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_commits_made_during_a_session_are_not_extra_sessions(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

    def commit(stamp: str, message: str, files: dict[str, str]) -> None:
        for name, content in files.items():
            (repo / name).write_text(content)
        dated = {**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        identity = ["-c", "user.name=Dana", "-c", "user.email=dana@corp.test"]
        for args in (["add", "-A"], ["commit", "-q", "-m", message]):
            subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, env=dated)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    agent = "wip\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    # three agent commits made while the session below was running, one long before it
    commit("2026-10-01T12:00:00+00:00", agent, {"a.py": "v0\n", "b.py": "v0\n"})
    for minute in (1, 2, 3):
        commit(f"2026-11-03T00:0{minute}:00+00:00", agent, {"a.py": f"v{minute}\n"})
    commit("2026-11-04T12:00:00+00:00", "fix the agent's change", {"a.py": "human\n"})

    s = Session("t", cwd=str(repo), start=datetime(2026, 11, 3, tzinfo=timezone.utc)).user("go")
    s.edit(str(repo / "a.py"), error=builders.EDIT_NOT_FOUND).read(str(repo / "b.py"))
    s.tick(240).edit(str(repo / "a.py"))
    options = AnalyzeOptions(repo=repo, transcripts=[s.save(tmp_path / "t")], now=NOW)
    with_git = analyze(options)
    (hotspot,) = with_git["hotspots"]
    # the rework is on the commit this very session made: still one session, no commits
    assert hotspot["signals"] == {"edit-miss": 1, "human-rework": 1}
    assert (hotspot["sessions"], hotspot["agent_commits"], hotspot["exposure_sessions"]) == (
        1,
        0,
        1,
    )
    assert "in the only session that touched it (" in hotspot["why"]
    assert with_git["exposure"]["file"] == {"a.py": 1, "b.py": 1}  # commits add no exposure
    options.include_git = False
    without_git = analyze(options)
    assert hotspot["score"] >= without_git["hotspots"][0]["score"]  # git never dilutes


def test_settings_describe_the_scan(result: dict[str, Any]) -> None:
    assert result["settings"] == {"excerpts": True, "git": False, "key_mode": "plain"}
    bare = story(include_excerpts=False)
    assert bare["settings"] == {"excerpts": False, "git": False, "key_mode": "pseudonymized"}
    assert list(result).index("settings") < list(result).index("window")


def test_repo_root_only_abbreviates_the_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, other = tmp_path / "someone", tmp_path / "root"
    repo = home / "x"
    repo.mkdir(parents=True)
    other.mkdir()
    transcript = Session("a", cwd=str(repo)).user("go").edit(str(repo / "a.py"))
    path = transcript.save(tmp_path / "t")
    options = AnalyzeOptions(repo=repo, transcripts=[path], include_git=False)
    for var in ("HOME", "USERPROFILE"):  # Path.home() reads USERPROFILE on Windows
        monkeypatch.setenv(var, str(other))
    root = analyze(options)["repo"]["root"]
    # Not abbreviated relative to the fake home. (On Windows the temp dir itself lives
    # under C:\Users\<runner>, which is masked as a home-like path, so only check the tail.)
    assert root != "~/x" and root.endswith("someone/x")
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    assert analyze(options)["repo"]["root"] == "~/x"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_only_analysis_when_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    repo = tmp_path / "proj"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    for day, message, content in (
        (1, "feat\n\nCo-Authored-By: Claude <noreply@anthropic.com>", "v1\n"),
        (2, "fix the agent's change", "v2\n"),
    ):
        (repo / "a.py").write_text(content)
        stamp = f"2026-11-0{day}T12:00:00+00:00"
        dated = {**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        identity = ["-c", "user.name=Dana", "-c", "user.email=dana@corp.test"]
        for args in (["add", "-A"], ["commit", "-q", "-m", message]):
            subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, env=dated)
    with pytest.raises(NoTranscriptsError):
        analyze(AnalyzeOptions(repo=repo, now=NOW))
    out = analyze(AnalyzeOptions(repo=repo, now=NOW, allow_no_transcripts=True))
    assert out["totals"]["sessions"] == 0 and out["settings"]["git"] is True
    (hotspot,) = out["hotspots"]
    assert hotspot["signals"] == {"human-rework": 1} and hotspot["confidence"] == "low"
    assert (hotspot["sessions"], hotspot["agent_commits"]) == (1, 1)
    assert hotspot["why"].startswith("1 friction event in 1 agent commit (")
    # nothing at all to analyse is still an error
    with pytest.raises(NoTranscriptsError):
        analyze(AnalyzeOptions(repo=tmp_path, now=NOW, allow_no_transcripts=True))
    with pytest.raises(NoTranscriptsError):
        analyze(AnalyzeOptions(repo=repo, allow_no_transcripts=True, include_git=False))


def test_ordinary_sessions_get_no_advice_about_commands(tmp_path: Path) -> None:
    """Three sessions of red -> edit -> green, with the green run via `make test`."""
    out = tmp_path / "t"
    for n in range(3):
        s = Session(f"o{n}", cwd="/repo").user("fix the total")
        s.read("/repo/tests/test_a.py")
        s.bash(
            "pytest tests/test_a.py", "FAILED tests/test_a.py::test_total\n1 failed", exit_code=1
        )
        s.edit("/repo/src/a.py").bash("make test", "5 passed")
        s.save(out)
    result = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[out], include_git=False))
    assert result["totals"]["friction_events"] == 0
    assert result["hotspots"] == result["commands"] == []


def test_idle_humans_do_not_inflate_minutes_lost(tmp_path: Path) -> None:
    out = tmp_path / "t"
    for n in range(3):
        s = Session(f"w{n}", cwd="/repo").user("run the tests")
        s.bash("pytest", "bash: pytest: command not found", exit_code=127, took=2)
        s.say("pytest is not installed here.").tick(3600).user("ok, thanks")
        s.save(out)
    result = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[out], include_git=False))
    assert result["totals"]["est_minutes_lost"] <= 0.5
    assert result["commands"][0]["est_minutes_lost"] <= 0.5


def test_permission_denials_get_their_own_advice(tmp_path: Path) -> None:
    out = tmp_path / "t"
    for n in range(3):
        s = Session(f"p{n}", cwd="/repo").user("go")
        s.tool("Bash", {"command": "pytest tests/"}, DENIED, is_error=True)
        s.save(out)
    result = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[out], include_git=False))
    (command,) = result["commands"]
    assert command["signals"] == {"permission-denied": 3} and command["path"] == "pytest"
    assert [r["id"] for r in command["recommendations"]] == ["allow-or-document-the-boundary"]
    assert "3 permission denials" in command["why"]
    assert next(s for s in result["signals"] if s["id"] == "command-failure")["count"] == 0


DENIED = "Claude requested permissions to use Bash, but you haven't granted it yet."


def test_control_characters_from_transcripts_never_reach_the_result(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    evil = "\x1b[2J\x1b]0;pwned\x07‮"
    s = Session("ctl", cwd=str(repo)).user("go")
    s.edit(f"{repo}/src/a‮b.py", error=builders.EDIT_NOT_FOUND)
    s.bash(f"./run.sh --tag '{evil}'", f"bash: ./run.sh: {evil}command not found", exit_code=127)
    s.edit(f"{repo}/src/c.py").user(f"No, you broke c.py {evil} revert that")
    s.save(tmp_path / "t")
    out = analyze(
        AnalyzeOptions(repo=repo, transcripts=[tmp_path / "t"], include_git=False, now=NOW)
    )
    text = json.dumps(out, ensure_ascii=False)
    assert out["totals"]["friction_events"] == 3 and out["totals"]["corrections"] == 1
    for bad in ("\x1b", "\x07", "‮", "\\u001b", "\\u202e"):
        assert bad not in text, repr(bad)
    assert "src/ab.py" in [h["path"] for h in out["hotspots"]]


def test_detector_errors_are_counted_per_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Session("odd", cwd=str(repo)).user("go")
    s.bash("pytest -q", "bash: pytest: command not found", exit_code=127)
    s.edit(f"{repo}/src/a.py", error=builders.EDIT_NOT_FOUND)
    s.save(tmp_path / "t")

    def boom(self: object, action: object, result: object) -> None:
        raise RuntimeError("unexpected transcript shape")

    monkeypatch.setattr(signals._Detector, "_on_shell", boom)
    out = analyze(
        AnalyzeOptions(repo=repo, transcripts=[tmp_path / "t"], include_git=False, now=NOW)
    )
    assert out["sources"][0]["skipped_events"] == 1
    assert out["totals"]["friction_events"] == 1  # the rest of the session still counts


# ---- round 6 ------------------------------------------------------------------------------


def _scan(tmp_path: Path, sessions: list[Session], **options: Any) -> dict[str, Any]:
    for index, session in enumerate(sessions):
        session.save(tmp_path / "t", f"s{index}")
    return analyze(
        AnalyzeOptions(
            repo=tmp_path / "repo",
            transcripts=[tmp_path / "t"],
            include_git=False,
            now=NOW,
            **options,
        )
    )


def test_productive_work_stays_below_the_incident_band(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "util.py").write_text("x = 1\n")
    sessions = []
    for i in range(5):
        s = Session(f"n{i}", cwd=str(repo)).user(f"implement feature {i}")
        s.grep(f"feature_{i}", f"{repo}/src").grep(f"Feature{i}Handler", f"{repo}/src")
        s.read(f"{repo}/src/app.py")
        for k in range(8):
            s.edit(f"{repo}/src/app.py", f"a{k}", f"b{k}")
        s.bash("pytest", "8 passed")
        for _ in range(3):
            s.read(f"{repo}/src/util.py")
        s.user("Actually, can you use a dataclass instead").user("great, thanks")
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    rows = [*out["hotspots"], *out["commands"], *out["directories"]]
    assert rows and out["totals"]["corrections"] == 0
    assert "edit-thrash" not in json.dumps(out["hotspots"])
    for row in rows:
        assert row["score"] <= 19.0, row["path"]
        assert row["recommendations"] == [] and row["score_detail"]["capped"] is True
        assert "capped at 19" in row["why"]
    assert "(1 lines)" not in json.dumps(out)


def test_probable_corrections_never_become_a_claim(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = []
    for i in range(4):
        s = Session(f"p{i}", cwd=str(repo)).user(f"implement step {i}")
        s.edit(f"{repo}/src/app.py").bash("pytest", "1 passed").say("Done.")
        s.user("Actually we should have a retry here instead, can you add it")
        s.write(f"{repo}/src/helper{i}.py", "x")
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    (row,) = out["hotspots"]
    assert out["totals"]["corrections"] == 0 and out["totals"]["probable_corrections"] == 4
    assert row["signals"] == {"probable-correction": 4} and row["score"] <= 19.0
    assert row["recommendations"] == [] and "corrected" not in json.dumps(out["hotspots"])
    listed = {s["id"]: s["count"] for s in out["signals"]}
    assert listed["probable-correction"] == 4 and listed["user-correction"] == 0


def test_sources_never_show_the_encoded_project_directory(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = tmp_path / "projects" / "-Users-alice-work-acme-repo"
    s = Session("11111111-aaaa", cwd=str(repo)).user("go")
    s.edit(f"{repo}/a.py", error=builders.EDIT_NOT_FOUND)
    s.save(project)
    options = AnalyzeOptions(repo=repo, transcripts=[project], include_git=False, now=NOW)
    out = analyze(options)
    assert "alice" not in json.dumps(out) and "acme" not in json.dumps(out)
    assert out["sources"][0]["path"] == "claude-code:repo"
    (evidence,) = out["hotspots"][0]["evidence"]
    assert evidence["source"] == "claude-code:repo/11111111-aaaa.jsonl" and evidence["line"] > 0

    bare = analyze(dataclasses.replace(options, include_excerpts=False))
    (evidence,) = bare["hotspots"][0]["evidence"]
    assert evidence["source"] is None and evidence["session_id"] == "11111111-aaaa"
    assert evidence["line"] > 0 and bare["sources"][0]["path"] == "claude-code:repo"


def test_one_home_rule_for_every_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home" / "alice"
    repo = home / "work" / "repo"
    repo.mkdir(parents=True)
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    rows = [
        {"session": "g", "kind": "user_message", "text": "go", "cwd": str(repo)},
        {"session": "g", "kind": "tool_call", "id": 1, "op": "edit", "path": "a.py"},
        {"session": "g", "kind": "tool_result", "id": 1, "ok": False, "error": "edit_not_found"},
    ]
    log = home / "logs" / "agent.jsonl"
    log.parent.mkdir()
    log.write_text("".join(json.dumps(row) + "\n" for row in rows))
    out = analyze(AnalyzeOptions(repo=repo, transcripts=[log], include_git=False, now=NOW))
    assert out["repo"]["root"] == "~/work/repo"
    assert out["sources"][0]["path"] == "~/logs/agent.jsonl"
    assert out["hotspots"][0]["evidence"][0]["source"] == "~/logs/agent.jsonl"
    assert "alice" not in json.dumps(out)


def test_a_resumed_session_is_one_session(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = Session("first", cwd=str(repo)).user("go")
    first.edit(f"{repo}/a.py", error=builders.EDIT_NOT_FOUND)
    resumed = Session("second", cwd=str(repo))
    for line in first.lines:  # the new file replays the old lines under a new session id
        resumed.raw({**json.loads(line), "sessionId": "second"})
    resumed.tick(3600).user("continue").edit(f"{repo}/a.py", error=builders.EDIT_NOT_FOUND)
    first.save(tmp_path / "t", "a-first")
    resumed.save(tmp_path / "t", "b-second")
    out = analyze(
        AnalyzeOptions(repo=repo, transcripts=[tmp_path / "t"], include_git=False, now=NOW)
    )
    (row,) = out["hotspots"]
    assert out["totals"]["sessions"] == row["sessions"] == out["sources"][0]["sessions"] == 1
    assert row["events"] == 2 and out["sources"][0]["files"] == 2


def test_inspect_transcripts_reports_what_each_file_held(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    good = Session("good", cwd=str(repo)).user("go").edit(f"{repo}/a.py")
    good.save(tmp_path / "t")
    (tmp_path / "t" / "empty.jsonl").write_text("")
    (tmp_path / "t" / "binary.jsonl").write_bytes(b"\x00\xff\x00{not json\n" * 3)
    (tmp_path / "t" / "partly.jsonl").write_text(good.text() + "{broken\n")
    options = AnalyzeOptions(repo=repo, transcripts=[tmp_path / "t"], include_git=False)
    report = {
        path.name: (events, skipped) for path, events, skipped in inspect_transcripts(options)
    }
    assert report["good.jsonl"] == (3, 0)
    assert report["empty.jsonl"] == (0, 0)
    assert report["binary.jsonl"] == (0, 3)
    assert report["partly.jsonl"][1] == 1


def test_probes_and_snippets_do_not_make_a_command_hotspot(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    sessions = []
    for i in range(10):
        s = Session(f"q{i}", cwd=str(tmp_path / "repo")).user(f"task {i}")
        s.bash("gh pr view", "no pull requests found", exit_code=1)
        s.bash("git diff --quiet", "", exit_code=1)
        s.bash("command -v docker", "", exit_code=1)
        s.bash(f"python -c 'import json; print(json.loads(\"{i}\")[0])'", "TypeError", exit_code=1)
        s.bash("git stash pop", "No stash entries found.", exit_code=1)
        s.bash("cd /tmp/scratch && python repro.py", "sh: 1: python: not found", exit_code=127)
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    assert sorted(c["path"] for c in out["commands"]) == ["git stash pop", "python <inline>"]
    for row in out["commands"]:
        assert row["score"] <= 19.0 and row["score_detail"]["capped"] is True
        assert row["recommendations"] == []


def test_time_lost_after_a_failed_edit_is_one_round_trip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = []
    for i in range(3):
        s = Session(f"e{i}", cwd=str(repo)).user("go")
        s.edit(f"{repo}/src/app.py", error=builders.EDIT_NOT_FOUND)
        for k in range(8):
            s.edit(f"{repo}/src/util.py", f"a{k}", f"b{k}").bash("pytest", "ok", took=30)
        s.edit(f"{repo}/src/app.py", "x", "y")
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    assert out["totals"]["est_minutes_lost"] < 1.0


def test_probable_corrections_are_never_quoted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Session("pq", cwd=str(repo)).user("implement it")
    s.edit(f"{repo}/src/app.py").say("Done.")
    s.user("Actually we should have a retry here instead, can you add it")
    s.write(f"{repo}/src/helper.py", "x")
    out = _scan(tmp_path, [s])
    evidence = [e for h in out["hotspots"] for e in h["evidence"]]
    assert [e["signal"] for e in evidence] == ["probable-correction"]
    assert evidence[0]["excerpt"] is None and "retry here" not in json.dumps(out)


def test_drive_letter_aliases_ignore_case(tmp_path: Path) -> None:
    repo = tmp_path / "shop"
    repo.mkdir()
    s = Session("win", cwd="c:\\users\\dev\\shop").user("go")
    s.edit("C:\\Users\\Dev\\Shop\\src\\a.py", error=builders.EDIT_NOT_FOUND)
    s.edit("c:/users/dev/shop/src/a.py", error=builders.EDIT_NOT_FOUND)
    out = analyze(
        AnalyzeOptions(
            repo=repo,
            transcripts=[s.save(tmp_path / "t")],
            repo_aliases=["C:\\Users\\Dev\\Shop"],
            include_git=False,
        )
    )
    assert [(h["path"], h["events"]) for h in out["hotspots"]] == [("src/a.py", 2)]


def test_the_same_generic_log_twice_is_one_set_of_events(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = [
        {"session": "g1", "ts": "2026-11-05T16:00:00Z", "kind": "user_message", "text": "go"},
        {"session": "g1", "ts": "2026-11-05T16:00:01Z", "kind": "tool_call", "id": 1, "op": "shell",
         "command": "make build", "cwd": str(repo)},
        {"session": "g1", "ts": "2026-11-05T16:00:02Z", "kind": "tool_result", "id": 1,
         "exit_code": 127, "output": "sh: 1: make: not found"},
    ]  # fmt: skip
    text = "\n".join(json.dumps(line) for line in lines) + "\n"
    for name in ("a.jsonl", "backup/a.jsonl"):
        (tmp_path / "t" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "t" / name).write_text(text)
    out = analyze(
        AnalyzeOptions(repo=repo, transcripts=[tmp_path / "t"], include_git=False, now=NOW)
    )
    assert out["totals"]["friction_events"] == 1 and out["totals"]["sessions"] == 1


# ---- round 8 --------------------------------------------------------------------------------


def _make_build_score(tmp_path: Path, cwd: str, command: str) -> list[tuple[str, float]]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    cwd = cwd.replace("PARENT", str(tmp_path)).replace("REPO", str(repo))
    # A shell takes the path as typed; on Windows agents write it with forward slashes.
    command = command.replace("REPO", repo.as_posix())
    s = Session("b1", cwd=cwd).user("build it")
    s.bash(command, "make: *** No rule to make target 'build'.  Stop.", exit_code=2)
    out = _scan(tmp_path, [s])
    return [(c["path"], c["score"]) for c in out["commands"]]


def test_a_session_started_in_a_parent_directory_is_seen_through_its_cd(tmp_path: Path) -> None:
    inside = _make_build_score(tmp_path, "REPO", "make build")
    assert inside and inside[0][0] == "make build"
    assert _make_build_score(tmp_path, "PARENT", "cd repo && make build") == inside
    assert _make_build_score(tmp_path, "PARENT", "cd REPO && make build") == inside


def test_a_session_that_never_enters_the_repo_is_still_left_out(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    s = Session("b1", cwd=str(tmp_path)).user("build it")
    s.bash("cd elsewhere && make build", "make: *** No rule to make target", exit_code=2)
    s.save(tmp_path / "t", "s0")
    out = analyze(
        AnalyzeOptions(
            repo=tmp_path / "repo",
            transcripts=[tmp_path / "t"],
            include_git=False,
            now=NOW,
        )
    )
    assert out["totals"]["sessions"] == 0 and out["commands"] == []


def _stuck_sessions(repo: Path, count: int) -> list[Session]:
    sessions = []
    for n in range(count):
        s = Session(f"stuck{n}", cwd=str(repo)).user("make the test pass")
        for i in range(8):
            s.edit(f"{repo}/src/app.py", f"a{i}", f"b{i}")
            s.bash("pytest tests/test_app.py", "1 failed", exit_code=1, took=60)
        sessions.append(s)
    return sessions


def test_work_that_never_converges_is_a_hotspot_with_time_lost(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_app.py").write_text("def test_app(): ...\n")
    out = _scan(tmp_path, _stuck_sessions(repo, 6))
    assert out["totals"]["est_minutes_lost"] > 40
    command = entry(out["commands"], "pytest")
    source = entry(out["hotspots"], "src/app.py")
    for row in (command, source):
        assert row["signals"]["no-convergence"] == 6 and row["score"] > 45
        assert row["score_detail"]["capped"] is False and row["est_minutes_lost"] > 40
    (rec,) = command["recommendations"]
    assert rec["id"] == "explain-the-failing-check"
    assert rec["title"] == "Agents cannot get `pytest` to pass"
    advice = {r["id"]: r for r in source["recommendations"]}
    assert "`pytest`" in advice["explain-the-failing-check"]["detail"]


def test_one_stuck_session_stays_capped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _scan(tmp_path, _stuck_sessions(repo, 1))
    command = entry(out["commands"], "pytest")
    assert command["signals"]["no-convergence"] == 1 and command["score"] <= 19.0
    assert command["score_detail"]["capped"] is True and command["recommendations"] == []


def test_routine_probes_do_not_become_a_pattern(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = []
    for i in range(5):
        s = Session(f"r{i}", cwd=str(repo)).user(f"task {i}")
        s.read(f"{repo}/CLAUDE.md", missing=True).read(f"{repo}/.env", missing=True)
        s.edit(f"{repo}/src/app.py", "old_name()", "new_name()").grep("old_name", str(repo))
        s.reject("ExitPlanMode", {"plan": "ship it"}, "split it in two steps")
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    assert out["totals"]["friction_events"] == 10 and out["directories"] == []
    assert {s["id"]: s["count"] for s in out["signals"]}["tool-rejected"] == 0
    for row in out["hotspots"]:
        assert row["signals"] == {"path-miss": 5} and row["score"] <= 19.0
        assert row["recommendations"] == []


def test_events_before_the_window_are_context_not_counts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Session("w", cwd=str(repo), start=datetime(2026, 11, 2, 23, 50, tzinfo=timezone.utc))
    s.user("add the client").edit(f"{repo}/src/app.py", error=builders.EDIT_NOT_FOUND)
    s.edit(f"{repo}/src/app.py").say("Done.")
    s.now = datetime(2026, 11, 3, 0, 5, tzinfo=timezone.utc)
    s.user("No, you broke the import in src/app.py").edit(f"{repo}/src/app.py", "x", "y")
    whole = _scan(tmp_path, [s])
    assert whole["totals"]["friction_events"] == 2 and whole["totals"]["corrections"] == 1
    late = _scan(tmp_path, [s], since=datetime(2026, 11, 3, tzinfo=timezone.utc))
    # the correction is in the window and still knows which file it is about ...
    assert late["totals"]["corrections"] == 1 and late["totals"]["friction_events"] == 1
    assert [h["path"] for h in late["hotspots"]] == ["src/app.py"]
    assert late["hotspots"][0]["signals"] == {"user-correction": 1}  # ... the miss is not counted
    assert late["totals"]["tool_calls"] == 1


# ---- round 9 --------------------------------------------------------------------------------


def test_the_headline_case_survives_a_pipe_to_tail(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("test:\n\tpytest\n")
    sessions = []
    for i in range(4):
        s = Session(f"pipe{i}", cwd=str(repo)).user("run the tests")
        s.bash("pytest -q 2>&1 | tail -5", "bash: pytest: command not found")
        s.bash("python -m pytest -q 2>&1 | tail -5", "/usr/bin/python: No module named pytest")
        s.bash("make test 2>&1 | tail -5", "5 passed in 0.3s")
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    assert out["totals"]["friction_events"] == 8
    for path in ("pytest", "python -m pytest"):
        row = entry(out["commands"], path)
        assert row["signals"] == {"command-failure": 4} and row["score"] > 19
        (rec,) = row["recommendations"]
        assert rec["id"] == "use-the-working-command" and "`make test`" in rec["title"]
    assert "use `python -m pytest`" not in json.dumps(out)


def test_evidence_says_how_many_seconds_it_claims(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Session("sec", cwd=str(repo)).user("go")
    s.bash("pytest", "bash: pytest: command not found", exit_code=127, took=7)
    s.bash("pytest", "1 failed in 0.1s", exit_code=1)
    out = _scan(tmp_path, [s])
    (row,) = out["commands"]
    seconds = [e["est_seconds_lost"] for e in row["evidence"]]
    assert sorted(seconds) == [0.0, pytest.approx(11.0)]  # 7s to run + 4s until the next call
    assert row["est_minutes_lost"] == pytest.approx(sum(seconds) / 60, abs=0.06)


def test_packages_of_a_monorepo_get_their_own_command_rows(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = []
    for i in range(3):
        s = Session(f"m{i}", cwd=str(repo)).user("test it")
        s.bash("cd web && npm test", "sh: 1: jest: not found", exit_code=127)
        s.bash("cd api && npm test", "8 passing")
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    assert [c["path"] for c in out["commands"]] == ["npm test (web/)"]
    assert out["exposure"]["command"]["npm test (api/)"] == 3
    hidden = _scan(tmp_path, sessions, include_excerpts=False)
    assert [c["path"] for c in hidden["commands"]] == ["npm test (web/)"]


# ---- round 10 -------------------------------------------------------------------------------


def test_capped_rows_are_low_confidence_and_claim_no_time(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = []
    for i in range(4):
        s = Session(f"w{i}", cwd=str(repo)).user(f"task {i}")
        s.grep(f"Feature{i}", f"{repo}/src").read(f"{repo}/.env", missing=True)
        s.bash("mypy", "Found 2 errors in 1 file (checked 3 source files)", exit_code=1)
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    rows = [*out["hotspots"], *out["commands"], *out["directories"]]
    assert len(rows) == 3 and out["totals"]["est_minutes_lost"] == 0
    for row in rows:
        assert row["score_detail"]["capped"] is True and row["confidence"] == "low"
        assert row["est_minutes_lost"] == 0 and "low confidence" in row["why"]

    stuck = _scan(tmp_path / "one", _stuck_sessions(tmp_path / "one" / "repo", 1))
    assert stuck["totals"]["est_minutes_lost"] == 0  # one stuck session: a lead, no minutes
    assert entry(stuck["commands"], "pytest")["confidence"] == "low"


def test_no_convergence_time_is_the_red_runs_not_the_span(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = []
    for n in range(3):
        s = Session(f"t{n}", cwd=str(repo)).user("fix the bug")
        for i in range(5):
            s.edit(f"{repo}/src/app.py", f"a{i}", f"b{i}").tick(120)  # two minutes of thinking
            s.bash("pytest -x", "1 failed in 3s", exit_code=1, took=30)
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    row = entry(out["commands"], "pytest")
    assert row["score_detail"]["capped"] is False
    assert 7.5 <= row["est_minutes_lost"] <= 9  # 3 x 5 red runs of 30 s, plus the edits between
    (stretch,) = [e for e in row["evidence"] if e["signal"] == "no-convergence"][:1]
    assert 150 <= stretch["est_seconds_lost"] <= 180


# ---- round 11 -------------------------------------------------------------------------------


def test_a_session_of_prose_with_no_location_touched_no_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ts = "2026-11-05T16:00:0{}Z"
    lines = [
        {"session": "chat", "ts": ts.format(0), "kind": "user_message", "text": "what is a monad"},
        {"session": "chat", "ts": ts.format(1), "kind": "assistant_message", "text": "a monoid"},
        # no cwd either, but a tool call: as before, it is taken to have run here
        {"session": "work", "ts": ts.format(2), "kind": "user_message", "text": "go"},
        {"session": "work", "ts": ts.format(3), "kind": "tool_call", "id": 1, "op": "shell",
         "command": "make build"},
        {"session": "work", "ts": ts.format(4), "kind": "tool_result", "id": 1,
         "exit_code": 127, "output": "sh: 1: make: not found"},
        # prose only, but recorded in the repo: counted
        {"session": "here", "ts": ts.format(5), "kind": "user_message", "text": "hello",
         "cwd": str(repo)},
    ]  # fmt: skip
    log = tmp_path / "log.jsonl"
    log.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    out = analyze(AnalyzeOptions(repo=repo, transcripts=[log], include_git=False, now=NOW))
    assert out["totals"]["sessions"] == 2
    assert out["sources"][0]["sessions_outside_repo"] == 1


# ---- round 12 -------------------------------------------------------------------------------

PG_DOWN = (
    'E   psycopg2.OperationalError: connection to server at "localhost" (127.0.0.1), '
    "port 5432 failed: Connection refused\n12 errors in 1.31s"
)


def _sessions(tmp_path: Path, count: int, body: Any) -> list[Session]:
    (tmp_path / "repo").mkdir(parents=True, exist_ok=True)
    sessions = []
    for i in range(count):
        s = Session(f"r{i}", cwd=str(tmp_path / "repo")).user(f"task {i}")
        body(s)
        sessions.append(s)
    return sessions


def _recommended(out: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r["id"], r["title"]) for c in out["commands"] for r in c["recommendations"]]


def test_fail_setup_pass_in_every_session_is_the_headline(tmp_path: Path) -> None:
    def body(s: Session) -> None:
        s.bash("pytest tests/test_a.py", PG_DOWN, exit_code=1, took=20)
        s.bash("docker compose up -d db", "Container db  Started", took=15)
        s.bash("pytest tests/test_a.py", "12 passed in 3.1s")

    out = _scan(tmp_path, _sessions(tmp_path, 4, body))
    (row,) = out["commands"]
    assert (row["path"], row["events"], row["confidence"]) == ("pytest", 4, "medium")
    assert row["score_detail"]["capped"] is False and row["est_minutes_lost"] > 0
    (rule,) = row["recommendations"]
    assert rule["id"] == "document-the-setup-step"
    assert "run `docker compose up` before `pytest`" in rule["detail"]
    assert out["hotspots"] == []  # the test file is not blamed for a database that is down


def test_a_command_that_fixed_it_once_is_a_coincidence_twice_is_a_setup_step(
    tmp_path: Path,
) -> None:
    def body(s: Session) -> None:
        s.bash("pytest", "1 failed in 1s", exit_code=1, took=30)
        s.bash("./scripts/reset-db.sh", "ok").bash("pytest", "4 passed")

    once = _scan(tmp_path / "one", _sessions(tmp_path / "one", 1, body))
    assert once["totals"]["friction_events"] == 0 and once["commands"] == []
    assert once["totals"]["dropped_red_runs"] == 1 and once["totals"]["dropped_minutes"] == 0.5
    often = _scan(tmp_path / "many", _sessions(tmp_path / "many", 3, body))
    (row,) = often["commands"]
    assert row["events"] == 3 and row["score_detail"]["capped"] is False
    assert _recommended(often) == [("document-the-setup-step", "Document the setup `pytest` needs")]
    assert "`scripts/reset-db.sh`" in row["recommendations"][0]["detail"]
    assert often["totals"]["dropped_red_runs"] == 0


def test_red_runs_that_were_not_reported_are_counted(tmp_path: Path) -> None:
    def body(s: Session) -> None:
        s.bash("pytest", "1 failed in 1s", exit_code=1, took=60)  # red, edit, green
        s.edit(str(tmp_path / "repo" / "a.py")).bash("pytest", "4 passed")
        for _ in range(2):  # waiting for a server
            s.bash("curl -sf localhost:8000/health", "", exit_code=7, took=3)
        s.bash("curl -sf localhost:8000/health", "ok")
        s.bash("npm test", "1 failing", exit_code=1, took=600)  # stays red: reported

    out = _scan(tmp_path, _sessions(tmp_path, 2, body))
    assert out["totals"]["dropped_red_runs"] == 6 and out["totals"]["dropped_minutes"] == 2.2
    assert out["totals"]["friction_events"] == 2


def test_a_check_that_stays_red_session_after_session_is_not_capped(tmp_path: Path) -> None:
    def stuck(s: Session) -> None:
        s.bash("npm run e2e", "2 failed", exit_code=1, took=40)
        s.read(str(tmp_path / "repo" / "a.py"))
        s.bash("npm run e2e -- --debug", "2 failed", exit_code=1, took=40)

    def once(s: Session) -> None:
        s.bash("npm run e2e", "2 failed", exit_code=1, took=40)

    for name, count, body, capped in (
        ("three", 3, stuck, False),
        ("two", 2, stuck, True),  # not yet a pattern
        ("single", 4, once, True),  # one red run when a session ends is routine
    ):
        out = _scan(tmp_path / name, _sessions(tmp_path / name, count, body))
        (row,) = out["commands"]
        assert row["score_detail"]["capped"] is capped, name
        assert (row["est_minutes_lost"] > 0) is (not capped), name


def test_environment_failures_get_their_own_advice(tmp_path: Path) -> None:
    cases = {
        "pytest": (PG_DOWN, "Say how to start PostgreSQL before `pytest`"),
        "./scripts/deploy.sh": (
            "Error: AWS credentials expired",
            "Say how `scripts/deploy.sh` gets its credentials",
        ),
        "npm run build": (
            "Error: Missing required environment variable DATABASE_URL",
            "Document the environment variables `npm run build` needs",
        ),
    }

    def body(s: Session) -> None:
        for command, (output, _) in cases.items():
            s.bash(command, output, exit_code=1)

    sessions = _sessions(tmp_path, 3, body)
    out = _scan(tmp_path, sessions)
    assert sorted(_recommended(out)) == sorted(
        ("document-the-environment", title) for _, title in cases.values()
    )
    assert all(c["confidence"] == "medium" and c["est_minutes_lost"] > 0 for c in out["commands"])
    assert "(`DATABASE_URL`)" in json.dumps(out, ensure_ascii=False)
    hidden = _scan(tmp_path, [], include_excerpts=False)
    assert "DATABASE_URL" not in json.dumps(hidden) and len(_recommended(hidden)) == 3
    # ... and a script of the repository stays readable where repo paths are shown anyway
    (tmp_path / "repo" / "scripts").mkdir()
    (tmp_path / "repo" / "scripts" / "deploy.sh").write_text("#!/bin/sh\n")
    named = _scan(tmp_path, [], include_excerpts=False)
    assert "scripts/deploy.sh" in [c["path"] for c in named["commands"]]
    assert "scripts/deploy.sh" not in [c["path"] for c in hidden["commands"]]


def test_advice_names_the_program_or_the_directory_that_worked(tmp_path: Path) -> None:
    def body(s: Session) -> None:
        s.bash("python scripts/gen.py", "bash: python: command not found", exit_code=127)
        s.bash("python3 scripts/gen.py", "generated")
        s.bash("npm test", "npm error enoent Could not read package.json", exit_code=254)
        s.bash("cd web && npm test", "Tests: 3 passed, 3 total")

    (tmp_path / "repo" / "web").mkdir(parents=True)
    out = _scan(tmp_path, _sessions(tmp_path, 2, body))
    assert sorted(_recommended(out)) == [
        ("document-the-command", "Tell agents to run `npm test` in `web/`"),
        ("use-the-working-command", "Tell agents to use `python3`"),
    ]
    details = " ".join(r["detail"] for c in out["commands"] for r in c["recommendations"])
    assert '"use `python3`, not `python`"' in details
    assert '"run `npm test` from `web/`"' in details


def test_half_a_surrogate_pair_in_a_transcript_can_be_written_out(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    s = Session("u", cwd=str(tmp_path / "repo")).user("go \ud83d")
    s.bash("pytest", "bash: pytest: command not found \ud83d", exit_code=127)
    path = tmp_path / "u.jsonl"  # escaped in the file, as a harness writes it
    path.write_text("\n".join(json.dumps(json.loads(line)) for line in s.lines) + "\n")
    assert "\\ud83d" in path.read_text()
    out = analyze(
        AnalyzeOptions(repo=tmp_path / "repo", transcripts=[path], include_git=False, now=NOW)
    )
    text = json.dumps(out, ensure_ascii=False)
    text.encode("utf-8")  # must not raise
    assert "command not found �" in text and out["totals"]["friction_events"] == 1


def test_a_transcript_outside_home_is_not_cited_by_its_absolute_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    line = {"session": "g", "ts": "2026-11-05T16:00:00Z", "kind": "tool_call", "id": 1,
            "op": "shell", "command": "make build", "cwd": str(repo)}  # fmt: skip
    done = {"session": "g", "ts": "2026-11-05T16:00:02Z", "kind": "tool_result", "id": 1,
            "exit_code": 127, "output": "sh: 1: make: not found"}  # fmt: skip
    log = tmp_path / "client-logs" / "nightly" / "run.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps(line) + "\n" + json.dumps(done) + "\n")
    for given, cited in ((tmp_path / "client-logs", "client-logs/nightly/run.jsonl"),
                         (log, "run.jsonl")):  # fmt: skip
        out = analyze(AnalyzeOptions(repo=repo, transcripts=[given], include_git=False, now=NOW))
        source = out["commands"][0]["evidence"][0]["source"]
        # On Windows the temp dir is under C:\Users\<name>, which is masked as home
        # (~/...); anywhere else it is cited relative to what was given.
        assert source == cited or (source.startswith("~/") and source.endswith(cited))
        assert out["sources"][0]["path"] == cited
