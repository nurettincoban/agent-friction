"""Signal catalog and transcript detectors.

Each detector looks at one :class:`~agent_friction.model.Stream` and emits
:class:`~agent_friction.model.Finding` records. Every threshold is a named
constant here and is explained in docs/signals.md.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

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
    denied_step,
    ends_looking,
    env_copy,
    in_sequence,
    inner_key,
    is_aggregate_check,
    is_inline_code,
    is_replacement,
    is_setup_command,
    is_setup_step,
    is_shell_edit,
    is_windows_path,
    may_change_files,
    normalize_argv,
    objectionable_step,
    parse_command,
    path_candidates,
    prints_another_file,
    setup_label,
    status_is_last_steps,
    written_file,
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
TOOL_FAILURE = "tool-failure"
#: A tool that failed once in the whole scan is an accident; the engine keeps the events
#: of a tool only from this many failures on.
TOOL_FAILURE_MIN_EVENTS = 2

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
#: Read-only inspection in between is not counted.
RECOVERY_LOOKAHEAD = 5
#: Per-event cap for the time-lost estimate, in seconds.
TIME_CAP_SECONDS = 300.0
_EXIT_LINE_RE = re.compile(r"(?mi)^\s*(?:Error: )?Exit code \d+\s*$")
#: Where a transcript does not say whether a person had to approve a call, the time from
#: call to result counts at most this long (or the command's usual run time, if longer).
UNATTENDED_RUN_SECONDS = 30.0
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
        f"{NO_CONVERGENCE_MAX_FILES} most edited, in proportion to their edits; only those the "
        "failing output names, if it names any).",
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
        "edit on that file, or a failed command or retry loop in the turn. Not recorded next "
        "to a no-convergence event of the same turn that names the file.",
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
        TOOL_FAILURE,
        "Tool failure",
        "A tool that is neither a file tool, a search nor the shell (an MCP tool, a web "
        "fetch, a sub-agent) returned an error, and that tool failed "
        f"{TOOL_FAILURE_MIN_EVENTS} or more times in the scanned sessions. A call the "
        "harness never started, a declined call and a denied call are not counted here.",
        "A tool the agent relies on keeps failing: a server that is not running, "
        "credentials that are missing, a site that refuses the request.",
        "No file or command: the event is unattributed, and counted per tool name.",
        0.5,
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
    r"|.* is not recognized as (?:the|a) name of a cmdlet.*"
    # Windows' stand-in for a Python that is not installed (exit 9009)
    r"|Python was not found[;,] (?:run without arguments to install|but can be installed).*"
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
    r"|[\w.-]*make[\w.-]*(?:\[\d+\])?: \*\*\* No rule to make target .*"
    r"|npm (?:ERR!|error) (?:Missing script|enoent|code ENOENT).*"
    r"|error Command \"[^\"\n]+\" not found\.?"
    r"|\s*(?:ERROR\s+)?ERR_PNPM_NO_SCRIPT\b.*"
    r"|npm (?:ERR!|error) (?:code EBADENGINE|engine Unsupported engine|notsup Unsupported engine).*"
    r"|error .*: The engine \"\w+\" is incompatible with this module\..*"
    r"|fatal: not a git repository.*"
    r"|error: could not find `Cargo\.toml`.*"
    r"|(?:ERROR|error): (?:Could not find a version|No matching distribution|"
    r"externally-managed-environment).*"
    r"|go: (?:cannot find main module|go\.mod file not found).*"
    # the launcher could not start what it was asked to run (uv, poetry)
    r"|error: Failed to spawn: .*"
    r"|Command not found: \S+"
    # the machine is not set up for it: no venv support, no permission or trust for
    # npm, no git identity or signing key
    r"|.*\bensurepip is not\s+available\b.*"
    r"|npm (?:ERR!|error) code (?:EPERM|EACCES|SELF_SIGNED_CERT_IN_CHAIN"
    r"|UNABLE_TO_GET_ISSUER_CERT_LOCALLY|CERT_HAS_EXPIRED)\b.*"
    r"|Author identity unknown"
    r"|fatal: unable to auto-detect email address.*"
    r"|error: gpg failed to sign the data.*"
    # dependencies could not be installed or built
    r"|" + _INSTALL_SHAPES +
    # the network was not there
    r"|curl: \((?!(?:6|7|28|52|56)\))\d+\) .*"
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


#: Signatures of a runner rather than of a check. ``npm ERR!`` and ``make: ***`` are
#: printed for whichever script or target failed, so on a line with two steps of the
#: same runner they do not say which one it was.
_RUNNER_SIGNATURES = frozenset({"build", "make", "npm", "cargo"})


def _verdict(name: str | None, output: str) -> str | None:
    signature = VERDICT_SIGNATURES.get(name or "")
    if signature is None:
        return None
    red, green = signature
    if red.search(output):
        return "red"
    return "green" if green is not None and green.search(output) else None


def _key_verdicts(key: str, output: str) -> tuple[str | None, str | None]:
    """``(specific, generic)`` verdicts for ``key``: by its check's summary, by its runner."""
    words = key.split()
    specific: str | None = None
    generic: str | None = None
    for name in (command_role(key), words[0] if words else ""):
        verdict = _verdict(name, output)
        if verdict is None:
            continue
        if name in _RUNNER_SIGNATURES:
            generic = "red" if "red" in (generic, verdict) else verdict
        else:
            specific = "red" if "red" in (specific, verdict) else verdict
    return specific, generic


def key_verdict(key: str, output: str) -> str | None:
    """``"red"``/``"green"`` if ``output`` carries that verdict *of the tool behind ``key``*.

    Only the signatures of the key's own role or program are consulted, so in
    ``ruff check .; mypy; pytest`` each step is judged by its own summary line.
    """
    specific, generic = _key_verdicts(key, output)
    if "red" in (specific, generic):
        return "red"
    return specific or generic


#: The target a failed ``make`` names: ``make: *** [Makefile:20: test] Error 1``.
_MAKE_TARGET_RE = re.compile(
    r"^make(?:\[\d+\])?: \*\*\* \[(?:[^\]\n]*:\d+: )?([^\]\n:]+)\] Error", re.M
)
#: The script a failed ``npm``/``pnpm``/``yarn`` run names.
_NPM_SCRIPT_RE = re.compile(
    r"^npm (?:ERR!|error) (?:(Test) failed\b|Lifecycle script `([\w:.-]+)` failed"
    r"|\S+@\S+ ([\w:.-]+): |Failed at the \S+ ([\w:.-]+) script)"
    r"|ELIFECYCLE\u2009? +(Test) failed|ELIFECYCLE\u2009? +Command failed.*",
    re.M,
)
_NODE_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})


def named_steps(steps: Sequence[Step], output: str) -> list[Step] | None:
    """The steps a runner's own failure line names, or ``None`` if it names nothing.

    ``make: *** [Makefile:20: test] Error 1`` is about the step whose target is
    ``test``, or about the one aggregate target that may include it; ``npm ERR! Test
    failed.`` is about ``npm test``. An empty list means that a target was named and
    no step on the line can be the one.
    """
    targets = {m.group(1).strip() for m in _MAKE_TARGET_RE.finditer(output)}
    scripts = {
        next(g for g in m.groups() if g).casefold()
        for m in _NPM_SCRIPT_RE.finditer(output)
        if any(m.groups())
    }
    if not targets and not scripts:
        return None
    named: list[Step] = []
    makes = [s for s in steps if s.program in ("make", "gmake")]
    direct = [s for s in makes if targets & set(s.args)]
    aggregates = [s for s in makes if is_aggregate_check(s.key)]
    if targets:
        named += direct or (aggregates if len({s.key for s in aggregates}) == 1 else [])
    if scripts:
        named += [
            s
            for s in steps
            if s.program in _NODE_RUNNERS and s.key.split()[-1].casefold() in scripts
        ]
    return named


#: Variables whose value says how a command is run, not who runs it.
_PUBLIC_ENV = frozenset(
    {"PYTHONPATH", "NODE_ENV", "NODE_OPTIONS", "NODE_PATH", "DJANGO_SETTINGS_MODULE"}
    | {"RAILS_ENV", "RACK_ENV", "APP_ENV", "CI", "GOFLAGS", "RUSTFLAGS", "TZ", "LC_ALL", "LANG"}
)
_ENV_ASSIGNMENT_RE = re.compile(r"(?:^|&&|;|\|\||\()\s*((?:[A-Za-z_]\w*=\S*\s+)+)(?=\S)")
_ENV_VALUE_RE = re.compile(r"[\w./:@,=+-]{1,60}")


def _env_prefix(command: str, values: bool) -> str:
    """Variables set in front of the last command of a line: ``PYTHONPATH=src``.

    Values are kept only for a fixed set of variables that say how to run things,
    and only when ``values`` is true; any other variable is shown as ``NAME=...``.
    """
    found = _ENV_ASSIGNMENT_RE.findall(command.split("<<", 1)[0])
    if not found:
        return ""
    shown = []
    for assignment in found[-1].split():
        name, _, value = assignment.partition("=")
        plain = values and name in _PUBLIC_ENV and _ENV_VALUE_RE.fullmatch(value) is not None
        shown.append(f"{name}={value}" if plain else f"{name}=...")
    return " ".join(shown)


#: Steps that put an environment in place for the rest of a command line.
_ACTIVATORS = frozenset(
    {"poetry shell", "pipenv shell", "conda activate", "mamba activate", "micromamba activate"}
)


def _activation(cmd: ParsedCommand, failed: str) -> str:
    """The step of a green command line that activates an environment, if ``failed`` had none.

    ``source .venv/bin/activate && pytest`` after a red ``pytest``: what was missing
    was the activation, and that is what is worth writing down. Also ``. venv/bin/activate``,
    ``poetry shell``, ``conda activate <env>``, ``nvm use``.
    """
    for step in cmd.steps:
        words = [step.program, *step.args]
        if step.program in ("source", ".") and step.args:
            script = step.args[0]
            if not re.search(r"(?:^|/)(?:activate(?:\.\w+)?|\S*env\S*|\.?envrc)$", script):
                continue
            text = f"source {script}"
        elif " ".join(words[:2]) in _ACTIVATORS:
            text = " ".join(
                words[:3] if words[0] != "poetry" and words[0] != "pipenv" else words[:2]
            )
        elif words[:2] in (["nvm", "use"], ["workon"]) or step.program == "workon":
            text = " ".join(words[:3])
        else:
            continue
        mentioned = "activate" if "activate" in text else text
        return "" if mentioned in failed else text
    return ""


def _added_flags(failed: str, passed: str) -> str:
    """The options ``passed`` has and ``failed`` lacks, if that is all that differs."""
    before, after = normalize_argv(failed).split(), normalize_argv(passed).split()
    if len(after) <= len(before) or any(ch in passed for ch in "|;&<>"):
        return ""
    rest = list(after)
    for word in before:
        if word not in rest:
            return ""
        rest.remove(word)
    plain = re.compile(r"--?[A-Za-z][\w-]*(?:=[\w./:@-]{1,40})?")
    if not rest or len(rest) > 3 or not all(plain.fullmatch(word) for word in rest):
        return ""
    return " ".join(rest)


def _up_to_verdict(step: Step | None, output: str, error: bool = False) -> str:
    """``output`` cut after the line that carries ``step``'s red verdict, if it has one.

    Excerpts keep the head and the tail of long output. On a line with several
    commands the tail belongs to the last of them, which may have passed. With
    ``error`` (a line of several commands) a line that reads as a setup/invocation
    error (`pytest: error: unrecognized arguments: -n0`) is what decided it, whatever a
    later step printed after it: the output is cut there, unless the step's own
    verdict came first.
    """
    cut = len(output) + 1
    if step is not None:
        for name in (command_role(step.key), step.program):
            signature = VERDICT_SIGNATURES.get(name or "")
            match = signature[0].search(output) if signature is not None else None
            if match is not None:
                cut = match.end()
                break
    if error:
        decided = [m for m in INVOCATION_ERROR_RE.finditer(output) if m.end() <= cut]
        if decided:
            cut = decided[-1].end()
    if cut > len(output):
        return output
    end = output.find("\n", cut)
    return output if end < 0 else output[:end]


def _candidates(cmd: ParsedCommand) -> list[Step]:
    return [
        s
        for s in cmd.steps
        if s.key and not s.inspection and s.program not in SETUP_PROGRAMS | HOUSEKEEPING_PROGRAMS
    ]


def step_verdicts(cmd: ParsedCommand, output: str) -> dict[str, str]:
    """Verdict per step key of a line with several commands, each by its own signature."""
    verdicts: dict[str, str] = {}
    # A step whose output went to /dev/null printed none of this.
    judged = [s for s in _candidates(cmd) if not s.silenced]
    named = named_steps(judged, output)
    both = {s.key: _key_verdicts(s.key, output) for s in judged}
    for step in judged:
        specific, generic = both[step.key]
        if generic == "red" and specific != "red":
            # `npm ERR!` after `npm run lint && npm test`: the runner's line belongs to
            # the step it names, or to the one whose own summary is red.
            others = [s for s in judged if s.key != step.key and s.program == step.program]
            if named is not None:
                if step not in named and any(s in named for s in others):
                    generic = None
            elif any(both[s.key][0] == "red" for s in others):
                generic = None
        verdict = "red" if "red" in (specific, generic) else specific or generic
        if verdict is not None and verdicts.get(step.key) != "red":
            verdicts[step.key] = verdict
    # `ruff format . && make check | tail -2 && git status`: an aggregate target has no
    # summary line of its own. It ran green when nothing in the output is red and the
    # output shows a passing summary that no other step on the line could have printed.
    steps = judged
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


