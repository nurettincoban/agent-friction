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
(b) follows at least one tool call by the agent, and (c) starts with a
correction phrase. Only the first 280 characters are inspected, so a pasted
log that happens to contain "revert" does not count. The rules favour
precision over recall: ordinary follow-ups must not be counted.

- **Unambiguous phrases (confidence 1.0):**
  - a leading "No," / "No." / "Nope -" followed by more text, or a bare "no"
    (not "No. 3 on the list", "No tests are needed", "no thanks", "no
    problem", "no worries", "no need", "no, that's fine", "nope, all good");
  - a leading "wrong." / "incorrect." / "stop." followed by more text;
  - "that's / this is / it was wrong | incorrect | broken" as a complete
    statement (not "this is wrong input"), or "... not right | not correct |
    not working | not how", "that's not what I / we ...";
  - "that / this / it didn't | doesn't work | help";
  - "you're editing the wrong file | branch | directory | function ...";
  - "you broke / removed / deleted / forgot / missed / ignored / overwrote"
    (not "you forgot nothing"), "you didn't run | read | follow | use | fix |
    check ...", "you shouldn't have", "you weren't supposed";
  - an imperative "revert" / "undo" / "roll back" at the start of the message
    or of a clause, followed by "that | this | it | your | the last ..." (not
    "Undo support is the next feature", "Revert button should be added");
  - "still failing | broken | not working | not fixed | wrong | crashing",
    "still the same error", "failing | broken again";
  - said to the agent: "I told you", "I already said", "I asked you to", "I
    said use / don't / not / only / to use", "I never asked", "I didn't ask"
    (not "I said to my boss ...");
  - "not what I asked | wanted | meant | said";
  - "why did you delete | remove | change | add | use | skip | ignore ...".
- **Two things void an unambiguous phrase:**
  - *Praise or a dismissal after it* in the same message ("thanks", "great",
    "nailed it", "keep going", "looks good", "that's fine", "never mind",
    "ignore it", "unrelated", "flaky", ...): "you forgot nothing, great job",
    "it's still failing in CI but that's flaky, ignore it", "not what I
    expected but it's great". Praise *before* the phrase does not void it
    ("Great effort, but you broke the login page").
  - *A leading "No" that answers a question:* if the last thing the agent
    wrote before the message ends with a question mark, "No, ..." is read as
    an answer, not an objection. The other phrases still count.
- **Weaker cues:** a leading "actually"; "don't <verb>" (not "don't forget /
  worry / bother / need", not "don't ... yet"); "stop <doing>"; "instead";
  "wrong / incorrect / mistake"; "shouldn't / should have / supposed to";
  "you ... again"; "deprecated / not allowed". **Two or more distinct cues**
  make a *probable* correction (confidence 0.4). **One cue alone is not an
  event**: "don't forget the tests", "actually, can you also ...", "use X
  instead" are normal conversation. A message that contains thanks, praise or
  a dismissal is never a probable correction.

The confidence multiplies the severity (a probable correction is worth 1.6,
less than an edit miss). Recommendations quote only unambiguous corrections.
Slash commands, harness-generated messages, tool results and prompts inside
subagents are never corrections. The phrase lists are English only; the test
suite pins them against a corpus of 40+ corrections and 60+ benign follow-ups
(thanks, praise, questions, next-step requests, feature requests that happen
to contain "undo" or "wrong"; `tests/test_signals.py`).

**Why it is friction:** human attention is the scarcest resource in the loop.

**Attribution:** files the agent touched in the preceding turn that the
message names (by path, or by bare file name if unambiguous). If it names
none, the correction is split evenly over the files edited in that turn (the
5 most recent). If nothing was edited it stays unattributed (counted in
`totals`, shown on no hotspot). A file the human mentions but the agent never
touched is not blamed.

**False positives:** a "No, ..." that answers a question the agent asked
several messages earlier or without a question mark; rhetorical "why did you
...". **False negatives (many, by design):** polite or indirect corrections
("hmm, could we use X?"), single-cue corrections ("use pnpm instead", "use
PaymentClient, not legacy"), a real "No, ..." right after the agent asked
something, corrections softened with praise, and corrections in other
languages. The count of corrections is a lower bound.

## `retry-loop` — Retry loop

**What:** the same normalized command fails 3 or more times in a row within a
session, with **no success of that command and no successful file edit** in
between. One event per loop; its summary carries the final count.

Edit, run the tests, see red, edit again, run again is the normal development
loop, not a retry loop: every successful edit starts the count again. What is
counted is re-running a failing command with nothing changed on disk (other
flags, another working directory, the same thing again).

**Why:** the failure output did not lead the agent to a fix.

**Attribution:** the normalized command.

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

**Attribution:** the file. Each agent commit counts as its own "session" for
the score.

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

**Why:** work on this file went in a circle.

**Attribution:** the file.

**False negatives:** reverts done by `git checkout`, by a larger rewrite, or
in a later session.

## `command-failure` — Command failure

**What:** a shell command exits non-zero (or the tool reports an error).

- *Setup/invocation errors* count fully (weight 1.0): exit codes 126/127, or
  an output **line** with the shape the shell, interpreter or tool itself
  prints when the command never got to do its job: `bash: x: command not
  found`, `bash: ./x.sh: No such file or directory | Permission denied`,
  `ModuleNotFoundError: No module named ...`, `ImportError while loading
  conftest`, `can't open file`, `Cannot find module`, `error: unrecognized
  arguments`, `make: *** No rule to make target`, `npm error Missing script`,
  `fatal: not a git repository`, `curl: (N) ...`. The full list is
  `INVOCATION_ERROR_RE`. The harness's own `Command timed out after ...`
  notice counts too, but only where the harness puts it: at the very start of
  the result. Words such as "timed out" or "permission denied" elsewhere in
  the output (a failing test about timeouts, a test runner printing that
  sentence) do not qualify.
- *Ordinary failures* (anything else: a red test, a linter or type checker
  reporting problems, `git diff --exit-code`) count at weight 0.3 and claim no
  lost time. A red test during development is the tool working as intended.
- *Red, fix, green is not counted at all:* an ordinary failure that is
  followed by a successful file edit and then by a success of the same
  command is dropped. That is the development loop doing its job. An ordinary
  failure stays on the record when the command goes green with no edit in
  between (flaky, or fixed by something other than code), or never goes green
  in the session. Setup/invocation errors are never dropped.
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

**False positives / limits:** in `a && b` we cannot see which step failed and
name the last one. A program that prints one of the anchored shapes itself
(a test runner echoing a shell error on its own line) is misread as a setup
error.

## `edit-thrash` — Edit thrash

**What:** 6 or more successful edits to one file within a single turn. One
event per file and turn.

**Why:** many small edits in one go usually mean trial and error.

**Attribution:** the file.

**False positives:** legitimately building a large file step by step. This is
a weak signal: on its own it never produces "high" confidence.

## `stale-edit` — Stale edit

**What:** an edit or write was refused because the file had not been read
first, or was modified on disk after the agent read it.

**Why:** "modified since read" means something else rewrites the file: a
formatter, a watcher, a code generator, another agent. "Not read first" is
mostly agent sloppiness and is recorded with a separate variant.

**Attribution:** the file.

## `path-miss` — Path miss

**What:** a read or edit of a path that does not exist.

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

**False positives:** searching to *confirm absence* ("no remaining callers")
is normal. Recommendations therefore need at least two misses.

## `read-loop` — Read loop

**What:** the same range of the same file is read 3 or more times in a session
with no successful edit to that file in between. Paging through a file with
different offsets does not count.

**Why:** the agent keeps returning to the file without progress (often after
losing context).

**Attribution:** the file. Weak signal, like `edit-thrash`.
