from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_friction import scoring, signals
from agent_friction.model import Finding
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
    s.edit(A, error=builders.NOT_READ)
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
    # the same word was searched for again in the turn: the agent wanted a hit
    assert found[0].detail == {"pattern": "Needle", "scope": "src", "wanted": "1"}


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
        ("curl: (3) URL using bad/illegal format or missing URL", 3),
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
    "Don't use requests here, use httpx instead",
    "Please don't use mocks here; use the real db instead",
    "we don't use moment in this codebase, use date-fns",
    "don't use any here, use unknown",
    "again: never commit directly to main",
    "Never edit the generated client by hand",
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
    "Don't call legacy_charge() here, it's deprecated. Use PaymentClient.charge() instead.",
    "Actually, use the factory instead",
    "the result is wrong, it shouldn't include drafts",
    "that helper is deprecated, don't import it",
    "you used the old client again, that's a mistake",
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
            "environment",  # the registry wants a login
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
    assert found.summary.startswith("Wording that could be a correction (confidence 0.4; matched ")
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


# ---- round 11 -----------------------------------------------------------------------------

HEREDOC = "python3 - <<'EOF'\nimport pathlib\npathlib.Path('src/a.py').write_text('x = 1')\nEOF"
SCRIPTED_CHANGES = [
    HEREDOC,
    "python -c \"open('src/a.py', 'w').write('x = 1')\"",
    "node -e \"require('fs').writeFileSync('src/a.js', 'x')\"",
    "python scripts/patch.py",
    "npx jscodeshift -t rename.js src/",
    "comby 'foo(:[a])' 'bar(:[a])' .go",
    "mv src/a.py src/b.py",
    "cp src/a.py src/b.py",
    "rm src/a.py",
    "git mv src/a.py src/b.py",
    "git rm src/a.py",
    "npm install left-pad",
    "uv run ruff format .",
    "./scripts/regenerate.sh",
]
NO_CHANGES = ["cat src/a.py", "git status", "ls -la", "grep -rn foo src", "ruff check .", "mypy"]


@pytest.mark.parametrize("change", SCRIPTED_CHANGES)
def test_a_change_made_by_a_command_is_a_change(tmp_path: Path, change: str) -> None:
    fixed = Session().user("go").bash("npm test", "1 failing", exit_code=1)
    fixed.bash(change, "").bash("npm test", "3 passing")
    found = detect(fixed, tmp_path)
    if change in ("python scripts/patch.py", "./scripts/regenerate.sh"):
        # no file was seen to change: recurrence across sessions decides (see the engine)
        assert [f.detail["fixed_by_command"] for f in found] == [change.removeprefix("./")]
    elif change == "npm install left-pad":
        assert [f.detail["fixed_by_setup"] for f in found] == ["npm install"]
    else:
        assert found == []  # red -> change -> green: the loop working

    loop = Session().user("go")
    for _ in range(3):
        loop.bash("npm test", "1 failing", exit_code=1).bash(change, "")
    found = detect(loop, tmp_path)
    assert by_signal(found, signals.RETRY_LOOP) == []  # each run was a new attempt
    assert len(by_signal(found, signals.COMMAND_FAILURE)) == 3  # ... and still red at the end

    polled = Session().user("go")  # not "waiting for something": something was done
    for _ in range(3):
        polled.bash("curl -sf localhost:3000/health", "", exit_code=7)
    polled.bash(change, "").bash("curl -sf localhost:3000/health", "ok")
    assert len(by_signal(detect(polled, tmp_path), signals.RETRY_LOOP)) == 1


@pytest.mark.parametrize("command", NO_CHANGES)
def test_looking_or_checking_is_not_a_change(tmp_path: Path, command: str) -> None:
    flaky = Session().user("go").bash("npm test", "1 failing", exit_code=1)
    flaky.bash(command, "").bash("npm test", "3 passing")
    assert [f.signal for f in detect(flaky, tmp_path)] == [signals.COMMAND_FAILURE]

    loop = Session().user("go")
    for _ in range(3):
        loop.bash("npm test", "1 failing", exit_code=1).bash(command, "")
    assert len(by_signal(detect(loop, tmp_path), signals.RETRY_LOOP)) == 1

    polled = Session().user("go")
    for _ in range(3):
        polled.bash("curl -sf localhost:3000/health", "", exit_code=7).bash(command, "")
    polled.bash("curl -sf localhost:3000/health", "ok")
    assert detect(polled, tmp_path) == []  # still just waiting


def test_a_possible_change_is_not_an_edit_and_never_its_own_fix(tmp_path: Path) -> None:
    # a script that failed changed nothing we can assume
    failed = Session().user("go")
    for _ in range(3):
        failed.bash("npm test", "1 failing", exit_code=1)
        failed.bash("python scripts/patch.py", "Traceback\nKeyError: 'x'", exit_code=1)
    loops = by_signal(detect(failed, tmp_path), signals.RETRY_LOOP)
    assert ("command", "npm test") in [k for loop in loops for k in keys(loop)]

    # the command that went green is not what fixed it
    again = Session().user("go").bash("python scripts/gen.py", "boom", exit_code=1)
    again.bash("python scripts/gen.py", "ok")
    assert [f.signal for f in detect(again, tmp_path)] == [signals.COMMAND_FAILURE]

    # ... but a script that ran earlier on the same line is
    same_line = Session().user("go").bash("npm test", "1 failing", exit_code=1)
    same_line.bash("python scripts/patch.py && npm test", "3 passing")
    (maybe,) = detect(same_line, tmp_path)
    assert maybe.detail["fixed_by_command"] == "python scripts/patch.py"

    # rounds of "run a script, run the check" are not counted as edit-and-run rounds:
    # nobody saw an edit, so no-convergence is not claimed and no file is named
    rounds = Session().user("go")
    for _ in range(6):
        rounds.bash("python scripts/patch.py", "").bash("npm test", "1 failing", exit_code=1)
    found = detect(rounds, tmp_path)
    assert by_signal(found, signals.NO_CONVERGENCE) == []
    assert all(t.kind == "command" for f in found for t in f.targets)


def test_a_wrapped_formatter_is_an_edit_and_an_aggregate_goes_green_on_a_summary(
    tmp_path: Path,
) -> None:
    s = Session().user("go")
    s.bash("make check 2>&1 | tail -2", "1 file would be reformatted\nmake: *** [lint] Error 1")
    s.bash(
        "uv run ruff format . | tail -1 && make check 2>&1 | tail -2 && git status --short",
        "1 file reformatted, 58 files left unchanged\n....   [100%]\n770 passed in 3.04s\n M a.py",
    )
    assert detect(s, tmp_path) == []
    # a summary another step on the line could have printed proves nothing about `make`
    other = Session().user("go")
    other.bash("make check 2>&1 | tail -2", "1 failed in 1s\nmake: *** [test] Error 1")
    other.edit(A).bash("make check 2>&1 | tail -1; pytest -q tests/test_a.py | tail -1", "1 passed")
    assert [f.signal for f in detect(other, tmp_path)] == [signals.COMMAND_FAILURE]


def test_inline_and_stdin_keys_carry_the_directory_too(tmp_path: Path) -> None:
    s = Session(cwd="/repo/web").user("go")
    s.bash("python3 - <<'EOF'\nraise SystemExit(1)\nEOF", "", exit_code=1)
    s.bash("node -e 'process.exit(1)'", "", exit_code=1)
    found = detect(s, tmp_path)
    assert [keys(f) for f in found] == [
        [("command", "python <stdin> (web/)")],
        [("command", "node <inline> (web/)")],
    ]


# ---- round 12 -----------------------------------------------------------------------------

PG_DOWN = (
    'E   psycopg2.OperationalError: connection to server at "localhost" (127.0.0.1), '
    "port 5432 failed: Connection refused\n12 errors in 1.31s"
)
PLAYWRIGHT = "Error: browserType.launch: Executable doesn't exist at /root/.cache/chrome"
SETUP_FIXES = [
    ("pytest tests/test_a.py", PG_DOWN, "docker compose up -d db", "docker compose up -d"),
    ("npm run build", "Error: boom", "cp .env.example .env", "cp .env.example .env"),
    ("npm run e2e", PLAYWRIGHT, "npx playwright install chromium", "npx playwright install"),
    ("npm run e2e", PLAYWRIGHT, "pip install -r requirements.txt", "pip install"),
    ("pytest", "1 failed in 1s", "make bootstrap", "make bootstrap"),
]


@pytest.mark.parametrize(("command", "output", "fix", "label"), SETUP_FIXES)
def test_a_failure_fixed_by_a_setup_step_is_what_we_are_here_for(
    tmp_path: Path, command: str, output: str, fix: str, label: str
) -> None:
    s = Session().user("go").edit(A).bash(command, output, exit_code=1)
    s.bash(fix, "ok").bash(command, "5 passed")
    (found,) = detect(s, tmp_path)
    assert found.detail["fixed_by_setup"] == label and found.weight == 1.0
    assert not scoring.is_weak(found) and found.interval is not None
    assert "passed after a setup step, with no file edited" in found.summary
    assert [t.kind for t in found.targets] == ["command"]  # the test file was not the cause

    # with an edit in between nobody can say what fixed it: the development loop
    for order in ((fix, None), (None, fix)):
        edited = Session().user("go").bash(command, output, exit_code=1)
        for step in order:
            edited = edited.bash(step, "ok") if step else edited.edit(A)
        edited.bash(command, "5 passed")
        assert detect(edited, tmp_path) == []

    # a retry loop is never erased by a later green run, and it learns what fixed it
    loop = Session().user("go")
    for _ in range(4):
        loop.bash(command, output, exit_code=1)
    loop.bash(fix, "ok").bash(command, "5 passed")
    found_loop = detect(loop, tmp_path)
    assert len(by_signal(found_loop, signals.RETRY_LOOP)) == 1
    failures = by_signal(found_loop, signals.COMMAND_FAILURE)
    assert [f.detail.get("fixed_by_setup") for f in failures] == [label] * 4


def test_a_setup_step_going_well_is_not_the_check_going_green(tmp_path: Path) -> None:
    # `npx playwright install` is "a test tool" by name; it must not stand in for the tests
    s = Session().user("go").bash("npm run e2e", PLAYWRIGHT, exit_code=1)
    s.bash("npx playwright install", "ok")
    (found,) = detect(s, tmp_path)
    assert found.detail.get("never_green") == "1" and "fixed_by_setup" not in found.detail
    # version control is never "the fix": the failure is dropped as before
    git = Session().user("go").bash("npm test", "1 failing", exit_code=1)
    git.bash("git commit -m wip", "[main 1a2b3c4] wip").bash("npm test", "3 passing")
    assert detect(git, tmp_path) == []


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (PG_DOWN, ("service", "PostgreSQL")),
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker "
         "daemon running?", ("service", "the Docker daemon")),
        ("Error: connect ECONNREFUSED 127.0.0.1:6379", ("service", "Redis")),
        ("curl: (7) Failed to connect to localhost port 8080: Connection refused",
         ("service", "the service on port 8080")),
        ("django.db.utils.OperationalError: could not connect to server", ("service", "")),
        ("Error: Missing required environment variable DATABASE_URL", ("variable", "DATABASE_URL")),
        ("./run.sh: line 3: API_TOKEN: unbound variable", ("variable", "API_TOKEN")),
        ("KeyError: 'STRIPE_KEY'", ("variable", "STRIPE_KEY")),
        ("Error: AWS credentials expired", ("auth", "")),
        ("error: You must be logged in to the server (Unauthorized)", ("auth", "")),
        ("To get started with GitHub CLI, please run:  gh auth login", ("auth", "")),
        ("FAILED tests/test_a.py::test_x - AssertionError\n1 failed, 3 passed", None),
        ("E   assert response.status_code == 401", None),
        ("KeyError: 'user_id'", None),
        ("error TS2304: Cannot find name 'foo'.", None),
    ],
)  # fmt: skip
def test_environment_errors(output: str, expected: tuple[str, str] | None) -> None:
    assert signals.environment_error(output) == expected