def output_verdict(
    output: str, first_party: Callable[[str], bool] | None = None, traceback: bool = True
) -> str | None:
    """``"red"``, ``"green"`` or ``None`` (cannot tell) for a command judged by its output.

    For command lines whose exit status belongs to something else (a trailing
    ``| tail``, ``; echo done``, ``|| true``): there an exit code of 0 says
    nothing about the command that matters.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if is_invocation_error(output, None, first_party) or RED_OUTPUT_RE.search(output):
        return "red"
    if own_module_missing(output, first_party):
        return "red"  # it did not run, whichever kind of failure that turns out to be
    # A traceback at the end is the judged command's own, unless the caller knows that
    # the end of the output belongs to a later step that only looks at something.
    traceback = traceback and bool(lines) and "Traceback (most recent call last)" in output
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


def _step_signature(step: Step, runner: bool = True) -> list[re.Pattern[str]]:
    if is_setup_command(step.key):
        return [INSTALL_FAILURE_RE]
    names = (command_role(step.key), step.program) if runner else (command_role(step.key),)
    return [STEP_SIGNATURES[name] for name in names if name in STEP_SIGNATURES]


#: What a git command prints when it is the one that failed.
_GIT_STEP_SIGNATURES: dict[str, re.Pattern[str]] = {
    "git push": re.compile(
        r"failed to push some refs|^ ! \[(?:remote )?rejected\]|has no upstream branch"
        r"|^hint: Updates were rejected",
        re.M,
    ),
    "git merge": re.compile(
        r"^CONFLICT \(|Automatic merge failed|not something we can merge"
        r"|would be overwritten by merge",
        re.M,
    ),
    "git rebase": re.compile(r"^CONFLICT \(|could not apply [0-9a-f]{7}|Cannot rebase:", re.M),
    "git pull": re.compile(
        r"^CONFLICT \(|Need to specify how to reconcile|no tracking information", re.M
    ),
    "git commit": re.compile(r"nothing to commit|no changes added to commit|hook\b.*failed", re.M),
    "git checkout": re.compile(r"did not match any file|would be overwritten by checkout"),
}


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
    # Nothing in the output was printed by a step sent to /dev/null.
    heard = [s for s in candidates if not s.silenced]
    # The runner names its target (`make: *** [Makefile:20: test] Error 1`) ...
    for step in named_steps(heard, output) or ():
        return step, True
    # ... git says which of its commands gave up (`git merge ... && git push`) ...
    for step in heard:
        said = _GIT_STEP_SIGNATURES.get(step.key)
        if said is not None and said.search(output):
            return step, True
    # ... a check's own summary says more than its runner's `npm ERR!` or `make: ***` ...
    for step in heard:
        if any(pattern.search(output) for pattern in _step_signature(step, runner=False)):
            return step, True
    for step in heard:
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
INVOCATION_EXIT_CODES = frozenset({126, 127, 9009})  # 9009: cmd.exe's "not recognized"


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
    collected = None if _TESTS_RAN_RE.search(output) else _COLLECTION_MISSING_RE.search(output)
    if INVOCATION_ERROR_RE.search(direct):
        missing = missing_name(direct)
        if missing is None and collected is not None:
            # "ImportError while loading conftest" with the module named on an `E` line
            missing = ("module", collected.group(1))
        ours = missing is not None and missing[0] == "module" and own(missing[1].split(".")[0])
        return not ours
    if collected is not None:
        return not own(collected.group(1).split(".")[0])
    return False


def own_module_missing(output: str, first_party: Callable[[str], bool] | None) -> bool:
    """Did the command stop because a module *of this repository* could not be imported?

    Two things look like this: a typo in an import (a bug, fixed by an edit) and a
    package that is there but not importable the way the command was run (no
    ``PYTHONPATH``, not installed, wrong runner). Only what happens next tells them
    apart, so the failure starts as ordinary and is promoted when a setup step or
    another command for the same job makes it pass with no edit.
    """
    if first_party is None or _TESTS_RAN_RE.search(output):
        return False
    direct = _QUOTED_LINE_RE.sub("", output)
    missing = missing_name(direct) if INVOCATION_ERROR_RE.search(direct) else None
    collected = _COLLECTION_MISSING_RE.search(output)
    if missing is None and collected is not None:
        missing = ("module", collected.group(1))
    return missing is not None and missing[0] == "module" and first_party(missing[1].split(".")[0])


#: Failures of the surroundings rather than of the code or the command line: a service
#: that is not running, credentials that are missing or expired, an unset variable.
_SERVICE_DOWN_RE = re.compile(
    r"Cannot connect to the Docker daemon|Connection refused|ECONNREFUSED"
    r"|could not connect to server|connection to server\b.{0,120}\bfailed"
    r"|Is the server running|Can't connect to (?:local )?(?:MySQL )?server"
    r"|No connection could be made|server closed the connection unexpectedly"
    # `docker compose exec web pytest` with the service down
    r"|(?m:^(?:Error[^\n]{0,40}: )?(?:service \"[^\"\n]+\" is not running"
    r"|no such service: \S+|Container \S+ is not running))"
    # kubectl and other Go clients: the cluster or host did not answer
    r"|Unable to connect to the server"
    r"|dial tcp [^\n]*: (?:i/o timeout|connect: connection refused|connect: no route to host)"
    # curl could not connect (7), timed out (28), got no reply (52) or lost the connection (56)
    r"|curl: \((?:7|28|52|56)\) ",
    re.I,
)
#: Programs an agent asks "is it up yet?" with. A refused connection that the same line
#: gets through a moment later was a server still starting, not friction.
_PROBE_PROGRAMS = frozenset({"curl", "wget", "http", "https", "nc", "pg_isready", "wait-on"})
_AUTH_RE = re.compile(
    r"credentials (?:have |are )?expired|expired ?token|token (?:has |is )?expired"
    r"|Unable to locate credentials|You must be logged in|not logged in(?:to)?\b"
    r"|authentication failed|Authentication required|Permission denied \(publickey"
    r"|\b(?:gh|gcloud|az|aws sso|docker|npm|vercel|firebase) (?:auth )?login\b"
    r"|InvalidClientTokenId|could not read Username"
    # a remote that refuses the credentials at hand
    r"|The requested URL returned error: 40[13]|Permission to \S+ denied to "
    r"|Invalid username or password|\b401 Unauthorized\b|\bcode E40[13]\b"
    # a database, a registry or a cloud API that turns the credentials down
    r"|ERROR 1045 \(28000\)|Access denied for user\b"
    r"|password authentication failed|\bunauthorized: (?:incorrect|authentication|access)\b"
    r"|No valid credential sources|\bBad credentials\b"
    r"|InvalidAccessKeyId|ExpiredToken|UnrecognizedClientException|SignatureDoesNotMatch"
    r"|security token included in the request is (?:invalid|expired)"
    # 403 as a tool reports it, not as a test asserts it
    r"|(?-i:^(?:npm (?:ERR!|error)|error:|fatal:|ERROR:|remote:|gh:|HTTP) [^\n]*\b403\b)"
    r"|\b403 Client Error: Forbidden",
    re.I | re.M,
)
#: A package registry that refused the credentials: an install that failed for want of
#: a login, not for how it was called.
_REGISTRY_AUTH_RE = re.compile(r"^npm (?:ERR!|error) code E40[13]\b", re.M)
#: `git push` turned down because the remote branch has commits the local one lacks.
PUSH_REJECTED_RE = re.compile(
    r"^ ! \[rejected\][^\n]*\((?:non-fast-forward|fetch first)\)"
    r"|^hint: Updates were rejected because (?:the tip of your current branch is behind"
    r"|the remote contains work)",
    re.M,
)
#: Searches for leftovers: debug statements, work markers, conflict markers, focused or
#: skipped tests. An empty result is the answer the agent wanted.
HYGIENE_PATTERN_RE = re.compile(
    r"console\\?\.(?:log|debug|warn)|\bdebugger\b|\bTODO\b|\bFIXME\b|\bXXX\b|\bHACK\b"
    r"|<{4,}|>{4,}|={7}|\\?\.only\b|\b(?:fit|fdescribe|xit|xdescribe)\\?\("
    r"|\bpdb\b|\bipdb\b|\bbreakpoint\\?\(|set_trace|binding\\?\.pry|\bdbg!|\bprint\\?\("
    r"|\bdd\\?\(|var_dump|\bNOCOMMIT\b|\bDO NOT (?:COMMIT|MERGE)\b",
    re.I,
)
_SEARCH_WORD_RE = re.compile(r"[A-Z]?[a-z0-9]{2,}|[A-Z]{3,}")


def _search_words(text: str) -> set[str]:
    """The words of a search pattern or path (``RefundPolicy`` -> refund, policy)."""
    return {w.casefold() for w in _SEARCH_WORD_RE.findall(text) if len(w) >= 4}


#: Where a build target or script the agent asked for turned out not to exist.
TARGETS_IN_MAKEFILE = "Makefile"
TARGETS_IN_PACKAGE = "package.json"
# "needed by" is a missing prerequisite of a target that does exist: not this.
_NO_MAKE_TARGET_RE = re.compile(
    r"(?m)^[\w.-]*make[\w.-]*(?:\[\d+\])?: \*\*\* No rule to make target "
    r"[`'\"]([^`'\"\n]+)['\"]\.\s+Stop\."
)
_NO_SCRIPT_RE = re.compile(
    r"(?m)^(?:npm (?:ERR!|error) Missing script:? "
    r"|\s*(?:ERROR\s+)?ERR_PNPM_NO_SCRIPT\s+Missing script: "
    r"|error Command )[\"']?([\w:.@/-]+)"
)


def missing_target(output: str) -> tuple[str, str] | None:
    """``("Makefile", "lint")`` when ``output`` says that this target or script is not defined."""
    found = _NO_MAKE_TARGET_RE.search(output)
    if found is not None:
        return TARGETS_IN_MAKEFILE, found.group(1)
    found = _NO_SCRIPT_RE.search(output)
    if found is not None:
        return TARGETS_IN_PACKAGE, found.group(1)
    return None


#: A green run that counted fewer than this share of the tests a red run counted ran a
#: subset of them, whatever its command line says.
SUBSET_PASS_SHARE = 0.5
_TEST_COUNT_RE = re.compile(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b")
#: Options that pick some of the tests: pytest, jest, vitest, go, cargo, dotnet, gradle.
_SELECTING_OPTIONS = frozenset(
    {"-k", "-m", "-t", "-run", "--run", "--filter", "--grep", "-g", "--testNamePattern"}
    | {"--tests", "--lf", "--last-failed", "--ff", "--only", "--test", "--testPathPattern"}
)
_SWITCHES = frozenset(
    {"-q", "-qq", "-x", "-v", "-vv", "-s", "-l", "--verbose", "--quiet", "--run", "--ci", "--"}
    | {"--no-cov", "--no-header", "--exitfirst", "--silent", "--bail", "--watchAll=false"}
)
_SELECTED_PATH_RE = re.compile(
    r"^(?:\./)?[\w@.-]+(?:/[\w@.+-]+)*(?:/|\.[A-Za-z]{1,5})?(?:::[^\s]+|:\d+)?$"
)


def tests_counted(output: str) -> int:
    """How many tests a run's summary counts (passed, failed, errors), 0 if it has none."""
    counts = [
        int(n)
        for n, what in _TEST_COUNT_RE.findall(output[-2000:])
        if what in ("passed", "failed", "error", "errors")
    ]
    return sum(counts)


#: Options that pick tests by name; a value that matches a failed test selects it.
_NAMING_OPTIONS = frozenset({"-k", "-t", "-run", "--run", "--grep", "-g", "--testNamePattern"})
#: Options that run what failed last time.
_LAST_FAILED_OPTIONS = frozenset({"--lf", "--last-failed"})


def _test_words(line: str) -> list[str]:
    """The words of the steps of ``line`` that run tests, each step led by ``;``."""
    try:
        steps = parse_command(line).steps
    except Exception:  # an unreadable line selects nothing we can name
        return []
    # Only the steps that run tests select tests: `python scripts/patch.py && npm test`
    # names a script, not a test file.
    testing = [s for s in steps if command_role(s.key) == "test"] or steps
    words: list[str] = []
    for step in testing:
        words += [";", step.program, *step.args]
    return words


def selected_names(line: str) -> list[str]:
    """The plain names a test line asks for: ``pytest -k test_total`` -> ``["test_total"]``.

    Empty when a value is an expression (``-k "a and not b"``, a regex with operators):
    what that selects cannot be told from here.
    """
    words = _test_words(line)
    names: list[str] = []
    for index, word in enumerate(words):
        name, eq, value = word.partition("=")
        if name not in _NAMING_OPTIONS:
            continue
        if not eq:
            value = words[index + 1] if index + 1 < len(words) else ""
        value = value.strip("^$'\"/")
        if not re.fullmatch(r"[\w.:-]+(?: [\w.:-]+)*", value) or re.search(
            r"\b(?:and|or|not)\b", value
        ):
            return []
        names.append(value)
    return names


def _test_part(name: str) -> str:
    """The test's own name in a failure line: after the file, before its parameters."""
    return re.split(r"::| > | › ", name)[-1] if re.search(r"::| > | › ", name) else name


def selection(line: str) -> tuple[frozenset[str], frozenset[str]]:
    """What a test command line picks out: ``(paths and node ids, selecting options)``.

    ``pytest tests/test_core.py -k total`` -> ``({"tests/test_core.py"}, {"-k"})``.
    A bare word is a program, a target or a subcommand, never a selection.
    """
    paths: set[str] = set()
    options: set[str] = set()
    words = _test_words(line)
    previous = ""
    for word in words:
        name = word.split("=", 1)[0]
        if name in _SELECTING_OPTIONS and not (
            name == "-m" and _PYTHON_NAME_RE.match(previous.rsplit("/", 1)[-1])
        ):
            options.add(name)
        elif (
            previous != ";"
            and not word.startswith("-")
            # after an option that is not a plain switch the word is that option's value
            and (not previous.startswith("-") or "=" in previous or previous in _SWITCHES)
            and ("/" in word or "::" in word or re.search(r"\.[A-Za-z]{1,5}(?::\d+)?$", word))
            and word.strip("./") not in ("", "...")
            and _SELECTED_PATH_RE.match(word.removesuffix("/..."))
        ):
            paths.add(word.removeprefix("./"))
        previous = word
    return frozenset(paths), frozenset(options)


def selects(selected: str, test: str) -> bool:
    """Does the selection ``selected`` (a path or node id) include the test file ``test``?"""
    path = selected.split("::", 1)[0].removesuffix("/...").rstrip("/")
    path = re.sub(r":\d+$", "", path)
    return test == path or test.startswith(path + "/") or test.endswith("/" + path)


#: A silence this long after a call's result is nobody working (a closed laptop, an agent
#: waiting to be resumed), whatever follows it: it is never part of a round trip.
IDLE_GAP_SECONDS = TIME_CAP_SECONDS

#: What `timeout N cmd` exits with when the time ran out.
TIMEOUT_EXIT_CODE = 124
_TIMEOUT_WRAPPER_RE = re.compile(r"(?:^|[;&|(]\s*)g?timeout\s+(?:-\S+\s+)*\d")
#: A server saying that it is up.
READY_BANNER_RE = re.compile(
    r"\bready in \d|\bready (?:on|at|-) |\bstarted server\b|\blistening (?:on|at)\b"
    r"|compiled successfully|\bLocal:\s+https?://|running (?:on|at) https?://"
    r"|Application startup complete|Server (?:is )?(?:running|started|listening)"
    r"|Development server (?:is )?running|webpack \S+ compiled",
    re.I,
)
#: How many of the latest edits are remembered for "did the agent just add this import?".
ADDED_TEXTS_KEPT = 8
#: The shell could not change directory, so what came after `cd x &&` never ran.
#: bash (``bash: line 1: cd: web: No such file or directory``), zsh (``cd:1: no such
#: file or directory: web``, ``(eval):cd:1: ...``) and dash (``cd: can't cd to web``).
_CD_FAILED_RE = re.compile(
    r"(?:^|: )cd: (?:(?P<bash>[^\n:]+): )?(?:No such file or directory|Not a directory)\s*$"
    r"|(?:^|:)cd:(?:\d+:)? (?:no such file or directory|not a directory): (?P<zsh>[^\n]+?)\s*$"
    r"|: can't cd to (?P<dash>[^\n]+?)\s*$",
    re.M | re.I,
)
#: The port a server wants is taken, usually by one left running earlier.
_PORT_IN_USE_RE = re.compile(
    r"EADDRINUSE|address already in use|port (?:\d+ )?is already (?:in use|allocated)", re.I
)
#: The disk is full.
#: A host name that could not be resolved: no network, or no access to that host.
_NO_NETWORK_RE = re.compile(
    r"curl: \(6\) |Could not resolve host|unable to resolve host address"
    r"|Temporary failure in name resolution|Name or service not known"
    r"|getaddrinfo (?:ENOTFOUND|EAI_AGAIN)|nodename nor servname provided",
    re.I,
)
_DISK_FULL_RE = re.compile(r"\bENOSPC\b|No space left on device", re.I)
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
    (re.compile(r"Unable to connect to the server|kube|:6443\b", re.I), "the Kubernetes cluster"),
)
_PORT_RE = re.compile(r"(?:\bport |(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]):)(\d{2,5})\b")
ENV_SERVICE = "service"
ENV_AUTH = "auth"
ENV_VARIABLE = "variable"
ENV_PORT = "port"
ENV_DISK = "disk"
ENV_NETWORK = "network"


def environment_error(output: str) -> tuple[str, str] | None:
    """``(kind, name)`` if a failure's output blames the environment, else ``None``.

    ``kind`` is ``service`` (something the command talks to is not running),
    ``auth`` (credentials missing or expired), ``variable`` (an environment
    variable is not set), ``port`` (the port a server wants is taken) or ``disk``
    (no space left). ``name`` is the service from a fixed vocabulary
    ("PostgreSQL", "the Docker daemon", "the service on port 8080"), the
    variable's name, or empty.
    """
    if _DISK_FULL_RE.search(output):
        return ENV_DISK, ""
    if _PORT_IN_USE_RE.search(output):
        return ENV_PORT, ""
    if _NO_NETWORK_RE.search(output):
        return ENV_NETWORK, ""
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
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_INSTALL_WORDS = frozenset({"install", "i", "ci", "add"})


def _same_job_other_manager(failed_key: str, key: str, unfound: str) -> bool:
    """Is ``key`` the failed command under another package manager (``npm install``)?"""
    was, now = failed_key.split(), key.split()
    if not was or not now or was[0] != unfound or was[0] == now[0]:
        return False
    if was[0] not in _PACKAGE_MANAGERS or now[0] not in _PACKAGE_MANAGERS:
        return False
    rest, other = was[1:] or ["install"], now[1:] or ["install"]
    return rest == other or (rest[0] in _INSTALL_WORDS and other[0] in _INSTALL_WORDS)


