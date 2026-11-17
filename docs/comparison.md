# agent-friction and Claude Code's `/insights`, on the same sessions

Claude Code has a built-in `/insights` command that reads your recent sessions and writes an
HTML report about how you work, with suggestions. People reasonably ask whether agent-friction
duplicates it. We ran both on the same transcripts to find out. Short answer: they answer
different questions. `/insights` coaches **how you and Claude work**. agent-friction measures
**where this repository trips agents up**, with the evidence counted, and hands over the fix as
text to paste.

## Method

- **Date:** 2026-11-17. Claude Code 2.1.326 and agent-friction from this repository.
- **Data:** two synthetic, published datasets, so anyone can rerun this. No real transcripts.
  1. **shopfront** ([`tests/fixtures/transcripts`](../tests/fixtures/transcripts)): five Claude
     Code sessions (one with a subagent) and one session from another agent (Aider), on a small
     fake shop.
  2. **harness blocks** ([`scripts/harness_blocks_story.py`](../scripts/harness_blocks_story.py)):
     twenty sessions of an agent watching a build, where Claude Code's own rule refuses
     `sleep N; gh run view` 280 times.
- **Who built them:** the authors of agent-friction, around problems agent-friction was built
  to find. That favours agent-friction twice over: the datasets are home ground, and
  agent-friction was **improved on these same datasets** after a first comparison earlier the
  same day showed what it missed (see "What changed" below). The
  [answer key](comparison/answer-key.md) (what was planted, what counts as found, and three
  decoys a careful tool should *not* flag) was committed before the `/insights` runs below,
  so it fixes how `/insights` is scored; it was written after agent-friction's changes, so it
  does not protect agent-friction's score from that tuning.
- **`/insights`:** run **three times per dataset**, each in a fresh, empty Claude Code config
  directory holding only that dataset: `CLAUDE_CONFIG_DIR=<dir> claude -p "/insights"`. The
  text of all six reports is saved under [`comparison/runs/`](comparison/runs).
- **agent-friction:** deterministic, so run once: `agent-friction demo`
  ([text](../examples/shopfront.txt), [Markdown](../examples/shopfront.md)) and
  `agent-friction scan REPO --transcripts DIR --repo-alias /repo --no-git`
  ([output](comparison/agent-friction-harness-blocks.txt)).
- **Grading:** two graders scored every item with the key's rubric: one of us, and an
  independent model grader given only the key, the outputs and the transcripts. They agreed on
  every score except one (H2 below, settled by the key's wording). Both score sheets are in
  [comparison/scores.md](comparison/scores.md). Scores: **2** found (names
  the target and the mechanism), **1** partial (only a symptom or a count), **0** missed; and
  separately whether the fix offered would work as given.

## Sessions read

| | `/insights` (each of 3 runs) | agent-friction |
| --- | --- | --- |
| shopfront | 4 of 5 Claude Code sessions; not the Aider session | all 6 |
| harness blocks | 3 of 20 | all 20 |

`/insights` read the **same** sessions in all three runs. In both datasets the sessions it
skipped are exactly those with a single human message (the API-version session; harness
sessions 3 to 19), so it looks like a filter rather than random sampling. We did not find this
documented, so treat it as an observation.

## What each one found

Scores for `/insights` are run 1 / run 2 / run 3.

### Dataset 1: shopfront

| # | Planted item | `/insights` | agent-friction |
| --- | --- | --- | --- |
| S1 | A human says "use `PaymentClient`, not `legacy_charge`" in 3 sessions | **2 / 2 / 2**, fix works: a narrative, a ready CLAUDE.md section, a hook | **2**, fix works: "corrected here 3 times in 3 sessions", the rule as they put it ("use `PaymentClient` (said in 3 sessions), not `legacy_charge` (said in 2)"), and the line to paste |
| S2 | Generated `schema_gen.py` edited by hand | **2 / 2 / 2**, fix works: CLAUDE.md lines and a PreToolUse hook | **2**, fix works: the header's source and command named (`api/openapi.yaml`, `make codegen`), a line to paste and a deny rule, replayed: "would have refused 9 calls in 3 sessions, all on that file" |
| S3 | `pytest` fails here, `make test` works | **1 / 1 / 1**: only "Command Failed: 6". Runs 1 and 3 advise running `pytest`, which fails here | **2**, fix works: "use `make test`, not `pytest`" (7 failures in 4 sessions, `make test` did the job 5 times) |
| S4 | Agents search for a `RefundPolicy` that does not exist | **0 / 0 / 0** | **2**: 5 empty searches, the patterns listed. The fix is generic ("say where it is or that it does not exist") |
| S5 | A human declines `rm -rf migrations/versions` and says why | **1 / 1 / 1**: only "User Rejected: 1" | **2**, fix works: the reason quoted and a line to paste; the `Bash(rm -rf migrations/versions:*)` rule it adds stops that command as typed, not `rm -r ./migrations/versions`, and the report says so |
| S6 | "All tests pass", then the human finds it failing on staging | **2 / 2 / 2**, fix works: a definition of done, a verify skill | **2**, fix works: "said the work was done ... the last check it ran and saw pass was `make test`: it did not cover what broke", and a line to paste |
| S7 | The Aider session (coverage, not scored) | not read: `/insights` reads Claude Code sessions only, by design | read |
| D1 | Decoy: one `curl` to staging fails | not raised | a low-confidence row, not in "What to fix first" |
| D2 | Decoy: a red run the agent then fixed | not raised | "Not counted: 1 failed run of normal work" |

### Dataset 2: harness blocks

| # | Planted item | `/insights` | agent-friction |
| --- | --- | --- | --- |
| H1 | Claude Code's own rule refuses `sleep N; gh run view` 280 times | **1 / 1 / 1**: counted as "Other" tool errors and read as Claude polling too often; the refusals are never mentioned. Fix: `gh run watch --exit-status` (sound), next to "an `until` loop with `sleep 30`" (close to what is refused) | **2**, fix works: reported apart as the agent tool's own rule, 280 refusals in 20 sessions, what the refusal says to do instead, counted in no score |
| H2 | A human declines `sleep 60` twice | **2 / 2 / 2** by the key's letter (the count "User Rejected: 2"; one grader gave 1, as only a count) | **2**: listed, marked thin evidence |
| D3 | Decoy: a human stops `ls` in 3 sessions | not raised | listed under "Nothing to fix yet: leads to watch", unnumbered |

### Statements the sessions contradict

Both graders checked each report against the transcripts.

- **`/insights`, shopfront:** all 3 runs say the corrected sessions ended mid-fix or
  without a test run; the gift-message and refunds sessions end with `make test` passing (true
  of the discount session, where the staging check after the fix failed). Run 1 suggests adding
  a "DO NOT EDIT" header to `schema_gen.py`, which already has one, and says the agent
  "declared work done before tests confirmed it", although `make test` had passed. (All 3 runs
  also say the refunds were built on `payments/legacy.py`; the agent called `legacy_charge`
  and never opened that file, but this is close to the human's own words, so we do not count
  it.)
- **`/insights`, harness blocks, all 3 runs:** "no mid-session corrections" or "without
  interrupting", although each session it read has a stopped `ls`, and two of them a declined
  `sleep 60`.
- **agent-friction:** none found. The graders' complaints were about weakness, not error: the
  S4 fix is generic, a few one-event rows (a `curl`, a missing test file) are noise, and with
  only one event an `ls` that was stopped is listed under "Commands that keep failing".

### Where `/insights` is better

- It tells the story. Its account of the payments sessions, and its point that the human
  refers to a `PaymentClient` instruction no session shows (so the rule lives in someone's
  head), is good writing that agent-friction does not attempt.
- It coaches the person: ask for a plan before code, say what "done" means, skills and
  headless review to try, larger workflows. agent-friction stays inside one repository.
- It works across all your projects; agent-friction looks at one repository at a time.

## Totals

Planted items S1 to S6, H1 and H2; S7 is coverage and decoys are scored as false positives.

| | `/insights` (per run) | agent-friction |
| --- | --- | --- |
| Score, out of 16 | 11 / 11 / 11 | 16 |
| Items with a fix that works as given (7 expect one: S1 to S6, H1) | 4 / 4 / 4 (S1, S2, S6, H1); H1's sound command sits next to a doubtful one | 7 (S4's is generic) |
| Decoys raised as problems | 0 of 3 | 0 of 3 |
| Statements the sessions contradict | several in every run (listed above) | none found |