def test_an_environment_failure_counts_in_full_unless_code_fixed_it(tmp_path: Path) -> None:
    s = Session().user("go").edit(A).bash("pytest tests/test_a.py", PG_DOWN, exit_code=1)
    (found,) = detect(s, tmp_path)
    assert found.detail["variant"] == "environment" and found.detail["service"] == "PostgreSQL"
    assert found.weight == 1.0 and found.interval is not None and not scoring.is_weak(found)
    assert found.summary == "`pytest` failed (exit 1; PostgreSQL was not reachable)"
    assert [t.kind for t in found.targets] == ["command"]
    s.edit(A, "localhost", "db").bash("pytest tests/test_a.py", "12 passed")  # wrong host in code
    assert detect(s, tmp_path) == []
    snippet = Session().user("go").bash("python -c 'import redis; redis.Redis().ping()'",
                                        "redis.exceptions.ConnectionError: Connection refused",
                                        exit_code=1)  # fmt: skip
    assert detect(snippet, tmp_path)[0].detail["variant"] == "ordinary"  # ad-hoc code


def test_the_same_command_under_another_name_or_in_another_place(tmp_path: Path) -> None:
    s = Session().user("go")
    s.bash("python scripts/gen.py", "bash: python: command not found", exit_code=127)
    s.bash("python3 scripts/gen.py", "generated")
    (found,) = detect(s, tmp_path)
    assert (found.detail["not_found"], found.detail["works_as"]) == ("python", "python3")
    same = Session().user("go")
    same.bash("python scripts/gen.py", "bash: python: command not found", exit_code=127)
    same.bash("source .venv/bin/activate && python scripts/gen.py", "generated")
    assert "works_as" not in detect(same, tmp_path)[0].detail

    wd = Session().user("go")
    wd.bash("npm test", "npm error enoent Could not read package.json", exit_code=254)
    wd.bash("cd web && npm test", "Tests: 3 passed, 3 total")
    assert detect(wd, tmp_path)[0].detail["works_in"] == "web/"
    again = Session().user("go")
    again.bash("npm test", "npm error enoent Could not read package.json", exit_code=254)
    again.bash("npm install", "ok").bash("npm test", "Tests: 3 passed, 3 total")
    assert "works_in" not in detect(again, tmp_path)[0].detail


# ---- round 13 -----------------------------------------------------------------------------

OWN_MODULE = (
    "ImportError while loading conftest '/repo/tests/conftest.py'.\n"
    "tests/conftest.py:1: in <module>\n    from app import x\n"
    "E   ModuleNotFoundError: No module named 'app'"
)
APP = "/repo/app/__init__.py"
WORKING_FORMS = [
    ("make test", None, "recovered_by", "make test"),
    ("python -m pytest", None, "recovered_by", "python -m pytest"),
    ("uv run pytest", None, "recovered_by", "uv run pytest"),
    ("PYTHONPATH=src pytest -q", None, "works_with", "PYTHONPATH=src pytest"),
    ("pytest -q", "pip install -e .", "fixed_by_setup", "pip install"),
]


@pytest.mark.parametrize(("second", "between", "field", "value"), WORKING_FORMS)
@pytest.mark.parametrize("edits_later", [False, True])
def test_a_package_that_was_there_all_along_was_not_what_was_missing(
    tmp_path: Path, second: str, between: str | None, field: str, value: str, edits_later: bool
) -> None:
    s = Session().user("run the tests").bash("pytest -q", OWN_MODULE, exit_code=4)
    if between:
        s.bash(between, "ok")
    s.bash(second, "3 passed in 0.1s")
    if edits_later:  # work on the package afterwards must not erase the failure
        s.read(APP).edit(APP).bash(second, "3 passed in 0.1s")
    (found,) = detect(s, tmp_path, known=("app/__init__.py",))
    assert found.detail[field] == value and found.detail["variant"] == "invocation"
    assert found.weight == 1.0 and not scoring.is_weak(found) and found.interval is not None
    assert "setup/invocation error" in found.summary


def test_an_import_typo_fixed_by_an_edit_is_still_the_development_loop(tmp_path: Path) -> None:
    # the same command goes green after an edit: a bug was fixed, nothing to document
    s = Session().user("go").bash("pytest -q", OWN_MODULE, exit_code=4)
    s.edit("/repo/tests/conftest.py", "from app", "from apps").bash("pytest -q", "3 passed")
    assert detect(s, tmp_path, known=("app/__init__.py",)) == []
    # never resolved: ordinary, as documented
    red = Session().user("go").bash("pytest -q", OWN_MODULE, exit_code=4)
    (left,) = detect(red, tmp_path, known=("app/__init__.py",))
    assert left.detail["variant"] == "ordinary" and left.detail["own_module"] == "1"
    # a variable in front after an edit proves nothing either way: dropped with the edit
    both = Session().user("go").bash("pytest -q", OWN_MODULE, exit_code=4)
    both.edit("/repo/tests/conftest.py").bash("PYTHONPATH=src pytest -q", "3 passed")
    assert detect(both, tmp_path, known=("app/__init__.py",)) == []


def test_test_first_needs_a_file_that_is_created_before_anything_went_green(
    tmp_path: Path,
) -> None:
    missing = "ERROR: file or directory not found: tests/test_new.py"
    new = "/repo/tests/test_new.py"
    first = Session().user("go").bash("pytest tests/test_new.py", missing, exit_code=4)
    first.write(new).bash("pytest tests/test_new.py", "1 passed")
    assert detect(first, tmp_path) == []  # test-first: the file is created, then green

    edited = Session().user("go").read(new).bash("pytest tests/test_new.py", missing, exit_code=4)
    edited.edit(new).bash("pytest tests/test_new.py", "1 passed")
    assert len(detect(edited, tmp_path)) == 1  # it was there: an edit did not supply it

    late = Session().user("go").bash("pytest tests/test_new.py", missing, exit_code=4)
    late.bash("cd api && pytest tests/test_new.py", "1 passed")  # resolved another way
    late.write(new).bash("pytest tests/test_new.py", "1 passed")
    (kept,) = detect(late, tmp_path)
    assert kept.detail["works_in"] == "api/"


def test_an_inspection_command_that_cannot_reach_its_subject_counts(tmp_path: Path) -> None:
    daemon = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock."
    s = Session().user("go").bash("docker ps", daemon, exit_code=1)
    (found,) = detect(s, tmp_path)
    assert found.detail["service"] == "the Docker daemon" and keys(found) == [
        ("command", "docker ps")
    ]
    quiet = Session().user("go").bash("docker ps --filter name=x -q | grep x", "", exit_code=1)
    quiet.bash("grep -rn 'Connection refused' logs/", "", exit_code=1)
    quiet.bash("cat missing.log", "cat: missing.log: No such file or directory", exit_code=1)
    assert detect(quiet, tmp_path) == []


@pytest.mark.parametrize(
    ("message", "voided"),
    [
        ("Not quite what I had in mind but fine. Add a docstring too", True),
        ("no, wait, also add a --verbose flag", True),
        ("That's wrong but good enough for now, moving on", True),
        ("no, wait, that breaks the API. undo it", False),
        ("That's not what I asked for, but fix the test first", False),
    ],
)
def test_a_shrug_or_an_afterthought_is_not_a_correction(message: str, voided: bool) -> None:
    confidence, _ = correction_confidence(message)
    assert (confidence == 0.0) is voided


def test_a_refusal_is_filed_under_the_step_that_cannot_be_undone(tmp_path: Path) -> None:
    command = "cat >> notes.md <<'EOF'\nx\nEOF\ngit add -A && git commit -m wip && git push -f"
    said = "Permission to use Bash with command git has been denied. "
    s = Session().user("go").tool("Bash", {"command": command}, said + command, is_error=True)
    s.reject("Bash", {"command": command})
    denied, declined = detect(s, tmp_path)
    assert keys(denied) == [("command", "git push")] == keys(declined)
    only = "git add -A && git commit -m wip"
    plain = Session().user("go").reject("Bash", {"command": only})
    assert keys(detect(plain, tmp_path)[0]) == [("command", "git commit")]


def test_the_excerpt_of_a_line_of_commands_ends_at_the_step_that_failed(tmp_path: Path) -> None:
    output = (
        "Found 3 errors.\n" + "\n".join(f"ok line {n}" for n in range(400)) + "\n5 passed in 1s"
    )
    s = Session().user("go").bash("ruff check . | tail -1; pytest -q | tail -500", output)
    (found,) = detect(s, tmp_path)
    assert keys(found) == [("command", "ruff check")]
    assert found.excerpt.endswith("Found 3 errors.")


# ---- round 14 ------------------------------------------------------------------------


def test_one_stall_is_not_both_thrash_and_no_convergence(tmp_path: Path) -> None:
    found = detect(stuck(7), tmp_path)
    assert len(by_signal(found, signals.NO_CONVERGENCE)) == 1
    assert by_signal(found, signals.EDIT_THRASH) == []  # the seven edits are the seven rounds
    # ... but a file thrashed in a turn that did not get that far is still thrash
    s = Session().user("go")
    for i in range(7):
        s.edit(A, f"a{i}", f"b{i}")
    s.bash("python scripts/gen.py", "Traceback\nValueError: x", exit_code=1)
    (thrash,) = by_signal(detect(s, tmp_path), signals.EDIT_THRASH)
    assert "checked" not in thrash.detail
    # ... and the event says whether the turn ran a check
    s = Session().user("go")
    for i in range(7):
        s.edit(A, f"a{i}", f"b{i}")
    s.bash("pytest", "1 failed", exit_code=1)
    (thrash,) = by_signal(detect(s, tmp_path), signals.EDIT_THRASH)
    assert thrash.detail["checked"] == "1"


QUOTED_FROM_OTHERS = [
    "Here's the reviewer's comment: 'No, I said to keep the old name'. Do you agree?",
    'Bob wrote: "no, that is wrong, revert it"',
    "The ticket says “this is still broken, undo the change” - can you look into it?",
]


@pytest.mark.parametrize("text", QUOTED_FROM_OTHERS)
def test_a_correction_quoted_from_someone_else_is_not_one(text: str) -> None:
    assert correction_confidence(text) == (0.0, []), text


