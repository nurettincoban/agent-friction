# Agent friction map: shopfront

6 agent sessions analysed · 35 friction events · ~28m lost · 5 human corrections · 6 files affected

## What to fix first

1. `src/shopfront/api/schema_gen.py`: **Generated file: point agents at its source** This file looks generated (judging by a "code generated" marker), yet agents ran into trouble editing it directly 7 times in 4 sessions. Say in AGENTS.md which source to change and which command regenerates it, and consider denying direct edits to it.
2. `pytest`: **Tell agents to use `make test`** `pytest` failed 7 times in 4 sessions; 7 times the agent then got the same job done with `make test`, and `pytest` did not work later in those sessions. Add a line to AGENTS.md such as: "use `make test`, not `pytest`".
3. `src/shopfront/checkout/service.py`: **Write down the rule the human keeps repeating** A human corrected the agent here 3 times in 3 sessions. Put the rule in AGENTS.md / CLAUDE.md (or a comment at the top of the file) so it is read before the next attempt. In their words: "It's still failing for me on staging, and you used legacy_charge again in src/shopfront/checkout/service.py. I told you: PaymentClient only." / "No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint."
4. `src/shopfront`: **Point agents to what they are searching for** Agents searched under src/shopfront/ and found nothing 5 times in 2 sessions. They looked for `RefundPolicy` (2x), `**/*policy*.py`, `DiscountPolicy`. If the concept exists under another name or elsewhere, add a pointer in AGENTS.md (or a comment/alias where agents look). If it does not exist, say so.
5. `rm`: **State the boundary** A human declined the agent's action once in 1 session. State the boundary explicitly (what must not be touched or run, and what to do instead) in AGENTS.md, or encode it as a permission rule. Reasons given: "never delete existing migrations, add a new revision on top"

## Files where agents struggle

| # | File | Score | Sessions | Events | Est. time lost | Top signals | Confidence |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | `src/shopfront/api/schema_gen.py` | 56.8 | 4 | 12 | 11m | `edit-miss`×6, `user-correction`×3, `read-loop`×1 | high |
| 2 | `src/shopfront/checkout/service.py` | 39.8 | 3 | 6 | 19m | `user-correction`×3, `edit-revert`×2, `edit-thrash`×1 | high |
| 3 | `tests/test_refunds.py` | 15.0 | 1 | 2 | 5m | `command-failure`×1, `user-correction`×1 | low |
| 4 | `src/shopfront/payments/refunds.py` | 11.4 | 1 | 1 | 18s | `path-miss`×1 | low |
| 5 | `src/shopfront/payments/client.py` | 11.1 | 1 | 1 | 5m | `user-correction`×1 | low |
| 6 | `api/openapi.yaml` | 9.7 | 1 | 1 | 24s | `search-miss`×1 | low |

## Commands that keep failing

| # | Command | Score | Sessions | Events | Est. time lost | Top signals | Confidence |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | `pytest` | 50.5 | 4 | 8 | 7m | `command-failure`×7, `retry-loop`×1 | high |
| 2 | `rm` | 16.3 | 1 | 1 | 42s | `tool-rejected`×1 | low |
| 3 | `curl` | 15.2 | 1 | 1 | 12s | `command-failure`×1 | low |
| 4 | `python -m pytest` | 15.2 | 1 | 1 | 30s | `command-failure`×1 | low |

## Directories where agents search in vain

| # | Directory | Score | Sessions | Events | Est. time lost | Top signals | Confidence |
|---:|---|---:|---:|---:|---:|---|---|
| 1 | `src/shopfront` | 29.5 | 2 | 5 | 1m | `search-miss`×5 | medium |

## Evidence

<details>
<summary><code>src/shopfront/api/schema_gen.py</code> · score 56.8 · 4 sessions</summary>

12 friction events in all 4 sessions that touched it (6 edit misses, 3 user corrections, 1 read loop, 2 more signal types); high confidence because it recurs across sessions with several kinds of evidence.

**Recommendations**

