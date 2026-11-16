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

> **In short:** A human prompt that follows agent work and is worded as a
> correction of it ("No, …", "I told you …", "revert that", "still failing",
> "you broke …"), corroborated by behaviour: the agent had changed files, and
> the human names one of them or the agent then changes one of them again.
> Wording alone, or two weaker cues together, is only a probable correction.

**What:** a human prompt that (a) is not the first prompt of the session,
(b) follows at least one tool call by the agent, and (c) refers to the agent
or to the work it just finished in one of the ways below. Only the
first 280 characters are inspected, so a pasted log that happens to contain
"revert" does not count. Attachment markers at the start of the message
(`[Image #1]`, `[Pasted text #2 +14 lines]`) are skipped first, so a
screenshot followed by "still not aligned, wrong color" is read like the
sentence alone. The rules favour precision over recall: a new task that
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
    instruction or by something an agent does ("stop. use the other client",
    "stop editing the generated file", "stop guessing": a fixed list of verbs
    such as editing, changing, adding, removing, running, creating, using,
    committing, guessing; not "Stop. Hammer time", "stop the server", "stop.
    I need to go to lunch", and not "Stop showing the banner after the user
    dismisses it", which says what the product should stop doing);
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
    "never mind", "ignore it", "unrelated", "flaky", "my bad", ...) or only
    asking ("just curious", "just checking", "not complaining"), an
    acknowledgement ("you removed the flag as asked, now ..."), or saying
    that the problem predates the agent ("before your change", "already
    broken", "pre-existing", "not caused by you", "not your fault": "Still
    failing in CI on main before your change too - can you look at
    app/main.py?" is a new request): "you forgot
    nothing, great job", "I said use tabs in my last project but here spaces
    are fine". Praise *before* the phrase does not void it ("Great effort, but
    you broke the login page"). So do a shrug ("Not quite what I had in mind
    but fine", "... but it works", "... but good enough") and an afterthought
    that only adds to the request ("no, wait, also add a --verbose flag",
    "oh, one more thing").
  - *The human naming their own action as the cause* after the phrase:
    "because", "after" or "since", then "I" or "we", then what they did or
    failed to do ("hadn't", "didn't", "forgot", "never", "merged", "pulled",
    "rebased", "changed", "updated", "reverted", "switched", ...), or "because
    my VPN was off": "That didn't work because I hadn't pulled", "tests
    failing again after I merged main". Saying what they *saw* is not taking
    the blame, and stays a correction: "that doesn't work, I get a KeyError
    when I run it", "Still failing when I run make test", "It's still broken
    after I pulled your change" (what was pulled is the agent's work), "you
    broke the login since I can't sign in anymore", "No, that's wrong because
    we use tabs here". Unlike praise, self-blame is only the human's guess at
    the cause. If the agent then changes a file again that it had changed in
    the turn before the message, its work did need redoing, and the message
    is recorded as a *probable* correction (confidence 0.4, never quoted,
    `corroborated: softened`), attributed to those files. If the agent only
    reruns a command, or edits something else, nothing is recorded.
  - *Somebody else's words:* a quotation introduced by a reporting phrase
    ("Here's the reviewer's comment: 'No, I said to keep the old name'. Do
    you agree?", "Bob wrote: \"that is wrong, revert it\"", "the ticket says
    “...”") is taken out before anything is matched: the human is passing a
    remark on for discussion, not correcting the agent. "I said" and "you
    said" do not introduce somebody else's words ("No, I said to keep the old
    name" is a correction).
  - *Moving on* voids a verdict: "next task", "new task", "another issue",
    "let's move on", "next: ..." ("It doesn't work on Windows yet; next task: add Windows
    support").
  - *An opener that answers an offer or a choice:* if the final paragraph of
    the last thing the agent wrote puts an offer or a choice to the human
    ("Want me to refactor util.py? It would take a few minutes.", "Should I
    also rename it?", "Do you want tests?", "Which module?", "A or B?"), or
    the agent's last action was a question/choice tool (`AskUserQuestion`,
    `ExitPlanMode`, `ask_user`, ...), then a leading "No, ...", "Not quite"
    or "Stop." is read as an answer, not an objection. A closing courtesy is
    not such a question, because agents end most turns with one: "Anything
    else?", "Let me know if you'd like changes?", "How does that look?",
    "What next?". After any question, "wrong.", "no no", a request to undo
    ("roll it back", "revert that") and phrases addressed to the agent still
    count.
  - *A long pause:* a verdict typed more than 5 minutes after the agent's last
    activity, with nothing addressed to the agent, may be about something
    else by then. Naming a changed file is then not enough: it counts as a
    correction only if the agent goes back to the files in the turn that
    follows (the named ones, if any were named). Without that follow-up it
    is a *probable* correction (confidence 0.4, never quoted).
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

**What the evidence says:** the summary reads "The user corrected the agent
(confidence 1; matched "that's not what I …"; ...)" or, for a probable
correction, "Wording that could be a correction (...)". The `matched` labels
come from a fixed vocabulary that names the kind of phrase ("that's wrong",
"that's not right", "that's not what I …", "wrong.", "stop.", "revert that",
"roll it back", "still failing", "failing again", "you broke/forgot/…", ...),
never the human's own words: the summary is also shown without excerpts.

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
is read as a verdict. So is a "No, ..." in reply to a question that is neither
an offer nor a choice in the wording we know ("Does that look right?" - "No,
the header is off" is counted, and is usually meant, as a correction). Second-person phrases about the past ("you forgot the
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

> **In short:** A human prompt that is worded like a correction but is not
> corroborated by behaviour (the agent had changed nothing, or the human
> names no changed file and the agent did not go back to what it had
> changed), or that only contains two or more weaker cues. It may be an
> ordinary follow-up request.

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

> **In short:** The same command line failed 3 or more times in a row in one
> session with identical arguments, with no success of that command and
> nothing changed in between: no edit, and no other command that could have
> changed files (a script, an install, a move). Dropped as waiting if the
> same line then passes with nothing changed; never dropped because it passed
> after something was done.

**What:** the same command line fails 3 or more times in a row within a
session, with **identical arguments** (after collapsing whitespace and
dropping leading `VAR=value` assignments), no success of that command and
nothing changed in between (see below for what counts as a change). One event
per loop; its summary carries the final count.

Narrowing a failure down is progress, not a loop: `pytest`, then `pytest
tests/test_a.py -x -vv`, then `pytest tests/test_a.py::test_total --tb=long`
are three different invocations and are not counted.

Edit, run the tests, see red, edit again, run again is the normal development
loop, not a retry loop: every successful edit starts the count again. What is
counted is re-running a failing command with nothing changed on disk (other
working directory, the same thing again).

**What counts as a change.** We do not try to judge whether a change was
relevant, only whether something was, or may have been, changed:

1. *An edit:* any successful Edit/Write, whichever file it touches.
2. *A shell command that rewrites files* (`is_shell_edit`): `sed -i`, `perl
   -pi`, a formatter (`ruff format`, `black`, `prettier --write`, `gofmt -w`,
   `cargo fmt`, also behind `uv run` / `npx` / `python -m`), a `--fix` /
   `--write` flag, `patch`, `git apply | checkout | restore | stash | mv | rm
   | clean`, a codemod (`jscodeshift`, `comby`, `ast-grep`, `pyupgrade`, ...;
   `CODEMOD_PROGRAMS`), `mv` / `cp` / `rm` / `ln` / `rsync` / `tar` / `unzip`
   with an argument that is not a log or under `/tmp`, `>` / `tee` writes to
   such a file, and inline or piped code that calls something that writes:
   `python3 - <<'EOF' ... write_text(...) ... EOF`, `python -c "open(p,
   'w')..."`, `node -e "fs.writeFileSync(...)"` (`WRITE_CALL_RE`:
   `write_text`, `write_bytes`, `open(..., 'w' | 'a' | 'x')`, `.write(`,
   `writeFile[Sync]`, `fs.write | rename | unlink | rm`, `File.write`,
   `shutil.copy | move | rmtree`, `os.replace | rename | remove`, `.unlink(`,
   `.rename(`, `json.dump(`).
3. *A command that may have changed something:* any other command that ran
   to completion and is not a look and not a check. That is: not a read-only
   inspection command (`ls`, `cat`, `grep`, `git status | diff | log`, ...),
   not a test runner, linter or type checker, and not an aggregate target
   that runs them (`make check`). `python scripts/patch.py`,
   `./scripts/regenerate.sh`, `npm install`, `make generate`, `git commit`,
   `docker compose up` all qualify; commands that only show or send
   something (`git push`, `git fetch`, `docker logs`; `REPORT_ONLY_KEYS`) do
   not. Nobody saw what such a command did, so it
   is treated as "something possibly changed" and nothing more: it ends the
   streak of identical failures, but it is not an edit. It names no file,
   does not count as a round for `no-convergence`, and does not corroborate
   a correction. What a green run after such a command means is decided
   under [`command-failure`](#command-failure--command-failure) ("Fail,
   setup step, pass"). In a failed line
   only the steps that demonstrably ran before the failing one count
   (`python fix.py && pytest`), and the command a line is judged by is never
   its own fix: `python gen.py` red, `python gen.py` green is a failure that
   went away by itself, not red, fix, green.

A retry loop is *derived from* command failures. For confidence it is not a
second kind of evidence next to them (see methodology). Once a loop is recorded it stays, together with its
failures, even if the command goes green after a later edit, install or
any other command: the three runs with nothing changed already happened.
(If it went green after a setup step, its failures also record that step.)

**Waiting is not a loop.** If the very same command line fails 2 or more
times and then *succeeds*, with no change of any of the three kinds above in
between, the agent was waiting for something outside the session: `gh pr
checks` until CI is done, `curl -sf localhost:8000/health` until a server is
up. The loop and its failures are dropped. (For a test command only the loop
is dropped: nobody polls a test suite, and its red runs are kept as possible
flakiness, see `command-failure`.) A leading `sleep N &&` or `sleep N;` is
not part of the line: `sleep 3 && curl ...` is `curl ...` again. For a
connection probe (`curl`, `wget`, `nc`, `http`, `pg_isready`, `wait-on`) that
could not reach its server (see environment failures: connection refused,
curl's exit codes 7, 28, 52 and 56), **one** such failure is enough: `curl
localhost:3000/health` refused right after `npm run dev` was started, then
answered, is a server starting, not friction. Looking around while waiting (`cat
server.log`, `git status`) keeps it a wait. A loop that never passes, or that
passes only after a change or a command that may have changed something,
stays: then something was done about it.

**Why:** the failure output did not lead the agent to a fix.

**Attribution:** the normalized command.

**False negatives:** "May have changed something" is
generous by design: three identical failures with an unrelated `git commit`
or `docker restart db` between them are not a loop, although
nothing relevant changed. The rule prefers missing such a loop to reporting
the far more common red, script, red, script, green as one. A loop of
identical failures of a command that is not a test and then passes untouched
is dropped as waiting, although the agent may simply have been lucky.

**What still gets through (false positives):**

- *Changes the transcript does not show.* A fix made by the human in their
  editor, by another agent or session, by a file watcher, or by a subagent
  whose transcript is a separate stream, is invisible to the stream that
  re-ran the command. Its failures stay on the record, and three of them in
  a row are a retry loop.
- *Commands that are named like a check but change things.* `make test` that
  regenerates fixtures first, `pytest --snapshot-update`, `npm test -- -u`, a
  linter with an auto-fix configured in its settings and no `--fix` on the
  command line: by name these only report, so they are not a change.
- *Checks that never go green in the stream.* A check that is still red when
  the session (or the subagent) ends stays as ordinary failures, also when
  somebody else made it green afterwards.

## `no-convergence` — No convergence

> **In short:** Within one turn the agent ran the same command 4 or more
> times, each run after a file change since the previous one, every run
> failed as an ordinary failure, and the turn ended with the command still
> failing.

**What:** within one turn the agent ran the same command 4 or more times,
each of those runs came after a file change since the previous one (an
edit-and-run round; a first red run before anything was changed counts as
one of the four: red, edit, red, edit, red, edit, red), every run failed as an *ordinary* failure, and the turn
(or the session) ended with the command still red. One event per command and
turn. A green run of the command, or of another command doing the same job
(`make test` after `pytest ...`), at any point in the turn clears the count:
red, fix, green is never recorded, however many rounds it took. So do

- an **aggregate target** that runs every check: a make/just/npm/... target or
  script named `check`, `ci`, `verify`, `validate`, `all`, `precommit`,
  `qa`, ... (`AGGREGATE_WORDS`), or `pre-commit run`. A green `make check`
  ends a red `pytest` and a red `ruff check` alike (not `cargo check` or
  `ruff check`, which are single jobs);
- **any exit-0 command whose output carries the passing summary of the red
  command's own tool** (`./scripts/everything.sh` printing "All checks
  passed!" for a red `ruff check`);
- **the turn's last check being green:** if the last test, lint, type-check,
  format, build or aggregate command of the turn passed and nothing went red
  after it, the turn converged and no event is emitted, whatever the passing
  command was called.

If the command's **latest run showed neither red nor green** (`pytest -q | tail
-1` cut the verdict off; see `command-failure`), nobody saw how it ended: the
event is kept as a weak lead ("its later runs showed neither red nor green"),
capped, without recommendation and without any time claimed.

Instructions that a coordinating agent sends to a **subagent** are turn
boundaries like human prompts, so a subagent's rounds are counted per
instruction, not across its whole transcript. Re-running
without changing anything is a [retry loop](#retry-loop--retry-loop) instead,
and rounds in different turns are not added up, because a human steered in
between. Inline code (`python -c`) and setup/invocation errors do not count
as rounds.

**Why:** eight rounds of edit, run, red are not eight red tests during
development. The agent could not work out what the check wants, and gave up
or handed back.

**Attribution:** the normalized command, in full, and the files edited between
the red runs (the 3 most edited, the event split evenly between them).

**Time:** only the red runs themselves (each from call to result) and the
edits between them, at most 15 minutes per event, newest first. The agent's
reading and thinking between the rounds is not claimed: it may have been
useful. An event from a single session (see below) claims no time at all.

**One session is not a pattern.** A hard task can take many rounds. A target
whose `no-convergence` events all come from one session is treated like weak
evidence (capped, low confidence, no recommendation, no minutes in the row
or in the totals); from two sessions on it counts in full.
It is derived from `command-failure`, so next to it it is not a second kind
of evidence for confidence.

**False positives:** a genuinely hard change that was abandoned for unrelated
reasons; a long-running refactoring that keeps a suite red on purpose.

## `tool-rejected` — Tool call declined

> **In short:** The human declined a tool call the agent proposed. Declining
> a plan or a question is not counted.

**What:** the human declined a proposed tool call (Claude Code records this as
a tool result starting "The user doesn't want to proceed with this tool use").
The reason, if the human typed one, is kept as the excerpt.

**Why:** the agent tried to do something a human considered wrong or unsafe.

**Not counted:** declining a plan or a question the agent put to the human
(`ExitPlanMode`, `AskUserQuestion`, `ask_followup_question` and their like in
other harnesses; `QUESTION_TOOL_RE`). "Not yet, look at B first" is an answer,
not a veto of an action. These are in no total.

**Time:** the time between the call and its result is a human reading a
permission prompt, and is never counted, however long it took. What is
claimed is the time from the human's answer to the next thing in the
transcript (at most the usual cap); nothing if nothing follows.

**Attribution:** the file the tool targeted, or the normalized shell command.
For a chain of commands it is the step the human most plausibly objected to,
in this order: a step whose program the stated reason names; the first
destructive step (`rm`, `mv`, `dd`, `git push`, `git reset`, ...); the first
step that is neither setup nor read-only inspection; otherwise the line's own
key. So declining `rm -rf migrations && alembic revision` is recorded on `rm`.
Among several destructive steps (or several that the reason names: "git"
names `git commit` and `git push` alike), one that is forced (`-f`,
`--force`, `--hard`) or cannot be undone locally comes before `git commit |
stash | branch | tag`: `cat >> notes && git commit -m x && git push -f` is
recorded on `git push`.
Declined tools with neither file nor command (e.g. MCP tools) are counted but
unattributed.

## `user-interrupt` — User interrupt

> **In short:** The human interrupted the agent after it had made at least
> one tool call in the turn. The interrupt that accompanies a declined tool
> call is not counted again.

**What:** a `[Request interrupted by user]` marker while the agent had made at
least one tool call in the turn. The marker that accompanies a declined tool
call is not counted again.

**Why:** the human saw the agent going wrong and stopped it.

**Attribution:** the target of the agent's most recent tool call. If that call
was a shell command, the command is *not* blamed (stopping a running `pytest`
says nothing about `pytest`): the event goes to the files edited in the turn
(the 5 most recent, split evenly), or is unattributed if there are none.

*Unless it keeps happening.* The event remembers which command was stopped
(detail `interrupted`). A command that humans stop in 3 or more sessions is
no longer about the work in progress: each of those events is then also
attributed to the command, with a share of 0.3, which gives it a light row
under "commands" and the advice to say when it should be run and what to run
instead.

**False positives:** people also interrupt to add information or because they
ran out of time.

## `human-rework` — Human rework (git)

> **In short:** A commit attributed to an agent changed a file, and the next
> commit touching that file, made by a human within the rework window,
> rewrote lines the agent commit had written or is described as a fix or
> revert. A human continuing work elsewhere in the file does not count.

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

**Generated files and lockfiles are skipped.** A human commit that changes
`package-lock.json`, `Cargo.lock`, `*_pb2.py`, a file under `dist/` or a file
that says "DO NOT EDIT" after an agent commit touched it is a tool being
re-run (a dependency bump, a regenerated client), not a human repairing the
agent's lines. Files that look generated (the same test as for the
`edit-the-source-not-the-output` recommendation: the path patterns and the
marker in the first 2 KB) get no `human-rework` events.

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

**A repo-wide sweep is not rework.** A human commit that touches more than
20 files, does not call itself a fix, and either names a mechanical change
in its subject (reformat, prettier, black, lint, style, whitespace, license
headers, rename, codemod, ...) or replaces about as many lines as it adds
(within 20 %) is skipped like a bot commit (`is_sweep`): it rewrites the
agent's lines together with everybody else's.

**False positives:** a human changing the agent's lines for a new requirement
rather than because they were wrong; a file the agent created, where any
later change except an append overlaps "its" lines; a subject such as "fix
typo in README" on an unrelated part of the file. **False negatives:** squash
merges hide agent trailers; rework in a later commit than the next one;
renamed files; only the newest 400 candidate commits have their patches read.

## `edit-miss` — Edit miss

> **In short:** An edit failed because the text to replace was not found in
> the file, or matched several places.

**What:** an edit failed because the text to replace was not found in the
file, or matched several places.

**Why:** the agent's picture of the file was wrong (stale read, reformatting,
whitespace), or the file has repeated blocks that are hard to target.

**Attribution:** the file being edited.

## `edit-revert` — Edit reverted

> **In short:** An edit exactly undid an earlier successful edit to the same
> file in the same session, or a write restored an earlier version of the
> file. Taking out lines that were only added and then run successfully (a
> debug print) is not counted.

**What:** a successful edit whose (old, new) strings are exactly the (new,
old) strings of an earlier successful edit to the same file in the same
session, or a write whose content equals an earlier version of the file after
a different version was written in between. Strings are compared by hash.

*Temporary debugging is not a revert:* if the earlier edit only **added**
text (its old string survives inside its new string, e.g. a `print` after an
existing line), a command ran successfully while it was in place, and the
edit is then exactly undone, nothing is recorded. The agent looked at
something and cleaned up after itself.

*An undo the human asked for is not a revert:* when the prompt that started
the turn asks for a change to be taken back ("No, revert src/app.py", "undo
that", "put the old name back", "restore ..."), edits in that turn that undo
earlier ones are the agent doing as told. The human's message is the event
(a `user-correction`, if it is one); it is not counted a second time as an
`edit-revert`, and it does not feed "edits did not converge".

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

> **In short:** A shell command failed: it exited non-zero or, where the exit
> status belongs to a pipe or a later step, its own output says so. Setup and
> invocation errors (command not found, missing module, bad arguments, …) and
> environment failures (a service that is not running, missing credentials,
> an unset variable) count fully; ordinary failures, such as a test that
> fails, count at 0.3. A red run followed by an edit and a green run of the
> same job is dropped; one that passed only after a setup step, with no file
> edited, is kept in full. Read-only inspection commands (ls, grep, …) are
> ignored.

**What:** a shell command exits non-zero (or the tool reports an error).

- *Setup/invocation errors* count fully (weight 1.0): exit codes 126/127, or
  an output **line** with the shape the shell, interpreter or tool itself
  prints when the command never got to do its job: `bash: x: command not
  found`, `bash: ./x.sh: No such file or directory | Permission denied`,
  `ModuleNotFoundError: No module named ...`, `ImportError while loading
  conftest`, `can't open file`, `Cannot find module`, `error: unrecognized
  arguments`, `make: *** No rule to make target`, `npm error Missing script`, yarn's
  `error Command "test" not found.`, `ERR_PNPM_NO_SCRIPT`, npm's `Unsupported
  engine` / `EBADENGINE` (the wrong Node version is a setup problem),
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
- *A module that cannot be imported:* a missing third-party module (`No
  module named 'yaml'`) at collection time is a setup error. A module of the
  repository itself (the top-level package exists: `src/`, `app/`,
  `shop.py`, ...) can be two things, and only what happens next tells them
  apart (`own_module_missing`; the same rule whether pytest prints the name
  on an `E` line, under "ImportError while loading conftest", or directly):
  - a typo in an import (`No module named 'src.utlis'`): a bug. The failure
    starts as *ordinary*, and is gone entirely once an edit turns **the same
    command** green;
  - a package that is there but cannot be found the way the command was run
    (`pytest` without `PYTHONPATH=src`, a package that is not installed): a
    setup problem. The failure is promoted to a setup/invocation error
    (weight 1.0, no file blamed) as soon as it passes without the code
    having been the fix: after a setup step (`pip install -e .`, with
    `fixed_by_setup`), with a variable in front (`PYTHONPATH=src pytest`,
    with `works_with`), or when **another command for the same job** goes
    green (`make test`, `python -m pytest`, `uv run pytest`, with
    `recovered_by`), also if files were edited in between: an edit that made
    `make test` pass says nothing about whether `pytest` can import the
    package.

  If neither happens (the session ends red), it stays ordinary.
- *A foreground server is not a failed command:* when the harness timeout hits
  a command that never exits by design (`npm run dev`, `vite`, `uvicorn`,
  `flask run`, `rails s`, `manage.py runserver`, `docker compose up` without
  `-d`, ...; `DEV_SERVER_RE`), the event says so, and the recommendation is to
  run it in the background, not to document how to invoke it.
- *Calls that never ran are not failures:* a result that is the harness's own
  error message (`<tool_use_error>` without an exit code: "Cancelled: parallel
  tool call ... errored", "InputValidationError: ...") is dropped. "Blocked:
  ..." is a [`permission-denied`](#permission-denied--permission-denied).
- *"command not found" in every shell:* `bash: x: command not found`, `zsh:
  command not found: x`, cmd.exe's `'x' is not recognized as an internal or
  external command` and PowerShell's `The term 'x' is not recognized as the
  name of a cmdlet, function, script file, or operable program`
  (`CommandNotFoundException`) are the same setup/invocation error and
  record the program (`not_found`). If the same command then runs under a
  versioned name (`python3` for `python`) or, on Windows, with the launcher
  (`py -m pytest` for `python -m pytest`; `py` and `python` share a command
  key), that name is recorded as `works_as` and the advice is "use `py`, not
  `python`".
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
    issues`, `Found 0 errors`, `Found N errors (N fixed, 0 remaining)`, `N
    passing`, `N examples, 0 failures`, pytest's last progress line `....
    [100%]` without `F` or `E`; `GREEN_OUTPUT_RE`);
  - In a line with **several commands** (`ruff check . | tail -1; mypy | tail
    -1; pytest -q | tail -1`) each step is judged only by the summary of its
    own kind of tool (`VERDICT_SIGNATURES`: test, lint, type-check, format,
    build): ruff's `Found N errors` is not mypy's `Found N errors in M
    files`, and `Found 2 errors (2 fixed, 0 remaining)` is a successful
    `--fix`, i.e. green. The first step with a red verdict is the one that
    failed (and its excerpt ends at that step's verdict line, not at the
    output of whatever ran after it); a step whose own passing summary appears ran green and closes
    its own red, fix, green loop even when another step of the line is red;
    an aggregate target on such a line (`ruff format . && make check | tail
    -2 && git status`), which has no summary line of its own, ran green when
    nothing in the output is red and the output shows a passing test, lint,
    type-check or build summary that no other step of the line could have
    printed;
    and a setup error counts only if the output pins it on a step (the shell
    names the program, or the step's own signature is present). Text that
    merely looks like an error somewhere in the output of such a line is
    not a failure of its last command.
  - **unknown** otherwise. An unknown run is not a failure, and it is never
    used as proof of success either: it does not close a red, fix, green
    loop, is never the `recovered_by` of an earlier failure, does not end a
    wait or a retry loop, and does not clear a `no-convergence` stretch.

  `a && b` is not such a case: there the exit status is real.
- *Environment failures* count fully too (weight 1.0, variant
  `environment`, one round trip of lost time): the command and the code may
  be fine, but something around them was not there (`environment_error`).
  Three kinds, read from the output of a failure that is not a
  setup/invocation error:
  - `service`: something the command talks to is not running. `Cannot connect
    to the Docker daemon`, `Connection refused`, `ECONNREFUSED`, `could not
    connect to server`, `connection to server ... failed`, `Is the server
    running`. The service is named only from a fixed vocabulary (by its
    default port or a word in the output: PostgreSQL, MySQL, Redis, MongoDB,
    RabbitMQ, Elasticsearch, Kafka, Memcached, "the Docker daemon", else "the
    service on port N"), so no transcript text is passed on;
  - `auth`: credentials are missing or expired. `credentials expired`,
    `token has expired`, `Unable to locate credentials`, `You must be logged
    in`, `authentication failed`, `Permission denied (publickey`, a hint to
    run `gh auth login` / `aws sso login` / `docker login`, a remote that
    answers `The requested URL returned error: 403` (or 401), `Permission
    to <repo> denied to <user>`, `Invalid username or password`, `401
    Unauthorized`;
  - `port`: the port a server wants is taken (`EADDRINUSE`, `address already
    in use`, `port is already allocated`), usually by one left running
    earlier;
  - `disk`: the disk is full (`ENOSPC`, `No space left on device`);
  - `variable`: an environment variable is not set. `Missing required
    environment variable X`, `X: unbound variable`, `X is not set`, a bare
    `KeyError: 'X'` (upper-case names only). The name is shown only where
    excerpts are.

  Such a failure blames no file on the command line. It is dropped like any
  red run when an edit and then a green run follow (the host name was wrong
  in the code). Inline snippets are never classified this way.
  *False positives:* a test that asserts on such a message and fails for
  another reason, in a session that ends red.
- *Flaky tests* (variant `flaky`): a red run of a test command (`pytest`,
  `npm test`, `make test`, ...: role "test") that is followed by a green run
  of the same command with **nothing changed in between** (no edit, no
  command that changes files, no setup step, no other spelling of the
  command) is at first an ordinary failure like any other. "The same
  command" is the same command line (whitespace, leading variables and a
  leading `sleep` aside): `pytest tests/test_cart.py` passing after a red
  `pytest`, or `npm test -- src/cart.test.js` after `npm test`, is a narrower
  selection that leaves the failing test out. It says nothing about
  flakiness, and the red run stays an ordinary failure. When that happens
  in 3 or more episodes of work (`FLAKY_MIN_SESSIONS`), it is no longer
  chance: those failures become variant `flaky`, count fully (weight 1.0),
  claim their run time, are attributed to the command alone, and the command
  gets `fix-the-flaky-test`. Below 3 they stay ordinary: weak evidence, no
  advice. They are never among the dropped runs: `totals.dropped_red_runs`
  counts red runs that an edit fixed, one-off fixes, and commands that are
  not tests and were polled until they passed.
- *Commands launched in the background:* a shell call with
  `run_in_background: true` (or one the user sent to the background) answers
  at once with "Command running in background with ID: ...". That answer is
  **not an outcome**: the launch is neither red nor green, closes no red,
  fix, green loop, credits no setup step and recovers nothing. How the
  command ended is read from what reports it later: the result of a
  `BashOutput` / `TaskOutput` call for that id (`<status>failed</status>`,
  `<exit_code>4</exit_code>`, the output), or a `<task-notification>`
  message (status, and the exit code in its summary). The first such report
  is treated as a run of the launched command at that point, with that exit
  status and output, and is classified like any foreground run (setup
  error, environment failure, ordinary failure, or green). The event cites
  the line of the report. A task that was killed (`KillShell`, status
  `killed`) or is still running has no outcome and is not judged: stopping
  a server one started is not a failure of the server. In the generic
  format: `background: true` on the call and a later result with `of`
  (schema).
- *An import the agent just wrote:* a `ModuleNotFoundError` / `Cannot find
  module` for a module that one of the agent's last 8 successful edits
  imports (`import x`, `from x import`, `require('x')`, `from 'x'`) is the
  new code being wrong, not the command being run wrongly: it is an ordinary
  failure, not a setup/invocation error, and never leads to "document how to
  run". (A missing interpreter or program, exit 126/127, stays a setup
  error.)
- *A time-boxed start is not a failure:* `timeout 10 npm run dev` that exits
  124 (the time ran out) after printing that it was up (`ready in 300 ms`,
  `Local: http://...`, `listening on`, `compiled successfully`, `Application
  startup complete`, ...; `READY_BANNER_RE`) did what the agent wanted: it
  started. Nothing is recorded. Without such a banner, with another exit
  status, or without `timeout` in front, it is judged like any other run.
- *A `cd` that failed:* in `cd missing-dir && npm test`, when the shell
  reports that it could not change directory (`cd: missing-dir: No such file
  or directory`), nothing after the `cd` ran. No failure of `npm test
  (missing-dir/)` is recorded: the line is dropped.
- *Ordinary failures* (anything else: a red test, a linter or type checker
  reporting problems, a script that raises) count at weight 0.3 and claim no
  lost time. A red test during development is the tool working as intended.
  They are weak evidence: a command with nothing but ordinary failures is
  capped below the "pattern" band and gets no recommendation, in however
  many sessions a single red run appears. Two things lift an ordinary
  failure out of that class: a setup step that made it pass (next but one
  point), and staying red session after session: when a command fails 2 or
  more times in a session and is still red when the session ends (detail
  `never_green`), and that happens in 3 or more sessions, those failures are
  no longer weak (detail `recurring_red`) and each claims its own run time.
- *Inline code is not a command of the repository:* `python -c '...'`,
  `node -e '...'`, `ruby -e`, `perl -e`, `bun -e`, `php -r`, and scripts piped
  to an interpreter are keyed `python <inline>` / `python <stdin>`. What an
  ad-hoc snippet does is the snippet's business, so its failures are ordinary
  whatever they print; only a missing or non-executable interpreter (exit
  126/127) is a setup error. Inline code never gets "document the command".
  A snippet run from a working directory outside the repository (`cd /tmp &&
  python - <<EOF`, or a session whose directory is elsewhere) is not about
  the repository at all and is not recorded, like a script that lives
  outside it.
- *Windows shells:* the Claude Code `PowerShell` tool is a shell like `Bash`.
  When the session's working directory is a Windows path (`C:\...`), or the
  command line itself contains one, a backslash before a name is read as a
  path separator, not as a POSIX escape: `python scripts\gen.py` is `python
  scripts/gen.py`, not `python scriptsgen.py`. Escaped quotes and spaces
  stay escapes.
- *Red, fix, green is not counted at all:* an ordinary or environment
  failure that is followed by an **edit** (Edit/Write, or a shell command
  that rewrites files: kinds 1 and 2 of ["What counts as a
  change"](#retry-loop--retry-loop)) and then by a success of the same
  command, **or of another command doing the same job** (`pytest
  tests/test_a.py` red, edit, `make test` green), is dropped. That is the
  development loop doing its job. A failure stays on the record when the
  command goes green with nothing but looks and other checks in between
  (flaky, or fixed outside the transcript), never goes green in the session,
  or was part of a [retry loop](#retry-loop--retry-loop).
- *Fail, setup step, pass is the thing to write down:* when a red run goes
  green and the only things that happened in between were commands that may
  have changed something (kind 3), **with no file edited**, the code was
  never the problem. What is done with the failure depends on the command in
  between, not on how the failure was worded:
  - a **setup step** (`is_setup_step`: `pip install`, `npm ci`, `uv sync`,
    `docker compose up`, `make bootstrap`, `alembic upgrade`, ... by key; a
    tool with `install`, `ci`, `bootstrap`, `setup`, `migrate`, `up`,
    `login` or `sync` among its first words, such as `npx playwright
    install`; or a local configuration file created from a template, `cp
    .env.example .env`): the failure is kept at full weight with
    `fixed_by_setup` (several steps joined with ` && `), blames no file,
    claims its run time, and from two such events on leads to
    `document-the-setup-step`: "run `docker compose up` before `pytest`";
  - an **environment activated on the green line itself**: `pytest` fails
    with a setup/invocation error and `source .venv/bin/activate && pytest`
    (or `. venv/bin/activate && ...`, `poetry shell && ...`, `conda activate
    <env> && ...`, `nvm use && ...`) then passes with no file changed in
    between. The command key is the same both times, so nothing "replaced"
    it; what was missing is the activation, recorded as `fixed_by_setup`
    (`source .venv/bin/activate`) and named by `document-the-setup-step`. Not
    when the failed line had the activation too. A wrapper (`uv run pytest`)
    is another command and leads to `use-the-working-command` instead;
  - **any other command** (`./scripts/reset-db.sh`, `python tools/gen.py`,
    `make generate`): once, that is a coincidence or a one-off script, and
    the failure is dropped like the development loop (counted in
    `totals.dropped_red_runs`). The same failing command passing after the
    same other command in **two or more sessions** is a setup step under a
    name we did not know: those failures are kept with `fixed_by_setup`,
    exactly as above;
  - **version control** (`git`, `gh`) is never the fix: the failure is
    dropped.

  A setup step that succeeds is never itself "the check going green": `npx
  playwright install` running fine says nothing about `npm run e2e`.
  With an edit anywhere between the red and the green run, nobody can say
  what fixed it, and the failure is dropped.
  *False positives:* a flaky test that happens to pass after an unrelated
  `npm install`; one event at full weight in that session.
- *The wrong runner:* an ordinary failure followed, with nothing changed in
  between, by a green run of **another command for the same job** (`pnpm
  test` after a red `yarn test`, `make test` after `go test ./...`), where
  the failing command does not work later in the session, is at first
  ordinary too. When the same pair recurs in 3 or more episodes of work
  (`RUNNER_MIN_SESSIONS`), the failing command is the wrong one for this
  repository: those failures get `recovered_by`, count in full, and lead to
  `use-the-working-command`. An aggregate target (`make check`) passing is
  not a runner for the job.
- *A flag that made it work:* a setup/invocation failure followed, with no
  file changed, by the same command line plus up to three options (`npm
  install --legacy-peer-deps` after `npm install` failed with `ERESOLVE`) is
  recorded with `works_with` naming the full form, like a variable in front.
  Not without excerpts: options are arguments, and no argument survives
  there.
- *A test file that is not there:* `pytest tests/test_new_feature.py` failing
  with `file or directory not found` for a path given on the command line,
  where the file is not known to exist and is not created afterwards, is
  recorded as a [`path-miss`](#path-miss--path-miss) on that path ("`pytest`
  was pointed at tests/test_new_feature.py, which does not exist"), not as a
  failure of the command: the agent guessed a path, and nothing is wrong
  with how `pytest` is run.
- *Test-first development is red, fix, green too:* a setup/invocation error
  that names what is missing (`No module named 'app.newfeat'`, `can't open
  file 'scripts/run.py'`, `file or directory not found: tests/test_x.py`,
  `Cannot find module './util'`) is dropped when a file that provides exactly
  that module or path is then **created** in the repo (a Write of a file the
  session had neither read nor changed before) **and** the command goes
  green. Editing a file that was there all along does not count: it cannot
  be what was missing. Nor does a file created after the command, or another
  command for the same job, had already gone green: by then the failure was
  resolved some other way (a setup step, another runner), and it stays on
  the record with that. A missing third-party module (`No module named
  'yaml'`) is never created in the repo and stays a setup error. Other
  setup/invocation errors are never dropped.
- *A typo the agent corrected is weak:* a setup/invocation failure followed,
  within the next 5 shell calls of the turn, by a successful command that
  differs from it in one word by an edit distance of at most 2 (`npm run
  biuld` then `npm run build`, `make tset` then `make test`) is marked as a
  mistyped form of that command. It is weak evidence, and never something to
  document.
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
- *An inspection command that could not reach what it inspects is not
  ignored:* `docker ps` answering `Cannot connect to the Docker daemon`, or
  `kubectl get pods` answering `You must be logged in`, is an environment
  failure (kinds `service` and `auth`) like any other.
- *Ignored:* read-only inspection commands and probes whose exit status is
  the answer (`ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, `find`, `sed`
  without `-i`, `awk`, `jq`, `diff`, `test`, `[`, `which`, `command -v`,
  `type`, `hash`, `pgrep`, `lsof`, `ag`, `fd`, `pg_isready`, `nc -z`, `ping`,
  `terraform plan -detailed-exitcode`, `git diff | status | log | show | blame |
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
reported red was not "replaced" by the next green one, unless the same pair
recurs untouched in 3 or more sessions: the wrong runner, above), and only if a command
*doing the same job* then succeeded (same
role: test, build, lint, type-check or format; or a wrapper of the same tool,
such as `uv run pytest` or `make test` for `pytest`) and the failing command
did not succeed later in the session; `fixed_by_setup` if the same command
succeeded after an install/setup command (`pip install`, `npm ci`, `uv sync`,
`docker compose up`, ...) or with an environment activated in front of it.
A failure that is the last thing in its session (no later tool call and no
later human message: the session was still running when it was read, or was
abandoned there) is marked `trailing` (`evidence[].trailing: true`). It
counts like any other failure - an agent that stops on a red run has not
been helped by the repository - but nothing shows whether it was resolved,
and reports say so. A `git commit` or `rm` that happens to succeed
next is neither.

**False positives / limits:** when the output of a chain matches no known
step signature the step is a guess (and marked as such). A program that prints one of the anchored shapes itself
(a test runner echoing a shell error on its own line) is misread as a setup
error.

## `edit-thrash` — Edit thrash

> **In short:** One file received 6 or more successful edits within a single
> turn in which something also went wrong: an edit miss, a stale edit or a
> reverted edit on that file, or a failed command or retry loop in the turn.
> Not recorded next to a no-convergence event of the same turn that names the
> file.

**What:** 6 or more successful edits to one file within a single turn **in
which something also went wrong**: an edit miss, a stale edit or a reverted
edit *on that file*, or a failed command or retry loop in the turn. A miss on
another file does not count. One event per file and turn. Eight
clean edits followed by a green test run are a productive sitting, not thrash,
and are not recorded. The event notes whether the turn's last command
succeeded (`ended_green`); if it did, the "edits did not converge"
recommendation does not count it. It also notes whether a test, lint or type
check ran in the turn (`checked`): thrash next to a check was not for want of
one, so that recommendation does not count it either.

When the same turn also produced a [`no-convergence`](#no-convergence--no-convergence)
event that names the file, no `edit-thrash` is recorded for it: the many
edits *are* the rounds of that stall, and one stall is one event, not two
kinds of evidence. (A `no-convergence` event whose last run showed no
verdict claims nothing, and leaves the thrash in place.)

**Why:** many small edits in one go, with failures in between, usually mean
trial and error.

**Attribution:** the file.

**False positives:** legitimately building a large file step by step while an
unrelated command fails in the same turn. This is a weak signal: on its own
its score is capped, it recommends nothing and never produces "high"
confidence.

## `file-too-large` — File too large to read

> **In short:** A read was refused because the file exceeds what the harness
> returns in one piece.

**What:** a read was refused because the file exceeds what the harness
returns in one piece ("File content (31245 tokens) exceeds maximum allowed
tokens").

**Why:** the agent has to page through the file or search it blind, and it
will hit the same wall in every session.

**Attribution:** the file. Two or more such reads recommend splitting the
file or documenting where its important parts are.

## `permission-denied` — Permission denied

> **In short:** A tool call was refused by permission settings, a hook or a
> safety check before it ran. No human was asked, and the command or edit
> never happened.

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

> **In short:** An edit or write was refused because the file had not been
> read first, or changed on disk after it was read.

**What:** an edit or write was refused because the file had not been read
first, or was modified on disk after the agent read it.

**Why:** "modified since read" means something else rewrites the file: a
formatter, a watcher, a code generator, another agent. "Not read first" is
mostly agent sloppiness and is recorded with a separate variant.

**Attribution:** the file.

## `path-miss` — Path miss

> **In short:** The agent tried to read or edit a path that does not exist. A
> read of a missing file that the agent then creates in the same session is
> not recorded.

**What:** a read or edit of a path that does not exist, or a test command
pointed at a test file that does not exist (see `command-failure`). A read of a missing
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

> **In short:** A Grep or Glob search returned nothing. A search that only
> confirms a name is free before the agent creates it is not recorded.

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
nothing but search misses has its score capped and gets no recommendation.

**What lifts it:** the same pattern missed in two or more episodes of work,
each time by an agent that **wanted a hit** (detail `wanted`): later in the
same turn it searched again with a pattern that shares a word with the
missed one (`RefundPolicy`, then `refund_policy` or `*policy*.py`; words are
the camel-case or snake-case parts of 4+ letters), or read, or tried to
read, a path containing such a word. A search the agent was content to see
come back empty (it moved on, or the turn ended there, as a last check after
edits does) never lifts anything, however often it recurs.

**Leftover checks never count** (detail `hygiene`): a pattern from the
families people grep for before finishing — debug statements (`console.log`,
`debugger`, `print(`, `pdb`, `breakpoint()`, `set_trace`, `binding.pry`,
`dbg!`, `var_dump`), work markers (`TODO`, `FIXME`, `XXX`, `HACK`,
`NOCOMMIT`), conflict markers (`<<<<<<<`, `>>>>>>>`), focused or skipped
tests (`.only`, `fit(`, `fdescribe(`) — is meant to find nothing
(`HYGIENE_PATTERN_RE`). Such misses are still listed as weak events, but
they never lift a hotspot out of the weak class and are left out of the
"point agents to what they are searching for" advice.

## `read-loop` — Read loop

> **In short:** The same range of a file was read 3 or more times in one
> session with no successful edit to it in between.

**What:** the same range of the same file is read 3 or more times in a session
with no successful edit to that file in between. Paging through a file with
different offsets does not count. A green run of a test, lint, type-check,
build or aggregate command starts the count again for every file: reading a
file once more between runs that pass is following along while things work,
not going round in circles.

**Why:** the agent keeps returning to the file without progress (often after
losing context).

**Attribution:** the file. Weak signal, like `edit-thrash`: alone it is
capped and recommends nothing.
