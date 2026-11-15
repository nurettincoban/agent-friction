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
| `tool-rejected` | 2.5 | transcript |
| `user-interrupt` | 2.5 | transcript |
| `edit-miss` | 2.0 | transcript |
| `edit-revert` | 2.0 | transcript |
| `command-failure` | 2.0 | transcript |
| `human-rework` | 1.5 | git |
| `edit-thrash` | 1.5 | transcript |
| `permission-denied` | 1.5 | transcript |
| `stale-edit` | 1.0 | transcript |
| `path-miss` | 1.0 | transcript |
| `search-miss` | 1.0 | transcript |
| `read-loop` | 0.5 | transcript |

Severities are ordered by one principle: a human having to step in costs more
than the agent wasting its own round trips, and a failure that provably
repeats costs more than one that might be routine.

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
went on to other things, or the session ends there) is a *probable*
correction (confidence 0.4): it is listed as evidence, never quoted in a
recommendation, and not counted in `totals.corrections` (it is counted in
`totals.probable_corrections`). The decision is made when the next human
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
  - "you're editing the wrong file | branch | directory | function ...";
  - "I told you", "I already said", "I asked you to", "I said use / don't /
    not / only / to use", "I never asked for / you to", "I didn't ask for /
    you to" (not "I didn't ask before, but ..."), "I didn't want that";
  - "not what I asked | wanted | meant | said";
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
    asking ("just curious", "just checking"): "you forgot
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
  "wrong / incorrect / mistake"; "shouldn't / should have / supposed to";
  "you ... again"; "deprecated / not allowed". **Two or more distinct cues**
  make a *probable* correction (confidence 0.4). **One cue alone is not an
  event**: "don't forget the tests", "actually, can you also ...", "use X
  instead" are normal conversation. A message that contains thanks, praise or
  a dismissal is never a probable correction.

The confidence multiplies the severity (a probable correction is worth 1.6,
less than an edit miss). Recommendations quote only corroborated corrections.
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

## `tool-rejected` — Tool call declined

**What:** the human declined a proposed tool call (Claude Code records this as
a tool result starting "The user doesn't want to proceed with this tool use").
The reason, if the human typed one, is kept as the excerpt.

**Why:** the agent tried to do something a human considered wrong or unsafe.

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

**Attribution:** the target of the agent's most recent tool call.

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
"(aider)" author; or a bot author with one of those names. Override with
`agent_commit_patterns`.

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
- *Ordinary failures* (anything else: a red test, a linter or type checker
  reporting problems, `git diff --exit-code`) count at weight 0.3 and claim no
  lost time. A red test during development is the tool working as intended.
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
- *Ignored:* read-only inspection commands (`ls`, `cat`, `head`, `tail`, `wc`,
  `grep`, `rg`, `find`, `sed` without `-i`, `awk`, `jq`, `diff`, `test`,
  `which`, `git diff | status | log | show | blame`, ...); pipelines ending in
  `grep`/`rg`/`diff`/`test` with exit code 1 ("nothing matched"); interrupted
  commands; declined commands; and commands run with a working directory
  outside the repo.

**Why:** agents guess how to build, test and run things; a wrong guess costs a
round trip in every session until the right command is written down.

**Attribution:** the normalized command (see methodology). In a chain such as
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

**What:** 6 or more successful edits to one file within a single turn. One
event per file and turn.

**Why:** many small edits in one go usually mean trial and error.

**Attribution:** the file.

**False positives:** legitimately building a large file step by step. This is
a weak signal: on its own it never produces "high" confidence.

## `permission-denied` — Permission denied

**What:** a tool call that was refused before it ran, by something other than
a human saying no: a missing permission ("Claude requested permissions to use
Bash, but you haven't granted it yet"), a deny rule ("Permission to use Bash
has been denied"), a hook ("PreToolUse:Bash hook error: ..."), or a safety
check ("Permission for this command was denied by ..."). The wording must
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

**Attribution:** the non-existent path itself.

## `search-miss` — Search miss

**What:** a Grep or Glob search that returned nothing.

**Why:** the agent looked for a name the codebase does not use.

**Attribution:** the file or directory that was searched (for Glob, the
literal directory prefix of the pattern). Directory-scoped misses appear under
`directories` and in the tree, not under file hotspots. The pattern is kept
as the excerpt.

**Not counted:** a check that a name is free before creating it. A miss is
dropped when, later in the same session, the agent creates or edits a file
inside the searched scope whose name matches the glob (`src/new_feature*.py`
then writing `src/new_feature.py`), or writes text that contains the grep
pattern (`NewFeature` then writing `class NewFeature`).

**False positives:** searching to *confirm absence* for other reasons ("no
remaining callers") is normal. Recommendations therefore need at least two
misses.

## `read-loop` — Read loop

**What:** the same range of the same file is read 3 or more times in a session
with no successful edit to that file in between. Paging through a file with
different offsets does not count.

**Why:** the agent keeps returning to the file without progress (often after
losing context).

**Attribution:** the file. Weak signal, like `edit-thrash`.