- **Generated file: point agents at its source** This file looks generated (judging by a "code generated" marker), yet agents ran into trouble editing it directly 7 times in 4 sessions. Say in AGENTS.md which source to change and which command regenerates it, and consider denying direct edits to it.
- **Large file: split it or add unique anchors** 6 edits failed to locate the target in this 1031-line file (2 matched several places, 4 matched nothing) across 4 sessions. Split it into smaller modules, or give repeated blocks unique nearby text (section comments, distinct names) so an edit can be targeted.
- **Write down the rule the human keeps repeating** A human corrected the agent here 3 times in 3 sessions. Put the rule in AGENTS.md / CLAUDE.md (or a comment at the top of the file) so it is read before the next attempt. In their words: "No, stop hand-editing src/shopfront/api/schema_gen.py. It is generated: change api/openapi.yaml and run make codegen." / "That's wrong, schema_gen.py is generated. Revert that, edit api/openapi.yaml."

**Evidence**

- `2026-11-03 09:01` session `4132e18c` · Stale edit: Edit on src/shopfront/api/schema_gen.py refused: the file changed on disk after it was read
  > File has been modified since read, either by the user or by a linter. Read it again before attempting to write it.
- `2026-11-03 09:04` session `4132e18c` · User interrupt: The user interrupted the agent while it was working on src/shopfront/api/schema_gen.py
- `2026-11-05 16:06` session `aider-20` · User correction: User correction (confidence 1; matched "that's wrong", "revert/undo"); names src/shopfront/api/schema_gen.py
  > That's wrong, schema_gen.py is generated. Revert that, edit api/openapi.yaml.
- `2026-11-12 11:00` session `fb4e20bf` · Edit miss: Edit on src/shopfront/api/schema_gen.py failed: the text to replace was not found
  > String to replace not found in file. String: API_VERSION = "2.3"
- `2026-11-12 11:01` session `fb4e20bf` · Read loop: src/shopfront/api/schema_gen.py was read 3 times with no edit in between

</details>

<details>
<summary><code>src/shopfront/checkout/service.py</code> · score 39.8 · 3 sessions</summary>

6 friction events in all 3 sessions that touched it (3 user corrections, 2 reverted edits, 1 edit thrash episode); high confidence because it recurs across sessions with several kinds of evidence.

**Recommendations**

- **Write down the rule the human keeps repeating** A human corrected the agent here 3 times in 3 sessions. Put the rule in AGENTS.md / CLAUDE.md (or a comment at the top of the file) so it is read before the next attempt. In their words: "It's still failing for me on staging, and you used legacy_charge again in src/shopfront/checkout/service.py. I told you: PaymentClient only." / "No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint."
- **Give agents a fast way to check changes to this file** Edits here did not converge: 1 thrash episode and 2 reverted edits in 1 session. Document the quickest command that validates this file (a single test, a type check) so agents can verify instead of iterating blind.

**Evidence**

- `2026-10-29 14:11` session `9f6ff626` · User correction: User correction (confidence 1; matched "no, …", "I told you"); after edits to src/shopfront/checkout/service.py, tests/test_refunds.py, src/shopfront/payments/client.py
  > No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint.
- `2026-11-09 15:02` session `8d986baa` · Edit reverted: Edit on src/shopfront/checkout/service.py undid an earlier edit from the same session
- `2026-11-09 15:03` session `8d986baa` · Edit thrash: 7 successful edits to src/shopfront/checkout/service.py within one user turn
- `2026-11-09 15:14` session `8d986baa` · User correction: User correction (confidence 1; matched "still failing", "I told you"); names src/shopfront/checkout/service.py
  > It's still failing for me on staging, and you used legacy_charge again in src/shopfront/checkout/service.py. I told you: PaymentClient only.
- `2026-11-09 15:15` session `8d986baa` · Edit reverted: Edit on src/shopfront/checkout/service.py undid an earlier edit from the same session

</details>

<details>
<summary><code>tests/test_refunds.py</code> · score 15.0 · 1 session</summary>

