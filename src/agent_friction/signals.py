"""Signal catalog and transcript detectors.

Each detector looks at one :class:`~agent_friction.model.Stream` and emits
:class:`~agent_friction.model.Finding` records. Every threshold is a named
constant here and is explained in docs/signals.md.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from agent_friction import model
from agent_friction.adapters.common import QUESTION_TOOL_RE
from agent_friction.attribution import (
    DESTRUCTIVE_PROGRAMS,
    HOUSEKEEPING_PROGRAMS,
    INTERPRETERS,
    MATCH_FILTERS,
    SETUP_PROGRAMS,
    ParsedCommand,
    PathResolver,
    Step,
    command_role,
    env_copy,
    is_aggregate_check,
    is_inline_code,
    is_replacement,
    is_setup_command,
    is_setup_step,
    is_shell_edit,
    may_change_files,
    normalize_argv,
    objectionable_step,
    parse_command,
    path_candidates,
)
from agent_friction.model import Event, Finding, Stream, Target


@dataclass(frozen=True)
class SignalDef:
    id: str
    title: str
    description: str
    why: str
    attribution: str
    severity: float


EDIT_MISS = "edit-miss"
STALE_EDIT = "stale-edit"
PERMISSION_DENIED = "permission-denied"
PATH_MISS = "path-miss"
EDIT_THRASH = "edit-thrash"
EDIT_REVERT = "edit-revert"
READ_LOOP = "read-loop"
SEARCH_MISS = "search-miss"
COMMAND_FAILURE = "command-failure"
RETRY_LOOP = "retry-loop"
TOOL_REJECTED = "tool-rejected"
USER_INTERRUPT = "user-interrupt"
USER_CORRECTION = "user-correction"
HUMAN_REWORK = "human-rework"
PROBABLE_CORRECTION = "probable-correction"
FILE_TOO_LARGE = "file-too-large"
NO_CONVERGENCE = "no-convergence"

# ---- thresholds (see docs/signals.md) -------------------------------------------------
#: Successful edits to one file within one user turn that count as thrashing.
EDIT_THRASH_MIN_EDITS = 6
#: Identical reads (same file, same range) with no edit in between that count as a loop.
READ_LOOP_MIN_READS = 3
#: Consecutive failures of one normalized command that count as a retry loop.
RETRY_LOOP_MIN_FAILURES = 3
#: Weight of a command failure that does not look like a setup/invocation problem.
ORDINARY_FAILURE_WEIGHT = 0.3
#: Shell calls after a failure in which a different successful command counts as its fix.
RECOVERY_LOOKAHEAD = 5
#: Per-event cap for the time-lost estimate, in seconds.
TIME_CAP_SECONDS = 300.0
#: Only the start of a user message is inspected for correction phrases.
CORRECTION_HEAD_CHARS = 280
#: A correction with no named file is split over at most this many recently edited files.
CORRECTION_MAX_FILES = 5
#: Characters of a user message scanned for file names.
CORRECTION_SCAN_CHARS = 2000
#: Files named in a failing command that are attributed, at most.
COMMAND_MAX_FILES = 5
#: Confidence of a message with an unambiguous correction phrase.
CORRECTION_STRONG = 1.0
#: Confidence of a message with two or more distinct weaker cues. One cue is not enough.
CORRECTION_PROBABLE = 0.4
#: Weaker cues needed for a "probable" correction.
CORRECTION_MIN_CUES = 2
#: A verdict ("No, ...", "that's wrong") typed longer than this after the agent finished
#: is only a probable correction: the human has probably moved on to something else.
CORRECTION_STALE_SECONDS = 300
#: Identical failures in a row that, when the same command line then succeeds with nothing
#: changed in between, are read as waiting (polling CI, a server coming up), not friction.
POLL_MIN_FAILURES = 2
#: Characters of a prompt or tool output scanned for names of optional files.
MENTION_SCAN_CHARS = 20_000
#: Edits whose removed text is remembered, to recognise a search that confirms the removal.
REMOVED_TEXTS_KEPT = 200
#: Edit-then-red-run cycles of one command within one turn, with no green run, that count
#: as work that does not converge.
NO_CONVERGENCE_MIN_CYCLES = 4
#: Cap for the time claimed by one such stretch (first red run to last red run), seconds.
NO_CONVERGENCE_CAP_SECONDS = 900.0
#: Files edited during the cycles that share the event, at most (the most edited first).
NO_CONVERGENCE_MAX_FILES = 3

#: Tools that act on the project in the directory they are run in. Their commands are
#: named with that directory when it is not the repo root (`npm test (web/)`).
DIRECTORY_TOOLS = frozenset(
    {"npm", "pnpm", "yarn", "bun", "npx", "make", "gmake", "just", "task", "cargo", "go", "uv"}
    | {"poetry", "pip", "pytest", "tox", "nox", "gradle", "gradlew", "mvn", "dotnet", "bundle"}
    | {"composer", "mix", "rake", "tsc", "vite", "jest", "vitest", "ruff", "mypy", "eslint"}
    | {"python", "node", "deno", "cmake", "ctest", "bazel", "pre-commit", "terraform"}
)

#: Files that agents look for as a matter of routine, whether or not a project has them:
#: instructions, environment files, READMEs and the usual tool configuration.
PROBE_FILE_RE = re.compile(
    r"(?:^|/)(?:"
    r"CLAUDE(?:\.local)?\.md|AGENTS\.md|GEMINI\.md|\.cursorrules|\.windsurfrules|\.clinerules"
    r"|copilot-instructions\.md|\.env(?:\.[\w.-]+)?|\.envrc"
    r"|(?:README|CONTRIBUTING|CHANGELOG|CHANGES|HISTORY|LICEN[CS]E|SECURITY|TODO|NOTES)"
    r"(?:\.\w+)?"
    r"|Makefile|GNUmakefile|justfile|Taskfile\.ya?ml|Dockerfile|\.dockerignore"
    r"|(?:docker-)?compose(?:\.[\w-]+)?\.ya?ml"
    r"|pyproject\.toml|setup\.(?:py|cfg)|requirements[\w.-]*\.txt|Pipfile|tox\.ini"
    r"|pytest\.ini|conftest\.py|mypy\.ini|\.flake8|\.pylintrc|\.?ruff\.toml|\.coveragerc"
    r"|\.editorconfig|\.gitignore|\.gitattributes|\.pre-commit-config\.ya?ml"
    r"|package\.json|[tj]sconfig(?:\.[\w.-]+)?\.json|\.npmrc|\.nvmrc|\.babelrc"
    r"|\.eslintrc(?:\.\w+)?|\.prettierrc(?:\.\w+)?"
    r"|(?:eslint|prettier|vite|vitest|jest|webpack|rollup|babel|next|tailwind|playwright)"
    r"\.config\.\w+"
    r"|Cargo\.toml|go\.mod|Gemfile|Rakefile|pom\.xml|build\.gradle(?:\.kts)?"
    r"|CMakeLists\.txt|\.python-version|\.tool-versions|\.ruby-version"
    r")$",
    re.I,
)
_FILE_NAME_RE = re.compile(r"[\w.-]*[A-Za-z][\w.-]*")

CATALOG: tuple[SignalDef, ...] = (
    SignalDef(
        USER_CORRECTION,
        "User correction",
        "A human prompt that follows agent work and is worded as a correction of it "
        '("No, …", "I told you …", "revert that", "still failing", "you broke …"), '
        "corroborated by behaviour: the agent had changed files, and the human names one of "
        "them or the agent then changes one of them again. Wording alone, or two weaker cues "
        "together, is only a probable correction.",
        "Human attention is the most expensive resource in the loop; a correction means the "
        "agent's work on these files had to be redirected by hand.",
        "Files the agent touched in the preceding turn that the message names; otherwise "
        f"split evenly over the files it edited in that turn (the {CORRECTION_MAX_FILES} most "
        "recent).",
        4.0,
    ),
    SignalDef(
        RETRY_LOOP,
        "Retry loop",
        f"The same command line failed {RETRY_LOOP_MIN_FAILURES} or more times in a row in "
        "one session with identical arguments, with no success of that command and nothing "
        "changed in between: no edit, and no other command that could have changed files (a "
        "script, an install, a move). Dropped as waiting if the same line then passes with "
        "nothing changed; never dropped because it passed after something was done.",
        "The failure output did not tell the agent what to do, so it kept guessing.",
        "The normalized command.",
        3.0,
    ),
    SignalDef(
        NO_CONVERGENCE,
        "No convergence",
        f"Within one turn the agent ran the same command {NO_CONVERGENCE_MIN_CYCLES} or more "
        "times, each run after a file change since the previous one, every run failed as an "
        "ordinary failure, and the turn ended with the command still failing.",
        "Edit, run, red, again and again without ever going green: the agent could not "
        "work out what the check wants.",
        "The normalized command, and the files edited between the runs (the "
        f"{NO_CONVERGENCE_MAX_FILES} most edited, split evenly).",
        3.0,
    ),
    SignalDef(
        TOOL_REJECTED,
        "Tool call declined",
        "The human declined a tool call the agent proposed. Declining a plan or a question "
        "is not counted.",
        "The agent wanted to do something a human considered wrong or unsafe here.",
        "The file the tool targeted, or the normalized command for shell calls.",
        2.5,
    ),
    SignalDef(
        USER_INTERRUPT,
        "User interrupt",
        "The human interrupted the agent after it had made at least one tool call in the "
        "turn. The interrupt that accompanies a declined tool call is not counted again.",
        "The human saw the agent going wrong and stopped it.",
        "The target of the agent's most recent tool call in that turn.",
        2.5,
    ),
    SignalDef(
        EDIT_MISS,
        "Edit miss",
        "An edit failed because the text to replace was not found in the file, or matched "
        "several places.",
        "The agent's picture of the file was wrong, or the file has repeated blocks that "
        "are hard to target; each miss costs a re-read and a retry.",
        "The file being edited.",
        2.0,
    ),
    SignalDef(
        EDIT_REVERT,
        "Edit reverted",
        "An edit exactly undid an earlier successful edit to the same file in the same "
        "session, or a write restored an earlier version of the file. Taking out lines that "
        "were only added and then run successfully (a debug print) is not counted.",
        "Work on this file went in a circle.",
        "The file being edited.",
        2.0,
    ),
    SignalDef(
        COMMAND_FAILURE,
        "Command failure",
        "A shell command failed: it exited non-zero or, where the exit status belongs to a "
        "pipe or a later step, its own output says so. Setup and invocation errors (command "
        "not found, missing module, bad arguments, …) and environment failures (a service "
        "that is not running, missing credentials, an unset variable) count fully; ordinary "
        f"failures, such as a test that fails, count at {ORDINARY_FAILURE_WEIGHT}. A red run "
        "followed by an edit and a green run of the same job is dropped; one that passed only "
        "after a setup step, with no file edited, is kept in full. Read-only inspection "
        "commands (ls, grep, …) are ignored.",
        "Agents guess how to build, test and run things; a wrong guess burns a round trip "
        "every session until the right command is written down.",
        "The normalized command. Ordinary failures are also attributed to repo files named "
        "on the command line; setup/invocation errors are not (the files are not the cause).",
        2.0,
    ),
    SignalDef(
        HUMAN_REWORK,
        "Human rework (git)",
        "A commit attributed to an agent changed a file, and the next commit touching that "
        "file, made by a human within the rework window, rewrote lines the agent commit had "
        "written or is described as a fix or revert. A human continuing work elsewhere in "
        "the file does not count.",
        "Agent output on this file needed a human follow-up soon after it landed.",
        "The file, from git history. Never raises confidence on its own.",
        1.5,
    ),
    SignalDef(
        EDIT_THRASH,
        "Edit thrash",
        f"One file received {EDIT_THRASH_MIN_EDITS} or more successful edits within a single "
        "turn in which something also went wrong: an edit miss, a stale edit or a reverted "
        "edit on that file, or a failed command or retry loop in the turn.",
        "Many small edits to one file in one go usually means trial and error rather than "
        "a plan. Weak signal on its own.",
        "The file being edited.",
        1.5,
    ),
    SignalDef(
        PERMISSION_DENIED,
        "Permission denied",
        "A tool call was refused by permission settings, a hook or a safety check before it "
        "ran. No human was asked, and the command or edit never happened.",
        "The agent wanted to do something the setup does not allow. Either the allow-list is "
        "missing a routine command, or the agent does not know the boundary.",
        "The file the tool targeted, or the normalized shell command (the step most likely "
        "to have been refused).",
        1.5,
    ),
    SignalDef(
        STALE_EDIT,
        "Stale edit",
        "An edit or write was refused because the file had not been read first, or changed "
        "on disk after it was read.",
        "Something else (formatter, code generator, watcher, another agent) rewrites this "
        "file, or the agent edits it blind.",
        "The file being edited.",
        1.0,
    ),
    SignalDef(
        PROBABLE_CORRECTION,
        "Probable correction",
        "A human prompt that is worded like a correction but is not corroborated by "
        "behaviour (the agent had changed nothing, or the human names no changed file and "
        "the agent did not go back to what it had changed), or that only contains two or more "
        "weaker cues. It may be an ordinary follow-up request.",
        "Possibly a redirect by a human. Kept apart from corrections so that a guess never "
        "inflates their count; never quoted, never the basis of a recommendation.",
        f"Split evenly over the files edited in the preceding turn (the {CORRECTION_MAX_FILES} "
        "most recent).",
        1.5,
    ),
    SignalDef(
        FILE_TOO_LARGE,
        "File too large to read",
        "A read was refused because the file exceeds what the harness returns in one piece.",
        "The agent has to page through the file or search it blind, in every session.",
        "The file.",
        1.0,
    ),
    SignalDef(
        PATH_MISS,
        "Path miss",
        "The agent tried to read or edit a path that does not exist. A read of a missing "
        "file that the agent then creates in the same session is not recorded.",
        "The agent expected something to live here; the layout or naming misled it.",
        "The non-existent path (so repeated wrong guesses add up).",
        1.0,
    ),
    SignalDef(
        SEARCH_MISS,
        "Search miss",
        "A Grep or Glob search returned nothing. A search that only confirms a name is free "
        "before the agent creates it is not recorded.",
        "The agent looked for a name that is not there; repeated misses in one place "
        "point at vocabulary the codebase does not use.",
        "The file or directory that was searched.",
        1.0,
    ),
    SignalDef(
        READ_LOOP,
        "Read loop",
        f"The same range of a file was read {READ_LOOP_MIN_READS} or more times in one "
        "session with no successful edit to it in between.",
        "The agent keeps going back to this file without making progress. Weak signal.",
        "The file being read.",
        0.5,
    ),
)

#: Why a red run was seen and not reported (``totals.dropped_red_runs``).
DROPPED_DEV_LOOP = "dev-loop"  # red, change, green
DROPPED_WAITING = "waiting"  # the same line failed, then passed untouched
DROPPED_ONE_OFF = "one-off"  # green after a command that fixed it in this session only

BY_ID: dict[str, SignalDef] = {s.id: s for s in CATALOG}
SEVERITY: dict[str, float] = {s.id: s.severity for s in CATALOG}

#: Lines printed when dependencies cannot be installed or a package cannot be built.
_INSTALL_SHAPES = (
    r"npm (?:ERR!|error) code E(?:40\d|5\d\d|RESOLVE|NOTFOUND|TARGET|ACCES|INTEGRITY|JSONPARSE"
    r"|CONNRESET|TIMEDOUT|NOENT).*"
    r"|ERR_PNPM_\S+.*"
    r"|error (?:Couldn't find package|An unexpected error occurred:).*"
    r"|\s*(?:[×x] |error: |ERROR: )?Failed to build .*"
    r"|\s*(?:[×x] )?(?:error: )?subprocess-exited-with-error.*"
    r"|ERROR: (?:Could not (?:build|install)|Failed building wheel|Cannot install).*"
    r"|\s*[×x] No solution found when resolving .*"
    r"|error: (?:failed to (?:select a version|download|get|load manifest)|no matching package)"
    r"\b.*"
)
#: Output shapes that mark a command failure as a setup/invocation problem. Each is
#: anchored to a whole line as the shell, the interpreter or the tool itself prints it,
#: so words like "timed out" or "permission denied" inside test output do not qualify.
INVOCATION_ERROR_RE = re.compile(
    r"^(?:"
    # the shell could not find or run the program
    r"(?:[\w./-]+: )?(?:line \d+: |\d+: )?[^\s:]+: (?:command )?not found"
    r"|(?:zsh: )?command not found(?:: \S+)?"
    r"|.* is not recognized as an internal or external command.*"
    r"|(?:[\w./-]+: )?(?:line \d+: )?[^\s:]+: (?:No such file or directory|Permission denied)"
    r"|.*executable file not found in \$PATH.*"
    # the interpreter could not start the program
    r"|ModuleNotFoundError: No module named .*"
    r"|(?:[\w./-]+: )?No module named \S+"
    r"|ImportError while loading conftest .*"
    r"|(?:[\w./-]+: )?can't open file .*"
    r"|(?:Error|node:internal\S*): Cannot find module .*"
    r"|.*Cannot find module '[^']+'\s*"
    # the tool rejected how it was called
    r"|(?:[\w./-]+: )?(?:error|ERROR): (?:unrecognized arguments?|unknown (?:option|flag|command)"
    r"|unexpected argument|no such (?:option|command)|invalid choice|file or directory not found)"
    r"\b.*"
    r"|(?:[\w./-]+: )?(?:unrecognized|unknown|invalid|illegal) option\b.*"
    r"|Error: No such (?:command|option)\b.*"
    r"|Usage: .*\n(?:.*\n)*?.*Error: .*"
    # the project is not set up the way the command assumes
    r"|make(?:\[\d+\])?: \*\*\* No rule to make target .*"
    r"|.*No rule to make target .*"
    r"|npm (?:ERR!|error) (?:Missing script|enoent|code ENOENT).*"
    r"|fatal: not a git repository.*"
    r"|error: could not find `Cargo\.toml`.*"
    r"|(?:ERROR|error): (?:Could not find a version|No matching distribution|"
    r"externally-managed-environment).*"
    r"|go: (?:cannot find main module|go\.mod file not found).*"
    # dependencies could not be installed or built
    r"|" + _INSTALL_SHAPES +
    # the network was not there
    r"|curl: \(\d+\) .*"
    r")$",
    re.M,
)
#: The same install/build shapes on their own: they identify the failing step of a chain.
INSTALL_FAILURE_RE = re.compile(r"^(?:" + _INSTALL_SHAPES + r")$", re.M)
#: Output that only the named kind of step prints, used to tell which step of
#: ``a && b`` failed. Keyed by role or program; a step with no entry cannot be confirmed.
STEP_SIGNATURES: dict[str, re.Pattern[str]] = {
    "test": re.compile(
        r"^=+ .* in [\d.]+s|^(?:FAILED|ERROR) \S+::|^=+ (?:FAILURES|ERRORS|test session starts) =+"
        r"|^collected \d+ items?|no tests ran|^(?:FAIL|Tests:|Test Suites:)\s|^--- FAIL"
        r"|test result: FAILED|^\s*\d+ (?:failed|failing)\b|^FAILED \(",
        re.M,
    ),
    "lint": re.compile(
        r"^Found [1-9]\d* errors?\b(?! in \d+ files?)(?! \((?:\d+ fixed, )?0 remaining\))"
        r"|[1-9]\d* problems? \(|^\S+:\d+:\d+: [A-Z]+\d+ ",
        re.M,
    ),
    "typecheck": re.compile(
        r"error TS\d+|^Found [1-9]\d* errors? in \d+ files?|^\S+:\d+: error: ", re.M
    ),
    "make": re.compile(r"^make(?:\[\d+\])?: \*\*\* ", re.M),
    "cargo": re.compile(r"^error(?:\[E\d+\])?: |could not compile", re.M),
    "git": re.compile(r"^fatal: |^error: (?:pathspec|failed to push|Your local changes)", re.M),
    "docker": re.compile(r"^docker: |Error response from daemon|failed to solve", re.M),
    "go": re.compile(r"^FAIL\b|^--- FAIL|cannot find package", re.M),
}


#: What each kind of check prints when it failed and when it passed: ``(red, green)``, by
#: role or program. A ruff summary says nothing about mypy, and the other way round.
_TEST_RED = (
    r"^(?:=+ )?(?:\d+ \w+, )*\d+ (?:failed|errors?)\b.* in [\d.]+s"
    r"|^(?:FAILED|ERROR) \S+::|^FAILED \((?:failures|errors)="
    r"|^Tests?:\s+.*\b\d+ failed|^Test (?:Suites|Files):?\s+.*\b\d+ failed|^\s*FAIL\s+\S+"
    r"|^\s*[1-9]\d* failing\b|\b\d+ examples?, [1-9]\d* failures?"
    r"|^--- FAIL|^FAIL(?:\s+\S+\s+[\d.]+s)?$|^test result: FAILED"
    r"|^\s*[1-9]\d* (?:failed|failures?)\b"
)
_TEST_GREEN = (
    r"^(?:=+ )?\d+ passed\b(?!.*\b(?:failed|error))"
    r"|^Tests?:\s+(?!.*failed)\d+ passed|^Test (?:Suites|Files):?\s+(?!.*failed)\d+ passed"
    r"|^ok\s+\S+\s+[\d.]+s|^PASS\b|^test result: ok|^OK(?: \(|$)|^\s*\d+ passing\b"
    r"|\b\d+ examples?, 0 failures"
    r"|^[.sx]+\s+\[100%\]$"  # pytest -q: the last progress line, with no F or E in it
)
#: ``Found 2 errors (2 fixed, 0 remaining).`` is ``ruff --fix`` having succeeded, and
#: ``Found 3 errors in 2 files`` is mypy's line, not a linter's.
_LINT_RED = (
    r"^Found [1-9]\d* errors?\b(?! in \d+ files?)(?! \((?:\d+ fixed, )?0 remaining\))"
    r"|[1-9]\d* problems? \([1-9]\d* errors?"
)
_LINT_GREEN = r"^All checks passed|^Found \d+ errors? \(\d+ fixed, 0 remaining\)|\b0 problems\b"
_TYPE_RED = r"error TS\d+|^Found [1-9]\d* errors? in \d+ files?|^\S+:\d+: error: "
_TYPE_GREEN = r"^Success: no issues|^Found 0 errors|^0 errors\b"
_FORMAT_RED = r"^Would reformat|\bfiles? would be reformatted\b"
_FORMAT_GREEN = r"^\d+ files? (?:already formatted|left unchanged|reformatted)"
_BUILD_RED = (
    r"^make(?:\[\d+\])?: \*\*\* |^error(?:\[E\d+\])?: |could not compile|^npm (?:ERR!|error) "
)
_BUILD_GREEN = r"^\s*Finished .*(?:dev|release|test)"


def _signature(red: str, green: str) -> tuple[re.Pattern[str], re.Pattern[str] | None]:
    return re.compile(red, re.M), re.compile(green, re.M) if green else None


VERDICT_SIGNATURES: dict[str, tuple[re.Pattern[str], re.Pattern[str] | None]] = {
    "test": _signature(_TEST_RED, _TEST_GREEN),
    "lint": _signature(_LINT_RED, _LINT_GREEN),
    "typecheck": _signature(_TYPE_RED, _TYPE_GREEN),
    "format": _signature(_FORMAT_RED, _FORMAT_GREEN),
    "build": _signature(_BUILD_RED, _BUILD_GREEN),
    "make": _signature(r"^make(?:\[\d+\])?: \*\*\* ", ""),
    "cargo": _signature(r"^error(?:\[E\d+\])?: |could not compile", _BUILD_GREEN),
    "npm": _signature(r"^npm (?:ERR!|error) ", ""),
}
#: Output of a check that failed, whatever the exit status says. Used when the status is
#: not the command's own (``pytest | tail -5``, ``make test; echo done``) and the line has
#: one command, whose output may come from any tool (``make check``).
RED_OUTPUT_RE = re.compile(
    "|".join((_TEST_RED, _LINT_RED, _TYPE_RED, _FORMAT_RED, _BUILD_RED)), re.M
)
#: The last line of an uncaught Python exception.
_TRACEBACK_END_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)(?::|$)")
#: Output of a check that passed.
GREEN_OUTPUT_RE = re.compile(
    "|".join((_TEST_GREEN, _LINT_GREEN, _TYPE_GREEN, _FORMAT_GREEN, _BUILD_GREEN)), re.M
)


def key_verdict(key: str, output: str) -> str | None:
    """``"red"``/``"green"`` if ``output`` carries that verdict *of the tool behind ``key``*.

    Only the signatures of the key's own role or program are consulted, so in
    ``ruff check .; mypy; pytest`` each step is judged by its own summary line.
    """
    words = key.split()
    names = (command_role(key), words[0] if words else "")
    found: str | None = None
    for name in names:
        signature = VERDICT_SIGNATURES.get(name or "")
        if signature is None:
            continue
        red, green = signature
        if red.search(output):
            return "red"
        if green is not None and green.search(output):
            found = "green"
    return found


def _candidates(cmd: ParsedCommand) -> list[Step]:
    return [
        s
        for s in cmd.steps
        if s.key and not s.inspection and s.program not in SETUP_PROGRAMS | HOUSEKEEPING_PROGRAMS
    ]


def step_verdicts(cmd: ParsedCommand, output: str) -> dict[str, str]:
    """Verdict per step key of a line with several commands, each by its own signature."""
    verdicts: dict[str, str] = {}
    for step in _candidates(cmd):
        verdict = key_verdict(step.key, output)
        if verdict is not None and verdicts.get(step.key) != "red":
            verdicts[step.key] = verdict
    # `ruff format . && make check | tail -2 && git status`: an aggregate target has no
    # summary line of its own. It ran green when nothing in the output is red and the
    # output shows a passing summary that no other step on the line could have printed.
    steps = _candidates(cmd)
    aggregates = {s.key for s in steps if is_aggregate_check(s.key)} - set(verdicts)
    if len(aggregates) == 1 and not RED_OUTPUT_RE.search(output):
        (aggregate,) = aggregates
        taken = {command_role(s.key) for s in steps if s.key != aggregate}
        for role in ("test", "lint", "typecheck", "build"):
            green = VERDICT_SIGNATURES[role][1]
            if role not in taken and green is not None and green.search(output):
                verdicts[aggregate] = "green"
                break
    return verdicts


def output_verdict(output: str, first_party: Callable[[str], bool] | None = None) -> str | None:
    """``"red"``, ``"green"`` or ``None`` (cannot tell) for a command judged by its output.

    For command lines whose exit status belongs to something else (a trailing
    ``| tail``, ``; echo done``, ``|| true``): there an exit code of 0 says
    nothing about the command that matters.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if is_invocation_error(output, None, first_party) or RED_OUTPUT_RE.search(output):
        return "red"
    traceback = bool(lines) and "Traceback (most recent call last)" in output
    if traceback and _TRACEBACK_END_RE.match(lines[-1].strip()):
        return "red"
    if GREEN_OUTPUT_RE.search(output):
        return "green"
    return None