@pytest.mark.parametrize(
    "text",
    [
        "No, I said to keep the old name",
        'As I said: "no emojis". Wrong again, revert it',
        "No, the reviewer's comment was about the tests, don't touch them",
        "No - the review said nothing about the tests, revert that",
    ],
)
def test_the_humans_own_words_still_count(text: str) -> None:
    assert correction_confidence(text)[0] == signals.CORRECTION_STRONG, text


def test_a_screenshot_with_still_not_right_is_a_correction(tmp_path: Path) -> None:
    text = "[Image #1] still not aligned, wrong color"
    assert correction_confidence(text)[0] == signals.CORRECTION_STRONG
    assert correction_confidence("[Image #1] here is the design") == (0.0, [])
    assert correction_confidence("the sidebar is off by default, add a toggle") == (0.0, [])
    s = Session().user("fix the header").edit(A).user(text).edit(A, "x", "y")
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.USER_CORRECTION and keys(found) == [("file", "src/a.py")]
    # the agent did not go back to the file: only a probable correction
    s = Session().user("fix the header").edit(A).user(text).read(B)
    assert [f.signal for f in detect(s, tmp_path)] == [signals.PROBABLE_CORRECTION]


def test_an_inline_snippet_run_outside_the_repo_is_not_about_it(tmp_path: Path) -> None:
    snippet = "python - <<'EOF'\nimport app\nEOF"
    output = 'Traceback (most recent call last):\n  File "<stdin>", line 1\nValueError: x'
    outside = Session(cwd="/tmp/scratch").user("go").bash(snippet, output, exit_code=1).edit(A)
    assert detect(outside, tmp_path) == []
    inside = Session(cwd="/repo").user("go").bash(snippet, output, exit_code=1).edit(A)
    (found,) = detect(inside, tmp_path)
    assert keys(found) == [("command", "python <stdin>")]


def test_windows_commands_keep_their_paths(tmp_path: Path) -> None:
    home = "C:\\Users\\dev\\shop"
    error = "python: can't open file 'scripts\\gen.py': [Errno 2] No such file or directory"
    s = Session(cwd=home).user("go").bash("python scripts\\gen.py", error, exit_code=2)
    (found,) = detect(s, tmp_path, repo=home)
    assert keys(found) == [("command", "python scripts/gen.py")]
    # a POSIX session keeps POSIX escapes
    s = Session().user("go").bash("python my\\ script.py", error, exit_code=2)
    (found,) = detect(s, tmp_path)
    assert keys(found) == [("command", "python my script.py")]


# ---- round 15 ------------------------------------------------------------------------

CLASSIFIED = [
    (
        "git push",
        "remote: Permission to acme/x.git denied to bot.\nfatal: unable to access "
        "'https://github.com/acme/x.git/': The requested URL returned error: 403",
        ("environment", "auth"),
    ),
    ("git fetch", "remote: Invalid username or password.", ("environment", "auth")),
    (
        "npm run dev",
        "Error: listen EADDRINUSE: address already in use :::3000",
        ("environment", "port"),
    ),
    ("npm run build", "Error: ENOSPC: no space left on device, write", ("environment", "disk")),
    ("yarn test", 'error Command "test" not found.', ("invocation", None)),
    ("pnpm test", "ERR_PNPM_NO_SCRIPT  Missing script: test", ("invocation", None)),
    (
        "npm ci",
        'npm ERR! engine Unsupported engine: wanted: {"node":">=20"} (current: {"node":"18"})',
        ("invocation", None),
    ),
    # ... and what only looks like those stays an ordinary failure
    ("git push", "! [rejected] main -> main (non-fast-forward)", ("ordinary", None)),
    ("yarn test", "error Command failed with exit code 1.", ("ordinary", None)),
    ("npm run build", "error TS2304: Cannot find name 'space'", ("ordinary", None)),
    ("pytest", "E   assert response.status_code == 403\n1 failed", ("ordinary", None)),
]


@pytest.mark.parametrize(("command", "output", "expected"), CLASSIFIED)
def test_more_failures_of_the_surroundings_are_told_apart(
    tmp_path: Path, command: str, output: str, expected: tuple[str, str | None]
) -> None:
    s = Session().user("go").bash(command, output, exit_code=1)
    (found,) = detect(s, tmp_path)
    assert (found.detail["variant"], found.detail.get("environment")) == expected


def test_a_command_after_a_cd_that_failed_never_ran(tmp_path: Path) -> None:
    gone = "/bin/bash: line 1: cd: missing-dir: No such file or directory"
    s = Session().user("go").bash("cd missing-dir && npm test", gone, exit_code=1)
    # no failure of `npm test (missing-dir/)`: what is on record is the guessed directory
    (miss,) = detect(s, tmp_path)
    assert miss.signal == signals.PATH_MISS and keys(miss) == [("file", "missing-dir")]
    # the directory was there: the failure is that of the command in it
    s = Session().user("go").bash("cd web && npm test", "1 failing", exit_code=1)
    (found,) = detect(s, tmp_path)
    assert keys(found) == [("command", "npm test (web/)")]
    # the same words from a command that changed no directory are its own failure
    s = (
        Session()
        .user("go")
        .bash("make build", "sh: cd: out: No such file or directory", exit_code=2)
    )
    assert len(detect(s, tmp_path)) == 1


def test_an_undo_the_human_asked_for_is_not_a_reverted_edit(tmp_path: Path) -> None:
    def session(said: str) -> Session:
        s = Session().user("rename it").edit(A, "a", "b")
        return s.user(said).edit(A, "b", "a")

    (found,) = detect(session("No, revert src/a.py"), tmp_path)
    assert found.signal == signals.USER_CORRECTION  # counted once, as what the human said
    assert detect(session("Please put the old name back"), tmp_path) == []
    # the agent backing out on its own is still a reverted edit
    assert [f.signal for f in detect(session("hmm, the build is slow today"), tmp_path)] == [
        signals.EDIT_REVERT
    ]
    s = Session().user("rename it").edit(A, "a", "b").edit(A, "b", "a")
    assert [f.signal for f in detect(s, tmp_path)] == [signals.EDIT_REVERT]


def test_commands_run_through_a_variable(tmp_path: Path) -> None:
    def key(command: str) -> str:
        s = Session().user("go").bash(command, "Error: boom", exit_code=1)
        (found,) = detect(s, tmp_path)
        return found.targets[0].key

    assert key("A=scripts/x.sh; $A") == "scripts/x.sh"
    assert key("SP=scripts && node ${SP}/shot.js") == "node scripts/shot.js"
    assert key("node $SP/shot.js") == "node <variable>/shot.js"
    assert key("$RUNNER test") == key("$OTHER --all") == "<variable>"


def test_an_interrupted_command_is_named_on_the_event(tmp_path: Path) -> None:
    s = Session().user("go")
    s.tool("Bash", {"command": "npm run e2e"}, builders.INTERRUPT_TOOL, is_error=True)
    s.user(builders.INTERRUPT_TOOL, as_blocks=True)
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.USER_INTERRUPT and found.targets == []
    assert found.detail == {"interrupted": "npm run e2e"}
    s = Session().user("go").read(A).interrupt()
    (found,) = detect(s, tmp_path)
    assert "interrupted" not in found.detail and keys(found) == [("file", "src/a.py")]


# ---- round 16 ------------------------------------------------------------------------


def test_a_miss_the_agent_keeps_chasing_wanted_a_hit(tmp_path: Path) -> None:
    def details(s: Session) -> list[dict[str, str]]:
        found = by_signal(detect(s, tmp_path), signals.SEARCH_MISS)
        return [{k: v for k, v in f.detail.items() if k in ("wanted", "hygiene")} for f in found]

    # reworded and searched again; then a file of that name is tried
    s = Session().user("change the refund policy")
    s.grep("RefundPolicy", "/repo/src").grep("refund_policy", "/repo/src")
    s.read("/repo/src/policy.py", missing=True)
    assert details(s) == [{"wanted": "1"}, {"wanted": "1"}]
    # content with the empty result: on to something else
    s = Session().user("add a feature")
    s.grep("RefundPolicy", "/repo/src").read(A).edit(A)
    assert details(s) == [{}]
    # an unrelated search afterwards is not the same hunt
    s = Session().user("add a feature")
    s.grep("RefundPolicy", "/repo/src").grep("ShippingRate", "/repo/src")
    assert details(s) == [{}, {}]
    # a new turn is a new question
    s = Session().user("add a feature").grep("RefundPolicy", "/repo/src")
    s.user("and now?").grep("refund_policy", "/repo/src")
    assert details(s) == [{}, {}]


@pytest.mark.parametrize(
    "pattern",
    [
        r"console\.log|debugger",
        "TODO|FIXME|XXX",
        "<<<<<<<|>>>>>>>",
        r"it\.only|describe\.only",
        r"pdb|breakpoint\(\)",
        r"print\(",
    ],
)
def test_searching_for_leftovers_is_marked_as_such(tmp_path: Path, pattern: str) -> None:
    s = Session().user("clean up").edit(A).grep(pattern, "/repo/src").grep(pattern, "/repo")
    found = by_signal(detect(s, tmp_path), signals.SEARCH_MISS)
    assert found and all(f.detail.get("hygiene") == "1" for f in found)
    for ordinary in ("TodoList", "debugger_port", "only_admins", "printer", "DataXXL"):
        s = Session().user("go").grep(ordinary, "/repo/src")
        (miss,) = detect(s, tmp_path)
        assert "hygiene" not in miss.detail, ordinary


def test_red_before_the_first_edit_counts_as_a_run(tmp_path: Path) -> None:
    def rounds(command: str, code: int, edits: int) -> list[Finding]:
        s = Session().user("fix it")
        for i in range(edits + 1):
            if i:
                s.edit(A, f"a{i}", f"b{i}")
            s.bash(command, "E  assert 1 == 2\n1 failed in 0.1s", exit_code=code, took=30)
        return by_signal(detect(s, tmp_path), signals.NO_CONVERGENCE)

    # red, edit, red, edit, red, edit, red: four runs, piped or not, blamed on the file
    for command, code in (("pytest -q 2>&1 | tail -5", 0), ("pytest -q", 1)):
        (found,) = rounds(command, code, 3)
        assert keys(found) == [("command", "pytest"), ("file", "src/a.py")]
        assert found.detail["count"] == "4" and "4 runs with edits in between" in found.summary
        assert rounds(command, code, 2) == []  # three runs are not yet a stall


def test_a_time_boxed_start_that_came_up_is_not_a_failure(tmp_path: Path) -> None:
    up = "> dev\n  VITE v5.0 ready in 300 ms\n  Local: http://localhost:5173/"
    for banner in (up, "Server listening on port 3000", "webpack compiled successfully"):
        s = Session().user("go").bash("timeout 10 npm run dev", banner, exit_code=124)
        assert detect(s, tmp_path) == [], banner
    # it never came up, it was not time-boxed, or it ended some other way: a failure
    for command, output, code in (
        ("timeout 10 npm run dev", "> dev\nstarting...", 124),
        ("timeout 10 npm run dev", up, 1),
        ("npm run build", up, 124),
        ("timeout 60 pytest", "collected 3 items", 124),
    ):
        s = Session().user("go").bash(command, output, exit_code=code)
        assert len(detect(s, tmp_path)) == 1, (command, code)