2 friction events in the only session that touched it (1 command failure, 1 user correction); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-10-29 14:04` session `9f6ff626` · Command failure: `pytest` failed (exit 1; ordinary failure)
  > $ pytest tests/test_refunds.py -x Exit code 1 tests/conftest.py:11: in engine     return make_engine(os.environ['DATABASE_URL']) E   KeyError: 'DATABASE_URL' ERROR tests/test_checkout.py - KeyError: 'DATABASE_URL'
- `2026-10-29 14:11` session `9f6ff626` · User correction: User correction (confidence 1; matched "no, …", "I told you"); after edits to src/shopfront/checkout/service.py, tests/test_refunds.py, src/shopfront/payments/client.py
  > No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint.

</details>

<details>
<summary><code>src/shopfront/payments/refunds.py</code> · score 11.4 · 1 session</summary>

1 friction event in the only session that touched it (1 path miss); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-10-29 14:00` session `9f6ff626` · Path miss: Read targeted src/shopfront/payments/refunds.py, which does not exist
  > File does not exist.

</details>

<details>
<summary><code>src/shopfront/payments/client.py</code> · score 11.1 · 1 session</summary>

1 friction event in 1 of the 2 sessions that touched it (1 user correction); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-10-29 14:11` session `9f6ff626` · User correction: User correction (confidence 1; matched "no, …", "I told you"); after edits to src/shopfront/checkout/service.py, tests/test_refunds.py, src/shopfront/payments/client.py
  > No, I said use PaymentClient, not the legacy module. src/shopfront/payments/legacy.py is deprecated and is being deleted next sprint.

</details>

<details>
<summary><code>api/openapi.yaml</code> · score 9.7 · 1 session</summary>

1 friction event in 1 of the 2 sessions that touched it (1 search miss); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-11-12 11:01` session `fb4e20bf` · Search miss: Grep search in api/openapi.yaml returned nothing
  > pattern: API_VERSION

</details>

<details>
<summary><code>pytest</code> · score 50.5 · 4 sessions</summary>

8 friction events in all 4 sessions that touched it (7 command failures, 1 retry loop); high confidence because it recurs across sessions with several kinds of evidence.

**Recommendations**

- **Tell agents to use `make test`** `pytest` failed 7 times in 4 sessions; 7 times the agent then got the same job done with `make test`, and `pytest` did not work later in those sessions. Add a line to AGENTS.md such as: "use `make test`, not `pytest`".
- **Make this failure explain itself** Agents re-ran `pytest` up to 3 times in a row without success (1 retry loop in 1 session). The output did not lead them to a fix: note the known cause in AGENTS.md, or make the command fail fast with a message that says what to do.

**Evidence**

- `2026-10-26 10:03` session `e65615b3` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest tests/test_checkout.py -q Exit code 4 ImportError while loading conftes … /shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'
- `2026-10-29 14:03` session `9f6ff626` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest tests/test_refunds.py -x Exit code 4 ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'
- `2026-10-29 14:04` session `9f6ff626` · Retry loop: `pytest` failed 3 times in a row
- `2026-11-05 16:01` session `aider-20` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest -q ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'
- `2026-11-09 15:01` session `8d986baa` · Command failure: `pytest` failed (exit 4; setup/invocation error)
  > $ pytest tests/test_checkout.py Exit code 4 ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'

</details>

<details>
<summary><code>rm</code> · score 16.3 · 1 session</summary>

1 friction event in the only session that touched it (1 declined tool call); low confidence because there is too little independent evidence so far.

**Recommendations**

- **State the boundary** A human declined the agent's action once in 1 session. State the boundary explicitly (what must not be touched or run, and what to do instead) in AGENTS.md, or encode it as a permission rule. Reasons given: "never delete existing migrations, add a new revision on top"

**Evidence**

- `2026-11-03 09:03` session `4132e18c` · Tool call declined: The user declined Bash on `rm` and said why
  > $ rm -rf migrations/versions &amp;&amp; alembic revision --autogenerate -m currency user said: never delete existing migrations, add a new revision on top

</details>

<details>
<summary><code>curl</code> · score 15.2 · 1 session</summary>

