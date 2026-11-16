"""The docs promise that every number is reproducible: keep them in step with the code."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from agent_friction import attribution, engine, gitsignals, recommend, redact, scoring, signals
from agent_friction.adapters import generic
from tests.builders import detect

DOCS = Path(__file__).parent.parent / "docs"


def read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_signal_catalog_table_matches_the_code() -> None:
    text = read("signals.md")
    rows = dict(re.findall(r"^\| `([a-z-]+)` \| ([\d.]+) \|", text, re.M))
    assert rows == {s.id: f"{s.severity:.1f}" for s in signals.CATALOG}
    assert list(rows) == [s.id for s in signals.CATALOG]  # same order: most severe first
    for definition in signals.CATALOG:
        assert f"## `{definition.id}` — {definition.title}" in text


def test_documented_thresholds_match_the_code() -> None:
    catalog = read("signals.md")
    method = read("methodology.md")
    assert f"fails {signals.RETRY_LOOP_MIN_FAILURES} or more times in a row" in catalog
    assert f"{signals.EDIT_THRASH_MIN_EDITS} or more successful edits" in catalog
    assert f"read {signals.READ_LOOP_MIN_READS} or more times" in catalog
    assert f"first {signals.CORRECTION_HEAD_CHARS} characters" in catalog
    assert f"weight {signals.ORDINARY_FAILURE_WEIGHT}" in catalog
    assert f"correction (confidence {signals.CORRECTION_PROBABLE})" in catalog
    worth = signals.SEVERITY[signals.PROBABLE_CORRECTION]
    assert f"a probable correction weighs {worth:g}" in catalog
    assert f"the score is capped at **{scoring.WEAK_ONLY_SCORE_CAP:g}**" in method
    for weak in scoring.WEAK_ALONE_SIGNALS:
        assert f"`{weak}`" in method.split("**Weak evidence is capped.**")[1][:200]
    assert f"({gitsignals.DEFAULT_REWORK_WINDOW_DAYS}\ndays by default)" in catalog
    assert f"exp(-mean points / {scoring.SEVERITY_SCALE:g})" in method
    bits = math.log2(1 + scoring.RECURRENCE_FULL_SESSIONS)
    assert f"log2(1 + friction sessions) / {bits:g})" in method
    assert f"reaches 1 at {scoring.RECURRENCE_FULL_SESSIONS} friction sessions" in method
    floor, prior = scoring.CONSISTENCY_FLOOR, scoring.EXPOSURE_PRIOR_SESSIONS
    assert (
        f"consistency = {floor:g} + {1 - floor:g} x friction sessions / "
        f"(exposure sessions + {prior})"
    ) in method
    assert f"only the newest {gitsignals.MAX_PATCH_COMMITS} candidate commits" in catalog
    rework, miss = signals.SEVERITY[signals.HUMAN_REWORK], signals.SEVERITY[signals.EDIT_MISS]
    assert rework < miss and f"weighs less than an edit miss ({rework:g})" in catalog
    stale = signals.CORRECTION_STALE_SECONDS / 60
    assert f"typed more than {stale:g} minutes after the agent's last" in catalog
    assert f"first {attribution.PUBLIC_HASH_DIGITS} hex digits of its SHA-256" in method
    minutes = engine.COMMIT_SESSION_SLACK.total_seconds() / 60
    assert f"{minutes:g} minutes after its last" in catalog
    assert f"first {engine.JSONL_PROBE_LINES} lines is a JSON object" in read("schema.md")
    assert f"capped at **{signals.TIME_CAP_SECONDS / 60:g} minutes**" in method
    assert f"next {signals.RECOVERY_LOOKAHEAD} shell calls" in method
    assert f"has {recommend.LARGE_FILE_LINES}+ lines" in method
    assert f"default {redact.DEFAULT_EXCERPT_CHARS} characters" in method
    assert f"strings of {redact.ENTROPY_MIN_LENGTH}+ characters" in method
    assert f"hex strings of {redact.HEX_MIN_LENGTH}+ characters" in method
    assert f"strings of exactly {redact.GIT_SHA_LENGTH} lower-case" in method
    assert f"command line fails {signals.POLL_MIN_FAILURES} or more" in catalog
    assert f"{scoring.HIGH_MIN_SESSIONS} or more distinct sessions **and**" in method


def test_documented_recommendation_ids_match_the_code() -> None:
    section = read("methodology.md").split("## Recommendations")[1].split("\n## ")[0]
    documented = re.findall(r"^\| `([a-z-]+)` \| ", section, re.M)
    assert documented == list(recommend.RULE_IDS)


def test_generic_format_example_in_the_docs_parses(tmp_path: Path) -> None:
    block = re.search(r"```jsonl\n(.*?)```", read("schema.md"), re.S)
    assert block is not None
    path = tmp_path / "example.jsonl"
    path.write_text(block.group(1), encoding="utf-8")
    assert all(json.loads(line)["session"] == "s1" for line in block.group(1).splitlines())
    parsed = generic.parse_file(path)
    assert parsed.skipped_lines == 0 and len(parsed.streams[0].events) == 6
    found = detect(path, tmp_path, repo="/work/shop")
    # the only edit failed, so nothing corroborates the complaint: a probable correction
    assert [f.signal for f in found] == ["edit-miss", "command-failure", "probable-correction"]


def test_score_table_in_the_methodology_is_computed_by_the_code() -> None:
    from datetime import datetime, timezone

    from agent_friction.model import Finding

    def score(friction: int, exposure: int) -> float:
        when = datetime(2026, 11, 2, tzinfo=timezone.utc)
        items = [
            (Finding(signals.EDIT_MISS, f"s{i}", "x.jsonl", 1, when, ""), 1.0)
            for i in range(friction)
        ]
        return scoring.score_items(items, exposure).score

    rows = re.findall(
        r"^  \| (\d+) of (\d+) sessions? \| ([\d.]+) \|$", read("methodology.md"), re.M
    )
    assert len(rows) == 4
    for friction, exposure, documented in rows:
        assert score(int(friction), int(exposure)) == float(documented)


def test_documented_command_examples_match_the_code() -> None:
    from agent_friction.attribution import normalize_command

    method = re.sub(r"\s+", " ", read("methodology.md"))
    examples = re.findall(r"`([^`]+)` -> `([^`]+)`", method)
    assert len(examples) >= 8
    for command, key in examples:
        if "..." not in command:
            assert normalize_command(command) == key, command


def test_documented_placeholders_are_computed_by_the_code() -> None:
    method = re.sub(r"\s+", " ", read("methodology.md"))
    for name, shown in (
        ("python deploy_acme_customer.py", "python <script:93d162>"),
        ("make deploy-customer-acme", "make <target:37bd73>"),
        ("mytool", "<tool:373279>"),
    ):
        assert attribution.public_key(name) == shown and shown in method


def _section(text: str, signal_id: str) -> str:
    """One signal's section of signals.md as running text (quote marks and wrapping removed)."""
    body = text.split(f"\n## `{signal_id}` — ", 1)[1].split("\n## ", 1)[0]
    return " ".join(re.sub(r"^> ?", "", body, flags=re.M).split())