def test_reading_between_green_runs_is_not_a_read_loop(tmp_path: Path) -> None:
    s = Session().user("go")
    for _ in range(5):
        s.read(A).bash("pytest -q", "3 passed")
    assert detect(s, tmp_path) == []
    # between red runs, or with nothing run at all, it still is one
    s = Session().user("go")
    for _ in range(5):
        s.read(A).bash("pytest -q", "1 failed", exit_code=1)
    assert len(by_signal(detect(s, tmp_path), signals.READ_LOOP)) == 1
    s = Session().user("go")
    for _ in range(5):
        s.read(A).bash("git status", "clean")
    assert len(by_signal(detect(s, tmp_path), signals.READ_LOOP)) == 1


def test_a_plain_rerun_that_passes_is_remembered(tmp_path: Path) -> None:
    s = Session().user("go").bash("npm test", "Tests: 1 failed", exit_code=1)
    s.bash("npm test", "Tests: 11 passed")
    (found,) = detect(s, tmp_path)
    assert found.detail["rerun_green"] == "1" and found.detail["variant"] == "ordinary"
    # twice red, then green untouched: not "waiting" where a test suite is concerned
    s = Session().user("go")
    s.bash("npm test", "Tests: 1 failed", exit_code=1).bash("npm test", "1 failed", exit_code=1)
    s.bash("npm test", "Tests: 11 passed")
    assert all(
        f.detail.get("rerun_green") for f in by_signal(detect(s, tmp_path), signals.COMMAND_FAILURE)
    )
    # ... but polling something else still is
    s = Session().user("go")
    for _ in range(3):
        s.bash("gh pr checks 12", "build pending", exit_code=8)
    s.bash("gh pr checks 12", "build pass")
    assert detect(s, tmp_path) == []
    # an edit in between is the development loop
    s = Session().user("go").bash("npm test", "Tests: 1 failed", exit_code=1).edit(A)
    assert detect(s.bash("npm test", "Tests: 11 passed"), tmp_path) == []


# ---- round 17 ------------------------------------------------------------------------

LAUNCHED = "Command running in background with ID: {}"
CONFTEST = "ImportError while loading conftest\nModuleNotFoundError: No module named 'yaml'"


def background(s: Session, command: str, task: str, ended: str | None = None) -> Session:
    s.tool("Bash", {"command": command, "run_in_background": True}, LAUNCHED.format(task))
    if ended is not None:
        s.tool("BashOutput", {"bash_id": task}, ended)
    return s


def test_a_background_launch_is_neither_red_nor_green(tmp_path: Path) -> None:
    # red in the foreground, then only launched again: nothing is known to have passed
    s = Session().user("go").bash("pytest -q", CONFTEST, exit_code=4)
    background(s, "pip install pyyaml", "b0")
    background(s, "pytest -q", "b1")
    (found,) = detect(s, tmp_path)
    assert found.detail["variant"] == "invocation"
    assert not {"fixed_by_setup", "recovered_by", "rerun_green"} & found.detail.keys()
    # the same launches in the foreground would have closed it
    s = Session().user("go").bash("pytest -q", CONFTEST, exit_code=4)
    s.bash("pip install pyyaml", "ok").bash("pytest -q", "3 passed")
    assert detect(s, tmp_path)[0].detail["fixed_by_setup"] == "pip install"


def test_how_a_background_command_ended_is_judged_like_a_foreground_run(tmp_path: Path) -> None:
    failed = "<status>failed</status>\n<exit_code>{}</exit_code>\n<stdout>\n{}\n</stdout>"
    s = Session().user("go")
    background(s, "pytest -q", "b1", failed.format(4, CONFTEST))
    background(s, "npm run dev", "b2", failed.format(1, "Error: listen EADDRINUSE :::3000"))
    background(s, "make build", "b3", "<status>completed</status>\n<exit_code>0</exit_code>")
    first, second = detect(s, tmp_path)
    assert keys(first) == [("command", "pytest")] and first.detail["variant"] == "invocation"
    assert "ModuleNotFoundError" in first.excerpt and first.excerpt.startswith("$ pytest -q")
    assert keys(second) == [("command", "npm run dev")]
    assert second.detail["environment"] == "port"
    # a reported green end is a green run: red, edit, green in the background is the loop
    s = Session().user("go").bash("pytest -q", "1 failed", exit_code=1).edit(A)
    background(s, "pytest -q", "b1", "<status>completed</status>\n<exit_code>0</exit_code>")
    assert detect(s, tmp_path) == []


def test_a_missing_module_the_agent_just_imported_is_its_own_bug(tmp_path: Path) -> None:
    missing = (
        "ImportError while loading conftest\nE   ModuleNotFoundError: No module named 'tenacity'"
    )

    def session(added: str) -> Session:
        s = Session().user("add retries").edit(A, "import os", added)
        return s.bash("pytest -q", missing, exit_code=4)

    (found,) = detect(session("import os\nimport tenacity"), tmp_path)
    assert found.detail["variant"] == "ordinary"
    (found,) = detect(session("import os\nfrom tenacity import retry"), tmp_path)
    assert found.detail["variant"] == "ordinary"
    # an edit that has nothing to do with it leaves the failure what it is: a setup problem
    (found,) = detect(session("import os\nimport json  # tenacity later"), tmp_path)
    assert found.detail["variant"] == "invocation"
    s = Session().user("go").bash("pytest -q", missing, exit_code=4)
    assert detect(s, tmp_path)[0].detail["variant"] == "invocation"


# ---- round 18: self-blame, questions, activation, trailing red runs ----------------------

BLAMES_THE_AGENT = [
    "that doesn't work, I get a KeyError when I run it",
    "Still failing when I run make test",
    "It's still broken after I pulled your change",
    "That didn't work. When I click save nothing happens",
    "you broke the login since I can't sign in anymore",
    "No, that's wrong because we use tabs here",
]
BLAMES_THEMSELVES = [
    "That didn't work because I hadn't pulled. Try again now.",
    "It's not working because my VPN was off.",
    "tests failing again after I merged main",
    "Still failing since we never rebased, sorry",
]


@pytest.mark.parametrize("text", BLAMES_THE_AGENT)
def test_saying_what_the_human_saw_is_not_taking_the_blame(tmp_path: Path, text: str) -> None:
    assert correction_confidence(text)[0] == signals.CORRECTION_STRONG, text
    assert signals.softened_correction(text) == []
    s = Session().user("add the feature").edit(A).say("Done.").user(text).edit(A, "p", "q")
    (fix,) = detect(s, tmp_path)
    assert fix.signal == signals.USER_CORRECTION and keys(fix) == [("file", "src/a.py")]


@pytest.mark.parametrize("text", BLAMES_THEMSELVES)
def test_self_blame_voids_a_correction_unless_the_agent_redoes_its_work(
    tmp_path: Path, text: str
) -> None:
    assert correction_confidence(text) == (0.0, []), text
    assert signals.softened_correction(text), text
    # the agent only runs the tests again: the human was right to blame themselves
    calm = Session().user("add the feature").edit(A).say("Done.").user(text)
    calm.bash("pytest", "1 passed")
    assert detect(calm, tmp_path) == []
    # the agent goes back to the file it had just changed: its work did need redoing
    redo = Session().user("add the feature").edit(A).say("Done.").user(text).edit(A, "p", "q")
    (fix,) = detect(redo, tmp_path)
    assert fix.signal == signals.PROBABLE_CORRECTION and keys(fix) == [("file", "src/a.py")]
    assert fix.detail["corroborated"] == "softened" and fix.excerpt == ""
    assert fix.line == 5 and "named their own action as the cause" in fix.summary
    # another file, or the next prompt's work, is not a response to it
    other = Session().user("add the feature").edit(A).say("Done.").user(text).edit(B)
    other.user("now the docs").edit(A, "p", "q")
    assert detect(other, tmp_path) == []


def test_praise_after_a_phrase_is_never_a_softened_correction(tmp_path: Path) -> None:
    text = "still failing but that's unrelated, ignore it. Add a flag to src/a.py"
    assert signals.softened_correction(text) == []
    s = Session().user("add the feature").edit(A).say("Done.").user(text).edit(A, "p", "q")
    assert detect(s, tmp_path) == []


CLOSERS = [
    "Done. Anything else?",
    "All tests pass. Let me know if you'd like any changes?",
    "I've updated the handler. Is there anything else you'd like me to change or add?",
    "Done.\n\nHow does that look?",
    "What would you like to do next?",
]
OFFERS = [
    "Should I also rename the helper?",
    "Do you want me to add tests?",
    "Use the REST client or the gRPC one?",
    "Which module?",
    "Done.\n\nWant me to refactor util.py? It would take a few minutes.",
    "Shall I proceed?",
]


def test_only_an_offer_or_a_choice_makes_the_reply_an_answer() -> None:
    from agent_friction.adapters.common import asks_question

    assert not any(asks_question(text) for text in CLOSERS)
    assert all(asks_question(text) for text in OFFERS)


@pytest.mark.parametrize("closer", CLOSERS)
def test_a_closing_courtesy_does_not_swallow_a_correction(tmp_path: Path, closer: str) -> None:
    for text in ("wrong. use PaymentClient", "no no no. roll it back", "No, keep the old name"):
        s = Session().user("add the feature").edit(A).say(closer).user(text).edit(A, "p", "q")
        (fix,) = detect(s, tmp_path)
        assert fix.signal == signals.USER_CORRECTION, (closer, text)


def test_wrong_and_roll_it_back_are_corrections_after_any_question(tmp_path: Path) -> None:
    for text in ("wrong. use PaymentClient", "no no no. roll it back", "roll it back"):
        assert correction_confidence(text, after_question=True)[0] == signals.CORRECTION_STRONG
        s = Session().user("add the feature").edit(A).say("Should I also add tests?")
        s.user(text).edit(A, "p", "q")
        (fix,) = detect(s, tmp_path)
        assert fix.signal == signals.USER_CORRECTION, text
    for text in ("No, keep the old name", "Stop. Just commit.", "Not quite, only the unit tests"):
        assert correction_confidence(text, after_question=True) == (0.0, []), text


NO_FLASK = "Traceback\nModuleNotFoundError: No module named 'flask'"


@pytest.mark.parametrize(
    ("line", "step"),
    [
        ("source .venv/bin/activate && pytest", "source .venv/bin/activate"),
        (". .venv/bin/activate && pytest", "source .venv/bin/activate"),
        ("poetry shell && pytest", "poetry shell"),
        ("conda activate shop && pytest", "conda activate shop"),
    ],
)
def test_an_activation_in_front_of_the_green_run_is_the_setup_step(
    tmp_path: Path, line: str, step: str
) -> None:
    s = Session().user("go").bash("pytest", NO_FLASK, exit_code=1).bash(line, "5 passed")
    (failure,) = detect(s, tmp_path)
    assert failure.detail["fixed_by_setup"] == step
    assert failure.summary.endswith("it passed with an environment activated first")


def test_an_activation_is_not_the_fix_when_it_was_there_before_or_code_changed(
    tmp_path: Path,
) -> None:
    line = "source .venv/bin/activate && pytest"
    both = Session().user("go").bash(line, NO_FLASK, exit_code=1).bash(line, "5 passed")
    edited = Session().user("go").bash("pytest", NO_FLASK, exit_code=1).edit(A).bash(line, "ok")
    for s in (both, edited):
        assert all("fixed_by_setup" not in f.detail for f in detect(s, tmp_path))


