from __future__ import annotations

from pathlib import Path

import pytest

from agent_friction import signals
from agent_friction.signals import correction_confidence
from tests import builders
from tests.builders import Session, by_signal, detect

A = "/repo/src/a.py"
B = "/repo/src/b.py"


def keys(finding: object) -> list[tuple[str, str]]:
    return [(t.kind, t.key) for t in finding.targets]  # type: ignore[attr-defined]


def test_catalog_is_consistent() -> None:
    ids = [s.id for s in signals.CATALOG]
    assert len(ids) == len(set(ids)) == 13
    for definition in signals.CATALOG:
        assert definition.title and definition.description and definition.why
        assert definition.attribution and definition.severity > 0


# ---- edit-miss / stale-edit / path-miss ---------------------------------------------------


def test_edit_miss_variants_and_time_until_recovery(tmp_path: Path) -> None:
    s = Session().user("go")
    s.read(A)
    s.edit(A, error=builders.EDIT_NOT_FOUND)
    s.tick(30).edit(A, error=builders.EDIT_AMBIGUOUS, n=4)
    s.tick(20).edit(A)
    found = detect(s, tmp_path)
    misses = by_signal(found, signals.EDIT_MISS)
    assert [f.detail["variant"] for f in misses] == ["not-found", "ambiguous"]
    assert misses[1].detail["matches"] == "4" and "4 places" in misses[1].summary
    assert all(keys(f) == [("file", "src/a.py")] for f in misses)
    assert "String to replace not found" in misses[0].excerpt
    # Both misses stay open until the edit finally lands (its result, 64s after the first call).
    first, second = (f.interval for f in misses)
    assert first is not None and second is not None
    assert first[1] - first[0] == pytest.approx(64)
    assert second[1] - second[0] == pytest.approx(28)
    assert len(found) == 2


def test_unrecovered_miss_costs_one_round_trip(tmp_path: Path) -> None:
    s = Session().user("go")
    s.edit(A, error=builders.EDIT_NOT_FOUND)  # result after 2s
    s.tick(30).read(B)  # next event: 30s + 4s think later
    s.tick(500).user("ok, new topic")
    s.edit(A)  # a success in a later turn does not reach back
    (miss,) = detect(s, tmp_path)
    assert miss.interval is not None
    assert miss.interval[1] - miss.interval[0] == pytest.approx(36)


def test_successful_edits_and_outside_repo_edits_are_not_misses(tmp_path: Path) -> None:
    s = Session().user("go")
    s.edit(A).edit(B)
    s.edit("/elsewhere/x.py", error=builders.EDIT_NOT_FOUND)
    s.write("/tmp/scratch.txt", error=builders.NOT_READ)
    assert detect(s, tmp_path) == []


def test_time_estimate_is_capped(tmp_path: Path) -> None:
    s = Session().user("go")
    s.edit(A, error=builders.EDIT_NOT_FOUND)
    s.tick(3600).edit(A)
    (miss,) = detect(s, tmp_path)
    assert miss.interval is not None
    assert miss.interval[1] - miss.interval[0] == signals.TIME_CAP_SECONDS


def test_stale_edit_variants(tmp_path: Path) -> None:
    s = Session().user("go")
    s.write(A, error=builders.NOT_READ)
    s.edit(A, error=builders.STALE)
    found = by_signal(detect(s, tmp_path), signals.STALE_EDIT)
    assert [f.detail["variant"] for f in found] == ["not-read", "modified"]
    assert "had not been read" in found[0].summary and "changed on disk" in found[1].summary


def test_path_miss_on_read_and_edit_but_not_other_errors(tmp_path: Path) -> None:
    s = Session().user("go")
    s.read("/repo/src/nope.py", missing=True)
    s.edit("/repo/src/nope.py", error=builders.MISSING)
    s.tool(
        "Read",
        {"file_path": A},
        "<tool_use_error>File is too large</tool_use_error>",
        is_error=True,
    )
    s.read("/outside/nope.py", missing=True)
    found = detect(s, tmp_path)
    assert [f.signal for f in found] == [signals.PATH_MISS] * 2
    assert keys(found[0]) == [("file", "src/nope.py")]


