from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_friction import __version__, scoring, signals
from agent_friction.attribution import public_key, public_tool
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
from tests.builders import Session, by_signal, detect

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
    # four corroborated corrections; "Actually legacy_charge() is deprecated ..." is only probable
    assert (totals["corrections"], totals["probable_corrections"]) == (4, 1)
    assert all(source["skipped_events"] == 0 for source in result["sources"])
    assert totals["files_affected"] == len(result["hotspots"]) == 5
    assert totals["est_minutes_lost"] > 0


def test_story_generated_schema_is_the_top_file(result: dict[str, Any]) -> None:
    top = result["hotspots"][0]
    assert top["path"] == SCHEMA and top["confidence"] == "high" and top["sessions"] == 4
    assert top["signals"]["edit-miss"] == 6 and top["signals"]["user-correction"] == 2
    assert top["file"] == {
        "exists": True,
        "lines": 1031,
        "generated": True,
        "removed_at": None,
        "renamed_to": None,
    }
    recs = {r["id"]: r["detail"] for r in top["recommendations"]}
    assert list(recs)[:2] == ["edit-the-source-not-the-output", "split-or-anchor-large-file"]
    assert "stop hand-editing src/shopfront/api/schema_gen.py" in recs["write-down-the-rule"]
    assert top["why"].startswith(
        "12 friction events in all 4 sessions that touched it (6 edit misses, 2 user corrections"
    )
    assert (top["exposure_sessions"], top["friction_rate"]) == (4, 1.0)
    detail = top["score_detail"]
    assert detail["sessions"][0].keys() == {"session_id", "episode", "weighted_events", "points"}
    product = 100 * detail["severity"] * detail["recurrence"] * detail["consistency"]
    assert top["score"] == pytest.approx(product, abs=0.2)


def test_story_deprecated_api_corrections_land_on_the_service(result: dict[str, Any]) -> None:
    service = entry(result["hotspots"], SERVICE)
    assert service["sessions"] == 3 and service["confidence"] == "high"
    assert service["signals"] == {
        "user-correction": 2,
        # the second revert answers "you used legacy_charge again": the correction is the
        # event, and taking the change back is not counted next to it
        "edit-revert": 1,
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
    assert "Actually legacy_charge() is deprecated" not in text
    probable = [
        e
        for h in story(max_evidence=50)["hotspots"]
        for e in h["evidence"]
        if e["signal"] == "probable-correction" and "confidence 0.4" in e["summary"]
    ]
    assert probable and all(e["excerpt"] is None for e in probable)
    assert "Actually legacy_charge" not in json.dumps(story(max_evidence=50))


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
    assert late["totals"]["friction_events"] + early["totals"]["friction_events"] == total == 33
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
    # no headline number without evidence: the event is listed, in the evidence shape
    (item,) = out["unattributed"]
    assert set(item) == {
        "signal",
        "session_id",
        "timestamp",
        "summary",
        "excerpt",
        "source",
        "line",
        "est_seconds_lost",
    }
    assert item["session_id"] == "solo" and item["line"] == 4
    # a probable correction is listed but never quoted
    assert (item["signal"], item["excerpt"]) == ("probable-correction", None)


def test_unattributed_evidence_is_capped_and_can_be_stripped(tmp_path: Path) -> None:
    from agent_friction.engine import MAX_UNATTRIBUTED_EVIDENCE

    s = Session("many").user("go")
    for _ in range(MAX_UNATTRIBUTED_EVIDENCE + 5):
        s.reject("Task", {"prompt": "sk-ant-api03-secretsecretsecretsecret"}, "not now")
    options = AnalyzeOptions(repo=Path("/repo"), transcripts=[s.save(tmp_path)], include_git=False)
    out = analyze(options)
    assert out["totals"]["unattributed_events"] == MAX_UNATTRIBUTED_EVIDENCE + 5
    assert len(out["unattributed"]) == MAX_UNATTRIBUTED_EVIDENCE == 20
    lines = [item["line"] for item in out["unattributed"]]
    assert lines == sorted(lines) and "sk-ant" not in json.dumps(out)
    options.include_excerpts = False
    bare = analyze(options)["unattributed"]
    assert len(bare) == 20 and {item["excerpt"] for item in bare} == {None}
    # ... and a scan where everything has a target lists nothing
    s = Session("tidy").user("go").edit("/repo/a.py", error=builders.EDIT_NOT_FOUND)
    options.transcripts = [s.save(tmp_path / "tidy")]
    assert analyze(options)["unattributed"] == []


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


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_human_bumping_a_generated_file_is_not_rework(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    files = {
        "package-lock.json": '{{"v": "{0}"}}\n',
        "Cargo.lock": "v = {0}\n",
        "api_pb2.py": "v = '{0}'\n",
        "schema.py": "# Code generated by gen.py. DO NOT EDIT.\nv = '{0}'\n",
        "app.py": "v = '{0}'\n",
    }

    def commit(day: int, message: str, version: str) -> None:
        for name, content in files.items():
            (repo / name).write_text(content.format(version))
        stamp = f"2026-11-{day:02d}T12:00:00+00:00"
        dated = {**env, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        identity = ["-c", "user.name=Dana", "-c", "user.email=dana@corp.test"]
        for args in (["add", "-A"], ["commit", "-q", "-m", message]):
            subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, env=dated)

    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    for day in (1, 5):
        commit(day, "feat: use it\n\nCo-Authored-By: Claude <noreply@anthropic.com>", f"a{day}")
        commit(day + 1, "tidy up after it", f"h{day}")
    out = analyze(AnalyzeOptions(repo=repo, allow_no_transcripts=True, now=NOW))
    # lockfiles and compiled artefacts (by name) were regenerated, not repaired. They are
    # set aside, and the report says how many. A file that only *says* it is generated
    # stays: people do rewrite such files by hand.
    assert sorted(h["path"] for h in out["hotspots"]) == ["app.py", "schema.py"]
    assert all(h["signals"] == {"human-rework": 2} for h in out["hotspots"])
    assert out["totals"]["friction_events"] == 4
    (git,) = out["sources"]
    assert git["rework_set_aside"] == 6
    hotspot = entry(out["hotspots"], "app.py")
    # git-only evidence: nothing claims that sessions happened or that anything was edited
    assert [r["id"] for r in hotspot["recommendations"]] == ["learn-from-rework"]
    text = json.dumps(hotspot["recommendations"])
    assert "session" not in text and "editing it directly" not in text


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
    # nothing at all to analyse: an empty result, since that was allowed
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path / "bare")], check=True, env=env)
    (tmp_path / "none").mkdir()
    (tmp_path / "no-logs").mkdir()
    for options in (
        AnalyzeOptions(repo=bare, now=NOW, allow_no_transcripts=True),  # no commits yet
        AnalyzeOptions(repo=tmp_path / "none", now=NOW, allow_no_transcripts=True),  # no git
        AnalyzeOptions(repo=repo, allow_no_transcripts=True, include_git=False),
        AnalyzeOptions(
            repo=repo,
            transcripts=[tmp_path / "no-logs"],
            allow_no_transcripts=True,
            include_git=False,
        ),
    ):
        empty = analyze(options)
        assert empty["totals"]["sessions"] == 0 and empty["totals"]["friction_events"] == 0
        assert empty["hotspots"] == [] and empty["commands"] == [] and empty["unattributed"] == []
        assert empty["settings"]["git"] is False and empty["schema_version"] == 1
        with pytest.raises(NoTranscriptsError):
            analyze(dataclasses.replace(options, allow_no_transcripts=False))
    # a path that is not there is a mistake, allowed-empty or not
    with pytest.raises(NoTranscriptsError, match="does not exist"):
        analyze(
            AnalyzeOptions(repo=repo, transcripts=[tmp_path / "typo"], allow_no_transcripts=True)
        )


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
    assert evidence["line"] is None and bare["sources"][0]["path"] == "claude-code:repo"


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
    for package in ("web", "api"):  # on disk: without excerpts only real directories are named
        (repo / package).mkdir(parents=True)
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
    assert "run `docker compose up -d` before `pytest`" in rule["detail"]
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
        s.decision = {"decision": "accept", "source": "config"}  # nobody was asked
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
        listed = out["sources"][0]["path"]
        assert listed == cited or (listed.startswith("~/") and listed.endswith(cited))


# ---- round 13 -------------------------------------------------------------------------------

OWN_MODULE = (
    "ImportError while loading conftest '/x/tests/conftest.py'.\n"
    "tests/conftest.py:1: in <module>\n    from app import x\n"
    "E   ModuleNotFoundError: No module named 'app'"
)


@pytest.mark.parametrize(
    ("second", "between", "title"),
    [
        ("make test", None, "Tell agents to use `make test`"),
        ("python -m pytest", None, "Tell agents to use `python -m pytest`"),
        ("PYTHONPATH=src pytest -q", None, "Tell agents to use `PYTHONPATH=src pytest`"),
        ("pytest -q", "pip install -e .", "Document the setup `pytest` needs"),
    ],
)
def test_the_wrong_test_command_survives_later_work_on_the_package(
    tmp_path: Path, second: str, between: str | None, title: str
) -> None:
    package = tmp_path / "repo" / "src" / "app" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("x = 1\n")

    def body(s: Session) -> None:
        s.bash("pytest -q", OWN_MODULE, exit_code=4)
        if between:
            s.bash(between, "ok")
        s.bash(second, "3 passed in 0.1s")
        s.read(str(package)).edit(str(package)).bash(second, "3 passed in 0.1s")

    out = _scan(tmp_path, _sessions(tmp_path, 3, body))
    (row,) = out["commands"]
    assert (row["path"], row["events"], row["confidence"]) == ("pytest", 3, "medium")
    assert [r["title"] for r in row["recommendations"]] == [title]
    assert out["totals"]["dropped_red_runs"] == 0 and out["hotspots"] == []
    if "PYTHONPATH" in second:  # the value is transcript text: not without excerpts
        hidden = _scan(tmp_path, [], include_excerpts=False)
        titles = [r["title"] for c in hidden["commands"] for r in c["recommendations"]]
        assert titles == ["Tell agents to use `PYTHONPATH=... pytest`"]


def test_a_command_that_keeps_ending_red_gets_a_question(tmp_path: Path) -> None:
    def stuck(s: Session) -> None:
        s.bash("npm run e2e", "2 failed", exit_code=1, took=40)
        s.read(str(tmp_path / "repo" / "a.py"))
        s.bash("npm run e2e -- --debug", "2 failed", exit_code=1, took=40)

    out = _scan(tmp_path, _sessions(tmp_path, 3, stuck))
    (rule,) = out["commands"][0]["recommendations"]
    assert (rule["id"], rule["title"]) == (
        "say-if-it-is-known-broken",
        "`npm run e2e` keeps ending red",
    )
    assert "failed 6 times in 3 sessions and was still red" in rule["detail"]
    fewer = _scan(tmp_path / "two", _sessions(tmp_path / "two", 2, stuck))
    assert fewer["commands"][0]["recommendations"] == []


# ---- round 15 ------------------------------------------------------------------------

DOCUMENTED = (
    "# Shopfront\n\n"
    "Use `make test`, not `pytest`. Never hand-edit src/shopfront/api/schema_gen.py; "
    "edit api/openapi.yaml and run make codegen.\n"
)


def recommendation(out: dict[str, Any], path: str, rule: str) -> dict[str, Any]:
    rows = [*out["hotspots"], *out["commands"], *out["directories"]]
    (found,) = (r for r in entry(rows, path)["recommendations"] if r["id"] == rule)
    return dict(found)


def test_advice_that_is_already_written_down_says_so(tmp_path: Path) -> None:
    repo = tmp_path / "shopfront"
    shutil.copytree(fixture_story.REPO, repo)
    (repo / "AGENTS.md").write_text(DOCUMENTED)
    out = story(repo=repo)
    pytest_rec = recommendation(out, "pytest", "use-the-working-command")
    assert pytest_rec["documented_in"] == [{"file": "AGENTS.md", "line": 3}]
    assert pytest_rec["replacement"] == "make test" and "make test" in out["exposure"]["command"]
    assert pytest_rec["title"] == "`make test` is documented, yet agents run `pytest`"
    assert pytest_rec["detail"].startswith(
        "`AGENTS.md:3` already names `make test`; agents still ran `pytest` and failed 7 times "
        "in 4 sessions. Check that the cited line tells agents to use `make test` here. If it "
        "does, another line will not help; the next step is to enforce it: a hook or permission"
    )
    assert pytest_rec["documented_as"] == "instruction"
    assert "Add a line to AGENTS.md" not in pytest_rec["detail"]
    schema = recommendation(out, SCHEMA, "edit-the-source-not-the-output")
    assert schema["documented_in"] == [{"file": "AGENTS.md", "line": 3}]
    assert schema["title"] == "Generated file: telling agents was not enough"
    assert schema["detail"].startswith(
        "`AGENTS.md:3` already has an instruction about this file, yet agents"
    )
    assert "Check that the cited line says not to edit it by hand." in schema["detail"]
    assert "Say in AGENTS.md" not in schema["detail"]
    # other documentable advice about the same file keeps its text and gains a note
    rule = recommendation(out, SCHEMA, "write-down-the-rule")
    assert rule["documented_in"] == schema["documented_in"]
    assert "(Also mentioned at `AGENTS.md:3`: check whether it already says" in rule["detail"]
    assert rule["documented_as"] == "mention"
    # ... advice that is not about documentation is left alone
    split = recommendation(out, SCHEMA, "split-or-anchor-large-file")
    assert split["documented_in"] == [] and "already" not in split["detail"]
    # ... and so is everything the file does not name
    service = recommendation(out, SERVICE, "write-down-the-rule")
    assert service["documented_in"] == [] and "AGENTS.md:3" not in service["detail"]
    # the instruction file itself is never quoted, with or without excerpts
    bare = story(repo=repo, include_excerpts=False)
    assert recommendation(bare, SCHEMA, "edit-the-source-not-the-output") == schema
    assert "Never hand-edit" not in json.dumps(out) + json.dumps(bare)