_PYTHON_NAME_RE = re.compile(r"^(?:python|pypy)[\d.]*$")
#: Largest edit distance at which a failed command counts as a mistyped other one.
TYPO_MAX_DISTANCE = 2


def _distance(a: str, b: str) -> int:
    """Levenshtein distance."""
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        previous, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (ca != cb))
    return row[-1]


def is_typo_of(failed: str, other: str) -> bool:
    """``npm run biuld`` for ``npm run build``: one word differs, by a slip of the keyboard."""
    mine, theirs = failed.split(), other.split()
    if len(mine) != len(theirs) or mine == theirs:
        return False
    differing = [(a, b) for a, b in zip(mine, theirs, strict=True) if a != b]
    if len(differing) != 1:
        return False
    a, b = differing[0]
    return min(len(a), len(b)) >= 4 and _distance(a, b) <= TYPO_MAX_DISTANCE


def _step_signature(step: Step) -> list[re.Pattern[str]]:
    if is_setup_command(step.key):
        return [INSTALL_FAILURE_RE]
    names = (command_role(step.key), step.program)
    return [STEP_SIGNATURES[name] for name in names if name in STEP_SIGNATURES]


def failing_step(cmd: ParsedCommand, output: str) -> tuple[Step | None, bool]:
    """Which command of a chain failed, judged by what the output looks like.

    Returns ``(step, certain)``. With a single real command there is nothing
    to decide. Otherwise the first step whose own kind of error output is
    present is the one; if no step can be confirmed, the result is the step
    the line is named after, marked uncertain.
    """
    primary = next((s for s in cmd.steps if s.key == cmd.key), None)
    candidates = [
        s
        for s in cmd.steps
        if not s.inspection and s.program not in SETUP_PROGRAMS | HOUSEKEEPING_PROGRAMS
    ]
    if len({s.key for s in candidates}) <= 1:
        return primary, True
    for step in candidates:
        if any(pattern.search(output) for pattern in _step_signature(step)):
            return step, True
    return primary, False


