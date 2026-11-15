# Signal catalog

A *signal* is one kind of observable friction. Every signal is detected by a
deterministic rule over the transcript (or git history): no model, no
sampling. Each detected occurrence is a *friction event* with a pointer to the
transcript line that proves it.

Severity is the event's weight in the score (see [methodology](methodology.md)).
All thresholds are named constants in `src/agent_friction/signals.py`.

| Signal | Severity | Source |
| --- | --- | --- |
| `user-correction` | 4.0 | transcript |
| `retry-loop` | 3.0 | transcript |
| `no-convergence` | 3.0 | transcript |
| `tool-rejected` | 2.5 | transcript |
| `user-interrupt` | 2.5 | transcript |
| `edit-miss` | 2.0 | transcript |
| `edit-revert` | 2.0 | transcript |
| `command-failure` | 2.0 | transcript |
| `human-rework` | 1.5 | git |
| `edit-thrash` | 1.5 | transcript |
| `permission-denied` | 1.5 | transcript |
| `stale-edit` | 1.0 | transcript |
| `probable-correction` | 1.5 | transcript |
| `file-too-large` | 1.0 | transcript |
| `path-miss` | 1.0 | transcript |
| `search-miss` | 1.0 | transcript |
| `read-loop` | 0.5 | transcript |

Severities are ordered by one principle: a human having to step in costs more
than the agent wasting its own round trips, and a failure that provably
repeats costs more than one that might be routine.

**Weak signals.** `edit-thrash`, `read-loop`, `search-miss`,
`probable-correction` and *ordinary* command failures (a command that ran and
exited non-zero, as opposed to a setup/invocation error) also show up in
perfectly productive sessions. A file, command or directory whose *only*
evidence is of these kinds has its score capped (see methodology), gets no
recommendation and never "high" confidence. Two more kinds are weak in this
sense: a `path-miss` on a routinely probed optional file (`README.md`, `.env`,
`AGENTS.md`, ...), and a `no-convergence` event seen in only one session.
The one exception: a search for the *same* pattern that misses in two or more
sessions is treated as real evidence.

Paths are attributed only if they resolve inside the repository; everything
else is dropped. "Turn" means the agent's work between two human prompts.

---

## `user-correction` — User correction

**What:** a human prompt that (a) is not the first prompt of the session,
(b) follows at least one tool call by the agent, and (c) refers to the agent
or to the work it just finished in one of the ways below. Only the
first 280 characters are inspected, so a pasted log that happens to contain
"revert" does not count. The rules favour precision over recall: a new task that
*describes a bug* ("fix the bug where the app crashes again") must not be
read as a complaint about the agent.

**Wording is never enough for a certain correction.** A phrase from the lists
below makes a message a *candidate*. It becomes a correction with confidence
1.0 only if behaviour corroborates it:

1. the agent had changed files (Edit/Write) in the turn the human reacts to,
   **and**
2. either the human names one of those files, **or** in the turn that
   follows the agent changes one of those same files again (an edit, a
   write, or a shell command that rewrites, removes or restores it: `sed -i`,
   `rm`, `git checkout -- <file>`, `git restore`, `git revert`, `git reset`,
   `git stash`).