def test_without_instruction_files_nothing_is_documented(result: dict[str, Any]) -> None:
    rows = [*result["hotspots"], *result["commands"], *result["directories"]]
    recs = [r for row in rows for r in row["recommendations"]]
    assert recs and all(r["documented_in"] == [] for r in recs)
    assert "already" not in recommendation(result, "pytest", "use-the-working-command")["detail"]
    assert recommendation(result, "pytest", "use-the-working-command")["title"] == (
        "Tell agents to use `make test`"
    )


def scan(tmp_path: Path, sessions: list[Session], repo: Path | None = None, **kw: Any) -> Any:
    files = [s.save(tmp_path / "t" / s.session_id) for s in sessions]
    kw.setdefault("include_git", False)
    return analyze(AnalyzeOptions(repo=repo or Path("/repo"), transcripts=files, **kw))


def missed(paths: list[str], root: str = "/repo") -> list[Session]:
    sessions = []
    for i in range(3):
        s = Session(f"s{i}", cwd=root).user("go")
        for path in paths:
            s.edit(f"{root}/{path}", error=builders.EDIT_NOT_FOUND)
            s.grep("Nothing", f"{root}/{path.rsplit('/', 1)[0]}")
        sessions.append(s)
    return sessions


def test_other_peoples_code_is_not_a_hotspot(tmp_path: Path) -> None:
    theirs = [
        "node_modules/react/index.js",
        "web/node_modules/x/a.js",
        ".venv/lib/python3.12/site-packages/django/db/base.py",
        "vendor/github.com/x/y.go",
        "target/debug/build.rs",
        "dist/app.js",
        "pkg/__pycache__/a.py",
    ]
    out = scan(tmp_path, missed([*theirs, "src/app.py", "src/builders/dist.py"]))
    assert [h["path"] for h in out["hotspots"]] == ["src/app.py", "src/builders/dist.py"]
    assert [d["path"] for d in out["directories"]] == ["src", "src/builders"]
    assert [c["name"] for c in out["tree"]["children"]] == ["src"]
    assert out["totals"]["third_party_events"] == 3 * 2 * len(theirs)
    assert out["totals"]["friction_events"] == 3 * 2 * 2
    assert not any("node_modules" in key for key in json.dumps(out["exposure"]).split('"'))
    # nothing of the kind: nothing is set aside
    assert scan(tmp_path, missed(["src/app.py"]))["totals"]["third_party_events"] == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_gitignore_decides_what_is_build_output(tmp_path: Path) -> None:
    repo = tmp_path / "proj"
    for name in ("build/gen.py", "out/gen.py", "src/a.py"):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x = 1\n")
    (repo / ".gitignore").write_text("out/\n*.log\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    paths = ["build/gen.py", "out/gen.py", "src/a.py", "node_modules/x/a.js"]
    sessions = missed(paths, str(repo))
    # `build/` is not ignored here, so it is this repository's own; `out/` is ignored
    out = scan(tmp_path, sessions, repo, include_git=True)
    assert [h["path"] for h in out["hotspots"]] == ["build/gen.py", "src/a.py"]
    assert out["totals"]["third_party_events"] == 3 * 2 * 2
    # without git, names are all there is to go by
    out = scan(tmp_path, sessions, repo)
    assert [h["path"] for h in out["hotspots"]] == ["out/gen.py", "src/a.py"]


def test_a_transcript_that_cannot_be_read_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_friction import engine

    real = engine.parse_transcript

    def parse(path: Path, *args: Any) -> Any:
        if path.name == "locked.jsonl":
            raise PermissionError(13, "Permission denied", str(path))
        if path.name == "gone.jsonl":
            raise OSError(5, "Input/output error")
        return real(path, *args)

    monkeypatch.setattr(engine, "parse_transcript", parse)
    good = Session("ok").user("go").edit("/repo/a.py", error=builders.EDIT_NOT_FOUND)
    files = [good.save(tmp_path / "private-client", name) for name in ("ok", "locked", "gone")]
    out = analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=files, include_git=False))
    assert [(s["kind"], s["path"], s.get("error")) for s in out["sources"][1:]] == [
        ("unreadable", "gone.jsonl", "could not be read"),
        ("unreadable", "locked.jsonl", "permission denied"),
    ]
    assert "error" not in out["sources"][0] and out["totals"]["sessions"] == 1
    assert "private-client" not in json.dumps(out["sources"][1:])


def stopped(sessions: int) -> list[Session]:
    made = []
    for i in range(sessions):
        s = Session(f"s{i}").user("go")
        s.tool("Bash", {"command": "npm run e2e"}, builders.INTERRUPT_TOOL, is_error=True)
        made.append(s.user(builders.INTERRUPT_TOOL, as_blocks=True))
    return made


def test_a_command_stopped_in_three_sessions_is_a_row(tmp_path: Path) -> None:
    out = scan(tmp_path, stopped(3))
    (row,) = out["commands"]
    assert (row["path"], row["signals"], row["sessions"]) == (
        "npm run e2e",
        {"user-interrupt": 3},
        3,
    )
    assert out["unattributed"] == [] and out["totals"]["unattributed_events"] == 0
    assert [r["title"] for r in row["recommendations"]] == [
        "Say when agents should run `npm run e2e`"
    ]
    # a light row: well below what three corrections would score
    assert 0 < row["score"] < 40
    # twice is not yet about the command
    out = scan(tmp_path, stopped(2))
    assert out["commands"] == [] and len(out["unattributed"]) == 2


def test_one_session_and_a_low_score_is_a_lead_not_advice(tmp_path: Path) -> None:
    def invocation(sessions: int) -> list[Session]:
        made = []
        for i in range(sessions):
            s = Session(f"s{i}").user("go")
            for _ in range(2):
                s.bash("npm test", 'npm ERR! Missing script: "test"', exit_code=1)
            made.append(s)
        return made

    (row,) = scan(tmp_path, invocation(1))["commands"]
    assert row["score"] < 20 and row["sessions"] == 1 and row["recommendations"] == []
    (row,) = scan(tmp_path, invocation(2))["commands"]
    assert [r["id"] for r in row["recommendations"]] == ["name-the-missing-target"]
    # what a human said in so many words is still passed on from a single session
    s = Session("one").user("go")
    s.reject("Bash", {"command": "rm -rf migrations"}, "never delete migrations")
    (row,) = scan(tmp_path, [s])["commands"]
    assert row["score"] < 20 and [r["id"] for r in row["recommendations"]] == ["state-the-boundary"]


# ---- round 16 ------------------------------------------------------------------------

MNF = "ImportError while loading conftest\nE   ModuleNotFoundError: No module named 'app'"


def long_session(gap: timedelta, tasks: int = 10) -> Session:
    s = Session("long")
    for task in range(tasks):
        s.now = builders.T0 + task * gap
        s.user(f"task {task}: improve things")
        s.edit("/repo/src/app.py", f"zz{task}", "y", error=builders.EDIT_NOT_FOUND)
        s.edit("/repo/src/app.py", f"a{task}", f"b{task}")
        s.bash("pytest", MNF, exit_code=4).bash("make test", "3 passed")
    return s


def test_one_session_id_picked_up_on_ten_days_is_ten_episodes(tmp_path: Path) -> None:
    out = scan(tmp_path, [long_session(timedelta(days=1))])
    assert (out["totals"]["sessions"], out["totals"]["episodes"]) == (1, 10)
    (row,) = out["hotspots"]
    # shown as the one session it is, scored and graded as the ten occasions it was
    assert (row["sessions"], row["exposure_sessions"], row["friction_rate"]) == (1, 1, 1.0)
    assert (row["episodes"], row["exposure_episodes"]) == (10, 10)
    assert row["score_detail"]["recurrence"] == 1.0 and row["confidence"] == "medium"
    assert "the only session that touched it (in 10 separate episodes of work)" in row["why"]
    assert "recurs across episodes of work" in row["why"]
    units = {s["episode"] for s in row["score_detail"]["sessions"]}
    assert units == {"long", *(f"long#{n}" for n in range(2, 11))}
    assert {s["session_id"] for s in row["score_detail"]["sessions"]} == {"long"}
    (command,) = out["commands"]
    assert command["episodes"] == 10
    assert [r["id"] for r in command["recommendations"]] == ["use-the-working-command"]
    assert out["tree"]["sessions"] == 1 and out["exposure"]["file"] == {"src/app.py": 1}


def test_one_long_sitting_is_one_episode(tmp_path: Path) -> None:
    out = scan(tmp_path, [long_session(timedelta(minutes=110), tasks=4)])
    assert (out["totals"]["sessions"], out["totals"]["episodes"]) == (1, 1)
    (row,) = out["hotspots"]
    assert (row["sessions"], row["episodes"], row["confidence"]) == (1, 1, "low")
    assert "episodes" not in row["why"]
    assert row["score_detail"]["recurrence"] < 0.5


def test_subagents_and_silent_stretches_do_not_start_an_episode(tmp_path: Path) -> None:
    from agent_friction.adapters import parse_transcript
    from agent_friction.engine import episode_starts

    main = Session("s").user("go").read("/repo/a.py")
    main.tick(3 * 3600).read("/repo/a.py")  # the agent worked on after a long wait: no prompt
    main.tick(60).user("thanks, next")  # ... and this prompt follows activity at once
    side = Session("s", sidechain=True, start=builders.T0 + timedelta(hours=9))
    side.user("subagent task").read("/repo/a.py")
    streams = [
        stream
        for i, s in enumerate((main, side))
        for stream in parse_transcript(s.save(tmp_path / str(i))).streams
    ]
    assert episode_starts(streams) == {}
    main.tick(10 * 3600).user("back again")
    streams[0] = parse_transcript(main.save(tmp_path / "0")).streams[0]
    (start,) = episode_starts(streams)["s"]
    assert builders.T0.timestamp() + 13 * 3600 < start <= main.now.timestamp()


def test_every_hotspot_is_listed_unless_a_limit_is_given(tmp_path: Path) -> None:
    s = Session("many").user("go")
    for i in range(60):
        s.edit(f"/repo/src/f{i:02d}.py", error=builders.EDIT_NOT_FOUND)
    assert AnalyzeOptions(repo=Path("/repo")).max_hotspots is None
    assert len(scan(tmp_path, [s])["hotspots"]) == 60
    limited = scan(tmp_path, [s], max_hotspots=7)
    assert len(limited["hotspots"]) == 7 and limited["totals"]["files_affected"] == 60


def test_a_test_that_passes_on_a_rerun_in_three_sessions_is_flaky(tmp_path: Path) -> None:
    def sessions(count: int) -> list[Session]:
        made = []
        for i in range(count):
            s = Session(f"s{i}").user("go")
            s.bash("npm test", "Tests: 1 failed, 10 passed", exit_code=1, took=40)
            made.append(s.bash("npm test", "Tests: 11 passed"))
        return made

    (row,) = scan(tmp_path, sessions(3))["commands"]
    assert row["signals"] == {"command-failure": 3} and row["confidence"] == "medium"
    assert not row["score_detail"]["capped"] and row["est_minutes_lost"] > 0
    (rec,) = row["recommendations"]
    assert (rec["id"], rec["title"]) == ("fix-the-flaky-test", "`npm test` looks flaky")
    assert (
        "fails and then passes on a re-run with nothing changed in between (3 times in 3"
        in (rec["detail"])
    )
    assert "passed on a plain re-run, with nothing changed" in row["evidence"][0]["summary"]
    # twice may be chance: weak evidence, no advice
    (row,) = scan(tmp_path, sessions(2))["commands"]
    assert row["score_detail"]["capped"] and row["recommendations"] == []


def test_leftover_searches_never_become_a_pattern(tmp_path: Path) -> None:
    def sessions(pattern: str, chase: bool) -> list[Session]:
        made = []
        for i in range(4):
            s = Session(f"s{i}").user("go").edit("/repo/src/a.py")
            s.grep(pattern, "/repo/src")
            if chase:
                s.grep(pattern, "/repo")
            made.append(s)
        return made

    for pattern, chase in ((r"console\.log|debugger", True), ("RefundPolicy", False)):
        out = scan(tmp_path, sessions(pattern, chase))
        rows = [*out["directories"], *out["hotspots"]]
        assert rows and all(r["confidence"] == "low" and r["score_detail"]["capped"] for r in rows)
        assert all(r["recommendations"] == [] for r in rows), pattern
    # the same name, looked for again and again by agents that wanted to find it
    out = scan(tmp_path, sessions("RefundPolicy", True))
    row = entry(out["directories"], "src")
    assert row["confidence"] == "medium" and not row["score_detail"]["capped"]
    assert [r["id"] for r in row["recommendations"]] == ["add-a-pointer"]


# ---- round 17 ------------------------------------------------------------------------


def test_without_excerpts_guessed_paths_are_not_shown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("x = 1\n")
    root = str(repo)
    sessions = []
    for i in range(2):
        s = Session(f"s{i}", cwd=root).user("go")
        s.read(f"{root}/src/ZZDIRX/missing.py", missing=True)
        s.read(f"{root}/src/ZZDIRX/missing.py", missing=True)
        s.edit(f"{root}/src/app.py", error=builders.EDIT_NOT_FOUND)
        s.read(f"{root}/src/gone_since.py")  # read successfully then: it existed
        s.edit(f"{root}/src/gone_since.py", error=builders.EDIT_NOT_FOUND)
        sessions.append(s)
    plain = scan(tmp_path, sessions, repo)
    assert "src/ZZDIRX/missing.py" in [h["path"] for h in plain["hotspots"]]
    bare = scan(tmp_path, sessions, repo, include_excerpts=False)
    text = json.dumps(bare)
    assert "ZZDIRX" not in text and "missing.py" not in text
    paths = sorted(h["path"] for h in bare["hotspots"])
    assert paths[1:] == ["src/app.py", "src/gone_since.py"]
    assert re.fullmatch(r"src/<path:[0-9a-f]{6}>/<path:[0-9a-f]{6}>", paths[0])
    guessed = entry(bare["hotspots"], paths[0])
    assert guessed["signals"] == {"path-miss": 4} and paths[0] in guessed["evidence"][0]["summary"]
    assert bare["exposure"]["file"][paths[0]] == 2
    assert [c["path"] for c in bare["tree"]["children"][0]["children"]][-1].startswith("src/")
    # the same guess gets the same placeholder in every report
    assert (
        sorted(
            h["path"] for h in scan(tmp_path, sessions, repo, include_excerpts=False)["hotspots"]
        )
        == paths
    )