# ---- edit-thrash / edit-revert ------------------------------------------------------------


def test_edit_thrash_needs_six_edits_in_one_turn(tmp_path: Path) -> None:
    s = Session().user("go")
    for i in range(7):
        s.edit(A, f"old{i}", f"new{i}")
    (thrash,) = detect(s, tmp_path)
    assert thrash.signal == signals.EDIT_THRASH
    assert thrash.summary.startswith("7 successful edits to src/a.py")

    spread = Session().user("go")
    for i in range(5):
        spread.edit(A, f"old{i}", f"new{i}")
    spread.user("next task please")
    for i in range(5, 10):
        spread.edit(A, f"old{i}", f"new{i}")
    for i in range(5):
        spread.edit(B, f"old{i}", f"new{i}")
    for _ in range(3):  # failed edits do not count
        spread.edit(B, error=builders.EDIT_NOT_FOUND)
    assert by_signal(detect(spread, tmp_path), signals.EDIT_THRASH) == []


def test_edit_revert_detects_exact_undo_only(tmp_path: Path) -> None:
    s = Session().user("go")
    s.edit(A, "alpha", "beta")
    s.tick(60).edit(A, "gamma", "delta")
    s.tick(60).edit(A, "beta", "alpha")  # undoes the first edit
    s.edit(B, "beta", "alpha")  # same strings, different file: not a revert
    s.edit(A, "beta", "alpha2")  # not exact
    s.edit(A, "same", "same")
    (revert,) = detect(s, tmp_path)
    assert revert.signal == signals.EDIT_REVERT and keys(revert) == [("file", "src/a.py")]
    assert revert.interval is not None
    assert revert.interval[1] - revert.interval[0] == pytest.approx(132)


def test_reapplying_after_a_revert_is_not_a_second_revert(tmp_path: Path) -> None:
    s = Session().user("go")
    s.edit(A, "a", "b").edit(A, "b", "a").edit(A, "a", "b")
    found = by_signal(detect(s, tmp_path), signals.EDIT_REVERT)
    assert len(found) == 1  # a->b->a is one undo; going forward again is not another


def test_write_restoring_an_earlier_version_is_a_revert(tmp_path: Path) -> None:
    s = Session().user("go")
    s.write(A, "v1").write(A, "v2").write(A, "v1")
    s.write(B, "v1").write(B, "v1")  # identical rewrite is not a revert
    (revert,) = detect(s, tmp_path)
    assert revert.signal == signals.EDIT_REVERT and keys(revert) == [("file", "src/a.py")]


# ---- read-loop ----------------------------------------------------------------------------


def test_read_loop_same_range_without_edit(tmp_path: Path) -> None:
    s = Session().user("go")
    s.read(A).read(A).read(A).read(A)
    (loop,) = detect(s, tmp_path)
    assert loop.signal == signals.READ_LOOP
    assert loop.summary == "src/a.py was read 4 times with no edit in between"
    assert loop.interval is None


def test_read_loop_negative_cases(tmp_path: Path) -> None:
    paging = Session().user("go")
    for offset in (0, 200, 400, 600):
        paging.read(A, offset=offset, limit=200)
    assert detect(paging, tmp_path) == []

    edited = Session().user("go")
    edited.read(A).read(A).edit(A).read(A).read(A)
    assert detect(edited, tmp_path) == []

    failed = Session().user("go")
    failed.read(A).read(A).edit(A, error=builders.EDIT_NOT_FOUND).read(A)
    assert [f.signal for f in detect(failed, tmp_path)] == [signals.EDIT_MISS, signals.READ_LOOP]


# ---- search-miss --------------------------------------------------------------------------