1 friction event in the only session that touched it (1 command failure); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-11-09 15:15` session `8d986baa` · Command failure: `curl` failed (exit 7; setup/invocation error)
  > $ curl -s -H 'Authorization: Bearer [REDACTED:token]' https://staging.shopfront.example/api/health Exit code 7 curl: (7) Failed to connect to staging.shopfront.example port 443: Connection refused

</details>

<details>
<summary><code>python -m pytest</code> · score 15.2 · 1 session</summary>

1 friction event in the only session that touched it (1 command failure); low confidence because there is too little independent evidence so far.

**Evidence**

- `2026-11-12 11:02` session `fb4e20bf` · Command failure: `python -m pytest` failed (exit 4; setup/invocation error)
  > $ python -m pytest -q Exit code 4 ImportError while loading conftest '~/shopfront/tests/conftest.py'. tests/conftest.py:3: in &lt;module&gt;     from shopfront.db import make_engine E   ModuleNotFoundError: No module named 'shopfront'

</details>

<details>
<summary><code>src/shopfront</code> · score 29.5 · 2 sessions</summary>

5 friction events in all 2 sessions that touched it (5 search misses); medium confidence because it recurs across sessions.

**Recommendations**

- **Point agents to what they are searching for** Agents searched under src/shopfront/ and found nothing 5 times in 2 sessions. They looked for `RefundPolicy` (2x), `**/*policy*.py`, `DiscountPolicy`. If the concept exists under another name or elsewhere, add a pointer in AGENTS.md (or a comment/alias where agents look). If it does not exist, say so.

**Evidence**

- `2026-10-29 14:00` session `9f6ff626` · Search miss: Grep search in src/shopfront returned nothing
  > pattern: RefundPolicy
- `2026-10-29 14:00` session `9f6ff626` · Search miss: Glob search in src/shopfront returned nothing
  > pattern: src/shopfront/**/refund*.py
- `2026-11-09 15:00` session `8d986baa` · Search miss: Grep search in src/shopfront returned nothing
  > pattern: DiscountPolicy
- `2026-11-09 15:00` session `8d986baa` · Search miss: Grep search in src/shopfront returned nothing
  > pattern: RefundPolicy
- `2026-11-09 15:00` session `8d986baa` · Search miss: Glob search in src/shopfront returned nothing
  > pattern: **/*policy*.py

</details>

## Signals

| Signal | Count | Meaning |
|---|---:|---|
| `user-correction` User correction | 5 | A human prompt that follows agent work and opens with an unambiguous correction ("No, …", "I told you …", "revert that", "still failing", "you broke …"), or with two weaker cues together. Single weak cues and friendly follow-ups do not count. |
| `retry-loop` Retry loop | 1 | The same normalized shell command failed 3 or more times in a row in one session without succeeding in between. |
| `tool-rejected` Tool call declined | 1 | The human declined a tool call the agent proposed. |
| `user-interrupt` User interrupt | 1 | The human interrupted the agent mid-turn (not counted when it merely accompanies a declined tool call). |
| `edit-miss` Edit miss | 6 | An edit failed because the text to replace was not found in the file, or matched several places. |
| `edit-revert` Edit reverted | 2 | An edit exactly undid an earlier successful edit to the same file in the same session (or a write restored an earlier version of the file). |
| `command-failure` Command failure | 9 | A shell command exited non-zero. Failures that look like setup or invocation problems (command not found, missing module, bad arguments, …) count fully; other failures, such as a test that legitimately fails, count at 0.3. Read-only inspection commands (ls, grep, …) are ignored. |
| `edit-thrash` Edit thrash | 1 | One file received 6 or more successful edits within a single user turn. |
| `stale-edit` Stale edit | 1 | An edit or write was refused because the file had not been read first, or changed on disk after it was read. |
| `path-miss` Path miss | 1 | The agent tried to read or edit a path that does not exist. |
| `search-miss` Search miss | 6 | A Grep or Glob search returned nothing. |
| `read-loop` Read loop | 1 | The same range of a file was read 3 or more times in one session with no edit to it in between. |

<sub>Generated by agent-friction 0.1.0 at 2026-11-13T09:00:00Z.</sub>
