from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_friction import scoring, signals
from agent_friction.signals import correction_confidence
from tests import builders
from tests.builders import Session, by_signal, detect

A = "/repo/src/a.py"
B = "/repo/src/b.py"


def keys(finding: object) -> list[tuple[str, str]]:
    return [(t.kind, t.key) for t in finding.targets]  # type: ignore[attr-defined]


def test_catalog_is_consistent() -> None:
    ids = [s.id for s in signals.CATALOG]
    assert len(ids) == len(set(ids)) == 17
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
    assert detect(s, tmp_path) == []  # seven edits and nothing went wrong: just writing
    s.edit(A, error=builders.EDIT_NOT_FOUND)
    miss, thrash = sorted(detect(s, tmp_path), key=lambda f: f.signal)
    assert miss.signal == signals.EDIT_MISS and thrash.signal == signals.EDIT_THRASH
    assert thrash.summary.startswith("7 successful edits to src/a.py")
    assert "ended_green" not in thrash.detail
    s.bash("pytest -q", "3 passed")
    thrash = by_signal(detect(s, tmp_path), signals.EDIT_THRASH)[0]
    assert thrash.detail["ended_green"] == "1"  # it converged after all

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
    s.grep("Other", "/repo/src", found=True)
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
    s.bash("make list | grep -q deploy", "", exit_code=1)  # nothing matched
    s.tool("Bash", {"command": "sleep 99"}, "partial", tool_use_result={"interrupted": True})
    assert detect(s, tmp_path) == []

    elsewhere = Session(cwd="/somewhere/else").user("go")
    elsewhere.bash("make test", "boom", exit_code=2)
    assert detect(elsewhere, tmp_path) == []


def test_inspection_commands_are_never_failures(tmp_path: Path) -> None:
    s = Session().user("go")
    for command in (
        "sed -n '1,40p' src/a.py",
        "awk '{print $1}' missing.txt",
        "jq .name package.json",
        "head -5 nope.txt",
        "tail -n 3 nope.txt",
        "cat nope.txt | wc -l",
        "diff a.txt b.txt",
        "git diff --stat HEAD~1",
        "git status --short",
        "git log --oneline -5",
        "git show HEAD:nope.py",
        "cd src && git status",
    ):
        s.bash(command, "fatal: bad revision", exit_code=128)
    assert detect(s, tmp_path) == []