#: A command told to watch for changes (`tsc -w`, `jest --watch`, `vitest watch`).
_WATCH_RE = re.compile(r"\s(?:--watch(?:All)?|-w|watch)(?:\s|=|$)")
#: "bash: pnpm: command not found" and its variants: the program a shell could not find.
#: Also cmd.exe ("'x' is not recognized as an internal or external command") and
#: PowerShell ("The term 'x' is not recognized as the name of a cmdlet, function, ...").
_NOT_FOUND_PROGRAM_RE = re.compile(
    r"(?:^|: )([^\s:]+): (?:command )?not found\s*$|command not found: (\S+)\s*$"
    r"|'([^'\n]+)' is not recognized as an internal or external command"
    r"|The term '([^'\n]+)' is not recognized as (?:the|a) name of a cmdlet"
    r"|^(Python) was not found[;,] (?:run without arguments to install|but can be installed)"
    # a program called by its absolute path: `bash: /usr/bin/time: No such file or directory`
    r"|^(?:[\w./-]+: )?(?:line \d+: )?(/[^\s:]+): No such file or directory\s*$",
    re.M,
)
#: PowerShell saying that a path is not there: ``Get-Content : Cannot find path
#: 'C:\\repo\\src\\a.py' because it does not exist.`` (5.1) or ``Get-Content: Cannot ...`` (7).
_PS_MISSING_RE = re.compile(
    r"^(?:([A-Za-z][\w-]*) ?: )?Cannot find path '([^'\n]+)' because it does not exist", re.M
)
#: Cmdlets (and their aliases) that ask whether something is there or list a directory:
#: a miss is the answer to a question, like ``ls`` of a directory that is not there.
_PS_LISTING = frozenset(
    {"get-childitem", "gci", "dir", "ls", "get-item", "gi", "test-path", "resolve-path", "rvpa"}
)
_PS_MOVING = frozenset({"set-location", "sl", "cd", "chdir", "push-location", "pushd"})
#: `...; echo "exit $?"`: the line prints the status its own exit code no longer shows.
_STATUS_ECHO_RE = re.compile(r"\becho\s+(?:-\w+\s+)*[\"']?[^\"';|&\n]*\$\?")
_ECHOED_STATUS_RE = re.compile(
    r"^\s*(?:exit(?: ?code| status)?|status|rc|code|EXIT)\s*[:=]?\s*(\d{1,3})\s*$", re.M | re.I
)


def echoed_status(command: str, output: str) -> int | None:
    """The exit status a line printed for itself with ``echo "exit $?"``, if it did."""
    if len(_STATUS_ECHO_RE.findall(command)) != 1:
        return None
    found = _ECHOED_STATUS_RE.findall(output)
    return int(found[-1]) if len(found) == 1 else None


def _unfound_program(output: str) -> str:
    """The program a shell said it could not find, by its base name, or ``""``."""
    match = _NOT_FOUND_PROGRAM_RE.search(output)
    if match is None:
        return ""
    name = next(g for g in match.groups() if g).replace("\\", "/").rsplit("/", 1)[-1]
    name = "python" if name == "Python" else name  # the Windows stub names the product
    return re.sub(r"\.(?:exe|cmd|bat|ps1)$", "", name, flags=re.I)


#: The tests a red run names: pytest's ``FAILED tests/test_a.py::test_x`` (and ``ERROR``),
#: go's ``--- FAIL: TestX``.
FAILED_TEST_RE = re.compile(r"^(?:(?:FAILED|ERROR) ([\w./-]+::[\w.:\[\]-]+)|--- FAIL: (\w+))", re.M)
MAX_FAILED_TESTS = 10
#: jest and vitest: ``FAIL src/a.test.ts`` heads a red file; jest then names tests as
#: ``● suite › test``, vitest on the line itself as ``FAIL src/a.test.ts > suite > test``.
_JS_FAIL_FILE_RE = re.compile(
    r"^\s*(?:FAIL|×|✗|❯)\s+(\S+\.[cm]?[jt]sx?)(?: > (.+?))?\s*(?:\d+ ?ms)?$"
)
_JEST_TEST_RE = re.compile(r"^\s*● (.+ › .+?)\s*$")


def failed_tests(output: str) -> list[tuple[str, str]]:
    """``(test, file it names or "")`` for each test a red run names, in order.

    pytest's ``FAILED tests/test_a.py::test_x``, go's ``--- FAIL: TestX``, jest's
    ``● cart › adds an item`` under ``FAIL src/cart.test.ts`` (named ``src/cart.test.ts
    › cart › adds an item``) and vitest's ``FAIL src/cart.test.ts > cart > adds an
    item``. A red jest file that names no test is named by itself.
    """
    found: list[tuple[str, str]] = []
    for match in FAILED_TEST_RE.finditer(output):
        name = match.group(1) or match.group(2)
        found.append((name, name.split("::", 1)[0] if "::" in name else ""))
    files: list[str] = []
    named: set[str] = set()
    current = ""
    for line in output.splitlines():
        head = _JS_FAIL_FILE_RE.match(line)
        if head is not None:
            current = head.group(1)
            if current not in files:
                files.append(current)
            if head.group(2):
                found.append((f"{current} > {head.group(2)}", current))
                named.add(current)
            continue
        test = _JEST_TEST_RE.match(line)
        if test is not None:
            found.append((f"{current} › {test.group(1)}" if current else test.group(1), current))
            named.add(current)
    found.extend((path, path) for path in files if path not in named)
    return list(dict.fromkeys(found))


#: Praise, thanks and dismissals. A correction phrase *followed* by one of these is not a
#: correction ("you forgot nothing, great job", "still failing but that's unrelated, ignore
#: it"), and a message containing one is never a "probable" correction.
_SOFTENER_RE = re.compile(
    r"\b(?:thanks|thank you|thx|great|good job|nice(?:ly)?|perfect|awesome|excellent|well done"
    r"|love it|like it|nailed it|keep going|looks good|lgtm|all good|amazing|wonderful"
    r"|no worries|never ?mind|ignore (?:it|that|this)|unrelated|not your fault|flaky"
    r"|(?:that's|it's|that is|it is|is|are) (?:fine|ok|okay)"
    # a shrug ("not quite what I had in mind but fine") or an afterthought ("no, wait, also")
    r"|but (?:fine|ok|okay|whatever|it works|that works|good enough|i can live with (?:it|that))"
    r"|(?:wait|oh|hang on|hold on),? (?:also|and also|one more thing|additionally|while you)"
    # the human takes the blame, or is only asking
    r"|my (?:bad|fault|mistake)"
    r"|just (?:curious|wondering|asking|checking|confirming|making sure)"
    r"|out of curiosity|no complaints?|not complaining|curious|genuine(?:ly)? (?:question|asking)"
    # an acknowledgement: "you removed the flag as asked, now ..."
    r"|as (?:asked|requested|expected|planned|discussed|agreed|i (?:asked|wanted|said))"
    # the problem predates the agent's work
    r"|before your (?:change|changes|edit|edits|commit|commits|fix|pr)|not caused by you(?:r \w+)?"
    r"|already (?:broken|failing|red|there)|pre-?existing|not (?:related to|because of) your"
    r"|since (?:yesterday|last (?:night|week|month|\w+day)|this morning|\w+day|before)"
    r"|for (?:days|weeks|months|a while|ages))\b",
    re.I,
)
#: The human names their own action or omission as the cause: "... because I hadn't
#: pulled", "failing again after I merged main", "because my VPN was off". Not "after I
#: pulled your change", "when I run it", "since I can't sign in", "because we use tabs",
#: which say what is wrong with the agent's work. It voids a phrase like any softener,
#: but if the agent then changes the same files again its work did need redoing.
_SELF_BLAME_RE = re.compile(
    r"\b(?:because|after|since|as|cause|cuz) (?:i|we)(?:'d|'ve| had| have| just| accidentally"
    r"| never| also)* "
    r"(?:hadn't|had not|haven't|have not|didn't|did not|forgot|never|merged|pulled|rebased"
    r"|changed|edited|updated|upgraded|downgraded|reverted|reset|switched|checked out|deleted"
    r"|removed|renamed|moved|broke|messed|misconfigured|mistyped|stashed|bumped|touched"
    r"|was|were|wasn't|weren't)\b"
    r"(?! (?:in |down |up )?(?:your|you\b|it\b|that\b|this\b|the (?:change|changes|fix|patch"
    r"|edit|edits|commit)\b))"
    r"|\b(?:because|since) (?:of )?(?:my|our) \w+(?: \w+)? (?:was|were|is|are|wasn't|isn't"
    r"|had|hadn't|has|hasn't)\b",
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
#: What an agent can be told to stop doing. Other gerunds after "stop" describe what the
#: product should stop doing, which is a feature request.
_AGENT_DOING = (
    r"doing|editing|changing|adding|removing|deleting|touching|running|creating|writing"
    r"|rewriting|modifying|refactoring|guessing|making|trying|using|installing|committing"
    r"|pushing|asking|apologi[sz]ing|reverting|renaming|ignoring|skipping|mocking|importing"
    r"|moving|putting|generating|overwriting|breaking|messing|going|assuming|inventing"
    r"|hallucinating|repeating|reading|searching|looping|wasting|overthinking|overengineering"
    r"|over-engineering|fiddling|tweaking|formatting|reformatting|commenting|working"
)
#: What an agent can be told never to do.
_NEVER_DO = (
    r"commit|push|edit|change|touch|delete|remove|use|run|add|write|rewrite|modify|skip"
    r"|mock|import|install|force|merge|rebase|disable|hardcode|hard-code|ignore|create|call"
    r"|put|store|log|catch|swallow|do"
)
_UNDO = r"(?:revert|undo|roll ?back)"
#: What an undo must refer to: the agent's own change, by pronoun or as "the last edit".
_UNDO_OBJECT = (
    r"(?:that|this|it|your|those|these|all (?:of )?(?:that|this|your)|everything|what you"
    r"|(?:the|my) (?:last|previous|latest) (?:change|changes|edit|edits|commit|commits|one"
    r"|thing|step|two|three|few|\d+)"
    r"|the (?:change|changes|edit|edits|commit|commits))\b"
    # "revert the commit abc123 from last week" is about old history, not the agent's work
    r"(?!(?: [0-9a-f]{6,40})? (?:from|of) (?:last|yesterday|earlier|before|\d|an? |the other"
    r"|the previous|(?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:the )?\w+ release))"
)
#: The human asks for a change to be taken back: "revert src/app.py", "put it back".
#: A message that adds to the job: "also ...", "and ...", "plus ...", "one more thing".
_ADDITIVE_RE = re.compile(
    r"\W*(?:oh[, ]+)?(?:also|and|plus|additionally|in addition|one more thing|btw|by the way"
    r"|while you(?:'re| are) (?:at it|there))\b",
    re.I,
)
#: ... unless it objects to something while doing so.
_OBJECTION_RE = re.compile(
    r"\b(?:no|not|don't|dont|do not|stop|wrong|instead|never|shouldn't|should not|wait|why"
    r"|revert|undo|actually|rather)\b",
    re.I,
)
_UNDO_ASKED_RE = re.compile(
    rf"\b{_UNDO}\b|\b(?:put|roll) (?:\S+ ){{1,4}}back\b|\brestore\b|\bgo back to\b", re.I
)
_STILL = (
    r"still (?:failing|fails|broken|not working|not fixed|doesn't work|does not work"
    r"|not (?:right|correct|aligned|centered|centred|showing|rendering|rendered|visible"
    r"|resolved|there)|misaligned|incorrect|off\b(?! (?:the|by default))"
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
_STOP_LABEL = "wrong. / stop."
_ANSWER_LABELS = frozenset({_ANSWER_LABEL, "not quite", _STOP_LABEL})

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
            # A rule of the house, stated in reply to the agent's work: "we don't use
            # moment in this codebase, use date-fns", "again: never commit to main".
            "we don't use …",
            _OPEN + r"(?:again\s*[:,—-]*\s*)?we (?:don't|do not|never) use\b",
            False,
        ),
        (
            "don't use X, use Y",
            _OPEN + r"(?:please )?(?:don't|do not) use (?:[^.,;:!?\n]|\.(?=\w)){1,40}[,;.:—-]+\s*"
            r"(?:use|prefer|we use|it's|it is|always)\b",
            False,
        ),
        (
            # not "never mind" (a dismissal), "never seen this before"
            "never …",
            _OPEN + r"(?:again\s*[:,—-]*\s*)?never (?!mind\b|seen\b|heard\b|been\b|had\b)"
            rf"(?:{_NEVER_DO})\b",
            False,
        ),
        ("again: …", r"^again\s*[:—-]+\s*\S", False),
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
            # "wrong. use pnpm" / "wrong file — it should go in util.py" / "stop. use the
            # other client"; not "Stop. Hammer time", "Wrong version of node in CI".
            _STOP_LABEL,
            rf"^(?:wrong|incorrect)(?: (?:{_THING}))?(?:[.!]*$|\s*[.,;:!—–-]+\s+\S)"
            r"|^stop(?:[.!]*$|\s*[.,;:!—-]+\s+(?:use|don't|do not|you|that|this|it\b|revert|undo"
            r"|wait|not |wrong|go back|put|leave|keep|read|look|check|ask|please|just)"
            # "stop editing the tests", not "Stop showing the banner after it is dismissed"
            rf"|\s+(?:that|it|this|right there|(?:{_AGENT_DOING})\b))",
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
            r"(?: just)? (?:broke|breaks)\b(?! (?:nothing|none|no ))",
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
            r"|\bput (?:it|that|this|them|those|everything) (?:all )?back\b"
            r"|\broll (?:it|that|this|them|those|everything) (?:all )?back\b",
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


#: Instructions that redirect work just done: "Don't use time.sleep here", "I don't want
#: a new dependency", "use that instead". On their own they are how anyone briefs an
#: agent; they count as a correction only if the agent had just changed files and then
#: goes back to one of them (see ``instruction_cues``).
_INSTRUCTION_CUES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.I))
    for label, pattern in (
        (
            # not "don't forget ...", "don't worry", "don't commit yet"
            "don't …",
            _OPEN + r"(?:please |pls )?(?:don't|do not) (?!forget|worry|bother|need|hesitate|mind"
            r"|have to|think|know|see|understand|care|get me wrong|be sorry|apologi[sz]e)\w+\b"
            r"(?!(?: \S+){0,3},? yet\b)",
        ),
        (
            "I don't want …",
            _OPEN + r"i (?:don't|do not) want (?:an?|any|the|this|that|these|those|you to"
            r"|to (?:add|use|have|see|change|introduce|pull in|depend))\b",
        ),
        (
            "you're over… this",
            r"\byou(?:'re| are) (?:over-?(?:complicat|engineer|think|do)ing\b"
            r"|making (?:this|it) (?:too|more|overly) )",
        ),
        (
            # what is already there, by pronoun: "we have a retry helper, use that
            # instead"; "use the factory instead" may just as well be the next step
            "use that instead",
            r"\b(?:use|reuse|keep|call) (?:that|this|it|those|these|them|ours|the existing"
            r"(?: \w+)?|what (?:we|you) (?:already )?(?:have|had)) instead\b(?! of\b)",
        ),
        (
            # "again, no mocks in these tests"; not "No tests are needed"
            "no … here",
            _OPEN + r"no (?:more )?\w[\w-]*(?: \w[\w-]*)? "
            r"(?:in (?:the|this|these|those|here|our|my)\b|here\b)",
        ),
        ("can you not …", _OPEN + r"(?:can|could) you (?:please )?not \w+"),
        (
            "restore the old …",
            r"\brestore (?:(?:the|my|that|this) (?:old|original|previous|earlier|former)\b"
            r"|(?:it|that)\b)",
        ),
    )
)