#: An invocation failure that names the module or file it could not find: ``(kind, name)``.
_MISSING_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("module", re.compile(r"No module named ['\"]?([\w.]+)")),
    ("file", re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]")),
    ("file", re.compile(r"can't open file ['\"]([^'\"]+)['\"]")),
    ("file", re.compile(r"file or directory not found: (\S+)")),
    ("file", re.compile(r"No such file or directory: ['\"]([^'\"]+)['\"]")),
)
#: `git commit` with nothing staged: a no-op, not a failure of anything.
NOTHING_TO_COMMIT_RE = re.compile(
    r"nothing to commit|no changes added to commit|nothing added to commit", re.I
)
#: A pre-commit hook reformatted files and stopped the commit so they can be re-staged.
HOOK_MODIFIED_RE = re.compile(r"files were modified by this hook|^Fixing \S+", re.I | re.M)
#: git subcommands that put files back the way they were.
_GIT_UNDO_KEYS = frozenset({"git revert", "git reset", "git restore", "git stash"})


def missing_name(output: str) -> tuple[str, str] | None:
    """The module or file an invocation error says does not exist, if it names one."""
    for kind, pattern in _MISSING_RES:
        match = pattern.search(output)
        if match is not None:
            return kind, match.group(1)
    return None


def provides(path: str, kind: str, name: str) -> bool:
    """Would creating repo file ``path`` supply the missing module or file ``name``?"""
    stem = path.rsplit(".", 1)[0] if "." in path.rsplit("/", 1)[-1] else path
    if kind == "module":
        want = name.strip(".").replace(".", "/")
        if not want:
            return False
        return any(
            candidate == want or candidate.endswith("/" + want)
            for candidate in (stem, stem.removesuffix("/__init__"))
        )
    want = posixpath.normpath(name.replace("\\", "/")).lstrip("./")
    if not want:
        return False
    return any(
        a == b or a.endswith("/" + b) or b.endswith("/" + a)
        for a, b in ((path, want), (stem, want))
    )


#: Exit codes the shell itself uses for "cannot execute" and "not found".
INVOCATION_EXIT_CODES = frozenset({126, 127})


#: The harness's own timeout notice. It opens the tool result; the same words printed
#: by a test runner further down are ordinary output.
HARNESS_TIMEOUT_RE = re.compile(r"\s*(?:Exit code \d+\s*\n\s*)?Command timed out after \d")


#: Lines of a test report that quote an error raised *inside a test*: pytest's ``E   ...``
#: lines, its ``FAILED path::test - reason`` summary, and the frames of a traceback.
_QUOTED_LINE_RE = re.compile(
    r"^(?:E(?:\s.*)?|(?:FAILED|ERROR) \S+(?:::\S+)? - .*|\s+File \".*|\s+at .*|>\s.*)$", re.M
)
#: Signs that tests actually ran: then a quoted import error is a test's own failure. Without
#: them the run stopped at collection, and the module pytest names is what is missing.
_TESTS_RAN_RE = re.compile(
    r"^=+ FAILURES =+|^FAILED \S+::|\b\d+ (?:passed|failed|xfailed|xpassed)\b", re.M
)
_COLLECTION_MISSING_RE = re.compile(
    r"^E\s+ModuleNotFoundError: No module named ['\"]?([\w.]+)", re.M
)


def is_invocation_error(
    output: str, exit_code: int | None, first_party: Callable[[str], bool] | None = None
) -> bool:
    """True when a failed command never got to do its job (setup or usage problem).

    Errors quoted from inside a test (``E   ModuleNotFoundError ...`` in a
    pytest report, a ``FAILED ... - `` line, traceback frames) are the test's
    business, not the command's. A module that cannot be imported is a setup
    problem only if it is not the repository's own (``first_party`` says so for
    a top-level package name): a typo in an import is a bug to fix, a missing
    dependency is an environment to set up.
    """
    if exit_code in INVOCATION_EXIT_CODES or HARNESS_TIMEOUT_RE.match(output):
        return True
    own = first_party or (lambda top: False)
    direct = _QUOTED_LINE_RE.sub("", output)
    if INVOCATION_ERROR_RE.search(direct):
        missing = missing_name(direct)
        ours = missing is not None and missing[0] == "module" and own(missing[1].split(".")[0])
        return not ours
    match = _COLLECTION_MISSING_RE.search(output)
    if match is not None and not _TESTS_RAN_RE.search(output):
        return not own(match.group(1).split(".")[0])
    return False


#: Failures of the surroundings rather than of the code or the command line: a service
#: that is not running, credentials that are missing or expired, an unset variable.
_SERVICE_DOWN_RE = re.compile(
    r"Cannot connect to the Docker daemon|Connection refused|ECONNREFUSED"
    r"|could not connect to server|connection to server\b.{0,120}\bfailed"
    r"|Is the server running|Can't connect to (?:local )?(?:MySQL )?server"
    r"|No connection could be made|server closed the connection unexpectedly",
    re.I,
)
_AUTH_RE = re.compile(
    r"credentials (?:have |are )?expired|expired ?token|token (?:has |is )?expired"
    r"|Unable to locate credentials|You must be logged in|not logged in(?:to)?\b"
    r"|authentication failed|Authentication required|Permission denied \(publickey"
    r"|\b(?:gh|gcloud|az|aws sso|docker|npm|vercel|firebase) (?:auth )?login\b"
    r"|InvalidClientTokenId|could not read Username",
    re.I,
)
_VAR = r"[`'\"]?([A-Z][A-Z0-9_]{2,})[`'\"]?"
_ENV_VAR_RE = re.compile(
    rf"[Mm]issing(?: required)? env(?:ironment)? var(?:iable)?s?:? {_VAR}"
    rf"|[Ee]nv(?:ironment)? var(?:iable)? {_VAR} (?:is )?(?:not set|missing|required|undefined)"
    rf"|\b{_VAR}: unbound variable|^KeyError: {_VAR}\s*$"
    rf"|\b{_VAR} (?:is not set|must be set|environment variable (?:is )?(?:not set|required))",
    re.M,
)
#: Services named only from a fixed vocabulary, so that no transcript text is passed on.
_SERVICE_PORTS = {
    "5432": "PostgreSQL",
    "3306": "MySQL",
    "6379": "Redis",
    "27017": "MongoDB",
    "5672": "RabbitMQ",
    "9200": "Elasticsearch",
    "9092": "Kafka",
    "11211": "Memcached",
}
_SERVICE_WORDS = (
    (re.compile(r"docker daemon|docker\.sock", re.I), "the Docker daemon"),
    (re.compile(r"psycopg|postgres|psql|\bpg_", re.I), "PostgreSQL"),
    (re.compile(r"mysql|mariadb", re.I), "MySQL"),
    (re.compile(r"redis", re.I), "Redis"),
    (re.compile(r"mongo", re.I), "MongoDB"),
    (re.compile(r"rabbitmq|amqp", re.I), "RabbitMQ"),
    (re.compile(r"elasticsearch|opensearch", re.I), "Elasticsearch"),
    (re.compile(r"kafka", re.I), "Kafka"),
)
_PORT_RE = re.compile(r"(?:\bport |(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):)(\d{2,5})\b")
ENV_SERVICE = "service"
ENV_AUTH = "auth"
ENV_VARIABLE = "variable"


def environment_error(output: str) -> tuple[str, str] | None:
    """``(kind, name)`` if a failure's output blames the environment, else ``None``.

    ``kind`` is ``service`` (something the command talks to is not running),
    ``auth`` (credentials missing or expired) or ``variable`` (an environment
    variable is not set). ``name`` is the service from a fixed vocabulary
    ("PostgreSQL", "the Docker daemon", "the service on port 8080"), the
    variable's name, or empty.
    """
    down = _SERVICE_DOWN_RE.search(output)
    if down is not None:
        port = _PORT_RE.search(output)
        if port is not None and port.group(1) in _SERVICE_PORTS:
            return ENV_SERVICE, _SERVICE_PORTS[port.group(1)]
        for pattern, name in _SERVICE_WORDS:
            if pattern.search(output):
                return ENV_SERVICE, name
        return ENV_SERVICE, f"the service on port {port.group(1)}" if port else ""
    variable = _ENV_VAR_RE.search(output)
    if variable is not None:
        return ENV_VARIABLE, next(g for g in variable.groups() if g)
    if _AUTH_RE.search(output):
        return ENV_AUTH, ""
    return None


#: Commands that keep running until stopped. Run in the foreground they can only end in
#: the harness's timeout, which says nothing about how to invoke them.
DEV_SERVER_RE = re.compile(
    r"(?:^|[\s;&|(])(?:"
    r"(?:npm|pnpm|yarn|bun)(?: run)? (?:dev|start|serve|preview|watch|storybook)\b"
    r"|(?:npx |bunx |pnpm exec |uv run |poetry run |python3? -m )?(?:vite|next dev|nuxt dev"
    r"|webpack serve|webpack-dev-server|uvicorn|gunicorn|hypercorn|daphne|flask run"
    r"|http\.server|live-server|nodemon|ng serve|streamlit run|mkdocs serve|hugo server"
    r"|jekyll serve|php artisan serve|rails s(?:erver)?|bin/rails s(?:erver)?|cargo watch"
    r"|docker compose up(?! (?:.* )?(?:-d|--detach)\b)|tail -f)(?:\s|$)"
    r"|python3? (?:\S*/)?manage\.py runserver\b"
    r")"
)
#: "bash: pnpm: command not found" and its variants: the program a shell could not find.
_NOT_FOUND_PROGRAM_RE = re.compile(
    r"(?:^|: )([^\s:]+): (?:command )?not found\s*$|command not found: (\S+)\s*$", re.M
)


#: Praise, thanks and dismissals. A correction phrase *followed* by one of these is not a
#: correction ("you forgot nothing, great job", "still failing but that's unrelated, ignore
#: it"), and a message containing one is never a "probable" correction.
_SOFTENER_RE = re.compile(
    r"\b(?:thanks|thank you|thx|great|good job|nice(?:ly)?|perfect|awesome|excellent|well done"
    r"|love it|like it|nailed it|keep going|looks good|lgtm|all good|amazing|wonderful"
    r"|no worries|never ?mind|ignore (?:it|that|this)|unrelated|not your fault|flaky"
    r"|(?:that's|it's|that is|it is|is|are) (?:fine|ok|okay)"
    # the human takes the blame, or is only asking
    r"|(?:because|after|since|when|once) (?:i|my|we|our)\b|my (?:bad|fault|mistake)"
    r"|just (?:curious|wondering|asking|checking|confirming|making sure)"
    r"|out of curiosity|no complaints?|not complaining|curious|genuine(?:ly)? (?:question|asking)"
    # an acknowledgement: "you removed the flag as asked, now ..."
    r"|as (?:asked|requested|expected|planned|discussed|agreed|i (?:asked|wanted|said))"
    # the problem predates the agent's work
    r"|before your (?:change|changes|edit|edits|commit|commits|fix|pr)|not caused by you(?:r \w+)?"
    r"|already (?:broken|failing|red|there)|pre-?existing|not (?:related to|because of) your)\b",
    re.I,
)
#: The human is moving on to something else: a description of a bug ("it doesn't work on
#: Windows yet; next task: ...") is then a task, not a verdict on the agent's work.
#: A phrase whose sentence ends by asking for confirmation is a question.
_CONFIRM_TAIL_RE = re.compile(r"^[^.!?\n]*,? (?:right|correct|yes|no|or not)\?", re.I)
_NEW_TASK_RE = re.compile(
    r"\b(?:next|new|another|separate|different|unrelated) (?:task|topic|thing|issue|ticket|bug)\b"
    r"|\bnext(?: up)?:|\bmov(?:e|ing) on\b|\bnext,? (?:please )?(?:add|fix|implement|do|let's)\b",
    re.I,
)
#: Interjections that may precede a verdict at the start of a message ("hmm, that's wrong").
_OPEN = (
    r"^(?:(?:hmm+|ugh+|argh+|wtf|ok(?:ay)?|well|so|sorry|oh|hey|no+|nope|nah|but|and|damn|sigh"
    r"|um+|uh+|eh|again|man|dude|wait|still)[,.!…:;—-]*\s+)*"
)
_YOU_DID = (
    r"\byou (?:broke|removed|deleted|forgot|missed|ignored|misunderstood|overwrote)\b"
    r"(?! (?:nothing|anything)\b)"
    r"|\byou(?:'ve| have) (?:broken|removed|deleted|forgotten|missed|ignored|overwritten)\b"
    r"(?! (?:nothing|anything)\b)"
    r"|\byou (?:didn't|did not) (?:\w+ )?(?:run|read|follow|do|use|update|fix|check|add|listen"
    r"|test|change|remove|answer)\b(?! (?:nothing|anything)\b)"
    r"|\byou (?:shouldn't|should not) have\b|\byou (?:weren't|were not) supposed\b"
    r"|\byou keep \w+ing\b"
)
_THING = (
    r"file|branch|directory|dir|folder|function|module|place|repo|path|approach|way|one|test"
    r"|package|version|line|class|method|command|thing|part|section|variable|field|column"
    r"|table|endpoint|component|config|setting|key|value|import|test file|page"
)
_UNDO = r"(?:revert|undo|roll ?back)"
#: What an undo must refer to: the agent's own change, by pronoun or as "the last edit".
_UNDO_OBJECT = (
    r"(?:that|this|it|your|those|these|all (?:of )?(?:that|this|your)|everything|what you"
    r"|(?:the|my) (?:last|previous|latest) (?:change|changes|edit|edits|commit|commits|one"
    r"|thing|step|two|three|few|\d+)"
    r"|the (?:change|changes|edit|edits|commit|commits))\b"
)
_STILL = (
    r"still (?:failing|fails|broken|not working|not fixed|doesn't work|does not work"
    r"|wrong|the same (?:error|problem|issue|bug)|crash(?:es|ing)?"
    r"|(?:getting|seeing|hitting) (?:the|that|this|an?) (?:same |exact same )?"
    r"(?:error|errors|failure|failures|exception|problem|issue|bug|crash|traceback))\b"
    r"|(?:failing|fails|broken|crashing|crashes) again\b"
)

#: Subjects that stand for the outcome of the agent's work in "the tests are still failing".
#: A longer noun phrase ("the login page is still broken") more often describes a task.
_RESULT_NOUN = (
    r"tests?|build|ci|pipeline|lint|linter|type ?check|compile|app|server|page|command|script"
    r"|error|bug|issue|problem|output|result|check|checks|job|deploy|import"
)

#: Labels of the openers that are an *answer*, not a verdict, when the agent had just asked
#: a question ("Shall I refactor util.py?" -> "No, leave it." / "Stop. Just commit.").
_ANSWER_LABEL = "no, …"
_ANSWER_LABELS = frozenset({_ANSWER_LABEL, "no no", "not quite", "wrong. / stop."})

