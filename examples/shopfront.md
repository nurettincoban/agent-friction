# Agent friction map: shopfront

6 agent sessions analysed · 34 friction events · ~9m lost · 4 human corrections · 5 files affected

_Not counted: 1 failed run of normal work (the agent fixed the failure, or was only waiting and polled until it passed; ~6s of run time)._

## What to fix first

1. `src/shopfront/api/schema_gen.py`: **Generated file: point agents at its source** This file looks generated (judging by a "code generated" marker), yet agents ran into trouble editing it directly 7 times in 4 sessions. Say in AGENTS.md which source to change and which command regenerates it, and consider denying direct edits to it.
2. `pytest`: **Tell agents to use `make test`** `pytest` failed 7 times in 4 sessions; 5 times the agent then got the same job done with `make test`, and `pytest` did not work later in those sessions. Add a line to AGENTS.md such as: "use `make test`, not `pytest`".
3. `src/shopfront/checkout/service.py`: **Write down the rule the human keeps repeating** A human corrected the agent here 2 times in 2 sessions. Put the rule in AGENTS.md / CLAUDE.md (or a comment at the top of the file) so it is read before the next attempt. In their words: "It's still failing for me on staging, and you used legacy_charge again in src/shopfront/checkout/service.py. I told you: PaymentClient only." / "No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint."
4. `src/shopfront`: **Point agents to what they are searching for** Agents searched under src/shopfront/ and found nothing 5 times in 2 sessions. They looked for `RefundPolicy` (2x), `**/*policy*.py`, `DiscountPolicy`. If the concept exists under another name or elsewhere, add a pointer in AGENTS.md (or a comment/alias where agents look). If it does not exist, say so.
5. `rm`: **State the boundary** A human declined the agent's action once in 1 session. State the boundary explicitly (what must not be touched or run, and what to do instead) in AGENTS.md, or encode it as a permission rule. Reasons given: "never delete existing migrations, add a new revision on top"

## Files where agents struggle

| # | File | Score | Sessions | Events | Est. time lost | Top signals | Confidence |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | `src/shopfront/api/schema_gen.py` | 56.8 | 4 | 12 | 4m | `edit-miss`×6, `user-correction`×2, `probable-correction`×1 | high |
| 2 | `src/shopfront/checkout/service.py` | 43.4 | 3 | 6 | 2m | `edit-revert`×2, `user-correction`×2, `edit-thrash`×1 | high |
| 3 | `src/shopfront/payments/refunds.py` | 11.4 | 1 | 1 | 18s | `path-miss`×1 | low |
| 4 | `api/openapi.yaml` | 9.7 | 1 | 1 | n/a | `search-miss`×1 | low |
| 5 | `tests/test_refunds.py` | 8.5 | 1 | 1 | n/a | `command-failure`×1 | low |

## Commands that keep failing

| # | Command | Score | Sessions | Events | Est. time lost | Top signals | Confidence |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | `pytest` | 48.6 | 4 | 7 | 3m | `command-failure`×7 | medium |
| 2 | `rm` | 16.3 | 1 | 1 | 18s | `tool-rejected`×1 | low |
| 3 | `curl` | 15.2 | 1 | 1 | 12s | `command-failure`×1 | low |
| 4 | `python -m pytest` | 15.2 | 1 | 1 | 30s | `command-failure`×1 | low |

## Directories where agents search in vain

Only friction in the directory itself (searches that found nothing there); `agent-friction show <dir>` rolls up everything inside it.

| # | Directory | Score | Sessions | Events | Est. time lost | Top signals | Confidence |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | `src/shopfront` | 29.5 | 2 | 5 | n/a | `search-miss`×5 | medium |

## Evidence

<details>
<summary><code>src/shopfront/api/schema_gen.py</code> · score 56.8 · 4 sessions</summary>

12 friction events in all 4 sessions that touched it (6 edit misses, 2 user corrections, 1 probable correction, 3 more signal types); high confidence because it recurs across sessions with several kinds of evidence.

**Recommendations**