def test_every_catalog_description_is_the_summary_of_its_documented_rule() -> None:
    # The catalog text travels with every report (`signals[].description`); the doc holds
    # the full rule. The description is quoted in the doc, so neither can drift alone.
    text = read("signals.md")
    for definition in signals.CATALOG:
        assert f"**In short:** {definition.description}" in _section(text, definition.id)


def test_catalog_descriptions_state_the_rules_that_decide() -> None:
    described = {s.id: s.description for s in signals.CATALOG}
    correction = described[signals.USER_CORRECTION]
    assert "corroborated by behaviour" in correction
    assert "two weaker cues together, is only a probable correction" in correction
    assert "or with two weaker cues" not in correction
    assert "not corroborated" in described[signals.PROBABLE_CORRECTION]
    assert "could have changed files" in described[signals.RETRY_LOOP]
    assert "something also went wrong" in described[signals.EDIT_THRASH]
    assert "edit and a green run" in described[signals.COMMAND_FAILURE]
    assert "after a setup step, with no file edited, is kept" in described[signals.COMMAND_FAILURE]
    text = read("signals.md")
    # ... and the doc says the same in its own words
    assert "only if behaviour corroborates it" in _section(text, signals.USER_CORRECTION)
    assert "may have changed something" in _section(text, signals.RETRY_LOOP)


def test_documented_rules_for_recurring_red_runs_match_the_code() -> None:
    from agent_friction import engine

    runs, sessions = engine.RECURRING_RED_MIN_RUNS, engine.RECURRING_RED_MIN_SESSIONS
    for name in ("signals.md", "methodology.md"):
        text = " ".join(read(name).split())
        assert f"failed {runs} or more times in a session" in text.replace("fails", "failed")
        assert f"in {sessions} or more sessions" in text
    assert "`dropped_red_runs`" in read("schema.md") and "`dropped_minutes`" in read("schema.md")