def test_search_miss_attribution(tmp_path: Path) -> None:
    s = Session().user("go")
    s.grep("Needle", "/repo/src")  # directory
    s.grep("Needle", A)  # file
    s.grep("Needle")  # no path: repo root
    s.glob("src/api/**/*.proto")  # literal prefix narrows the scope
    s.glob("**/*.proto", "/repo/pkg")
    s.glob("/repo/docs/*.rst")
    s.grep("Needle", "/repo/src", found=True)
    s.glob("*.py", found=True)
    s.grep("Needle", "/var/log")  # outside the repo
    found = detect(s, tmp_path)
    assert all(f.signal == signals.SEARCH_MISS for f in found)
    assert [keys(f)[0] for f in found] == [
        ("dir", "src"),
        ("file", "src/a.py"),
        ("dir", "."),
        ("dir", "src/api"),
        ("dir", "pkg"),
        ("dir", "docs"),
    ]
    assert found[0].excerpt == "pattern: Needle" and "Needle" not in found[0].summary
    assert found[0].detail == {"pattern": "Needle", "scope": "src"}


# ---- command-failure / retry-loop ---------------------------------------------------------


def test_command_failure_classes_and_weights(tmp_path: Path) -> None:
    s = Session().user("go")
    s.read(A)
    s.bash("pytest src/a.py -q", "E   ModuleNotFoundError: No module named 'x'", exit_code=4)
    s.bash("FOO=1 pytest src/a.py", "1 failed, 3 passed", exit_code=1)
    s.bash("frobnicate --now", "bash: frobnicate: command not found", exit_code=127)
    s.bash("pytest -q", "4 passed")
    found = detect(s, tmp_path)
    assert [f.signal for f in found] == [signals.COMMAND_FAILURE] * 3
    setup, ordinary, missing = found
    assert (setup.weight, setup.detail["variant"]) == (1.0, "invocation")
    assert keys(setup) == [("command", "pytest")]  # setup errors do not blame the file
    assert (ordinary.weight, ordinary.detail["variant"]) == (
        signals.ORDINARY_FAILURE_WEIGHT,
        "ordinary",
    )
    assert keys(ordinary) == [("command", "pytest"), ("file", "src/a.py")]
    assert keys(missing) == [("command", "frobnicate")] and missing.weight == 1.0
    assert setup.excerpt.startswith("$ pytest src/a.py -q\nExit code 4")
    assert "exit 4; setup/invocation error" in setup.summary


