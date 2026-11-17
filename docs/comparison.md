# agent-friction and Claude Code's `/insights`, on the same sessions

Claude Code has a built-in `/insights` command that reads your recent sessions and writes an
HTML report about how you work, with suggestions. People reasonably ask whether agent-friction
duplicates it. We ran both on the same transcripts to find out. Short answer: they answer
different questions and work well together. `/insights` is a coach for **how you and Claude
work**. agent-friction is a measurement of **where this repository trips agents up**, with the
evidence counted.

## Method

- **Date:** 2026-11-17. Claude Code 2.1.326 and agent-friction from this repository.
- **Data:** two synthetic, published datasets, so anyone can rerun this. No real transcripts.
  1. **shopfront** ([`tests/fixtures/transcripts`](../tests/fixtures/transcripts)): five Claude
     Code sessions (one with a subagent) and one session from another agent, on a small fake
     shop. Planted problems: `pytest` cannot run here, `make test` can; a generated file is
     hand-edited; a human keeps saying "use `PaymentClient`"; agents search for a
     `RefundPolicy` that does not exist; a human declines `rm -rf migrations/versions`.
  2. **harness blocks** ([`scripts/harness_blocks_story.py`](../scripts/harness_blocks_story.py)):
     twenty sessions of an agent watching a build. Claude Code's own rule refuses
     `sleep N; gh run view` 280 times; a human declines `sleep 60` twice and stops `ls` three
     times. The repository has nothing to do with any of it.
- **How `/insights` was run:** each dataset in its own empty Claude Code config directory, so
  only those sessions were visible: `CLAUDE_CONFIG_DIR=<dir> claude -p "/insights"`, with the
  transcripts under `<dir>/projects/`. The text of both reports is saved next to this page:
  [shopfront](comparison/insights-shopfront.txt),
  [harness blocks](comparison/insights-harness-blocks.txt).
- **How agent-friction was run:** `agent-friction demo` (dataset 1, output in
  [examples/shopfront.txt](../examples/shopfront.txt)) and
  `agent-friction scan REPO --transcripts DIR --repo-alias /repo --no-git` (dataset 2,
  [output](comparison/agent-friction-harness-blocks.txt)).

`/insights` uses a language model, so another run can word things differently and may pick
other sessions. The observations below are from one run each.

## What each one found

### Dataset 1: shopfront

| Planted problem | `/insights` | agent-friction |
| --- | --- | --- |
| Human keeps saying "use `PaymentClient`, not `legacy_charge`" | **Found**, told well: a narrative, a ready CLAUDE.md section, a PreToolUse hook that blocks `legacy_charge`, and an architecture test | **Found**: "Write down the rule the human keeps repeating", with the human's words quoted, pinned to `checkout/service.py` |
| Generated `schema_gen.py` edited by hand | **Found**, with a CLAUDE.md line and a hook that blocks edits to it | **Found** and ranked first: header detected, 7 events in 4 sessions, ~4m lost; also flags its 6 edit misses (1,031 lines: split or anchor) |
| `pytest` fails, `make test` works | Not found. Only a count: "Command Failed: 6" | **Found**: 7 failures in 4 sessions, `make test` did the job 5 times, "use `make test`, not `pytest`" |
| Agents search for a `RefundPolicy` that does not exist | Not found | **Found**: 5 empty searches under `src/shopfront/`, patterns listed |
| Human declines `rm -rf migrations/versions` and says why | Only a count: "User Rejected: 1" | **Found**: the boundary, with the human's reason quoted |
| The session from another agent (Aider format) | Not read: `/insights` reads Claude Code sessions | Read |
| Sessions covered | 4 of 5 Claude Code sessions | All 6 |

`/insights` also gave advice agent-friction does not try to give: on prompting habits ("get a
plan before any code is written"), on verifying before claiming success, and on larger workflows
(parallel agents to migrate off the legacy module).

### Dataset 2: harness blocks

| | `/insights` | agent-friction |
| --- | --- | --- |
| Sessions covered | 3 of 20 ("8 messages across 3 sessions (20 total)") | All 20 |
| 280 refusals of `sleep` by Claude Code's own rule | Counted as "Other: 42" tool errors (the 3 sessions it read), not explained | **Ranked first**: "Built into the agent tool, not this repository", 280 refusals in 20 sessions, quotes what the refusal says to do instead (`run_in_background`, Monitor), says nothing in the repository needs to change |
| Advice on waiting for the build | `gh run watch <id> --exit-status` (sound), or "a watch loop with a sleep interval" (may run into the same rule) | Use what the refusal names; one line in AGENTS.md saves the round trips |
| 2 declines of `sleep 60`, 3 stopped `ls` | Declines counted ("User Rejected: 2"); says no session shows "corrections, or interruptions", though each of the 3 it read has a stopped `ls` | Listed under their command, marked thin evidence, kept out of "What to fix first" |

## Cost and repeatability

| | `/insights` | agent-friction |
| --- | --- | --- |
| Run time here (wall clock, one run) | about 36 s (dataset 2) | 0.3 s (dataset 1), 0.6 s (dataset 2) |
| Model calls | Yes: it summarizes sessions with a model, which uses tokens | None. Pattern rules over the transcript, no network |
| Same input, same output | No: model-written, worded differently on another run | Yes, byte for byte, which is what makes `compare` possible |
| Sessions read | A sample of recent Claude Code sessions | Every session that touched the repository, from any agent in the [generic format](schema.md#generic-transcript-format) |
| Scope | You, across your projects | One repository (or one directory of it) |
| Evidence | Narrative, with examples | Counts per file and command, each event traceable to a transcript line |
| Before and after | No | `agent-friction compare before.json after.json` |

## Which to use

Use both. Run `/insights` to see how you work with Claude and what to try next. Run
agent-friction on a repository to find the files and commands where agents keep failing, fix
them with evidence, and measure whether the fix worked.

These are two small synthetic datasets, built to contain known problems. They show what each
tool can see, not how often each one is right on real work. If you run both on a real
repository and see something different, please
[open an issue](https://github.com/nurettincoban/agent-friction/issues/new).