def test_a_red_run_with_nothing_after_it_is_marked_trailing(tmp_path: Path) -> None:
    last = Session().user("go").bash("pytest", "1 failed", exit_code=1)
    (failure,) = detect(last, tmp_path)
    assert failure.detail["trailing"] == "1"
    # the agent's closing words do not resolve it
    (failure,) = detect(last.say("The test still fails."), tmp_path)
    assert failure.detail["trailing"] == "1"
    for more in (
        Session().user("go").bash("pytest", "1 failed", exit_code=1).read(A),
        Session().user("go").bash("pytest", "1 failed", exit_code=1).user("ok, leave it"),
    ):
        assert all("trailing" not in f.detail for f in detect(more, tmp_path))


def test_a_tool_denied_without_a_target_is_named_once(tmp_path: Path) -> None:
    s = Session().user("go")
    refused = "Claude requested permissions to use WebFetch, but you haven't granted it yet."
    s.tool("WebFetch", {"url": "https://example.com"}, refused, is_error=True)
    s.reject("mcp__tracker__create", {"title": "x"})
    denied, declined = detect(s, tmp_path)
    assert denied.summary == "WebFetch was denied by permission settings; it did not run"
    assert declined.summary == "The user declined mcp__tracker__create"


# ---- round 19 ----------------------------------------------------------------------------

ONE_RED = "FAILED tests/test_x.py::test_a\n1 failed, 40 passed in 2.0s"


@pytest.mark.parametrize(
    ("red", "output", "code", "green"),
    [
        ("pytest", ONE_RED, 1, "pytest tests/test_cart.py"),
        ("npm test", "Tests: 1 failed, 40 passed", 1, "npm test -- src/cart.test.js"),
        ("pytest -k discount", "no tests ran in 0.1s", 5, "pytest tests/test_a.py"),
    ],
)
def test_a_narrower_selection_passing_is_not_a_plain_rerun(
    tmp_path: Path, red: str, output: str, code: int, green: str
) -> None:
    narrowed = Session().user("go").bash(red, output, exit_code=code).bash(green, "5 passed")
    (failure,) = detect(narrowed, tmp_path)
    assert "rerun_green" not in failure.detail and failure.detail["variant"] == "ordinary"
    # the same line again, also after a wait, is one
    for again in (red, f"sleep 2 && {red}"):
        same = Session().user("go").bash(red, output, exit_code=code).bash(again, "41 passed")
        (failure,) = detect(same, tmp_path)
        assert failure.detail["rerun_green"] == "1", again


REFUSED = "curl: (7) Failed to connect to localhost port 3000 after 0 ms: Connection refused"


def test_polling_a_server_that_is_starting_is_waiting(tmp_path: Path) -> None:
    s = Session().user("start the app")
    s.tool("Bash", {"command": "npm run dev", "run_in_background": True}, "started")
    s.bash("curl -s localhost:3000/health", REFUSED, exit_code=7)
    s.bash("sleep 3 && curl -s localhost:3000/health", '{"ok":true}')
    assert detect(s, tmp_path) == []


@pytest.mark.parametrize(
    "output",
    [
        REFUSED,
        "curl: (28) Operation timed out after 5001 milliseconds",
        "curl: (52) Empty reply from server",
        "curl: (56) Recv failure: Connection reset by peer",
    ],
)
def test_a_server_that_never_answers_is_an_environment_failure(tmp_path: Path, output: str) -> None:
    assert not signals.is_invocation_error(output, 7)
    s = Session().user("go").bash("curl -s localhost:3000/health", output, exit_code=7)
    s.bash("pytest", "5 passed")
    (failure,) = detect(s, tmp_path)
    assert failure.detail["variant"] == "environment" and failure.detail["environment"] == "service"
    # another URL answering does not make the first one a wait
    other = Session().user("go").bash("curl -s localhost:3000/health", output, exit_code=7)
    other.bash("curl -s localhost:9999/health", "ok")
    assert len(detect(other, tmp_path)) == 1


def test_a_declined_call_does_not_claim_the_time_the_human_took(tmp_path: Path) -> None:
    s = Session().user("clean up")
    call = s.call("Bash", {"command": "rm -rf build"})
    s.tick(900).result(call, builders.REJECTED + builders.REJECTED_SAID + "keep it", is_error=True)
    s.tick(6).read(A)
    (declined,) = detect(s, tmp_path)
    assert declined.signal == signals.TOOL_REJECTED and declined.interval is not None
    start, end = declined.interval
    assert 0 < end - start <= 10  # from the answer to the agent's next move, not the prompt
    # nothing follows: nothing is claimed
    alone = Session().user("clean up")
    call = alone.call("Bash", {"command": "rm -rf build"})
    alone.tick(900).result(call, builders.REJECTED + builders.REJECTED_STOP, is_error=True)
    (declined,) = detect(alone, tmp_path)
    assert declined.interval is None


POWERSHELL = (
    "python : The term 'python' is not recognized as the name of a cmdlet, function, script "
    "file, or operable program. Check the spelling of the name, or if a path was included, "
    "verify that the path is correct and try again.\nAt line:1 char:1\n+ python -m pytest\n"
    "    + CategoryInfo          : ObjectNotFound: (python:String) [], CommandNotFoundException"
)


def test_powershell_command_not_found_and_the_py_launcher(tmp_path: Path) -> None:
    assert signals.is_invocation_error(POWERSHELL, 1)
    s = Session().user("go").bash("python -m pytest", POWERSHELL, exit_code=1)
    s.bash("py -m pytest", "5 passed in 0.1s")
    (failure,) = detect(s, tmp_path)
    assert failure.detail["variant"] == "invocation" and failure.detail["not_found"] == "python"
    assert failure.detail["works_as"] == "py" and keys(failure) == [("command", "python -m pytest")]
    cmd = "'pnpm' is not recognized as an internal or external command,\noperable program"
    s = Session().user("go").bash("pnpm test", cmd, exit_code=1)
    (failure,) = detect(s, tmp_path)
    assert failure.detail["not_found"] == "pnpm"


def test_another_runner_passing_untouched_is_remembered(tmp_path: Path) -> None:
    s = Session().user("go").bash("yarn test", "Tests: 1 failed", exit_code=1)
    s.bash("pnpm test", "Tests: 5 passed")
    (failure,) = detect(s, tmp_path)
    assert failure.detail["green_as"] == "pnpm test"
    # not after an edit, and not if the first runner works later in the session
    edited = Session().user("go").bash("yarn test", "Tests: 1 failed", exit_code=1).edit(A)
    edited.bash("pnpm test", "Tests: 5 passed")
    later = Session().user("go").bash("yarn test", "Tests: 1 failed", exit_code=1)
    later.bash("pnpm test", "Tests: 5 passed").bash("yarn test", "Tests: 5 passed")
    for other in (edited, later):
        assert all("green_as" not in f.detail for f in detect(other, tmp_path))


def test_a_test_file_that_does_not_exist_is_a_path_miss(tmp_path: Path) -> None:
    missing = "ERROR: file or directory not found: tests/test_new_feature.py\n\nno tests ran"
    s = Session().user("go").bash("pytest tests/test_new_feature.py", missing, exit_code=4)
    (miss,) = detect(s, tmp_path)
    assert miss.signal == signals.PATH_MISS and keys(miss) == [
        ("file", "tests/test_new_feature.py")
    ]
    assert miss.summary == "`pytest` was pointed at tests/test_new_feature.py, which does not exist"
    # test-first: the file is then written, and nothing was missed
    first = Session().user("go").bash("pytest tests/test_new_feature.py", missing, exit_code=4)
    first.write("/repo/tests/test_new_feature.py", "def test_a(): pass\n")
    assert by_signal(detect(first, tmp_path), signals.PATH_MISS) == []


def test_a_flag_that_made_the_command_work_is_named(tmp_path: Path) -> None:
    eresolve = "npm ERR! code ERESOLVE\nnpm ERR! ERESOLVE unable to resolve dependency tree"
    s = Session().user("go").bash("npm install", eresolve, exit_code=1)
    s.bash("npm install --legacy-peer-deps", "added 100 packages")
    (failure,) = detect(s, tmp_path)
    assert failure.detail["works_with"] == "npm install --legacy-peer-deps"
    # another argument is not a flag that fixed it
    s = Session().user("go").bash("npm install", eresolve, exit_code=1)
    s.bash("npm install left-pad", "added 1 package")
    assert all("works_with" not in f.detail for f in detect(s, tmp_path))


def test_stop_plus_what_the_product_does_is_a_request(tmp_path: Path) -> None:
    for text in (
        "Stop showing the banner after the user dismisses it",
        "Stop sending the welcome email twice",
        "stop logging passwords in the audit trail",
    ):
        assert correction_confidence(text) == (0.0, []), text
    for text in ("Stop editing the tests", "stop. use the other client", "Stop doing that"):
        assert correction_confidence(text) == (signals.CORRECTION_STRONG, ["stop."]), text


def test_the_matched_label_names_what_was_said(tmp_path: Path) -> None:
    for text, label in (
        ("that's not what I asked for", "that's not what I …"),
        ("That's wrong, the port is 8080", "that's wrong"),
        ("this is not right", "that's not right"),
        ("wrong. use PaymentClient", "wrong."),
        ("It doesn't work", "that doesn't work"),
        ("roll it back", "roll it back"),
        ("failing again", "failing again"),
    ):
        assert correction_confidence(text)[1][0] == label, text
    s = Session().user("add it").edit(A).say("Done.").user("that's not what I asked for")
    s.edit(A, "p", "q")
    (fix,) = detect(s, tmp_path)
    assert fix.summary.startswith(
        'The user corrected the agent (confidence 1; matched "that\'s not what I …", '
    )


# ---- round 20 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("we don't use moment in this codebase, use date-fns", "we don't use …"),
        ("don't use any here, use unknown", "don't use X, use Y"),
        ("again: never commit directly to main", "never …"),
        ("Again, we never use default exports", "we don't use …"),
        ("again: the header goes on top", "again: …"),
    ],
)
def test_a_rule_stated_in_reply_is_a_correction_once_the_agent_goes_back(
    tmp_path: Path, text: str, label: str
) -> None:
    confidence, labels = correction_confidence(text)
    assert confidence == signals.CORRECTION_STRONG and labels[0] == label
    s = Session().user("add the feature").edit(A).say("Done.").user(text).edit(A, "p", "q")
    (fix,) = detect(s, tmp_path)
    assert fix.signal == signals.USER_CORRECTION and keys(fix) == [("file", "src/a.py")]
    # the agent moves on to something else: wording alone is not a correction
    s = Session().user("add the feature").edit(A).say("Done.").user(text).edit(B)
    assert [f.signal for f in detect(s, tmp_path)] == [signals.PROBABLE_CORRECTION]


@pytest.mark.parametrize(
    "text",
    [
        "never mind, it works now",
        "I have never seen this error before, can you explain it?",
        "Again, thanks for the help!",
        "we don't have a staging environment yet, can you add one?",
        "don't forget to update the changelog",
        "use date-fns for the new picker",
        "Can we never show the banner to admins?",
        "once again, great work",
        "Again, can you also add a --verbose flag?",
    ],
)
def test_rule_cues_leave_ordinary_follow_ups_alone(text: str) -> None:
    assert correction_confidence(text) == (0.0, []), text