def test_linters_and_exit_code_checks_are_ordinary_failures(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("git diff --exit-code", "diff --git a/x b/x", exit_code=1)
    s.bash("ruff check .", "src/a.py:1:1: F401 unused import\nFound 1 error.", exit_code=1)
    s.bash("mypy", "src/a.py:3: error: Incompatible types in assignment", exit_code=1)
    s.bash("npx eslint .", "1 problem (1 error, 0 warnings)", exit_code=1)
    s.bash("pytest -q", "FAILED tests/test_io.py::test_timeout - assert timed out", exit_code=1)
    s.bash("pytest -q", "E   PermissionError: [Errno 13] Permission denied: 'x'", exit_code=1)
    s.bash("pytest -q", "E   FileNotFoundError: No such file or directory: 'x'", exit_code=1)
    s.bash("pytest -q", "assert 'sh: foo: command not found' in captured.err", exit_code=1)
    found = detect(s, tmp_path)
    # `git diff --exit-code` is a probe; seven failures plus the retry loop on `pytest -q`
    assert len(found) == 7 + 1
    for failure in by_signal(found, signals.COMMAND_FAILURE):
        assert failure.detail["variant"] == "ordinary", failure.excerpt
        assert failure.weight == signals.ORDINARY_FAILURE_WEIGHT and failure.interval is None


@pytest.mark.parametrize(
    ("output", "code"),
    [
        ("bash: frobnicate: command not found", 127),
        ("sh: 1: jest: not found", 127),
        ("zsh: command not found: pnpm", 127),
        ("anything at all", 126),
        ("bash: ./run.sh: Permission denied", 1),
        ("bash: line 1: ./scripts/build.sh: No such file or directory", 1),
        ("E   ModuleNotFoundError: No module named 'shop'", 4),
        ("/usr/bin/python3: No module named pytest", 1),
        ("ImportError while loading conftest '/r/tests/conftest.py'.", 4),
        ("python: can't open file '/r/manage.py': [Errno 2] No such file or directory", 2),
        ("Error: Cannot find module '/r/dist/index.js'", 1),
        ("pytest: error: unrecognized arguments: --cov", 4),
        ("ERROR: file or directory not found: tests/unit", 4),
        ("error: unexpected argument '--fast' found", 2),
        ("make: *** No rule to make target 'tset'.  Stop.", 2),
        ('npm error Missing script: "tset"', 1),
        ("fatal: not a git repository (or any of the parent directories): .git", 128),
        ("error: could not find `Cargo.toml` in `/r` or any parent directory", 101),
        ("curl: (7) Failed to connect to localhost port 8000: Connection refused", 7),
    ],
)
def test_invocation_error_shapes(output: str, code: int) -> None:
    assert signals.is_invocation_error(f"some preamble\n{output}\ntrailer", code), output


def test_chains_are_named_after_their_last_significant_command(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("mkdir -p out && pytest tests/x.py", "1 failed", exit_code=1)
    s.bash(
        "touch x && uv pip install -e . && python -m pytest | tail -5",
        "E   ModuleNotFoundError: No module named 'shop'",
        exit_code=1,
    )
    first, second = detect(s, tmp_path)
    assert keys(first)[0] == ("command", "pytest")
    assert keys(second) == [("command", "python -m pytest")] and second.weight == 1.0


def test_pipeline_real_failure_still_counts(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash(
        "make build 2>&1 | grep -v warning", "make: *** No rule to make target 'build'", exit_code=2
    )
    (failure,) = detect(s, tmp_path)
    assert keys(failure) == [("command", "make build")]


def test_harness_timeout_counts_only_at_the_start_of_the_output(tmp_path: Path) -> None:
    assert signals.is_invocation_error("Command timed out after 2m 0s\npartial output", 1)
    assert signals.is_invocation_error("Exit code 143\nCommand timed out after 2m 0s", 143)
    printed_by_a_test = (
        "> jest\n\nFAIL src/a.test.js\n  \u25cf handles Command timed out after 5s\n\n"
        "Command timed out after 5s\n"
    )
    assert not signals.is_invocation_error(printed_by_a_test, 1)
    s = Session().user("go")
    s.bash("npm test", printed_by_a_test, exit_code=1)
    s.bash("npm run e2e", "Command timed out after 2m 0s\nTerminated", exit_code=1)
    ordinary, timeout = detect(s, tmp_path)
    assert (ordinary.detail["variant"], ordinary.weight) == ("ordinary", 0.3)
    assert (timeout.detail["variant"], timeout.weight) == ("invocation", 1.0)


def test_shell_structure_does_not_hide_the_failing_command(tmp_path: Path) -> None:
    s = Session().user("go")
    no_module = "ModuleNotFoundError: No module named 'y'"
    s.bash("cat <<'EOF' > gen.py\nimport y\nEOF\npython gen.py", no_module, exit_code=1)
    s.bash(
        "cat > t.sh <<EOF\nfoo\nEOF\nnpm run build",
        'npm error Missing script: "build"',
        exit_code=1,
    )
    no_rule = "make: *** No rule to make target 'lint'.  Stop."
    s.bash("if ! make lint; then exit 1; fi", no_rule, exit_code=1)
    not_found = "ERROR: file or directory not found: a"
    s.bash("for t in a b; do pytest $t || exit 1; done", not_found, exit_code=1)
    s.bash("make -j4 lint", no_rule, exit_code=2)
    s.bash("bash -c 'cd api && cargo test'", "error: could not find `Cargo.toml`", exit_code=101)
    found = detect(s, tmp_path)
    assert [keys(f) for f in by_signal(found, signals.COMMAND_FAILURE)] == [
        [("command", "python gen.py")],
        [("command", "npm run build")],
        [("command", "make lint")],
        [("command", "pytest")],
        [("command", "make lint")],
        [("command", "cargo test (api/)")],
    ]
    assert all(f.detail["variant"] == "invocation" for f in found)


def test_files_named_by_setup_steps_are_not_blamed(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash(
        "cp config/base.py config/local.py && cat notes.md && pytest tests/test_x.py | tail -5",
        "FAILED tests/test_x.py::test_total\n1 failed",
        exit_code=1,
    )
    known = ("config/base.py", "config/local.py", "notes.md", "tests/test_x.py")
    (failure,) = detect(s, tmp_path, known=known)
    assert keys(failure) == [("command", "pytest"), ("file", "tests/test_x.py")]


# ---- the normal red/green loop is not friction ------------------------------------------------


def tdd_session(name: str = "tdd") -> Session:
    """Edit, run the tests, see them fail, edit again ... until they pass."""
    s = Session(name).user("implement the feature with tests")
    for step in range(4):
        s.edit(A, f"a{step}", f"b{step}")
        s.bash(
            "pytest tests/test_feat.py -x",
            "FAILED tests/test_feat.py::test_x\n1 failed",
            exit_code=1,
        )
    s.edit(A, "z", "y")
    s.bash("pytest tests/test_feat.py -x", "1 passed")
    return s


def test_healthy_tdd_is_not_friction(tmp_path: Path) -> None:
    assert detect(tdd_session(), tmp_path) == []


def test_red_then_green_without_a_fix_is_still_reported(tmp_path: Path) -> None:
    flaky = Session().user("go")
    flaky.bash("pytest", "1 failed", exit_code=1).bash("pytest", "1 passed")
    (failure,) = detect(flaky, tmp_path)
    assert (failure.signal, failure.weight) == (signals.COMMAND_FAILURE, 0.3)

    never_green = Session().user("go")
    never_green.bash("pytest", "1 failed", exit_code=1).edit(A).bash("make build", "ok")
    assert len(detect(never_green, tmp_path)) == 1  # edited, but the tests were never re-run

    setup = Session().user("go")
    setup.bash("pytest", "ModuleNotFoundError: No module named 'x'", exit_code=1)
    setup.edit("/repo/conftest.py").bash("pytest", "1 passed")
    (invocation,) = detect(setup, tmp_path)
    assert invocation.detail["variant"] == "invocation"  # a setup problem stays friction


def test_an_edit_between_failures_resets_the_retry_loop(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("npm test", "boom", exit_code=1).bash("npm test", "boom", exit_code=1)
    s.edit(A)  # the agent changed something: the next run is a new attempt
    s.bash("npm test", "boom", exit_code=1).bash("npm test", "boom", exit_code=1)
    found = detect(s, tmp_path)
    assert by_signal(found, signals.RETRY_LOOP) == []
    s.bash("npm test", "boom", exit_code=1)  # three in a row with nothing changed
    assert len(by_signal(detect(s, tmp_path), signals.RETRY_LOOP)) == 1

    failed_edit = Session().user("go")
    for _ in range(3):
        failed_edit.bash("npm test", "boom", exit_code=1)
        failed_edit.edit(A, error=builders.EDIT_NOT_FOUND)  # nothing changed on disk
    assert len(by_signal(detect(failed_edit, tmp_path), signals.RETRY_LOOP)) == 1


def test_retry_loop_three_consecutive_failures(tmp_path: Path) -> None:
    s = Session().user("go")
    for prefix in ("", "CI=1 ", "", "CI=1  "):  # env and spacing do not make it another command
        s.bash(f"{prefix}npm test  -- --runInBand", "Error: boom", exit_code=1)
    found = detect(s, tmp_path)
    (loop,) = by_signal(found, signals.RETRY_LOOP)
    assert loop.summary == (
        "`npm test` was run 4 times in a row with identical arguments and failed each time"
    )
    assert loop.detail["count"] == "4"
    assert keys(loop) == [("command", "npm test")] and loop.interval is None
    assert len(by_signal(found, signals.COMMAND_FAILURE)) == 4


def test_narrowing_a_failing_test_is_not_a_retry_loop(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("pytest", "3 failed", exit_code=1)
    s.bash("pytest tests/test_a.py -x -vv", "1 failed", exit_code=1)
    s.bash("pytest tests/test_a.py::test_total --tb=long", "1 failed", exit_code=1)
    s.bash("pytest tests/test_a.py::test_total --tb=long -s", "1 failed", exit_code=1)
    found = detect(s, tmp_path)
    assert by_signal(found, signals.RETRY_LOOP) == [] and len(found) == 4


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
    # One round trip, 5 (run) + 10 + 4 (think): the read succeeded, so the agent had moved
    # on; what it did until `make test` is work, not waste.
    assert setup.interval[1] - setup.interval[0] == pytest.approx(19)
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


def test_declined_chain_is_keyed_by_the_step_the_human_objected_to(tmp_path: Path) -> None:
    s = Session().user("go")
    s.reject(
        "Bash",
        {"command": "rm -rf migrations/versions && alembic revision --autogenerate"},
        "don't delete migrations",
    )
    s.reject("Bash", {"command": "cd api && git add -A && git push --force origin main"})
    s.reject("Bash", {"command": "make build && ./deploy.sh prod"}, "never run deploy.sh here")
    s.reject("Bash", {"command": "cat Makefile && npm run build && npm test"})
    s.reject("Bash", {"command": "ls && cat x"})
    found = detect(s, tmp_path)
    assert [keys(f) for f in found] == [
        [("command", "rm")],  # destructive step, not the last one
        [("command", "git push")],
        [("command", "deploy.sh")],  # the step the reason names
        [("command", "npm run build")],  # first step that changes anything
        [("command", "cat")],
    ]


def test_interrupt_attributes_to_last_tool_target(tmp_path: Path) -> None:
    s = Session().user("go")
    s.read(B).edit(A)
    s.tick(30).interrupt()
    (interrupt,) = detect(s, tmp_path)
    assert interrupt.signal == signals.USER_INTERRUPT and keys(interrupt) == [("file", "src/a.py")]
    assert interrupt.interval is not None and interrupt.interval[1] > interrupt.interval[0]

    shell = Session().user("go")
    shell.bash("npm run build", "…").interrupt()
    (stopped,) = detect(shell, tmp_path)  # stopping a command says nothing about the command
    assert stopped.targets == [] and "while it was running `npm run build`" in stopped.summary

    testing = Session().user("go")
    testing.edit(A).edit(B).bash("pytest", "…").interrupt()
    (stopped,) = detect(testing, tmp_path)  # it goes to what the agent had been changing
    assert [(t.key, t.share) for t in stopped.targets] == [("src/b.py", 0.5), ("src/a.py", 0.5)]


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


STRONG_CORRECTIONS = [
    "No, that's the wrong file",
    "no. use the other one",
    "Nope, try again",
    "No - the other module",
    "No, I said use PaymentClient, not the legacy module.",
    "that's not what I asked for",
    "This is wrong.",
    "That is incorrect, the limit is 50",
    "it's not working, same stack trace",
    "you broke the build",
    "You didn\u2019t run the tests",
    "you forgot the migration",
    "you removed the retry logic, put it back",
    "You shouldn't have touched the lockfile",
    "please revert that change",
    "undo the last edit",
    "Revert. We agreed on the old API",
    "roll back those changes to config.py",
    "it's still failing with the same error",
    "Still broken on CI",
    "the test is still not working",
    "I said use pnpm",
    "I told you not to touch the lockfile",
    "I asked you to keep the old signature",
    "I already said we don't support python 3.8",
    "I never asked for a refactor",
    "this is not what I wanted",
    "why did you delete the fixture?",
    "Why did you change the public API?",
    "Great effort, but you broke the login page",
    "hmm that doesn't work, use pnpm",
    "wrong. use pnpm",
    "that didn't work",
    "This doesn't work. Try again",
    "wtf, you deleted my file",
    "That's not right",
    "stop. use the other client",
    "it is broken again",
    "ugh, tests are failing again",
    "You're editing the wrong file",
    "NO!!! not like that",
    "no",
    "No, I meant the other module",
    "stop editing the generated file, change the template instead",
    "Not quite, the header should stay",
    "not exactly - the total excludes tax",
    "you keep editing the generated file",
    "bad idea. put it back",
    "You're still editing the wrong branch",
    "ok that's wrong, the total excludes tax",
    "the build is still broken",
    "undo your change to the lockfile",
    "Fix it properly this time, you broke the build",
]

PROBABLE_CORRECTIONS = [
    "Don't use requests here, use httpx instead",
    "Don't call legacy_charge() here, it's deprecated. Use PaymentClient.charge() instead.",
    "Actually, use the factory instead",
    "the result is wrong, it shouldn't include drafts",
    "that helper is deprecated, don't import it",
    "you used the old client again, that's a mistake",
    "Please don't use mocks here; use the real db instead",
]

BENIGN_FOLLOW_UPS = [
    "No way, that was fast! Thanks",
    "Looks good. Why do you think this approach is faster?",
    "I said earlier we'd do b.py next, go ahead",
    "It was not urgent but thanks for the quick fix",
    "Don't forget to add tests",
    "don't forget the changelog",
    "Actually, can you also add a docstring?",
    "Actually let's also add a test",
    "that should be enough",
    "never mind the README",
    "actually, never mind the cache; stop there",
    "Great, thanks! Now add pagination to the list endpoint.",
    "no problem, carry on",
    "No worries, take your time",
    "no thanks, that's all for today",
    "No, that's fine as it is",
    "No need to add more tests",
    "No rush on this one",
    "Now do the same for the admin page",
    "Looks good. Commit it.",
    "Can you also update the docs?",
    "not bad! next: the README",
    "Thanks, that works. Don't change anything else, just commit.",
    "Perfect. Instead of stopping here, let's also do the CLI.",
    "Nice! What should we do about the wrong status code in the legacy endpoint?",
    "use the factory instead",
    "it should be sorted by date",
    "Why do you prefer a dataclass here?",
    "why are you so fast today",
    "What would you do differently?",
    "Please stop the dev server when you're done",
    "Could you explain what this function does?",
    "ok, next step: wire it into the router",
    "Yes, go ahead and don't wait for me",
    "Add a test for the case where the input is wrong",
    "The docs say this flag is deprecated - can you check?",
    "Run the tests again please",
    "I think we should also handle the empty case",
    "Is it supposed to return None there?",
    "Let's revert to this topic later; first the parser",
    "How do I undo a commit in git, by the way?",
    "Before you continue: which Python version does CI use?",
    "nothing else, thank you",
    "note: we never deploy on Fridays",
    "",
    # a numbered item, a determiner, an agreement: "no" is not always an objection
    "No. 3 on the list is next, please do that",
    "Great. No. 2 on the list next: add docs to src/feat.py",
    "No tests are needed for this, just ship it",
    "Nope, all good, continue",
    # a strong phrase followed by praise or a dismissal
    "This is not what I expected but it's great, keep going",
    "Now it's still failing in CI but that's a flaky infra issue unrelated to you, ignore it.",
    "Can you explain why did you use a dict here? I like it",
    "you forgot nothing, great job! Next: implement the parser",
    "I asked you to do X earlier and you nailed it. Now do Y",
    # the phrase is about someone or something else
    "I said to my boss this is great. Next: add logging",
    "Undo support is the next feature: add an undo stack",
    "Undo support is the next feature, please add it",
    "Revert button should be added to the UI",
    "The test that checks that this is wrong input raises ValueError passes. Now add another",
    # one weak cue is an instruction, not a correction
    "Don't commit yet, instead show me the diff",
    "use PaymentClient, not legacy",
    # a new task that describes a bug is not a verdict on the agent's work
    "Fix the bug where the app crashes again after login",
    "The customer says it's not working. Investigate the billing module.",
    "It doesn't work on Windows yet; next task: add Windows support",
    "Can you explain why this is not working in prod? New task.",
    "The login page is still broken on Safari, can you look at that next?",
    "Users report that it doesn't work offline. Add a cache.",
    "Investigate why the nightly job is failing again",
    "We broke the build yesterday, that's fixed now. Add a changelog entry",
    "Write a test that proves this is not working for empty input",
    "The docs are wrong. Update them to mention pnpm",
    # undo/revert/stop as part of a task, not aimed at the agent's change
    "revert the migration tool design doc to draft status",
    "Add an undo button; revert should restore the last saved state",
    "Implement rollback: roll back the last migration when the health check fails",
    "Stop the server and restart it with DEBUG=1",
    "Stop. Hammer time",
    "Nope, nothing else!",
    "No, not yet",
    "I said use tabs in my last project but here spaces are fine",
    # languages other than English are not recognised (documented limitation)
    "Nein, das ist falsch",
]

#: Replies that open with "no" answer a question when the agent has just asked one.
ANSWERS_TO_A_QUESTION = [
    "No, I meant the other feature; can you also add pagination?",
    "No, keep the old name",
    "no",
    "Nope, the old one",
    "No. Just the unit tests",
]


@pytest.mark.parametrize("text", STRONG_CORRECTIONS)
def test_strong_corrections(text: str) -> None:
    assert correction_confidence(text)[0] == signals.CORRECTION_STRONG, text


@pytest.mark.parametrize("text", PROBABLE_CORRECTIONS)
def test_probable_corrections_need_two_cues(text: str) -> None:
    confidence, cues = correction_confidence(text)
    assert confidence == signals.CORRECTION_PROBABLE and len(cues) >= 2, text


@pytest.mark.parametrize("text", BENIGN_FOLLOW_UPS)
def test_ordinary_follow_ups_are_not_corrections(text: str) -> None:
    assert correction_confidence(text) == (0.0, []), text


@pytest.mark.parametrize("text", ANSWERS_TO_A_QUESTION)
def test_no_after_an_agent_question_is_an_answer(text: str) -> None:
    assert correction_confidence(text)[0] == signals.CORRECTION_STRONG, text
    assert correction_confidence(text, after_question=True) == (0.0, []), text


def test_a_verdict_after_a_long_pause_is_only_probable() -> None:
    late = signals.CORRECTION_STALE_SECONDS + 1
    for text in ("No, that's wrong", "Not quite, the header should stay", "Still broken on CI"):
        assert correction_confidence(text, idle_seconds=60)[0] == signals.CORRECTION_STRONG
        confidence, cues = correction_confidence(text, idle_seconds=late)
        assert confidence == signals.CORRECTION_PROBABLE and cues[-1] == "long pause", text
    # words addressed to the agent do not go stale
    for text in ("you broke the build", "please revert that change", "I told you to use pnpm"):
        assert correction_confidence(text, idle_seconds=late)[0] == signals.CORRECTION_STRONG


def test_new_task_prompts_long_after_the_agent_finished(tmp_path: Path) -> None:
    """Three sessions: edit, "Done.", and ten minutes later a new task that describes a bug."""
    prompts = (
        "Fix the bug where the app crashes again after login",
        "The customer says it's not working. Investigate the billing module.",
        "It doesn't work on Windows yet; next task: add Windows support",
        "Can you explain why this is not working in prod? New task.",
        "revert the migration tool design doc to draft status",
        "I said use tabs in my last project but here spaces are fine",
        "Nope, nothing else!",
        "Stop. Hammer time",
    )
    for text in prompts:
        s = Session().user("add the feature")
        s.edit(A).say("Done.").tick(600).user(text)
        assert detect(s, tmp_path) == [], text
    late = Session().user("add the feature")
    late.edit(A).say("Done.").tick(600).user("No, the other module")
    (fix,) = detect(late, tmp_path)
    assert fix.signal == signals.PROBABLE_CORRECTION and '"long pause"' in fix.summary


def test_other_strong_phrases_still_count_after_a_question() -> None:
    for text in ("you broke the build", "No, that's the wrong file", "I told you to use pnpm"):
        assert correction_confidence(text, after_question=True)[0] == signals.CORRECTION_STRONG


def test_answering_the_agents_question_is_not_a_correction(tmp_path: Path) -> None:
    asked = Session().user("add the feature")
    asked.edit(A).say("Done. Should I also rename the helper?")
    asked.user("No, I meant the other feature next: can you also add pagination?")
    asked.edit(A, "p", "q").say("Pagination is in.")
    asked.user("No, that is the wrong endpoint")  # nothing was asked this time
    (fix,) = detect(asked, tmp_path)
    assert fix.signal == signals.PROBABLE_CORRECTION and fix.line == len(asked.lines)
    assert fix.excerpt == "" and "quote" not in fix.detail  # a guess is never quoted

    # the question must be the last thing the agent said before the reply
    stale = Session().user("add the feature")
    stale.say("Which module?").edit(A).say("I picked src/a.py.")
    stale.user("No, the other module")
    assert len(detect(stale, tmp_path)) == 1


def test_generic_assistant_message_marks_an_answer(tmp_path: Path) -> None:
    def events(question: str) -> str:
        rows = [
            {"session": "g", "kind": "user_message", "text": "add it", "cwd": "/repo"},
            {"session": "g", "kind": "tool_call", "id": 1, "op": "edit", "path": "src/a.py"},
            {"session": "g", "kind": "tool_result", "id": 1, "ok": True},
            {"session": "g", "kind": "assistant_message", "text": question},
            {"session": "g", "kind": "user_message", "text": "No, the other module"},
        ]
        return "".join(json.dumps(row) + "\n" for row in rows)

    path = tmp_path / "g.jsonl"
    path.write_text(events("Should I also add tests?"))
    assert detect(path, tmp_path) == []
    path.write_text(events("Added it to src/a.py."))
    assert [f.signal for f in detect(path, tmp_path)] == [signals.PROBABLE_CORRECTION]


def test_a_friendly_conversation_produces_no_corrections(tmp_path: Path) -> None:
    assert len(BENIGN_FOLLOW_UPS) >= 60 and len(STRONG_CORRECTIONS) >= 40
    s = Session().user("start")
    for text in BENIGN_FOLLOW_UPS:
        s.edit(A, text or "x", "y").user(text or "ok")
    found = detect(s, tmp_path)
    assert by_signal(found, signals.USER_CORRECTION) == []


def test_correction_phrases_are_only_read_at_the_start() -> None:
    pasted_log = "Here is the CI log:\n" + "x" * 400 + "\nerror: you broke it, revert that"
    assert correction_confidence(pasted_log)[0] == 0.0


def test_correction_splits_over_edited_files(tmp_path: Path) -> None:
    s = Session().user("add the feature")
    s.read("/repo/src/c.py").edit(A).edit(B).edit(A)
    s.tick(100).user("No, that is not how we do it here")
    s.edit(B, "new", "newer")  # the agent goes back to one of the files: corroborated
    (fix,) = detect(s, tmp_path)
    assert fix.signal == signals.USER_CORRECTION and fix.weight == 1.0
    assert fix.detail["corroborated"] == "followed" and "then changed src/b.py" in fix.summary
    # ... and that file, not everything edited before, is what the correction was about
    assert [(t.key, t.share) for t in fix.targets] == [("src/b.py", 1.0)]
    assert fix.excerpt == "No, that is not how we do it here"
    assert "not how we do it" not in fix.summary and 'matched "no, …"' in fix.summary
    assert fix.interval is not None
    # the interval ends at the agent's last activity, not at the (slow) human reply
    assert fix.interval[1] - fix.interval[0] < 60

    unanswered = Session().user("add the feature")
    unanswered.edit(A).edit(B).user("No, that is not how we do it here")
    (fix,) = detect(unanswered, tmp_path)  # probable: spread over what had been edited
    assert [(t.key, t.share) for t in fix.targets] == [("src/b.py", 0.5), ("src/a.py", 0.5)]
    assert fix.signal == signals.PROBABLE_CORRECTION


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
    path.write_text("".join(json.dumps(e) + "\n" for e in other_agent()))
    found = detect(path, tmp_path, repo="/home/dev/shopfront")
    assert [f.signal for f in found] == [
        signals.EDIT_MISS,
        signals.COMMAND_FAILURE,
        signals.USER_CORRECTION,
    ]
    assert found[1].detail["recovered_by"] == "make test"
    assert keys(found[2]) == [("file", "src/shopfront/api/schema_gen.py")]


# ---- round 3: replacements, failing steps, probes ------------------------------------------


def test_only_a_command_doing_the_same_job_is_a_replacement(tmp_path: Path) -> None:
    not_found = "bash: pytest: command not found"
    install = Session().user("go")
    install.bash("pytest", not_found, exit_code=127)
    install.bash("pip install pytest", "Successfully installed pytest")
    install.bash("pytest", "1 passed")
    (failure,) = detect(install, tmp_path)
    assert "recovered_by" not in failure.detail
    assert failure.detail["fixed_by_setup"] == "pip install"

    vcs = Session().user("go")
    vcs.bash("npm test", "sh: 1: jest: not found", exit_code=127)
    vcs.bash("git add -A && git commit -m wip", "[main 1a2b3c4] wip")
    vcs.bash("rm -rf dist", "").bash("make codegen", "ok")
    (failure,) = detect(vcs, tmp_path)
    assert "recovered_by" not in failure.detail and "fixed_by_setup" not in failure.detail

    same_job = Session().user("go")
    same_job.bash("pytest", not_found, exit_code=127)
    same_job.bash("git status", "clean").bash("uv run pytest", "1 passed")
    same_job.bash("npm run build", 'npm error Missing script: "build"', exit_code=1)
    same_job.bash("make build", "ok")
    first, second = detect(same_job, tmp_path)
    assert first.detail["recovered_by"] == "uv run pytest"
    assert second.detail["recovered_by"] == "make build"

    works_later = Session().user("go")
    works_later.bash("pytest", not_found, exit_code=127)
    works_later.bash("make test", "ok")
    works_later.user("again").bash("pytest", "1 passed")  # it was not the command's fault
    (failure,) = detect(works_later, tmp_path)
    assert "recovered_by" not in failure.detail


@pytest.mark.parametrize(
    ("command", "output", "code", "key", "variant"),
    [
        (
            "touch x && uv pip install -e . && python -m pytest tests/test_a.py | tail -5",
            "  x Failed to build shop @ file:///repo\n  Readme file does not exist: README.md",
            1,
            "uv pip install",
            "invocation",
        ),
        (
            "npm install && pytest tests/",
            "npm error code E401\nnpm error Incorrect or missing password.",
            1,
            "npm install",
            "invocation",
        ),
        (
            "pip install -r requirements.txt && pytest tests/test_a.py",
            "  error: subprocess-exited-with-error\n  x python setup.py egg_info did not run",
            1,
            "pip install",
            "invocation",
        ),
        (
            "make build && pytest tests/test_a.py",
            "cc -c x.c\nmake: *** [Makefile:3: build] Error 1",
            2,
            "make build",
            "ordinary",
        ),
        (
            "npm ci && pytest tests/test_a.py",
            "added 3 packages\n===== 1 failed, 2 passed in 0.31s =====",
            1,
            "pytest",
            "ordinary",
        ),
    ],
)
def test_the_output_decides_which_step_of_a_chain_failed(
    tmp_path: Path, command: str, output: str, code: int, key: str, variant: str
) -> None:
    s = Session().user("go")
    s.bash(command, output, exit_code=code)
    (failure,) = detect(s, tmp_path, known=("tests/test_a.py",))
    assert keys(failure)[0] == ("command", key)
    assert failure.detail["variant"] == variant and "ambiguous_step" not in failure.detail
    # test files are blamed only when the test step is the one that failed
    assert (("file", "tests/test_a.py") in keys(failure)) == (key == "pytest")


def test_an_undecidable_chain_blames_no_file_and_says_so(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("./gen.sh && mytool check tests/test_a.py", "something went wrong", exit_code=1)
    (failure,) = detect(s, tmp_path, known=("tests/test_a.py",))
    assert keys(failure) == [("command", "mytool")]
    assert failure.detail["ambiguous_step"] == "1"
    assert "does not show which step" in failure.summary
    # a single real command is never ambiguous
    one = Session().user("go")
    one.bash("cd api && mytool check tests/test_a.py | tail -3", "bad", exit_code=1)
    (failure,) = detect(one, tmp_path, known=("tests/test_a.py",))
    assert "ambiguous_step" not in failure.detail and ("file", "tests/test_a.py") in keys(failure)


@pytest.mark.parametrize(
    "output",
    [
        "npm error code ERESOLVE\nnpm error ERESOLVE unable to resolve dependency tree",
        "  x Failed to build `shop @ file:///repo`",
        "error: subprocess-exited-with-error",
        "ERROR: Failed building wheel for cryptography",
        "  x No solution found when resolving dependencies:",
        "ERR_PNPM_OUTDATED_LOCKFILE  Cannot install with frozen-lockfile",
    ],
)
def test_install_and_build_failures_are_setup_errors(output: str) -> None:
    assert signals.is_invocation_error(f"preamble\n{output}\ntrailer", 1), output


def test_existence_probes_before_creating_something_are_not_misses(tmp_path: Path) -> None:
    s = Session().user("add the feature")
    s.glob("src/new_feature*.py").grep("NewFeature", "/repo/src")
    s.grep("OldHelper", "/repo/src")  # looked for something that really is not there
    s.write("/repo/src/new_feature.py", "class NewFeature:\n    pass\n")
    (miss,) = detect(s, tmp_path)
    assert miss.signal == signals.SEARCH_MISS and miss.detail["pattern"] == "OldHelper"

    edit = Session().user("add the helper")
    edit.grep("def parse_sku", "/repo/src").edit(A, "x = 1", "def parse_sku(raw):\n    return raw")
    assert detect(edit, tmp_path) == []

    elsewhere = Session().user("go")
    elsewhere.glob("src/new_feature*.py").grep("NewFeature", "/repo/src")
    elsewhere.write("/repo/docs/new_feature.py", "class NewFeature: ...")  # outside the scope
    elsewhere.write("/repo/src/other.py", "x = 1")  # neither the name nor the text
    assert len(detect(elsewhere, tmp_path)) == 2


def test_a_retry_loop_keeps_its_failures_even_if_the_run_ends_green(tmp_path: Path) -> None:
    s = Session().user("go")
    for _ in range(12):
        s.bash("pytest", "1 failed", exit_code=1)  # re-run with nothing changed
    s.edit("/repo/notes.md").bash("pytest", "1 passed")
    found = detect(s, tmp_path)
    assert len(by_signal(found, signals.RETRY_LOOP)) == 1
    assert len(by_signal(found, signals.COMMAND_FAILURE)) == 12
    # fewer than three in a row is the ordinary loop and is forgiven by edit -> green
    short = Session().user("go")
    short.bash("pytest", "1 failed", exit_code=1).bash("pytest", "1 failed", exit_code=1)
    short.edit(A).bash("pytest", "1 passed")
    assert detect(short, tmp_path) == []


def test_command_keys_can_be_made_public(tmp_path: Path) -> None:
    from agent_friction.adapters import parse_transcript
    from agent_friction.attribution import PathResolver, public_key

    s = Session().user("go")
    s.bash("make deploy-customer-acme", "boom", exit_code=2)
    s.bash("python deploy_acme_customer.py", "boom", exit_code=1)
    s.bash("pytest", "bash: pytest: command not found", exit_code=127)
    s.bash("make test", "ok")
    s.reject("Bash", {"command": "uvx internal-tool-acme --prod"})
    ctx = signals.DetectContext(
        PathResolver(Path("/repo")), lambda rel: True, lambda rel: False, command_key=public_key
    )
    stream = parse_transcript(s.save(tmp_path)).streams[0]
    found, exposed = signals.detect_with_exposure(stream, ctx)
    text = " ".join([f.summary for f in found] + [k for _, k in exposed])
    text += " ".join(t.key for f in found for t in f.targets)
    text += " ".join(v for f in found for k, v in f.detail.items() if k != "command")
    assert "acme" not in text
    assert [keys(f)[0][1] for f in found] == [
        public_key("make deploy-customer-acme"),
        public_key("python deploy_acme_customer.py"),
        "pytest",
        public_key("uvx internal-tool-acme"),
    ]
    assert found[2].detail["recovered_by"] == "make test"


# ---- round 4 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("red", "green"),
    [
        ("pytest tests/test_a.py", "make test"),
        ("pytest -k total", "python -m pytest"),
        ("npm test -- a.test.js", "npm run test:all"),
    ],
)
def test_red_edit_green_with_another_test_command_is_not_friction(
    tmp_path: Path, red: str, green: str
) -> None:
    s = Session().user("fix the total")
    s.bash(red, "FAILED tests/test_a.py::test_total - assert 1 == 2\n1 failed", exit_code=1)
    s.edit(A).bash(green, "5 passed")
    assert detect(s, tmp_path, known=("tests/test_a.py",)) == []
    # without the edit the red run stays, but nothing "replaced" a command that did run
    plain = Session().user("go")
    plain.bash(red, "1 failed", exit_code=1).bash(green, "5 passed")
    (failure,) = detect(plain, tmp_path)
    assert failure.detail["variant"] == "ordinary" and "recovered_by" not in failure.detail


def test_shell_commands_that_rewrite_files_count_as_edits(tmp_path: Path) -> None:
    lint = "src/a.py:1:1: F401 unused import\nFound 1 error."
    s = Session().user("clean up")
    s.bash("ruff check .", lint, exit_code=1).bash("ruff check --fix .", "Fixed 1 error.")
    s.bash("ruff check .", "All checks passed!")
    s.bash("ruff format --check .", "Would reformat: src/a.py", exit_code=1)
    s.bash("ruff format .", "1 file reformatted").bash("ruff format --check .", "ok")
    s.bash("npm test", "1 failing", exit_code=1)
    s.bash("sed -i 's/tru/true/' src/a.js", "").bash("npm test", "3 passing")
    s.bash("make build", "error", exit_code=2)
    s.bash("cat > src/gen.py <<'EOF'\nX = 1\nEOF", "").bash("make build", "ok")
    assert detect(s, tmp_path) == []

    log = Session().user("go")  # writing a log file is not a change to the project
    log.bash("npm test", "1 failing", exit_code=1)
    log.bash("echo retry > /tmp/attempts.log", "").bash("npm test", "3 passing")
    assert len(detect(log, tmp_path)) == 1


def test_waiting_for_the_human_is_not_time_lost(tmp_path: Path) -> None:
    s = Session().user("run the tests")
    s.bash("pytest", "bash: pytest: command not found", exit_code=127, took=2)
    s.tick(3).say("pytest is not installed. How do you want to proceed?")
    s.tick(3600).user("use make test")  # an hour later
    s.bash("make test", "ok")
    (failure,) = detect(s, tmp_path)
    assert failure.interval is not None
    # 2s run + 3s until the agent spoke: nothing of the hour is claimed
    assert failure.interval[1] - failure.interval[0] == pytest.approx(5)

    silent = Session().user("run the tests")
    silent.bash("pytest", "bash: pytest: command not found", exit_code=127, took=2)
    silent.tick(3600).user("use make test")  # the human is the next thing in the transcript
    (failure,) = detect(silent, tmp_path)
    assert failure.interval is not None
    assert failure.interval[1] - failure.interval[0] == pytest.approx(2)  # the call itself

    miss = Session().user("go")
    miss.grep("Nothing", "/repo/src").tick(7200).user("ok, next")
    (search,) = detect(miss, tmp_path)
    assert search.interval is None or search.interval[1] - search.interval[0] < 10


DENIALS = [
    ("Claude requested permissions to use Bash, but you haven't granted it yet.", "permission"),
    (
        "Permission to use Bash has been denied. IMPORTANT: You *may* attempt to accomplish "
        "this action using other tools.",
        "permission",
    ),
    ("Permission for this command was denied by the safety classifier.", "permission"),
    ("PreToolUse:Bash hook error: [python3 guard.py]: pushing to main is blocked", "hook"),
]


@pytest.mark.parametrize(("output", "variant"), DENIALS)
def test_permission_and_hook_denials_are_not_command_failures(
    tmp_path: Path, output: str, variant: str
) -> None:
    s = Session().user("go")
    s.tool("Bash", {"command": "cd api && git push origin main"}, output, is_error=True)
    (denied,) = detect(s, tmp_path)
    assert denied.signal == signals.PERMISSION_DENIED and denied.detail["variant"] == variant
    assert keys(denied) == [("command", "git push")]
    by = "a hook" if variant == "hook" else "permission settings"
    assert denied.summary == f"Bash on `git push` was denied by {by}; it did not run"


def test_denied_edits_and_ordinary_output_mentioning_permissions(tmp_path: Path) -> None:
    s = Session().user("go")
    refused = (
        "Claude requested permissions to write to /repo/src/a.py, but you haven't granted it yet."
    )
    s.tool("Edit", {"file_path": A, "old_string": "a", "new_string": "b"}, refused, is_error=True)
    # a program that fails with a permission error did run: still a command failure
    s.bash("make deploy", "deploy: Permission to use bucket has been denied", exit_code=1)
    denied, failure = detect(s, tmp_path)
    assert denied.signal == signals.PERMISSION_DENIED and keys(denied) == [("file", "src/a.py")]
    assert failure.signal == signals.COMMAND_FAILURE


IDE_BLOCK = (
    "<ide_opened_file>The user opened the file /repo/README.md in the IDE. This may or may "
    "not be related to the current task. No, that's wrong.</ide_opened_file>"
)


def ide_prompt(session: Session, *texts: str) -> Session:
    blocks = [{"type": "text", "text": text} for text in texts]
    session._line("user", {"role": "user", "content": blocks})
    return session


def test_ide_context_blocks_are_not_human_text(tmp_path: Path) -> None:
    s = Session().user("add the feature")
    s.read("/repo/README.md").edit(A)
    ide_prompt(s, IDE_BLOCK, "No, use the Decimal type, I told you.")
    s.edit(A, "new", "newer")
    (fix,) = detect(s, tmp_path, known=("README.md",))
    assert fix.excerpt == "No, use the Decimal type, I told you."  # the quote is the human's
    assert keys(fix) == [("file", "src/a.py")]  # README.md is named by the IDE, not the human

    friendly = Session().user("add the feature")
    friendly.edit(A)
    ide_prompt(friendly, IDE_BLOCK, "Great, thanks. Now add a docstring.")
    ide_prompt(friendly.edit(A), "<ide_selection>you broke it, revert that</ide_selection>")
    ide_prompt(
        friendly.edit(A),
        "<ide_diagnostics>[{'message': 'this is wrong'}]</ide_diagnostics>",
        "<system-reminder>I told you not to</system-reminder>looks good, next: the CLI",
    )
    selection = "<ide_selection>The user selected lines 1 to 3:\nwrong wrong</ide_selection>"
    friendly.edit(A).user(selection + "\nCan you explain this part?")
    assert detect(friendly, tmp_path) == []


@pytest.mark.parametrize(
    "text",
    [
        "wrong.py needs the same treatment",
        "No. Let's move on to the API layer.",
        "That didn't work because I hadn't pulled. Try again now.",
        "It's not working because my VPN was off.",
        "Why did you use a dataclass here? Just curious",
    ],
)
def test_more_prompts_that_are_not_corrections(text: str) -> None:
    assert correction_confidence(text) == (0.0, []), text


# ---- round 5: robustness, corroborated corrections, ordinary workflows -------------------


def test_an_exception_in_a_detector_skips_the_event_not_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Session().user("go")
    s.bash("pytest -q", "bash: pytest: command not found", exit_code=127)
    s.edit(A, error=builders.EDIT_NOT_FOUND)
    path = s.save(tmp_path)

    def boom(self: object, action: object, result: object) -> None:
        raise IndexError("string index out of range")

    monkeypatch.setattr(signals._Detector, "_on_shell", boom)
    from agent_friction.adapters import parse_transcript
    from agent_friction.attribution import PathResolver

    ctx = signals.DetectContext(PathResolver(Path("/repo")), lambda rel: True, lambda rel: True)
    (stream,) = parse_transcript(path).streams
    found = signals.detect_stream(stream, ctx)
    assert found.skipped_events == 1
    assert [f.signal for f in found.findings] == [signals.EDIT_MISS]

    monkeypatch.undo()
    monkeypatch.setattr(signals, "parse_command", lambda command: 1 / 0)
    found = signals.detect_stream(stream, ctx)
    assert found.skipped_events == 1  # classification failed; the call is kept, unattributed
    assert [f.signal for f in found.findings] == [signals.EDIT_MISS]


WORDING_FALSE_POSITIVES = [
    "Not quite sure how auth works here. Explain it to me",
    "I didn't ask before, but can you also add a changelog entry?",
    "You are still running the server right?",
    "you didn't do anything wrong, I'm just checking",
    "Tests failing again after I merged main. Can you rebase and fix?",
    "It doesn't work in Safari, can you add a polyfill?",
    "Almost, but one more feature: export to CSV",
    "No, go with option B",
    "No, option 2 please",
    "stop. I need to go to lunch, we'll continue later",
    "Close, but can we also support YAML?",
    "It broke after we upgraded node, since I bumped the version",
]


@pytest.mark.parametrize("text", WORDING_FALSE_POSITIVES)
def test_wording_that_only_resembles_a_correction(text: str) -> None:
    assert correction_confidence(text)[0] < signals.CORRECTION_STRONG, text


FORMERLY_MISSED = [
    "doesn't work",
    "Doesn't work.",
    "that broke the build",
    "you've broken the tests",
    "can you undo that",
    "Could you please revert your change?",
    "I didn't want that",
    "Still getting the error",
    "I'm still getting the same error",
    "hmm, it broke the login page",
]


@pytest.mark.parametrize("text", FORMERLY_MISSED)
def test_plain_complaints_are_strong_wording(text: str) -> None:
    assert correction_confidence(text)[0] == signals.CORRECTION_STRONG, text


def _corrected(reply: str, question: str = "Done.") -> Session:
    s = Session().user("add the feature")
    s.edit(A).edit(B).say(question)
    return s.tick(20).user(reply)


def test_wording_alone_is_only_a_probable_correction(tmp_path: Path) -> None:
    # the agent goes on to something else: nothing shows the human meant its changes
    moved_on = _corrected("that broke the build").read("/repo/src/c.py").bash("make build")
    (fix,) = detect(moved_on, tmp_path)
    assert fix.signal == signals.PROBABLE_CORRECTION and fix.detail["corroborated"] == "no"
    assert "confidence 0.4" in fix.summary and "not corroborated" in fix.summary

    # settled by the next human message, not by what happens after it
    late = _corrected("that broke the build").read("/repo/src/c.py").user("ok, thanks").edit(A)
    (fix,) = detect(late, tmp_path)
    assert fix.signal == signals.PROBABLE_CORRECTION

    # nothing had been changed in the turn the human reacts to
    s = Session().user("look around")
    s.read(A).bash("make build").say("Built.").user("No, that's the wrong branch")
    s.edit(A)
    (fix,) = detect(s, tmp_path)
    assert fix.signal == signals.PROBABLE_CORRECTION and fix.targets == []
    assert "no file was changed in the preceding turn" in fix.summary


def test_a_correction_is_certain_when_the_agent_returns_to_the_files(tmp_path: Path) -> None:
    edited = _corrected("that broke the build").edit(B, "new", "newer")
    (fix,) = detect(edited, tmp_path)
    assert fix.signal == signals.USER_CORRECTION and fix.detail["corroborated"] == "followed"

    for command in ("git checkout -- src/a.py", "git restore src/b.py", "git stash", "rm src/a.py"):
        undone = _corrected("can you undo that").bash(command)
        (fix,) = detect(undone, tmp_path)
        assert fix.signal == signals.USER_CORRECTION, command
        assert "the agent then ran `" in fix.summary, command

    for command in ("git checkout main", "pytest src/a.py", "git status", "rm other.txt"):
        other = _corrected("can you undo that").bash(command)
        (fix,) = detect(other, tmp_path)
        assert fix.signal == signals.PROBABLE_CORRECTION, command


def test_a_correction_is_certain_when_the_human_names_a_changed_file(tmp_path: Path) -> None:
    named = _corrected("you broke b.py")  # the session ends here
    (fix,) = detect(named, tmp_path)
    assert fix.signal == signals.USER_CORRECTION and fix.detail["corroborated"] == "named"
    assert keys(fix) == [("file", "src/b.py")]

    # naming a file the agent only read proves nothing about its changes
    s = Session().user("add it")
    s.read("/repo/src/c.py").edit(A).say("Done.").user("No, c.py is the wrong place to look")
    (fix,) = detect(s, tmp_path)
    assert fix.signal == signals.PROBABLE_CORRECTION and keys(fix) == [("file", "src/c.py")]


def test_a_question_anywhere_in_the_final_paragraph_makes_no_an_answer(tmp_path: Path) -> None:
    question = "I fixed the bug.\n\nWant me to refactor util.py? It would take a few minutes."
    for reply in ("No, leave util.py alone. Just commit.", "Stop. Just commit.", "Not quite yet"):
        asked = _corrected(reply, question).edit(A, "new", "newer")
        assert detect(asked, tmp_path) == [], reply
    # a question in an earlier paragraph was not what the agent ended on
    earlier = "Why was this failing? A typo.\n\nI fixed it in src/a.py."
    told = _corrected("No, that's the wrong fix", earlier).edit(A, "new", "newer")
    (fix,) = detect(told, tmp_path)
    assert fix.signal == signals.USER_CORRECTION


def test_a_question_tool_makes_the_reply_an_answer(tmp_path: Path) -> None:
    s = Session().user("add the feature")
    s.edit(A).tool("AskUserQuestion", {"questions": [{"question": "Which one?"}]}, "shown")
    s.user("No, the second one").edit(A, "new", "newer")
    assert detect(s, tmp_path) == []

    rows = [
        {"session": "g", "kind": "user_message", "text": "add it", "cwd": "/repo"},
        {"session": "g", "kind": "tool_call", "id": 1, "op": "edit", "path": "src/a.py"},
        {"session": "g", "kind": "tool_result", "id": 1, "ok": True},
        {"session": "g", "kind": "tool_call", "id": 2, "op": "other", "tool": "ask_user"},
        {"session": "g", "kind": "tool_result", "id": 2, "ok": True},
        {"session": "g", "kind": "user_message", "text": "No, the other module"},
    ]
    path = tmp_path / "g.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert detect(path, tmp_path) == []


def test_polling_until_something_is_ready_is_not_friction(tmp_path: Path) -> None:
    s = Session().user("wait for CI then merge")
    for _ in range(4):
        s.bash("gh pr checks 12", "build  pending", exit_code=8).bash("sleep 30")
    s.bash("gh pr checks 12", "build  pass")
    s.bash("curl -sf localhost:8000/health", "", exit_code=7)
    s.bash("curl -sf localhost:8000/health", "", exit_code=7)
    s.bash("curl -sf localhost:8000/health", "ok")
    assert detect(s, tmp_path) == []


def test_a_loop_that_never_passes_or_needed_a_fix_is_still_friction(tmp_path: Path) -> None:
    stuck = Session().user("go")
    for _ in range(3):
        stuck.bash("make deploy-check", "build  fail", exit_code=8)
    assert len(by_signal(detect(stuck, tmp_path), signals.RETRY_LOOP)) == 1

    installed = Session().user("go")
    for _ in range(3):
        installed.bash("npm test", "sh: 1: jest: not found", exit_code=127)
    installed.bash("npm ci").bash("npm test", "ok")
    found = detect(installed, tmp_path)
    assert len(by_signal(found, signals.RETRY_LOOP)) == 1
    assert len(by_signal(found, signals.COMMAND_FAILURE)) == 3

    flaky = Session().user("go")  # one red run, then green with nothing changed: on record
    flaky.bash("pytest -q", "1 failed", exit_code=1).bash("pytest -q", "1 passed")
    assert [f.signal for f in detect(flaky, tmp_path)] == [signals.COMMAND_FAILURE]


def test_test_first_missing_module_is_red_then_green(tmp_path: Path) -> None:
    missing = "E   ModuleNotFoundError: No module named 'app.newfeat'"
    s = Session().user("add newfeat, test first")
    s.write("/repo/tests/test_newfeat.py", "from app.newfeat import f\n")
    s.bash("pytest tests/test_newfeat.py", missing, exit_code=2)
    s.write("/repo/app/newfeat.py", "def f(): ...\n")
    s.bash("pytest tests/test_newfeat.py", "1 passed")
    assert detect(s, tmp_path) == []

    # a missing dependency is not created in the repo: that stays a setup error
    dep = Session().user("go")
    dep.bash("pytest -q", "E   ModuleNotFoundError: No module named 'yaml'", exit_code=2)
    dep.write("/repo/app/newfeat.py", "x = 1\n").bash("pip install pyyaml").bash("pytest -q", "ok")
    (failure,) = detect(dep, tmp_path)
    assert failure.detail["variant"] == "invocation" and failure.weight == 1.0

    # created, but the command never went green
    red = Session().user("go")
    red.bash("pytest -q", missing, exit_code=2).write("/repo/app/newfeat.py", "x = 1\n")
    assert len(detect(red, tmp_path)) == 1


@pytest.mark.parametrize(
    ("kind", "name", "path", "expected"),
    [
        ("module", "app.newfeat", "app/newfeat.py", True),
        ("module", "app.newfeat", "src/app/newfeat/__init__.py", True),
        ("module", "newfeat", "app/newfeat_test.py", False),
        ("module", "yaml", "app/newfeat.py", False),
        ("file", "tests/test_x.py", "tests/test_x.py", True),
        ("file", "/repo/scripts/run.py", "scripts/run.py", True),
        ("file", "./lib/util", "src/lib/util.js", True),
        ("file", "run.py", "scripts/other.py", False),
    ],
)
def test_provides(kind: str, name: str, path: str, expected: bool) -> None:
    assert signals.provides(path, kind, name) is expected


def test_a_temporary_debug_print_is_not_an_edit_revert(tmp_path: Path) -> None:
    code, debug = "    total = add(a, b)", "    total = add(a, b)\n    print('DEBUG', total)"
    s = Session().user("why is the total off?")
    s.edit(A, code, debug).bash("python -m app.report", "DEBUG 41").edit(A, debug, code)
    assert detect(s, tmp_path) == []

    unused = Session().user("go")  # added and removed without ever being run
    unused.edit(A, code, debug).edit(A, debug, code)
    assert [f.signal for f in detect(unused, tmp_path)] == [signals.EDIT_REVERT]

    replaced = Session().user("go")  # a real change tried, run, and taken back
    replaced.edit(A, "x = 1", "x = 2").bash("python -m app.report", "ok").edit(A, "x = 2", "x = 1")
    assert [f.signal for f in detect(replaced, tmp_path)] == [signals.EDIT_REVERT]


def test_a_denied_chain_is_keyed_to_its_destructive_step(tmp_path: Path) -> None:
    command = "ls build; rm -f build/out.bin"
    s = Session().user("go")
    s.tool(
        "Bash",
        {"command": command},
        f"Permission to use Bash with command {command} has been denied.",
        is_error=True,
    )
    (denied,) = detect(s, tmp_path)
    assert denied.signal == signals.PERMISSION_DENIED and keys(denied) == [("command", "rm")]


def test_reading_a_file_before_creating_it_is_not_a_path_miss(tmp_path: Path) -> None:
    s = Session().user("add a changelog")
    s.read("/repo/CHANGELOG.md", missing=True).write("/repo/CHANGELOG.md", "# Changelog\n")
    assert detect(s, tmp_path) == []
    never = Session().user("go")
    never.read("/repo/CHANGELOG.md", missing=True).write("/repo/NEWS.md", "# News\n")
    assert [f.signal for f in detect(never, tmp_path)] == [signals.PATH_MISS]


def test_git_commit_no_ops_and_hook_auto_fixes_are_not_friction(tmp_path: Path) -> None:
    s = Session().user("commit it")
    s.bash(
        "git commit -m 'wip'",
        "On branch main\nnothing to commit, working tree clean",
        exit_code=1,
    )
    hook = "ruff-format....Failed\n- hook id: ruff-format\n- files were modified by this hook"
    s.bash("git add -A && git commit -m 'feat: x'", hook, exit_code=1)
    s.bash("git add -A && git commit -m 'feat: x'", "[main 1a2b3c4] feat: x")
    assert detect(s, tmp_path) == []

    rejected = Session().user("commit it")  # a hook that just says no stays on the record
    rejected.bash("git commit -m 'x'", "mypy....Failed\n- hook id: mypy\nerror: x", exit_code=1)
    rejected.bash("git commit -m 'x'", "[main 1a2b3c4] x")
    assert [f.signal for f in detect(rejected, tmp_path)] == [signals.COMMAND_FAILURE]


def test_inspection_steps_never_win_a_denied_or_declined_chain(tmp_path: Path) -> None:
    command = (
        "ls -la /repo/out/ 2>&1; rm -f /repo/out/x.html && rmdir /repo/out 2>&1; ls /repo 2>&1"
    )
    said = (
        "Permission to use Bash has been denied. This Bash command contains multiple "
        "operations. The following part requires approval: "
    )
    s = Session().user("go")
    # the harness re-renders the command, so its quotation is not byte-identical
    s.tool("Bash", {"command": command}, said + command.replace(" 2>&1", ""), is_error=True)
    s.reject("Bash", {"command": command}, "do not ls and rm in one go")
    denied, declined = detect(s, tmp_path)
    assert denied.signal == signals.PERMISSION_DENIED and keys(denied) == [("command", "rm")]
    assert declined.signal == signals.TOOL_REJECTED and keys(declined) == [("command", "rm")]


# ---- round 6 ------------------------------------------------------------------------------


def test_a_wish_is_not_a_reproach() -> None:
    confidence, cues = correction_confidence("We should have a retry here, can you add it?")
    assert (confidence, cues) == (0.0, [])
    assert "shouldn't" in correction_confidence("You should have used the client instead")[1]


def test_probable_corrections_are_a_signal_of_their_own(tmp_path: Path) -> None:
    s = Session().user("implement the step")
    s.edit(A).bash("pytest", "1 passed").say("Done.")
    s.user("Actually we should have a retry here instead, can you add it")
    s.write("/repo/src/helper.py", "x = 1\n")
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.PROBABLE_CORRECTION and found.weight == 1.0
    assert found.summary.startswith("Probable correction (confidence 0.4; matched ")
    assert signals.SEVERITY[signals.PROBABLE_CORRECTION] < signals.SEVERITY[signals.EDIT_MISS]


def test_a_productive_sitting_is_not_thrash(tmp_path: Path) -> None:
    s = Session().user("implement the feature")
    s.grep("feature_x", "/repo/src").grep("FeatureXHandler", "/repo/src").read(A)
    for k in range(8):
        s.edit(A, f"a{k}", f"b{k}")
    s.bash("pytest", "8 passed").user("great, thanks")
    assert {f.signal for f in detect(s, tmp_path)} == {signals.SEARCH_MISS}


def test_a_search_that_finds_the_thing_elsewhere_in_the_turn_is_not_a_miss(
    tmp_path: Path,
) -> None:
    s = Session().user("where is the refund policy?")
    s.grep("RefundPolicy", "/repo/src/api").grep("refundpolicy", "/repo/src/models")
    s.grep("RefundPolicy", "/repo/src", found=True)
    assert detect(s, tmp_path) == []
    later = Session().user("where is it?").grep("RefundPolicy", "/repo/src/api")
    later.user("try again").grep("RefundPolicy", "/repo/src", found=True)
    assert len(detect(later, tmp_path)) == 1  # found only after the human stepped in


PYTEST_TEST_ERROR = """=== FAILURES ===
    def test_x():
>       import_module("plugin_x")
E       ModuleNotFoundError: No module named 'plugin_x'
tests/test_app.py:5: ModuleNotFoundError
FAILED tests/test_app.py::test_x - ModuleNotFoundError: No module named 'plugin_x'
1 failed in 0.1s"""
PYTEST_COLLECTION_ERROR = """==== ERRORS ====
____ ERROR collecting tests/test_app.py ____
ImportError while importing test module '/repo/tests/test_app.py'.
tests/test_app.py:1: in <module>
    from src.utlis import helper
E   ModuleNotFoundError: No module named '{module}'
=========== short test summary info ===========
ERROR tests/test_app.py
!!!!!!! Interrupted: 1 error during collection !!!!!!!
1 error in 0.10s"""


def test_errors_quoted_from_inside_a_test_are_not_setup_errors() -> None:
    assert not signals.is_invocation_error(PYTEST_TEST_ERROR, 1)
    quoted = "E   AssertionError: assert 'bash: x: command not found' in out\n1 failed"
    assert not signals.is_invocation_error(quoted, 1)
    traceback = 'Traceback (most recent call last):\n  File "curl: (7) x"\nValueError: bad'
    assert not signals.is_invocation_error(traceback, 1)
    typo, dependency = (PYTEST_COLLECTION_ERROR.format(module=m) for m in ("src.utlis", "yaml"))
    assert signals.is_invocation_error(typo, 2)  # nothing known about the repo: a guess
    assert not signals.is_invocation_error(typo, 2, lambda top: top == "src")
    assert signals.is_invocation_error(dependency, 2, lambda top: top == "src")
    script = "Traceback (most recent call last):\nModuleNotFoundError: No module named 'src.x'"
    assert signals.is_invocation_error(script, 1)
    assert not signals.is_invocation_error(script, 1, lambda top: top == "src")


def test_a_typo_in_a_first_party_import_is_red_fix_green(tmp_path: Path) -> None:
    typo = PYTEST_COLLECTION_ERROR.format(module="src.utlis")
    s = Session().user("fix the test")
    s.read(A).bash("pytest", typo, exit_code=2).edit(A, "utlis", "utils").bash("pytest", "1 passed")
    assert detect(s, tmp_path) == []
    s = Session().user("fix the test")
    s.read(A).bash("pytest tests/test_app.py", PYTEST_TEST_ERROR, exit_code=1)
    s.edit(A).bash("pytest tests/test_app.py", "1 passed")
    assert detect(s, tmp_path) == []
    dep = Session().user("run the tests")
    dep.read(A).bash("pytest", PYTEST_COLLECTION_ERROR.format(module="yaml"), exit_code=2)
    (failure,) = detect(dep, tmp_path)
    assert failure.detail["variant"] == "invocation"


def test_calls_the_harness_never_started_are_not_failures(tmp_path: Path) -> None:
    def error(text: str) -> str:
        return f"<tool_use_error>{text}</tool_use_error>"

    s = Session().user("go")
    s.tool(
        "Bash",
        {"command": "npm test"},
        error("Cancelled: parallel tool call Read(src/nope.py) errored"),
        is_error=True,
    )
    s.tool(
        "Bash",
        {"command": "npm run lint"},
        error("InputValidationError: Bash failed due to the following issue:\nmissing"),
        is_error=True,
    )
    s.tool("Bash", {"command": "npm run build"}, error("Something else went wrong"), is_error=True)
    s.tool("Read", {"file_path": A}, error("Cancelled: parallel tool call errored"), is_error=True)
    assert detect(s, tmp_path) == []

    blocked = Session().user("go")
    blocked.tool(
        "Bash",
        {"command": "sleep 30 && gh run view"},
        error("Blocked: sleep 30 followed by: gh run view. To wait for a condition, use Monitor"),
        is_error=True,
    )
    (denied,) = detect(blocked, tmp_path)
    assert denied.signal == signals.PERMISSION_DENIED and denied.detail["variant"] == "blocked"
    assert denied.summary == "Bash on `gh run view` was denied by the harness; it did not run"

    # a command that really ran and printed the same words did fail
    ran = Session().user("go").bash("npm test", "Cancelled: parallel tool call", exit_code=1)
    assert [f.signal for f in detect(ran, tmp_path)] == [signals.COMMAND_FAILURE]


def test_hook_feedback_is_not_the_human_speaking(tmp_path: Path) -> None:
    for text in (
        "Stop hook feedback:\n- No, you forgot to run the linter. Revert that.",
        "PostToolUse:Edit hook blocking error from command: No, that is wrong, revert that",
        "PreToolUse:Bash hook error: you broke a.py",
    ):
        s = Session().user("go").edit(A).user(text).edit(A, "new", "newer")
        assert detect(s, tmp_path) == [], text
    human = Session().user("go").edit(A).user("Stop the hook, you broke a.py")
    assert [f.signal for f in detect(human, tmp_path)] == [signals.USER_CORRECTION]


def test_a_read_refused_as_too_large_is_evidence(tmp_path: Path) -> None:
    s = Session().user("go")
    s.tool(
        "Read",
        {"file_path": A},
        "<tool_use_error>File content (31245 tokens) exceeds maximum allowed tokens (25000). "
        "Use offset and limit parameters.</tool_use_error>",
        is_error=True,
    )
    (big,) = detect(s, tmp_path)
    assert big.signal == signals.FILE_TOO_LARGE and keys(big) == [("file", "src/a.py")]


TIMED_OUT = "Command timed out after 2m 0s\n> dev\n> vite\n  Local: http://localhost:5173/"


@pytest.mark.parametrize(
    ("command", "server"),
    [
        ("npm run dev", True),
        ("cd web && pnpm dev", True),
        ("uvicorn app.main:app --reload", True),
        ("flask run", True),
        ("bin/rails s", True),
        ("python manage.py runserver", True),
        ("npx vite", True),
        ("docker compose up", True),
        ("docker compose up -d", False),
        ("npm test", False),
        ("npm run devtools-check", False),
        ("pytest tests/test_server.py", False),
    ],
)
def test_a_foreground_server_that_times_out_is_marked(
    tmp_path: Path, command: str, server: bool
) -> None:
    s = Session().user("start it")
    s.tool("Bash", {"command": command}, TIMED_OUT, is_error=True)
    (failure,) = detect(s, tmp_path)
    assert failure.detail["timeout"] == "1" and ("server" in failure.detail) is server
    assert ("never exits" in failure.summary) is server


def test_a_pipeline_is_keyed_to_the_stage_the_shell_could_not_find(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("pnpm test 2>&1 | tee out.log | tail -20", "bash: pnpm: command not found", exit_code=1)
    s.bash("cat data.json | jqq . | sort", "bash: jqq: command not found", exit_code=127)
    s.bash("make build && frob --x", "sh: 1: frob: not found", exit_code=127)
    first, second, third = detect(s, tmp_path)
    assert keys(first) == [("command", "pnpm test")]
    assert keys(second) == [("command", "jqq")] and keys(third) == [("command", "frob")]


# ---- round 7: time, probes, commands run elsewhere ------------------------------------------


def test_a_failed_edit_costs_one_round_trip_once_the_agent_moves_on(tmp_path: Path) -> None:
    s = Session().user("go").edit(A, error=builders.EDIT_NOT_FOUND)
    for i in range(8):  # unrelated, successful work
        s.edit(B, f"a{i}", f"b{i}").bash("pytest -q", "1 passed", took=30)
    s.edit(A, "x", "y")
    (miss,) = by_signal(detect(s, tmp_path), signals.EDIT_MISS)
    assert miss.interval is not None
    assert miss.interval[1] - miss.interval[0] < 30

    again = Session().user("go").edit(A, error=builders.EDIT_NOT_FOUND)
    again.edit(A, error=builders.EDIT_NOT_FOUND).edit(A, "x", "y")
    first, second = by_signal(detect(again, tmp_path), signals.EDIT_MISS)
    assert first.interval is not None and second.interval is not None
    # consecutive failures on the same file: the first one lasts until the edit lands
    assert first.interval[1] == second.interval[1] > first.interval[0] + 5


@pytest.mark.parametrize(
    "command",
    [
        "cd /tmp/scratch && python repro.py",
        "cd ~/other-project && npm test",
        "cd .. && make lint",
        "cd ../sibling; pytest",
        "git -C /srv/elsewhere pull",
        "make -C ../other lint",
        "npm --prefix /srv/web test",
        "uv --directory /srv/tool run pytest",
        "rm /nonexistent/build/out.html",
    ],
)
def test_commands_run_outside_the_repo_are_not_its_friction(tmp_path: Path, command: str) -> None:
    s = Session().user("go")
    s.bash(command, "sh: 1: thing: not found", exit_code=127)
    s.reject("Bash", {"command": command}, "not there")
    assert detect(s, tmp_path) == []


@pytest.mark.parametrize(
    ("command", "key"),
    [
        ("cd src && pytest", "pytest (src/)"),
        ("cd /repo/web && npm test", "npm test (web/)"),
        ("cd /repo/web && git push", "git push"),
        ("cd $WORK && make lint", "make lint"),  # cannot be followed: keep what we knew
        ("git -C /repo push", "git push"),
        ("rm build/out.html", "rm"),
    ],
)
def test_commands_run_inside_the_repo_still_count(tmp_path: Path, command: str, key: str) -> None:
    s = Session().user("go").bash(command, "sh: 1: thing: not found", exit_code=127)
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.COMMAND_FAILURE and found.targets[0].key == key


@pytest.mark.parametrize(
    "command",
    [
        "command -v docker",
        "type rg",
        "hash node",
        "pgrep -f server.py",
        "lsof -i :8000",
        "gh pr view",
        "gh pr checks",
        "pip show requests",
        "npm ls left-pad",
        "git diff --quiet",
        "git diff --exit-code",
        "git merge-base --is-ancestor main HEAD",
        "git check-ignore -q build",
        "git rev-parse --verify feature",
        "test -f config.yml",
        "[ -d node_modules ]",
    ],
)
def test_a_probe_that_answers_no_is_not_a_failure(tmp_path: Path, command: str) -> None:
    s = Session().user("go").bash(command, "", exit_code=1)
    assert detect(s, tmp_path) == []


def test_inline_code_is_ordinary_unless_the_interpreter_is_missing(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("node -e 'process.exit(1)'", "", exit_code=1)
    s.bash("python -c 'import nope'", "ModuleNotFoundError: No module named 'nope'", exit_code=1)
    s.bash("ruby -e 'puts 1'", "sh: 1: ruby: not found", exit_code=127)
    node, python, ruby = detect(s, tmp_path)
    assert [f.targets[0].key for f in (node, python, ruby)] == [
        "node <inline>",
        "python <inline>",
        "ruby <inline>",
    ]
    assert node.detail["variant"] == python.detail["variant"] == "ordinary"
    assert ruby.detail["variant"] != "ordinary"


def test_thrash_needs_the_trouble_on_the_same_file(tmp_path: Path) -> None:
    s = Session().user("go")
    for i in range(7):
        s.edit(A, f"old{i}", f"new{i}")
    s.edit(B, error=builders.EDIT_NOT_FOUND)  # a miss on another file
    assert {f.signal for f in detect(s, tmp_path)} == {signals.EDIT_MISS}


def test_a_revert_of_an_earlier_turns_edit_costs_one_round_trip(tmp_path: Path) -> None:
    s = Session().user("go").edit(A, "one", "two").tick(60).edit(A, "two", "one")
    (same,) = by_signal(detect(s, tmp_path), signals.EDIT_REVERT)
    assert same.interval is not None and same.interval[1] - same.interval[0] > 60
    later = Session().user("go").edit(A, "one", "two")
    for i in range(6):
        later.edit(B, f"a{i}", f"b{i}").bash("pytest -q", "1 passed", took=30)
    later.user("now the next feature").edit(A, "two", "one")
    (other,) = by_signal(detect(later, tmp_path), signals.EDIT_REVERT)
    assert other.interval is None or other.interval[1] - other.interval[0] < 30


# ---- round 8 --------------------------------------------------------------------------------

NO_RULE = "make: *** No rule to make target 'build'.  Stop."


@pytest.mark.parametrize("command", ["cd repo && make build", "cd /work/repo && make build"])
def test_a_cd_into_the_repo_from_a_parent_directory_counts(tmp_path: Path, command: str) -> None:
    s = Session(cwd="/work").user("go").bash(command, NO_RULE, exit_code=2)
    (found,) = detect(s, tmp_path, repo="/work/repo")
    assert found.signal == signals.COMMAND_FAILURE and keys(found) == [("command", "make build")]
    assert found.detail["variant"] == "invocation"


@pytest.mark.parametrize("command", ["make build", "cd other && make build", "cd $X && make"])
def test_commands_in_a_parent_directory_still_do_not_count(tmp_path: Path, command: str) -> None:
    s = Session(cwd="/work").user("go").bash(command, NO_RULE, exit_code=2)
    assert detect(s, tmp_path, repo="/work/repo") == []


def stuck(rounds: int, *, green: bool = False) -> Session:
    s = Session().user("make the test pass")
    for i in range(rounds):
        s.edit(A, f"a{i}", f"b{i}")
        s.bash("pytest tests/test_app.py", "1 failed", exit_code=1, took=60)
    if green:
        s.edit(A, "last", "fix").bash("pytest tests/test_app.py", "1 passed")
    return s


def test_edit_and_run_rounds_that_never_go_green_do_not_converge(tmp_path: Path) -> None:
    (found,) = by_signal(detect(stuck(8), tmp_path), signals.NO_CONVERGENCE)
    assert keys(found) == [("command", "pytest"), ("file", "src/a.py")]
    assert found.detail["count"] == "8" and "8 rounds" in found.summary
    # only the red runs (60 s each) and the edits between them are claimed
    claimed = sum(end - start for start, end in found.intervals)
    assert found.interval is None and 8 * 60 <= claimed < 8 * 60 + 8 * 10
    long = by_signal(detect(stuck(40), tmp_path), signals.NO_CONVERGENCE)[0]
    capped = sum(end - start for start, end in long.intervals)
    assert capped == pytest.approx(signals.NO_CONVERGENCE_CAP_SECONDS)


def test_rounds_that_converge_or_are_few_are_not_recorded(tmp_path: Path) -> None:
    converged = {f.signal for f in detect(stuck(8, green=True), tmp_path)}
    # red -> fix -> green, eventually: no failures on record, at most a weak thrash note
    assert converged <= {signals.EDIT_THRASH}
    few = detect(stuck(signals.NO_CONVERGENCE_MIN_CYCLES - 1), tmp_path)
    assert by_signal(few, signals.NO_CONVERGENCE) == []
    # re-running without editing is a retry loop, not edit-and-run rounds
    s = Session().user("go").edit(A, "a", "b")
    for _ in range(6):
        s.bash("pytest tests/test_app.py", "1 failed", exit_code=1)
    assert by_signal(detect(s, tmp_path), signals.NO_CONVERGENCE) == []
    # rounds spread over turns the human steered are not one stretch
    spread = Session().user("go")
    for i in range(6):
        spread.edit(A, f"a{i}", f"b{i}").bash("pytest", "1 failed", exit_code=1)
        spread.user(f"try approach {i}")
    assert by_signal(detect(spread, tmp_path), signals.NO_CONVERGENCE) == []
    # a green run of another command doing the same job ends it too
    other = stuck(5).edit(A, "x", "y").bash("make test", "ok")
    assert by_signal(detect(other, tmp_path), signals.NO_CONVERGENCE) == []


@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE.md",
        "AGENTS.md",
        ".cursorrules",
        ".env",
        ".env.local",
        "README.md",
        "docs/README",
        "pyproject.toml",
        "setup.cfg",
        ".editorconfig",
        "tsconfig.json",
        "web/package.json",
    ],
)
def test_a_look_for_an_optional_file_is_a_probe(tmp_path: Path, name: str) -> None:
    s = Session().user("go").read(f"/repo/{name}", missing=True)
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.PATH_MISS and found.detail.get("probe") == "1"
    assert scoring.is_weak(found)


def test_a_missing_file_somebody_named_is_not_a_probe(tmp_path: Path) -> None:
    told = Session().user("the settings are in .env.local, read them")
    told.read("/repo/.env.local", missing=True)
    (found,) = detect(told, tmp_path)
    assert "probe" not in found.detail and not scoring.is_weak(found)
    listed = Session().user("go").bash("ls docs", "README.md\nguide.md")
    listed.read("/repo/README.md", missing=True)
    assert "probe" not in detect(listed, tmp_path)[0].detail
    ordinary = Session().user("go").read("/repo/src/models.py", missing=True)
    assert "probe" not in detect(ordinary, tmp_path)[0].detail


def test_a_search_for_text_the_agent_removed_confirms_the_removal(tmp_path: Path) -> None:
    s = Session().user("rename it").edit(A, "legacy_charge(order)", "charge(order)")
    s.grep("legacy_charge", "/repo/src")
    assert detect(s, tmp_path) == []
    kept = Session().user("go").edit(A, "legacy_charge(order)", "legacy_charge(order, retry=1)")
    kept.grep("legacy_charge", "/repo/src")  # still there according to the edit: a real miss
    assert [f.signal for f in detect(kept, tmp_path)] == [signals.SEARCH_MISS]
    before = Session().user("go").grep("legacy_charge", "/repo/src")
    before.edit(A, "legacy_charge(order)", "charge(order)")
    assert [f.signal for f in detect(before, tmp_path)] == [signals.SEARCH_MISS]


@pytest.mark.parametrize("tool", ["ExitPlanMode", "AskUserQuestion", "ask_followup_question"])
def test_declining_a_plan_or_a_question_is_not_a_declined_tool(tmp_path: Path, tool: str) -> None:
    s = Session().user("go").edit(A).reject(tool, {"plan": "do it"}, "not yet, look at B first")
    assert detect(s, tmp_path) == []
    real = (
        Session().user("go").reject("Edit", {"file_path": A, "old_string": "a", "new_string": "b"})
    )
    assert signals.TOOL_REJECTED in {f.signal for f in detect(real, tmp_path)}


@pytest.mark.parametrize(
    "text",
    [
        "same error",
        "Same error as before.",
        "hmm, I'm getting the same error",
        "go back",
        "Go back to the previous version",
        "no no, not that file",
        "you changed the wrong thing",
        "this isn't what I wanted",
    ],
)
def test_round_eight_correction_openers(text: str) -> None:
    assert signals.correction_confidence(text)[0] == signals.CORRECTION_STRONG


@pytest.mark.parametrize(
    "text",
    [
        "I told you about the flag, right?",
        "You removed the flag as asked, now add the tests",
        "Why did you add this? (not complaining, curious)",
        "go back to the dashboard page and add a button",
        "same error handling as in util.py please",
    ],
)
def test_round_eight_not_corrections(text: str) -> None:
    assert signals.correction_confidence(text)[0] == 0.0


# ---- round 9: exit statuses that are not the command's own ----------------------------------

NOT_FOUND = "bash: pytest: command not found"
NO_PYTEST = "/usr/bin/python: No module named pytest"


def test_a_failure_behind_a_pipe_is_judged_from_its_output(tmp_path: Path) -> None:
    s = Session().user("run the tests")
    s.bash("pytest -q 2>&1 | tail -5", NOT_FOUND)  # exit 0: the status of `tail`
    s.bash("python -m pytest -q 2>&1 | tail -5", NO_PYTEST)
    s.bash("make test 2>&1 | tail -5", "5 passed in 0.31s")
    first, second = detect(s, tmp_path)
    assert keys(first) == [("command", "pytest")] and first.detail["variant"] == "invocation"
    assert first.detail["status"] == "hidden" and "judged from its output" in first.summary
    assert first.detail["recovered_by"] == "make test"
    assert keys(second) == [("command", "python -m pytest")]
    assert second.detail["recovered_by"] == "make test"  # never the piped run that printed an error

    plain = Session().user("run the tests").bash("pytest -q", NOT_FOUND, exit_code=127)
    plain.bash("make test", "5 passed in 0.31s")
    (same,) = detect(plain, tmp_path)
    assert same.detail["recovered_by"] == "make test" and "status" not in same.detail


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("pytest 2>&1 | tail -3", "===== 1 failed, 4 passed in 0.52s ====="),
        ("pytest -q | tail -1", "1 failed, 4 passed in 0.52s"),
        ("npm test 2>&1 | tail -6", "Tests:       2 failed, 9 passed, 11 total"),
        ("npx vitest run | tail", " Test Files  1 failed | 3 passed (4)"),
        ("go test ./... 2>&1 | tail -3", "--- FAIL: TestRefund (0.00s)\nFAIL\tshop/pay\t0.012s"),
        ("cargo test 2>&1 | tail -2", "test result: FAILED. 3 passed; 1 failed; 0 ignored"),
        ("make build 2>&1 | tail -3", "make: *** [Makefile:12: build] Error 1"),
        ("ruff check . | head -20", "Found 3 errors."),
        ("npx tsc --noEmit | head", "src/a.ts(3,1): error TS2304: Cannot find name 'x'."),
        (
            "python scripts/gen.py 2>&1 | tail -4",
            "Traceback (most recent call last):\n  ...\nKeyError: 'sku'",
        ),
        ("pytest; echo done", "1 failed in 0.1s\ndone"),
        ("pytest || true", "1 failed in 0.1s"),
        ("pytest -q 2>&1 | tee out.log", "1 failed in 0.1s"),
    ],
)
def test_red_output_with_a_hidden_status_is_a_failure(
    tmp_path: Path, command: str, output: str
) -> None:
    s = Session().user("go").bash(command, output)
    (found,) = detect(s, tmp_path, files=("scripts/gen.py",))
    assert found.signal == signals.COMMAND_FAILURE and found.detail["status"] == "hidden"


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("pytest 2>&1 | tail -3", "===== 5 passed in 0.52s ====="),
        ("pytest 2>&1 | tail -3", ""),  # no evidence either way
        ("make build 2>&1 | tail -3", "gcc -o app main.c"),
        ("ruff check . | head -20", "All checks passed!"),
        ("pytest && echo ok", "1 failed in 0.1s"),  # exit 0 here really is pytest's
        ("grep -rn 'FAILED tests' logs | tail", "FAILED tests/a.py::test_x"),  # reading a log
    ],
)
def test_other_output_with_a_hidden_status_is_not_a_failure(
    tmp_path: Path, command: str, output: str
) -> None:
    assert detect(Session().user("go").bash(command, output), tmp_path) == []


def test_an_unknown_outcome_closes_nothing(tmp_path: Path) -> None:
    # red, edit, then a piped run that shows nothing: not red -> fix -> green
    s = Session().user("go").bash("pytest", "1 failed in 0.1s", exit_code=1)
    s.edit(A).bash("pytest 2>&1 | tail -1", "")
    assert [f.signal for f in detect(s, tmp_path)] == [signals.COMMAND_FAILURE]
    shown = Session().user("go").bash("pytest", "1 failed in 0.1s", exit_code=1)
    shown.edit(A).bash("pytest 2>&1 | tail -1", "5 passed in 0.2s")
    assert detect(shown, tmp_path) == []  # the output says green: the loop closed
    # a setup error is not "recovered by" a run nobody saw succeed
    blind = Session().user("go").bash("pytest", NOT_FOUND, exit_code=127)
    blind.bash("make test 2>&1 | tail -2", "")
    assert "recovered_by" not in detect(blind, tmp_path)[0].detail
    # ... and rounds that stay unknown do not end a stretch that never went green
    stuck_then_blind = stuck(5).edit(A, "x", "y").bash("pytest tests/test_app.py | tail -1", "")
    assert by_signal(detect(stuck_then_blind, tmp_path), signals.NO_CONVERGENCE)
    # polling: `gh pr checks | tail` showing nothing is not the green that ends the wait
    poll = Session().user("go")
    for _ in range(3):
        poll.bash("make deploy-check", "not ready", exit_code=1)
    poll.bash("make deploy-check | tail -1", "")
    assert signals.RETRY_LOOP in {f.signal for f in detect(poll, tmp_path)}


def test_piped_edit_and_run_rounds_do_not_converge_either(tmp_path: Path) -> None:
    s = Session().user("make the test pass")
    for i in range(5):
        s.edit(A, f"a{i}", f"b{i}")
        s.bash("pytest tests/test_app.py 2>&1 | tail -3", "1 failed in 0.4s", took=30)
    (found,) = by_signal(detect(s, tmp_path), signals.NO_CONVERGENCE)
    assert found.detail["count"] == "5"


def test_a_correction_claims_the_rework_not_the_turn_it_corrects(tmp_path: Path) -> None:
    s = Session().user("build the feature")
    for i in range(20):
        s.edit(A, f"a{i}", f"b{i}").tick(10)
    s.bash("pytest", "20 passed").say("Done.")
    s.user("Not quite - rename the helper in src/a.py")
    s.read(A).edit(
        A,
        "helper",
        "format_price",
    ).bash("pytest", "20 passed")
    (found,) = by_signal(detect(s, tmp_path), signals.USER_CORRECTION)
    assert found.interval is not None
    assert 0 < found.interval[1] - found.interval[0] < 30  # the follow-up, not 20 edits

    alone = Session().user("build it").edit(A).say("Done.")
    alone.user("No, you broke src/a.py")  # names the file; the session ends there
    (last,) = by_signal(detect(alone, tmp_path), signals.USER_CORRECTION)
    assert last.interval is None
    other = Session().user("build it").edit(A).say("Done.")
    other.user("No, you broke src/a.py").read(B)  # no follow-up on the file: one round trip
    (one,) = by_signal(detect(other, tmp_path), signals.USER_CORRECTION)
    assert one.interval is not None and one.interval[1] - one.interval[0] < 30


def test_an_interrupt_claims_the_call_it_cut_short(tmp_path: Path) -> None:
    s = Session().user("go")
    for i in range(10):
        s.edit(A, f"a{i}", f"b{i}").tick(20)
    s.edit(B).interrupt()
    (found,) = by_signal(detect(s, tmp_path), signals.USER_INTERRUPT)
    assert found.interval is not None and found.interval[1] - found.interval[0] < 30


@pytest.mark.parametrize(
    "command",
    ["python3 /tmp/x/r7c.py", "node ~/scratch/probe.js", "bash ../other/run.sh"],
)
def test_a_script_outside_the_repo_is_not_its_command(tmp_path: Path, command: str) -> None:
    s = Session().user("go").bash(command, "Traceback\nKeyError: 1", exit_code=1)
    assert detect(s, tmp_path) == []
    inside = Session().user("go").bash("python3 scripts/r7c.py", "KeyError: 1", exit_code=1)
    assert keys(detect(inside, tmp_path)[0])[0] == ("command", "python scripts/r7c.py")


def test_the_same_command_in_two_packages_is_two_commands(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("cd web && npm test", "sh: 1: jest: not found", exit_code=127)
    s.bash("cd api && npm test", "sh: 1: jest: not found", exit_code=127)
    s.bash("npm test", "sh: 1: jest: not found", exit_code=127)
    s.bash("cd web && git push", "fatal: unable to access", exit_code=128)
    sub = Session("s2", cwd="/repo/web").user("go")
    sub.bash("npm test", "sh: 1: jest: not found", exit_code=127)
    found = [keys(f)[0][1] for f in detect(s, tmp_path) + detect(sub, tmp_path)]
    assert found == [
        "npm test (web/)",
        "npm test (api/)",
        "npm test",
        "git push",
        "npm test (web/)",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Still failing in CI on main before your change too - can you look at app/main.py?",
        "That's broken, but it was already broken on main",
        "Still failing - not caused by you, the runner is down",
        "still failing, not your fault",
    ],
)
def test_a_problem_that_predates_the_agent_is_not_a_correction(text: str) -> None:
    assert signals.correction_confidence(text)[0] == 0.0


# ---- round 10 -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "verdict"),
    [
        ("Found 2 errors (2 fixed, 0 remaining).", "green"),
        ("Found 2 errors (1 fixed, 1 remaining).", "red"),
        ("Found 3 errors.", "red"),
        ("All checks passed!", "green"),
        ("Found 3 errors in 2 files (checked 9 source files)", "red"),
        ("Success: no issues found in 9 source files", "green"),
        ("........  [100%]", "green"),
        ("..F.....  [100%]", None),
        ("compiling...", None),
    ],
)
def test_output_verdicts(output: str, verdict: str | None) -> None:
    assert signals.output_verdict(output) == verdict


def test_each_tool_is_judged_by_its_own_summary_line() -> None:
    mypy_red = "All checks passed!\nFound 3 errors in 2 files (checked 9 source files)"
    assert signals.key_verdict("ruff check", mypy_red) == "green"
    assert signals.key_verdict("mypy", mypy_red) == "red"
    assert signals.key_verdict("uv run pytest", mypy_red) is None
    assert signals.key_verdict("ruff check", "Found 2 errors (2 fixed, 0 remaining).") == "green"
    assert signals.key_verdict("git push", "1 failed in 2s") is None


def test_a_successful_fix_run_in_a_chain_is_not_a_failure(tmp_path: Path) -> None:
    s = Session().user("add the feature")
    for i in range(5):
        s.edit(A, f"a{i}", f"b{i}")
        s.bash(
            "ruff check --fix src | tail -1; pytest -q 2>&1 | tail -1",
            "Found 2 errors (2 fixed, 0 remaining).\n21 passed in 1s",
        )
    assert detect(s, tmp_path) == []
    red = (
        Session()
        .user("go")
        .bash(
            "ruff check src | tail -1; pytest -q 2>&1 | tail -1", "Found 2 errors.\n21 passed in 1s"
        )
    )
    (found,) = detect(red, tmp_path)
    assert keys(found)[0] == ("command", "ruff check") and found.detail["variant"] == "ordinary"


def test_a_green_step_closes_its_own_loop_even_if_another_step_is_red(tmp_path: Path) -> None:
    line = "ruff check . | tail -1; mypy | tail -1; pytest -q | tail -1"
    s = Session().user("go")
    s.bash("ruff check .", "Found 1 error.", exit_code=1).edit(A)
    s.bash(line, "All checks passed!\nSuccess: no issues found\n1 failed, 20 passed in 2s")
    (found,) = detect(s, tmp_path)  # ruff went red -> fix -> green; pytest is the red one
    assert keys(found)[0] == ("command", "pytest") and found.detail["status"] == "hidden"

    stuck_lint = Session().user("go")
    for i in range(5):
        stuck_lint.edit(A, f"a{i}", f"b{i}").bash("ruff check .", "Found 1 error.", exit_code=1)
    stuck_lint.edit(A, "x", "y")
    stuck_lint.bash(line, "All checks passed!\nSuccess: no issues found\n1 failed, 2 passed in 2s")
    assert by_signal(detect(stuck_lint, tmp_path), signals.NO_CONVERGENCE) == []
    # an error that cannot be pinned on a step of such a line is not a failure of its last one
    quoted = (
        Session()
        .user("go")
        .bash(
            "python3 scripts/gen.py | tail -3; mypy | tail -1",
            "bash: frobnicate: command not found\nSuccess: no issues found",
        )
    )
    assert detect(quoted, tmp_path) == []


@pytest.mark.parametrize(
    ("red", "green", "output"),
    [
        ("pytest tests/test_app.py", "make check", "42 passed in 3s"),
        ("npm test", "npm run ci", "done"),
        ("ruff check .", "pre-commit run --all-files", "ruff....Passed"),
        ("pytest -x", "nox -s tests", "ok"),
        ("pytest -x", "pytest -q 2>&1 | tail -3 | head -1", "........  [100%]"),
        ("ruff check .", "./scripts/everything.sh", "All checks passed!"),
    ],
)
def test_a_green_aggregate_or_namesake_ends_the_stretch(
    tmp_path: Path, red: str, green: str, output: str
) -> None:
    s = Session().user("fix the bug")
    for i in range(5):
        s.edit(A, f"a{i}", f"b{i}").bash(red, "1 failed, 41 passed in 3s", exit_code=1)
    s.edit(A, "z", "y").bash(green, output)
    assert by_signal(detect(s, tmp_path), signals.NO_CONVERGENCE) == []


def test_what_does_not_end_a_stretch(tmp_path: Path) -> None:
    for green, output in (("git status", "clean"), ("cargo check", "ok"), ("ls", "x")):
        s = Session().user("fix the bug")
        for i in range(5):
            s.edit(A, f"a{i}", f"b{i}").bash("pytest", "1 failed in 3s", exit_code=1)
        s.edit(A, "z", "y").bash(green, output)
        assert len(by_signal(detect(s, tmp_path), signals.NO_CONVERGENCE)) == 1, green
    # the last run showed neither red nor green: a weak lead that claims no time
    s = Session().user("fix the bug")
    for i in range(5):
        s.edit(A, f"a{i}", f"b{i}").bash("pytest", "1 failed in 3s", exit_code=1)
    s.edit(A, "z", "y").bash("pytest -q 2>&1 | tail -1", "")
    (found,) = by_signal(detect(s, tmp_path), signals.NO_CONVERGENCE)
    assert found.detail["uncertain"] == "1" and found.intervals == () and scoring.is_weak(found)
    assert "neither red nor green" in found.summary


def test_a_pause_does_not_undo_a_corroborated_correction(tmp_path: Path) -> None:
    for message in ("No, that's wrong, revert src/a.py", "No, that's wrong. Use a dict."):
        s = Session().user("go").edit(A, "a", "b").say("done")
        s.tick(3600).user(message).edit(A, "b", "a")
        (found,) = by_signal(detect(s, tmp_path), signals.USER_CORRECTION)
        assert "the agent then changed src/a.py again" in found.summary
        idle = Session().user("go").edit(A, "a", "b").say("done")
        idle.tick(3600).user(message).read(B)  # after the pause, nothing happens to the file
        (weak,) = detect(idle, tmp_path)
        assert weak.signal == signals.PROBABLE_CORRECTION and "not corroborated" in weak.summary
    cues = Session().user("go").edit(A).say("done")
    cues.user("Actually we should use a dict instead")
    assert "weaker cues only" in detect(cues, tmp_path)[0].summary


@pytest.mark.parametrize(
    ("typo", "right", "is_typo"),
    [
        ("npm run biuld", "npm run build", True),
        ("make tset", "make test", True),
        ("make lint", "make test", False),
        ("npm run build", "npm run build", False),
        ("make a", "make b", False),
        ("pytest", "make test", False),
    ],
)
def test_typos(typo: str, right: str, is_typo: bool) -> None:
    assert signals.is_typo_of(typo, right) is is_typo


def test_a_mistyped_command_the_agent_corrected_is_weak(tmp_path: Path) -> None:
    s = (
        Session()
        .user("build")
        .bash("npm run biuld", 'npm error Missing script: "biuld"', exit_code=1)
    )
    s.bash("npm run build", "done")
    (found,) = detect(s, tmp_path)
    assert found.detail["typo_of"] == "npm run build" and scoring.is_weak(found)
    assert "mistyped `npm run build`" in found.summary
    never = (
        Session()
        .user("build")
        .bash("npm run biuld", 'npm error Missing script: "biuld"', exit_code=1)
    )
    (kept,) = detect(never, tmp_path)
    assert "typo_of" not in kept.detail and not scoring.is_weak(kept)


def test_weak_signals_claim_no_time(tmp_path: Path) -> None:
    s = Session().user("go").grep("NoSuchName", "/repo/src").read("/repo/.env", missing=True)
    s.read("/repo/src/models.py", missing=True)
    search, probe, miss = detect(s, tmp_path)
    assert search.interval is None and probe.interval is None and miss.interval is not None


def test_instructions_to_a_subagent_are_turn_boundaries(tmp_path: Path) -> None:
    s = Session(sidechain=True).user("task one: make the test pass")
    for i in range(3):
        s.edit(A, f"a{i}", f"b{i}").bash("pytest", "1 failed in 1s", exit_code=1)
    s.user("task two: now the other test")  # injected by the coordinating agent
    for i in range(3, 6):
        s.edit(A, f"a{i}", f"b{i}").bash("pytest", "1 failed in 1s", exit_code=1)
    assert by_signal(detect(s, tmp_path), signals.NO_CONVERGENCE) == []