def test_tool_calls_in_the_repo_are_counted_apart(tmp_path: Path) -> None:
    s = Session("wide", cwd="/work").user("go")
    s.read("/work/repo/a.py").edit("/work/repo/a.py", error=builders.EDIT_NOT_FOUND)
    s.read("/work/other/b.py").bash("ls /work/other", "b.py")
    s.bash("cd /work/repo && make test", "ok").grep("Thing", "/work/other")
    s.tool("TodoWrite", {"todos": []}, "ok")
    out = scan(tmp_path, [s], Path("/work/repo"))
    assert (out["totals"]["tool_calls"], out["totals"]["tool_calls_in_repo"]) == (7, 3)
    inside = Session("in").user("go").read("/repo/a.py").bash("make test", "ok")
    inside.tool("TodoWrite", {"todos": []}, "ok")
    totals = scan(tmp_path, [inside])["totals"]
    assert totals["tool_calls"] == totals["tool_calls_in_repo"] == 3


def test_background_failures_score_like_foreground_ones(tmp_path: Path) -> None:
    conftest = "ImportError while loading conftest\nModuleNotFoundError: No module named 'yaml'"
    polled = f"<status>failed</status>\n<exit_code>4</exit_code>\n<stdout>\n{conftest}\n</stdout>"

    def sessions(in_background: bool) -> list[Session]:
        made = []
        for i in range(4):
            s = Session(f"s{i}").user("run the tests").bash("pytest -q", conftest, exit_code=4)
            for k in range(3):
                if not in_background:
                    s.bash("pytest -q", conftest, exit_code=4)
                    continue
                launch = {"command": "pytest -q", "run_in_background": True}
                s.tool("Bash", launch, f"Command running in background with ID: b{k}")
                s.tool("BashOutput", {"bash_id": f"b{k}"}, polled)
            made.append(s)
        return made

    (front,) = scan(tmp_path, sessions(False))["commands"]
    out = scan(tmp_path, sessions(True))
    (back,) = out["commands"]
    assert back["score"] == front["score"] > 50 and back["signals"] == front["signals"]
    assert [r["id"] for r in back["recommendations"]] == [r["id"] for r in front["recommendations"]]
    assert "document-the-command" in [r["id"] for r in back["recommendations"]]
    # the reported ends are not calls the agent made
    assert out["totals"]["tool_calls"] == 4 * 7


def test_no_excerpts_hides_project_tool_names(tmp_path: Path) -> None:
    refused = "Claude requested permissions to use {}, but you haven't granted it yet."
    sessions = []
    for i in range(3):
        s = Session(f"s{i}").user("go")
        s.tool("mcp__SECRETMCP__do", {"x": 1}, refused.format("mcp__SECRETMCP__do"), is_error=True)
        s.reject("mcp__SECRETMCP__do", {"x": 2})
        s.tool(
            "WebFetch", {"url": "https://example.com"}, refused.format("WebFetch"), is_error=True
        )
        s.edit("/repo/src/a.py", error=builders.EDIT_NOT_FOUND)
        sessions.append(s)
    generic = tmp_path / "g.jsonl"
    rows: list[dict[str, Any]] = []
    for i in range(3):
        base = {"session": f"g{i}", "cwd": "/repo", "tool": "acme_secret_editor"}
        call = {**base, "kind": "tool_call", "op": "edit", "path": "src/a.py"}
        rows += [
            {"session": f"g{i}", "kind": "user_message", "text": "go", "cwd": "/repo"},
            {**call, "id": 1, "old": "a", "new": "b"},
            {**base, "kind": "tool_result", "id": 1, "error": "edit_not_found"},
            {**call, "id": 2, "old": "c", "new": "d"},
            {**base, "kind": "tool_result", "id": 2, "error": "rejected"},
        ]
    generic.write_text("".join(json.dumps(row) + "\n" for row in rows))
    files = [s.save(tmp_path / "t" / s.session_id) for s in sessions] + [generic]

    def run(**kw: Any) -> str:
        options = AnalyzeOptions(repo=Path("/repo"), transcripts=files, include_git=False, **kw)
        return json.dumps(analyze(options))

    full, bare = run(), run(include_excerpts=False)
    for name in ("SECRETMCP", "mcp__SECRETMCP__do", "acme_secret_editor"):
        assert name in full and name not in bare, name
    server, tool = public_tool("mcp__SECRETMCP__do")[5:].split("__")
    assert re.fullmatch(r"<server:[0-9a-f]{6}>", server) and re.fullmatch(
        r"<tool:[0-9a-f]{6}>", tool
    )
    assert f"The user declined mcp__{server}__{tool}" in bare
    assert f"{public_tool('acme_secret_editor')} on " in bare
    # built-in tools keep their names, and a tool without a target is named once
    assert "Edit on " in bare and "WebFetch on WebFetch" not in full + bare
    assert "WebFetch was denied by permission settings" in bare
    assert all(public_tool(name) == name for name in ("Read", "MultiEdit", "Bash", "edit", "shell"))


def test_until_is_exclusive_so_two_windows_never_share_an_event(tmp_path: Path) -> None:
    midnight = datetime(2026, 11, 1, tzinfo=timezone.utc)
    s = Session("s0", start=midnight - timedelta(seconds=9)).user("go")
    s.bash("pytest", "1 failed", exit_code=1, took=5)  # called at -5s, red at midnight sharp
    s.tick(60).bash("pytest", "1 failed", exit_code=1)

    def events(**window: Any) -> int:
        total: int = scan(tmp_path, [s], **window)["totals"]["friction_events"]
        return total

    assert events() == 2
    assert events(until=midnight) == 0 and events(since=midnight) == 2
    assert events(until=midnight + timedelta(seconds=1)) == 1
    assert events(until=midnight) + events(since=midnight) == events()


def test_a_trailing_red_run_is_marked_in_its_evidence(tmp_path: Path) -> None:
    s = Session("s0").user("go").bash("pytest", "1 failed", exit_code=1)
    s.tick(30).bash("pytest", "1 failed", exit_code=1)
    (command,) = scan(tmp_path, [s])["commands"]
    first, last = sorted(command["evidence"], key=lambda e: e["line"])
    assert "trailing" not in first and last["trailing"] is True
    assert command["events"] == 2  # it still counts


def test_the_activation_step_is_what_the_advice_names(tmp_path: Path) -> None:
    missing = "Traceback\nModuleNotFoundError: No module named 'flask'"
    sessions = []
    for i in range(4):
        s = Session(f"s{i}").user("run the tests").bash("pytest", missing, exit_code=1)
        sessions.append(s.bash("source .venv/bin/activate && pytest", "5 passed"))
    (command,) = scan(tmp_path, sessions)["commands"]
    (advice,) = command["recommendations"]
    assert advice["id"] == "document-the-setup-step"
    assert advice["setup"] == "source .venv/bin/activate" and "`pytest`" in advice["title"]
    assert "run `source .venv/bin/activate` before `pytest`" in advice["detail"]


# ---- round 19 ----------------------------------------------------------------------------


def _commands(tmp_path: Path, build: Any, count: int = 4, **kw: Any) -> list[dict[str, Any]]:
    sessions = []
    for i in range(count):
        s = Session(f"s{i}", cwd=kw.get("cwd", "/repo")).user("go")
        build(s)
        sessions.append(s)
    found: list[dict[str, Any]] = scan(tmp_path, sessions, kw.get("repo"))["commands"]
    return found


def test_a_subset_passing_is_not_flakiness(tmp_path: Path) -> None:
    red = "FAILED tests/test_x.py::test_a\n1 failed, 40 passed in 2.0s"
    (row,) = _commands(
        tmp_path,
        lambda s: s.bash("pytest", red, exit_code=1).bash("pytest tests/test_cart.py", "5 passed"),
    )
    # not flaky; but the same test red in session after session is worth saying
    assert [r["id"] for r in row["recommendations"]] == ["say-if-it-is-known-broken"]
    assert all("re-run" not in e["summary"] for e in row["evidence"])
    (flaky,) = _commands(
        tmp_path / "again",
        lambda s: s.bash("pytest", red, exit_code=1).bash("pytest", "41 passed"),
    )
    assert [r["id"] for r in flaky["recommendations"]] == ["fix-the-flaky-test"]


def test_waiting_for_a_server_is_dropped_not_documented(tmp_path: Path) -> None:
    refused = "curl: (7) Failed to connect to localhost port 3000 after 0 ms: Connection refused"

    def build(s: Session) -> None:
        launch = {"command": "npm run dev", "run_in_background": True}
        s.tool("Bash", launch, "Command running in background with ID: b1")
        s.bash("curl -s localhost:3000/health", refused, exit_code=7)
        s.bash("sleep 3 && curl -s localhost:3000/health", '{"ok":true}')

    sessions = [Session(f"s{i}").user("go") for i in range(4)]
    for s in sessions:
        build(s)
    out = scan(tmp_path, sessions)
    assert out["commands"] == [] and out["totals"]["dropped_red_runs"] == 4
    (down,) = _commands(
        tmp_path / "down",
        lambda s: s.bash("curl -s localhost:3000/health", refused, exit_code=7).read("/repo/a.py"),
    )
    assert [r["id"] for r in down["recommendations"]] == ["document-the-environment"]
    assert "port 3000" in down["recommendations"][0]["title"]


def test_git_bash_paths_on_windows_are_the_repo(tmp_path: Path) -> None:
    home = "C:\\Users\\me\\repo"
    missing = "ImportError while loading conftest\nModuleNotFoundError: No module named 'yaml'"
    for spelled in ("/c/Users/me/repo", "/mnt/c/Users/me/repo", "/cygdrive/c/users/me/repo"):
        sessions = []
        for i in range(3):
            s = Session(f"s{i}", cwd=home).user("go")
            sessions.append(s.bash(f"cd {spelled} && pytest -q", missing, exit_code=4))
        out = scan(tmp_path / spelled.split("/")[1], sessions, Path(home))
        assert out["totals"]["tool_calls_in_repo"] == 3, spelled
        assert [c["path"] for c in out["commands"]] == ["pytest"], spelled
    # on a POSIX repository /c/... is just another directory
    sessions = [
        Session(f"s{i}").user("go").bash("cd /c/repo && pytest -q", missing, exit_code=4)
        for i in range(3)
    ]
    assert scan(tmp_path / "posix", sessions)["commands"] == []


def test_the_wrong_runner_is_named_once_it_recurs(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.bash("yarn test", "Tests: 1 failed", exit_code=1).bash("pnpm test", "Tests: 5 passed")

    (row,) = _commands(tmp_path, build, 3)
    (advice,) = row["recommendations"]
    assert advice["id"] == "use-the-working-command" and advice["replacement"] == "pnpm test"
    assert row["evidence"][0]["summary"] == (
        "`yarn test` failed (exit 1; `pnpm test` then passed, with nothing changed)"
    )
    (twice,) = _commands(tmp_path / "twice", build, 2)
    assert twice["recommendations"] == [] and "pnpm" not in json.dumps(twice)


def test_py_for_python_and_a_flag_that_fixed_it(tmp_path: Path) -> None:
    unknown = (
        "python : The term 'python' is not recognized as the name of a cmdlet, function, "
        "script file, or operable program.\n    + FullyQualifiedErrorId : CommandNotFoundException"
    )
    (row,) = _commands(
        tmp_path,
        lambda s: s.bash("python -m pytest", unknown, exit_code=1).bash("py -m pytest", "5 passed"),
    )
    assert "use `py`, not `python`" in row["recommendations"][0]["detail"]
    eresolve = "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree"
    (row,) = _commands(
        tmp_path / "npm",
        lambda s: s.bash("npm install", eresolve, exit_code=1).bash(
            "npm install --legacy-peer-deps", "added 100 packages"
        ),
    )
    (advice,) = row["recommendations"]
    assert advice["title"] == "Tell agents to use `npm install --legacy-peer-deps`"
    assert "add the option where the command is defined" in advice["detail"]


def test_error_messages_point_at_the_published_schema(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("not a transcript\n")
    with pytest.raises(AgentFrictionError) as raised:
        analyze(AnalyzeOptions(repo=tmp_path, transcripts=[tmp_path / "notes.txt"]))
    assert "https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md" in str(
        raised.value
    )


# ---- round 20 ----------------------------------------------------------------------------


def test_each_failure_is_bound_to_the_setup_step_that_followed_it(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.bash("pytest", "ModuleNotFoundError: No module named 'django'", exit_code=1)
        s.bash("pip install -r requirements.txt", "Successfully installed")
        s.bash("pytest", PG_DOWN, exit_code=1)
        s.bash("docker compose up -d db", "Started")
        s.bash("pytest", "5 passed in 1s")

    for bare in (False, True):
        sessions = [Session(f"s{i}").user("go") for i in range(4)]
        for s in sessions:
            build(s)
        out = scan(tmp_path / str(bare), sessions, include_excerpts=not bare)
        (row,) = out["commands"]
        (advice,) = row["recommendations"]
        assert advice["id"] == "document-the-setup-step" and row["events"] == 8
        assert advice["setup"] == "pip install && docker compose up -d"
        assert (
            "it worked after the agent ran `pip install` (4 times) and "
            "`docker compose up -d` (4 times)" in advice["detail"]
        )
        assert (
            '"run `pip install`, then `docker compose up -d` before `pytest`"' in advice["detail"]
        )


@pytest.mark.parametrize(
    ("command", "output", "step", "named"),
    [
        (
            "npm run e2e",
            "Executable doesn't exist",
            "npx playwright install chromium",
            "npx playwright install",
        ),
        (
            "npm test",
            "@prisma/client did not initialize yet",
            "npx prisma generate",
            "npx prisma generate",
        ),
        (
            "go test ./...",
            "no required module provides package x\nFAIL",
            "go mod download",
            "go mod download",
        ),
    ],
)
def test_a_setup_step_is_named_so_that_it_can_be_run(
    tmp_path: Path, command: str, output: str, step: str, named: str
) -> None:
    (row,) = _commands(
        tmp_path,
        lambda s: s.bash(command, output, exit_code=1).bash(step, "done").bash(command, "ok"),
    )
    (advice,) = row["recommendations"]
    assert advice["id"] == "document-the-setup-step" and advice["setup"] == named
    assert f"run `{named}` before " in advice["detail"]


def test_utf16_and_bom_transcripts_are_analysed_like_utf8(tmp_path: Path) -> None:
    rows = [
        {"session": "g", "kind": "tool_call", "id": 1, "op": "edit", "path": "src/a.py"},
        {"session": "g", "kind": "tool_result", "id": 1, "error": "edit_not_found"},
    ]
    text = "".join(json.dumps({**row, "cwd": "/repo"}) + "\n" for row in rows)
    totals = []
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):
        path = tmp_path / f"{encoding}.jsonl"
        path.write_bytes(text.encode(encoding))
        options = AnalyzeOptions(repo=Path("/repo"), transcripts=[path], include_git=False)
        out = analyze(options)
        totals.append((out["totals"]["friction_events"], out["sources"][0]["skipped_lines"]))
    assert totals == [(1, 0)] * 3
    # a file in which nothing can be read says what it was read as
    odd = tmp_path / "odd.jsonl"
    odd.write_bytes("not json at all\n".encode("utf-16"))
    with pytest.raises(AgentFrictionError, match="read as UTF-16"):
        analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[odd], include_git=False))
    odd.write_bytes(b"\x00\x01\x02 binary \x00\n")
    with pytest.raises(AgentFrictionError, match="NUL bytes"):
        analyze(AnalyzeOptions(repo=Path("/repo"), transcripts=[odd], include_git=False))