#: ``[Image #1]``, ``[Pasted text #2 +14 lines]``: markers the harness puts where the
#: human attached something.
_ATTACHMENT_RE = re.compile(r"^(?:\s*\[(?:Image|Pasted text|File)[^\]\n]{0,40}\]\s*)+", re.I)
#: Words that say a file was damaged: what follows is the file the complaint is about.
_BLAME = (
    r"\b(?:broke|breaks?|broken|breaking|messed up|screwed up|ruined|deleted|removed|overwrote"
    r"|overwritten|clobbered|touched|changed|modified|reverted|regress(?:ed|ion)|bug in"
    r"|wrong in|mistake in|error in)\b"
)
#: Checks that read the code without running it: any check passing after an edit will do.
_STATIC_CHECKS = frozenset({"typecheck", "lint"})
#: What does the job of a tool that is not installed: what agents fall back to.
_STAND_INS: dict[str, frozenset[str]] = {
    "rg": frozenset({"grep", "egrep", "fgrep", "ag", "ack"}),
    "ag": frozenset({"grep", "egrep", "rg"}),
    "fd": frozenset({"find"}),
    "fdfind": frozenset({"find", "fd"}),
    "jq": frozenset({"python", "python3", "node"}),
    "yq": frozenset({"python", "python3"}),
    "tree": frozenset({"find", "ls"}),
    "bat": frozenset({"cat"}),
    "wget": frozenset({"curl"}),
    "curl": frozenset({"wget"}),
    "gh": frozenset({"curl", "git"}),
}
_NONE: frozenset[str] = frozenset()
#: A fenced code block (closed or running to the end) and Markdown-quoted lines.
_QUOTED_BLOCK_RE = re.compile(r"```.*?(?:```|\Z)|^[ \t]*>[^\n]*$", re.S | re.M)
#: A reporting phrase and the quotation it introduces: "the reviewer's comment: 'No, I
#: said ...'", "Bob wrote: \"that's wrong\"".
_REPORTED_SPEECH_RE = re.compile(
    r"\b(?:comments?|feedback|review|reviewer|message|reply|note|quote|email|ticket|issue"
    r"|(?<!\byou )(?<!\bi )(?:said|says|wrote|writes|written|commented|replied|asked|asks"
    r"|complained|reported))\b"
    r"[^\n\"“”']{0,40}?[:,]?\s*"
    r"(?:\"[^\"\n]{1,300}\"|“[^”\n]{1,300}”|(?<![A-Za-z])'[^\n]{1,300}?'(?![A-Za-z]))",
    re.I,
)


#: Finer names for what a pattern matched, still from a fixed vocabulary: the label is
#: shown without excerpts too, so it never carries the human's own words.
_REFINEMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "that's wrong": (
        (r"not what", "that's not what I …"),
        (r"\bnot (?:right|correct|true)", "that's not right"),
        (r"\bnot working", "that's not working"),
        (r"\bnot how", "that's not how …"),
        (r"broken", "that's broken"),
    ),
    _STOP_LABEL: ((r"^stop", "stop."), (r"", "wrong.")),
    "that didn't work": ((r"help", "that didn't help"), (r"doesn't|does not", "that doesn't work")),
    "revert/undo": (
        (r"\bundo", "undo that"),
        (r"\broll", "roll it back"),
        (r"\bput", "put it back"),
        (r"", "revert that"),
    ),
    "still failing": (
        (r"\bagain\b", "failing again"),
        (r"still (?:broken|crash)", "still broken"),
        (r"still (?:not|doesn't|does not)", "still not working"),
        (r"still (?:wrong|incorrect|misaligned|off)", "still wrong"),
        (r"same|getting|seeing|hitting", "still the same error"),
    ),
}


def _refined(label: str, matched: str) -> str:
    text = matched.lower()
    for pattern, name in _REFINEMENTS.get(label, ()):
        if re.search(pattern, text):
            return name
    return label


def softened_correction(text: str, after_question: bool = False) -> list[str]:
    """The correction phrases in ``text`` that the human took back by blaming themselves.

    "Still failing after I merged main" is not counted as a correction: the
    human names their own action as the cause. If the agent nevertheless goes
    back to the files it had just changed, its work did need redoing, and the
    detector then records a probable correction. Phrases followed by praise or
    a dismissal ("... but that's fine") are not returned.
    """
    return _judge(text, after_question, None)[2]


def instruction_cues(text: str, after_question: bool = False) -> list[str]:
    """Instructions in ``text`` that redirect work the agent has just done.

    "Don't use time.sleep. Use the scheduler.", "I don't want a new dependency",
    "you're overcomplicating this", "we already have a helper, use that instead",
    "again, no mocks in these tests", "can you not change the public API? restore
    the old name". Such a sentence is not a correction by its wording: it is one if
    the agent had changed files in the turn before and changes one of them again in
    reply. Nothing is returned for a message that carries a clearer phrase, praise, a
    dismissal, self-blame or a new task, or that answers a question from the agent.
    """
    return _judge(text, after_question, None)[3]


def correction_confidence(
    text: str, after_question: bool = False, idle_seconds: float | None = None
) -> tuple[float, list[str]]:
    """Confidence in [0, 1] that ``text`` corrects the agent's last turn, and why.

    * An *addressed* phrase ("you broke ...", "I told you ...", "revert that")
      gives 1.0 wherever it stands in the head of the message.
    * A *verdict* ("No, ...", "that's wrong", "still failing") gives 1.0 only
      when it opens the message, the message does not move on to a new task,
      and it is not a "No" answering an offer or a choice the agent just put
      to the human (``after_question``; "wrong", "no no" and a request to undo
      stay corrections after any question). If the human replied more than
      ``CORRECTION_STALE_SECONDS`` after the agent finished (``idle_seconds``),
      a verdict alone is only a probable correction (0.4).
    * Praise, a dismissal or the human blaming themselves after a phrase voids it
      (see ``softened_correction`` for the last).
    * Otherwise two or more distinct weaker cues give 0.4, unless the message
      contains thanks, praise or a dismissal.
    * A single weaker cue ("don't forget ...", "actually, can you also ...")
      is ordinary conversation and gives 0.
    """
    confidence, labels, _, _ = _judge(text, after_question, idle_seconds)
    return confidence, labels


#: Where a sentence ends: not at the dot in ``src/app.py``.
_SENTENCE_END_RE = re.compile(r"\?|!|\n|\.(?=\s|$)")
#: The human says the work went to the wrong place: with another file named, and the
#: agent going there next, that is a correction of the work on the file it had edited.
_WRONG_PLACE_RE = re.compile(
    r"\bwrong (?:file|module|place|spot|one|class|function|component|script|test|package)\b"
    r"|\bnot (?:in )?(?:that|this) (?:file|one|module)\b"
    r"|\b(?:bug|problem|issue|fix|change|error)(?:'s| is| was| belongs| goes| lives"
    r"| should (?:be|go)) in\b",
    re.I,
)


def _judge(
    text: str, after_question: bool, idle_seconds: float | None
) -> tuple[float, list[str], list[str], list[str]]:
    """``correction_confidence``, the phrases only self-blame voided, and instructions."""
    # A pasted screenshot comes first in the message; the words after it are the reply.
    # Pasted output (a code fence, `>` quoted lines) is material, not the human's verdict:
    # "wrong", "still failing" or "you forgot" in a log say nothing about the agent.
    said = _QUOTED_BLOCK_RE.sub("\n", _ATTACHMENT_RE.sub("", text.strip())).strip()
    head = said[:CORRECTION_HEAD_CHARS].replace("’", "'")
    # Somebody else's words, quoted for discussion, are not the human correcting the agent.
    head = _REPORTED_SPEECH_RE.sub(" ", head)
    softeners = [m.start() for m in _SOFTENER_RE.finditer(head)]
    last_softener = max(softeners) if softeners else -1
    blame = [m.start() for m in _SELF_BLAME_RE.finditer(head)]
    last_blame = max(blame) if blame else -1
    softened: list[str] = []
    new_task = bool(_NEW_TASK_RE.search(head))
    strong: list[str] = []
    addressed_any = False
    for label, pattern, addressed in _STRONG_CORRECTION:
        match = pattern.search(head)
        if match is None or match.start() < last_softener:
            continue
        if not addressed and new_task:
            continue
        # "Stop. Just commit." answers an offer; "wrong. use the client" does not
        stops = label != _STOP_LABEL or match.group().lower().startswith("stop")
        if label in _ANSWER_LABELS and after_question and stops:
            continue
        if match.start() < last_blame:
            softened.append(_refined(label, match.group()))
            continue
        if label == "I told you" and _CONFIRM_TAIL_RE.search(head[match.start() :]):
            continue  # "I told you about the flag, right?" asks; it does not reproach
        if label == "still failing":
            ending = _SENTENCE_END_RE.search(head, match.end())
            if ending is not None and ending.group().startswith("?"):
                continue  # "Still getting the error? If so ...": a question, not a verdict
        strong.append(_refined(label, match.group()))
        addressed_any = addressed_any or addressed
    if strong:
        stale = idle_seconds is not None and idle_seconds > CORRECTION_STALE_SECONDS
        if stale and not addressed_any:
            return CORRECTION_PROBABLE, [*strong, "long pause"], [], []
        return CORRECTION_STRONG, strong, [], []
    if softeners or blame:
        return 0.0, [], softened, []
    instructed: list[str] = []
    if not new_task and not after_question:
        instructed = [label for label, pattern in _INSTRUCTION_CUES if pattern.search(head)]
    weak = [label for label, pattern in _WEAK_CORRECTION if pattern.search(head)]
    if len(weak) >= CORRECTION_MIN_CUES:
        return CORRECTION_PROBABLE, weak, [], instructed
    return 0.0, [], [], instructed


@dataclass
class DetectContext:
    """What detectors need to know about the repository."""

    resolver: PathResolver
    #: True if a repo-relative path is (or looks like) a file rather than a directory.
    is_file: Callable[[str], bool]
    #: True if a repo-relative path is a file known from disk or from any transcript.
    is_known_file: Callable[[str], bool]
    #: True if a repo-relative path is on disk today, as a file or as a directory.
    exists: Callable[[str], bool] = lambda rel: False
    #: How a command key is shown (identity, or ``attribution.public_key`` without excerpts).
    command_key: Callable[[str], str] = str
    #: How a tool name is shown (identity, or ``attribution.public_tool`` without excerpts).
    tool_name: Callable[[str], str] = str
    #: True if a top-level module/package name belongs to the repository itself.
    first_party: Callable[[str], bool] = lambda top: False
    #: The episode of work an event of a session belongs to (default: the session).
    unit: Callable[[str, datetime | None], str] = lambda session, ts: session
    #: The one directory of the repository with this name (``packages/web`` for ``web``),
    #: repo-relative; ``None`` if there is none or more than one.
    workspace_dir: Callable[[str], str | None] = lambda name: None


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


def _said_anything(output: str) -> bool:
    """Is there anything in ``output`` beyond the harness's own "Exit code N" line?"""
    return bool(_EXIT_LINE_RE.sub("", output).strip())


def _capped(start: float | None, end: float | None) -> tuple[float, float] | None:
    """Interval [start, end] limited to TIME_CAP_SECONDS from its start."""
    if start is None or end is None or end <= start:
        return None
    return (start, min(end, start + TIME_CAP_SECONDS))


def _resumed(action: _Action) -> float | None:
    """When the agent next did something after ``action``; ``None`` after an idle gap."""
    done = _epoch(action.result) if action.result is not None else None
    after = action.next_any
    if after is not None and done is not None and after - done > IDLE_GAP_SECONDS:
        return None
    return after


def _awake(
    interval: tuple[float, float] | None, gaps: Sequence[tuple[float, float]]
) -> tuple[float, float] | None:
    """``interval`` without the idle gaps it spans: what came before the first of them.

    If nothing came before it (the interval starts at the gap), what came after.
    """
    if interval is None:
        return None
    start, end = interval
    for idle_from, idle_to in gaps:
        if idle_from >= end or idle_to <= start:
            continue
        if idle_from > start:
            return (start, idle_from)
        start = min(idle_to, end)
    return (start, end)


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
    #: An instruction, not a verdict: unless the agent acts on it, nothing is recorded.
    tentative: bool = False
    #: The weaker cues the same message carries: what is left of it if nothing follows.
    fallback: str = ""
    #: Files the human named *instead* ("wrong file, the bug is in parser.py"): repo
    #: paths and bare file names. Going there next confirms the correction too.
    elsewhere: frozenset[str] = frozenset()
    #: The human said which file it is about: going back elsewhere does not change that.
    settled: bool = False
    redirected: bool = False
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
    # files the session has read or changed successfully
    had: set[str] = field(default_factory=set)
    #: Commands so far that rewrite files (a formatter, `--fix`, `sed -i`, a commit hook),
    #: and how many of them each file had seen when the agent last read or edited it.
    rewrites: int = 0
    seen_rewrites: dict[str, int] = field(default_factory=dict)
    read_findings: dict[tuple[str, str], Finding] = field(default_factory=dict)
    # (old, new) of each successful edit -> (when, successful commands so far, only added?)
    edit_history: dict[str, dict[tuple[str, str], tuple[float | None, int, bool, int]]] = field(
        default_factory=dict
    )
    ok_commands: int = 0
    checks_run: int = 0  # test, lint, build or aggregate commands that ran, red or green
    write_checks: dict[tuple[str, str], int] = field(default_factory=dict)
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
    known_files: set[str] = field(default_factory=set)  # files read or changed so far
    changes_at: dict[int, int] = field(default_factory=dict)  # ``changes`` per failure
    tests_run: dict[int, int] = field(default_factory=dict)  # tests a red run counted
    red_seen: set[str] = field(default_factory=set)  # keys and labels that ran red
    turn_of: dict[int, int] = field(default_factory=dict)  # the turn a failure was seen in
    trips: dict[int, tuple[float, float]] = field(default_factory=dict)  # see ``_promote``
    key_of: dict[int, str] = field(default_factory=dict)  # command key per failure
    # failures for which another command stood in, per failed command
    replaced: dict[str, list[Finding]] = field(default_factory=dict)
    # setup command that succeeded after a failure that is still pending, by finding id
    setup_after: dict[int, str] = field(default_factory=dict)
    # failures whose setup steps are settled: the same command has run again since
    bound: set[int] = field(default_factory=set)
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
    # a correction the human blamed on themselves: (prompt, files changed before it, phrases)
    softened: tuple[Event, frozenset[str], str] | None = None
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
    undo_asked: bool = False  # the prompt of this turn asks for an undo
    interrupted: Finding | None = None  # the interrupt that ended the turn, if one did
    # files the prompt of this turn was read as a correction, or a probable one, about
    corrected: frozenset[str] = frozenset()
    added: list[str] = field(default_factory=list)  # text of the latest successful edits
    turn_misses: list[tuple[Finding, str]] = field(default_factory=list)