#: (label, pattern, addressed). *Addressed* phrases speak to the agent in the second person
#: or about what the human had asked for, and count wherever they stand in the head of the
#: message. The others are verdicts ("that's wrong", "still failing") that only refer to the
#: agent's work when they open the message: the same words later in a prompt usually
#: describe a bug to work on ("the customer says it's not working").
_STRONG_CORRECTION: tuple[tuple[str, re.Pattern[str], bool], ...] = tuple(
    (label, re.compile(pattern, re.I), addressed)
    for label, pattern, addressed in (
        (
            # "No, ..." / "No. ..." / "Nope - ..." / a bare "no"; not "No. 3 ...", "no thanks",
            # "no problem", "no way", "No tests are needed", "Nope, nothing else".
            _ANSWER_LABEL,
            r"^(?:no|nope|nah)(?:[.!]*$|\s*[,.;:!—-]+\s+(?!\d)"
            r"(?!(?:thanks|thank you|problem|worries|rush|need|that's (?:fine|ok|okay|great|all|it)"
            r"|it's (?:fine|ok|okay)|all (?:good|set|done)|go ahead|way|nothing|none"
            r"|not (?:yet|now|really|necessary|needed|today)|i'm (?:good|fine|done)"
            r"|we're (?:good|fine|done)|just (?:that|this)|leave it|keep it"
            # a choice between options the agent offered, not a verdict on its work
            r"|(?:go|let's go|stick|continue|proceed) with|option|the (?:first|second|third"
            r"|former|latter|other) (?:one|option))\b)\S)",
            False,
        ),
        ("no no", r"^(?:no|nope)[\s,.!]+(?:no|nope)\b", False),
        (
            # "same error", "I'm getting the exact same failure"; not "same error handling
            # as in util.py, please"
            "same error",
            _OPEN
            + r"(?:(?:i|we)(?:'m|'re| am| are)? (?:still )?(?:get(?:ting)?|see(?:ing)?|got) )?"
            r"(?:the |that |exact(?:ly)? )*same (?:error|errors|problem|issue|failure|exception"
            r"|bug|crash|result|thing)\b(?! (?:handling|message|format|code|class|type|as in)\b)",
            False,
        ),
        (
            # "go back", "go back to how it was"; not "go back to the dashboard page and ..."
            "go back",
            r"^(?:(?:please|pls|just|ok|okay|now)[, ]+)*go back(?:[.!]*$|[.!,;—-]+\s"
            r"|\s+to (?:the (?:previous|old|older|original|earlier|first|last|way)\b|how\b"
            r"|what (?:it|you|we|i)\b|before\b|(?:my|your) (?:previous|first|original|earlier)\b))",
            False,
        ),
        (
            "not quite",
            # not "Not quite sure how auth works", "Almost, but one more feature: ..."
            r"^(?:not quite|not exactly)\b(?! (?:sure|certain|clear|ready|following"
            r"|understanding|done yet|finished)\b)|^not like that\b",
            False,
        ),
        (
            # "wrong. use pnpm" / "stop. use the other client"; not "Stop. Hammer time".
            "wrong. / stop.",
            r"^(?:wrong|incorrect)(?:[.!]*$|\s*[.,;:!—-]+\s+\S)"
            r"|^stop(?:[.!]*$|\s*[.,;:!—-]+\s+(?:use|don't|do not|you|that|this|it\b|revert|undo"
            r"|wait|not |wrong|go back|put|leave|keep|read|look|check|ask|please|just)"
            r"|\s+(?:that|it|this|doing|right there|\w+ing)\b)",
            False,
        ),
        (
            "that's wrong",
            _OPEN + r"(?:that|this|it)(?:'s| is| was) (?:"
            r"(?:still |just |simply |completely |totally |plain )?(?:wrong|incorrect|broken)"
            r"(?: again)?(?=\s*(?:[.,;:!?—-]|$|and\b|but\b))"
            r"|not (?:right|correct|working|true|how)\b"
            r"|not what (?:i|we)\b)",
            False,
        ),
        (
            "that didn't work",
            # also a bare "doesn't work."; not "It doesn't work in Safari, can you ...",
            # which describes where a feature is missing.
            _OPEN + r"(?:(?:that|this|it) )?(?:didn't|doesn't|does not|did not) (?:work|help)\b"
            r"(?! (?:in|on|for|with|when|if|under|until|unless|without)\b)",
            False,
        ),
        (
            "that broke …",
            _OPEN + r"(?:that|this|it|your (?:change|changes|edit|edits|fix|commit))"
            r"(?: just)? (?:broke|breaks)\b",
            False,
        ),
        (
            "the wrong file",
            r"\b(?:you(?:'re| are| were)?|that's|that is|this is|it's|it is) (?:\w+ ){0,3}"
            rf"the wrong (?:{_THING})\b",
            True,
        ),
        ("you broke/forgot/…", _YOU_DID, True),
        (
            # An imperative about the agent's own change: "revert that", "undo your change",
            # "put it back". Not "Undo support is the next feature", "revert the design doc".
            "revert/undo",
            rf"^(?:(?:please|pls|just|now|ok|okay|so)[, ]+)*{_UNDO}(?:[.!,]|$|\s+{_UNDO_OBJECT})"
            rf"|(?:[.,;:!?]|\b(?:please|just|and|then|so))\s+{_UNDO}\s+{_UNDO_OBJECT}"
            rf"|\b(?:can|could|would|will) you (?:please |just )?{_UNDO}\s+{_UNDO_OBJECT}"
            r"|\bput (?:it|that|this|them|those|everything) (?:all )?back\b",
            True,
        ),
        (
            "still failing",
            _OPEN + r"(?:(?:it|that|this|i|we|(?:the |my |our )?(?:" + _RESULT_NOUN + r"))"
            r"(?:'s|'m|'re| is| am| are| was| were| keeps?)? )?(?:" + _STILL + ")",
            False,
        ),
        (
            # Said *to the agent*: "I told you", "I asked you to", "I said use X".
            "I told you",
            r"\bi (?:told you(?! about\b)|already (?:said|told|asked)|specifically (?:said|asked)"
            r"|(?:didn't|did not|never) ask(?:ed)? (?:for|you to)\b"
            r"|(?:didn't|did not|never) (?:want|mean) (?:that|this|it|those|these|any of)\b"
            r"|asked you (?:not )?to\b"
            r"|said (?:not to\b|to (?:use|keep|leave|run|stop|add|remove|put)\b|use\b|don't"
            r"|do not|no\b|not\b|only\b))",
            True,
        ),
        (
            "not what I asked",
            r"\b(?:not|isn't|wasn't|ain't) what i (?:asked|wanted|meant|said)\b",
            True,
        ),
        (
            "why did you",
            r"\bwhy did you (?:delete|remove|change|edit|touch|rewrite|revert|add|create|skip"
            r"|ignore|use|modify|disable|comment out|run|do that|not|drop|rename|move|replace)\b",
            True,
        ),
    )
)
_WEAK_CORRECTION: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        ("actually", r"^actually\b"),
        (
            # not "don't forget ...", "don't worry", "don't commit yet"
            "don't",
            r"\b(?:don't|do not) (?!forget|worry|bother|need|hesitate|mind|have to|think)\w+\b"
            r"(?!(?: \w+){0,2},? yet\b)",
        ),
        ("stop", r"\bstop (?:\w+ing|that|it|doing)\b"),
        ("instead", r"\binstead\b"),
        ("wrong", r"\b(?:wrong|incorrect|mistake)\b"),
        # "we should have a retry here" is a wish, not a reproach
        ("shouldn't", r"\b(?:shouldn't|should not|you should have|supposed to)\b"),
        ("again", r"\byou (?:\w+ )?(?:\w+ed|did|used|put|broke) (?:\S+ ){0,4}again\b"),
        ("not allowed", r"\b(?:deprecated|not allowed|forbidden|off limits)\b"),
    )
)


def correction_confidence(
    text: str, after_question: bool = False, idle_seconds: float | None = None
) -> tuple[float, list[str]]:
    """Confidence in [0, 1] that ``text`` corrects the agent's last turn, and why.

    * An *addressed* phrase ("you broke ...", "I told you ...", "revert that")
      gives 1.0 wherever it stands in the head of the message.
    * A *verdict* ("No, ...", "that's wrong", "still failing") gives 1.0 only
      when it opens the message, the message does not move on to a new task,
      and it is not a "No" answering a question the agent just asked
      (``after_question``). If the human replied more than
      ``CORRECTION_STALE_SECONDS`` after the agent finished (``idle_seconds``),
      a verdict alone is only a probable correction (0.4).
    * Praise or a dismissal after a phrase voids it.
    * Otherwise two or more distinct weaker cues give 0.4, unless the message
      contains thanks, praise or a dismissal.
    * A single weaker cue ("don't forget ...", "actually, can you also ...")
      is ordinary conversation and gives 0.
    """
    head = text.strip()[:CORRECTION_HEAD_CHARS].replace("’", "'")
    softeners = [m.start() for m in _SOFTENER_RE.finditer(head)]
    last_softener = max(softeners) if softeners else -1
    new_task = bool(_NEW_TASK_RE.search(head))
    strong: list[str] = []
    addressed_any = False
    for label, pattern, addressed in _STRONG_CORRECTION:
        match = pattern.search(head)
        if match is None or match.start() < last_softener:
            continue
        if not addressed and new_task:
            continue
        if label in _ANSWER_LABELS and after_question:
            continue
        if label == "I told you" and _CONFIRM_TAIL_RE.search(head[match.start() :]):
            continue  # "I told you about the flag, right?" asks; it does not reproach
        strong.append(label)
        addressed_any = addressed_any or addressed
    if strong:
        stale = idle_seconds is not None and idle_seconds > CORRECTION_STALE_SECONDS
        if stale and not addressed_any:
            return CORRECTION_PROBABLE, [*strong, "long pause"]
        return CORRECTION_STRONG, strong
    if softeners:
        return 0.0, []
    weak = [label for label, pattern in _WEAK_CORRECTION if pattern.search(head)]
    if len(weak) >= CORRECTION_MIN_CUES:
        return CORRECTION_PROBABLE, weak
    return 0.0, []


@dataclass
class DetectContext:
    """What detectors need to know about the repository."""

    resolver: PathResolver
    #: True if a repo-relative path is (or looks like) a file rather than a directory.
    is_file: Callable[[str], bool]
    #: True if a repo-relative path is a file known from disk or from any transcript.
    is_known_file: Callable[[str], bool]
    #: How a command key is shown (identity, or ``attribution.public_key`` without excerpts).
    command_key: Callable[[str], str] = str
    #: True if a top-level module/package name belongs to the repository itself.
    first_party: Callable[[str], bool] = lambda top: False


@dataclass
class _Action:
    call: Event
    result: Event | None = None
    file: str | None = None  # repo-relative path for read/edit/write
    cmd: ParsedCommand | None = None  # shell calls made inside the repo
    next_any: float | None = None  # time of the next transcript event after the call


def _span(action: _Action) -> tuple[float, float] | None:
    """A call's own duration: from the call to its result."""
    start, end = _epoch(action.call), _epoch(action.result)
    return (start, end) if start is not None and end is not None and end > start else None


def _epoch(event: Event | None) -> float | None:
    return event.ts.timestamp() if event is not None and event.ts is not None else None


def _text_matches(pattern: str, text: str) -> bool:
    """Does written ``text`` contain what an earlier search looked for?"""
    if not text:
        return False
    if pattern in text:
        return True
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return False


def _capped(start: float | None, end: float | None) -> tuple[float, float] | None:
    """Interval [start, end] limited to TIME_CAP_SECONDS from its start."""
    if start is None or end is None or end <= start:
        return None
    return (start, min(end, start + TIME_CAP_SECONDS))


def _capped_back(start: float | None, end: float | None) -> tuple[float, float] | None:
    """Interval [start, end] limited to the last TIME_CAP_SECONDS before its end."""
    if start is None or end is None or end <= start:
        return None
    return (max(start, end - TIME_CAP_SECONDS), end)


def _scope(call: Event, ctx: DetectContext) -> str | None:
    """Repo-relative file or directory a search was scoped to (``""`` is the repo root)."""
    base = ctx.resolver.resolve(call.path or ".", call.cwd)
    pattern = call.pattern or ""
    if call.tool == "Glob" or (call.op == model.OP_SEARCH and call.tool.lower() == "glob"):
        literal: list[str] = []
        for part in pattern.replace("\\", "/").split("/")[:-1]:
            if any(ch in part for ch in "*?[{"):
                break
            literal.append(part)
        prefix = "/".join(literal)
        if pattern.startswith("/"):
            return ctx.resolver.resolve(prefix or "/", call.cwd)
        if prefix and base is not None:
            joined = posixpath.normpath(posixpath.join(base, prefix))
            return None if joined.startswith("..") else ("" if joined == "." else joined)
    return base


@dataclass
class _Unconfirmed:
    """A correction whose wording is clear but whose effect is not yet seen."""

    finding: Finding
    files: frozenset[str]  # what the agent had changed in the turn being corrected
    matched: str
    how: str
    note: str = ""  # what the agent then did, once it has done it
    followed: list[str] = field(default_factory=list)  # which of ``files`` it went back to


@dataclass
class _Rework:
    """What a correction cost: the agent's follow-up on the files it was about."""

    finding: Finding
    files: frozenset[str]
    start: float | None = None  # the agent's first call after the correction
    first: tuple[float, float] | None = None  # that call's round trip
    end: float | None = None  # its last change to one of ``files`` in that turn