def test_a_copy_of_a_transcript_is_listed_as_duplicates_not_as_empty(tmp_path: Path) -> None:
    from agent_friction.engine import inspect_sources

    s = Session("s0").user("go").edit("/repo/src/a.py", error=builders.EDIT_NOT_FOUND)
    original = s.save(tmp_path / "t", "a")
    (tmp_path / "t" / "b.jsonl").write_bytes(original.read_bytes())
    options = AnalyzeOptions(repo=Path("/repo"), transcripts=[tmp_path / "t"], include_git=False)
    first, copy = inspect_sources(options)
    assert (first["events"], first["duplicate_lines"]) == (3, 0)
    assert (copy["events"], copy["duplicate_lines"], copy["encoding"]) == (0, 3, "utf-8")
    (source,) = analyze(options)["sources"]
    assert source["files"] == 2 and source["duplicate_lines"] == 3


def test_the_same_red_test_in_three_sessions_is_a_standing_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_legacy.py").write_text("def test_old(): assert False\n")
    red = "FAILED tests/test_legacy.py::test_old - AssertionError\n1 failed, 40 passed in 2.0s"

    def sessions(count: int) -> list[Session]:
        made = []
        for i in range(count):
            s = Session(f"s{i}", cwd=str(repo)).user(f"task {i}")
            made.append(s.bash("pytest", red, exit_code=1).read(f"{repo}/tests/test_legacy.py"))
        return made

    out = scan(tmp_path / "five", sessions(5), repo)
    (command,) = out["commands"]
    # the advice is given once, on the test's file; the command's row only counts it
    assert command["score"] > 19 and command["recommendations"] == []
    (row,) = out["hotspots"]
    assert row["path"] == "tests/test_legacy.py" and row["signals"] == {"command-failure": 5}
    (advice,) = row["recommendations"]
    assert advice["id"] == "say-if-it-is-known-broken"
    assert advice["title"] == "`tests/test_legacy.py::test_old` is red session after session"
    # without excerpts the test is not named, the file (a repo path) still is
    bare = scan(tmp_path / "bare", sessions(5), repo, include_excerpts=False)
    assert "test_old" not in json.dumps(bare)
    assert bare["hotspots"][0]["recommendations"][0]["title"] == (
        "The same test is red session after session"
    )
    # twice is not a pattern, and a test the session fixed is the development loop
    twice = scan(tmp_path / "two", sessions(2), repo)
    assert twice["commands"][0]["score"] <= 19 and twice["hotspots"] == []
    fixed = []
    for i in range(5):
        s = Session(f"s{i}", cwd=str(repo)).user("go").bash("pytest", red, exit_code=1)
        fixed.append(s.edit(f"{repo}/tests/test_legacy.py").bash("pytest", "41 passed"))
    assert scan(tmp_path / "fixed", fixed, repo)["commands"] == []