What these numbers support: on these planted items, in these runs, agent-friction found every
one and `/insights` scored the same 11 of 16 three times; neither raised a decoy. What they do
not support: detection rates on real work, "more accurate" in general, or anything statistical.
Eight items in two datasets made by one side, with agent-friction tuned on them, are far too
few for that. They show what each tool can see.

## Cost and repeatability

| | `/insights` | agent-friction |
| --- | --- | --- |
| Run time here (wall clock) | 31 to 55 s per run (6 runs) | about 0.3 s (shopfront), 0.5 s (harness blocks) |
| Model calls | Yes: it summarizes sessions with a model, which uses tokens | None. Pattern rules over the transcript, no network |
| Same input, same output | No: worded differently each run (scores were stable here) | Yes, byte for byte, which is what makes `compare` possible |
| Sessions read | Claude Code sessions with more than one human message, as observed here | Every session that touched the repository, from any agent in the [generic format](schema.md#generic-transcript-format) |
| Scope | You, across your projects | One repository (or one directory of it) |
| Evidence | Narrative, with examples | Counts per file and command, each event traceable to a transcript line |
| The fix | CLAUDE.md sections, hooks, skills to copy | Lines and deny rules to paste, each replayed against the sessions |
| Before and after | No | `agent-friction compare before.json after.json` |

## What changed in agent-friction for this comparison

The first version of this page (same day) found that `/insights` handed over finished fixes
while agent-friction left homework ("say in AGENTS.md which source to change"). Since then:

- **Ready to paste**, in every format: the AGENTS.md lines and `.claude/settings.json` deny
  rules the advice names, filled in from the evidence; each deny rule replayed over the
  sessions to show what it would have refused.
- A generated file's advice names the source and the command its header gives.
- A correction that names code the agent had just written ("Actually `legacy_charge()` is
  deprecated") counts once the agent changes that code, so the payments rule is now 3
  corrections in 3 sessions, not 2; the rule is stated from the names the human used.
- New advice, "Say how to check work here before calling it done", when the agent said it was
  done and the human found it failing (S6, which `/insights` caught and agent-friction did not).
- With only thin evidence, the list is "Nothing to fix yet: leads to watch", not "What to fix
  first".

## Which to use

Use both. Run `/insights` to see how you work with Claude and what to try next. Run
agent-friction on a repository to find the files and commands where agents keep failing, paste
the fix, and measure whether it worked.

If you run both on a real repository and see something different, please
[open an issue](https://github.com/nurettincoban/agent-friction/issues/new).