def test_network_and_path_forms_of_a_missing_program(tmp_path: Path) -> None:
    for output in (
        "curl: (6) Could not resolve host: registry.example",
        "wget: unable to resolve host address ‘registry.example’",
    ):
        assert not signals.is_invocation_error(output, 6)
        assert signals.environment_error(output) == (signals.ENV_NETWORK, "")
    s = Session().user("go")
    s.bash("zebractl status", "bash: zebractl: command not found", exit_code=127)
    s.bash("./bin/zebractl status", "ok")
    (failure,) = detect(s, tmp_path)
    assert failure.detail["works_as"] == "./bin/zebractl"


def test_the_tests_a_red_run_names_are_kept(tmp_path: Path) -> None:
    red = "FAILED tests/test_legacy.py::test_old - AssertionError\n1 failed, 40 passed in 2.0s"
    s = Session().user("go").bash("pytest", red, exit_code=1)
    (failure,) = detect(s, tmp_path, known=("tests/test_legacy.py",))
    assert failure.detail["failed_tests"] == "tests/test_legacy.py::test_old"
    assert failure.detail["failed_test_files"] == "tests/test_legacy.py"
    go = Session().user("go").bash("go test ./...", "--- FAIL: TestOld (0.00s)\nFAIL", exit_code=1)
    (failure,) = detect(go, tmp_path)
    assert failure.detail["failed_tests"] == "TestOld" and failure.detail["failed_test_files"] == ""


# ---- round 21 ----------------------------------------------------------------------------


def test_a_new_file_refused_by_the_read_first_rule_is_weak(tmp_path: Path) -> None:
    new = Session().user("go").write("/repo/src/new.py", error=builders.NOT_READ)
    (refused,) = detect(new, tmp_path)
    assert refused.signal == signals.STALE_EDIT and refused.detail["variant"] == "unread-write"
    assert scoring.is_weak(refused) and refused.interval is None
    # a file the session had read or changed is the real thing
    known = Session().user("go").read(A).write(A, error=builders.NOT_READ)
    (stale,) = detect(known, tmp_path)
    assert stale.detail["variant"] == "not-read" and not scoring.is_weak(stale)


def test_an_interrupt_after_a_missing_read_is_not_about_that_path(tmp_path: Path) -> None:
    s = Session().user("go").read("/repo/src/nope.py", missing=True).interrupt()
    miss, stop = detect(s, tmp_path)
    assert miss.signal == signals.PATH_MISS and keys(miss) == [("file", "src/nope.py")]
    assert stop.signal == signals.USER_INTERRUPT and stop.targets == []
    assert "a file that does not exist" in stop.summary
    # after a read that worked, the file is what the agent was on
    s = Session().user("go").read(A).interrupt()
    (stop,) = detect(s, tmp_path)
    assert keys(stop) == [("file", "src/a.py")]


def test_a_denied_chain_is_keyed_to_its_first_step(tmp_path: Path) -> None:
    refused = "Claude requested permissions to use Bash, but you haven't granted it yet."
    for line, key in (
        ("cat notes.md && uv run ruff check .", "cat"),
        ("cd api && sleep 2 && npm run build && npm test", "npm run build (api/)"),
        ("npm run build && git push -f origin main", "git push"),  # the destructive step
    ):
        s = Session().user("go").tool("Bash", {"command": line}, refused, is_error=True)
        (denied,) = detect(s, tmp_path)
        assert keys(denied) == [("command", key)], line


# ---- round 22 ------------------------------------------------------------------------------

NPM_RED = "Tests:       1 failed, 40 passed, 41 total\nnpm ERR! Test failed.  See above."
MAKE_RED = "1 failed, 1317 passed in 6.2s\nmake: *** [Makefile:20: test] Error 1"


@pytest.mark.parametrize(
    ("command", "output", "code", "key"),
    [
        # the runner's line (`npm ERR!`, `make: ***`) fits both steps: the summary decides
        ("npm run lint && npm test 2>&1 | tail -5", NPM_RED, 0, "npm test"),
        ("make lint && make test 2>&1 | tail -5", MAKE_RED, 0, "make test"),
        ("make lint && make test", MAKE_RED, 2, "make test"),
        # ... and so does the target or script the runner names
        (
            "make lint && make test 2>&1 | tail -3",
            "make: *** [Makefile:12: lint] Error 1",
            0,
            "make lint",
        ),
        ("make lint && make test", "make: *** [Makefile:12: lint] Error 1", 2, "make lint"),
        (
            "npm run lint && npm test 2>&1 | tail -3",
            "npm ERR! code ELIFECYCLE\nnpm ERR! shop@1.0.0 lint: `eslint .`\nnpm ERR! Exit 1",
            0,
            "npm run lint",
        ),
        (
            "npm run lint && npm test 2>&1 | tail -3",
            "npm error Lifecycle script `test` failed with error:\nnpm error code 1",
            0,
            "npm test",
        ),
        # a step sent to /dev/null printed none of it
        ("npm run build >/dev/null 2>&1; npm test | tail -3", NPM_RED, 0, "npm test"),
        ("npm run build &>/dev/null; npm test | tail -3", NPM_RED, 0, "npm test"),
        ("make examples >/dev/null 2>&1; make check 2>&1 | tail -2", MAKE_RED, 0, "make check"),
        ("make examples > /dev/null 2> /dev/null; make check | tail -2", MAKE_RED, 0, "make check"),
        # only stdout is hidden: the error line may still be the first step's
        (
            "make examples >/dev/null; make check 2>&1 | tail -2",
            "make: *** [Makefile:9: examples] Error 1",
            0,
            "make examples",
        ),
    ],
)
def test_a_red_run_is_pinned_on_the_step_that_printed_it(
    tmp_path: Path, command: str, output: str, code: int, key: str
) -> None:
    s = Session().user("go").bash(command, output, exit_code=code)
    (failure,) = detect(s, tmp_path)
    assert keys(failure)[0] == ("command", key) and "ambiguous_step" not in failure.detail
    if key.split()[-1] not in ("test", "check"):
        return
    # red, an edit, green: the development loop, for the step that was red
    green = "Tests:       41 passed, 41 total" if "npm" in command else "1318 passed in 6.1s"
    loop = Session().user("go").bash(command, output, exit_code=code).edit(A)
    loop.bash(command, green)
    assert detect(loop, tmp_path) == []


def test_a_runner_line_alone_still_blames_the_first_step_it_fits(tmp_path: Path) -> None:
    # nothing names a target and no summary is shown: as before
    s = Session().user("go").bash("make lint && make test", "make: *** Error 1", exit_code=2)
    (failure,) = detect(s, tmp_path)
    assert keys(failure)[0] == ("command", "make lint")
    # two different tools each keep their own verdict
    both = "error: could not compile `shop`\n===== 1 failed, 2 passed in 0.31s ====="
    s = Session().user("go").bash("cargo build; pytest | tail -5", both)
    (failure,) = detect(s, tmp_path)
    assert keys(failure)[0] == ("command", "cargo build")


@pytest.mark.parametrize(
    "output",
    [
        "/bin/bash: line 1: cd: frontend: No such file or directory",
        "bash: cd: frontend: No such file or directory",
        "(eval):cd:1: no such file or directory: frontend",
        "cd:1: no such file or directory: frontend",
        "cd: no such file or directory: frontend",
        "sh: 1: cd: can't cd to frontend",
    ],
)
def test_a_cd_into_a_directory_that_is_not_there_is_a_path_miss(
    tmp_path: Path, output: str
) -> None:
    s = Session().user("go").bash("cd frontend && npm test", output, exit_code=1)
    (miss,) = detect(s, tmp_path)
    assert miss.signal == signals.PATH_MISS and keys(miss) == [("file", "frontend")]
    assert miss.summary == "`cd` was pointed at frontend, which does not exist"
    assert miss.detail == {"variant": "directory"} and miss.interval is not None
    # resolved against where the shell stood
    s = Session().user("go").bash("cd web && cd frontend && npm test", output, exit_code=1)
    (miss,) = detect(s, tmp_path)
    assert keys(miss) == [("file", "web/frontend")]


def test_a_failed_cd_out_of_the_repo_or_into_a_real_directory_is_nothing(
    tmp_path: Path,
) -> None:
    gone = "bash: cd: /srv/elsewhere: No such file or directory"
    s = Session().user("go").bash("cd /srv/elsewhere && npm test", gone, exit_code=1)
    assert detect(s, tmp_path) == []
    # the directory is on disk: the shell stood somewhere else, nobody guessed a path
    s = Session().user("go")
    s.bash("cd frontend && npm test", "bash: cd: frontend: No such file or directory", exit_code=1)
    assert detect(s, tmp_path, dirs=("frontend",)) == []


ENVIRONMENT_VOCABULARY = [
    (
        "mysql -e 'select 1'",
        "ERROR 1045 (28000): Access denied for user 'root'@'localhost'",
        "auth",
    ),
    (
        "docker pull registry.example/x",
        'Error response from daemon: Get "https://registry.example/v2/": unauthorized: '
        "incorrect username or password",
        "auth",
    ),
    ("terraform apply", "Error: No valid credential sources found", "auth"),
    (
        "aws s3 cp a s3://b",
        "An error occurred (InvalidAccessKeyId) when calling the PutObject operation: The AWS "
        "Access Key Id you provided does not exist in our records.",
        "auth",
    ),
    (
        "aws s3 cp a s3://b",
        "An error occurred (ExpiredToken) when calling the PutObject operation: The provided "
        "token has expired.",
        "auth",
    ),
    (
        "aws dynamodb create-table",
        "An error occurred (UnrecognizedClientException): The security token included in the "
        "request is invalid.",
        "auth",
    ),
    ("gh api -X POST repos/a/b/issues", '{"message":"Bad credentials","status":"401"}', "auth"),
    (
        "npm install",
        "npm ERR! code E403\nnpm ERR! 403 403 Forbidden - GET https://npm.pkg.example/@acme%2fui",
        "auth",
    ),
    ("pip install -r requirements.txt", "ERROR: HTTP error 403 while getting https://x", "auth"),
    (
        "kubectl apply -f k8s/",
        "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout",
        "service",
    ),
]


@pytest.mark.parametrize(("command", "output", "kind"), ENVIRONMENT_VOCABULARY)
def test_credentials_and_clusters_in_the_words_of_more_tools(
    tmp_path: Path, command: str, output: str, kind: str
) -> None:
    s = Session().user("go").bash(command, output, exit_code=1)
    (found,) = detect(s, tmp_path)
    assert (found.detail["variant"], found.detail.get("environment")) == ("environment", kind)
    assert signals.environment_error(output) is not None


def test_a_cluster_that_does_not_answer_a_question_is_still_down(tmp_path: Path) -> None:
    timeout = "Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout"
    s = Session().user("go").bash("kubectl get pods", timeout, exit_code=1)
    (found,) = detect(s, tmp_path)
    assert found.detail["service"] == "the Kubernetes cluster"
    assert "the Kubernetes cluster was not reachable" in found.summary