def test_a_missing_program_that_works_by_its_path(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.bash("zebractl status", "bash: zebractl: command not found", exit_code=127)
        s.bash("./bin/zebractl status", "ok")

    (row,) = _commands(tmp_path, build)
    assert "use `./bin/zebractl`, not `zebractl`" in row["recommendations"][0]["detail"]
    sessions = [Session(f"s{i}").user("go") for i in range(4)]
    for s in sessions:
        build(s)
    bare = scan(tmp_path / "bare", sessions, include_excerpts=False)
    assert "zebractl" not in json.dumps(bare)
    tool = public_key("zebractl")
    detail = bare["commands"][0]["recommendations"][0]["detail"]
    assert f"use `./bin/{tool}`, not `{tool}`" in detail


def test_a_host_that_cannot_be_resolved_is_the_environment(tmp_path: Path) -> None:
    refused = "curl: (6) Could not resolve host: registry.example"
    (row,) = _commands(
        tmp_path,
        lambda s: s.bash("curl -s https://registry.example/x", refused, exit_code=6).read(
            "/repo/a"
        ),
    )
    (advice,) = row["recommendations"]
    assert advice["id"] == "document-the-environment"
    assert advice["title"] == "Say what `curl` can reach from where agents work"


# ---- round 21 ----------------------------------------------------------------------------


def test_a_repo_given_with_dots_has_its_own_name(tmp_path: Path) -> None:
    repo = tmp_path / "shop"
    (repo / "src").mkdir(parents=True)
    s = Session("s0", cwd=str(repo)).user("go").edit(f"{repo}/a.py", error=builders.EDIT_NOT_FOUND)
    path = s.save(tmp_path / "t")
    for given in (repo / "src" / "..", repo / "." / "src" / ".." / "."):
        out = analyze(AnalyzeOptions(repo=given, transcripts=[path], include_git=False))
        assert out["repo"]["name"] == "shop" and not out["repo"]["root"].endswith("..")
        assert out["tree"]["name"] == "shop" and out["totals"]["friction_events"] == 1
        assert out["sources"][0]["path"] == "claude-code:shop"


def test_a_window_judges_its_events_with_what_came_after(tmp_path: Path) -> None:
    """Red before the cut, fixed after it: the development loop, in either scan."""
    cut = datetime(2026, 11, 1, tzinfo=timezone.utc)
    s = Session("s0", start=cut - timedelta(minutes=10)).user("fix the bug")
    s.bash("make check", "1 failed", exit_code=1)  # fixed after the cut: red, edit, green
    s.bash("pytest", "ModuleNotFoundError: No module named 'yaml'", exit_code=1)  # set up after
    s.edit("/repo/src/a.py", error=builders.EDIT_NOT_FOUND)  # friction, before the cut
    s.tick(600)
    s.edit("/repo/src/a.py").bash("make check", "ok")
    s.bash("pip install pyyaml", "ok").bash("pytest", "5 passed")
    s.bash("npm test", "Tests: 1 failed", exit_code=1).user("thanks")  # friction, after the cut

    def totals(**window: Any) -> tuple[int, int, list[str]]:
        out = scan(tmp_path, [s], **window)
        rows = sorted(row["path"] for row in out["commands"])
        return out["totals"]["friction_events"], out["totals"]["dropped_red_runs"], rows

    full, before, after = totals(), totals(until=cut), totals(since=cut)
    assert full == (3, 1, ["npm test", "pytest"])
    assert before == (2, 1, ["pytest"]) and after == (1, 0, ["npm test"])
    assert before[0] + after[0] == full[0] and before[1] + after[1] == full[1]
    # the failure that a later setup step fixed is still told so
    out = scan(tmp_path, [s], until=cut)
    assert "setup" in out["commands"][0]["evidence"][0]["summary"]
    assert out["totals"]["tool_calls"] == 3  # what came after is context, never counted


def test_an_unresolved_import_of_the_repos_own_module_recurring(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    conftest = (
        "ImportError while loading conftest '/r/tests/conftest.py'.\n"
        "E   ModuleNotFoundError: No module named 'app'"
    )

    def sessions(count: int) -> list[Session]:
        return [
            Session(f"s{i}", cwd=str(repo)).user("go").bash("pytest", conftest, exit_code=4)
            for i in range(count)
        ]

    (row,) = scan(tmp_path / "four", sessions(4), repo)["commands"]
    assert row["score"] > 19 and row["recommendations"][0]["id"] == "document-the-command"
    (row,) = scan(tmp_path / "two", sessions(2), repo)["commands"]
    assert row["score"] <= 19 and row["recommendations"] == []


def test_advice_quotes_commands_as_they_are_typed(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.bash("npm test", "Error: Cannot find module 'jest'\nRequire stack:", exit_code=1)
        s.bash("npm install", "added 5 packages").bash("npm test", "Tests: 5 passed")

    (row,) = _commands(tmp_path, build, cwd="/repo/web")
    (advice,) = row["recommendations"]
    assert row["path"] == "npm test (web/)" and advice["setup"] == "npm install (web/)"
    assert '("in `web/`, run `npm install` before `npm test`")' in advice["detail"]


def test_where_the_sessions_that_missed_the_repo_ran(tmp_path: Path) -> None:
    from agent_friction.engine import session_directories

    home = str(Path.home())
    sessions = [Session(f"old{i}", cwd=f"{home}/work/old-name").user("go") for i in range(3)]
    sessions.append(Session("other", cwd="/srv/other").user("go"))
    sessions.append(Session("here", cwd="/repo").user("go"))
    files = [s.edit(f"{s.cwd}/a.py").save(tmp_path / "t" / s.session_id) for s in sessions]
    options = AnalyzeOptions(repo=Path("/repo"), transcripts=files, include_git=False)
    assert session_directories(options) == [("~/work/old-name", 3), ("/srv/other", 1)]
    assert session_directories(options, limit=1) == [("~/work/old-name", 3)]
    aliased = dataclasses.replace(options, repo_aliases=[f"{home}/work/old-name"])
    assert session_directories(aliased) == [("/srv/other", 1)]


# ---- round 22 ------------------------------------------------------------------------------


def test_a_piped_red_run_is_reported_for_the_step_that_was_red(tmp_path: Path) -> None:
    red = "1 failed, 1317 passed in 6.2s\nmake: *** [Makefile:20: test] Error 1"
    line = "make examples >/dev/null 2>&1; make check 2>&1 | tail -2"
    (row,) = _commands(tmp_path, lambda s: s.bash(line, red).read("/repo/a.py"))
    assert row["path"] == "make check"
    # red, an edit, green: nothing to report, and the red runs are counted as dropped
    result = scan(
        tmp_path / "loop",
        [
            Session(f"s{i}", cwd="/repo")
            .user("go")
            .bash(line, red)
            .edit("/repo/src/a.py")
            .bash(line, "1318 passed in 6.1s")
            for i in range(4)
        ],
    )
    assert result["commands"] == [] and result["totals"]["dropped_red_runs"] == 4


def test_a_directory_agents_keep_guessing_is_a_path_agents_expect(tmp_path: Path) -> None:
    gone = "/bin/bash: line 1: cd: frontend: No such file or directory"

    def sessions() -> list[Session]:
        return [
            Session(f"s{i}", cwd="/repo")
            .user("go")
            .bash("cd frontend && npm test", gone, exit_code=1)
            for i in range(4)
        ]

    result = scan(tmp_path, sessions())
    assert result["commands"] == []  # `npm test (frontend/)` never ran
    (row,) = result["hotspots"]
    assert row["path"] == "frontend" and row["signals"] == {"path-miss": 4}
    (advice,) = row["recommendations"]
    assert advice["id"] == "path-agents-expect"
    assert (
        "tried to `cd` into this non-existent directory 4 times in 4 sessions" in advice["detail"]
    )
    assert row["evidence"][0]["summary"] == "`cd` was pointed at frontend, which does not exist"
    # without excerpts the guessed name is not shown
    bare = scan(tmp_path / "bare", sessions(), include_excerpts=False)
    assert "frontend" not in json.dumps(bare)
    assert bare["hotspots"][0]["signals"] == {"path-miss": 4}


def test_credentials_the_tool_names_get_environment_advice(tmp_path: Path) -> None:
    denied = "ERROR 1045 (28000): Access denied for user 'root'@'localhost' (using password: NO)"
    (row,) = _commands(tmp_path, lambda s: s.bash("mysql -e 'select 1'", denied, exit_code=1))
    (advice,) = row["recommendations"]
    assert advice["id"] == "document-the-environment"
    assert advice["title"] == "Say how `mysql` gets its credentials"
    timeout = "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout"
    (row,) = _commands(tmp_path / "k", lambda s: s.bash("kubectl get pods", timeout, exit_code=1))
    assert row["recommendations"][0]["title"] == (
        "Say how to start the Kubernetes cluster before `kubectl get`"
    )


def test_a_push_rejected_session_after_session_gets_advice(tmp_path: Path) -> None:
    rejected = (
        " ! [rejected]        main -> main (non-fast-forward)\n"
        "error: failed to push some refs to 'github.com:acme/shop.git'"
    )

    def build(s: Session) -> None:
        s.bash("git push", rejected, exit_code=1).read("/repo/a.py")

    (row,) = _commands(tmp_path, build, count=3)
    (advice,) = row["recommendations"]
    assert advice["id"] == "pull-before-pushing"
    assert "rejected 3 times in 3 sessions" in advice["detail"]
    assert row["est_minutes_lost"] == 0  # a habit to name, not time to claim
    # twice is how parallel work goes: a capped lead, no advice
    (row,) = _commands(tmp_path / "twice", build, count=2)
    assert row["recommendations"] == [] and row["score"] <= 19


def test_a_test_red_only_after_the_agents_own_change_is_not_a_standing_failure(
    tmp_path: Path,
) -> None:
    red = "FAILED tests/test_legacy.py::test_old - AssertionError\n1 failed, 40 passed in 2.0s"

    def after_edit(s: Session) -> None:
        s.edit("/repo/src/a.py").bash("pytest", red, exit_code=1).read("/repo/a.py")

    (row,) = _commands(tmp_path, after_edit)
    assert row["recommendations"] == [] and row["score"] <= 19
    assert all("session after session" not in e["summary"] for e in row["evidence"])

    # red before anything was changed, in the same number of sessions: it was broken already
    def on_arrival(s: Session) -> None:
        s.bash("pytest", red, exit_code=1).edit("/repo/src/a.py").bash("pytest", red, exit_code=1)

    (row,) = _commands(tmp_path / "standing", on_arrival)
    assert [r["id"] for r in row["recommendations"]] == ["say-if-it-is-known-broken"]

    # edit and re-run, round after round: that is not converging, and it is said so
    def rounds(s: Session) -> None:
        for _ in range(4):
            s.edit("/repo/src/a.py").bash("pytest", red, exit_code=1)

    (row,) = _commands(tmp_path / "rounds", rounds)
    assert [r["id"] for r in row["recommendations"]] == ["explain-the-failing-check"]


# ---- round 23 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "minutes"),
    [
        # let through by the permission settings: the run time is the command's
        ({"decision": "accept", "source": "config"}, 4.7),
        # a person approved it: only what followed the result
        ({"decision": "accept", "source": "user"}, 0.2),
        # the transcript does not say: call to result counts up to 30 s
        (None, 0.7),
    ],
)
def test_time_at_a_permission_prompt_is_not_lost_time(
    tmp_path: Path, decision: dict[str, str] | None, minutes: float
) -> None:
    denied = "Error: No valid credential sources found"
    sessions = []
    for i in range(2):
        s = Session(f"s{i}", cwd="/repo").user("go")
        s.decision = decision
        s.bash("terraform plan", denied, exit_code=1, took=270).tick(10).read("/repo/a.py")
        sessions.append(s)
    (row,) = scan(tmp_path, sessions)["commands"]
    # two events, each in its own session
    assert row["est_minutes_lost"] == pytest.approx(2 * minutes, abs=0.11)


def test_an_unknown_wait_is_bounded_by_the_commands_usual_run_time(tmp_path: Path) -> None:
    denied = "Error: No valid credential sources found"
    s = Session(cwd="/repo").user("go")
    for _ in range(3):
        s.bash("terraform plan", "No changes.", took=90)
    s.bash("terraform plan", denied, exit_code=1, took=270).tick(10).read("/repo/a.py")
    (found,) = builders.detect(s, tmp_path)
    assert found.interval is not None
    assert found.interval[1] - found.interval[0] == pytest.approx(90 + 14)


def test_strong_events_leave_out_what_is_routine_on_its_own(tmp_path: Path) -> None:
    def session(i: int) -> Session:
        s = Session(f"s{i}", cwd="/repo").user("go")
        s.bash("pytest", "1 failed in 1s", exit_code=1).read("/repo/a.py")  # a red test
        s.grep("Nothing", "/repo/src")  # a search that found nothing
        s.edit("/repo/src/a.py", error=builders.EDIT_NOT_FOUND)  # an edit that missed
        return s

    totals = scan(tmp_path, [session(0), session(1)])["totals"]
    assert (totals["friction_events"], totals["strong_events"]) == (6, 2)
    quiet = scan(
        tmp_path / "q", [Session(cwd="/repo").user("go").bash("pytest", "1 failed", exit_code=1)]
    )
    assert (quiet["totals"]["friction_events"], quiet["totals"]["strong_events"]) == (1, 0)


# ---- round 24 ------------------------------------------------------------------------------


def test_the_alias_hint_names_the_sessions_root_not_where_it_spent_its_time(
    tmp_path: Path,
) -> None:
    from agent_friction.engine import session_directories

    home = str(Path.home())
    old = f"{home}/work/old-name"
    # started at the root, worked in web/: the root is what an alias must name
    web = Session("web", cwd=old).user("go").bash("ls", "x")
    web.cwd = f"{old}/web"
    for _ in range(4):
        web.bash("npm test", "ok")
    # started in the home directory and moved in: the files say where the project is
    moved = Session("moved", cwd=home).user("go").bash("ls", "x")
    moved.cwd = f"{old}/web"
    moved.edit(f"{old}/src/a.py").edit(f"{old}/web/b.ts")
    # no file touched and nothing but the home directory above: where it worked
    bare = Session("bare", cwd=home).user("go").bash("ls", "x")
    bare.cwd = "/srv/other/api"
    bare.bash("make", "ok").bash("make", "ok")
    # an excursion to /tmp does not make /tmp the root
    trip = Session("trip", cwd="/srv/third").user("go").bash("ls", "x").bash("ls", "x")
    trip.cwd = "/tmp"
    trip.bash("ls", "x")
    files = [s.save(tmp_path / "t" / s.session_id) for s in (web, moved, bare, trip)]
    options = AnalyzeOptions(repo=Path("/repo"), transcripts=files, include_git=False)
    assert session_directories(options) == [
        ("~/work/old-name", 2),
        ("/srv/other/api", 1),
        ("/srv/third", 1),
    ]
    # unmasked paths come back with forward slashes, which --repo-alias accepts on any OS
    assert session_directories(options, limit=1, mask_home=False) == [(old.replace("\\", "/"), 2)]


def test_the_repositorys_own_name_stays_readable_without_excerpts(tmp_path: Path) -> None:
    repo = tmp_path / "shop_front"
    repo.mkdir()
    sessions = []
    for i in range(3):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        s.bash("shop-front serve", "bash: shop-front: command not found", exit_code=127)
        s.bash("zebractl sync", "bash: zebractl: command not found", exit_code=127)
        sessions.append(s.read(f"{repo}/a.py").save(tmp_path / "t" / f"s{i}"))
    options = AnalyzeOptions(repo=repo, transcripts=sessions, include_git=False, now=NOW)
    bare = analyze(dataclasses.replace(options, include_excerpts=False))
    paths = sorted(row["path"] for row in bare["commands"])
    assert paths[1] == "shop-front" and re.fullmatch(r"<tool:\w+>", paths[0])
    assert "zebractl" not in json.dumps(bare)
    # evidence names no transcript there, and so no line of one either
    evidence = [e for row in bare["commands"] for e in row["evidence"]]
    assert evidence and all(e["source"] is None and e["line"] is None for e in evidence)
    full = analyze(options)
    assert all(e["line"] > 0 for row in full["commands"] for e in row["evidence"])


def _noisy_repo(
    tmp_path: Path, sessions: int, rate: float, hot: int = 0, seed: int = 7
) -> tuple[Path, list[Path]]:
    """Healthy sessions over 15 files; before each edit a miss with probability ``rate``.

    In the first ``hot`` sessions the agent also misses twice on ``src/hot.py``.
    """
    import random

    rng = random.Random(seed)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    names = [f"src/mod_{i:02d}.py" for i in range(15)]
    for name in [*names, "src/hot.py"]:
        (repo / name).write_text("x = 1\n" * 40)
    files = []
    for i in range(sessions):
        s = Session(f"s{i:03d}", cwd=str(repo)).user("do the task")
        for _ in range(6):
            name = rng.choice(names)
            s.read(f"{repo}/{name}")
            if rng.random() < rate:
                s.edit(f"{repo}/{name}", error=builders.EDIT_NOT_FOUND)
            s.edit(f"{repo}/{name}")
        if i < hot:
            s.read(f"{repo}/src/hot.py")
            s.edit(f"{repo}/src/hot.py", error=builders.EDIT_NOT_FOUND)
            s.edit(f"{repo}/src/hot.py", error=builders.EDIT_NOT_FOUND)
            s.edit(f"{repo}/src/hot.py")
        s.bash("pytest -q", "5 passed in 0.1s")
        files.append(s.save(tmp_path / "t" / f"s{i:03d}"))
    return repo, files


def test_uniform_background_noise_is_one_fact_not_a_pattern_per_file(tmp_path: Path) -> None:
    repo, files = _noisy_repo(tmp_path, 60, 0.06)
    out = analyze(AnalyzeOptions(repo=repo, transcripts=files, include_git=False, now=NOW))
    assert out["totals"]["friction_events"] >= 15 and out["totals"]["strong_events"] == 0
    assert len(out["hotspots"]) >= 8
    for row in out["hotspots"]:
        assert row["recommendations"] == [] and row["confidence"] == "low"
        assert row["score"] <= scoring.WEAK_ONLY_SCORE_CAP
    (item,) = out["recommendations"]
    assert item["id"] == "note-the-background-rate" and item["signals"] == ["edit-miss"]
    assert item["above"] == [] and item["events"] == out["totals"]["friction_events"]
    assert item["title"] == (
        f"Edit misses run at {100 * item['hit_visits'] / item['visits']:.0f}% of file visits "
        "across the repository"
    )
    assert "No file is clearly above that rate" in item["detail"]
    assert "formatter" not in item["detail"] and 0.04 < item["rate"] < 0.12


def test_a_file_clearly_above_the_noise_still_gets_its_advice(tmp_path: Path) -> None:
    repo, files = _noisy_repo(tmp_path, 40, 0.06, hot=8)
    out = analyze(AnalyzeOptions(repo=repo, transcripts=files, include_git=False, now=NOW))
    top = out["hotspots"][0]
    assert top["path"] == "src/hot.py" and top["confidence"] == "medium"
    assert [r["id"] for r in top["recommendations"]] == ["make-edits-land"]
    assert top["recommendations"][0]["title"] == "Edits to this file keep missing their target"
    assert (
        "clearly more often than in the rest of this repository"
        in (top["recommendations"][0]["detail"])
    )
    assert all(row["recommendations"] == [] for row in out["hotspots"][1:])
    (item,) = out["recommendations"]
    assert item["above"] == ["src/hot.py"]
    assert (
        "1 file is clearly above that rate and has its own entry: `src/hot.py`." in item["detail"]
    )


def test_a_formatter_between_read_and_edit_is_the_evidence_for_saying_so(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.read("/repo/a.py").edit("/repo/a.py")
        s.bash("ruff format .", "1 file reformatted")
        s.edit("/repo/a.py", error=builders.EDIT_NOT_FOUND)
        s.read("/repo/a.py")
        s.edit("/repo/a.py", error=builders.EDIT_NOT_FOUND)  # read since: plain misremembering

    s = Session("s1", cwd="/repo").user("go")
    build(s)
    first, second = by_signal(detect(s, tmp_path), signals.EDIT_MISS)
    assert first.detail.get("rewritten") == "1" and "rewritten" not in second.detail


def test_too_little_of_a_signal_is_no_repository_level_item(tmp_path: Path) -> None:
    out = story()
    assert out["recommendations"] == []
    schema = entry(out["hotspots"], SCHEMA)
    assert "edit-the-source-not-the-output" in [r["id"] for r in schema["recommendations"]]


# ---- round 26 ------------------------------------------------------------------------------


def test_a_directory_a_failed_command_was_pointed_at_is_not_shown_without_excerpts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "a.py").write_text("x\n")
    files = []
    for i in range(3):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        s.bash("make -C NOPE1 build", "make: *** NOPE1: No such file or directory.", exit_code=2)
        s.bash("npm --prefix NOPE2 test", "npm error enoent no package.json", exit_code=254)
        s.bash(
            "(cd NOPE3; cargo test)", "error: could not find `Cargo.toml` in `/x`", exit_code=101
        )
        s.bash("pushd NOPE4 && go test ./...", "go: go.mod file not found", exit_code=1)
        s.bash("npm --prefix web test", 'npm error Missing script: "test"', exit_code=1)
        files.append(s.read(f"{repo}/a.py").save(tmp_path / "t" / f"s{i}"))
    options = AnalyzeOptions(repo=repo, transcripts=files, include_git=False, now=NOW)
    full = analyze(options)
    assert "make build (NOPE1/)" in [row["path"] for row in full["commands"]]
    bare = analyze(dataclasses.replace(options, include_excerpts=False))
    text = json.dumps(bare)
    assert "NOPE" not in text  # path, exposure, summaries, recommendation titles
    paths = sorted(row["path"] for row in bare["commands"])
    assert "npm test (web/)" in paths  # a directory that is there keeps its name
    hidden = [p for p in paths if p != "npm test (web/)"]
    assert len(hidden) == 4 and all(re.fullmatch(r".+ \(<path:\w{6}>/\)", p) for p in hidden)
    assert sorted(bare["exposure"]["command"]) == paths
    titles = [r["title"] for row in bare["commands"] for r in row["recommendations"]]
    assert any(re.search(r"in `<path:\w{6}>/`", title) for title in titles)


def test_a_command_through_a_launcher_is_the_working_command(tmp_path: Path) -> None:
    missing = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'requests'"

    def build(s: Session) -> None:
        s.bash("python scripts/gen.py", missing, exit_code=1).bash("ls", "x")
        s.bash("uv run python scripts/gen.py", "ok")
        s.bash("uv run python scripts/seed.py", "ok")

    rows = _commands(tmp_path, build)
    (row,) = [r for r in rows if r["recommendations"]]
    (advice,) = row["recommendations"]
    assert row["path"] == "python scripts/gen.py"
    assert advice["id"] == "use-the-working-command"
    assert advice["title"] == "Tell agents to use `uv run python scripts/gen.py`"
    assert advice["replacement"] == "uv run python scripts/gen.py"


# ---- round 27 ------------------------------------------------------------------------------


def test_story_the_undo_that_answers_a_correction_is_not_counted_again(
    result: dict[str, Any],
) -> None:
    service = entry(result["hotspots"], SERVICE)
    corrected = next(
        e
        for e in service["evidence"]
        if e["signal"] == "user-correction" and "legacy_charge again" in (e["excerpt"] or "")
    )
    reverts = [e for e in service["evidence"] if e["signal"] == "edit-revert"]
    # the one revert left is the agent's own, made before anybody said anything
    assert len(reverts) == 1 and reverts[0]["line"] < corrected["line"]
    assert reverts[0]["summary"].endswith("after a check had run on it")
    assert "add-a-fast-check" not in [r["id"] for r in service["recommendations"]]


def test_with_nothing_else_edited_the_advice_does_not_speak_of_the_rest(tmp_path: Path) -> None:
    sessions = []
    for i in range(3):
        s = Session(f"s{i}").user("go")
        s.edit("/repo/src/only.py", error=builders.EDIT_NOT_FOUND)
        s.edit("/repo/src/only.py", error=builders.EDIT_NOT_FOUND)
        sessions.append(s)
    (row,) = scan(tmp_path, sessions)["hotspots"]
    (rec,) = row["recommendations"]
    assert rec["id"] == "make-edits-land" and "rest of this repository" not in rec["detail"]
    assert "Too little else was edited here to compare against" in rec["detail"]


def _failing_tools(count: int, tool: str = "mcp__postgres__query") -> list[Session]:
    sessions = []
    for i in range(count):
        s = Session(f"s{i}").user("look it up")
        s.tool(tool, {"sql": "select 1"}, "connection to db.internal refused", is_error=True)
        sessions.append(s)
    return sessions


def test_recurring_failures_of_other_tools_are_weak_unattributed_events(tmp_path: Path) -> None:
    sessions = _failing_tools(3)
    sessions[0].tool("WebFetch", {"url": "https://example.com"}, "403 Forbidden", is_error=True)
    sessions[1].tool("WebFetch", {"url": "https://example.com"}, "403 Forbidden", is_error=True)
    sessions[1].tool("Task", {"prompt": "x"}, "API Error: overloaded", is_error=True)  # once
    out = scan(tmp_path, sessions)
    totals = out["totals"]
    assert totals["tool_failures"] == 5 == totals["unattributed_events"]
    assert totals["friction_events"] == 5 and totals["strong_events"] == 0
    assert totals["est_minutes_lost"] == 0
    assert out["tool_failures"] == [
        {"tool": "mcp__postgres__query", "events": 3, "sessions": 3},
        {"tool": "WebFetch", "events": 2, "sessions": 2},
    ]
    assert out["hotspots"] == out["commands"] == out["directories"] == []
    assert [e["signal"] for e in out["unattributed"]] == ["tool-failure"] * 5
    first = out["unattributed"][0]
    assert first["summary"] == "mcp__postgres__query returned an error"
    assert first["excerpt"] == "connection to [REDACTED:internal-host] refused"
    counted = {s["id"]: s["count"] for s in out["signals"]}
    assert counted["tool-failure"] == 5
    # a tool that failed once in the whole scan is an accident
    out = scan(tmp_path / "once", _failing_tools(1))
    assert out["totals"]["tool_failures"] == 0 == out["totals"]["friction_events"]
    assert out["tool_failures"] == [] and out["unattributed"] == []


def test_without_excerpts_a_failing_tool_is_named_by_a_placeholder(tmp_path: Path) -> None:
    out = scan(tmp_path, _failing_tools(2), include_excerpts=False)
    hidden = public_tool("mcp__postgres__query")
    assert "<tool:" in hidden and out["tool_failures"][0]["tool"] == hidden
    assert "postgres" not in json.dumps(out) and "db.internal" not in json.dumps(out)
    assert out["unattributed"][0]["summary"] == f"{hidden} returned an error"
    assert out["unattributed"][0]["excerpt"] is None
    # the harness's own tools keep their names
    out = scan(tmp_path / "own", _failing_tools(2, "WebFetch"), include_excerpts=False)
    assert out["tool_failures"] == [{"tool": "WebFetch", "events": 2, "sessions": 2}]


def test_events_outside_the_repository_are_counted_in_the_totals(tmp_path: Path) -> None:
    denied = "Permission to use Bash with command rm has been denied."
    s = Session("s0").user("go")
    for _ in range(3):
        s.tool("Bash", {"command": "cd /srv/other && rm -rf build"}, denied, is_error=True)
    s.tool("Bash", {"command": "rm -rf build"}, denied, is_error=True)
    s.bash("cd /srv/other && make test", "boom", exit_code=2)
    out = scan(tmp_path, [s])
    assert out["totals"]["events_outside_repo"] == 4
    assert out["totals"]["friction_events"] == 1
    assert [c["path"] for c in out["commands"]] == ["rm"]
    quiet = scan(tmp_path / "q", [Session("s1").user("go").bash("make test", "ok")])
    assert quiet["totals"]["events_outside_repo"] == 0


def test_one_command_run_from_several_directories_is_tied_together(tmp_path: Path) -> None:
    missing = "ModuleNotFoundError: No module named 'app'"
    sessions = []
    for i, where in enumerate(["/repo", "/repo/src", "/repo/tests", "/repo/src"]):
        s = Session(f"s{i}", cwd=where).user("go")
        s.bash("pytest", missing, exit_code=2)
        s.bash("ruff check .", "ruff: command not found", exit_code=127)
        sessions.append(s)
    out = scan(tmp_path, sessions)
    rows = {c["path"]: c for c in out["commands"]}
    family = {
        "command": "pytest",
        "members": ["pytest", "pytest (src/)", "pytest (tests/)"],
        "events": 4,
        "sessions": 4,
    }
    for path in family["members"]:  # type: ignore[union-attr]
        assert rows[path]["family"] == family
    assert rows["pytest (src/)"]["events"] == 2  # each row keeps its own numbers
    # the family is not cut by max_hotspots, and a command run in one place has none
    cut = scan(tmp_path / "cut", sessions, max_hotspots=1)
    assert len(cut["commands"]) == 1 and cut["commands"][0]["family"]["events"] in (4,)
    alone = scan(tmp_path / "alone", sessions[:1])
    assert [c["family"] for c in alone["commands"]] == [None] * len(alone["commands"])
    assert "family" not in (out["hotspots"] or [{}])[0]


def test_a_missing_make_target_is_answered_with_the_targets_that_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text(
        ".PHONY: test check\nPY := python\ntest:\n\tpytest\ncheck: test\n\truff check .\n"
        "fmt:\n\truff format .\n"
    )
    sessions = []
    for i in range(2):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        s.bash("make lint", "make: *** No rule to make target 'lint'.  Stop.", exit_code=2)
        sessions.append(s)
    (row,) = scan(tmp_path, sessions, repo)["commands"]
    (rec,) = row["recommendations"]
    assert rec["id"] == "name-the-missing-target"
    assert rec["title"] == "`make lint` does not exist: say what to run instead"
    assert "What it does define: `test`, `check`, `fmt`." in rec["detail"]
    assert "Document how to run" not in rec["title"]
    # without excerpts neither the missing name nor the Makefile's targets are shown
    (row,) = scan(tmp_path / "bare", sessions, repo, include_excerpts=False)["commands"]
    (rec,) = row["recommendations"]
    assert rec["id"] == "name-the-missing-target"
    assert "the Makefile has no target." in rec["detail"]
    assert "What it does define" not in rec["detail"] and "fmt" not in json.dumps(row)


# ---- round 28 ------------------------------------------------------------------------------


def _untimed(tmp_path: Path, name: str, timed: bool) -> Path:
    base: dict[str, object] = {"session": name, "cwd": "/repo"}
    events: list[dict[str, object]] = [
        {**base, "kind": "user_message", "text": "build it"},
        {**base, "kind": "tool_call", "id": 1, "op": "shell", "command": "make build"},
        {
            **base,
            "kind": "tool_result",
            "id": 1,
            "ok": False,
            "exit_code": 127,
            "output": "make: cc: command not found",
        },
    ]
    if timed:
        for index, event in enumerate(events):
            event["ts"] = f"2026-11-05T10:00:{index:02d}Z"
    path = tmp_path / f"{name}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    return path


def test_sessions_without_timestamps_are_in_no_window(tmp_path: Path) -> None:
    files = [_untimed(tmp_path, "bare", False), _untimed(tmp_path, "dated", True)]
    moment = datetime(2026, 11, 6, tzinfo=timezone.utc)

    def totals(**window: datetime) -> dict[str, Any]:
        options = AnalyzeOptions(
            repo=Path("/repo"), transcripts=files, include_git=False, now=NOW, **window
        )
        out: dict[str, Any] = analyze(options)["totals"]
        return out

    # no window: the untimed session is analysed like any other
    full = totals()
    assert (full["sessions"], full["friction_events"]) == (2, 2)
    assert full["untimed_sessions_excluded"] == 0
    # either bound alone leaves it out, and says so
    before, after = totals(until=moment), totals(since=moment)
    assert (before["sessions"], before["friction_events"]) == (1, 1)
    assert (after["sessions"], after["friction_events"]) == (0, 0)
    assert before["untimed_sessions_excluded"] == after["untimed_sessions_excluded"] == 1
    # so a before/after pair no longer counts it twice
    assert before["friction_events"] + after["friction_events"] == 1
    both = totals(since=datetime(2026, 11, 1, tzinfo=timezone.utc), until=moment)
    assert both["untimed_sessions_excluded"] == 1 and both["sessions"] == 1


def test_an_untimed_session_of_another_repository_is_not_counted_as_excluded(
    tmp_path: Path,
) -> None:
    path = _untimed(tmp_path, "bare", False)
    out = analyze(
        AnalyzeOptions(
            repo=Path("/elsewhere"),
            transcripts=[path],
            include_git=False,
            now=NOW,
            since=datetime(2026, 11, 1, tzinfo=timezone.utc),
            allow_no_transcripts=True,
        )
    )
    assert out["totals"]["untimed_sessions_excluded"] == 0


def test_story_has_no_untimed_sessions(result: dict[str, Any]) -> None:
    assert result["totals"]["untimed_sessions_excluded"] == 0
    assert story(since=datetime(2026, 11, 1))["totals"]["untimed_sessions_excluded"] == 0


def test_a_workspace_selector_puts_the_command_in_its_directory(tmp_path: Path) -> None:
    missing = "ERR_PNPM_NO_SCRIPT  Missing script: test"
    lines = [
        "pnpm --filter web test",
        "pnpm --filter=web test",
        "pnpm -F web test",
        "cd web && pnpm test",
    ]
    sessions = []
    for i, line in enumerate(lines):
        s = Session(f"s{i}").user("go").bash(line, missing, exit_code=1)
        s.bash("yarn workspace web test", 'error Command "test" not found.', exit_code=1)
        s.bash("npm --prefix web test", 'npm error Missing script: "test"', exit_code=1)
        s.bash("pnpm --filter @acme/web test", missing, exit_code=1)
        s.bash("pip3 install nope", "ERROR: No matching distribution found", exit_code=1)
        s.bash("pip install nope", "ERROR: No matching distribution found", exit_code=1)
        sessions.append(s)
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    for s in sessions:
        s.cwd = str(repo)
    sessions = [_rebased(s, str(repo)) for s in sessions]
    rows = {c["path"]: c["events"] for c in scan(tmp_path, sessions, repo)["commands"]}
    assert rows["pnpm test (web/)"] == 4 and rows["yarn test (web/)"] == 4
    assert rows["npm test (web/)"] == 4
    assert rows["pnpm test"] == 4  # the scoped name says nothing about a directory
    assert not [key for key in rows if "pip3" in key or "workspace" in key or "filter" in key]


def _rebased(session: Session, root: str) -> Session:
    """The same transcript, recorded in ``root`` instead of ``/repo``."""
    session.lines = [line.replace('"/repo"', json.dumps(root)) for line in session.lines]
    return session


def test_a_workspace_name_is_a_directory_only_where_the_repository_has_one(
    tmp_path: Path,
) -> None:
    def rows(repo: Path, where: Path) -> list[str]:
        sessions = []
        for i in range(2):
            s = Session(f"s{i}", cwd=str(repo)).user("go")
            s.bash(
                "pnpm --filter web test", "ERR_PNPM_NO_SCRIPT  Missing script: test", exit_code=1
            )
            sessions.append(s)
        return [c["path"] for c in scan(where, sessions, repo)["commands"]]

    nested = tmp_path / "nested"
    (nested / "packages" / "web").mkdir(parents=True)
    (nested / "node_modules" / "web").mkdir(parents=True)  # not the repository's own
    assert rows(nested, tmp_path / "a") == ["pnpm test (packages/web/)"]
    direct = tmp_path / "direct"
    (direct / "web").mkdir(parents=True)
    (direct / "packages" / "web").mkdir(parents=True)
    assert rows(direct, tmp_path / "b") == ["pnpm test (web/)"]  # the directory itself wins
    twice = tmp_path / "twice"
    (twice / "packages" / "web").mkdir(parents=True)
    (twice / "apps" / "web").mkdir(parents=True)
    assert rows(twice, tmp_path / "c") == ["pnpm test"]  # two candidates: no answer
    none = tmp_path / "none"
    none.mkdir()
    assert rows(none, tmp_path / "d") == ["pnpm test"]  # the package is called web, no more


# ---- round 30 ------------------------------------------------------------------------------


def _package_sessions(tmp_path: Path, repo: Path, cwd: Path | None = None) -> list[Session]:
    sessions = []
    for i in range(3):
        s = Session(f"s{i}", cwd=str(cwd or repo)).user("go")
        where = "" if cwd else "cd packages/web && "
        s.bash(f"{where}npm test", 'npm error Missing script: "test"', exit_code=1)
        s.bash(f"{where}pnpm test", "3 passed")
        sessions.append(s)
    return sessions


def test_a_command_documented_for_its_directory_counts_as_documented(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "packages" / "web").mkdir(parents=True)
    sessions = _package_sessions(tmp_path, repo)
    (row,) = scan(tmp_path, sessions, repo)["commands"]
    (rec,) = row["recommendations"]
    assert row["path"] == "npm test (packages/web/)" and rec["id"] == "use-the-working-command"
    # the title names the command as it is typed; the directory is said in the text
    assert rec["title"] == "Tell agents to use `pnpm test`" and rec["documented_in"] == []
    assert "`npm test` in `packages/web/` failed 3 times in 3 sessions" in rec["detail"]
    assert "got the same job done with `pnpm test` in `packages/web/`" in rec["detail"]
    assert 'such as: "in `packages/web/`, use `pnpm test`, not `npm test`".' in rec["detail"]
    assert rec["replacement"] == "pnpm test (packages/web/)"  # the key, as in `exposure`
    # the tool's own suggested line, written down: that is an instruction, not a mention
    (repo / "AGENTS.md").write_text(
        "# Agents\n\nIn `packages/web/`, use `pnpm test`, not `npm test`.\n"
    )
    (row,) = scan(tmp_path / "again", sessions, repo)["commands"]
    (rec,) = row["recommendations"]
    assert rec["documented_as"] == "instruction"
    assert rec["documented_in"] == [{"file": "AGENTS.md", "line": 3}]
    assert rec["title"] == "`pnpm test` is documented, yet agents run `npm test`"
    assert rec["detail"].startswith(
        "`AGENTS.md:3` already names `pnpm test`; agents still ran `npm test` in `packages/web/`"
    )
    assert "(packages/web/)" not in rec["title"] + rec["detail"]
    # a line that rules the working command out documents nothing
    (repo / "AGENTS.md").write_text("# Agents\n\nNever use `pnpm test` here.\n")
    (row,) = scan(tmp_path / "ruled-out", sessions, repo)["commands"]
    assert row["recommendations"][0].get("documented_as") != "instruction"


def test_a_package_scan_reads_the_instruction_files_above_it(tmp_path: Path) -> None:
    repo = tmp_path / "mono"
    web = repo / "packages" / "web"
    web.mkdir(parents=True)
    (repo / ".git").mkdir()  # the repository root: nothing above it is read
    (tmp_path / "AGENTS.md").write_text("Use `pnpm test`.\n")
    (repo / "AGENTS.md").write_text("# Agents\n\nIn `packages/web/`, use `pnpm test`.\n")
    sessions = _package_sessions(tmp_path, web, cwd=web)
    (row,) = scan(tmp_path, sessions, web)["commands"]
    (rec,) = row["recommendations"]
    assert row["path"] == "npm test" and rec["documented_as"] == "instruction"
    assert rec["documented_in"] == [{"file": "../../AGENTS.md", "line": 3}]
    assert rec["detail"].startswith("`../../AGENTS.md:3` already names `pnpm test`")
    # the package's own file comes first, the one in between is read too
    (web / "AGENTS.md").write_text("Run `pnpm test`.\n")
    (repo / "packages" / "CLAUDE.md").write_text("x\n\nuse `pnpm test`\n")
    (row,) = scan(tmp_path / "own", sessions, web)["commands"]
    assert [m["file"] for m in row["recommendations"][0]["documented_in"]] == [
        "AGENTS.md",
        "../CLAUDE.md",
        "../../AGENTS.md",
    ]
    # with an alias the sessions were recorded elsewhere: what lies above this copy is
    # not what those agents loaded
    (web / "AGENTS.md").unlink()
    aliased = scan(tmp_path / "alias", sessions, web, repo_aliases=["/work/mono/packages/web"])
    assert aliased["commands"][0]["recommendations"][0]["documented_in"] == []


def test_guidance_reads_nothing_above_a_root_or_outside_a_repository(tmp_path: Path) -> None:
    from agent_friction.guidance import Guidance

    (tmp_path / "AGENTS.md").write_text("Use `pnpm test`.\n")
    plain = tmp_path / "plain" / "pkg"
    plain.mkdir(parents=True)  # no .git anywhere above: not inside a repository
    assert Guidance(plain).mentions(["pnpm test"]) == []
    root = tmp_path / "root"
    (root / ".git").mkdir(parents=True)  # the scanned directory is the root itself
    assert Guidance(root).mentions(["pnpm test"]) == []
    inside = root / "pkg"
    inside.mkdir()
    (root / "AGENTS.md").write_text("Use `pnpm test`.\n")
    assert [str(m) for m in Guidance(inside).mentions(["pnpm test"])] == ["../AGENTS.md:1"]
    assert Guidance(inside, parents=False).mentions(["pnpm test"]) == []


def _renamed(tmp_path: Path) -> tuple[Path, list[Session]]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "api.ts").write_text("export const a = 1;\n" * 40)
    (repo / "src" / "old.ts").write_text("x\n")
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_DATE": "2026-11-03T12:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-11-03T12:00:00+00:00",
    }
    identity = ["-c", "user.name=Dev", "-c", "user.email=dev@example.test"]
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        [*identity, "commit", "-q", "-m", "init"],
        ["mv", "src/api.ts", "src/client.ts"],
        ["rm", "-q", "src/old.ts"],
        [*identity, "commit", "-q", "-m", "rename the client"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    sessions = []
    for i in range(3):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        for name in ("api.ts", "old.ts", "never.ts"):
            s.edit(f"{repo}/src/{name}", error=builders.EDIT_NOT_FOUND)
            s.edit(f"{repo}/src/{name}", error=builders.EDIT_NOT_FOUND)
        sessions.append(s)
    return repo, sessions


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_renamed_or_deleted_file_is_said_to_be_gone(tmp_path: Path) -> None:
    repo, sessions = _renamed(tmp_path)
    rows = {h["path"]: h for h in scan(tmp_path, sessions, repo, include_git=True)["hotspots"]}
    api, old, never = rows["src/api.ts"], rows["src/old.ts"], rows["src/never.ts"]
    assert api["file"] == {
        "exists": False,
        "lines": None,
        "generated": False,
        "removed_at": "2026-11-03T12:00:00+00:00",
        "renamed_to": "src/client.ts",
    }
    (rec,) = api["recommendations"]
    assert rec["id"] == "file-is-gone"
    assert rec["title"] == "This file was renamed to `src/client.ts`"
    assert "renamed to `src/client.ts` on 2026-11-03" in rec["detail"]
    assert "scan with `--since 2026-11-03` to leave this history out." in rec["detail"]
    assert "CRLF" not in rec["detail"] and rec["signals"] == ["edit-miss"]
    assert (old["file"]["removed_at"], old["file"]["renamed_to"]) == (
        "2026-11-03T12:00:00+00:00",
        None,
    )
    (rec,) = old["recommendations"]
    assert rec["title"] == "This file is no longer in the tree"
    assert "It was removed on 2026-11-03" in rec["detail"]
    # a path git never knew is not "gone": it keeps the advice the events call for
    assert (never["file"]["removed_at"], never["file"]["renamed_to"]) == (None, None)
    assert [r["id"] for r in never["recommendations"]] == ["make-edits-land"]
    # without git nothing is known, and nothing is claimed
    plain = scan(tmp_path / "plain", sessions, repo, include_git=False)["hotspots"]
    for row in plain:
        assert row["file"]["removed_at"] is None and row["file"]["renamed_to"] is None
        assert [r["id"] for r in row["recommendations"]] == ["make-edits-land"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_what_a_human_said_about_a_file_that_is_gone_is_still_passed_on(tmp_path: Path) -> None:
    repo, _ = _renamed(tmp_path)
    (repo / "src" / "api.ts").write_text("x\n")  # there while the sessions ran
    sessions = []
    for i in range(2):
        s = Session(f"s{i}", cwd=str(repo)).user("go").edit(f"{repo}/src/api.ts", "a", "b")
        s.user("No, never touch the retry logic in src/api.ts")
        s.edit(f"{repo}/src/api.ts", "b", "c")
        sessions.append(s)
    files = [s.save(tmp_path / "t" / s.session_id) for s in sessions]
    (repo / "src" / "api.ts").unlink()
    out = analyze(AnalyzeOptions(repo=repo, transcripts=files, include_git=True))
    api = entry(out["hotspots"], "src/api.ts")
    assert [r["id"] for r in api["recommendations"]] == ["file-is-gone", "write-down-the-rule"]
    assert (
        "read the advice below as being about the work, not the file"
        in (api["recommendations"][0]["detail"])
    )


def test_a_rejected_push_that_a_pull_put_right_is_a_habit_only_when_it_recurs(
    tmp_path: Path,
) -> None:
    rejected = (
        " ! [rejected]        main -> main (non-fast-forward)\n"
        "error: failed to push some refs to 'github.com:acme/shop.git'"
    )

    def build(s: Session) -> None:
        s.bash("git push", rejected, exit_code=1)
        s.bash("git pull --rebase && git push", "To github.com:acme/shop.git")

    sessions = [Session(f"s{i}").user("go") for i in range(4)]
    for s in sessions:
        build(s)
    out = scan(tmp_path, sessions)
    (row,) = out["commands"]
    assert row["path"] == "git push" and row["events"] == 4
    assert [r["id"] for r in row["recommendations"]] == ["pull-before-pushing"]
    assert "rejected 4 times in 4 sessions" in row["recommendations"][0]["detail"]
    assert out["totals"]["dropped_red_runs"] == 0
    # once or twice, and put right at once: the loop working, not reported
    out = scan(tmp_path / "twice", sessions[:2])
    assert out["commands"] == [] and out["totals"]["dropped_red_runs"] == 2


def test_a_hidden_command_name_is_not_offered_for_documentation(tmp_path: Path) -> None:
    sessions = []
    for i in range(2):
        s = Session(f"s{i}").user("go")
        s.bash("acmectl sync", "acmectl: error: unrecognized arguments: sync", exit_code=2)
        s.bash("acmectl sync", "acmectl: error: unrecognized arguments: sync", exit_code=2)
        sessions.append(s)
    (row,) = scan(tmp_path, sessions)["commands"]
    assert [r["title"] for r in row["recommendations"]] == ["Document how to run `acmectl`"]
    (row,) = scan(tmp_path / "bare", sessions, include_excerpts=False)["commands"]
    assert row["path"] == public_key("acmectl") and "acmectl" not in json.dumps(row)
    assert [r["title"] for r in row["recommendations"]] == [
        "Document how to run this command (its name is hidden without excerpts)"
    ]


# ---- round 31 ------------------------------------------------------------------------------


def test_an_ad_hoc_snippet_is_never_the_step_that_fixed_a_command(tmp_path: Path) -> None:
    red = "FAILED tests/test_sync.py::test_pull - AssertionError: 3 != 4\n1 failed, 9 passed"

    def snippets(s: Session) -> None:
        number = s.session_id[-1]
        s.bash("pytest", red, exit_code=1)
        s.bash(f"python -c 'import os; print(os.getcwd(), {number})'", "/repo")
        s.bash(f"python3 - <<'EOF'\nimport sys\nprint(sys.version, {number})\nEOF", "3.12")
        s.bash(f"node -e 'console.log({number})'", number)
        s.bash("pytest", "10 passed")

    (row,) = _commands(tmp_path, snippets)
    text = json.dumps(row)
    assert "<inline>" not in text and "<stdin>" not in text
    assert "document-the-setup-step" not in [r["id"] for r in row["recommendations"]]
    # nothing was changed in between, so the test went green untouched: that is flakiness
    assert [r["id"] for r in row["recommendations"]] == ["fix-the-flaky-test"]

    # a real script, the same one every time, is still a setup step under another name
    def script(s: Session) -> None:
        s.bash("pytest", red, exit_code=1)
        s.bash("python scripts/seed.py", "seeded")
        s.bash("pytest", "10 passed")

    (row,) = _commands(tmp_path / "script", script)
    # ... and that is the whole advice: a test that passes once the step has run is not
    # "red session after session, and nothing made it pass"
    (rec,) = row["recommendations"]
    assert "session after session" not in json.dumps(row)
    assert rec["id"] == "document-the-setup-step" and rec["setup"] == "python scripts/seed.py"


def _in_a_subdirectory(tmp_path: Path, count: int, red: str, edit: bool = False) -> Any:
    repo = tmp_path / "repo"
    (repo / "src" / "app").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app" / "__init__.py").write_text("")
    sessions = []
    for i in range(count):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        s.bash("pytest", red, exit_code=2)
        if edit:
            s.edit(f"{repo}/src/app/__init__.py", "a", "b")
        s.bash("cd src && pytest -q", "5 passed in 0.2s")
        sessions.append(s)
    return scan(tmp_path / f"t{count}{edit}", sessions, repo)


@pytest.mark.parametrize(
    "red",
    [
        "E   ModuleNotFoundError: No module named 'app'\n1 error in 0.1s",
        "E   fixture 'client' not found\n1 error in 0.1s",
        # a failing test inside that directory is one the green run there ran too
        "FAILED src/tests/test_a.py::test_a - AssertionError: data.json is empty\n1 failed",
    ],
)
def test_red_at_the_root_and_green_unchanged_in_a_subdirectory_says_where_to_run_it(
    tmp_path: Path, red: str
) -> None:
    out = _in_a_subdirectory(tmp_path, 3, red)
    row = entry(out["commands"], "pytest")
    assert row["score"] > scoring.WEAK_ONLY_SCORE_CAP
    (rec,) = row["recommendations"]
    assert rec["id"] == "document-the-command"
    assert rec["title"] == "Tell agents to run `pytest` in `src/`"
    assert "3 times it then worked in `src/`" in rec["detail"]
    # twice is not yet a habit of the repository
    row = entry(_in_a_subdirectory(tmp_path, 2, red)["commands"], "pytest")
    assert "document-the-command" not in [r["id"] for r in row["recommendations"]]
    # and with an edit in between, the edit is what made it pass
    out = _in_a_subdirectory(tmp_path, 3, red, edit=True)
    assert all(
        "document-the-command" not in [r["id"] for r in row["recommendations"]]
        for row in out["commands"]
    )


STANDING_RED = (
    "FAILED tests/test_billing.py::test_total - AssertionError: 3 != 4\n1 failed, 40 passed in 2s"
)
JS_RED = " FAIL  src/billing/total.test.ts > total\nTests  1 failed | 40 passed (41)"


@pytest.mark.parametrize(
    ("red", "output", "green", "passed"),
    [
        ("make test", STANDING_RED, "pytest tests/test_core.py -q", "12 passed in 0.3s"),
        ("npm test", JS_RED, "npx vitest run src/app/core.test.ts", "Tests  3 passed (3)"),
        ("tox", STANDING_RED, "python -m pytest tests/test_core.py::test_new", "1 passed"),
        ("pytest -q", STANDING_RED, "cd src/app && pytest -q", "12 passed in 0.3s"),
        ("make test", STANDING_RED, "pytest -k core", "12 passed in 0.3s"),
        # nothing on the line narrows it, but it ran a fraction of the tests
        ("make test", STANDING_RED, "pytest", "12 passed in 0.3s"),
    ],
)
def test_a_narrower_green_run_is_no_answer_to_a_standing_red_test(
    tmp_path: Path, red: str, output: str, green: str, passed: str
) -> None:
    def build(s: Session) -> None:
        s.bash(red, output, exit_code=1).bash(green, passed)

    (row,) = _commands(tmp_path, build)
    assert [r["id"] for r in row["recommendations"]] == ["say-if-it-is-known-broken"]
    assert "use `" not in json.dumps(row) and "works_in" not in json.dumps(row)

    # with a stash (or anything else that may change files) in between as well: the
    # red run is not written off as "red, change, green"
    def stashed(s: Session) -> None:
        s.bash(red, output, exit_code=1).bash("git stash", "Saved working directory")
        s.bash(green, passed)

    out = scan(tmp_path / "stash", [stashed_session(i, stashed) for i in range(4)])
    assert out["totals"]["dropped_red_runs"] == 0
    (row,) = out["commands"]
    assert [r["id"] for r in row["recommendations"]] == ["say-if-it-is-known-broken"]


def stashed_session(index: int, build: Any) -> Session:
    s = Session(f"s{index}", cwd="/repo").user("go")
    build(s)
    return s


def test_a_runner_that_was_red_itself_is_not_the_one_to_use(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.bash("make test", STANDING_RED, exit_code=2)
        s.bash("pytest tests/test_billing.py", STANDING_RED, exit_code=1)
        s.bash("pytest tests/test_core.py", "12 passed")

    rows = _commands(tmp_path, build)
    assert "use-the-working-command" not in [r["id"] for c in rows for r in c["recommendations"]]
    assert entry(rows, "make test")["recommendations"][0]["id"] == "say-if-it-is-known-broken"


def test_another_runner_that_ran_as_much_is_still_the_one_to_use(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.bash("yarn test", "error Command failed.\n1 failed, 40 passed", exit_code=1)
        s.bash("pnpm test", "41 passed")

    (row,) = _commands(tmp_path / "all", build)
    assert row["recommendations"][0]["title"] == "Tell agents to use `pnpm test`"

    # a narrower run that ran the very test that failed covers it, too
    def narrower(s: Session) -> None:
        s.bash("make test", STANDING_RED, exit_code=2)
        s.bash("pytest tests/test_billing.py -q", "3 passed")

    (row,) = _commands(tmp_path / "narrow", narrower)
    assert row["recommendations"][0]["title"] == "Tell agents to use `pytest`"

    # and red, edit, green on that test is the development loop, as ever
    def fixed(s: Session) -> None:
        s.bash("make test", STANDING_RED, exit_code=2).edit("/repo/src/billing.py")
        s.bash("pytest tests/test_billing.py -q", "3 passed")

    assert _commands(tmp_path / "fixed", fixed) == []


@pytest.mark.parametrize(
    ("lines", "path"),
    [
        (("npm test --prefix packages/web", "npm --prefix packages/web test"), "npm test"),
        (("pnpm test -C packages/web", "pnpm -C packages/web test"), "pnpm test"),
        (("pnpm test --dir packages/web", "pnpm --dir=packages/web test"), "pnpm test"),
    ],
)
def test_a_directory_option_keys_the_same_on_either_side_of_the_subcommand(
    tmp_path: Path, lines: tuple[str, str], path: str
) -> None:
    repo = tmp_path / "repo"
    (repo / "packages" / "web").mkdir(parents=True)
    sessions = []
    for i, line in enumerate(lines * 2):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        sessions.append(s.bash(line, "sh: 1: jest: not found", exit_code=127))
    (row,) = scan(tmp_path / "t", sessions, repo)["commands"]
    assert row["path"] == f"{path} (packages/web/)" and row["events"] == 4


def test_go_outside_its_module_then_inside_it(tmp_path: Path) -> None:
    def build(s: Session) -> None:
        s.bash(
            "go test ./...",
            "go: directory prefix . does not contain main module or its selected dependencies",
            exit_code=1,
        )
        s.bash("cd svc && go test ./...", "ok  \tshop/svc\t0.2s")

    rows = _commands(tmp_path, build, count=3)
    (rec,) = entry(rows, "go test")["recommendations"]
    assert rec["title"] == "Tell agents to run `go test` in `svc/`"


def test_a_test_a_setup_step_turns_green_is_not_a_standing_failure(tmp_path: Path) -> None:
    red = "FAILED tests/test_sync.py::test_pull - AssertionError: 3 != 4\n1 failed, 9 passed"

    def fixed(s: Session) -> None:
        s.bash("pytest", red, exit_code=1)
        s.bash("docker compose up -d", "started")
        s.bash("pytest", "10 passed")

    (row,) = _commands(tmp_path, fixed)
    assert [r["id"] for r in row["recommendations"]] == ["document-the-setup-step"]
    assert all("session after session" not in e["summary"] for e in row["evidence"])

    # the same test left red in every session is still a standing failure
    def left_red(s: Session) -> None:
        s.bash("pytest", red, exit_code=1).read("/repo/a.py")

    (row,) = _commands(tmp_path / "red", left_red)
    assert [r["id"] for r in row["recommendations"]] == ["say-if-it-is-known-broken"]
    assert (
        "`tests/test_sync.py::test_pull` is red session after session"
        in (row["recommendations"][0]["title"])
    )


@pytest.mark.parametrize(
    ("red", "green", "passed"),
    [
        ("pytest", "pytest -v", "41 passed"),
        ("pytest", "pytest tests/test_billing.py::test_total", "1 passed"),
        ("pytest", "pytest --lf", "1 passed"),
        ("make test", "make test V=1", "41 passed"),
    ],
)
def test_a_test_that_passes_on_a_rerun_with_other_flags_is_flaky_not_standing(
    tmp_path: Path, red: str, green: str, passed: str
) -> None:
    def build(s: Session) -> None:
        s.bash(red, STANDING_RED, exit_code=1).bash(green, passed)

    (row,) = _commands(tmp_path, build)
    assert [r["id"] for r in row["recommendations"]] == ["fix-the-flaky-test"]
    assert "session after session" not in json.dumps(row)
    # below the flaky threshold it is neither: an ordinary failure, weak evidence
    (row,) = _commands(tmp_path / "two", build, count=2)
    assert row["recommendations"] == [] and row["score_detail"]["capped"] is True


def _documented_fallback(tmp_path: Path, text: str) -> dict[str, Any]:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    (repo / "AGENTS.md").write_text(f"# Agents\n\n{text}\n")
    red = "/bin/sh: 1: docker: command not found\nmake: *** [Makefile:3: test] Error 127"
    sessions = []
    for i in range(4):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        sessions.append(s.bash("make test", red, exit_code=2).bash("pytest", "41 passed"))
    (row,) = scan(tmp_path / "t", sessions, repo)["commands"]
    (rec,) = row["recommendations"]
    assert rec["id"] == "use-the-working-command"
    found: dict[str, Any] = rec
    return found


@pytest.mark.parametrize(
    "text",
    [
        "Use `make test`, not `pytest`.",
        "Always run tests with `make test`.",
        "Never run `pytest` directly; use `make test`.",
        "Tests:\n\n```\nmake test\n```",
    ],
)
def test_advice_never_contradicts_an_instruction_the_files_hold(tmp_path: Path, text: str) -> None:
    rec = _documented_fallback(tmp_path, text)
    assert rec["documented_as"] == "conflict" and "replacement" not in rec
    assert rec["title"] == "The documented `make test` does not work where agents run"
    line = rec["documented_in"][0]["line"]
    assert rec["detail"] == (
        f"`AGENTS.md:{line}` tells agents to run `make test`, which failed 4 times in 4 "
        "sessions; agents fell back to `pytest`. Make `make test` work where agents run, or "
        "change that line."
    )
    assert "use `pytest`, not `make test`" not in rec["detail"]


@pytest.mark.parametrize(
    ("text", "documented"),
    [
        ("We used to have a `make test` target.", "mention"),
        # ruling out the command that failed is what the advice would say, too
        ("Do not run `make test` locally, it needs docker.", "mention"),
        ("`make test` is slow.", "mention"),
        ("Use `pytest` to run the tests.", "instruction"),
        ("Format with `ruff format`.", None),
    ],
)
def test_a_line_that_does_not_say_to_run_the_failing_command_is_no_conflict(
    tmp_path: Path, text: str, documented: str | None
) -> None:
    rec = _documented_fallback(tmp_path, text)
    assert rec.get("documented_as") == documented and rec["replacement"] == "pytest"
    if documented != "instruction":
        assert 'such as: "use `pytest`, not `make test`"' in rec["detail"]


def test_a_headless_session_picked_up_after_a_long_silence_is_a_new_episode(
    tmp_path: Path,
) -> None:
    from agent_friction.adapters import parse_transcript
    from agent_friction.engine import episode_starts

    headless = Session("h").user("run the nightly job").bash("make build", "ok")
    headless.tick(17 * 3600).bash("make build", "ok")  # the container was restarted
    (stream,) = parse_transcript(headless.save(tmp_path / "h")).streams
    assert len(episode_starts([stream])["h"]) == 1
    # a command that itself ran for hours is not a silence
    slow = Session("l").user("train it").bash("make train", "done", took=5 * 3600)
    slow.bash("make eval", "ok")
    (stream,) = parse_transcript(slow.save(tmp_path / "l")).streams
    assert episode_starts([stream]) == {}


KEY_ERROR = "Traceback (most recent call last):\n  File \"<stdin>\", line 2\nKeyError: 'k'"


@pytest.mark.parametrize(
    ("red", "output", "between", "green"),
    [
        # the real shape: a heredoc with its own bug, the project's tool, another heredoc
        (
            "python3 - <<E\nimport json\nprint(json.load(open('a'))['k'])\nE",
            KEY_ERROR,
            "mytool show src/a.py",
            "python3 - <<E\nimport json\nprint(json.load(open('a')).get('k'))\nE",
        ),
        (
            "python3 -c \"print(open('nope.txt').read())\"",
            "FileNotFoundError: [Errno 2] No such file or directory: 'nope.txt'",
            "mytool list",
            "python3 -c \"print(open('there.txt').read())\"",
        ),
        (
            'psql -c "select nme from users"',
            'ERROR:  column "nme" does not exist',
            "mytool schema users",
            'psql -c "select name from users"',
        ),
        (
            "python scripts/analyze.py --month 2026-13",
            "Traceback (most recent call last):\nValueError: month must be in 1..12",
            "mytool dump | head -3",
            "python scripts/analyze.py --month 2026-12",
        ),
        (
            "python scripts/analyze.py --month 2026-13",
            "Traceback (most recent call last):\nValueError: month must be in 1..12",
            "mytool dump",
            "python scripts/analyze.py --month 2026-12",
        ),
    ],
)
def test_a_changed_invocation_that_passes_credits_nothing_that_ran_in_between(
    tmp_path: Path, red: str, output: str, between: str, green: str
) -> None:
    def build(s: Session) -> None:
        s.bash(red, output, exit_code=1).bash(between, "x").bash(green, "ok")

    sessions = [stashed_session(i, build) for i in range(3)]
    out = scan(tmp_path, sessions)
    assert out["commands"] == [] and out["totals"]["friction_events"] == 0
    assert out["totals"]["dropped_red_runs"] == 3


def test_the_same_line_passing_after_another_command_still_names_that_command(
    tmp_path: Path,
) -> None:
    red = "E   fixture data missing\n1 failed"

    def build(s: Session) -> None:
        s.bash("pytest", red, exit_code=1).bash("mytool seed", "seeded").bash("pytest", "9 passed")

    (row,) = _commands(tmp_path / "same", build, count=3)
    (rec,) = row["recommendations"]
    assert rec["id"] == "document-the-setup-step" and rec["setup"] == "mytool seed"

    # a known setup step keeps the credit when only switches changed
    def flags(s: Session) -> None:
        s.bash("pytest tests/test_a.py -x", PG_DOWN, exit_code=1)
        s.bash("docker compose up -d db", "Started").bash("pytest tests/test_a.py", "9 passed")

    (row,) = _commands(tmp_path / "flags", flags, count=3)
    assert row["recommendations"][0]["id"] == "document-the-setup-step"

    # ... but something run only to be read through a pager or filter is a look
    def looked(s: Session) -> None:
        s.bash("pytest", red, exit_code=1).bash("mytool seed | head -3", "x")
        s.bash("pytest", "9 passed")

    rows = _commands(tmp_path / "looked", looked, count=3)
    assert "document-the-setup-step" not in [r["id"] for c in rows for r in c["recommendations"]]


@pytest.mark.parametrize(
    ("text", "documented"),
    [
        ("# mytool", None),
        ("# mytool\n\nThis repo is mytool, a widget library.", None),
        ("## Using mytool", "mention"),
        ("mytool is our widget CLI.", None),  # the repository's own name mentions nothing
        ("Run `mytool` before the tests.", "instruction"),
        ("Before testing:\n\n```\nmytool\n```", "instruction"),
    ],
)
def test_a_title_or_a_sentence_does_not_document_a_setup_step(
    tmp_path: Path, text: str, documented: str | None
) -> None:
    repo = tmp_path / "mytool"
    repo.mkdir()
    (repo / "AGENTS.md").write_text(text + "\n")
    sessions = []
    for i in range(3):
        s = Session(f"s{i}", cwd=str(repo)).user("go")
        s.bash("pytest", "E   fixture data missing\n1 failed", exit_code=1)
        sessions.append(s.bash("mytool --seed", "seeded").bash("pytest", "9 passed"))
    (row,) = scan(tmp_path / "t", sessions, repo)["commands"]
    (rec,) = row["recommendations"]
    assert rec["id"] == "document-the-setup-step" and rec["setup"] == "mytool"
    assert rec.get("documented_as") == documented
    assert ("is documented, yet agents start without it" in rec["title"]) == (
        documented == "instruction"
    )
