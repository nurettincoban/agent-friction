"""Measured token cost: usage read from the transcripts, tallied over the turns a problem
claims (the same stretches as its time lost)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent_friction import model, scoring, signals
from agent_friction.adapters import claude_code
from agent_friction.compare import summary_line
from agent_friction.engine import AnalyzeOptions, analyze
from agent_friction.model import Finding, Target, Usage
from agent_friction.report import render_html, render_investigate, render_markdown, render_terminal
from agent_friction.report._common import fmt_count, headline_tokens, tokens_phrase
from tests.builders import Session

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
START = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
USAGE = {
    "input_tokens": 10,
    "output_tokens": 200,
    "cache_creation_input_tokens": 1_000,
    "cache_read_input_tokens": 20_000,
}
PG_DOWN = "dial tcp 127.0.0.1:5432: connect: connection refused\nFAIL"


def _scan(
    tmp_path: Path, sessions: list[Session], folder: str = "t", **options: Any
) -> dict[str, Any]:
    for index, session in enumerate(sessions):
        session.save(tmp_path / folder, f"s{index}")
    return analyze(
        AnalyzeOptions(
            repo=tmp_path / "repo",
            transcripts=[tmp_path / folder],
            include_git=False,
            now=NOW,
            **options,
        )
    )


def _failing(repo: Path, name: str, day: int, usage: dict[str, int] | None) -> Session:
    s = Session(name, cwd=str(repo), start=START + timedelta(days=day))
    s.usage = usage
    s.user("add a bookings endpoint").say("Looking at the store first.")
    s.edit(f"{repo}/store.go").bash("go test ./...", PG_DOWN, exit_code=1)
    s.say("The database is not running; starting it.")
    s.bash("docker compose up -d db", "started").bash("go test ./...", "ok")
    return s


# ---- reading usage -----------------------------------------------------------------------


def _line(ident: str, ts: str, usage: dict[str, int], sidechain: bool = False) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "uuid": f"u-{ident}-{ts}",
            "sessionId": "s",
            "isSidechain": sidechain,
            "timestamp": ts,
            "message": {
                "id": ident,
                "role": "assistant",
                "content": [{"type": "text", "text": "x"}],
                "usage": usage,
            },
        }
    )


def test_one_response_written_as_several_lines_counts_once(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    early = {**USAGE, "output_tokens": 30}
    zero = {"input_tokens": 0, "output_tokens": 0}
    path.write_text(
        "\n".join(
            [
                _line("msg_a", "2026-11-01T10:00:00Z", early),
                _line("msg_a", "2026-11-01T10:00:02Z", USAGE),  # same response, final count
                _line("msg_b", "2026-11-01T10:00:05Z", zero),  # an API error placeholder
                _line("msg_c", "2026-11-01T10:00:09Z", USAGE, sidechain=True),
            ]
        )
        + "\n"
    )
    parsed = claude_code.parse_file(path)
    main, side = parsed.streams
    assert main.usage == [
        Usage(START.timestamp(), input=10, output=200, cache_write=1_000, cache_read=20_000)
    ]
    assert len(side.usage) == 1 and side.sidechain


# ---- assigning it ------------------------------------------------------------------------


def _finding(
    stream: str, interval: tuple[float, float] | None, *extra: tuple[float, float]
) -> Finding:
    return Finding(
        signal=signals.COMMAND_FAILURE,
        session_id="s",
        source="x",
        line=1,
        ts=None,
        summary="",
        targets=[Target(model.COMMAND, "go test")],
        interval=interval,
        intervals=extra,
        stream=stream,
    )


def _index(*times: float) -> scoring.TokenIndex:
    return scoring.TokenIndex({"a": [Usage(t, input=1, output=10, cache_read=100) for t in times]})


def test_a_stretch_claims_the_responses_written_inside_it_once() -> None:
    index = _index(100, 105, 110, 200)
    # (100, 110]: the response at 100 wrote the failing call; 105 and 110 dealt with it
    one = index.claimed_by([_finding("a", (100, 110))])
    assert one == {
        "input": 2,
        "output": 20,
        "cache_write": 0,
        "cache_read": 200,
        "responses": 2,
    }
    # two findings over the same turns: counted once, as for time lost
    both = index.claimed_by([_finding("a", (100, 110)), _finding("a", (104, 106))])
    assert both is not None and both["responses"] == 2
    # another stream's responses are not this one's
    other = index.claimed_by([_finding("b", (100, 110))])
    assert other is not None and other["responses"] == 0


def test_a_failed_run_alone_claims_the_response_that_read_it() -> None:
    index = _index(100, 130, 900)
    run_only = index.claimed_by([_finding("a", (100, 120))])  # call at 100, result at 120
    assert run_only is not None and run_only["responses"] == 1  # the response at 130
    # after an idle gap the next response is not about the failure any more
    stale = index.claimed_by([_finding("a", (300, 320))])
    assert stale is not None and stale["responses"] == 0
    # and a response one stretch claimed is not claimed again by the next
    chained = index.claimed_by([_finding("a", (100, 120), (125, 129))])
    assert chained is not None and chained["responses"] == 1


def test_no_usage_in_the_transcripts_is_no_count_not_zero() -> None:
    index = scoring.TokenIndex({"a": []})
    assert index.claimed_by([_finding("a", (0, 10))]) is None
    assert scoring.TokenIndex({}).claimed_by([]) is None


def test_a_finding_that_claims_no_time_claims_no_tokens() -> None:
    claimed = _index(100, 105).claimed_by([_finding("a", None)])
    assert claimed is not None and claimed["responses"] == 0


# ---- the scan and the reports ------------------------------------------------------------


def test_the_scan_counts_the_tokens_of_the_turns_a_problem_took(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = [_failing(repo, f"s{i}", i, USAGE) for i in range(3)]
    out = _scan(tmp_path, sessions)
    totals = out["totals"]
    responses = sum(
        1 for s in sessions for line in s.lines if json.loads(line).get("type") == "assistant"
    )
    assert totals["tokens_total"]["responses"] == responses
    assert totals["tokens_total"]["cache_read"] == responses * 20_000
    (go_test,) = [c for c in out["commands"] if c["path"] == "go test"]
    lost = go_test["tokens_lost"]
    assert lost["responses"] >= 3  # at least the turn after each failure, in each session
    assert lost["cache_read"] == lost["responses"] * 20_000
    assert totals["tokens_lost"]["responses"] <= responses

    text = render_terminal(out, width=120)
    assert "tokens: " in text and "cache reads" in text
    assert "Cost so far: " in text
    assert "cache reads" in render_markdown(out)
    assert "agent-friction's assignment" in render_investigate(out)
    assert "tokens_note" in render_html(out)


def test_without_usage_no_token_count_is_shown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    out = _scan(tmp_path, [_failing(repo, f"s{i}", i, None) for i in range(3)])
    assert out["totals"]["tokens_lost"] is None and out["totals"]["tokens_total"] is None
    assert all(c["tokens_lost"] is None for c in out["commands"])
    text = render_terminal(out, width=120)
    assert "tokens" not in text and "Cost so far" not in text


def test_usage_outside_the_window_is_left_out(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    sessions = [_failing(repo, f"s{i}", i, USAGE) for i in range(3)]
    whole = _scan(tmp_path, sessions)
    since = _scan(tmp_path, sessions, "t2", since=START + timedelta(days=1))
    inside, everything = (r["totals"]["tokens_total"]["responses"] for r in (since, whole))
    assert 0 < inside < everything


def test_counts_are_short_and_cache_reads_are_named_apart() -> None:
    assert [fmt_count(n) for n in (950, 1_000, 6_240, 79_312, 999_499, 999_500, 7_536_419)] == [
        "950",
        "1k",
        "6.2k",
        "79k",
        "999k",
        "1M",
        "7.5M",
    ]
    tally = {"input": 20, "output": 215, "cache_write": 5_990, "cache_read": 79_312, "responses": 3}
    assert tokens_phrase(tally) == "6.2k tokens (+79k cache reads)"
    assert tokens_phrase({**tally, "responses": 0}) == ""
    assert tokens_phrase(None) == ""
    totals = {"tokens_lost": tally, "tokens_total": {**tally, "responses": 9}}
    assert headline_tokens(totals).startswith("6.2k (+79k cache reads) in the turns counted")


def test_compare_sets_tokens_per_session_side_by_side_only_when_both_have_them() -> None:
    tally = {"input": 0, "output": 1_000, "cache_write": 0, "cache_read": 0, "responses": 4}
    with_usage = {"totals": {"sessions": 2, "est_minutes_lost": 4, "tokens_lost": tally}}
    without = {"totals": {"sessions": 2, "est_minutes_lost": 4, "tokens_lost": None}}
    assert summary_line(with_usage, with_usage).count("500 tokens") == 2
    assert "tokens" not in summary_line(without, with_usage)