@pytest.mark.parametrize(
    "output",
    [
        "E   AssertionError: expected 403 Forbidden but got 200\n1 failed in 0.2s",
        "FAILED tests/test_auth.py::test_denied - assert 403 == 200\n1 failed in 0.2s",
        "E   AssertionError: 'Access granted for user' != 'ok'\n1 failed in 0.2s",
        "error: test expected status 200\n  got 403\n1 failed in 0.2s",
    ],
)
def test_a_test_about_permissions_is_not_a_credential_problem(tmp_path: Path, output: str) -> None:
    s = Session().user("go").bash("pytest", output, exit_code=1)
    (found,) = detect(s, tmp_path)
    assert found.detail["variant"] == "ordinary" and signals.environment_error(output) is None


def test_a_push_the_remote_turned_down_is_an_ordinary_failure_with_a_name(
    tmp_path: Path,
) -> None:
    rejected = (
        "To github.com:acme/shop.git\n ! [rejected]        main -> main (non-fast-forward)\n"
        "error: failed to push some refs to 'github.com:acme/shop.git'\n"
        "hint: Updates were rejected because the tip of your current branch is behind"
    )
    s = Session().user("go").bash("git push", rejected, exit_code=1)
    (found,) = detect(s, tmp_path)
    assert found.detail["variant"] == "ordinary" and found.detail["push_rejected"] == "1"
    assert found.summary == (
        "`git push` failed (exit 1; the remote branch had commits the local one lacked)"
    )
    assert scoring.is_weak(found)
    # any other failed push is just that
    denied = "remote: Permission to acme/shop.git denied to dev.\nfatal: unable to access"
    s = Session().user("go").bash("git push", denied, exit_code=128)
    (found,) = detect(s, tmp_path)
    assert "push_rejected" not in found.detail and found.detail["variant"] == "environment"


INSTRUCTIONS = [
    ("Don't use time.sleep here, it makes the suite slow", "don't …"),
    ("I don't want a new dependency for this, write it by hand", "I don't want …"),
    ("you're overcomplicating this, a dict is enough", "you're over… this"),
    ("We already have a retry helper in utils, use that instead", "use that instead"),
    ("again, no mocks in these tests", "no … here"),
    ("can you not change the public API? restore the old name", "can you not …"),
]


@pytest.mark.parametrize(("text", "label"), INSTRUCTIONS)
def test_an_instruction_the_agent_acts_on_is_a_correction(
    tmp_path: Path, text: str, label: str
) -> None:
    assert label in signals.instruction_cues(text)
    assert correction_confidence(text)[0] == 0  # the wording alone says nothing
    s = Session().user("go").edit(A, "a", "b").say("done").user(text).edit(A, "b", "c")
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.USER_CORRECTION and keys(found) == [("file", "src/a.py")]
    assert f'matched "{label}"' in found.summary and found.detail["corroborated"] == "followed"
    assert "the agent then changed src/a.py again" in found.summary
    # the agent moves on to another file: an instruction for what comes next, not recorded
    s = Session().user("go").edit(A, "a", "b").say("done").user(text).edit(B, "b", "c")
    assert detect(s, tmp_path) == []
    # nothing had been changed: there is no work it could redirect
    s = Session().user("go").read(A).say("done").user(text).edit(A, "b", "c")
    assert detect(s, tmp_path) == []
    # in reply to a question from the agent it is the answer
    assert signals.instruction_cues(text, after_question=True) == []


def test_dont_use_x_use_y_reads_a_dotted_name(tmp_path: Path) -> None:
    confidence, labels = correction_confidence("Don't use time.sleep. Use the scheduler.")
    assert (confidence, labels) == (1.0, ["don't use X, use Y"])


@pytest.mark.parametrize(
    "text",
    [
        "Don't forget to update the changelog",
        "Don't commit yet",
        "Don't worry about the docs",
        "No tests are needed here, thanks",
        "Now add logging to it",
        "I don't want to bother you but can you also add a flag",
        "use tabs instead of spaces in the new file",
        "use the factory instead",
        "next task: don't use the cache in the importer",
        "Looks great. Don't change anything else",
        "My bad, I don't want the flag after all",
        "restore the database from the backup and run the migration",
        "can you note the public API in the README?",
    ],
)
def test_ordinary_instructions_are_not_cues(tmp_path: Path, text: str) -> None:
    assert signals.instruction_cues(text) == []
    s = Session().user("go").edit(A, "a", "b").say("done").user(text).edit(A, "b", "c")
    assert by_signal(detect(s, tmp_path), signals.USER_CORRECTION) == []


def test_an_instruction_with_weaker_cues_stays_probable_if_nothing_follows(
    tmp_path: Path,
) -> None:
    text = "Don't call legacy_charge() here, it's deprecated. Use the client instead."
    s = Session().user("go").edit(A, "a", "b").say("done").user(text).read(B)
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.PROBABLE_CORRECTION and "weaker cues only" in found.summary
    assert found.excerpt == "" and "quote" not in found.detail
    s = Session().user("go").edit(A, "a", "b").say("done").user(text).edit(A, "b", "c")
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.USER_CORRECTION and 'matched "don\'t …"' in found.summary


def test_red_after_the_agents_own_change_is_marked(tmp_path: Path) -> None:
    red = "FAILED tests/test_a.py::test_old - AssertionError\n1 failed, 40 passed in 2.0s"
    s = (
        Session()
        .user("go")
        .bash("pytest", red, exit_code=1)
        .edit(A)
        .bash("pytest", red, exit_code=1)
    )
    first, second = by_signal(detect(s, tmp_path), signals.COMMAND_FAILURE)
    assert "after_edit" not in first.detail and second.detail["after_edit"] == "1"


# ---- round 23 ------------------------------------------------------------------------------

CONFIG = {"decision": "accept", "source": "config"}
DENIED_BY_SETTINGS = "Claude requested permissions to use Bash, but you haven't granted it yet."


@pytest.mark.parametrize(
    ("decision", "seconds"),
    [(CONFIG, 284), ({"decision": "accept", "source": "user"}, 14), (None, 44)],
)
def test_a_round_trip_never_includes_a_persons_time_at_the_prompt(
    tmp_path: Path, decision: dict[str, str] | None, seconds: float
) -> None:
    s = Session().user("go")
    s.decision = decision
    s.bash("terraform plan", "Error: No valid credential sources found", exit_code=1, took=270)
    s.tick(10).read(A)
    (found,) = detect(s, tmp_path)
    assert found.interval is not None
    assert found.interval[1] - found.interval[0] == pytest.approx(seconds)


def test_a_failed_edit_that_waited_at_a_prompt_claims_what_followed(tmp_path: Path) -> None:
    s = Session().user("go")
    s.decision = {"decision": "accept", "source": "prompt"}
    call = s.call("Edit", {"file_path": A, "old_string": "a", "new_string": "b"})
    s.result(call, builders.EDIT_NOT_FOUND, is_error=True, took=120)
    s.tick(6).read(A)
    (miss,) = detect(s, tmp_path)
    assert miss.interval is not None
    assert miss.interval[1] - miss.interval[0] == pytest.approx(10)  # 6 s + 4 s of thinking


def test_a_message_from_another_agent_is_not_a_human_correction(tmp_path: Path) -> None:
    wrapped = '<teammate-message teammate_id="lead" summary="x">No, that is wrong, revert that.'
    wrapped += "</teammate-message>"
    s = Session().user("go").edit(A, "a", "b").say("done").user(wrapped).edit(A, "b", "a")
    found = detect(s, tmp_path)
    assert by_signal(found, signals.USER_CORRECTION) == []
    assert by_signal(found, signals.PROBABLE_CORRECTION) == []
    # ... but it does start a new turn, like any instruction
    assert [f.signal for f in found] == [signals.EDIT_REVERT]
    # what the human typed next to it is the human's, and the envelope is never quoted
    s = Session().user("go").edit(A, "a", "b").say("done")
    s.user('<agent-message from="peer">status: ok</agent-message>\nNo, that is wrong, revert that.')
    s.edit(A, "b", "a")
    (found_one,) = by_signal(detect(s, tmp_path), signals.USER_CORRECTION)
    assert found_one.detail["quote"] == "No, that is wrong, revert that."


def test_a_correction_about_work_in_another_repository_is_not_ours(tmp_path: Path) -> None:
    wrong = "No, that is wrong, revert that in repoB"
    # started above both repositories: read here, edited next door
    s = Session(cwd="/").user("go").read(A).edit("/repoB/x.py", "a", "b").say("done")
    s.user(wrong).edit("/repoB/x.py", "b", "a")
    assert detect(s, tmp_path) == []
    # the human names a path next door, and nothing was changed here
    s = Session(cwd="/").user("go").read(A).say("done").user("No, that's wrong: fix /repoB/x.py")
    assert detect(s, tmp_path) == []
    # nothing was edited anywhere and nothing outside is named: still on record
    s = Session(cwd="/").user("go").read(A).say("done").user("No, that is wrong.")
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.PROBABLE_CORRECTION and found.targets == []
    # an edit here keeps the correction here, whatever else was touched
    s = Session(cwd="/").user("go").edit(A, "a", "b").edit("/repoB/x.py").say("done")
    s.user("No, that is wrong.").edit(A, "b", "c")
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.USER_CORRECTION and keys(found) == [("file", "src/a.py")]


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("cat > notes.md <<'EOF'\nhi\nEOF", ("file", "notes.md")),
        ("cat >> docs/notes.md <<'EOF'\nhi\nEOF\nuv run ruff check .", ("file", "docs/notes.md")),
        ("echo hi | tee notes.md", ("file", "notes.md")),
        ("cd web && cat > a.txt <<EOF\nx\nEOF", ("file", "web/a.txt")),
        ("cat notes.md", ("command", "cat")),
        ("cat a.txt b.txt > /dev/null", ("command", "cat")),
    ],
)
def test_a_refused_redirected_write_is_about_the_file(
    tmp_path: Path, command: str, target: tuple[str, str]
) -> None:
    s = Session().user("go").tool("Bash", {"command": command}, DENIED_BY_SETTINGS, is_error=True)
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.PERMISSION_DENIED and keys(found) == [target]
    if target[0] == "file":
        assert found.summary.startswith(f"Bash on {target[1]} was denied")
    # a file outside the repository is nobody's here
    away = Session().user("go")
    away.tool("Bash", {"command": "cat > /tmp/x <<EOF\nhi\nEOF"}, DENIED_BY_SETTINGS, is_error=True)
    assert detect(away, tmp_path) == []


@pytest.mark.parametrize(
    ("command", "output", "code", "key", "variant"),
    [
        (
            '( /usr/bin/time -v mytool scan . ; echo "exit $?" ); ls; which time',
            "bash: line 1: /usr/bin/time: No such file or directory\nexit 127\nREADME.md",
            0,
            "time",
            "invocation",
        ),
        ("time -v pytest", "bash: time: command not found", 127, "time", "invocation"),
        ('mytool run; echo "exit $?"', "exit 127", 0, "mytool", "invocation"),
        ('pytest -q; echo "exit $?"', "some output\nexit 1", 0, "pytest", "ordinary"),
        # the wrapper is there and the command behind it failed: the command's failure
        ("time pytest -q", "1 failed in 0.2s\nreal 0m0.2s", 1, "pytest", "ordinary"),
    ],
)
def test_a_missing_wrapper_and_an_echoed_status(
    tmp_path: Path, command: str, output: str, code: int, key: str, variant: str
) -> None:
    s = Session().user("go").bash(command, output, exit_code=code).read(A)
    (found,) = detect(s, tmp_path)
    assert keys(found)[0] == ("command", key) and found.detail["variant"] == variant


