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
| `human-rework` | 2.5 | git |
| `edit-miss` | 2.0 | transcript |
| `edit-revert` | 2.0 | transcript |
| `command-failure` | 2.0 | transcript |
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
log that happens to contain "revert" does not count.

- Strong phrases (confidence 1.0): a leading "no" / "nope" (not "no problem",
  "no worries", ...), "that's not / that's wrong", "you broke / forgot /
  removed / ignored / didn't ...", "revert / undo / roll back that",
  "still failing / broken / not working", "I said / I told you / I meant /
  I didn't ask", "not what I asked", "why did you".
- Weak phrases: a leading "actually", "don't / do not / never / stop",
  "instead", "wrong / incorrect", "should be / should use / shouldn't".
  One weak phrase gives confidence 0.5, two or more distinct ones 0.75.

The confidence multiplies the severity. Slash commands, harness-generated
messages, tool results and prompts inside subagents are never corrections.

**Why it is friction:** human attention is the scarcest resource in the loop.

**Attribution:** files the agent touched in the preceding turn that the
message names (by path, or by bare file name if unambiguous). If it names
none, the correction is split evenly over the files edited in that turn (the
5 most recent). If nothing was edited it stays unattributed (counted in
`totals`, shown on no hotspot). A file the human mentions but the agent never
touched is not blamed.

**False positives:** "don't forget to ..." and "it should be sorted" are often
plain instructions (hence 0.5). **False negatives:** polite or indirect
corrections ("hmm, could we use X?"), and corrections in other languages.

## `retry-loop` — Retry loop

**What:** the same normalized command fails 3 or more times in a row within a
session, with no success of that command in between. One event per loop; its
summary carries the final count.

**Why:** the failure output did not lead the agent to a fix.

**Attribution:** the normalized command.

## `tool-rejected` — Tool call declined

**What:** the human declined a proposed tool call (Claude Code records this as
a tool result starting "The user doesn't want to proceed with this tool use").
The reason, if the human typed one, is kept as the excerpt.

**Why:** the agent tried to do something a human considered wrong or unsafe.

**Attribution:** the file the tool targeted, or the normalized shell command.
Declined tools with neither (e.g. MCP tools) are counted but unattributed.

## `user-interrupt` — User interrupt

**What:** a `[Request interrupted by user]` marker while the agent had made at
least one tool call in the turn. The marker that accompanies a declined tool
call is not counted again.

**Why:** the human saw the agent going wrong and stopped it.

**Attribution:** the target of the agent's most recent tool call.

**False positives:** people also interrupt to add information or because they
ran out of time.

## `human-rework` — Human rework (git)

**What:** a commit attributed to an agent changed a file, and the *next*
commit touching that file was made by a human within the rework window (14
days by default). One event per (human commit, file). A human commit that
follows another human commit is not rework.

Agent commits are recognised by `Co-Authored-By` trailers naming Claude,
Anthropic, Copilot, Codex, Cursor, Aider, Devin, OpenHands, Gemini, Jules or
Windsurf; "Generated with Claude Code"; an `aider: ` subject prefix; an
"(aider)" author; or a bot author with one of those names. Override with
`agent_commit_patterns`.

**Why:** agent output here needed a human follow-up soon after it landed.

**Attribution:** the file. Each agent commit counts as its own "session".

**False positives:** active files get follow-up commits for unrelated reasons;
this signal does not check that the same lines were touched. Squash merges
hide agent trailers (false negatives).

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
  output matching patterns such as "command not found", "No module named",
  "Cannot find module", "No such file or directory", "permission denied",
  "unrecognized argument", "usage:", "No rule to make target", "Missing
  script", "connection refused", "timed out". Full list:
  `INVOCATION_ERROR_RE`.
- *Ordinary failures* (anything else, typically a test that legitimately
  fails) count at weight 0.3 and claim no lost time. A red test during
  development is the tool working as intended.
- *Ignored:* read-only inspection commands (`ls`, `cat`, `grep`, `rg`, `find`,
  `test`, `diff`, `which`, ...), pipelines ending in one of them with exit
  code 1, interrupted commands, declined commands, and commands run with a
  working directory outside the repo.

**Why:** agents guess how to build, test and run things; a wrong guess costs a
round trip in every session until the right command is written down.

**Attribution:** the normalized command (see methodology). Ordinary failures
are also attributed to repo files named on the command line; setup errors are
not, because the files are not the cause.

**False positives:** the invocation patterns can match a program's own output
(a test asserting on "No such file or directory").

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