@dataclass
class _State:
    """Mutable per-stream detector state."""

    rework: _Rework | None = None

    turn: list[_Action] = field(default_factory=list)
    agent_seen: float | None = None  # last agent prose in this turn
    prompts: int = 0
    turn_edits: dict[str, int] = field(default_factory=dict)
    thrash: dict[str, Finding] = field(default_factory=dict)
    reads: dict[tuple[str, str], int] = field(default_factory=dict)
    read_findings: dict[tuple[str, str], Finding] = field(default_factory=dict)
    # (old, new) of each successful edit -> (when, successful commands so far, only added?)
    edit_history: dict[str, dict[tuple[str, str], tuple[float | None, int, bool]]] = field(
        default_factory=dict
    )
    ok_commands: int = 0
    write_history: dict[str, list[str]] = field(default_factory=dict)
    write_times: dict[tuple[str, str], float | None] = field(default_factory=dict)
    # consecutive failures of one exact command line: (key, normalized argv) -> count
    fail_runs: dict[tuple[str, str], int] = field(default_factory=dict)
    retry: dict[tuple[str, str], Finding] = field(default_factory=dict)
    pending: list[tuple[Finding, str, int]] = field(default_factory=list)
    open_edits: dict[str, list[Finding]] = field(default_factory=dict)
    # ordinary failures per command that are still red, and those an edit has followed
    red: dict[str, list[Finding]] = field(default_factory=dict)
    red_then_edited: dict[str, list[Finding]] = field(default_factory=dict)
    # ... and those followed only by commands that may have changed something, with
    # those commands per finding id: (label, is it a setup step?)
    red_then_ran: dict[str, list[Finding]] = field(default_factory=dict)
    ran_after: dict[int, list[tuple[str, bool]]] = field(default_factory=dict)
    looped: set[int] = field(default_factory=set)  # failures that are part of a retry loop
    key_of: dict[int, str] = field(default_factory=dict)  # command key per failure
    # failures for which another command stood in, per failed command
    replaced: dict[str, list[Finding]] = field(default_factory=dict)
    # setup command that succeeded after a failure that is still pending, by finding id
    setup_after: dict[int, str] = field(default_factory=dict)
    # empty searches that a later created file or written text may explain
    probes: list[tuple[Finding, Event, str]] = field(default_factory=list)
    # failures of one exact command line since the last change or setup step
    streak: dict[tuple[str, str], list[Finding]] = field(default_factory=dict)
    # invocation failures naming a module/file that does not exist: (finding, key, kind, name)
    missing: list[tuple[Finding, str, str, str]] = field(default_factory=list)
    # reads of files that did not exist (yet), by path
    missing_reads: dict[str, list[Finding]] = field(default_factory=dict)
    # a strongly worded correction waiting for the agent to act on it
    unconfirmed: _Unconfirmed | None = None
    # this turn: did anything go wrong, how did its last command end, which searches missed
    turn_trouble: bool = False
    turn_trouble_files: set[str] = field(default_factory=set)
    # successful tool calls so far, and that count when each finding was recorded
    ok_actions: int = 0
    turns: int = 0
    # successful file changes so far: (number, path or "" for a shell rewrite)
    changes: int = 0
    change_log: list[tuple[int, str, tuple[float, float] | None]] = field(default_factory=list)
    turn_changes: int = 0  # ``changes`` when the turn began
    # this turn: red runs of a command that each followed a change, never a green one
    cycles: dict[str, _Cycle] = field(default_factory=dict)
    # optional files (README, .env, ...) that a prompt or tool output named
    mentioned: set[str] = field(default_factory=set)
    # text that successful edits took out of files: (old, new)
    removed: list[tuple[str, str]] = field(default_factory=list)
    edit_turns: dict[tuple[str, str, str], int] = field(default_factory=dict)
    write_turns: dict[tuple[str, str], int] = field(default_factory=dict)
    emitted_at: dict[int, int] = field(default_factory=dict)
    turn_shell_ok: bool | None = None
    turn_check_ok: bool | None = None  # ... and its last test/lint/build/aggregate command
    turn_misses: list[tuple[Finding, str]] = field(default_factory=list)


@dataclass
class _Cycle:
    """Edit -> red run -> edit -> red run ... of one command within a turn."""

    seen: int  # ``_State.changes`` at its last run
    count: int = 0
    result: Event | None = None
    command: str = ""
    label: str = ""
    judged: bool = False  # a red run of it was read from output, not from an exit status
    unknown: bool = False  # its latest run showed neither red nor green
    spans: list[tuple[float, float]] = field(default_factory=list)  # red runs, edits between
    files: dict[str, int] = field(default_factory=dict)


#: What makes a run of edits to one file "thrash" rather than a productive sitting.
#: A failed command counts for every file of the turn; a miss or revert only for its own.
_TROUBLE = frozenset({COMMAND_FAILURE, RETRY_LOOP})
_FILE_TROUBLE = frozenset({EDIT_MISS, STALE_EDIT, EDIT_REVERT})