- **Generated file: point agents at its source** This file looks generated (judging by a "code generated" marker), yet agents ran into trouble editing it directly 7 times in 4 sessions. Say in AGENTS.md which source to change and which command regenerates it, and consider denying direct edits to it.
- **Large file: split it or add unique anchors** 6 edits failed to locate the target in this 1031-line file (2 matched several places, 4 matched nothing) across 4 sessions. Split it into smaller modules, or give repeated blocks unique nearby text (section comments, distinct names) so an edit can be targeted.
- **Write down the rule the human keeps repeating** A human corrected the agent here 2 times in 2 sessions. Put the rule in AGENTS.md / CLAUDE.md (or a comment at the top of the file) so it is read before the next attempt. In their words: "No, stop hand-editing src/shopfront/api/schema_gen.py. It is generated: change api/openapi.yaml and run make codegen." / "That's wrong, schema_gen.py is generated. Revert that, edit api/openapi.yaml."

**Evidence**

- `2026-10-26 10:08 UTC` session `e65615b3` · Probable correction: Wording that could be a correction (confidence 0.4; matched "don't", "instead", "not allowed"; weaker cues only, no clear correction phrase); after edits to src/shopfront/checkout/service.py, src/shopfront/api/schema_gen.py
- `2026-11-03 09:01 UTC` session `4132e18c` · Stale edit: Edit on src/shopfront/api/schema_gen.py refused: the file changed on disk after it was read
  > File has been modified since read, either by the user or by a linter. Read it again before attempting to write it.
- `2026-11-03 09:04 UTC` session `4132e18c` · User interrupt: The user interrupted the agent while it was working on src/shopfront/api/schema_gen.py
- `2026-11-05 16:06 UTC` session `aider-20` · User correction: The user corrected the agent (confidence 1; matched "that's wrong", "revert that"; the user names src/shopfront/api/schema_gen.py, which it had changed); names src/shopfront/api/schema_gen.py
  > That's wrong, schema_gen.py is generated. Revert that, edit api/openapi.yaml.
- `2026-11-12 11:00 UTC` session `fb4e20bf` · Edit miss: Edit on src/shopfront/api/schema_gen.py failed: the text to replace was not found
  > String to replace not found in file. String: API_VERSION = "2.3"

</details>

<details>
<summary><code>src/shopfront/checkout/service.py</code> · score 43.4 · 3 sessions</summary>

6 friction events in all 3 sessions that touched it (2 reverted edits, 2 user corrections, 1 edit thrash episode, 1 more signal type); high confidence because it recurs across sessions with several kinds of evidence.

**Recommendations**

- **Write down the rule the human keeps repeating** A human corrected the agent here 2 times in 2 sessions. Put the rule in AGENTS.md / CLAUDE.md (or a comment at the top of the file) so it is read before the next attempt. In their words: "It's still failing for me on staging, and you used legacy_charge again in src/shopfront/checkout/service.py. I told you: PaymentClient only." / "No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint."
- **Give agents a fast way to check changes to this file** Edits here did not converge: 2 reverted edits in 1 session. Document the quickest command that validates this file (a single test, a type check) so agents can verify instead of iterating blind.

**Evidence**

- `2026-10-26 10:08 UTC` session `e65615b3` · Probable correction: Wording that could be a correction (confidence 0.4; matched "don't", "instead", "not allowed"; weaker cues only, no clear correction phrase); after edits to src/shopfront/checkout/service.py, src/shopfront/api/schema_gen.py
- `2026-10-29 14:11 UTC` session `9f6ff626` · User correction: The user corrected the agent (confidence 1; matched "no, …", "I told you"; the agent then changed src/shopfront/checkout/service.py again); attributed to src/shopfront/checkout/service.py
  > No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint.
- `2026-11-09 15:03 UTC` session `8d986baa` · Edit thrash: 7 successful edits to src/shopfront/checkout/service.py within one user turn
- `2026-11-09 15:14 UTC` session `8d986baa` · User correction: The user corrected the agent (confidence 1; matched "still failing", "I told you"; the user names src/shopfront/checkout/service.py, which it had changed); names src/shopfront/checkout/service.py
  > It's still failing for me on staging, and you used legacy_charge again in src/shopfront/checkout/service.py. I told you: PaymentClient only.
- `2026-11-09 15:15 UTC` session `8d986baa` · Edit reverted: Edit on src/shopfront/checkout/service.py undid an earlier edit from the same session

</details>

