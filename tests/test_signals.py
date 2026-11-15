from __future__ import annotations

import json
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
    assert len(ids) == len(set(ids)) == 14
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
    assert len(found) == 8 + 1  # eight failures plus the retry loop on `pytest -q`
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
        [("command", "cargo test")],
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
    assert fix.weight == signals.CORRECTION_PROBABLE and '"long pause"' in fix.summary


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
    assert fix.signal == signals.USER_CORRECTION and "wrong endpoint" in fix.excerpt

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
    assert [f.signal for f in detect(path, tmp_path)] == [signals.USER_CORRECTION]


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