class _Detector:
    def __init__(self, stream: Stream, ctx: DetectContext) -> None:
        self.stream = stream
        self.ctx = ctx
        self.findings: list[Finding] = []
        self.exposed: set[tuple[str, str]] = set()  # every target the agent went near
        self.skipped_events = 0  # events a detector could not handle (never fatal)
        #: Red runs taken off the record: (transcript line, seconds the run took, reason)
        self.dropped: list[tuple[int, float, str]] = []
        self.state = _State()

    # ---- helpers ---------------------------------------------------------------------

    def _emit(
        self,
        signal: str,
        event: Event,
        summary: str,
        targets: list[Target],
        *,
        excerpt: str = "",
        weight: float = 1.0,
        interval: tuple[float, float] | None = None,
        detail: dict[str, str] | None = None,
    ) -> Finding:
        finding = Finding(
            signal=signal,
            session_id=self.stream.session_id,
            source=str(self.stream.source),
            line=event.line,
            ts=event.ts,
            summary=summary,
            excerpt=excerpt,
            targets=targets,
            weight=weight,
            interval=interval,
            stream=self.stream.key,
            detail=detail or {},
        )
        self.findings.append(finding)
        self.exposed.update((t.kind, t.key) for t in targets)
        self.state.emitted_at[id(finding)] = self.state.ok_actions
        if signal in _FILE_TROUBLE:
            self.state.turn_trouble_files.update(t.key for t in targets if t.kind == model.FILE)
        elif signal in _TROUBLE:
            self.state.turn_trouble = True
        return finding

    def _label(self, key: str, step: Step | None, cwd: str | None) -> str:
        """How a command is named in the result: its key, and where in the repo it ran.

        ``npm test`` in ``web/`` and in ``api/`` of a monorepo are different commands:
        ``npm test (web/)``. At the repo root the key stands alone.
        """
        name = self.ctx.command_key(key)
        if key.split(" ", 1)[0] not in DIRECTORY_TOOLS and command_role(key) is None:
            return name  # `git push`, `curl`, `docker ps`: the same thing wherever it runs
        here = self._where(step, cwd)
        rel = self.ctx.resolver.resolve(here, None) if here else None
        return name + (f" ({rel}/)" if rel else "")

    def _primary(self, action: _Action) -> Step | None:
        cmd = action.cmd
        return next((s for s in cmd.steps if s.key == cmd.key), None) if cmd else None

    def _action_targets(self, action: _Action) -> list[Target]:
        if action.file is not None:
            return [Target(model.FILE, action.file)]
        if action.cmd is not None and action.cmd.key:
            label = self._label(action.cmd.key, self._primary(action), action.call.cwd)
            return [Target(model.COMMAND, label)]
        return []

    def _round_trip(self, action: _Action) -> tuple[float, float] | None:
        """From a call until the next transcript event: the cost of one wasted round trip."""
        end = action.next_any if action.next_any is not None else _epoch(action.result)
        return _capped(_epoch(action.call), end)

    def _recover(self, finding: Finding, action: _Action) -> None:
        """Extend a failure's interval up to the successful ``action`` that resolved it."""
        if finding.interval is None:
            return
        if self.state.emitted_at.get(id(finding)) != self.state.ok_actions:
            # Something else already succeeded in between: the agent had moved on, and
            # the failure cost one round trip, not everything until it came back.
            return
        end = _epoch(action.result) or _epoch(action.call)
        extended = _capped(finding.interval[0], end)
        if extended is not None and extended[1] > finding.interval[1]:
            finding.interval = extended

    def _drop(self, ids: set[int], reason: str = "") -> None:
        """Remove findings (by ``id``) from the record and from every pending list.

        With a ``reason``, failed commands among them are counted as red runs that were
        seen and deliberately not reported.
        """
        if not ids:
            return
        state = self.state
        if reason:
            for finding in self.findings:
                if id(finding) in ids and finding.signal == COMMAND_FAILURE:
                    low, high = finding.run or (0.0, 0.0)
                    self.dropped.append((finding.line, max(0.0, high - low), reason))
        self.findings = [f for f in self.findings if id(f) not in ids]
        state.pending = [p for p in state.pending if id(p[0]) not in ids]
        state.missing = [m for m in state.missing if id(m[0]) not in ids]
        for table in (state.red, state.red_then_edited, state.red_then_ran, state.replaced):
            for name in list(table):
                table[name] = [f for f in table[name] if id(f) not in ids]
                if not table[name]:
                    del table[name]

    # ---- pairing ---------------------------------------------------------------------

    def _where(self, step: Step | None, cwd: str | None) -> str | None:
        """Directory ``step`` runs in: ``cwd`` after its ``cd``s, as far as they can be followed."""
        if step is None or not step.moves:
            return cwd
        return self.ctx.resolver.chdir(cwd, step.moves) or cwd

    def _elsewhere(self, step: Step | None, cwd: str | None) -> bool:
        """Does ``step`` run, or only act, outside the repository?

        ``cd /tmp/scratch && python repro.py``, ``git -C ~/other status`` and
        ``rm -rf /var/tmp/build`` say nothing about this repo.
        """
        if step is None:
            return False
        resolver = self.ctx.resolver
        if step.moves:
            there = resolver.chdir(cwd, step.moves)
            if there is not None and not resolver.contains(there):
                return True
            cwd = there or cwd
        words = [step.program, *step.args]
        for index, word in enumerate(words[:-1]):
            name = word.rsplit("/", 1)[-1]
            if name in INTERPRETERS or _PYTHON_NAME_RE.match(name):
                script = words[index + 1]
                # `python3 /tmp/scratch/repro.py`, `uv run python ~/x.py`: a script that
                # lives outside the repo
                if not script.startswith("-") and "/" in script and resolver.outside(script, cwd):
                    return True
                break
        if step.program in HOUSEKEEPING_PROGRAMS | DESTRUCTIVE_PROGRAMS:
            paths = [a for a in step.args if not a.startswith("-")]
            return bool(paths) and all(resolver.outside(path, cwd) for path in paths)
        return False

    def _classify(self, action: _Action) -> None:
        """Resolve what a call is about: a repo file, a command, or a search scope."""
        event = action.call
        resolver = self.ctx.resolver
        if event.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE):
            rel = resolver.resolve(event.path, event.cwd)
            action.file = rel or None
            if action.file:
                self.exposed.add((model.FILE, action.file))
        elif event.op == model.OP_SHELL and event.command:
            parsed = parse_command(event.command)
            primary = next((s for s in parsed.steps if s.key == parsed.key), None)
            # ``cd /abs/repo && make`` from a session started in a parent directory runs
            # in the repo: what counts is where the step runs, not where the shell began.
            inside = resolver.contains(self._where(primary, event.cwd))
            if inside and parsed.key and not self._elsewhere(primary, event.cwd):
                action.cmd = parsed
                if not parsed.inspection:
                    label = self._label(parsed.key, primary, event.cwd)
                    self.exposed.add((model.COMMAND, label))
        elif event.op == model.OP_SEARCH:
            scope = _scope(event, self.ctx)
            if scope is not None:
                is_file = bool(scope) and self.ctx.is_file(scope)
                self.exposed.add((model.FILE if is_file else model.DIR, scope or "."))

    def _items(self) -> list[_Action | Event]:
        items: list[_Action | Event] = []
        by_id: dict[str, _Action] = {}
        for event in self.stream.events:
            if event.kind == model.TOOL_CALL:
                action = _Action(call=event)
                try:
                    self._classify(action)
                except Exception:  # keep the call, without attribution
                    self.skipped_events += 1
                    action.file = action.cmd = None
                by_id[event.call_id] = action
                items.append(action)
            elif event.kind == model.TOOL_RESULT:
                paired = by_id.get(event.call_id)
                if paired is not None and paired.result is None:
                    paired.result = event
            else:
                items.append(event)
        # Time of the agent's next activity after each call. A human message ends the
        # agent's turn: whatever follows it is waiting, not work, and is never claimed.
        next_any: float | None = None
        for item in reversed(items):
            if isinstance(item, _Action):
                item.next_any = next_any
                next_any = _epoch(item.call) or next_any
            elif item.kind == model.AGENT_MESSAGE:
                next_any = _epoch(item) or next_any
            else:
                next_any = None
        return items

    # ---- main loop -------------------------------------------------------------------

    def run(self) -> list[Finding]:
        for item in self._items():
            try:
                self._step(item)
            except Exception:  # one odd event must never end the scan
                self.skipped_events += 1
        self._settle_correction()
        self._end_turn()
        state = self.state
        still_red = set(state.red) | set(state.red_then_edited) | set(state.red_then_ran)
        for finding in self.findings:
            if state.key_of.get(id(finding)) in still_red:
                finding.detail["never_green"] = "1"  # red when the session ended
        return self.findings

    def _end_turn(self) -> None:
        """Judge what can only be judged once a turn is over."""
        state = self.state
        # Many edits to a file with no command failing and nothing on that file missed or
        # taken back: that is just writing.
        calm = {
            id(finding)
            for path, finding in state.thrash.items()
            if not state.turn_trouble and path not in state.turn_trouble_files
        }
        self._drop(calm)
        if state.turn_shell_ok:
            for finding in state.thrash.values():
                finding.detail["ended_green"] = "1"
        for key, cycle in state.cycles.items():
            # If the last check of the turn was green and nothing went red after it, the
            # turn converged, whatever name the passing command went by.
            if state.turn_check_ok:
                break
            if cycle.count >= NO_CONVERGENCE_MIN_CYCLES and cycle.result is not None:
                self._no_convergence(key, cycle)
        state.cycles = {}
        state.turn_changes = state.changes
        state.thrash = {}
        state.turn_trouble = False
        state.turns += 1
        state.turn_trouble_files = set()
        state.turn_shell_ok = None
        state.turn_check_ok = None
        state.turn_misses = []

    def _no_convergence(self, key: str, cycle: _Cycle) -> None:
        assert cycle.result is not None
        ranked = sorted(cycle.files.items(), key=lambda kv: (-kv[1], kv[0]))
        files = [path for path, _ in ranked[:NO_CONVERGENCE_MAX_FILES]]
        targets = [Target(model.COMMAND, cycle.label)]
        targets += [Target(model.FILE, path, 1.0 / len(files)) for path in files]
        # Its last run showed neither red nor green (`pytest | tail -1` cut the verdict
        # off): it may well have gone green out of sight. Kept as a weak lead that claims no time.
        uncertain = cycle.unknown
        # Only the red runs themselves and the edits between them are claimed, newest
        # first up to the cap: reading and thinking in between may have been useful.
        spans: list[tuple[float, float]] = []
        budget = NO_CONVERGENCE_CAP_SECONDS
        for start, end in sorted(cycle.spans, reverse=True):
            if budget <= 0 or uncertain:
                break
            start = max(start, end - budget)
            spans.append((start, end))
            budget -= end - start
        where = f" (edits to {', '.join(files)})" if files else ""
        ending = (
            "its later runs showed neither red nor green" if uncertain else "the turn ended red"
        )
        finding = self._emit(
            NO_CONVERGENCE,
            cycle.result,
            f"`{cycle.label}` was still failing after {cycle.count} rounds of "
            f"edit and re-run{where}; {ending}",
            targets,
            excerpt=f"$ {cycle.command}\n{cycle.result.output}",
            detail={"count": str(cycle.count), "command": cycle.command},
        )
        finding.intervals = tuple(sorted(spans))
        if uncertain:
            finding.detail["uncertain"] = "1"

    def _mention(self, text: str) -> None:
        """Remember optional files that a prompt or a tool's output named."""
        if not text:
            return
        for match in _FILE_NAME_RE.finditer(text[:MENTION_SCAN_CHARS]):
            word = match.group(0)
            if PROBE_FILE_RE.search(word):
                self.state.mentioned.add(word.casefold())

    def _step(self, item: _Action | Event) -> None:
        state = self.state
        if isinstance(item, _Action):
            if state.rework is not None and state.rework.first is None:
                state.rework.start = _epoch(item.call)
                state.rework.first = self._round_trip(item)
            try:
                self._on_action(item)
            finally:
                if item.result is not None and item.result.ok:
                    state.ok_actions += 1
                if item.result is not None:
                    self._mention(item.result.output)
            state.turn.append(item)
        elif item.kind == model.AGENT_MESSAGE:
            state.agent_seen = _epoch(item) or state.agent_seen
        elif item.kind == model.INTERRUPT:
            self._on_interrupt(item)
        elif item.kind == model.USER_MESSAGE:
            self._mention(item.text)
            if not self.stream.sidechain:
                if not item.synthetic:
                    self._settle_correction()
                    if state.prompts > 0 and state.turn:
                        self._on_prompt(item)
                state.prompts += 1
            self._end_turn()
            state.turn = []
            state.agent_seen = None
            state.turn_edits = {}
            state.pending = []
            state.open_edits = {}

    def _on_action(self, action: _Action) -> None:
        result = action.result
        if result is None:
            return
        op = action.call.op
        if result.error == model.ERR_REJECTED:
            self._on_rejected(action, result)
        elif result.error == model.ERR_DENIED:
            self._on_denied(action, result)
        elif result.error in (model.ERR_INTERRUPTED, model.ERR_NOT_RUN):
            return  # it never ran (to the end): nothing to learn from it
        elif op in (model.OP_EDIT, model.OP_WRITE):
            self._on_edit(action, result)
        elif op == model.OP_READ:
            self._on_read(action, result)
        elif op == model.OP_SEARCH:
            self._on_search(action, result)
        elif op == model.OP_SHELL:
            self._on_shell(action, result)

    # ---- file tools ------------------------------------------------------------------

    def _on_edit(self, action: _Action, result: Event) -> None:
        path = action.file
        if path is None:
            return
        call = action.call
        targets = [Target(model.FILE, path)]
        interval = self._round_trip(action)
        before = len(self.findings)
        if not result.ok:
            if result.error == model.ERR_EDIT_NOT_FOUND:
                self._emit(
                    EDIT_MISS,
                    result,
                    f"{call.tool} on {path} failed: the text to replace was not found",
                    targets,
                    excerpt=result.output,
                    interval=interval,
                    detail={"variant": "not-found"},
                )
            elif result.error == model.ERR_EDIT_AMBIGUOUS:
                count = result.matches or 0
                self._emit(
                    EDIT_MISS,
                    result,
                    f"{call.tool} on {path} failed: the text to replace matched "
                    f"{count or 'several'} places",
                    targets,
                    excerpt=result.output,
                    interval=interval,
                    detail={"variant": "ambiguous", "matches": str(count)},
                )
            elif result.error in (model.ERR_NOT_READ, model.ERR_STALE):
                stale = result.error == model.ERR_STALE
                reason = (
                    "the file changed on disk after it was read"
                    if stale
                    else "the file had not been read first"
                )
                self._emit(
                    STALE_EDIT,
                    result,
                    f"{call.tool} on {path} refused: {reason}",
                    targets,
                    excerpt=result.output,
                    interval=interval,
                    detail={"variant": "modified" if stale else "not-read"},
                )
            elif result.error == model.ERR_MISSING:
                self._path_miss(action, result)
                return
            self.state.open_edits.setdefault(path, []).extend(self.findings[before:])
            return
        for finding in self.state.open_edits.pop(path, []):
            self._recover(finding, action)
        self._on_edit_success(action, result, path)

    def _on_edit_success(self, action: _Action, result: Event, path: str) -> None:
        state = self.state
        call = action.call
        now = _epoch(call)
        targets = [Target(model.FILE, path)]
        self._on_change(path, _span(action))
        if call.old_text:
            state.removed.append((call.old_text, call.new_text))
            del state.removed[:-REMOVED_TEXTS_KEPT]
        self._explain_probes(path, call)
        self._confirm_correction({path}, f"the agent then changed {path} again")
        self._note_rework({path}, action)
        if call.op == model.OP_WRITE:
            # Reading a file to see whether it exists, then creating it, is not a miss.
            self._drop({id(f) for f in state.missing_reads.pop(path, [])})
        # Creating the module a command could not import is test-first development: the
        # failure becomes red -> change, and is forgotten when the command goes green.
        for entry in [m for m in state.missing if provides(path, m[2], m[3])]:
            state.missing.remove(entry)
            state.red_then_edited.setdefault(entry[1], []).append(entry[0])
        # Any successful change resets read-loop tracking for the file.
        for key in [k for k in state.reads if k[0] == path]:
            del state.reads[key]
            state.read_findings.pop(key, None)
        # edit-thrash
        count = state.turn_edits.get(path, 0) + 1
        state.turn_edits[path] = count
        if count == EDIT_THRASH_MIN_EDITS:
            state.thrash[path] = self._emit(EDIT_THRASH, result, "", targets)
        if path in state.thrash:
            state.thrash[path].summary = f"{count} successful edits to {path} within one user turn"
            state.thrash[path].detail["count"] = str(count)
        # edit-revert
        reverted_at: float | None = None
        reverted = False
        history = state.edit_history.setdefault(path, {})
        for old, new in call.edits:
            if old == new:
                continue
            if (new, old) in history:
                made_at, commands_then, inserted = history.pop((new, old))
                if inserted and state.ok_commands > commands_then:
                    # Lines were only added, a command ran successfully with them in place,
                    # and now they are gone again: a temporary debug print, not a retreat.
                    continue
                reverted = True
                # Only within one turn is the time in between the detour itself; an undo
                # of work from an earlier turn costs the undoing call and nothing more.
                same_turn = state.edit_turns.pop((path, new, old), -1) == state.turns
                reverted_at = made_at if same_turn else None
            else:
                history[(old, new)] = (now, state.ok_commands, (old, new) in call.insertions)
                state.edit_turns[(path, old, new)] = state.turns
        if call.op == model.OP_WRITE and call.content_digest:
            versions = state.write_history.setdefault(path, [])
            if versions and versions[-1] != call.content_digest and call.content_digest in versions:
                reverted = True
                written = state.write_times.get((path, versions[-1]))
                same_turn = state.write_turns.get((path, versions[-1]), -1) == state.turns
                reverted_at = written if same_turn else None
            versions.append(call.content_digest)
            state.write_times[(path, call.content_digest)] = now
            state.write_turns[(path, call.content_digest)] = state.turns
        if reverted:
            self._emit(
                EDIT_REVERT,
                result,
                f"{call.tool} on {path} undid an earlier edit from the same session",
                targets,
                interval=_capped_back(reverted_at, now) or self._round_trip(action),
            )

    def _on_change(
        self,
        path: str = "",
        span: tuple[float, float] | None = None,
        certain: bool = True,
        by: tuple[str, bool] | None = None,
    ) -> None:
        """Files changed (an edit, a write, or a shell command that rewrites files).

        ``certain=False``: a command ran that *may* have changed something. It ends
        streaks, but it is not an edit: if the red runs before it go green with nothing
        but such commands in between, it was the environment that got fixed, not the
        code. ``by`` names the command and says whether it is a setup step.
        """
        state = self.state
        # Re-running a failing command three times with nothing changed is friction
        # whatever happens afterwards: a retry loop and its failures are never dropped.
        state.looped |= {id(f) for same in state.retry for f in state.streak.get(same, ())}
        if certain:
            state.changes += 1
            state.change_log.append((state.changes, path, span))
            # An edit turns the red runs so far into red -> edit -> (maybe) green.
            for table in (state.red, state.red_then_ran):
                for failed_key, findings in table.items():
                    fresh = [f for f in findings if id(f) not in state.looped]
                    if fresh:
                        state.red_then_edited.setdefault(failed_key, []).extend(fresh)
            state.red_then_ran = {}
        else:
            for failed_key, findings in state.red.items():
                state.red_then_ran.setdefault(failed_key, []).extend(findings)
            if by is not None:
                for findings in state.red_then_ran.values():
                    for finding in findings:
                        state.ran_after.setdefault(id(finding), []).append(by)
        state.red = {}
        # A change ends every "same command, nothing changed" streak.
        state.fail_runs.clear()
        state.retry.clear()
        state.streak.clear()

    def _explain_probes(self, path: str, call: Event) -> None:
        """Drop earlier empty searches that only checked a name was free before creating it."""
        explained: set[int] = set()
        for finding, search, scope in self.state.probes:
            if scope and path != scope and not path.startswith(scope + "/"):
                continue
            pattern = search.pattern or ""
            if search.tool.lower() == "glob":
                name = pattern.replace("\\", "/").rsplit("/", 1)[-1]
                hit = bool(name) and fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], name)
            else:
                hit = bool(pattern) and _text_matches(pattern, call.new_text)
            if hit:
                explained.add(id(finding))
        if explained:
            self.findings = [f for f in self.findings if id(f) not in explained]
            self.state.probes = [p for p in self.state.probes if id(p[0]) not in explained]

    def _path_miss(self, action: _Action, result: Event) -> None:
        if action.file is None:
            return
        # Looking whether a project has a README, a .env or an AGENTS.md is routine, unless
        # the human or some output said that the file is there.
        name = action.file.rsplit("/", 1)[-1].casefold()
        mentioned = self.state.mentioned
        probe = PROBE_FILE_RE.search(action.file) is not None and not (
            name in mentioned or action.file.casefold() in mentioned
        )
        finding = self._emit(
            PATH_MISS,
            result,
            f"{action.call.tool} targeted {action.file}, which does not exist"
            + (" (a routine look for an optional file)" if probe else ""),
            [Target(model.FILE, action.file)],
            excerpt=result.output,
            interval=None if probe else self._round_trip(action),
            detail={"probe": "1"} if probe else None,
        )
        if action.call.op == model.OP_READ:
            self.state.missing_reads.setdefault(action.file, []).append(finding)

    def _on_read(self, action: _Action, result: Event) -> None:
        path = action.file
        if path is None:
            return
        if not result.ok:
            if result.error == model.ERR_MISSING:
                self._path_miss(action, result)
            elif result.error == model.ERR_TOO_LARGE:
                self._emit(
                    FILE_TOO_LARGE,
                    result,
                    f"{action.call.tool} of {path} was refused: the file is too large to "
                    "read in one piece",
                    [Target(model.FILE, path)],
                    excerpt=result.output,
                    interval=self._round_trip(action),
                )
            return
        state = self.state
        key = (path, action.call.read_range)
        count = state.reads.get(key, 0) + 1
        state.reads[key] = count
        if count == READ_LOOP_MIN_READS:
            state.read_findings[key] = self._emit(
                READ_LOOP, action.call, "", [Target(model.FILE, path)]
            )
        if key in state.read_findings:
            finding = state.read_findings[key]
            finding.summary = f"{path} was read {count} times with no edit in between"
            finding.detail["count"] = str(count)

    def _on_search(self, action: _Action, result: Event) -> None:
        call = action.call
        wanted = (call.pattern or "").casefold()
        if result.ok and not result.empty and wanted:
            # Found after all, elsewhere, in the same turn: the misses were the search.
            found = {id(f) for f, pattern in self.state.turn_misses if pattern == wanted}
            if found:
                self._drop(found, DROPPED_DEV_LOOP)
                self.state.probes = [p for p in self.state.probes if id(p[0]) not in found]
                self.state.turn_misses = [
                    m for m in self.state.turn_misses if id(m[0]) not in found
                ]
        if not (result.ok and result.empty):
            return
        scope = _scope(call, self.ctx)
        if scope is None:
            return
        pattern = call.pattern or ""
        if (
            pattern
            and call.tool.lower() != "glob"
            and any(
                _text_matches(pattern, old) and not _text_matches(pattern, new)
                for old, new in self.state.removed
            )
        ):
            return  # the agent took this text out itself: the search confirms it is gone
        is_file = bool(scope) and self.ctx.is_file(scope)
        where = scope or "the repo root"
        finding = self._emit(
            SEARCH_MISS,
            result,
            f"{call.tool} search in {where} returned nothing",
            [Target(model.FILE if is_file else model.DIR, scope or ".")],
            excerpt=f"pattern: {call.pattern}" if call.pattern else "",
            # a weak signal: it may be a lead, but it claims no time
            detail={"pattern": call.pattern or "", "scope": scope or "."},
        )
        self.state.probes.append((finding, call, scope))
        if wanted:
            self.state.turn_misses.append((finding, wanted))

    # ---- shell -----------------------------------------------------------------------

    def _on_shell(self, action: _Action, result: Event) -> None:
        cmd = action.cmd
        if cmd is None:
            return
        # Any command that ran and is not itself a check (a script, a generator, an
        # install, a commit) may have changed something. That is enough to end a "nothing
        # changed in between" streak and to make a later green run a fix; it is not
        # counted as an edit, and no file is named.
        before: list[Step] = []
        after: list[Step] = []
        edit = result.ok and is_shell_edit(cmd, action.call.command or "")
        # `cp .env.example .env` writes a file, but it sets up the environment
        template = result.ok and any(env_copy(step) for step in cmd.steps)
        if template and not is_shell_edit(
            replace(cmd, steps=tuple(s for s in cmd.steps if not env_copy(s))), ""
        ):
            edit = False
        ran = not cmd.inspection and result.error in ("", model.ERR_EXIT, model.ERR_OTHER)
        if ran and not edit:
            judged = _candidates(cmd)
            steps = [s for s in cmd.steps if s in judged or env_copy(s)]
            if not result.ok:
                # in a failed line, only what demonstrably ran before the step that failed
                blamed, sure = self._blamed_step(cmd, result.output)
                steps = steps[: steps.index(blamed)] if sure and blamed in steps else []
            # `python fix.py && pytest`: the script ran before the check on the same line.
            # The command the line is judged by is never its own fix, so it counts after.
            at = next((i for i, step in enumerate(steps) if step.key == cmd.key), len(steps))
            before, after = steps[:at], steps[at:]
        for step in before:
            self._possibly_changed(step, action.call.cwd)
        self._judge_shell(action, cmd, result, edit)
        for step in after:
            self._possibly_changed(step, action.call.cwd)

    def _possibly_changed(self, step: Step, cwd: str | None) -> None:
        """``step`` ran and may have changed something (see ``may_change_files``)."""
        copied = env_copy(step)
        setup = copied is not None or is_setup_step(step)
        if not setup and not may_change_files(step.key):
            return
        # Version control records work, it does not make a failing command pass.
        named = step.program not in ("git", "gh", "glab")
        label = copied if copied and self.ctx.command_key is str else None
        label = label or self._label(step.key, step, cwd)
        self._on_change(certain=False, by=(label, setup) if named else None)

    def _judge_shell(self, action: _Action, cmd: ParsedCommand, result: Event, edit: bool) -> None:
        state = self.state
        key = cmd.key
        located: Step | None = self._primary(action)

        def shown(name: str) -> str:
            return self._label(name, located, action.call.cwd)

        state.pending = [(f, k, n - 1) for f, k, n in state.pending if n > 1]
        if edit:
            self._on_change()  # `sed -i`, a formatter, `--fix`: a change like any edit
        # `pytest | tail -5` exits with the status of `tail`. Exit 0 is then no verdict:
        # the output decides, and where it does not, the run is neither red nor green.
        ok = result.ok
        verdict: str | None = None
        hidden = result.ok and cmd.masked and not cmd.inspection
        ran = not cmd.inspection and result.error in ("", model.ERR_EXIT, model.ERR_OTHER)
        # In `ruff check .; mypy; pytest` every step prints its own verdict. Each is judged
        # by its own summary line; one red step does not make the others red.
        several = len({s.key for s in _candidates(cmd)}) > 1
        own = step_verdicts(cmd, result.output) if ran and several else {}
        red_step: Step | None = None
        setup_error: bool | None = None  # for a hidden status: was it a setup error?
        if hidden and several:
            # Nothing says that anything failed at all, so a step is red only on its own
            # evidence: its summary line, or a setup error that can be pinned on it.
            red_step = next((s for s in _candidates(cmd) if own.get(s.key) == "red"), None)
            setup_error = False
            if red_step is None and is_invocation_error(result.output, None, self.ctx.first_party):
                blamed, sure = self._blamed_step(cmd, result.output)
                if sure and blamed is not None and own.get(blamed.key) != "green":
                    red_step, setup_error = blamed, True
            verdict = "red" if red_step is not None else own.get(key)
            ok = verdict != "red"
        elif hidden:
            verdict = output_verdict(result.output, self.ctx.first_party)
            ok = verdict != "red"
        unknown = hidden and verdict is None
        if ran:
            # A step that printed its own passing summary ran green, whatever came after.
            for step_key, step_verdict in own.items():
                if step_verdict == "green" and (step_key != key or not ok or unknown):
                    self._went_green(step_key)
            if result.ok:
                # ... and so did any check whose passing summary an exit-0 command shows
                # (`make check` printing "All checks passed!" for an earlier red `ruff`).
                for failed_key in [k for k in state.cycles if k != key]:
                    if key_verdict(failed_key, result.output) == "green":
                        self._went_green(failed_key)
        if unknown and key in state.cycles:
            state.cycles[key].unknown = True
        if not cmd.inspection and result.error != model.ERR_INTERRUPTED and not unknown:
            state.turn_shell_ok = ok
            if command_role(key) is not None or is_aggregate_check(key):
                state.turn_check_ok = ok
        if ok:
            if not cmd.inspection:
                state.ok_commands += 1
                self._shell_undo(action)
            if unknown:
                return  # unknown outcome: it closes nothing and recovers nothing
            # The very same command line failed repeatedly and now passes, and nothing was
            # changed or installed in between: the agent was waiting for something (CI, a
            # server starting), which is not friction.
            argv = normalize_argv(action.call.command or "")
            waited: set[int] = set()
            for same in [k for k in state.streak if k[1] == argv]:
                streak = state.streak.pop(same)
                if len(streak) >= POLL_MIN_FAILURES:
                    waited |= {id(f) for f in streak}
                    if same in state.retry:
                        waited.add(id(state.retry[same]))
            self._drop(waited, DROPPED_WAITING)
            if is_setup_command(key):
                state.streak.clear()  # whatever passes after an install was not just waiting
            state.fail_runs = {k: v for k, v in state.fail_runs.items() if k[0] != key}
            state.retry = {k: v for k, v in state.retry.items() if k[0] != key}
            # red -> edit -> green is the development loop working: forget those failures,
            # also when the green run is another command doing the same job (`make test`
            # after a red `pytest tests/test_a.py`). A setup step is nobody's stand-in:
            # `npx playwright install` going well says nothing about `npm run e2e`.
            self._went_green(key, exact=located is not None and is_setup_step(located))
            # The command works after all, so nothing "replaced" it earlier in the session.
            for finding in state.replaced.pop(key, []):
                finding.detail.pop("recovered_by", None)
            if cmd.inspection:
                return
            waiting: list[tuple[Finding, str, int]] = []
            for finding, failed_key, left in state.pending:
                if failed_key == key:
                    setup = state.setup_after.pop(id(finding), "")
                    if setup:
                        finding.detail.setdefault("fixed_by_setup", setup)
                    # The same command, somewhere else or under another name
                    label = shown(key)
                    was = finding.targets[0].key if finding.targets else label
                    if label != was and label.startswith(was + " ("):
                        finding.detail["works_in"] = label[len(was) + 2 : -1]
                    unfound = finding.detail.get("not_found", "")
                    other = re.search(
                        rf"(?:^|[\s;&|(/]){re.escape(unfound)}(\d[\d.]*)(?=\s|$)",
                        action.call.command or "",
                    )
                    if unfound and other is not None:
                        finding.detail["works_as"] = unfound + other.group(1)
                    self._recover(finding, action)
                elif finding.detail["variant"] != "ordinary" and is_typo_of(failed_key, key):
                    # `npm run biuld`, then `npm run build`: a slip of the keyboard that
                    # the agent corrected itself. Nothing to document.
                    finding.detail["typo_of"] = shown(key)
                    finding.summary += f"; a mistyped `{shown(key)}`, which then ran"
                elif is_setup_command(key):
                    state.setup_after[id(finding)] = shown(key)
                    waiting.append((finding, failed_key, left))
                elif is_replacement(failed_key, key) and finding.detail["variant"] != "ordinary":
                    # Only a command that could not run can be "replaced". A red test
                    # followed by another green test command is just a test going green.
                    finding.detail["recovered_by"] = shown(key)
                    state.replaced.setdefault(failed_key, []).append(finding)
                    self._recover(finding, action)
                else:  # an unrelated command (git, housekeeping, another job) fixes nothing
                    waiting.append((finding, failed_key, left))
            state.pending = waiting
            return
        code = None if hidden else result.exit_code
        if cmd.inspection:
            return  # probing (ls, cat, git status, ...) is not friction
        if code == 1 and cmd.last_program in MATCH_FILTERS and cmd.last_program != cmd.program:
            return  # `... | grep x` with nothing matched
        call = action.call
        timed_out = HARNESS_TIMEOUT_RE.match(result.output) is not None
        if result.harness_error and code is None and not timed_out:
            return  # the harness's own error message: the command was never executed
        invocation = is_invocation_error(result.output, code, self.ctx.first_party)
        step, certain = self._blamed_step(cmd, result.output)
        if red_step is not None:
            step, certain = red_step, True
            invocation = bool(setup_error)
        if self._elsewhere(step, call.cwd):
            return  # the step that failed ran, or only touched things, outside the repo
        if step is not None and step.key:
            key = step.key
            located = step
        inline = is_inline_code(key)
        if inline and code not in INVOCATION_EXIT_CODES:
            # Ad-hoc code failing says something about that code, not about how to run
            # anything here. Only a missing interpreter is a setup problem.
            invocation = False
        hook_fixed = False
        if key == "git commit":
            if NOTHING_TO_COMMIT_RE.search(result.output):
                return  # nothing was staged: a no-op, not a failure
            hook_fixed = HOOK_MODIFIED_RE.search(result.output) is not None
            if hook_fixed:
                self._on_change()  # the hook rewrote files; committing again is the routine
        tokens = step.args if step is not None else cmd.tokens
        targets = [Target(model.COMMAND, shown(key))]
        # A setup/invocation error says nothing about the files on the command line, and
        # neither does a failure we cannot pin on one step.
        environment = None if invocation or inline else environment_error(result.output)
        blameless = invocation or not certain or environment is not None
        for candidate in () if blameless else path_candidates(tokens):
            rel = self.ctx.resolver.resolve(candidate, self._where(step, call.cwd))
            if not (rel and self.ctx.is_known_file(rel)):
                rel = self.ctx.resolver.resolve(candidate, call.cwd)
            if rel and self.ctx.is_known_file(rel) and Target(model.FILE, rel) not in targets:
                targets.append(Target(model.FILE, rel))
            if len(targets) > COMMAND_MAX_FILES:
                break
        status = f"exit {code}" if code is not None else "error"
        if hidden:
            status = "judged from its output: the exit status was that of a pipe or a later step"
        kind = "setup/invocation error" if invocation else "ordinary failure"
        if environment is not None:
            kind = {
                ENV_SERVICE: f"{environment[1] or 'a service it needs'} was not reachable",
                ENV_AUTH: "credentials were missing or expired",
                ENV_VARIABLE: "an environment variable was not set",
            }[environment[0]]
        summary = f"`{shown(key)}` failed ({status}; {kind})"
        if not certain:
            summary = (
                f"A chain of commands ending in `{shown(key)}` failed ({status}; {kind}); "
                "the output does not show which step, so no file is blamed"
            )
        detail = {
            "variant": "invocation" if invocation else "ordinary",
            "command": call.command or "",
        }
        if environment is not None:
            detail["variant"] = "environment"
            detail["environment"] = environment[0]
            if environment[0] == ENV_SERVICE and environment[1]:
                detail["service"] = environment[1]
            elif environment[0] == ENV_VARIABLE:
                detail["env_var"] = environment[1]
        gone = _NOT_FOUND_PROGRAM_RE.search(result.output) if invocation else None
        if gone is not None:
            name = (gone.group(1) or gone.group(2)).rsplit("/", 1)[-1]
            if step is not None and name == step.program:
                detail["not_found"] = name
        if not certain:
            detail["ambiguous_step"] = "1"
        if hidden:
            detail["status"] = "hidden"
        if inline:
            detail["inline"] = "1"
        if timed_out:
            detail["timeout"] = "1"
            if DEV_SERVER_RE.search(call.command or ""):
                detail["server"] = "1"
                summary = (
                    f"`{shown(key)}` ran until the harness timed it out: it is a server or "
                    "watcher that never exits on its own"
                )
        finding = self._emit(
            COMMAND_FAILURE,
            result,
            summary,
            targets,
            excerpt=f"$ {call.command}\n{result.output}",
            weight=1.0 if invocation or environment else ORDINARY_FAILURE_WEIGHT,
            # An ordinary failure (a red test) is information, not lost time.
            interval=self._round_trip(action) if invocation or environment else None,
            detail=detail,
        )
        finding.run = _capped(_epoch(call), _epoch(result))
        state.key_of[id(finding)] = key
        state.pending.append((finding, key, RECOVERY_LOOKAHEAD))
        if hook_fixed:
            state.red_then_edited.setdefault(key, []).append(finding)
        elif not invocation:
            state.red.setdefault(key, []).append(finding)
            if not inline:
                self._red_run(key, shown(key), action, result, hidden)
        else:
            missing = missing_name(result.output)
            if missing is not None:
                state.missing.append((finding, key, *missing))
        # The same command line again, not a narrowed or otherwise changed invocation.
        same = (key, normalize_argv(call.command or ""))
        run = state.fail_runs.get(same, 0) + 1
        state.fail_runs[same] = run
        state.streak.setdefault(same, []).append(finding)
        if run == RETRY_LOOP_MIN_FAILURES:
            state.retry[same] = self._emit(
                RETRY_LOOP, result, "", [Target(model.COMMAND, shown(key))]
            )
        if same in state.retry:
            state.retry[same].summary = (
                f"`{shown(key)}` was run {run} times in a row with identical arguments "
                "and failed each time"
            )
            state.retry[same].detail["count"] = str(run)

    @staticmethod
    def _blamed_step(cmd: ParsedCommand, output: str) -> tuple[Step | None, bool]:
        """The step of a line that failed, and whether the output makes that certain."""
        # In `a && b` the output tells which step failed; b may never have run.
        step, certain = failing_step(cmd, output)
        # ... and a shell that could not find a program names it, wherever it stood.
        unfound = _NOT_FOUND_PROGRAM_RE.search(output)
        if unfound is not None:
            name = (unfound.group(1) or unfound.group(2)).rsplit("/", 1)[-1]
            named = next((s for s in cmd.steps if s.key and name in (s.program, s.key)), None)
            if named is not None:
                step, certain = named, True
        return step, certain

    def _went_green(self, key: str, exact: bool = False) -> None:
        """``key`` ran green: close what a green run of it, or of its job, closes.

        red -> edit -> green is the development loop working, so those failures are
        forgotten, also when the green run is another command doing the same job
        (`make test` after a red `pytest tests/test_a.py`) or an aggregate target that
        runs every check (`make check`, `npm run ci`, `pre-commit run`).
        """
        state = self.state

        def closes(failed_key: str) -> bool:
            if failed_key == key:
                return True
            if exact:
                return False
            if is_replacement(failed_key, key):
                return True
            return is_aggregate_check(key) and command_role(failed_key) is not None

        for failed_key in [k for k in state.cycles if closes(k)]:
            del state.cycles[failed_key]
        fixed: set[int] = set()
        for failed_key in [k for k in state.red_then_edited if closes(k)]:
            fixed |= {id(f) for f in state.red_then_edited.pop(failed_key)}
        # Green after nothing but commands, with no file edited: what was wrong was the
        # environment, and the command that put it right is worth writing down.
        for failed_key in [k for k in state.red_then_ran if closes(k)]:
            for finding in state.red_then_ran.pop(failed_key):
                ran = state.ran_after.pop(id(finding), [])
                setups = list(dict.fromkeys(label for label, setup in ran if setup))
                others = [label for label, setup in ran if not setup]
                if setups:
                    # ... and the files on its command line were not what was wrong
                    finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
                    finding.detail["fixed_by_setup"] = " && ".join(setups)
                    finding.weight = 1.0
                    finding.interval = finding.interval or finding.run
                    finding.summary += "; it passed after a setup step, with no file edited"
                elif id(finding) in state.looped:
                    continue  # stays as it is
                elif others:
                    # A script, a generator, a build: perhaps the fix, perhaps a
                    # coincidence. Whether it recurs across sessions decides (engine).
                    finding.detail["fixed_by_command"] = others[-1]
                else:
                    fixed.add(id(finding))
        # Green with no change in between: those red runs stay on the record for good.
        for failed_key in [k for k in state.red if closes(k)]:
            del state.red[failed_key]
        self._drop(fixed, DROPPED_DEV_LOOP)

    def _red_run(
        self, key: str, label: str, action: _Action, result: Event, hidden: bool = False
    ) -> None:
        """Count a red run that followed a change: one more round that did not converge."""
        state = self.state
        cycle = state.cycles.get(key)
        fresh = cycle is None
        if cycle is None:
            cycle = state.cycles[key] = _Cycle(seen=state.turn_changes)
        if state.changes > cycle.seen:
            cycle.count += 1
            for number, path, span in reversed(state.change_log):
                if number <= cycle.seen:
                    break
                if path:
                    cycle.files[path] = cycle.files.get(path, 0) + 1
                if span is not None and not fresh:
                    cycle.spans.append(span)  # an edit between two red runs
        cycle.seen = state.changes
        run = _span(action)
        if run is not None:
            cycle.spans.append(run)
        cycle.judged = cycle.judged or hidden
        cycle.unknown = False
        cycle.result = result
        cycle.command = action.call.command or ""
        cycle.label = label

    def _shell_undo(self, action: _Action) -> None:
        """A successful shell command that changes or restores files a correction was about."""
        pending = self.state.unconfirmed
        cmd = action.cmd
        if pending is None or cmd is None:
            return
        command = action.call.command or ""
        named = {
            rel
            for rel in (
                self.ctx.resolver.resolve(c, action.call.cwd) for c in path_candidates(cmd.tokens)
            )
            if rel
        }
        hit = named & pending.files
        whole_tree = cmd.key in _GIT_UNDO_KEYS and not named
        changes = (
            cmd.key in _GIT_UNDO_KEYS
            or cmd.key == "git checkout"
            or cmd.program in ("rm", "mv")
            or is_shell_edit(cmd, command)
        )
        if changes and (hit or whole_tree):
            what = sorted(hit)[0] if hit else "the working tree"
            self._confirm_correction(
                hit or pending.files,
                f"the agent then ran `{self.ctx.command_key(cmd.key)}` on {what}",
            )
            self._note_rework(hit or pending.files, action)

    # ---- human signals ---------------------------------------------------------------

    @staticmethod
    def _correction_summary(confidence: float, matched: str, note: str, how: str) -> str:
        what = "User correction" if confidence >= CORRECTION_STRONG else "Probable correction"
        return f"{what} (confidence {confidence:g}; matched {matched}; {note}); {how}"

    def _note_rework(self, paths: set[str] | frozenset[str], action: _Action) -> None:
        """The agent changed files again that the last correction was about."""
        rework = self.state.rework
        if rework is not None and rework.files & paths:
            rework.end = _epoch(action.result) or _epoch(action.call) or rework.end

    def _confirm_correction(self, paths: set[str] | frozenset[str], note: str) -> None:
        """The agent acted on files a strongly worded correction was about."""
        pending = self.state.unconfirmed
        if pending is None or not (pending.files & paths):
            return
        pending.note = pending.note or note
        if len(paths) < len(pending.files) or len(pending.files) == 1:
            # named files only: an undo of the whole working tree singles none of them out
            pending.followed.extend(sorted((pending.files & paths) - set(pending.followed)))

    def _settle_correction(self) -> None:
        """The turn after a strongly worded correction is over: did the agent act on it?"""
        rework, self.state.rework = self.state.rework, None
        self._settle_wording()
        if rework is not None and rework.finding.signal == USER_CORRECTION:
            # A correction costs the rework it caused: from the agent's first call after
            # it to its last change to the files in question. The turn that was corrected
            # may have been mostly fine, so none of it is claimed.
            again = _capped(rework.start, rework.end) if rework.end is not None else None
            rework.finding.interval = again or rework.first

    def _settle_wording(self) -> None:
        pending = self.state.unconfirmed
        if pending is None:
            return
        self.state.unconfirmed = None
        finding = pending.finding
        if pending.note:
            how = pending.how
            if pending.followed:
                # The files the agent went back to are the ones the correction was about.
                back = pending.followed[:CORRECTION_MAX_FILES]
                finding.targets = [Target(model.FILE, rel, 1.0 / len(back)) for rel in back]
                how = "attributed to " + ", ".join(back)
            finding.summary = self._correction_summary(
                CORRECTION_STRONG, pending.matched, pending.note, how
            )
            finding.detail["corroborated"] = "followed"
            return
        finding.signal = PROBABLE_CORRECTION
        finding.excerpt = ""  # a guess is never put in anyone's mouth
        finding.detail.pop("quote", None)
        finding.detail["confidence"] = f"{CORRECTION_PROBABLE:g}"
        finding.detail["corroborated"] = "no"
        finding.summary = self._correction_summary(
            CORRECTION_PROBABLE,
            pending.matched,
            "not corroborated: the agent did not change those files again",
            pending.how,
        )

    def _on_rejected(self, action: _Action, result: Event) -> None:
        call = action.call
        if QUESTION_TOOL_RE.search(call.tool):
            return  # a plan or a question put to the human: "no" is an answer, not a veto
        targets = self._action_targets(action)
        if call.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE) and not targets:
            return  # a file outside the repo
        if call.op == model.OP_SHELL:
            if action.cmd is None:
                return
            # Name the step the human objected to (`rm`, `git push`), not the last one.
            step = objectionable_step(action.cmd, result.reason)
            if self._elsewhere(step, call.cwd):
                return
            if step is not None and step.key:
                targets = [Target(model.COMMAND, self._label(step.key, step, call.cwd))]
        what = targets[0].key if targets else call.tool
        what = f"`{what}`" if targets and targets[0].kind == model.COMMAND else what
        excerpt_parts = []
        if call.op == model.OP_SHELL and call.command:
            excerpt_parts.append(f"$ {call.command}")
        if result.reason:
            excerpt_parts.append(f"user said: {result.reason}")
        self._emit(
            TOOL_REJECTED,
            result,
            f"The user declined {call.tool} on {what}" + (" and said why" if result.reason else ""),
            targets,
            excerpt="\n".join(excerpt_parts),
            interval=self._round_trip(action),
            detail={"reason": result.reason, "command": call.command or ""},
        )

    def _on_denied(self, action: _Action, result: Event) -> None:
        """A permission rule, hook or safety check refused the call: it never ran."""
        call = action.call
        targets = self._action_targets(action)
        if call.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE) and not targets:
            return  # a file outside the repo
        if call.op == model.OP_SHELL:
            if action.cmd is None:
                return
            # The harness quotes the whole command line in its message; only words it adds
            # can say which step was the problem. Otherwise: the destructive step.
            said = result.output.replace(call.command or "\0", " ")
            step = objectionable_step(action.cmd, said)
            if self._elsewhere(step, call.cwd):
                return
            if step is not None and step.key:
                targets = [Target(model.COMMAND, self._label(step.key, step, call.cwd))]
        by = {"hook": "a hook", "blocked": "the harness"}.get(result.reason, "permission settings")
        what = targets[0].key if targets else call.tool
        what = f"`{what}`" if targets and targets[0].kind == model.COMMAND else what
        excerpt = [f"$ {call.command}"] if call.op == model.OP_SHELL and call.command else []
        self._emit(
            PERMISSION_DENIED,
            result,
            f"{call.tool} on {what} was denied by {by}; it did not run",
            targets,
            excerpt="\n".join([*excerpt, result.output]),
            interval=self._round_trip(action),
            detail={"variant": result.reason or "permission", "command": call.command or ""},
        )

    def _turn_bounds(self) -> tuple[float | None, float | None]:
        turn = self.state.turn
        start = next((s for s in (_epoch(a.call) for a in turn) if s is not None), None)
        stamps = [s for a in turn for s in (_epoch(a.call), _epoch(a.result)) if s is not None]
        return start, max(stamps) if stamps else None

    def _on_interrupt(self, event: Event) -> None:
        turn = self.state.turn
        if not turn:
            return
        last = turn[-1]
        if last.result is not None and last.result.error == model.ERR_REJECTED:
            return  # already counted as tool-rejected
        targets = self._action_targets(last)
        if not targets:
            if last.call.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE, model.OP_SHELL):
                return  # the agent was working outside the repo
            what = f"running {last.call.tool}"
        elif targets[0].kind == model.COMMAND:
            # Stopping a running command is about the work in progress, not about the
            # command: the event goes to what the agent had been changing, if anything.
            what = f"running `{targets[0].key}`"
            edited: list[str] = []
            for action in reversed(turn):
                changed = action.call.op in (model.OP_EDIT, model.OP_WRITE)
                if changed and action.file and action.file not in edited:
                    edited.append(action.file)
            edited = edited[:CORRECTION_MAX_FILES]
            targets = [Target(model.FILE, rel, 1.0 / len(edited)) for rel in edited]
            if edited:
                what += " after edits to " + ", ".join(edited)
        else:
            what = f"working on {targets[0].key}"
        # Only the call that was cut short is claimed: the turn up to it may have been fine.
        self._emit(
            USER_INTERRUPT,
            event,
            f"The user interrupted the agent while it was {what}",
            targets,
            interval=_capped(_epoch(turn[-1].call), _epoch(event)),
        )

    def _on_prompt(self, event: Event) -> None:
        _, finished = self._turn_bounds()
        if self.state.agent_seen is not None:
            finished = max(finished or 0.0, self.state.agent_seen)
        asked = _epoch(event)
        idle = asked - finished if asked is not None and finished is not None else None
        confidence, labels = correction_confidence(event.text, event.after_question, idle)
        if confidence <= 0:
            return
        touched: list[str] = []  # most recent first
        edited: list[str] = []
        for action in reversed(self.state.turn):
            if action.file is None:
                continue
            if action.file not in touched:
                touched.append(action.file)
            if action.call.op in (model.OP_EDIT, model.OP_WRITE) and action.file not in edited:
                edited.append(action.file)
        named: list[str] = []
        for candidate in path_candidates(event.text[:CORRECTION_SCAN_CHARS].split()):
            rel = self.ctx.resolver.resolve(candidate, event.cwd)
            hits = [rel] if rel in touched else []
            if not hits and "/" not in candidate and "\\" not in candidate:
                by_name = [f for f in touched if f.rsplit("/", 1)[-1] == candidate]
                hits = by_name if len(by_name) == 1 else []
            named.extend(h for h in hits if h not in named)
        targets: list[Target]
        edited_all = list(edited)
        if named:
            targets = [Target(model.FILE, rel) for rel in named[:CORRECTION_MAX_FILES]]
            how = "names " + ", ".join(t.key for t in targets)
        elif edited:
            edited = edited[:CORRECTION_MAX_FILES]
            targets = [Target(model.FILE, rel, 1.0 / len(edited)) for rel in edited]
            how = "after edits to " + ", ".join(edited)
        else:
            targets = []
            how = "no file edited in the preceding turn, so not attributed to a path"
        matched = ", ".join(f'"{label}"' for label in labels)
        # Wording alone never makes a certain correction: either the human names a file
        # the agent had just changed, or the agent goes back to one of those files next.
        changed = frozenset(edited_all)
        # A verdict typed long after the agent finished may be about something else. It is
        # still a correction if the agent then goes back to the files: behaviour decides.
        stale = "long pause" in labels
        strong = confidence >= CORRECTION_STRONG or stale
        named_changed = [rel for rel in named if rel in changed]
        corroborated = ""
        if not strong:
            note = "weaker cues only, no clear correction phrase"
        elif named_changed and not stale:
            note, corroborated = f"the user names {named_changed[0]}, which it had changed", "named"
        elif changed:
            confidence = CORRECTION_STRONG
            note = "awaiting the agent's response"
        else:
            confidence = CORRECTION_PROBABLE
            note = "not corroborated: no file was changed in the preceding turn"
        certain = confidence >= CORRECTION_STRONG
        finding = self._emit(
            USER_CORRECTION if certain else PROBABLE_CORRECTION,
            event,
            self._correction_summary(confidence, matched, note, how),
            targets,
            # a guess is never put in anyone's mouth: only a correction keeps the words
            excerpt=event.text if certain else "",
            detail={"confidence": f"{confidence:g}", **({"quote": event.text} if certain else {})},
        )
        if corroborated:
            finding.detail["corroborated"] = corroborated
            self.state.rework = _Rework(finding, frozenset(named_changed))
        elif strong and changed:
            about = frozenset(named_changed) or changed
            self.state.unconfirmed = _Unconfirmed(finding, about, matched, how)
            self.state.rework = _Rework(finding, about)