@dataclass
class _Cycle:
    """Edit -> red run -> edit -> red run ... of one command within a turn."""

    seen: int  # ``_State.changes`` at its last run
    count: int = 0
    result: Event | None = None
    command: str = ""
    label: str = ""
    baseline: bool = False  # its first red run came before any change of the turn
    judged: bool = False  # a red run of it was read from output, not from an exit status
    unknown: bool = False  # its latest run showed neither red nor green
    spans: list[tuple[float, float]] = field(default_factory=list)  # red runs, edits between
    files: dict[str, int] = field(default_factory=dict)  # edits between its red runs
    before: dict[str, int] = field(default_factory=dict)  # edits before its first red run
    named: set[str] = field(default_factory=set)  # of those, files its red output names


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
        self.exposed_in: set[tuple[str, str, str]] = set()  # ... and in which episode
        self.skipped_events = 0  # events a detector could not handle (never fatal)
        # Transcript lines of calls that were refused or failed while acting outside the
        # repository: seen, and deliberately not counted as this repository's friction.
        self.outside: list[int] = []
        self._durations: dict[str, list[tuple[_Action, float]]] = {}
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
        for target in targets:
            self._see(target.kind, target.key, event)
        self.state.emitted_at[id(finding)] = self.state.ok_actions
        if signal in _FILE_TROUBLE:
            self.state.turn_trouble_files.update(t.key for t in targets if t.kind == model.FILE)
        elif signal in _TROUBLE:
            self.state.turn_trouble = True
        return finding

    def _tool(self, call: Event) -> str:
        """The tool's name as it may be shown (a pseudonym without excerpts)."""
        return self.ctx.tool_name(call.tool)

    def _label(self, key: str, step: Step | None, cwd: str | None) -> str:
        """How a command is named in the result: its key, and where in the repo it ran.

        ``npm test`` in ``web/`` and in ``api/`` of a monorepo are different commands:
        ``npm test (web/)``. At the repo root the key stands alone.
        """
        detached = key.endswith(" -d")  # a setup label: `docker compose up -d`
        name = self.ctx.command_key(key[:-3] if detached else key) + (" -d" if detached else "")
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

    def _started(self, action: _Action) -> float | None:
        """When the call began to cost the agent's time, as far as that can be known.

        Between a call and its result lies the tool's own run time and, where a
        person had to approve the call first, that person's time. The second is
        never counted:

        * the transcript says the call was let through by the permission settings
          (``prompted`` is False): the whole of call to result is the tool's;
        * it says a person approved it: nothing before the result is claimed;
        * it does not say: call to result counts up to the larger of
          ``UNATTENDED_RUN_SECONDS`` and the median time the same command (or tool)
          took in its other runs of the session. A run that took far longer than
          that was most likely waiting at a prompt.
        """
        called, ended = _epoch(action.call), _epoch(action.result)
        result = action.result
        if called is None or ended is None or result is None or ended <= called:
            return called
        if result.prompted is False:
            return called
        if result.prompted:
            return ended
        others = [
            d for a, d in self._durations.get(self._duration_key(action), ()) if a is not action
        ]
        bound = max(statistics.median(others) if others else 0.0, UNATTENDED_RUN_SECONDS)
        return max(called, ended - bound)

    @staticmethod
    def _duration_key(action: _Action) -> str:
        return action.cmd.key if action.cmd is not None else f"tool:{action.call.tool}"

    def _round_trip(self, action: _Action) -> tuple[float, float] | None:
        """From a call until the next transcript event: the cost of one wasted round trip."""
        end = _resumed(action)
        return _capped(self._started(action), end if end is not None else _epoch(action.result))

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
        if step is None:
            return cwd
        here = (self.ctx.resolver.chdir(cwd, step.moves) or cwd) if step.moves else cwd
        if step.workspace:
            # `pnpm --filter web test`: in `web/` if that is a directory here, in the one
            # workspace directory called `web` (`packages/web/`) if there is exactly one,
            # and nowhere in particular otherwise.
            resolver = self.ctx.resolver
            base = resolver.resolve(here or ".", None) if here else ""
            if base is not None:
                direct = posixpath.join(base, step.workspace)
                found = (
                    direct if self.ctx.exists(direct) else self.ctx.workspace_dir(step.workspace)
                )
                if found:
                    return posixpath.join(resolver.primary, found)
        return here

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
        if is_inline_code(step.key) and cwd and not resolver.contains(cwd):
            return True  # an ad-hoc snippet run from somewhere else is about somewhere else
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
                self._see(model.FILE, action.file, event)
        elif event.op == model.OP_SHELL and event.command:
            parsed = parse_command(event.command, is_windows_path(event.cwd) or None)
            primary = next((s for s in parsed.steps if s.key == parsed.key), None)
            # ``cd /abs/repo && make`` from a session started in a parent directory runs
            # in the repo: what counts is where the step runs, not where the shell began.
            inside = resolver.contains(self._where(primary, event.cwd))
            if inside and parsed.key and not self._elsewhere(primary, event.cwd):
                action.cmd = parsed
                if not parsed.inspection:
                    label = self._label(parsed.key, primary, event.cwd)
                    self._see(model.COMMAND, label, event)
        elif event.op == model.OP_SEARCH:
            scope = _scope(event, self.ctx)
            if scope is not None:
                is_file = bool(scope) and self.ctx.is_file(scope)
                self._see(model.FILE if is_file else model.DIR, scope or ".", event)

    def _see(self, kind: str, key: str, event: Event) -> None:
        self.exposed.add((kind, key))
        self.exposed_in.add((kind, key, self.ctx.unit(self.stream.session_id, event.ts)))

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
        items = self._items()
        for item in items:
            # how long each command took, for `_started`
            if isinstance(item, _Action) and item.result is not None:
                called, ended = _epoch(item.call), _epoch(item.result)
                if called is not None and ended is not None and not item.result.prompted:
                    key = self._duration_key(item)
                    self._durations.setdefault(key, []).append((item, ended - called))
        for item in items:
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
        # A command pointed at a file that does not exist, and that was never created
        # afterwards: the agent guessed a path. That is a miss on the path, and says
        # nothing about how the command is run.
        waiting = {id(m[0]) for m in state.missing}
        for finding in self.findings:
            rel = finding.detail.get("missing_file", "")
            if rel and id(finding) in waiting and finding.signal == COMMAND_FAILURE:
                command = next((t.key for t in finding.targets if t.kind == model.COMMAND), "")
                finding.signal = PATH_MISS
                finding.targets = [Target(model.FILE, rel)]
                finding.summary = f"`{command}` was pointed at {rel}, which does not exist"
                finding.detail["variant"] = "command"
                self.exposed.add((model.FILE, rel))
                unit = self.ctx.unit(self.stream.session_id, finding.ts)
                self.exposed_in.add((model.FILE, rel, unit))
        # A red run with nothing after it: the session was still going when it was read,
        # or was abandoned there. Nobody can say whether the failure got resolved.
        acted = [e.line for e in self.stream.events if e.kind != model.AGENT_MESSAGE]
        for finding in self.findings:
            if acted and finding.signal == COMMAND_FAILURE and finding.line == acted[-1]:
                finding.detail["trailing"] = "1"
        # Nobody was working while the laptop slept: no interval reaches across a silence
        # of more than ``IDLE_GAP_SECONDS``, in the middle of a turn or anywhere else.
        gaps = self._idle_gaps()
        for finding in self.findings if gaps else ():
            finding.interval = _awake(finding.interval, gaps)
            finding.run = _awake(finding.run, gaps)
            finding.intervals = tuple(
                kept for kept in (_awake(i, gaps) for i in finding.intervals) if kept is not None
            )
        return self.findings

    def _idle_gaps(self) -> list[tuple[float, float]]:
        """Silences in the stream longer than ``IDLE_GAP_SECONDS``, as ``(from, to)``.

        The time between a call and its own result is the command running, however long.
        """
        gaps: list[tuple[float, float]] = []
        before: Event | None = None
        for event in self.stream.events:
            now, then = _epoch(event), _epoch(before) if before is not None else None
            if now is None:
                continue
            answered = (
                before is not None
                and before.kind == model.TOOL_CALL
                and event.kind == model.TOOL_RESULT
            )
            if then is not None and now - then > IDLE_GAP_SECONDS and not answered:
                gaps.append((then, now))
            before = event
        return gaps

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
        if state.turn_check_ok is not None:
            for finding in state.thrash.values():
                finding.detail["checked"] = "1"  # a test, lint or type check ran in the turn
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
        # The files edited between the red runs; if there were none (one edit, then only
        # re-runs), the ones edited before the first. Where the failing output names
        # some of them, the others were bystanders. Shares follow the number of edits.
        edited = cycle.files or cycle.before
        blamed = {path: n for path, n in edited.items() if path in cycle.named} or edited
        ranked = sorted(blamed.items(), key=lambda kv: (-kv[1], kv[0]))
        ranked = ranked[:NO_CONVERGENCE_MAX_FILES]
        files = [path for path, _ in ranked]
        total = sum(n for _, n in ranked)
        targets = [Target(model.COMMAND, cycle.label)]
        targets += [Target(model.FILE, path, n / total) for path, n in ranked]
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
            f"`{cycle.label}` was still failing after "
            + (
                f"{cycle.count} runs with edits in between"
                if cycle.baseline
                else f"{cycle.count} rounds of edit and re-run"
            )
            + f"{where}; {ending}",
            targets,
            excerpt=f"$ {cycle.command}\n{cycle.result.output}",
            detail={"count": str(cycle.count), "command": cycle.command},
        )
        finding.intervals = tuple(sorted(spans))
        if uncertain:
            finding.detail["uncertain"] = "1"
            return
        # The many edits to these files *are* the rounds just reported: one stall, said once.
        state = self.state
        same = {path: state.thrash[path] for path in files if path in state.thrash}
        self._drop({id(found) for found in same.values()})
        for path in same:
            del state.thrash[path]

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
        if self._acted_outside(action, result):
            self.outside.append(result.line)
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
        elif op == model.OP_OTHER and not result.ok:
            self._on_tool_failure(action, result)

    def _acted_outside(self, action: _Action, result: Event) -> bool:
        """Did a call that went wrong act on something outside the repository?"""
        call = action.call
        if result.ok or result.error in (model.ERR_INTERRUPTED, model.ERR_NOT_RUN):
            return False
        if call.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE):
            return bool(call.path) and action.file is None
        if call.op == model.OP_SHELL and call.command and action.cmd is None:
            try:
                parsed = parse_command(call.command, is_windows_path(call.cwd) or None)
            except Exception:  # already counted as skipped when the call was classified
                return False
            return bool(parsed.key) and not parsed.inspection
        return False

    def _on_tool_failure(self, action: _Action, result: Event) -> None:
        """An MCP tool, a web fetch or a sub-agent returned an error."""
        call = action.call
        if not call.tool or QUESTION_TOOL_RE.search(call.tool):
            return
        tool = self._tool(call)
        self._emit(
            TOOL_FAILURE,
            result,
            f"{tool} returned an error",
            [],
            excerpt=result.output,
            detail={"tool": tool},
        )

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
                    f"{self._tool(call)} on {path} failed: the text to replace was not found",
                    targets,
                    excerpt=result.output,
                    interval=interval,
                    detail={
                        "variant": "not-found",
                        # a formatter, a fixer or a hook ran since the agent last saw it
                        **(
                            {"rewritten": "1"}
                            if self.state.rewrites > self.state.seen_rewrites.get(path, 1 << 30)
                            else {}
                        ),
                    },
                )
            elif result.error == model.ERR_EDIT_AMBIGUOUS:
                count = result.matches or 0
                self._emit(
                    EDIT_MISS,
                    result,
                    f"{self._tool(call)} on {path} failed: the text to replace matched "
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
                # A whole-file write to a path the session had never seen is not an edit
                # of stale text: the agent was creating the file (or thought so), and the
                # harness's read-first rule got in the way. That says nothing about the file.
                unread = not stale and call.op == model.OP_WRITE and path not in self.state.had
                variant = "modified" if stale else "unread-write" if unread else "not-read"
                self._emit(
                    STALE_EDIT,
                    result,
                    f"{self._tool(call)} on {path} refused: {reason}",
                    targets,
                    excerpt=result.output,
                    interval=None if unread else interval,
                    detail={"variant": variant},
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
        state.had.add(path)
        state.seen_rewrites[path] = state.rewrites
        self._on_change(path, _span(action))
        if call.new_text:
            state.added.append(call.new_text)
            del state.added[:-ADDED_TEXTS_KEPT]
        if call.old_text:
            state.removed.append((call.old_text, call.new_text))
            del state.removed[:-REMOVED_TEXTS_KEPT]
        self._explain_probes(path, call)
        self._confirm_correction({path}, f"the agent then changed {path} again")
        pending = state.unconfirmed
        if (
            pending is not None
            and not pending.note
            and path not in pending.files
            and (path in pending.elsewhere or path.rsplit("/", 1)[-1] in pending.elsewhere)
        ):
            pending.note = f"the agent then went to {path}, the file the user named instead"
            pending.redirected = True
        self._note_rework({path}, action)
        if call.op == model.OP_WRITE:
            # Reading a file to see whether it exists, then creating it, is not a miss.
            self._drop({id(f) for f in state.missing_reads.pop(path, [])})
        # Creating the module a command could not import is test-first development: the
        # failure becomes red -> change, and is forgotten when the command goes green.
        # Only a new file counts. Editing a package that was there all along did not
        # supply it: there the command was run in a way that could not find it.
        created = call.op == model.OP_WRITE and path not in state.known_files
        state.known_files.add(path)
        for entry in [m for m in state.missing if created and provides(path, m[2], m[3])]:
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
        checked = False  # a check ran between the change and its undoing
        history = state.edit_history.setdefault(path, {})
        for old, new in call.edits:
            if old == new:
                continue
            if (new, old) in history:
                made_at, commands_then, inserted, checks_then = history.pop((new, old))
                if inserted and state.ok_commands > commands_then:
                    # Lines were only added (or a switch was flipped: `DEBUG = True`), a
                    # command ran successfully with them in place, and now they are gone
                    # again: a temporary debug print, not a retreat.
                    continue
                reverted = True
                checked = checked or state.checks_run > checks_then
                # Only within one turn is the time in between the detour itself; an undo
                # of work from an earlier turn costs the undoing call and nothing more.
                same_turn = state.edit_turns.pop((path, new, old), -1) == state.turns
                reverted_at = made_at if same_turn else None
            else:
                history[(old, new)] = (
                    now,
                    state.ok_commands,
                    (old, new) in call.insertions,
                    state.checks_run,
                )
                state.edit_turns[(path, old, new)] = state.turns
        if call.op == model.OP_WRITE and call.content_digest:
            versions = state.write_history.setdefault(path, [])
            if versions and versions[-1] != call.content_digest and call.content_digest in versions:
                reverted = True
                written = state.write_times.get((path, versions[-1]))
                since = state.write_checks.get((path, versions[-1]), state.checks_run)
                checked = checked or state.checks_run > since
                same_turn = state.write_turns.get((path, versions[-1]), -1) == state.turns
                reverted_at = written if same_turn else None
            versions.append(call.content_digest)
            state.write_times[(path, call.content_digest)] = now
            state.write_checks[(path, call.content_digest)] = state.checks_run
            state.write_turns[(path, call.content_digest)] = state.turns
        # Taking a change back in answer to the human is not a second event: either they
        # asked for the undo in so many words, or the turn opened with a correction about
        # this very file. The correction is the event.
        if reverted and not state.undo_asked and path not in state.corrected:
            self._emit(
                EDIT_REVERT,
                result,
                f"{self._tool(call)} on {path} undid an earlier edit from the same session"
                + (", after a check had run on it" if checked else ""),
                targets,
                interval=_capped_back(reverted_at, now) or self._round_trip(action),
                detail={"checked": "1"} if checked else None,
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
        if not path and (by is None or not by[1]):
            state.rewrites += 1  # not an edit tool, not a setup step: it rewrites files
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
                        if id(finding) not in state.bound:
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
            f"{self._tool(action.call)} targeted {action.file}, which does not exist"
            + (" (a routine look for an optional file)" if probe else ""),
            [Target(model.FILE, action.file)],
            excerpt=result.output,
            interval=None if probe else self._round_trip(action),
            detail={"probe": "1"} if probe else None,
        )
        if action.call.op == model.OP_READ:
            self.state.missing_reads.setdefault(action.file, []).append(finding)

    def _powershell_miss(self, action: _Action, result: Event) -> bool:
        """``Cannot find path '...' because it does not exist``: a miss on that path.

        True if the result was such a message (whether or not the path is ours): the
        cmdlet did not fail as a command, it was pointed at something that is not there.
        """
        found = _PS_MISSING_RE.search(result.output)
        if found is None or result.ok:
            return False
        cmdlet = found.group(1) or ""
        rel = self.ctx.resolver.resolve(found.group(2), action.call.cwd)
        if not rel or self.ctx.exists(rel) or self.ctx.is_known_file(rel):
            return True  # outside the repo, or there after all
        rel = rel.rstrip("/")
        moving = cmdlet.lower() in _PS_MOVING
        listing = cmdlet.lower() in _PS_LISTING or PROBE_FILE_RE.search(rel) is not None
        name = f"`{self.ctx.command_key(cmdlet)}`" if cmdlet else "A PowerShell command"
        detail = {"variant": "directory"} if moving else {"variant": "command"}
        if listing and not moving:
            detail["probe"] = "1"
        self._emit(
            PATH_MISS,
            result,
            f"{name} was pointed at {rel}, which does not exist"
            + (" (a look at whether it is there)" if "probe" in detail else ""),
            [Target(model.FILE, rel)],
            excerpt=result.output,
            interval=None if "probe" in detail else self._round_trip(action),
            detail=detail,
        )
        return True

    def _cd_miss(self, action: _Action, result: Event, name: str) -> None:
        """A `cd` into a directory that is not there: a miss on that path."""
        cmd = action.cmd
        moved = [s for s in cmd.steps if s.program in ("cd", "pushd") and s.moves] if cmd else []
        step = next((s for s in moved if s.moves[-1] == name), None)
        if step is None and len(moved) == 1:
            step = moved[0]
        if step is None:
            return
        resolver = self.ctx.resolver
        base = resolver.chdir(action.call.cwd, step.moves[:-1]) if step.moves[:-1] else None
        rel = resolver.resolve(step.moves[-1], base or action.call.cwd)
        if not rel or self.ctx.exists(rel) or self.ctx.is_known_file(rel):
            return  # outside the repo, or there after all: the shell stood somewhere else
        rel = rel.rstrip("/")
        self._emit(
            PATH_MISS,
            result,
            f"`cd` was pointed at {rel}, which does not exist",
            [Target(model.FILE, rel)],
            excerpt=result.output,
            interval=self._round_trip(action),
            detail={"variant": "directory"},
        )

    def _on_read(self, action: _Action, result: Event) -> None:
        self._looked_for(action.call.path or "")
        path = action.file
        if path is None:
            return
        if result.ok:
            self.state.had.add(path)
            self.state.seen_rewrites[path] = self.state.rewrites
        if not result.ok:
            if result.error == model.ERR_MISSING:
                self._path_miss(action, result)
            elif result.error == model.ERR_TOO_LARGE:
                self._emit(
                    FILE_TOO_LARGE,
                    result,
                    f"{self._tool(action.call)} of {path} was refused: the file is too large to "
                    "read in one piece",
                    [Target(model.FILE, path)],
                    excerpt=result.output,
                    interval=self._round_trip(action),
                )
            return
        state = self.state
        key = (path, action.call.read_range)
        state.known_files.add(key[0])
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

    def _looked_for(self, text: str, but_not: Finding | None = None) -> None:
        """The agent goes on looking: earlier misses of this turn for it wanted a hit.

        A reworded search, or a read or glob of a path named like what was searched
        for, shows that the empty result was not what the agent hoped to see.
        """
        words = _search_words(text)
        for finding, _ in self.state.turn_misses:
            if finding is not but_not and words & _search_words(finding.detail["pattern"]):
                finding.detail["wanted"] = "1"

    def _on_search(self, action: _Action, result: Event) -> None:
        call = action.call
        wanted = (call.pattern or "").casefold()
        self._looked_for(call.pattern or "")
        if result.ok and not result.empty and wanted:
            # Found after all, elsewhere, in the same turn: the misses were the search.
            found = {id(f) for f, pattern in self.state.turn_misses if pattern == wanted}
            if found:
                self._drop(found, DROPPED_DEV_LOOP)
                self.state.probes = [p for p in self.state.probes if id(p[0]) not in found]
                self.state.turn_misses = [
                    m for m in self.state.turn_misses if id(m[0]) not in found
                ]
        if not result.ok and result.error == model.ERR_MISSING and call.path:
            # "Path does not exist": the search never ran, the agent guessed a directory
            rel = (self.ctx.resolver.resolve(call.path, call.cwd) or "").rstrip("/")
            if rel and not self.ctx.exists(rel) and not self.ctx.is_known_file(rel):
                self._emit(
                    PATH_MISS,
                    result,
                    f"{self._tool(call)} was pointed at {rel}, which does not exist",
                    [Target(model.FILE, rel)],
                    excerpt=result.output,
                    interval=self._round_trip(action),
                    detail={"variant": "search"},
                )
            return
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
            f"{self._tool(call)} search in {where} returned nothing",
            [Target(model.FILE if is_file else model.DIR, scope or ".")],
            excerpt=f"pattern: {call.pattern}" if call.pattern else "",
            # a weak signal: it may be a lead, but it claims no time
            detail={"pattern": call.pattern or "", "scope": scope or "."},
        )
        if HYGIENE_PATTERN_RE.search(pattern):
            finding.detail["hygiene"] = "1"  # finding nothing is what such a search is for
        self.state.probes.append((finding, call, scope))
        if wanted:
            self.state.turn_misses.append((finding, wanted))

    # ---- shell -----------------------------------------------------------------------

    def _on_shell(self, action: _Action, result: Event) -> None:
        cmd = action.cmd
        if cmd is None:
            return
        if result.background:
            # Launched in the background: this only says that it started. Neither red nor
            # green, no fix and no recovery; its outcome, if reported, is judged then.
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

    def _just_imported(self, result: Event) -> bool:
        """Is the module a failure reports missing one that a recent edit imports?"""
        missing = missing_name(_QUOTED_LINE_RE.sub("", result.output)) or missing_name(
            result.output
        )
        if missing is None or missing[0] != "module":
            return False
        top = re.escape(missing[1].strip("'\"").split(".")[0].split("/")[0])
        wanted = re.compile(
            rf"\b(?:import|from)\s+{top}\b|\brequire\(\s*['\"]{top}\b|\bfrom\s+['\"]{top}\b"
        )
        return bool(top) and any(wanted.search(text) for text in self.state.added)

    def _possibly_changed(self, step: Step, cwd: str | None) -> None:
        """``step`` ran and may have changed something (see ``may_change_files``)."""
        if is_inline_code(step.key):
            # `python -c ...`, `node -e ...`, a heredoc: every such snippet has the same
            # key and each one is different. One that writes files is an edit and was
            # counted as one; any other changed nothing that a later session could
            # repeat, so it is nobody's setup step.
            return
        copied = env_copy(step)
        setup = copied is not None or is_setup_step(step)
        if not setup and not may_change_files(step.key):
            return
        # Version control records work, it does not make a failing command pass.
        named = step.program not in ("git", "gh", "glab")
        label = copied if copied and self.ctx.command_key is str else None
        label = label or self._label(setup_label(step), step, cwd)
        self._on_change(certain=False, by=(label, setup) if named else None)

    def _judge_shell(self, action: _Action, cmd: ParsedCommand, result: Event, edit: bool) -> None:
        state = self.state
        key = cmd.key
        located: Step | None = self._primary(action)

        def shown(name: str) -> str:
            return self._label(name, located, action.call.cwd)

        # Looking around (`ls .venv/bin`, `which pytest`, `cat pyproject.toml`) is how
        # the agent finds the command that works: it does not use up the look-ahead.
        spent = 0 if cmd.inspection else 1
        state.pending = [(f, k, n - spent) for f, k, n in state.pending if n > spent]
        if edit:
            self._on_change()  # `sed -i`, a formatter, `--fix`: a change like any edit
        # `pytest | tail -5` exits with the status of `tail`. Exit 0 is then no verdict:
        # the output decides, and where it does not, the run is neither red nor green.
        ok = result.ok
        verdict: str | None = None
        hidden = result.ok and cmd.masked and not cmd.inspection
        line = action.call.command or ""
        # `rg foo | head` where the shell could not find `rg`: the pipe's status hides
        # it, the shell's own words do not.
        unfound = _unfound_program(result.output) if cmd.inspection else ""
        no_tool = bool(unfound) and unfound in {
            name for s in cmd.steps for name in (s.program, *s.wrappers)
        }
        if no_tool and result.ok and cmd.masked:
            ok, hidden = False, True
        if (
            not result.ok
            and result.error == model.ERR_EXIT
            and result.exit_code not in INVOCATION_EXIT_CODES
            and not cmd.inspection
            and cmd.steps
            and cmd.steps[-1].inspection
            and cmd.steps[-1] is not located
            and status_is_last_steps(line)
        ):
            # `pytest; which time`: the status is that of the `which`, a question asked
            # afterwards. What the command before it did, only its output can say.
            hidden = True
        # ... and a document printed by a later step (`...; cat docs/troubleshooting.md`)
        # is not the command's own output: an error in it may be the document's, and
        # the two cannot be told apart. Nothing is read into such output.
        quoted = hidden and prints_another_file(line)
        ran = not cmd.inspection and result.error in ("", model.ERR_EXIT, model.ERR_OTHER)
        # In `ruff check .; mypy; pytest` every step prints its own verdict. Each is judged
        # by its own summary line; one red step does not make the others red.
        several = len({s.key for s in _candidates(cmd)}) > 1
        own = step_verdicts(cmd, result.output) if ran and several and not quoted else {}
        red_step: Step | None = None
        setup_error: bool | None = None  # for a hidden status: was it a setup error?
        if hidden and several:
            # Nothing says that anything failed at all, so a step is red only on its own
            # evidence: its summary line, or a setup error that can be pinned on it.
            red_step = next((s for s in _candidates(cmd) if own.get(s.key) == "red"), None)
            setup_error = False
            own_module = own_module_missing(result.output, self.ctx.first_party)
            if (
                red_step is None
                and not quoted
                and (own_module or is_invocation_error(result.output, None, self.ctx.first_party))
            ):
                blamed, sure = self._blamed_step(cmd, result.output)
                if sure and blamed is not None and own.get(blamed.key) != "green":
                    red_step, setup_error = blamed, not own_module
            verdict = "red" if red_step is not None else own.get(key)
            ok = verdict != "red"
        elif hidden:
            verdict = (
                None
                if quoted
                else output_verdict(result.output, self.ctx.first_party, not ends_looking(line))
            )
            # `cmd; echo "exit $?"`: the line says how its one command ended
            echoed = echoed_status(action.call.command or "", result.output)
            if echoed:
                verdict = "red"
            elif echoed == 0 and verdict is None:
                verdict = "green"
            ok = verdict != "red"
        unknown = hidden and verdict is None
        if ran:
            # A step that printed its own passing summary ran green, whatever came after.
            for step_key, step_verdict in own.items():
                if step_verdict == "green" and (step_key != key or not ok or unknown):
                    self._went_green(step_key, line=action.call.command or "", output=result.output)
            if result.ok:
                # ... and so did any check whose passing summary an exit-0 command shows
                # (`make check` printing "All checks passed!" for an earlier red `ruff`).
                for failed_key in [k for k in state.cycles if k != key]:
                    if key_verdict(failed_key, result.output) == "green":
                        self._went_green(
                            failed_key,
                            by=key,
                            line=action.call.command or "",
                            output=result.output,
                        )
        if unknown and key in state.cycles:
            state.cycles[key].unknown = True
        if not cmd.inspection and result.error != model.ERR_INTERRUPTED and not unknown:
            state.turn_shell_ok = ok
            if command_role(key) is not None or is_aggregate_check(key):
                state.turn_check_ok = ok
                state.checks_run += 1
                if ok:
                    # Reading a file again between green runs of a check is following
                    # along while things work, not going round in circles.
                    state.reads.clear()
                    state.read_findings.clear()
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
                if len(streak) >= POLL_MIN_FAILURES and command_role(key) == "test":
                    # Nobody polls a test suite: failing and then passing untouched is
                    # flakiness. The red runs stay (the engine decides); the loop does not.
                    if same in state.retry:
                        waited.add(id(state.retry[same]))
                    continue
                probing = cmd.program in _PROBE_PROGRAMS and all(
                    f.detail.get("environment") == ENV_SERVICE for f in streak
                )
                if len(streak) >= POLL_MIN_FAILURES or probing:
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
            self._went_green(
                key,
                exact=located is not None and is_setup_step(located),
                line=action.call.command or "",
                label=shown(key),
                output=result.output,
            )
            # The command works after all, so nothing "replaced" it earlier in the session.
            for finding in state.replaced.pop(key, []):
                finding.detail.pop("recovered_by", None)
                finding.detail.pop("green_as", None)
            # A tool that was not installed, and then the same job done with what is
            # there: `grep` after `rg: command not found`, `python3` after `jq`.
            programs = {s.program for s in cmd.steps}
            for entry in list(state.pending):
                finding = entry[0]
                stand_in = sorted(
                    programs & _STAND_INS.get(finding.detail.get("not_found", ""), _NONE)
                )
                if stand_in and "works_as" not in finding.detail:
                    finding.detail["works_as"] = stand_in[0]
                    finding.detail["stand_in"] = "1"  # another tool, not another name
                    self._recover(finding, action)
                    state.pending.remove(entry)
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
                    launcher = unfound.startswith("python") and re.search(
                        r"(?:^|[\s;&|(/\\])py(?:\.exe)?(?=\s)", action.call.command or "", re.I
                    )
                    if unfound and other is not None:
                        finding.detail["works_as"] = unfound + other.group(1)
                    elif launcher:
                        finding.detail["works_as"] = "py"  # the Windows launcher
                    elif unfound:
                        # ... or by its path: `./bin/zebractl` for `zebractl`
                        by_path = re.search(
                            rf"(?:^|[\s;&|(])((?:\.{{1,2}}/)?(?:[\w.-]+/)+){re.escape(unfound)}(?=\s|$)",
                            action.call.command or "",
                        )
                        if by_path is not None:
                            shown_name = self.ctx.command_key(unfound)
                            finding.detail["works_as"] = by_path.group(1) + shown_name
                    # ... or with a variable in front: `PYTHONPATH=src pytest`
                    prefix = _env_prefix(action.call.command or "", self.ctx.command_key is str)
                    unchanged = state.changes_at.get(id(finding)) == state.changes
                    plain = not _env_prefix(finding.detail["command"], True)
                    if (prefix and unchanged and plain) and (
                        finding.detail["variant"] != "ordinary" or self._promote(finding)
                    ):
                        finding.detail["works_with"] = f"{prefix} {label}"
                    # ... or with a flag added: `npm install --legacy-peer-deps`
                    flags = _added_flags(finding.detail["command"], action.call.command or "")
                    if (
                        flags
                        and unchanged
                        and self.ctx.command_key is str
                        and "works_with" not in finding.detail
                        and finding.detail["variant"] != "ordinary"
                    ):
                        finding.detail["works_with"] = f"{label} {flags}"
                    # ... or by a path into the project's own environment:
                    # `.venv/bin/pytest` where a bare `pytest` lacked the dependencies
                    edge = r"(?:^|[\s;&|(])"
                    word = re.escape(cmd.program)
                    nearby = re.search(
                        rf"{edge}((?:\.{{1,2}}/)?(?:[\w.-]+/)+){word}(?=\s|$)",
                        action.call.command or "",
                    )
                    bare = re.search(rf"{edge}{word}(?=\s|$)", finding.detail["command"])
                    if (
                        (nearby and bare and unchanged and not unfound)
                        and "works_with" not in finding.detail
                        and (finding.detail["variant"] != "ordinary" or self._promote(finding))
                    ):
                        finding.detail["works_with"] = nearby.group(1) + label
                    # ... or with an environment activated first on the same line
                    activation = _activation(cmd, finding.detail["command"])
                    if (activation and unchanged) and (
                        finding.detail["variant"] != "ordinary" or self._promote(finding)
                    ):
                        activation = self.ctx.command_key(activation)
                        finding.detail.setdefault("fixed_by_setup", activation)
                        finding.summary += "; it passed with an environment activated first"
                    self._recover(finding, action)
                elif finding.detail["variant"] != "ordinary" and is_typo_of(failed_key, key):
                    # `npm run biuld`, then `npm run build`: a slip of the keyboard that
                    # the agent corrected itself. Nothing to document.
                    finding.detail["typo_of"] = shown(key)
                    finding.summary += f"; a mistyped `{shown(key)}`, which then ran"
                elif _same_job_other_manager(failed_key, key, finding.detail.get("not_found", "")):
                    # `pnpm: command not found`, then `npm install` works: the program
                    # is what was wrong, as with `python` and `python3`.
                    finding.detail["works_as"] = self.ctx.command_key(key.split()[0])
                    self._recover(finding, action)
                elif is_setup_command(key) and inner_key(key) != inner_key(failed_key):
                    if id(finding) not in state.bound:
                        step_name = shown(setup_label(located) if located else key)
                        steps = state.setup_after.get(id(finding), "").split(" && ")
                        steps = [s for s in steps if s and s != step_name] + [step_name]
                        state.setup_after[id(finding)] = " && ".join(steps)
                    waiting.append((finding, failed_key, left))
                elif is_replacement(failed_key, key) and finding.detail["variant"] != "ordinary":
                    # Only a command that could not run can be "replaced". A red test
                    # followed by another green test command is just a test going green.
                    better = shown(key)
                    local = re.search(
                        rf"(?:^|[\s;&|(])\./{re.escape(key.split()[0])}(?=\s|$)",
                        action.call.command or "",
                    )
                    if local is not None and not better.startswith("<"):
                        better = "./" + better  # as it must be typed: `./bin/zebractl`
                    finding.detail["recovered_by"] = better
                    state.replaced.setdefault(failed_key, []).append(finding)
                    self._recover(finding, action)
                else:  # an unrelated command (git, housekeeping, another job) fixes nothing
                    waiting.append((finding, failed_key, left))
            state.pending = waiting
            return
        cd_failed = _CD_FAILED_RE.search(result.output)
        if cd_failed is not None and any(s.moves for s in cmd.steps):
            # `cd missing-dir && npm test`: the shell stopped at the `cd`. Nothing ran
            # there, so there is no failure of `npm test (missing-dir/)` to record. The
            # agent guessed a directory, though: that is a miss on the path.
            if located is not None and located.moves:
                # ... and the command was not run in a directory that does not exist
                seen = (model.COMMAND, shown(key))
                self.exposed.discard(seen)
                self.exposed_in = {e for e in self.exposed_in if e[:2] != seen}
            self._cd_miss(action, result, next((g for g in cd_failed.groups() if g), ""))
            return
        if self._powershell_miss(action, result):
            return
        if (
            result.exit_code == TIMEOUT_EXIT_CODE
            and _TIMEOUT_WRAPPER_RE.search(action.call.command or "")
            and (
                READY_BANNER_RE.search(result.output)
                or not _said_anything(result.output)
                # a server or a watcher never exits by itself: the time box ending it is
                # the plan, whatever it logged meanwhile
                or DEV_SERVER_RE.search(action.call.command or "")
                or _WATCH_RE.search(action.call.command or "")
            )
            and not INVOCATION_ERROR_RE.search(result.output)
        ):
            # `timeout 10 npm run dev` that came up and was then stopped by its own time
            # box: the agent only wanted to see it start. And a time box the agent set
            # itself that ran out without a word (`timeout 5 nc -l 8080`) did what it was
            # asked to: the command was cut off, it did not fail.
            return
        code = None if hidden else result.exit_code
        if hidden and not several:
            code = echoed_status(action.call.command or "", result.output) or None
        if cmd.inspection:
            # Probing (ls, cat, git status, ...) is not friction, whatever it answers.
            # `docker ps` that cannot reach the daemon answered nothing, though.
            # Nor did a tool that is not installed (`rg: command not found`): the agent
            # has to find another way, every session, until somebody says which.
            down = environment_error(result.output)
            if not no_tool and (down is None or down[0] == ENV_VARIABLE or result.ok):
                return
        if code == 1 and cmd.last_program in MATCH_FILTERS and cmd.last_program != cmd.program:
            return  # `... | grep x` with nothing matched
        call = action.call
        timed_out = HARNESS_TIMEOUT_RE.match(result.output) is not None
        if result.harness_error and code is None and not timed_out:
            return  # the harness's own error message: the command was never executed
        invocation = is_invocation_error(result.output, code, self.ctx.first_party)
        if code not in INVOCATION_EXIT_CODES and _REGISTRY_AUTH_RE.search(result.output):
            invocation = False  # the registry wants a login: see `environment_error`
        step, certain = self._blamed_step(cmd, result.output)
        if red_step is not None:
            step, certain = red_step, True
            invocation = bool(setup_error)
        if self._elsewhere(step, call.cwd):
            self.outside.append(result.line)
            return  # the step that failed ran, or only touched things, outside the repo
        if step is not None and step.key:
            key = step.key
            located = step
            missing_wrapper = _unfound_program(result.output)
            if missing_wrapper in step.wrappers and missing_wrapper != step.program:
                # `/usr/bin/time -v pytest`: what could not be found is `time`
                key, invocation = missing_wrapper, True
        inline = is_inline_code(key)
        if inline and code not in INVOCATION_EXIT_CODES:
            # Ad-hoc code failing says something about that code, not about how to run
            # anything here. Only a missing interpreter is a setup problem.
            invocation = False
        if invocation and code not in INVOCATION_EXIT_CODES and self._just_imported(result):
            # The module that is missing is one the agent has just written an import
            # for: the code is wrong, not the way the command is run.
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
                ENV_PORT: "the port it needs was already in use",
                ENV_DISK: "the disk was full",
                ENV_NETWORK: "a host name could not be resolved",
            }[environment[0]]
        pushed = key == "git push" and PUSH_REJECTED_RE.search(result.output) is not None
        if pushed and environment is None and not invocation:
            kind = "the remote branch had commits the local one lacked"
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
        absent = missing_target(result.output) if invocation else None
        # ... if the name it prints is the one that was asked for: `make test` answering
        # "No rule to make target 'tst'" is a broken Makefile, not a missing `test`.
        if absent is not None and re.search(
            rf"(?<![\w-]){re.escape(absent[1])}(?![\w-])", call.command or ""
        ):
            detail["no_target_in"], detail["no_target"] = absent
        if (
            not invocation
            and not inline
            and own_module_missing(result.output, self.ctx.first_party)
        ):
            detail["own_module"] = "1"
        if pushed and environment is None and not invocation:
            detail["push_rejected"] = "1"
        if environment is not None:
            detail["variant"] = "environment"
            detail["environment"] = environment[0]
            if environment[0] == ENV_SERVICE and environment[1]:
                detail["service"] = environment[1]
            elif environment[0] == ENV_VARIABLE:
                detail["env_var"] = environment[1]
        name = _unfound_program(result.output) if invocation else ""
        if name and step is not None:
            # the key says `python` for `python3` and `py` alike: keep the word as typed
            typed = re.search(
                rf"(?:^|[\s;&|(/\\]){re.escape(name)}(?:\.exe)?(?=\s|$)", call.command or "", re.I
            )
            if name == step.program or (typed is not None and step.program == "python"):
                detail["not_found"] = name
                detail["not_found_shown"] = self.ctx.command_key(name)
        if not certain:
            detail["ambiguous_step"] = "1"
        if hidden:
            detail["status"] = "hidden"
        if inline:
            detail["inline"] = "1"
        elif detail["variant"] == "ordinary" and command_role(key) == "test":
            # Which tests were red: the same one, session after session, is a standing
            # failure, whatever each session was about.
            failed: list[tuple[str, str]] = []
            for test, path in failed_tests(result.output):
                rel = (self.ctx.resolver.resolve(path, call.cwd) or "") if path else ""
                known = bool(rel) and self.ctx.is_known_file(rel)
                failed.append((test, rel if known else ""))
            failed = failed[:MAX_FAILED_TESTS]
            if failed:
                detail["failed_tests"] = "\n".join(test for test, _ in failed)
                detail["failed_test_files"] = "\n".join(rel for _, rel in failed)
                if state.changes:
                    # red after the agent's own change: perhaps its doing, not a test
                    # that was broken before the session began
                    detail["after_edit"] = "1"
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
            # In `a; b; c` the tail of the output is c's; show the step that failed.
            excerpt=f"$ {call.command}\n"
            + _up_to_verdict(
                red_step,
                result.output,
                error=in_sequence(call.command or ""),
            ),
            weight=1.0 if invocation or environment else ORDINARY_FAILURE_WEIGHT,
            # An ordinary failure (a red test) is information, not lost time.
            interval=self._round_trip(action) if invocation or environment else None,
            detail=detail,
        )
        finding.run = _capped(self._started(action), _epoch(result))
        state.key_of[id(finding)] = key
        state.changes_at[id(finding)] = state.changes
        state.red_seen.update((key, shown(key)))
        state.turn_of[id(finding)] = state.turns
        counted = tests_counted(result.output)
        if counted:
            state.tests_run[id(finding)] = counted
        if "own_module" in detail:
            trip = self._round_trip(action)
            if trip is not None:
                state.trips[id(finding)] = trip
        # The command has run again: what was set up before this run belongs to the
        # failures before it, and what is set up from here on does not.
        for earlier, earlier_key, _ in state.pending:
            if earlier_key == key and id(earlier) in state.setup_after:
                state.bound.add(id(earlier))
        for table in (state.red, state.red_then_ran):
            for earlier in table.get(key, ()):
                if any(setup for _, setup in state.ran_after.get(id(earlier), ())):
                    state.bound.add(id(earlier))
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
                rel = self.ctx.resolver.resolve(missing[1], self._where(step, call.cwd))
                guessed = missing[0] == "file" and command_role(key) == "test"
                # ... a test file named on the command line itself
                if guessed and missing[1] in tokens and rel and not self.ctx.is_known_file(rel):
                    finding.detail["missing_file"] = rel
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
        name = _unfound_program(output)
        if name:
            named = next(
                (s for s in cmd.steps if s.key and name in (s.program, s.key, *s.wrappers)), None
            )
            if named is not None:
                step, certain = named, True
        return step, certain

    def _covered(
        self, finding: Finding, failed_key: str, line: str, output: str, same: bool = True
    ) -> bool:
        """Did the green run ``line`` run what ``finding``'s red run ran?

        `pytest tests/test_core.py` passing says nothing about the test that
        `make test` showed red. A green run is narrower when its line selects
        something the red line did not (a path, a node id, ``-k``, ``-m``, ``-t``,
        ``--filter``, ...), or when it counted far fewer tests. A narrower run still
        covers the failure if the red output named its failing tests and every one
        of them lies inside the selection.
        """
        if command_role(failed_key) != "test" or finding.detail.get("variant") != "ordinary":
            return True
        red_paths, red_options = selection(finding.detail.get("command", ""))
        paths, options = selection(line)
        ran, passed = self.state.tests_run.get(id(finding)), tests_counted(output)
        fewer = bool(ran and passed and passed < ran * SUBSET_PASS_SHARE)
        if not (paths - red_paths) and not (options - red_options) and not fewer:
            return True
        names = [n for n in finding.detail.get("failed_tests", "").split("\n") if n]
        files = finding.detail.get("failed_test_files", "").split("\n")
        if not names:
            return False
        added = options - red_options
        by_name = False
        if added and added <= _LAST_FAILED_OPTIONS and same:
            pass  # the tests that failed last time: exactly the ones the red run named
        elif added and added <= _NAMING_OPTIONS:
            # `-k test_total`, `-t adds`, `-run TestAdd`: a name that matches each failure
            wanted = [w.lower() for w in selected_names(line)]
            if not wanted or not all(
                any(w in _test_part(name).lower() for w in wanted) for name in names
            ):
                return False
            by_name = True
        elif added or not (paths - red_paths):
            return False  # some other selection, or simply far fewer tests
        for index, name in enumerate(names):
            if not (paths - red_paths):
                break
            known = files[index] if index < len(files) else ""
            where = [p for p in (known, name.split("::")[0] if "::" in name else "") if p]
            if by_name and not where:
                continue  # `go test ./pkg -run TestAdd`: the name is all there is to go by
            if not any(
                selects(path, there) and ("::" not in path or name.startswith(path))
                for there in where
                for path in paths
            ):
                return False
        return True

    def _went_green(
        self,
        key: str,
        exact: bool = False,
        by: str = "",
        line: str = "",
        label: str = "",
        output: str = "",
    ) -> None:
        """``key`` ran green: close what a green run of it, or of its job, closes.

        red -> edit -> green is the development loop working, so those failures are
        forgotten, also when the green run is another command doing the same job
        (`make test` after a red `pytest tests/test_a.py`) or an aggregate target that
        runs every check (`make check`, `npm run ci`, `pre-commit run`).
        """
        state = self.state
        actual = by or key  # the command that was run; ``key`` may be a check it printed

        def closes(failed_key: str) -> bool:
            if failed_key == key:
                return True
            if exact:
                return False
            if is_replacement(failed_key, key):
                return True
            return is_aggregate_check(key) and command_role(failed_key) is not None

        def take(table: dict[str, list[Finding]], failed_key: str) -> list[Finding]:
            """The failures of ``failed_key`` this green run answers; the others stay."""
            found = table.pop(failed_key)
            same = failed_key == actual
            left = [f for f in found if not self._covered(f, failed_key, line, output, same)]
            if left:
                # A narrower selection passing closes nothing: the red run is still red.
                table[failed_key] = left
            return [f for f in found if not any(f is other for other in left)]

        for failed_key in [k for k in state.cycles if closes(k)]:
            del state.cycles[failed_key]
        # What went green was not waiting for a file to be created (test-first).
        state.missing = [m for m in state.missing if not closes(m[1])]
        fixed: set[int] = set()
        for failed_key in [k for k in state.red_then_edited if closes(k)]:
            for finding in take(state.red_then_edited, failed_key):
                if "push_rejected" in finding.detail:
                    # Rejected, pulled, pushed: no file was fixed, the branch was behind.
                    # Once is routine; whether it is a habit, the engine can tell.
                    finding.detail["push_recovered"] = "1"
                    continue
                # A module of the repo that could not be imported, an edit, and then
                # *another* command for the job passes: the edit proves nothing about the
                # import, and the command that failed has still not been seen to work.
                if failed_key == actual or not self._promote(finding):
                    fixed.add(id(finding))
        # A type-check or lint run that was red, an edit, and then any check passing in
        # the same turn (`npx tsc --noEmit`, edit, `npm test`): the agent verified its fix
        # with another check. That is the development loop too. A real invocation error
        # of the checker is not closed by something else passing.
        if not exact and (command_role(actual) is not None or is_aggregate_check(actual)):
            for failed_key in [
                k for k in state.red_then_edited if command_role(k) in _STATIC_CHECKS
            ]:
                found = state.red_then_edited[failed_key]
                closed = [
                    f
                    for f in found
                    if f.detail.get("variant") == "ordinary"
                    and state.turn_of.get(id(f)) == state.turns
                ]
                left = [f for f in found if not any(f is c for c in closed)]
                if left:
                    state.red_then_edited[failed_key] = left
                else:
                    del state.red_then_edited[failed_key]
                    state.cycles.pop(failed_key, None)
                fixed |= {id(f) for f in closed}
        # Green after nothing but commands, with no file edited: what was wrong was the
        # environment, and the command that put it right is worth writing down.
        for failed_key in [k for k in state.red_then_ran if closes(k)]:
            for finding in take(state.red_then_ran, failed_key):
                if "push_rejected" in finding.detail:
                    finding.detail["push_recovered"] = "1"
                    state.ran_after.pop(id(finding), None)
                    continue
                ran = state.ran_after.pop(id(finding), [])
                setups = list(dict.fromkeys(label for label, setup in ran if setup))
                others = [label for label, setup in ran if not setup]
                if failed_key != actual and self._promote(finding):
                    # A package of the repo could not be imported, and what passed in
                    # the end was another command: that, not the setup step, is the lead.
                    continue
                if setups:
                    self._promote(finding)
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
            for finding in take(state.red, failed_key):
                if failed_key != actual:
                    # the package was there; another way of running the job found it
                    promoted = self._promote(finding)
                    if (
                        not promoted
                        and not exact
                        and finding.detail.get("variant") == "ordinary"
                        and is_replacement(failed_key, actual)
                        and not is_aggregate_check(actual)
                        and state.changes_at.get(id(finding)) == state.changes
                        # a runner that was itself red in this session is no answer
                        and actual not in state.red_seen
                    ):
                        # Another runner for the same job passes with nothing changed
                        # (`pnpm test` after a red `yarn test`): perhaps the wrong runner
                        # for this repository. Whether it recurs decides (engine).
                        finding.detail["green_as"] = self.ctx.command_key(actual)
                        state.replaced.setdefault(failed_key, []).append(finding)
                elif (
                    finding.detail.get("variant") == "ordinary"
                    and state.changes_at.get(id(finding)) == state.changes
                    and self._elsewhere_in_repo(finding, label)
                    and label not in state.red_seen
                    # failing tests the red run named outside that directory were not
                    # run there: its green says nothing about them
                    and self._failed_under(finding, label)
                ):
                    # The same command, red where the agent first ran it and green in a
                    # subdirectory with nothing changed (`pytest`, then `cd src &&
                    # pytest`): perhaps it has to be run there. Recurrence decides (engine).
                    was = finding.targets[0].key
                    finding.detail["green_in"] = label[len(was) + 2 : -1]
                elif (
                    finding.detail.get("variant") == "ordinary"
                    and command_role(failed_key) == "test"
                    # The same command again, whatever its flags (`pytest -v` after
                    # `pytest`, `make test V=1`). A narrower selection that leaves the
                    # failing test out (`pytest tests/test_cart.py` after a red `pytest`)
                    # never gets here: it closed nothing (`_covered`).
                    and line
                ):
                    # A test that fails and then passes untouched may be flaky. Whether
                    # it is, only its recurring across sessions can say (engine).
                    finding.detail["rerun_green"] = "1"
        self._drop(fixed, DROPPED_DEV_LOOP)

    def _failed_under(self, finding: Finding, label: str) -> bool:
        """Do the failing tests the red run named (if any) all lie in ``label``'s directory?"""
        was = finding.targets[0].key if finding.targets else ""
        directory = label[len(was) + 2 : -1]
        names = [n for n in finding.detail.get("failed_tests", "").split("\n") if n]
        files = finding.detail.get("failed_test_files", "").split("\n")
        for index, name in enumerate(names):
            known = files[index] if index < len(files) else ""
            where = known or (name.split("::")[0] if "::" in name else "")
            if not where.startswith(directory):
                return False
        return True

    @staticmethod
    def _elsewhere_in_repo(finding: Finding, label: str) -> bool:
        """Is ``label`` the failed command's own label plus a directory (``pytest (src/)``)?"""
        was = finding.targets[0].key if finding.targets else ""
        return bool(was and label != was and label.startswith(was + " (") and label.endswith(")"))

    def _promote(self, finding: Finding) -> bool:
        """An "own module not found" failure turned out to be about how the command is run."""
        if finding.detail.pop("own_module", None) is None:
            return False
        finding.detail["variant"] = "invocation"
        finding.weight = 1.0
        # ... and so it cost a round trip, like any other command that could not run
        trip = self.state.trips.pop(id(finding), None)
        finding.interval = finding.interval or trip or finding.run
        finding.summary = finding.summary.replace("ordinary failure", "setup/invocation error")
        finding.targets = [t for t in finding.targets if t.kind == model.COMMAND]
        return True

    def _red_run(
        self, key: str, label: str, action: _Action, result: Event, hidden: bool = False
    ) -> None:
        """Count a red run that followed a change: one more round that did not converge."""
        state = self.state
        cycle = state.cycles.get(key)
        fresh = cycle is None
        if cycle is None:
            cycle = state.cycles[key] = _Cycle(seen=state.turn_changes)
        if fresh and state.changes <= cycle.seen:
            cycle.baseline = True  # red before anything was changed in the turn
        if state.changes > cycle.seen:
            # The run that showed the problem counts with the rounds that failed to fix it:
            # red, edit, red, edit, red, edit, red is four runs that never went green.
            cycle.count += 2 if cycle.baseline and not cycle.count else 1
            for number, path, span in reversed(state.change_log):
                if number <= cycle.seen:
                    break
                if path:
                    # What was edited before the command first went red is not part of
                    # the stall: only the edits that tried to get it green again are.
                    edited = cycle.before if fresh else cycle.files
                    edited[path] = edited.get(path, 0) + 1
                if span is not None and not fresh:
                    cycle.spans.append(span)  # an edit between two red runs
        cycle.seen = state.changes
        run = _capped(self._started(action), _epoch(action.result))
        if run is not None:
            cycle.spans.append(run)
        cycle.named.update(path for path in (*cycle.files, *cycle.before) if path in result.output)
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
        certain = confidence >= CORRECTION_STRONG
        what = "The user corrected the agent" if certain else "Wording that could be a correction"
        return f"{what} (confidence {confidence:g}; matched {matched}; {note}); {how}"

    def _note_rework(self, paths: set[str] | frozenset[str], action: _Action) -> None:
        """The agent changed files again that the last correction was about."""
        rework = self.state.rework
        if rework is not None and rework.files & paths:
            rework.end = _epoch(action.result) or _epoch(action.call) or rework.end

    def _confirm_correction(self, paths: set[str] | frozenset[str], note: str) -> None:
        """The agent acted on files a strongly worded correction was about."""
        softened = self.state.softened
        if softened is not None and softened[1] & paths:
            # The human blamed themselves, and the agent still had to redo its change.
            self.state.softened = None
            prompt, files, matched = softened
            back = sorted(files & paths)[:CORRECTION_MAX_FILES]
            finding = self._emit(
                PROBABLE_CORRECTION,
                prompt,
                self._correction_summary(
                    CORRECTION_PROBABLE,
                    matched,
                    f"the user named their own action as the cause, but {note}",
                    "attributed to " + ", ".join(back),
                ),
                [Target(model.FILE, rel, 1.0 / len(back)) for rel in back],
                detail={"confidence": f"{CORRECTION_PROBABLE:g}", "corroborated": "softened"},
            )
            self.findings.sort(key=lambda f: f.line)
            del finding
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
        self.state.softened = None
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
        if pending.tentative and not pending.note:
            if not pending.fallback:
                self.findings = [f for f in self.findings if f is not finding]
                return
            finding.signal = PROBABLE_CORRECTION
            finding.excerpt = ""
            finding.detail.pop("quote", None)
            finding.detail["confidence"] = f"{CORRECTION_PROBABLE:g}"
            finding.summary = self._correction_summary(
                CORRECTION_PROBABLE,
                pending.fallback,
                "weaker cues only, no clear correction phrase",
                pending.how,
            )
            return
        if pending.note:
            how = pending.how
            if pending.followed and not pending.settled:
                # The files the agent went back to are the ones the correction was about.
                back = pending.followed[:CORRECTION_MAX_FILES]
                finding.targets = [Target(model.FILE, rel, 1.0 / len(back)) for rel in back]
                how = "attributed to " + ", ".join(back)
            finding.summary = self._correction_summary(
                CORRECTION_STRONG, pending.matched, pending.note, how
            )
            finding.detail["corroborated"] = "redirected" if pending.redirected else "followed"
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

    def _written(self, step: Step | None, cwd: str | None) -> str | None:
        """The repo file a refused shell step would have written, by redirection.

        ``cat > notes.md <<EOF``, ``cat >> notes.md`` and ``tee notes.md`` are writes
        to that file under another tool's name: what was refused is the write, not
        ``cat``. ``""`` if the step is no such write, ``None`` if its file lies
        outside the repository.
        """
        raw = written_file(step) if step is not None else ""
        if not raw or any(ch in raw for ch in "$`*?"):
            return ""
        rel = self.ctx.resolver.resolve(raw, self._where(step, cwd))
        return rel or None

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
                self.outside.append(result.line)
                return
            if step is not None and step.key:
                targets = [Target(model.COMMAND, self._label(step.key, step, call.cwd))]
            written = self._written(step, call.cwd)
            if written is None:
                self.outside.append(result.line)
                return
            targets = [Target(model.FILE, written)] if written else targets
        tool = self._tool(call)
        what = targets[0].key if targets else ""
        what = f"`{what}`" if targets and targets[0].kind == model.COMMAND else what
        declined = f"{tool} on {what}" if what else tool
        excerpt_parts = []
        if call.op == model.OP_SHELL and call.command:
            excerpt_parts.append(f"$ {call.command}")
        if result.reason:
            excerpt_parts.append(f"user said: {result.reason}")
        self._emit(
            TOOL_REJECTED,
            result,
            f"The user declined {declined}" + (" and said why" if result.reason else ""),
            targets,
            excerpt="\n".join(excerpt_parts),
            # From the call to its result a human was reading a prompt, which is not the
            # agent's time. Only what the agent then needs to get going again is claimed.
            interval=_capped(_epoch(result), _resumed(action)),
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
            step = denied_step(action.cmd, said)
            if self._elsewhere(step, call.cwd):
                self.outside.append(result.line)
                return
            if step is not None and step.key:
                targets = [Target(model.COMMAND, self._label(step.key, step, call.cwd))]
            written = self._written(step, call.cwd)
            if written is None:
                self.outside.append(result.line)
                return  # `cat > /tmp/notes <<EOF`: a write to a file outside the repo
            targets = [Target(model.FILE, written)] if written else targets
        by = {
            "hook": "a hook",
            "blocked": "the harness",
            "classifier": "the auto-mode classifier",
            "safety": "a built-in safety check",
        }.get(result.reason, "permission settings")
        tool = self._tool(call)
        what = targets[0].key if targets else ""
        what = f"`{what}`" if targets and targets[0].kind == model.COMMAND else what
        denied = f"{tool} on {what}" if what else tool
        excerpt = [f"$ {call.command}"] if call.op == model.OP_SHELL and call.command else []
        self._emit(
            PERMISSION_DENIED,
            result,
            f"{denied} was denied by {by}; it did not run",
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
        stopped = ""
        targets = self._action_targets(last)
        if not targets:
            if last.call.op in (model.OP_READ, model.OP_EDIT, model.OP_WRITE, model.OP_SHELL):
                return  # the agent was working outside the repo
            what = f"running {self._tool(last.call)}"
        elif targets[0].kind == model.COMMAND:
            # Stopping a running command is about the work in progress, not about the
            # command: the event goes to what the agent had been changing, if anything.
            what = f"running `{targets[0].key}`"
            stopped = targets[0].key
            edited: list[str] = []
            for action in reversed(turn):
                changed = action.call.op in (model.OP_EDIT, model.OP_WRITE)
                if changed and action.file and action.file not in edited:
                    edited.append(action.file)
            edited = edited[:CORRECTION_MAX_FILES]
            targets = [Target(model.FILE, rel, 1.0 / len(edited)) for rel in edited]
            if edited:
                what += " after edits to " + ", ".join(edited)
        elif last.result is not None and last.result.error == model.ERR_MISSING:
            # The last thing was a look for a file that is not there. The human stopped
            # the agent, not the file: the miss is on the record, and an interrupt on the
            # same path would be read as a second, independent kind of evidence.
            what, targets = "looking for a file that does not exist", []
        else:
            what = f"working on {targets[0].key}"
        # Only the call that was cut short is claimed: the turn up to it may have been fine.
        self._emit(
            USER_INTERRUPT,
            event,
            f"The user interrupted the agent while it was {what}",
            targets,
            interval=_capped(_epoch(turn[-1].call), _epoch(event)),
            detail={"interrupted": stopped} if stopped else None,
        )
        self.state.interrupted = self.findings[-1]

    def _on_prompt(self, event: Event) -> None:
        # Taking a change back because the human said to is following an instruction.
        self.state.undo_asked = (
            _UNDO_ASKED_RE.search(event.text[:CORRECTION_HEAD_CHARS]) is not None
        )
        self.state.corrected = frozenset()
        _, finished = self._turn_bounds()
        if self.state.agent_seen is not None:
            finished = max(finished or 0.0, self.state.agent_seen)
        asked = _epoch(event)
        idle = asked - finished if asked is not None and finished is not None else None
        confidence, labels, softened, instructed = _judge(event.text, event.after_question, idle)
        stopped, self.state.interrupted = self.state.interrupted, None
        head = event.text[:CORRECTION_HEAD_CHARS]
        if (
            stopped is not None
            and confidence <= 0
            and not instructed
            and not softened
            and _ADDITIVE_RE.match(head)
            and not _OBJECTION_RE.search(head)
        ):
            # "also bump the version": the human cut in to add to the job, not because
            # the agent was going wrong. Nothing to hold against what it was doing.
            self._drop({id(stopped)})
        before = frozenset(
            a.file
            for a in self.state.turn
            if a.file and a.call.op in (model.OP_EDIT, model.OP_WRITE)
        )
        # An instruction that redirects the work just done ("don't use X here") is a
        # correction if the agent goes back to that work, and nothing if it does not.
        tentative = bool(instructed) and bool(before)
        fallback = ""
        if tentative:
            fallback = ", ".join(f'"{label}"' for label in labels)
            confidence, labels = CORRECTION_STRONG, instructed
        if confidence <= 0:
            if softened and before:
                matched = ", ".join(f'"{label}"' for label in softened)
                self.state.softened = (event, before, matched)
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
        # A session that works on several repositories at once: a correction about work
        # done next door is not this repository's. That is the case when every file the
        # agent had changed lies outside, or when the human names a path outside and
        # the agent had changed nothing here (or had been busy out there too).
        resolver = self.ctx.resolver
        away = [
            a
            for a in self.state.turn
            if a.file is None and a.call.path and resolver.outside(a.call.path, a.call.cwd)
        ]
        changed_away = any(a.call.op in (model.OP_EDIT, model.OP_WRITE) for a in away)
        names_away = not named and any(
            ("/" in c or "\\" in c) and resolver.outside(c, event.cwd)
            for c in path_candidates(event.text[:CORRECTION_SCAN_CHARS].split())
        )
        if not named and ((changed_away and not edited) or (names_away and (away or not edited))):
            return
        targets: list[Target]
        edited_all = list(edited)
        # A file of the repository the human names that the agent had not gone near.
        # "You broke src/app/billing.py" is about that file, whatever the agent had been
        # editing; "look at how util.py does it" is still about the work just done.
        text = event.text[:CORRECTION_SCAN_CHARS]
        others: list[str] = []
        for candidate in path_candidates(text.split()) if not named else ():
            rel = resolver.resolve(candidate, event.cwd)
            if not rel or rel in touched or rel in others:
                continue
            if self.ctx.exists(rel) or self.ctx.is_known_file(rel):
                others.append(rel)
        wrong_place = _WRONG_PLACE_RE.search(event.text[:CORRECTION_HEAD_CHARS]) is not None
        blamed = [
            rel
            for rel in others
            if not wrong_place
            and re.search(
                _BLAME + r"[^.!?\n]{0,40}?" + re.escape(rel.rsplit("/", 1)[-1]) + r"(?![\w-])",
                text,
                re.I,
            )
        ]
        if blamed:
            targets = [Target(model.FILE, rel) for rel in blamed[:CORRECTION_MAX_FILES]]
            how = "names " + ", ".join(t.key for t in targets) + ", which it had not touched"
        elif named:
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
        elif named_changed and not stale and not tentative:
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
        self.state.corrected = frozenset(t.key for t in targets)
        if others and not blamed and targets:
            finding.detail["also_named"] = others[0]  # recommendations say so
        if corroborated:
            finding.detail["corroborated"] = corroborated
            self.state.rework = _Rework(finding, frozenset(named_changed))
        elif strong and changed:
            about = frozenset(named_changed) or changed | frozenset(blamed)
            elsewhere: set[str] = set()
            if _WRONG_PLACE_RE.search(event.text[:CORRECTION_HEAD_CHARS]):
                for candidate in path_candidates(event.text[:CORRECTION_SCAN_CHARS].split()):
                    rel = self.ctx.resolver.resolve(candidate, event.cwd)
                    if "/" not in candidate and "\\" not in candidate:
                        elsewhere.add(candidate)
                    elif rel:
                        elsewhere.add(rel)
            self.state.unconfirmed = _Unconfirmed(
                finding,
                about,
                matched,
                how,
                tentative=tentative,
                fallback=fallback,
                elsewhere=frozenset(elsewhere - set(touched)),
                settled=bool(blamed),
            )
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
    #: ``exposed`` with the episode of work each target was touched in.
    exposed_in: set[tuple[str, str, str]] = field(default_factory=set)
    #: Lines of calls that were refused or failed while acting outside the repository.
    outside: list[int] = field(default_factory=list)


def detect_stream(stream: Stream, ctx: DetectContext) -> Detection:
    """Run every transcript detector over one stream."""
    detector = _Detector(stream, ctx)
    findings = detector.run()
    return Detection(
        findings,
        detector.exposed,
        detector.skipped_events,
        detector.dropped,
        detector.exposed_in,
        sorted(set(detector.outside)),
    )


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
    parsed = parse_command(event.command, is_windows_path(event.cwd) or None)
    primary = next((s for s in parsed.steps if s.key == parsed.key), None)
    if primary is None or not primary.moves:
        return resolver.contains(event.cwd)
    return resolver.contains(resolver.chdir(event.cwd, primary.moves) or event.cwd)


def detect(stream: Stream, ctx: DetectContext) -> list[Finding]:
    """Run every transcript detector over one stream."""
    return detect_with_exposure(stream, ctx)[0]
