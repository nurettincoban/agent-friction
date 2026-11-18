"""What a blind dataset, written by someone who never saw this code, showed was missing:
corrections in everyday words, a command that only works with a variable set, "done"
said too early, a refusal explained in the next message, and a correction of a command."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_friction import signals
from agent_friction.adapters import common
from agent_friction.attribution import is_shell_edit, parse_command
from agent_friction.engine import AnalyzeOptions, analyze
from tests.builders import Session, detect

A = "/repo/src/a.py"
RED = "FAILED tests/test_t.py::test_day - assert 1 == 2\n1 failed, 4 passed in 0.2s"


@pytest.mark.parametrize(
    "text",
    [
        "Not that file. It's generated from schema.avsc and gets overwritten.",
        "That's the old package. Nothing imports it any more.",
        "Please revert the timing bound change. It hides the flake.",
    ],
)
def test_everyday_corrections(text: str) -> None:
    confidence, labels = signals.correction_confidence(text)
    assert confidence == 1.0, (text, labels)


@pytest.mark.parametrize(
    "text",
    [
        "Thanks. One thing though: that hands a float to level_mm. There's already "
        "`units.mm_from_m` for this.",
        "Thanks. Please don't kick off the slow suite for every little change, though.",
        "No skipping, and no bumping the number either.",
        "Hang on: threshold in mm please, as an int.",
    ],
)
def test_everyday_instructions(text: str) -> None:
    assert signals.instruction_cues(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Thanks, that's fine. Now add the export.",
        "Not that I know of, go ahead.",
        "Great work! Next task: add the CSV export.",
        "I said use tabs in my last project but here spaces are fine",
    ],
)
def test_still_not_corrections(text: str) -> None:
    assert signals.correction_confidence(text) == (0.0, [])


def test_a_stash_and_pop_on_one_line_changes_nothing() -> None:
    line = "git stash && pytest -q tests/test_t.py; git stash pop"
    assert not is_shell_edit(parse_command(line), line)
    assert is_shell_edit(parse_command("git stash"), "git stash")


def _env_story(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    for name in ("s1", "s2"):
        s = Session(name, cwd=str(repo)).user("add the day filter")
        s.edit(str(repo / "src" / "a.py"))
        s.bash("pytest -q", RED, exit_code=1)
        s.bash("TZ=UTC pytest -q", "5 passed in 0.2s")
        s.say("The day filter is in.")
        s.save(tmp_path / "t")
    return analyze(AnalyzeOptions(repo=repo, transcripts=[tmp_path / "t"], include_git=False))


def test_a_command_that_passes_only_with_a_variable_set(tmp_path: Path) -> None:
    result = _env_story(tmp_path)
    (row,) = [c for c in result["commands"] if c["path"] == "pytest"]  # type: ignore[index]
    (rec,) = row["recommendations"]
    assert rec["id"] == "use-the-working-command" and rec.get("evidence") != "thin"
    assert "`TZ=UTC pytest`" in rec["agents_md"]


def test_once_is_not_enough_for_the_variable(tmp_path: Path) -> None:
    s = Session().user("add the day filter").edit(A)
    s.bash("pytest -q", RED, exit_code=1).bash("TZ=UTC pytest -q", "5 passed")
    (red,) = detect(s, tmp_path)
    assert red.detail["green_with"] == "TZ=UTC pytest"  # the engine decides on recurrence


@pytest.mark.parametrize(
    ("text", "claimed"),
    [
        ("Done:\n\n- the index is added, and it's applied.\n- the test is updated.", True),
        ("Switched to Parquet. It writes the columns. Done.", True),
        ("Done. I couldn't run the migration, though.", False),
        ("I looked at the code.\n\nHere is a plan: first the parser.", False),
    ],
)
def test_done_said_at_the_start_or_the_end(text: str, claimed: bool) -> None:
    assert common.claims_done(text) is claimed


def _claimed_then_failed(name: str, repo: Path, reply: str) -> Session:
    s = Session(name, cwd=str(repo)).user("export the daily file")
    s.edit(str(repo / "src" / "a.py"))
    s.say("Switched to Parquet. Done.")
    s.user(reply)
    return s


def test_said_done_and_it_fails_is_one_item_for_the_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    _claimed_then_failed("s1", repo, "Did you try it? I get an ImportError.").save(tmp_path / "t")
    _claimed_then_failed("s2", repo, "Did you run anything? The staging ingest blew up.").save(
        tmp_path / "t"
    )
    result = analyze(AnalyzeOptions(repo=repo, transcripts=[tmp_path / "t"], include_git=False))
    (item,) = [r for r in result["recommendations"] if r["id"] == "say-how-to-verify"]
    assert item["rank"] == "first" and item["sessions"] == 2
    lines = [p["line"] for p in result["paste"]["agents_md"]]
    assert lines[0].startswith("before saying the work is done")
    # ... and not again under the file
    for row in result["hotspots"]:
        assert all(r["id"] != "say-how-to-verify" for r in row["recommendations"])
    # once is not a habit
    one = tmp_path / "one"
    _claimed_then_failed("s3", repo, "Did you try it? I get an ImportError.").save(one)
    single = analyze(AnalyzeOptions(repo=repo, transcripts=[one], include_git=False))
    assert not [r for r in single["recommendations"] if r["id"] == "say-how-to-verify"]


def test_a_failure_long_after_done_still_counts_but_a_new_task_does_not(tmp_path: Path) -> None:
    s = Session().user("export it").edit(A).say("Done.").tick(3600)
    s.user("I get an ImportError when I run it.")
    (fix,) = detect(s, tmp_path)
    assert fix.signal == signals.USER_CORRECTION and fix.detail["claimed_done"] == "1"
    t = Session().user("export it").edit(A).say("Done.")
    t.user("It doesn't work on Windows yet; next task: add Windows support")
    assert detect(t, tmp_path / "t") == []


def test_the_reason_for_a_refusal_can_come_in_the_next_message(tmp_path: Path) -> None:
    s = Session().user("add the field")
    s.reject("Edit", {"file_path": A, "old_string": "a", "new_string": "b"})
    s.user("Not that file. It's generated from schema.avsc.")
    rejected = next(f for f in detect(s, tmp_path) if f.signal == signals.TOOL_REJECTED)
    assert rejected.detail["reason"].startswith("Not that file.")
    assert rejected.detail["reason_from"] == "next message"
    # a new request is not a reason
    t = Session().user("add the field")
    t.reject("Edit", {"file_path": A, "old_string": "a", "new_string": "b"})
    t.user("Next task: write the docs.")
    rejected = next(f for f in detect(t, tmp_path / "t") if f.signal == signals.TOOL_REJECTED)
    assert rejected.detail["reason"] == ""


def test_a_correction_that_names_a_command_is_about_that_command(tmp_path: Path) -> None:
    s = Session().user("speed up the parser").edit(A)
    s.bash("make test-all", "Command timed out after 2m 0.0s", exit_code=1)
    s.user("Please don't kick off `test-all` for every little change. It takes six minutes.")
    fix = next(f for f in detect(s, tmp_path) if f.signal == signals.USER_CORRECTION)
    assert [(t.kind, t.key) for t in fix.targets] == [("command", "make test-all")]
    # a command that went fine is what to run instead, not the subject
    t = Session().user("speed up the parser").edit(A).bash("make test", "5 passed")
    t.user("No, `make test` was enough, and put the old loop back.").edit(A, "x", "y")
    fix = next(f for f in detect(t, tmp_path / "t") if f.signal == signals.USER_CORRECTION)
    assert all(t.kind == "file" for t in fix.targets)