def test_an_echoed_zero_is_a_pass(tmp_path: Path) -> None:
    s = Session().user("go").bash('pytest -q; echo "exit $?"', "some output\nexit 0").read(A)
    assert detect(s, tmp_path) == []
    assert signals.echoed_status('make; echo "exit $?"', "exit 2") == 2
    assert signals.echoed_status('make; echo "rc=$?"', "rc=0") == 0
    # no echo of the status in the line, or more than one answer: nothing is read into it
    assert signals.echoed_status("make test", "exit 2") is None
    assert signals.echoed_status('a; echo "exit $?"; b; echo "exit $?"', "exit 1\nexit 0") is None


# ---- round 24 ------------------------------------------------------------------------

TROUBLESHOOTING = (
    "# Troubleshooting\n\n"
    "make: *** No rule to make target 'tset'.  Stop.\n"
    " ERR_PNPM_NO_SCRIPT  Missing script: tset\n"
    "If you see `bash: foo: command not found`, install foo.\n"
)


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("make build; cat docs/troubleshooting.md", "built ok\n" + TROUBLESHOOTING),
        ("pnpm build && cat docs/troubleshooting.md", "built ok\n" + TROUBLESHOOTING),
        ("make build || head -20 docs/troubleshooting.md", TROUBLESHOOTING),
        ("pytest -q; sed -n 1,40p docs/ci.md", "ok\n1 failed in 0.1s is what CI printed"),
        (
            "make build; grep -n rule docs/troubleshooting.md",
            "built\n3:If you see: No rule to make target, check the Makefile.",
        ),
        # words inside a line are not the tool's own error line
        ("make build | tail -3", "see the docs: No rule to make target means a typo"),
        ("pnpm build | tail -3", "docs: ERR_PNPM_NO_SCRIPT means the script is missing"),
    ],
)
def test_a_document_printed_after_a_command_is_not_its_verdict(
    tmp_path: Path, command: str, output: str
) -> None:
    s = Session().user("go").bash(command, output).read(A)
    assert detect(s, tmp_path) == []


@pytest.mark.parametrize(
    ("command", "output", "code", "variant"),
    [
        ("make tset | tail -3", "make: *** No rule to make target 'tset'.  Stop.", 0, "invocation"),
        ("gmake tset", "gmake[1]: *** No rule to make target 'tset'.  Stop.", 2, "invocation"),
        ("pnpm tset | tail", " ERR_PNPM_NO_SCRIPT  Missing script: tset", 0, "invocation"),
        # the file printed is the one the command wrote: that is its own output
        (
            "pytest -q > out.log 2>&1; tail -5 out.log",
            "FAILED tests/test_a.py::test_x - assert 1 == 2\n1 failed in 0.1s",
            0,
            "ordinary",
        ),
        (
            "mytool build > out.log 2>&1; cat out.log",
            "bash: mytool: command not found",
            0,
            "invocation",
        ),
        # a pipe into a reader prints the command's output, not a file
        ("pytest -q 2>&1 | grep -v warning", "1 failed in 0.1s", 0, "ordinary"),
    ],
)
def test_the_tools_own_error_line_still_counts(
    tmp_path: Path, command: str, output: str, code: int, variant: str
) -> None:
    s = Session().user("go").bash(command, output, exit_code=code).read(A)
    (found,) = detect(s, tmp_path)
    assert found.signal == signals.COMMAND_FAILURE and found.detail["variant"] == variant


def test_which_step_owns_the_exit_status() -> None:
    from agent_friction import attribution

    assert attribution.status_is_last_steps("pytest -q; which time")
    assert attribution.status_is_last_steps("( time mytool . ); ls; command -v time")
    assert not attribution.status_is_last_steps("pytest -q && which time")
    assert not attribution.status_is_last_steps("pytest -q | tail")
    assert not attribution.status_is_last_steps("pytest -q")
    assert attribution.prints_another_file("make; cat docs/x.md")
    assert attribution.prints_another_file("make || tail -n 20 docs/x.md")
    assert attribution.prints_another_file("make; grep -n rule docs/x.md")
    assert not attribution.prints_another_file("make > x.log; cat x.log")
    assert not attribution.prints_another_file("make 2>&1 | tee x.log; tail -3 x.log")
    assert not attribution.prints_another_file("make | grep rule")
    assert not attribution.prints_another_file("make; tail -5")
    assert not attribution.prints_another_file("cat docs/x.md")


@pytest.mark.parametrize(
    ("command", "output", "code", "expected"),
    [
        # the exit status is that of the question asked afterwards
        ("mytool scan .; which time", "scanned 3 files", 1, None),
        ("mytool scan .; command -v jq", "scanned 3 files", 1, None),
        ("pytest -q; ls nothere", "3 passed in 0.2s\nls: cannot access 'nothere'", 2, None),
        (
            "( /usr/bin/time -v mytool scan . ); ls; which time",
            "bash: line 1: /usr/bin/time: No such file or directory\nREADME.md",
            1,
            ("time", "invocation", "hidden"),
        ),
        # ... while the output still says what the command before it did
        ("pytest -q; which time", "1 failed in 0.2s", 1, ("pytest", "ordinary", "hidden")),
        # with `&&` the later step never ran: the status is the command's own
        ("pytest -q && which time", "1 failed in 0.2s", 1, ("pytest", "ordinary", "")),
    ],
)
def test_a_failed_question_at_the_end_is_not_the_commands_failure(
    tmp_path: Path, command: str, output: str, code: int, expected: tuple[str, str, str] | None
) -> None:
    s = Session().user("go").bash(command, output, exit_code=code).read(A)
    found = detect(s, tmp_path)
    if expected is None:
        assert found == []
        return
    (one,) = found
    key, variant, status = expected
    assert keys(one)[0] == ("command", key) and one.detail["variant"] == variant
    assert one.detail.get("status", "") == status
    assert ("exit 1" in one.summary) == (not status)


LOOKS = ("ls .venv/bin", "which pytest", "cat pyproject.toml", "git status", "pip list", "ls")


@pytest.mark.parametrize("looks", [0, 2, 6])
def test_looking_around_does_not_use_up_the_look_ahead(tmp_path: Path, looks: int) -> None:
    s = Session().user("go").bash("pytest -q", "bash: pytest: command not found", exit_code=127)
    for command in LOOKS[:looks]:
        s.bash(command, "x")
    s.bash(".venv/bin/pytest -q", "3 passed in 0.1s").read(A)
    (found,) = detect(s, tmp_path)
    assert found.detail["works_as"] == ".venv/bin/pytest"
    # six other jobs in between: whatever passes afterwards is no longer its fix
    far = Session().user("go").bash("pytest -q", "bash: pytest: command not found", exit_code=127)
    for command in ("npm run build", "make docs", "cargo build", "go build ./...", "make x"):
        far.bash(command, "ok")
    far.bash(".venv/bin/pytest -q", "3 passed in 0.1s").read(A)
    (found,) = detect(far, tmp_path)
    assert "works_as" not in found.detail


def test_a_command_that_works_by_its_local_path(tmp_path: Path) -> None:
    conftest = (
        "ImportError while loading conftest '/repo/tests/conftest.py'.\n"
        "E   ModuleNotFoundError: No module named 'requests'"
    )
    s = Session().user("go").bash("pytest -q", conftest, exit_code=4)
    s.bash("ls .venv/bin", "pytest").bash("which pytest", "/usr/bin/pytest")
    s.bash(".venv/bin/pytest -q", "3 passed in 0.1s").read(A)
    (found,) = detect(s, tmp_path)
    assert found.detail["works_with"] == ".venv/bin/pytest"
    # a red test that goes green by another path is a test going green
    red = Session().user("go").bash("pytest -q", "1 failed in 0.2s", exit_code=1)
    red.bash(".venv/bin/pytest -q", "3 passed in 0.1s").read(A)
    assert all("works_with" not in f.detail for f in detect(red, tmp_path))
    # ... and an edit in between means the edit may have fixed it
    edited = Session().user("go").bash("pytest -q", conftest, exit_code=4)
    edited.edit(A).bash(".venv/bin/pytest -q", "3 passed in 0.1s").read(A)
    assert all("works_with" not in f.detail for f in detect(edited, tmp_path))


def powershell_missing(cmdlet: str, path: str) -> str:
    return (
        f"{cmdlet} : Cannot find path '{path}' because it does not exist.\n"
        "At line:1 char:1\n"
        f"    + CategoryInfo          : ObjectNotFound: ({path}:String) [{cmdlet}], "
        "ItemNotFoundException"
    )


def test_powershell_saying_a_path_is_not_there_is_a_path_miss(tmp_path: Path) -> None:
    def one(command: str, output: str, **kw: object) -> Finding:
        s = Session().user("go").bash(command, output, exit_code=1).read(A)
        (found,) = detect(s, tmp_path, **kw)  # type: ignore[arg-type]
        return found

    read = one(
        "Get-Content src/missing.py", powershell_missing("Get-Content", "/repo/src/missing.py")
    )
    assert read.signal == signals.PATH_MISS and keys(read) == [("file", "src/missing.py")]
    assert read.summary == "`Get-Content` was pointed at src/missing.py, which does not exist"
    assert read.detail == {"variant": "command"} and not scoring.is_weak(read)
    # PowerShell 7 prints no space before the colon; an alias is named as typed
    seven = "gc: Cannot find path '/repo/src/missing.py' because it does not exist."
    assert one("gc src/missing.py | Select-Object -First 5", seven).signal == signals.PATH_MISS
    # asking whether something is there is a question, as with `ls`
    listing = one("Get-ChildItem src/nodir", powershell_missing("Get-ChildItem", "/repo/src/nodir"))
    assert listing.detail == {"variant": "command", "probe": "1"} and scoring.is_weak(listing)
    assert listing.summary.endswith("(a look at whether it is there)")
    optional = one("Get-Content README.md", powershell_missing("Get-Content", "/repo/README.md"))
    assert optional.detail.get("probe") == "1"
    moved = one("Set-Location web", powershell_missing("Set-Location", "/repo/web"))
    assert moved.detail == {"variant": "directory"} and keys(moved) == [("file", "web")]
    # a path that is there after all, or outside the repository, is nobody's miss ...
    there = Session().user("go")
    there.bash(
        "Get-Content src/a.py", powershell_missing("Get-Content", "/repo/src/a.py"), exit_code=1
    )
    there.bash("Get-Content /etc/x", powershell_missing("Get-Content", "/etc/x"), exit_code=1)
    assert detect(there.read(A), tmp_path) == []
    # ... and any other error of a cmdlet is still a command failing
    denied = "Get-Content : Access to the path '/repo/src/a.py' is denied."
    assert one("Get-Content src/a.py", denied).signal == signals.COMMAND_FAILURE