@dataclass
class Detection:
    """What one stream yielded."""

    findings: list[Finding]
    #: Every ``(kind, key)`` target the agent went near, friction or not.
    exposed: set[tuple[str, str]]
    #: Events a detector raised on. They are skipped and counted, never fatal.
    skipped_events: int = 0
    #: Red runs seen and not reported: (transcript line, seconds the run took, reason).
    dropped: list[tuple[int, float, str]] = field(default_factory=list)


def detect_stream(stream: Stream, ctx: DetectContext) -> Detection:
    """Run every transcript detector over one stream."""
    detector = _Detector(stream, ctx)
    findings = detector.run()
    return Detection(findings, detector.exposed, detector.skipped_events, detector.dropped)


def detect_with_exposure(
    stream: Stream, ctx: DetectContext
) -> tuple[list[Finding], set[tuple[str, str]]]:
    """Findings plus every ``(kind, key)`` target the agent touched, friction or not."""
    found = detect_stream(stream, ctx)
    return found.findings, found.exposed


def runs_inside(resolver: PathResolver, event: Event) -> bool:
    """Does the shell call ``event`` run its main step inside the repository?

    ``cd /abs/repo && make build`` does, wherever the session itself was started.
    """
    if not event.command:
        return False
    parsed = parse_command(event.command)
    primary = next((s for s in parsed.steps if s.key == parsed.key), None)
    if primary is None or not primary.moves:
        return resolver.contains(event.cwd)
    return resolver.contains(resolver.chdir(event.cwd, primary.moves) or event.cwd)


def detect(stream: Stream, ctx: DetectContext) -> list[Finding]:
    """Run every transcript detector over one stream."""
    return detect_with_exposure(stream, ctx)[0]
