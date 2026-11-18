"""The material for a diagnosis: error signatures, what the humans said, ``-f investigate``."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_friction import model, recommend, signals
from agent_friction.engine import AnalyzeOptions, analyze
from agent_friction.model import Finding, Target
from agent_friction.report import render_investigate, render_terminal
from agent_friction.report._common import tool_said
from agent_friction.signatures import error_lines, signatures
from tests.builders import Session, detect

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)

PG_DOWN = (
    "--- FAIL: TestCreateBooking (0.00s)\n"
    "    store_test.go:22: dial tcp 127.0.0.1:5432: connect: connection refused\n"
    "FAIL\nFAIL\texample.com/app/internal/store\t0.0{n}s\nFAIL"
)


def _failure(fid: int, session: str, output: str) -> Finding:
    return Finding(
        signal=signals.COMMAND_FAILURE,
        session_id=session,
        source="x.jsonl",
        line=fid,
        ts=None,
        summary="",
        excerpt=f"$ go test ./...\nExit code 1\n{output}",
        targets=[Target(model.COMMAND, "go test")],
        fid=fid,
    )


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


# ---- error signatures ------------------------------------------------------------------


def test_the_error_line_that_recurs_is_the_signature_and_the_cause_beats_the_test_name() -> None:
    found = [_failure(i, f"s{i}", PG_DOWN.format(n=i)) for i in range(3)]
    top, *rest = signatures(found)
    assert "connection refused" in top.text
    assert (top.events, top.sessions) == (3, 3)
    # the test name recurs as often, but says what failed, not why
    assert any("TestCreateBooking" in s.text for s in rest)


def test_durations_and_numbers_are_folded_but_ports_are_kept() -> None:
    found = [
        _failure(1, "a", "Command timed out after 2m 0.0s"),
        _failure(2, "b", "Command timed out after 20.0s"),
    ]
    (said,) = signatures(found)
    assert said.events == 2 and said.text == "Command timed out after 2m 0.0s"
    # ... and the other spelling is kept, so nobody reads "2m both times"
    assert said.variants == ("Command timed out after 20.0s",)
    hotspot = {
        "events": 2,
        "signatures": [
            {"text": said.text, "events": 2, "sessions": 2, "variants": list(said.variants)}
        ],
    }
    assert "numbers in it differ" in tool_said(hotspot, {"signals": ["command-failure"]})
    other_port = [
        _failure(1, "a", "dial tcp 127.0.0.1:5432: connect: connection refused"),
        _failure(2, "b", "dial tcp 127.0.0.1:6379: connect: connection refused"),
    ]
    assert signatures(other_port) == []  # two different services: no common line


def test_a_line_seen_once_is_not_a_signature_unless_it_is_all_there_is() -> None:
    found = [_failure(1, "a", "error: one thing"), _failure(2, "b", "error: another thing")]
    assert signatures(found) == []
    (only,) = signatures([_failure(1, "a", "make: bin/lint: No such file or directory")])
    assert only.events == 1


def test_the_echo_of_the_command_and_of_an_edit_string_are_not_error_lines() -> None:
    assert error_lines("$ make test-errors\nExit code 2\nString: if err != nil {\nok  done") == []


# ---- what the humans said --------------------------------------------------------------


def _with_turns(repo: Path) -> Session:
    s = Session("conv", cwd=str(repo)).user("add a refund reason column")
    s.read(f"{repo}/migrations/0004.sql").edit(f"{repo}/migrations/0004.sql")
    s.user("thanks. one thing though, 0004 is applied everywhere, add a new file instead")
    s.reject("Bash", {"command": "make db-reset"}, reason="that wipes my local data")
    s.write(f"{repo}/migrations/0005.sql").say("Done, all tests pass.")
    s.user("great")
    return s


def test_every_later_human_turn_is_listed_with_what_the_agent_had_just_done(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _scan(tmp_path, [_with_turns(repo)])
    (session,) = out["conversation"]
    assert session["opening"] == "add a refund reason column"
    first, declined, last = session["turns"]
    assert first["kind"] == "message" and "one thing though" in first["text"]
    assert first["after"] == ["Read migrations/0004.sql → ok", "Edit migrations/0004.sql → ok"]
    assert declined["kind"] == "declined" and declined["text"] == "that wipes my local data"
    assert declined["after"] == ["Bash make db-reset → declined by the human"]
    assert last["text"] == "great" and last.get("after_done_claim") is True


def test_without_excerpts_nobody_s_words_are_kept(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _scan(tmp_path, [_with_turns(repo)], include_excerpts=False)
    assert "conversation" not in out
    assert "one thing though" not in render_investigate(out)
    assert "--no-excerpts" in render_investigate(out)


def test_a_single_prompt_session_has_its_opening_and_no_turns(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    s = Session("one", cwd=str(repo)).user("fix the flaky test").edit(f"{repo}/src/a.py")
    out = _scan(tmp_path, [s])
    (session,) = out["conversation"]
    assert session["opening"] == "fix the flaky test" and session["turns"] == []


# ---- -f investigate --------------------------------------------------------------------


def test_investigate_lays_out_sessions_turns_and_error_lines(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = [_with_turns(repo)]
    for i in range(3):
        s = Session(f"pg{i}", cwd=str(repo)).user("add an endpoint")
        s.edit(f"{repo}/src/api.go").bash("go test ./...", PG_DOWN.format(n=i), exit_code=1)
        s.bash("go test ./internal/api/", "ok")
        sessions.append(s)
    out = _scan(tmp_path, sessions)
    text = render_investigate(out)
    assert "| S01 |" in text and "| S04 |" in text
    assert "> thanks. one thing though" in text
    assert "after: Bash make db-reset → declined by the human" in text
    assert "connection refused" in text
    row = next(c for c in out["commands"] if c["path"] == "go test")
    assert row["signatures"][0]["sessions"] == 3
    # three sessions of the same failure: a pattern, listed under "What to fix first"
    assert not any(r.get("evidence") == "thin" for r in row["recommendations"])
    terminal = render_terminal(out, color=False, width=200)
    assert "The tool said: `" in terminal and "connection refused" in terminal


def test_tool_said_quotes_with_double_backticks_when_the_line_has_one() -> None:
    hotspot = {"events": 2, "signatures": [{"text": "bad `x`", "events": 2, "sessions": 2}]}
    said = tool_said(hotspot, {"signals": ["command-failure"], "detail": ""})
    assert said == "The tool said: `` bad `x` `` (2 of 2 events, 2 sessions)."
    assert tool_said(hotspot, {"signals": ["user-correction"], "detail": ""}) == ""


# ---- a server is known by what it says -------------------------------------------------


def test_a_command_that_says_it_is_listening_and_times_out_is_a_server(tmp_path: Path) -> None:
    out = "Command timed out after 2m 0.0s\ntime=... level=INFO msg=listening addr=:8080"
    s = Session().user("check it")
    s.tool("Bash", {"command": "go run ./cmd/api"}, out, is_error=True)
    (failure,) = detect(s, tmp_path)
    assert failure.detail.get("server") == "1" and "never exits" in failure.summary
    t = Session().user("check it")
    t.tool("Bash", {"command": "go test ./internal/server/"}, out, is_error=True)
    (failure,) = detect(t, tmp_path)
    assert "server" not in failure.detail  # a test that hangs is not a server to background


def test_three_sessions_of_one_failure_are_not_thin_but_three_interruptions_are() -> None:
    failures = [_failure(i, f"s{i}", PG_DOWN.format(n=i)) for i in range(3)]
    for f in failures:
        f.detail.update(variant="environment", environment="service", service="PostgreSQL")
    recs = recommend.recommend(model.COMMAND, "go test", failures)
    assert recs and not any(recommend.is_thin(r) for r in recs)
