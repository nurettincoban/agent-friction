# Answer key for the `/insights` comparison

Written and committed **before** the 2026-11-17 re-runs of both tools, so that what
counts as "found" was fixed before anyone saw the results. The same people built the
datasets and agent-friction, which favours agent-friction; the key says so, and lists
decoys that a careful tool should *not* raise as a problem.

## Scoring

Each item gets one score per run:

- **2, found**: names the specific target (the file, the command, the rule) and the
  mechanism (why it goes wrong).
- **1, partial**: only the symptom or a count ("Command Failed: 6"), or the right
  theme on the wrong target.
- **0, missed.**

Scored separately:

- **Fix (yes/no)**: an actionable fix that would work as given. A fix that would run
  into the same problem (a `sleep` loop to wait for a build, when `sleep` is what is
  refused) is "no".
- **Unsupported claims**: statements the sessions contradict.

`/insights` samples sessions and is worded by a model, so it is run three times per
dataset, each in a fresh config directory, and each item is reported as its scores in
the three runs ("2 / 2 / 1"). We also note whether the run read a session that contains
the item: a miss on an unread session is a sampling miss, not a detection miss.
agent-friction is deterministic: it is run once.

## Dataset 1: shopfront (`tests/fixtures/transcripts`)

| # | Planted item | Found means | Fix means |
| --- | --- | --- | --- |
| S1 | A human tells the agent to use `PaymentClient`, not `legacy_charge` / the legacy module, in 3 sessions (gift message, partial refunds, discount codes) | names the rule and that it recurs across sessions | a rule to write down: use `PaymentClient`, not `legacy_charge` |
| S2 | `src/shopfront/api/schema_gen.py` is generated (header: from `api/openapi.yaml`, `make codegen`) and agents edit it by hand (edit misses, a stale edit, 2 corrections) | names the file and that it is generated output edited by hand | edit `api/openapi.yaml`, run `make codegen`; or block edits to the file |
| S3 | `pytest` cannot run here (no `PYTHONPATH`, no `DATABASE_URL`); `make test` can | names `pytest` failing and `make test` working | use `make test` |
| S4 | Agents search for a `RefundPolicy` that does not exist | names the vain searches for that concept | say where it is or that it does not exist |
| S5 | A human declines `rm -rf migrations/versions` and says why | names the declined command and the reason | a stated boundary (instruction or deny rule) |
| S6 | The agent says "Discount codes work and all tests pass"; the human finds it failing on staging | names the false success claim | a way to verify before claiming done |
| S7 | One session is from another agent (Aider, generic format) | the tool reads it | (none) |

Decoys (raising one as a repository problem to fix is a false positive):

| # | Decoy | Why it is not a problem to fix |
| --- | --- | --- |
| D1 | One `curl` to the staging host fails (connection refused) | a one-off: staging was down |
| D2 | A red test run that the agent then fixed | the normal development loop |

## Dataset 2: harness blocks (`scripts/harness_blocks_story.py`)

| # | Planted item | Found means | Fix means |
| --- | --- | --- | --- |
| H1 | Claude Code's own rule refuses `sleep N; gh run view` 280 times in 20 sessions | names the refusals and that they come from the agent tool, not the repository | do what the refusal says (run in the background, Monitor), or a blocking `gh run watch` |
| H2 | A human declines `sleep 60` twice | counts or names the declines | (thin: none expected) |

Decoy:

| # | Decoy | Why it is not a problem to fix |
| --- | --- | --- |
| D3 | A human stops an `ls` in 3 sessions | 3 interruptions of a harmless command: too little to change anything; a tool may list it, but not as a fix |

Coverage (sessions read) is reported for both datasets.