def test_benign_and_out_of_repo_failures_are_ignored(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("grep -rn foo src", "", exit_code=1)
    s.bash("git status --porcelain | grep foo", "", exit_code=1)
    s.bash("ls /nope", "ls: cannot access '/nope': No such file or directory", exit_code=2)
    s.bash("test -f x", "", exit_code=1)
    s.tool("Bash", {"command": "sleep 99"}, "partial", tool_use_result={"interrupted": True})
    assert detect(s, tmp_path) == []

    elsewhere = Session(cwd="/somewhere/else").user("go")
    elsewhere.bash("make test", "boom", exit_code=2)
    assert detect(elsewhere, tmp_path) == []


def test_pipeline_real_failure_still_counts(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash(
        "make build 2>&1 | grep -v warning", "make: *** No rule to make target 'build'", exit_code=2
    )
    (failure,) = detect(s, tmp_path)
    assert keys(failure) == [("command", "make build")]


def test_retry_loop_three_consecutive_failures(tmp_path: Path) -> None:
    s = Session().user("go")
    for args in ("", " -x", " -x -q", " --lf"):
        s.bash(f"npm test{args}", "Error: boom", exit_code=1)
    found = detect(s, tmp_path)
    (loop,) = by_signal(found, signals.RETRY_LOOP)
    assert loop.summary == "`npm test` failed 4 times in a row" and loop.detail["count"] == "4"
    assert keys(loop) == [("command", "npm test")] and loop.interval is None
    assert len(by_signal(found, signals.COMMAND_FAILURE)) == 4


def test_retry_loop_is_reset_by_success_and_is_per_command(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("npm test", "boom", exit_code=1).bash("npm test", "boom", exit_code=1)
    s.bash("npm test", "ok")
    s.bash("npm test", "boom", exit_code=1).bash("npm run lint", "boom", exit_code=1)
    s.bash("npm test", "boom", exit_code=1)
    assert by_signal(detect(s, tmp_path), signals.RETRY_LOOP) == []


def test_recovery_command_is_recorded(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("pytest", "No module named 'x'", exit_code=4)
    s.bash("cat Makefile", "test: ...")  # inspection: not a fix
    s.bash("make test", "ok")
    s.bash("cargo test", "boom", exit_code=101)
    s.bash("cargo test", "ok")  # the same command succeeding is not "another command"
    s.bash("go vet", "boom", exit_code=1)
    s.user("thanks, now something else")
    s.bash("make fmt", "ok")  # a new turn never counts as the fix
    first, second, third = detect(s, tmp_path)
    assert first.detail["recovered_by"] == "make test"
    assert "recovered_by" not in second.detail and "recovered_by" not in third.detail


def test_failure_time_runs_until_the_command_that_recovers(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("pytest", "No module named 'x'", exit_code=4)  # call .. +5s result
    s.tick(10).read(A)  # looking for the right command
    s.tick(10).bash("make test", "ok")
    s.bash("npm test", "1 failed", exit_code=1)  # an ordinary failure: no time is claimed
    s.bash("frobnicate", "command not found", exit_code=127)  # never recovered: one round trip
    setup, ordinary, lost = detect(s, tmp_path)
    assert setup.interval is not None and lost.interval is not None
    # 5 (run) + 10 + 4 (think) + 2 (read) + 10 + 4 (think) + 5 (run)
    assert setup.interval[1] - setup.interval[0] == pytest.approx(40)
    assert ordinary.interval is None
    assert lost.interval[1] - lost.interval[0] == pytest.approx(5)


# ---- tool-rejected / user-interrupt -------------------------------------------------------


def test_tool_rejected_targets_and_reason(tmp_path: Path) -> None:
    s = Session().user("go")
    s.reject("Edit", {"file_path": A, "old_string": "a", "new_string": "b"})
    s.reject("Bash", {"command": "rm -rf build"}, "never delete build, use make clean")
    s.reject("mcp__db__query", {"sql": "drop table"}, "no")
    s.reject("Write", {"file_path": "/etc/hosts", "content": "x"})
    found = detect(s, tmp_path)
    assert [f.signal for f in found] == [signals.TOOL_REJECTED] * 3
    assert [keys(f) for f in found] == [[("file", "src/a.py")], [("command", "rm")], []]
    assert found[1].detail["reason"] == "never delete build, use make clean"
    assert "user said: never delete build" in found[1].excerpt
    assert "never delete" not in found[1].summary


def test_interrupt_attributes_to_last_tool_target(tmp_path: Path) -> None:
    s = Session().user("go")
    s.read(B).edit(A)
    s.tick(30).interrupt()
    (interrupt,) = detect(s, tmp_path)
    assert interrupt.signal == signals.USER_INTERRUPT and keys(interrupt) == [("file", "src/a.py")]
    assert interrupt.interval is not None and interrupt.interval[1] > interrupt.interval[0]

    shell = Session().user("go")
    shell.bash("npm run build", "…").interrupt()
    assert keys(detect(shell, tmp_path)[0]) == [("command", "npm run build")]


def test_interrupt_negative_cases(tmp_path: Path) -> None:
    idle = Session().user("go").interrupt()  # nothing was running
    assert detect(idle, tmp_path) == []

    declined = Session().user("go")
    declined.reject("Edit", {"file_path": A, "old_string": "a", "new_string": "b"})
    found = detect(declined, tmp_path)  # reject() also writes the interrupt marker
    assert [f.signal for f in found] == [signals.TOOL_REJECTED]

    outside = Session().user("go")
    outside.edit("/elsewhere/x.py").interrupt()
    assert detect(outside, tmp_path) == []


# ---- user-correction ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("No, that's the wrong file", 1.0),
        ("no. use the other one", 1.0),
        ("Nope, try again", 1.0),
        ("that's not what I asked for", 1.0),
        ("This is wrong.", 1.0),
        ("you broke the build", 1.0),
        ("You didn\u2019t run the tests", 1.0),
        ("please revert that change", 1.0),
        ("undo the last edit", 1.0),
        ("it's still failing with the same error", 1.0),
        ("I said use pnpm", 1.0),
        ("I told you not to touch the lockfile", 1.0),
        ("why did you delete the fixture?", 1.0),
        ("Don't use requests here, use httpx instead", 0.75),
        ("actually, never mind the cache; stop there", 0.75),
        ("Actually let's also add a test", 0.5),
        ("don't forget the changelog", 0.5),
        ("use the factory instead", 0.5),
        ("the result is wrong for negative numbers", 0.5),
        ("it should be sorted by date", 0.5),
        ("Great, thanks! Now add pagination to the list endpoint.", 0.0),
        ("no problem, carry on", 0.0),
        ("Now do the same for the admin page", 0.0),
        ("Looks good. Commit it.", 0.0),
        ("Can you also update the docs?", 0.0),
        ("not bad! next: the README", 0.0),
        ("", 0.0),
    ],
)
def test_correction_confidence(text: str, expected: float) -> None:
    assert correction_confidence(text)[0] == expected


def test_correction_phrases_are_only_read_at_the_start() -> None:
    pasted_log = "Here is the CI log:\n" + "x" * 400 + "\nerror: you broke it, revert that"
    assert correction_confidence(pasted_log)[0] == 0.0


def test_correction_splits_over_edited_files(tmp_path: Path) -> None:
    s = Session().user("add the feature")
    s.read("/repo/src/c.py").edit(A).edit(B).edit(A)
    s.tick(100).user("No, that is not how we do it here")
    (fix,) = detect(s, tmp_path)
    assert fix.signal == signals.USER_CORRECTION and fix.weight == 1.0
    assert [(t.key, t.share) for t in fix.targets] == [("src/a.py", 0.5), ("src/b.py", 0.5)]
    assert fix.excerpt == "No, that is not how we do it here"
    assert "not how we do it" not in fix.summary and 'matched "no, …"' in fix.summary
    assert fix.interval is not None
    # the interval ends at the agent's last activity, not at the (slow) human reply
    assert fix.interval[1] - fix.interval[0] < 60


def test_correction_prefers_files_named_in_the_message(tmp_path: Path) -> None:
    s = Session().user("add the feature")
    s.read("/repo/src/c.py").edit(A).edit(B)
    s.user("That's wrong: c.py already has a helper, and don't touch src/b.py or docs/x.md")
    (fix,) = detect(s, tmp_path, known=("docs/x.md",))
    # docs/x.md is named but the agent never touched it, so it is not blamed
    assert [(t.key, t.share) for t in fix.targets] == [("src/c.py", 1.0), ("src/b.py", 1.0)]


def test_correction_negative_cases(tmp_path: Path) -> None:
    first = Session().user("No, wait, start with the parser").edit(A)
    assert detect(first, tmp_path) == []  # the first prompt cannot correct anything

    idle = Session().user("add it").user("no, add it to the other module")
    assert detect(idle, tmp_path) == []  # no agent work in between

    friendly = Session().user("add it")
    friendly.edit(A).user("Great. Now add tests for it.")
    assert detect(friendly, tmp_path) == []

    slash = Session().user("add it")
    slash.edit(A).user("<command-name>/compact</command-name>").user("no, the other module")
    assert detect(slash, tmp_path) == []  # the command started a new turn with no work

    sub = Session(sidechain=True).user("task")
    sub.edit(A).user("no, that is wrong")
    assert detect(sub, tmp_path) == []  # subagent "user" turns are not human


def test_correction_without_edits_is_unattributed(tmp_path: Path) -> None:
    s = Session().user("investigate")
    s.read(A).bash("npm test", "ok")
    s.user("no, I asked about the API tests")
    (fix,) = detect(s, tmp_path)
    assert fix.targets == [] and "not attributed" in fix.summary


def test_generic_stream_goes_through_the_same_detectors(tmp_path: Path) -> None:
    from tests.fixture_story import other_agent

    path = tmp_path / "g.jsonl"
    import json

    path.write_text("".join(json.dumps(e) + "\n" for e in other_agent()))
    found = detect(path, tmp_path, repo="/home/dev/shopfront")
    assert [f.signal for f in found] == [
        signals.EDIT_MISS,
        signals.COMMAND_FAILURE,
        signals.USER_CORRECTION,
    ]
    assert found[1].detail["recovered_by"] == "make test"
    assert keys(found[2]) == [("file", "src/shopfront/api/schema_gen.py")]