<details>
<summary><code>src/shopfront/payments/refunds.py</code> · score 11.4 · 1 session</summary>

1 friction event in the only session that touched it (1 path miss); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-10-29 14:00 UTC` session `9f6ff626` · Path miss: Read targeted src/shopfront/payments/refunds.py, which does not exist
  > File does not exist.

</details>

<details>
<summary><code>api/openapi.yaml</code> · score 9.7 · 1 session</summary>

1 friction event in 1 of the 2 sessions that touched it (1 search miss); low confidence because there is too little independent evidence so far. Only weak signals, which also accompany productive work: the score is capped at 19 and nothing is recommended.

**Evidence**

- `2026-11-12 11:01 UTC` session `fb4e20bf` · Search miss: Grep search in api/openapi.yaml returned nothing
  > pattern: API_VERSION

</details>

<details>
<summary><code>tests/test_refunds.py</code> · score 8.5 · 1 session</summary>

1 friction event in the only session that touched it (1 command failure); low confidence because there is too little independent evidence so far. Only weak signals, which also accompany productive work: the score is capped at 19 and nothing is recommended.

**Evidence**

- `2026-10-29 14:04 UTC` session `9f6ff626` · Command failure: `pytest` failed (exit 1; ordinary failure)
  > $ pytest tests/test_refunds.py -x Exit code 1 tests/conftest.py:11: in engine     return make_engine(os.environ\['DATABASE_URL'\]) E   KeyError: 'DATABASE_URL' ERROR tests/test_checkout.py - KeyError: 'DATABASE_URL'

</details>

<details>
<summary><code>pytest</code> · score 48.6 · 4 sessions</summary>

7 friction events in all 4 sessions that touched it (7 command failures); medium confidence because it recurs across sessions.

**Recommendations**

- **Tell agents to use `make test`** `pytest` failed 7 times in 4 sessions; 5 times the agent then got the same job done with `make test`, and `pytest` did not work later in those sessions. Add a line to AGENTS.md such as: "use `make test`, not `pytest`".

**Evidence**

- `2026-10-26 10:03 UTC` session `e65615b3` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest tests/test_checkout.py -q Exit code 4 ImportError while loading conftes … /shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'
- `2026-10-29 14:03 UTC` session `9f6ff626` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest tests/test_refunds.py Exit code 4 ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'
- `2026-10-29 14:03 UTC` session `9f6ff626` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest tests/test_refunds.py -x Exit code 4 ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'
- `2026-11-05 16:01 UTC` session `aider-20` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest -q ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'
- `2026-11-09 15:01 UTC` session `8d986baa` · Command failure: `pytest` failed (judged from its output: the exit status was that of a pipe or a later step; setup/invocation error)
  > $ pytest tests/test_checkout.py 2&gt;&amp;1 | tail -20 ImportError while loading confte … /shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'

</details>

<details>
<summary><code>rm</code> · score 16.3 · 1 session</summary>

1 friction event in the only session that touched it (1 declined tool call); low confidence because there is too little independent evidence so far.

**Recommendations**

- **State the boundary** A human declined the agent's action once in 1 session. State the boundary explicitly (what must not be touched or run, and what to do instead) in AGENTS.md, or encode it as a permission rule. Reasons given: "never delete existing migrations, add a new revision on top"

**Evidence**

- `2026-11-03 09:03 UTC` session `4132e18c` · Tool call declined: The user declined Bash on `rm` and said why
  > $ rm -rf migrations/versions &amp;&amp; alembic revision --autogenerate -m currency user said: never delete existing migrations, add a new revision on top

</details>

<details>
<summary><code>curl</code> · score 15.2 · 1 session</summary>

1 friction event in the only session that touched it (1 command failure); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-11-09 15:15 UTC` session `8d986baa` · Command failure: `curl` failed (exit 7; the service on port 443 was not reachable)
  > $ curl -s -H 'Authorization: Bearer \[REDACTED:token\]' https://staging.shopfront.example/api/health Exit code 7 curl: (7) Failed to connect to staging.shopfront.example port 443: Connection refused

</details>

<details>
<summary><code>python -m pytest</code> · score 15.2 · 1 session</summary>