A candidate without corroboration (nothing was changed before it, the agent
went on to other things, or the session ends there) is not a
`user-correction` at all. It is recorded under a separate signal,
[`probable-correction`](#probable-correction--probable-correction), a
"probable" correction (confidence 0.4): listed as evidence, never quoted,
never the basis of a recommendation, and counted in
`totals.probable_corrections`, not in `totals.corrections`. The decision is made when the next human
message arrives or the session ends; the evidence summary says which of the
two it was ("the user names ...", "the agent then changed ... again", "not
corroborated: ...").

- **Addressed to the agent (strong wording, anywhere in those 280
  characters):**
  - "you broke / removed / deleted / forgot / missed / ignored / overwrote",
    "you've broken / removed / forgotten ..." (not "you forgot nothing"),
    "you didn't run | read | follow | use | fix | check ..." (not "you didn't
    do anything wrong"), "you shouldn't have", "you weren't supposed", "you
    keep <doing>";
  - "you're editing the wrong file | branch | directory | function ...", "you
    changed the wrong thing | part | section | field ...";
  - "I told you", "I already said", "I asked you to", "I said use / don't /
    not / only / to use", "I never asked for / you to", "I didn't ask for /
    you to" (not "I didn't ask before, but ..."), "I didn't want that". Not
    "I told you about X", and not when the sentence ends by asking for
    confirmation ("I told you to use pnpm, right?");
  - "not | isn't | wasn't what I asked | wanted | meant | said";
  - "why did you delete | remove | change | add | use | skip | ignore ...";
  - an undo of the agent's own change: "revert" / "undo" / "roll back" at the
    start of the message or of a clause, followed by "that | this | it | your
    ... | the last edit | the change", or alone ("Revert."); "can / could you
    undo that"; and "put it | that | them back". Not "revert the design doc to draft", "Undo support is
    the next feature", "roll back the last migration when the check fails".
- **Verdicts on the work just done (strong wording, only when they open the
  message,** optionally after an interjection such as "hmm", "ugh", "ok",
  "no"**):**
  - a leading "No," / "No." / "Nope -" followed by more text, or a bare "no"
    (not "No. 3 on the list", "No tests are needed", "no thanks", "no
    problem", "no need", "no, that's fine", "nope, all good", "nope, nothing
    else", "no, not yet", and not a choice between options: "No, go with
    option B", "No, the second one");
  - "no no", "no, no, not that file";
  - "same error | problem | failure | result", "I'm getting the exact same
    error" (not "same error handling as in util.py, please");
  - "go back", "go back to the previous version | how it was | what you had"
    (not "go back to the dashboard page and add a button");
  - "Not quite", "not exactly", "not like that" (not "Not quite sure how auth
    works"; "almost, but ..." and "close, but ..." are not cues: they usually
    introduce one more request);
  - "wrong." / "incorrect." followed by more text; "stop" followed by an
    instruction or by what to stop ("stop. use the other client", "stop
    editing the generated file"; not "Stop. Hammer time", "stop the server",
    "stop. I need to go to lunch");
  - "that's / this is / it was wrong | incorrect | broken" as a complete
    statement, or "... not right | not correct | not working | not how";
  - "that / this / it didn't | doesn't work | help", or a bare "doesn't
    work" (not followed by where: "It doesn't work in Safari, can you add a
    polyfill?" describes a missing feature);
  - "that / this / it / your change broke ...";
  - "still failing | broken | not working | not fixed | wrong | crashing",
    "still the same error", "still getting | seeing the error", "failing |
    broken again", with no subject or with
    "it", "that", "the tests", "the build", "CI" and the like as the subject
    (`_RESULT_NOUN`).

  The same words later in a message are not counted, because there they
  usually describe a task: "The customer says it's not working. Investigate
  ...", "Can you explain why this is not working in prod?", "Fix the bug where
  the app crashes again". A longer subject ("the login page is still broken")
  is not counted either.
- **What voids a phrase:**
  - *Praise or a dismissal after it* in the same message ("thanks", "great",
    "nailed it", "keep going", "looks good", "that's fine", "... are fine",
    "never mind", "ignore it", "unrelated", "flaky", ...), the human taking
    the blame or naming their own action as the cause ("... because I hadn't
    pulled", "tests failing again after I merged main", "my bad") or only
    asking ("just curious", "just checking", "not complaining"), an
    acknowledgement ("you removed the flag as asked, now ..."), or saying
    that the problem predates the agent ("before your change", "already
    broken", "pre-existing", "not caused by you", "not your fault": "Still
    failing in CI on main before your change too - can you look at
    app/main.py?" is a new request): "you forgot
    nothing, great job", "I said use tabs in my last project but here spaces
    are fine". Praise *before* the phrase does not void it ("Great effort, but
    you broke the login page").
  - *Moving on* voids a verdict: "next task", "new task", "another issue",
    "let's move on", "next: ..." ("It doesn't work on Windows yet; next task: add Windows
    support").
  - *An opener that answers a question:* if the final paragraph of the last
    thing the agent wrote contains a question mark ("Want me to refactor
    util.py? It would take a few minutes."), or the agent's last action was a
    question/choice tool (`AskUserQuestion`, `ExitPlanMode`, `ask_user`, ...),
    then a leading "No, ...", "Not quite", "Wrong." or "Stop." is read as an
    answer, not an objection. Phrases addressed to the agent still count.
  - *A long pause:* a verdict typed more than 5 minutes after the agent's last
    activity, with nothing addressed to the agent, is only a *probable*
    correction (confidence 0.4, never quoted): by then the human has often
    moved on to something else.
- **Weaker cues:** a leading "actually"; "don't <verb>" (not "don't forget /
  worry / bother / need", not "don't ... yet"); "stop <doing>"; "instead";
  "wrong / incorrect / mistake"; "shouldn't / you should have / supposed to"
  (not "we should have a retry here", which is a wish);
  "you ... again"; "deprecated / not allowed". **Two or more distinct cues**
  make a *probable* correction (confidence 0.4). **One cue alone is not an
  event**: "don't forget the tests", "actually, can you also ...", "use X
  instead" are normal conversation. A message that contains thanks, praise or
  a dismissal is never a probable correction.

A `user-correction` always has confidence 1.0; a probable correction weighs 1.5,
less than an edit miss. Recommendations are based on, and quote, corroborated
corrections only: one of them yields "check whether this is a standing rule",
two or more "write down the rule the human keeps repeating". Hook output that
the harness puts into a user turn ("Stop hook feedback: ...", "PostToolUse:Edit
hook ...") is not read as a human message.
Slash commands, harness-generated messages, tool results and prompts inside
subagents are never corrections. Blocks that the harness or an IDE extension
adds to a user turn (`<ide_opened_file>`, `<ide_selection>`,
`<ide_diagnostics>`, `<system-reminder>`, `<local-command-*>`,
`<user-prompt-submit-hook>`, ...) are removed before the prompt is read,
quoted or used for attribution: a file the IDE reports as open is not a file
the human named. The phrase lists are English only; the test
suite pins them against a corpus of 50+ corrections and 80+ benign prompts
(thanks, praise, questions, next-step requests, new tasks that describe bugs,
feature requests that contain "undo" or "wrong"; `tests/test_signals.py`).

**Why it is friction:** human attention is the scarcest resource in the loop.

**Attribution:** files the agent touched in the preceding turn that the
message names (by path, or by bare file name if unambiguous). If it names
none and the agent then went back to some of the files it had changed, the
correction belongs to exactly those files. A probable correction is split
evenly over the files edited in the preceding turn (the 5 most recent). If
nothing was edited it stays unattributed (shown on no hotspot). A file the human mentions but the agent never
touched is not blamed.

**False positives (known):** corroboration is circumstantial. A message that
only *sounds* like a correction and is followed by more work on the same file
counts: "No, I'd also like a dark mode" after the agent asked nothing, then
another edit to the stylesheet it had just changed; "Still failing in CI"
about a failure the agent did not cause, which it then fixes in a file it had
touched. Naming a changed file corroborates even when the complaint is about
something else in the same sentence. A "No, ..." that answers a question the
agent asked several messages earlier, or one phrased without a question mark,
is read as a verdict. Second-person phrases about the past ("you forgot the
migration last week, and I fixed it") and rhetorical "why did you ..." are
strong wording.

**False negatives (many, by design):** a real correction after which the
agent fixes a *different* file, runs only commands, or the session ends (it
is kept as probable); a correction of something the agent did with a command
rather than an edit ("No, I told you not to push"); a real "Stop." or "No,
..." right after the agent asked something; corrections that do not open the
message ("I ran it. It's still failing."); a verdict with a long subject ("the
checkout page is still broken"); polite or indirect corrections ("hmm, could
we use X?"); single-cue corrections ("use pnpm instead", "use PaymentClient,
not legacy");
corrections softened with praise; corrections in other languages. The count of
corrections is a lower bound.

## `probable-correction` — Probable correction

**What:** a human prompt that is worded like a correction (see
[`user-correction`](#user-correction--user-correction)) but is not
corroborated by behaviour, or that only contains two or more weaker cues
("Actually, use dataclasses instead"). Often it is simply the next request.

**Why it is kept at all:** several of them on one file are a lead worth a
look. They are kept apart so that a guess can never inflate the number of
corrections, put words in a human's mouth, or trigger "write down the rule".

**Attribution:** split evenly over the files edited in the preceding turn (the
5 most recent). Weak signal: alone it is capped and recommends nothing.

**Never quoted.** The human's words are not kept for a guess: the event has a
summary (which cues matched) and a transcript line to open, but its `excerpt`
is always `null`, with or without `--no-excerpts`.

## `retry-loop` — Retry loop

**What:** the same command line fails 3 or more times in a row within a
session, with **identical arguments** (after collapsing whitespace and
dropping leading `VAR=value` assignments), no success of that command and no
file change in between. One event per loop; its summary carries the final
count.

Narrowing a failure down is progress, not a loop: `pytest`, then `pytest
tests/test_a.py -x -vv`, then `pytest tests/test_a.py::test_total --tb=long`
are three different invocations and are not counted.

Edit, run the tests, see red, edit again, run again is the normal development
loop, not a retry loop: every successful edit starts the count again. What is
counted is re-running a failing command with nothing changed on disk (other
working directory, the same thing again). Any successful edit counts as a
change, whichever file it touches; we do not try to judge whether the edit
was relevant. So does a shell command that rewrites files: `sed -i`, `perl
-pi`, a formatter (`ruff format`, `black`, `prettier --write`, `gofmt -w`,
`cargo fmt`), a `--fix` / `--write` flag, `patch`, `git apply | checkout |
restore | stash`, and `>` / `tee` / heredoc writes to a file that is not a
log or under `/tmp`.

A retry loop is *derived from* command failures. For confidence it is not a
second kind of evidence next to them (see methodology). Once a loop is recorded it stays, together with its
failures, even if the command goes green after a later edit: the three runs
with nothing changed already happened.

**Waiting is not a loop.** If the very same command line fails 2 or more
times and then *succeeds*, with no file change and no install/setup command
in between, the agent was waiting for something outside the session: `gh pr
checks` until CI is done, `curl -sf localhost:8000/health` until a server is
up. The loop and its failures are dropped. A loop that never passes, or that
passes only after an install or a change, stays.

**Why:** the failure output did not lead the agent to a fix.

**Attribution:** the normalized command.

**False negatives:** a flaky test that fails twice and then passes untouched
looks exactly like waiting and is dropped (a single red run followed by green
stays on the record as an ordinary failure).

## `no-convergence` — No convergence

**What:** within one turn the agent ran the same command 4 or more times,
each of those runs came after a file change since the previous one (an
edit-and-run round), every run failed as an *ordinary* failure, and the turn
(or the session) ended with the command still red. One event per command and
turn. A green run of the command, or of another command doing the same job
(`make test` after `pytest ...`), at any point in the turn clears the count:
red, fix, green is never recorded, however many rounds it took. Re-running
without changing anything is a [retry loop](#retry-loop--retry-loop) instead,
and rounds in different turns are not added up, because a human steered in
between. Inline code (`python -c`) and setup/invocation errors do not count
as rounds.

**Why:** eight rounds of edit, run, red are not eight red tests during
development. The agent could not work out what the check wants, and gave up
or handed back.

**Attribution:** the normalized command, in full, and the files edited between
the red runs (the 3 most edited, the event split evenly between them).

**Time:** from the first red run to the end of the last one, at most 15
minutes. Waiting for a human is never inside a turn, so it is not included.

**One session is not a pattern.** A hard task can take many rounds. A target
whose `no-convergence` events all come from one session is treated like weak
evidence (capped, no recommendation); from two sessions on it counts in full.
It is derived from `command-failure`, so next to it it is not a second kind
of evidence for confidence.

**False positives:** a genuinely hard change that was abandoned for unrelated
reasons; a long-running refactoring that keeps a suite red on purpose.

## `tool-rejected` — Tool call declined

**What:** the human declined a proposed tool call (Claude Code records this as
a tool result starting "The user doesn't want to proceed with this tool use").
The reason, if the human typed one, is kept as the excerpt.

**Why:** the agent tried to do something a human considered wrong or unsafe.

**Not counted:** declining a plan or a question the agent put to the human
(`ExitPlanMode`, `AskUserQuestion`, `ask_followup_question` and their like in
other harnesses; `QUESTION_TOOL_RE`). "Not yet, look at B first" is an answer,
not a veto of an action. These are in no total.

**Attribution:** the file the tool targeted, or the normalized shell command.
For a chain of commands it is the step the human most plausibly objected to,
in this order: a step whose program the stated reason names; the first
destructive step (`rm`, `mv`, `dd`, `git push`, `git reset`, ...); the first
step that is neither setup nor read-only inspection; otherwise the line's own
key. So declining `rm -rf migrations && alembic revision` is recorded on `rm`.
Declined tools with neither file nor command (e.g. MCP tools) are counted but
unattributed.

## `user-interrupt` — User interrupt

**What:** a `[Request interrupted by user]` marker while the agent had made at
least one tool call in the turn. The marker that accompanies a declined tool
call is not counted again.

**Why:** the human saw the agent going wrong and stopped it.

**Attribution:** the target of the agent's most recent tool call. If that call
was a shell command, the command is *not* blamed (stopping a running `pytest`
says nothing about `pytest`): the event goes to the files edited in the turn
(the 5 most recent, split evenly), or is unattributed if there are none.

**False positives:** people also interrupt to add information or because they
ran out of time.

## `human-rework` — Human rework (git)

**What:** a commit attributed to an agent changed a file, the *next* commit
touching that file was made by a human within the rework window (14
days by default), **and** that human commit either

- *rewrites the agent's lines:* at least one of its hunks changes, deletes or
  splits lines that the agent commit added or modified (line ranges from `git
  show --unified=0` of both commits; valid because the human commit is the
  next one to touch the file), or
- *says it is a repair:* its subject starts with or contains `fix`, `fixes`,
  `fixed`, `revert`, `undo`, `rollback`, `hotfix`, `regression`, `broke`,
  `broken`, `oops`, `mistake` (`FIX_SUBJECT_RE`).

A human who continues work elsewhere in the file, or appends after the agent's
lines, with an ordinary subject is **not** rework. One event per (human
commit, file). A human commit that follows another human commit is not rework.

Agent commits are recognised by `Co-Authored-By` trailers naming Claude,
Anthropic, Copilot, Codex, Cursor, Aider, Devin, OpenHands, Gemini, Jules or
Windsurf; "Generated with Claude Code"; an `aider: ` subject prefix; an
"(aider)" author; or a bot author with one of those names. Add patterns with
`--agent-commit REGEX` (or replace them with `agent_commit_patterns`).

**Automation is not a human.** Commits by other bots are ignored on the human
side, as if they were not in the history: an author name containing `[bot]`
or starting with `dependabot`, `renovate`, `pre-commit-ci`, `github-actions`
and similar, or a known bot e-mail address (`DEFAULT_BOT_PATTERNS`). A CI
auto-formatter that rewrites the agent's lines is not rework, and the human
commit after it is still compared with the agent commit. Add your own with
`--bot-author REGEX` (`bot_commit_patterns`; matched against `Name <email>`).

**Why:** agent output here needed a human follow-up soon after it landed.

**Attribution:** the file. If the agent commit was made during a transcript
session that is being analysed (between its first event and
10 minutes after its last), the event belongs to that session. Otherwise the agent commit
counts as a friction "session" of its own, reported separately as
`agent_commits` ("1 session and 2 agent commits"). Agent commits without
rework are not counted anywhere, and a hotspot's score with git evidence is
never lower than without it.

**Corroborating evidence only.** Git cannot show *why* a human changed the
lines, so this signal weighs less than an edit miss (1.5), and it never raises
confidence: agent commits are not counted as sessions, and `human-rework` is
not counted as a kind of evidence, when confidence is assigned. A file whose
only evidence is rework is always "low" confidence.

**False positives:** a human changing the agent's lines for a new requirement
rather than because they were wrong; a file the agent created, where any
later change except an append overlaps "its" lines; a subject such as "fix
typo in README" on an unrelated part of the file. **False negatives:** squash
merges hide agent trailers; rework in a later commit than the next one;
renamed files; only the newest 400 candidate commits have their patches read.

## `edit-miss` — Edit miss

**What:** an edit failed because the text to replace was not found in the
file, or matched several places.

**Why:** the agent's picture of the file was wrong (stale read, reformatting,
whitespace), or the file has repeated blocks that are hard to target.

**Attribution:** the file being edited.

## `edit-revert` — Edit reverted

**What:** a successful edit whose (old, new) strings are exactly the (new,
old) strings of an earlier successful edit to the same file in the same
session, or a write whose content equals an earlier version of the file after
a different version was written in between. Strings are compared by hash.

*Temporary debugging is not a revert:* if the earlier edit only **added**
text (its old string survives inside its new string, e.g. a `print` after an
existing line), a command ran successfully while it was in place, and the
edit is then exactly undone, nothing is recorded. The agent looked at
something and cleaned up after itself.

**Why:** work on this file went in a circle.

**Attribution:** the file.

**False positives (known):** an experiment that *replaces* code, is run, and
is deliberately taken back ("try the other algorithm, measure, restore") is
counted, although nothing went wrong; so is a debug statement that is added
and removed without any successful command in between (for example because
the only run with it failed), or a toggle the human asked for twice. One
revert on a file is weak evidence; the confidence rules treat it that way.

**False negatives:** reverts done by `git checkout`, by a larger rewrite, or
in a later session; an abandoned attempt that only added lines and was run
successfully once before being removed (read as debugging).

## `command-failure` — Command failure

**What:** a shell command exits non-zero (or the tool reports an error).

- *Setup/invocation errors* count fully (weight 1.0): exit codes 126/127, or
  an output **line** with the shape the shell, interpreter or tool itself
  prints when the command never got to do its job: `bash: x: command not
  found`, `bash: ./x.sh: No such file or directory | Permission denied`,
  `ModuleNotFoundError: No module named ...`, `ImportError while loading
  conftest`, `can't open file`, `Cannot find module`, `error: unrecognized
  arguments`, `make: *** No rule to make target`, `npm error Missing script`,
  `fatal: not a git repository`, `curl: (N) ...`, and install/build failures
  (`npm error code E401 | ERESOLVE | ...`, `Failed to build ...`, `error:
  subprocess-exited-with-error`, `ERROR: Failed building wheel`, `No solution
  found when resolving dependencies`). The full list is
  `INVOCATION_ERROR_RE`. The harness's own `Command timed out after ...`
  notice counts too, but only where the harness puts it: at the very start of
  the result. Words such as "timed out" or "permission denied" elsewhere in
  the output (a failing test about timeouts, a test runner printing that
  sentence) do not qualify.
- *Errors quoted from inside a test are the test's business:* pytest's `E
  ...` lines, its `FAILED path::test - reason` summary lines and traceback
  frames are ignored when looking for those shapes. A test that fails with
  `E   ModuleNotFoundError: No module named 'plugin_x'` is a red test.
- *A module that cannot be imported* is a setup error only if it is not the
  repository's own. If the top-level package exists in the repo (`src/`,
  `app/`, `shop.py`, ...), `No module named 'src.utlis'` is a typo to fix: an
  ordinary failure, and gone entirely once an edit turns the run green. A
  missing third-party module (`No module named 'yaml'`) at collection time is
  a setup error.
- *A foreground server is not a failed command:* when the harness timeout hits
  a command that never exits by design (`npm run dev`, `vite`, `uvicorn`,
  `flask run`, `rails s`, `manage.py runserver`, `docker compose up` without
  `-d`, ...; `DEV_SERVER_RE`), the event says so, and the recommendation is to
  run it in the background, not to document how to invoke it.
- *Calls that never ran are not failures:* a result that is the harness's own
  error message (`<tool_use_error>` without an exit code: "Cancelled: parallel
  tool call ... errored", "InputValidationError: ...") is dropped. "Blocked:
  ..." is a [`permission-denied`](#permission-denied--permission-denied).
- *"command not found" names its program:* if the shell says which program
  it could not find and that program is a step or pipeline stage of the line
  (`cat x | jqq . | sort`), the event is keyed to that stage.
- *An exit status that is not the command's own is no verdict.* In `pytest
  2>&1 | tail -5` the shell reports the status of `tail`; in `make test; echo
  done` that of `echo`; `pytest || true` always succeeds. When the command a
  line is named after is piped into something, or followed by `;` or `||`
  (and `set -o pipefail` is not in the line), an exit code of 0 is treated as
  **unknown** and the output decides (`output_verdict`):
  - **red** if it shows a setup/invocation error (the shapes above) or a
    failed check: a pytest/unittest summary with failures or errors, `FAILED
    path::test`, jest/vitest `Tests: N failed` / `Test Files N failed` / `FAIL
    file`, mocha `N failing`, rspec `N examples, M failures`, `--- FAIL` /
    `FAIL pkg` (go), `test result: FAILED` / `error[E...]:` / `could not
    compile` (cargo), `make: *** `, `Found N errors` (N > 0), `error TSnnnn`,
    `N problems (M errors`, `npm ERR!`, or a Python traceback whose last line
    is the exception (`RED_OUTPUT_RE`). The event is recorded like any other
    command failure, with "judged from its output" in the summary;
  - **green** if it shows a passing summary (`N passed`, `Tests: N passed`,
    `ok pkg 0.1s`, `test result: ok`, `All checks passed`, `Success: no
    issues`, `Found 0 errors`, `N passing`, `N examples, 0 failures`;
    `GREEN_OUTPUT_RE`);
  - **unknown** otherwise. An unknown run is not a failure, and it is never
    used as proof of success either: it does not close a red, fix, green
    loop, is never the `recovered_by` of an earlier failure, does not end a
    wait or a retry loop, and does not clear a `no-convergence` stretch.

  `a && b` is not such a case: there the exit status is real.
- *Ordinary failures* (anything else: a red test, a linter or type checker
  reporting problems, a script that raises) count at weight 0.3 and claim no
  lost time. A red test during development is the tool working as intended.
  They are weak evidence: a command with nothing but ordinary failures is
  capped below the "pattern" band and gets no recommendation, however many
  sessions it appears in.
- *Inline code is not a command of the repository:* `python -c '...'`,
  `node -e '...'`, `ruby -e`, `perl -e`, `bun -e`, `php -r`, and scripts piped
  to an interpreter are keyed `python <inline>` / `python <stdin>`. What an
  ad-hoc snippet does is the snippet's business, so its failures are ordinary
  whatever they print; only a missing or non-executable interpreter (exit
  126/127) is a setup error. Inline code never gets "document the command".
- *Red, fix, green is not counted at all:* an ordinary failure that is
  followed by a file change (an edit, or a shell command that rewrites files,
  see `retry-loop`) and then by a success of the same command, **or of
  another command doing the same job** (`pytest tests/test_a.py` red, edit,
  `make test` green), is dropped. That is the development loop doing its job. An ordinary
  failure stays on the record when the command goes green with no edit in
  between (flaky, or fixed by something other than code), never goes green
  in the session, or was part of a [retry loop](#retry-loop--retry-loop).
- *Test-first development is red, fix, green too:* a setup/invocation error
  that names what is missing (`No module named 'app.newfeat'`, `can't open
  file 'scripts/run.py'`, `file or directory not found: tests/test_x.py`,
  `Cannot find module './util'`) is dropped when a file that provides exactly
  that module or path is then written in the repo **and** the command goes
  green. A missing third-party module (`No module named 'yaml'`) is never
  created in the repo and stays a setup error. Other setup/invocation errors
  are never dropped.
- *Waiting:* the same command line failing repeatedly and then passing with
  nothing changed is dropped (see [retry loop](#retry-loop--retry-loop)).
- *`git commit` routines:* "nothing to commit" is a no-op and not recorded. A
  commit stopped by a pre-commit hook that reformatted files ("files were
  modified by this hook") is dropped when the next `git commit` succeeds; a
  hook that rejects the commit for another reason is an ordinary failure.
- *Which step of a chain failed:* for `a && b` the exit status does not say
  whether `b` ever ran. The output is matched against what each kind of step
  prints (`STEP_SIGNATURES`: `npm error code E401`, `Failed to build`,
  `error: subprocess-exited-with-error`, `make: *** [`, `error[E0308]:`,
  `fatal:`, a pytest/jest summary line, ...), and the first step whose own
  kind of error is present gets the event: `uv pip install -e . && python -m
  pytest tests/` failing with "Failed to build" is an `uv pip install` setup
  error, not a pytest failure. If no step can be confirmed, the event stays on
  the last significant command, **no file is blamed**, and the summary says
  that the step is unknown (`ambiguous_step`).
- *Ignored:* read-only inspection commands and probes whose exit status is
  the answer (`ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `find`, `sed`
  without `-i`, `awk`, `jq`, `diff`, `test`, `[`, `which`, `command -v`,
  `type`, `hash`, `pgrep`, `lsof`, `git diff | status | log | show | blame |
  rev-parse | merge-base | check-ignore`, `gh pr view | status | checks`, `pip
  show | list`, `npm ls`, `docker ps`, ...; the lists are
  `INSPECTION_PROGRAMS` and `INSPECTION_KEYS`); pipelines ending in
  `grep`/`rg`/`diff`/`test` with exit code 1 ("nothing matched"); interrupted
  commands; and declined commands.
- *Commands run somewhere else are not this repository's:* a leading `cd
  <dir>` (also `pushd`, and several in a row) is followed lexically from the
  event's working directory (absolute paths, `~`, `..`), as are `git -C`,
  `make -C`, `npm --prefix`, `uv --directory` and their like. If the step
  that failed ran outside the repo, the event is dropped (`cd /tmp/scratch &&
  python repro.py`, `cd .. && make lint`). So is a file command (`rm`, `cp`,
  `mv`, ...) whose path arguments all lie outside the repo, and an interpreter
  run on a script outside it (`python3 /tmp/x/repro.py`). A `cd` that
  cannot be followed (`cd -`, `cd "$DIR"`) changes nothing. The same applies
  to declined and denied commands.
- *... and commands that go into the repository are:* the same rule in the
  other direction. A session started in a parent directory (`~`, a monorepo
  root) whose command is `cd repo && make build` or `cd /abs/repo && make
  build` runs `make build` in the repo, and counts exactly as if the session
  had been started there. Subagents typically prefix every command this way.
  A command in the parent directory without such a `cd` still does not count.

**Why:** agents guess how to build, test and run things; a wrong guess costs a
round trip in every session until the right command is written down.

**Attribution:** the normalized command (see methodology), followed by the
directory it ran in when that is a subdirectory of the repo and the tool is
one that works on the directory it is in: `npm test (web/)`. In a chain such as
`mkdir -p out && pytest tests/x.py | tail -5` that is the last significant
command (`pytest`), not the setup in front of it or the filter behind it;
heredoc bodies are skipped and `if`/`for`/`bash -c` are looked through.
Ordinary failures are also attributed to repo files named in the arguments
**of that command** (not files that `cp`, `cat` or `cd` earlier in the line
mention); setup errors are not, because the files are not the cause.

**What happened next** is recorded for recommendations, conservatively:
`recovered_by` only for a *setup/invocation* failure (a command that ran and
reported red was not "replaced" by the next green one), and only if a command
*doing the same job* then succeeded (same
role: test, build, lint, type-check or format; or a wrapper of the same tool,
such as `uv run pytest` or `make test` for `pytest`) and the failing command
did not succeed later in the session; `fixed_by_setup` if the same command
succeeded after an install/setup command (`pip install`, `npm ci`, `uv sync`,
`docker compose up`, ...). A `git commit` or `rm` that happens to succeed
next is neither.

**False positives / limits:** when the output of a chain matches no known
step signature the step is a guess (and marked as such). A program that prints one of the anchored shapes itself
(a test runner echoing a shell error on its own line) is misread as a setup
error.

## `edit-thrash` — Edit thrash

**What:** 6 or more successful edits to one file within a single turn **in
which something also went wrong**: an edit miss, a stale edit or a reverted
edit *on that file*, or a failed command or retry loop in the turn. A miss on
another file does not count. One event per file and turn. Eight
clean edits followed by a green test run are a productive sitting, not thrash,
and are not recorded. The event notes whether the turn's last command
succeeded (`ended_green`); if it did, the "edits did not converge"
recommendation does not count it.

**Why:** many small edits in one go, with failures in between, usually mean
trial and error.

**Attribution:** the file.

**False positives:** legitimately building a large file step by step while an
unrelated command fails in the same turn. This is a weak signal: on its own
its score is capped, it recommends nothing and never produces "high"
confidence.

## `file-too-large` — File too large to read

**What:** a read was refused because the file exceeds what the harness
returns in one piece ("File content (31245 tokens) exceeds maximum allowed
tokens").

**Why:** the agent has to page through the file or search it blind, and it
will hit the same wall in every session.

**Attribution:** the file. Two or more such reads recommend splitting the
file or documenting where its important parts are.

## `permission-denied` — Permission denied

**What:** a tool call that was refused before it ran, by something other than
a human saying no: a missing permission ("Claude requested permissions to use
Bash, but you haven't granted it yet"), a deny rule ("Permission to use Bash
has been denied"), a hook ("PreToolUse:Bash hook error: ..."), or a safety
check ("Permission for this command was denied by ..."), or the harness
refusing a command pattern outright ("Blocked: sleep 30 followed by: ...";
variant `blocked`, with advice to document the alternative it suggests rather
than to add an allow rule). The wording must
open the tool result, as the harness writes it; a program that *ran* and
printed "permission denied" is a `command-failure`.

**Why:** the command or edit never happened, so this says nothing about the
command itself. It says the setup and the agent disagree about what is
allowed: either a routine command is missing from the allow-list (the agent
stalls on it in every session), or the agent does not know a boundary.

**Attribution:** the file the tool targeted, or the normalized shell command
(for a chain, the step chosen as for `tool-rejected`: a step the refusal
names in words of its own, else the first destructive step, so `ls build; rm
-f build/out.bin` is keyed to `rm`. The harness usually quotes the whole
command line in its message; that quotation is ignored when looking for the
step it names).

**False negatives:** harnesses and hooks word their refusals freely; only the
shapes above are recognised, anything else that is flagged as an error is
treated as a failed call.

## `stale-edit` — Stale edit

**What:** an edit or write was refused because the file had not been read
first, or was modified on disk after the agent read it.

**Why:** "modified since read" means something else rewrites the file: a
formatter, a watcher, a code generator, another agent. "Not read first" is
mostly agent sloppiness and is recorded with a separate variant.

**Attribution:** the file.

## `path-miss` — Path miss

**What:** a read or edit of a path that does not exist. A read of a missing
file that the agent then creates with a write in the same session (checking
whether `CHANGELOG.md` exists before adding it) is not recorded.

**Why:** the agent expected something to live there. Repeated identical wrong
guesses mean the layout suggests a file that is not there.

**Routine probes are weak.** Agents look for a set of well-known optional
files as a matter of course: `CLAUDE.md`, `AGENTS.md`, `.cursorrules`,
`.env*`, `README*`, `CONTRIBUTING*`, `Makefile`, `pyproject.toml`,
`setup.cfg`, `package.json`, `tsconfig*.json`, `.editorconfig`, and the other
usual instruction and tool-configuration names (`PROBE_FILE_RE`). A miss on
one of them is marked as a probe, and a path with nothing but probes is capped
and gets no recommendation, in however many sessions it happens: the absence
of a `.env` is not a layout problem. It is ordinary evidence again when the
file name appeared earlier in the session in a human prompt or in a tool's
output (somebody said it is there).

**Attribution:** the non-existent path itself.

## `search-miss` — Search miss

**What:** a Grep or Glob search that returned nothing.

**Why:** the agent looked for a name the codebase does not use.

**Attribution:** the file or directory that was searched (for Glob, the
literal directory prefix of the pattern). Directory-scoped misses appear under
`directories` and in the tree, not under file hotspots. The pattern is kept
as the excerpt.

**Not counted:** a miss that the agent resolves itself: when a later search
*in the same turn* for the same pattern (ignoring case) finds something, in a
wider or different scope, the earlier misses for it are dropped. Also not
counted: a check that a name is free before creating it. A miss is
dropped when, later in the same session, the agent creates or edits a file
inside the searched scope whose name matches the glob (`src/new_feature*.py`
then writing `src/new_feature.py`), or writes text that contains the grep
pattern (`NewFeature` then writing `class NewFeature`). Nor is a search that
confirms a removal: if an earlier successful edit in the session replaced
text that matches the pattern with text that does not (renaming
`legacy_charge` to `charge`, then `grep legacy_charge`), finding nothing is
the expected result.

**False positives:** searching to *confirm absence* for other reasons ("no
remaining callers") is normal. This is therefore a weak signal: a place with
nothing but search misses has its score capped and gets no recommendation,
unless the same pattern missed in two or more sessions (agents keep looking
for a name that is not there).

## `read-loop` — Read loop

**What:** the same range of the same file is read 3 or more times in a session
with no successful edit to that file in between. Paging through a file with
different offsets does not count.

**Why:** the agent keeps returning to the file without progress (often after
losing context).

**Attribution:** the file. Weak signal, like `edit-thrash`: alone it is
capped and recommends nothing.