1 friction event in the only session that touched it (1 command failure); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-11-12 11:02 UTC` session `fb4e20bf` · Command failure: `python -m pytest` failed (exit 4; setup/invocation error)
  > $ python -m pytest -q Exit code 4 ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'

</details>

<details>
<summary><code>src/shopfront</code> · score 29.5 · 2 sessions</summary>

5 friction events in all 2 sessions that touched it (5 search misses); medium confidence because it recurs across sessions.

**Recommendations**

- **Point agents to what they are searching for** Agents searched under src/shopfront/ and found nothing 5 times in 2 sessions. They looked for `RefundPolicy` (2x), `**/*policy*.py`, `DiscountPolicy`. If the concept exists under another name or elsewhere, add a pointer in AGENTS.md (or a comment/alias where agents look). If it does not exist, say so.

**Evidence**

- `2026-10-29 14:00 UTC` session `9f6ff626` · Search miss: Grep search in src/shopfront returned nothing
  > pattern: RefundPolicy
- `2026-10-29 14:00 UTC` session `9f6ff626` · Search miss: Glob search in src/shopfront returned nothing
  > pattern: src/shopfront/**/refund*.py
- `2026-11-09 15:00 UTC` session `8d986baa` · Search miss: Grep search in src/shopfront returned nothing
  > pattern: DiscountPolicy
- `2026-11-09 15:00 UTC` session `8d986baa` · Search miss: Grep search in src/shopfront returned nothing
  > pattern: RefundPolicy
- `2026-11-09 15:00 UTC` session `8d986baa` · Search miss: Glob search in src/shopfront returned nothing
  > pattern: **/*policy*.py

</details>

## Signals

| Signal | Count | Meaning |
|---|---:|---|
| `user-correction` User correction | 4 | A human prompt that follows agent work and is worded as a correction of it ("No, …", "I told you …", "revert that", "still failing", "you broke …"), corroborated by behaviour: the agent had changed files, and the human names one of them or the agent then changes one of them again. Wording alone, or two weaker cues together, is only a probable correction. |
| `tool-rejected` Tool call declined | 1 | The human declined a tool call the agent proposed. Declining a plan or a question is not counted. |
| `user-interrupt` User interrupt | 1 | The human interrupted the agent after it had made at least one tool call in the turn. The interrupt that accompanies a declined tool call is not counted again. |
| `edit-miss` Edit miss | 6 | An edit failed because the text to replace was not found in the file, or matched several places. |
| `edit-revert` Edit reverted | 2 | An edit exactly undid an earlier successful edit to the same file in the same session, or a write restored an earlier version of the file. Taking out lines that were only added and then run successfully (a debug print) is not counted. |
| `command-failure` Command failure | 9 | A shell command failed: it exited non-zero or, where the exit status belongs to a pipe or a later step, its own output says so. Setup and invocation errors (command not found, missing module, bad arguments, …) and environment failures (a service that is not running, missing credentials, an unset variable) count fully; ordinary failures, such as a test that fails, count at 0.3. A red run followed by an edit and a green run of the same job is dropped; one that passed only after a setup step, with no file edited, is kept in full. Read-only inspection commands (ls, grep, …) are ignored. |
| `edit-thrash` Edit thrash | 1 | One file received 6 or more successful edits within a single turn in which something also went wrong: an edit miss, a stale edit or a reverted edit on that file, or a failed command or retry loop in the turn. Not recorded next to a no-convergence event of the same turn that names the file. |
| `stale-edit` Stale edit | 1 | An edit or write was refused because the file had not been read first, or changed on disk after it was read. |
| `probable-correction` Probable correction | 1 | A human prompt that is worded like a correction but is not corroborated by behaviour (the agent had changed nothing, or the human names no changed file and the agent did not go back to what it had changed), or that only contains two or more weaker cues. It may be an ordinary follow-up request. |
| `path-miss` Path miss | 1 | The agent tried to read or edit a path that does not exist. A read of a missing file that the agent then creates in the same session is not recorded. |
| `search-miss` Search miss | 6 | A Grep or Glob search returned nothing. A search that only confirms a name is free before the agent creates it is not recorded. |
| `read-loop` Read loop | 1 | The same range of a file was read 3 or more times in one session with no successful edit to it in between. |

<sub>Generated by agent-friction 0.1.0 at 2026-11-13T09:00:00Z.</sub>
