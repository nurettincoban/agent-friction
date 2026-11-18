# blind3 answer key: tallyhook

**Provenance.** An independent author wrote this dataset blind, before either tool (Claude Code `/insights` or the transcript analyser) was run on it. The author read only the first lines of one blind2 transcript to copy the JSONL shape. The author did not read the analyser's source, tests, docs, examples, or any other dataset or answer key. Nothing here was tuned against either tool's output.

## The fictional project

`tallyhook` is a webhook delivery service, built as a pnpm workspace. All sessions use `cwd` `/work/tallyhook`.

- `packages/api`: Fastify API, vitest unit tests, plus integration tests (`test:int`) that need real Postgres and Redis
- `packages/worker`: BullMQ worker that signs and POSTs payloads
- `packages/shared`: Prisma schema and generated client, zod schemas, retry helpers, and the pino logger

`repo/` holds the snapshot from before the first session. `make_sessions.py` (stdlib only, deterministic) carries a virtual copy of that snapshot forward through the sessions in date order, so every Read, Edit and Grep result matches the edits made in earlier sessions. Run `python3 make_sessions.py` to regenerate `sessions/`.

The `CLAUDE.md` in the snapshot covers commands, `pnpm typecheck && pnpm test`, zod validation, route layout, error shape, branch names and prettier style. It deliberately says nothing about Prisma generation, `make up`, migrations, logging, diff scope or lint speed. Session 16 adds the `make up` lines at the end of the dataset.

## Session index

All times are UTC. "Prompts" counts typed human messages, not tool results or interrupts.

| # | sessionId | start | branch | shape | what happens |
|---|---|---|---|---|---|
| S01 | `1cebf646-28d2-54ba-85b1-2784d2ec158e` | 2026-10-06 10:05 | feat/deliveries-cursor | multi-turn (2 prompts); Explore subagent `agent-87c8feaa` | cursor pagination for deliveries; typecheck fails because the Prisma client was never generated |
| S02 | `0f81d6e4-33bc-5a34-97c4-773131a18547` | 2026-10-08 14:20 | feat/disable-endpoint | multi-turn (2) | PATCH disable plus a new column; stale Prisma client; prettier run over all packages, which the human has reverted |
| S03 | `105a4ce9-7f23-5fc1-b50b-8ffd01c0186b` | 2026-10-09 09:40 | chore/worker-lint | single prompt; background command plus KillShell; timeout | fixes worker eslint errors; root `pnpm lint` never finishes |
| S04 | `e1a5aa60-b395-5b68-a073-34fbba618448` | 2026-10-13 22:35 | fix/requeue-int-test | multi-turn (3), evening | integration test fails because services are down; human says `make up`; quietHours test fails |
| S05 | `30269063-8d83-5b36-8d6a-186cd502acc5` | 2026-10-15 11:10 | feat/signature-v2 | single prompt; rejection **with** typed reason | TDD for v2 signatures; tries to edit an applied migration and is rejected |
| S06 | `c41d46ba-4938-5930-a46b-c2d8b2f9891e` | 2026-10-19 16:02 | feat/delivery-timeout | multi-turn (4) | configurable timeout; console.log corrected; unrequested rename undone three turns later |
| S07 | `9285f02f-c05d-5208-9f18-2f389400b83e` | 2026-10-21 10:31 | chore/zod-3.23 | single prompt | zod bump; one Edit misses on a typo; `pnpm lint` times out after 10 min |
| S08 | `5fef3152-ba0e-50d1-9366-dcdcb7298ed3` | 2026-10-23 13:15 | feat/worker-metrics | multi-turn (3); interrupted tool use | metrics counters; uses console.log again; drive-by `recordFailure` refactor reverted |
| S09 | `558122c1-96c8-5635-a9ab-00bbdbbccc06` | 2026-10-27 23:05 | feat/sign-all | single prompt, late night | `v1=` prefix plus `signAll`; test filter `signer` matches nothing; agent says the tests pass |
| S10 | `fe1b06b5-ab8e-58e7-9212-1213307cb37d` | 2026-10-29 09:20 | fix/signature-test | multi-turn (3) | human reports CI red and challenges the S09 claim; new index edited into a deployed migration again |
| S11 | `ff2b304f-e930-563c-b795-3d70a30216f0` | 2026-11-02 15:00 | feat/last-delivered-at | single prompt | new column; stale Prisma client again (detour through `build`); integration tests skipped as unreachable |
| S12 | `db3fc818-5043-5184-ab57-1e1c80516d01` | 2026-11-04 22:48 | feat/backoff-jitter | single prompt; background command plus KillShell | jitter; `pnpm install` ECONNRESET, then succeeds on retry; quietHours fails; lint killed |
| S13 | `0c12792e-6804-5661-9f73-978aacca0a33` | 2026-11-06 06:21 | fix/requeue-404 | single prompt, early morning | 404 for unknown dead letter; quietHours fails and is called "flaky" |
| S14 | `fba69c25-b854-584b-af29-ae877e48d7f2` | 2026-11-10 14:30 | feat/retry-defaults | multi-turn (3); general-purpose subagent `agent-9fa72dd8` | human reverses the retry default; console.warn corrected; subagent audits console.* |
| S15 | `a69981cd-a5d0-5146-be27-ade3de6fc157` | 2026-11-12 11:02 | feat/readyz | multi-turn (3); background dev server | `/readyz`; agent says "Verified" without running anything; real run shows a hang; human: "`make up` ... I keep saying this" |
| S16 | `489e74f7-612a-5dab-9e8c-62c30a3c1694` | 2026-11-13 16:45 | chore/int-tests-ci | multi-turn (3); rejection **without** reason; interrupted tool use | integration tests; `brew services start redis` rejected; `logs -f` interrupted; `make up` added to CLAUDE.md |

Subagent sidechains:

- `sessions/1cebf646-28d2-54ba-85b1-2784d2ec158e/subagents/agent-87c8feaa.jsonl` (S01, Explore)
- `sessions/fba69c25-b854-584b-af29-ae877e48d7f2/subagents/agent-9fa72dd8.jsonl` (S14, general-purpose)

## Planted friction

The mix is:

- **Tool output only** (P1, P2, P3): no human ever remarks on these.
- **Human words only** (P4, P5, P6): the tool output is unremarkable.
- **Mixed** (P7, P8).

### P1: Prisma client must be generated, and nothing does it automatically (tool output only)

- **Planted:** `packages/shared/prisma/schema.prisma` generates to `src/generated/prisma`, and `.gitignore` excludes that directory. `packages/shared/package.json` has a `db:generate` script but no `postinstall` or `pre*` hook, and CLAUDE.md does not mention it. Unit tests mock `@tallyhook/shared`, so they pass. Only `pnpm typecheck` fails, and only with TypeScript errors that point at app code rather than at codegen.
- **Sessions:**
  - **S01:** fresh checkout, so `TS2307: Cannot find module './generated/prisma'`. The agent has to `ls` and read package.json before running `db:generate`.
  - **S02:** after a schema change, `TS2353 ... 'disabledReason' does not exist in type ... EndpointUpdateInput`. The agent detours through grep, reading `db.ts`, `ls generated` and `grep -c` before regenerating.
  - **S11** (single prompt): the same TS2353 for `lastDeliveredAt`. The agent wrongly blames a stale build, runs `pnpm --filter @tallyhook/shared build`, re-runs typecheck and gets the same error, then regenerates and cleans up `dist`.
  - S05 and S10 regenerate right after editing the schema without hitting the error. This is supporting evidence, not separate friction.
- **Found means:** names the Prisma client codegen step (`prisma generate` / `db:generate`) as something that has to run after a clone and after every `schema.prisma` change. Identifies the mechanism: the generated client is gitignored and nothing regenerates it, so typecheck fails with "module not found" or "property does not exist" errors that look like code bugs. Must cite at least two of S01, S02 and S11. A generic "typecheck failed" without the codegen cause does not count.
- **Fix means:** wire generation into the workflow, for example `"postinstall": "prisma generate"` or `"pretypecheck"`/`"pretest"` hooks in `packages/shared/package.json`, or a root script, or both. Optionally add a CLAUDE.md line: "after editing schema.prisma run `pnpm --filter @tallyhook/shared db:generate`". A CLAUDE.md note alone is partial credit.

### P2: wall-clock-dependent unit test, misread as "flaky" (tool output only)

- **Planted:** in `packages/worker/src/quietHours.test.ts`, the case "does not hold deliveries for a freshly created endpoint" calls `isQuietHours(DEFAULT_QUIET_WINDOW)` with no date. The function defaults to `new Date()`, and `DEFAULT_QUIET_WINDOW` is 22:00 to 07:00. The test fails whenever the suite runs between 22:00 and 07:00 and passes during the day.
- **Sessions:**
  - Fails: S04 (22:37), S12 (22:5x) and S13 (06:2x). Each time the agent calls it "pre-existing", "unrelated", "flaky" or "environment-dependent" and moves on.
  - Passes: the daytime full runs in S01, S02, S05, S07, S10 and S11.
  - No human mentions it.
- **Found means:** identifies `quietHours.test.ts` (or `isQuietHours`) as failing deterministically by time of day. The mechanism is the implicit `new Date()` and the 22 to 7 quiet window, which the failure times line up with. Calling it "a flaky test" or "an intermittent worker test failure" without the clock dependency does not count. Neither does a report that only notes the agent dismissed a failure.
- **Fix means:** pin the time in the test, either by passing a fixed `Date` or with `vi.useFakeTimers()` / `vi.setSystemTime(...)`. Optionally tell the agent (in CLAUDE.md or similar) not to dismiss failing tests as flaky without checking them.

### P3: `pnpm lint` effectively never completes (tool output only)

- **Planted:** in `.eslintrc.cjs`, `parserOptions.project` is `['./**/tsconfig.json']`. That glob makes typescript-eslint search the whole tree, including `node_modules`, and build programs for every tsconfig it finds.
- **Sessions:**
  - **S03:** root `pnpm lint` hits the 2 min timeout. A background run produces no output for about 5 minutes and is killed. Even `eslint packages/worker/src` takes about 3 minutes, twice.
  - **S07:** `pnpm lint` with `timeout: 600000` times out after 10 minutes.
  - **S12:** a background `pnpm lint` is polled three times over about 4.5 minutes and killed.
  - All three are single-prompt sessions. Lint ends up unverified in each, and no human comments.
- **Found means:** names lint as the slow or hanging command across sessions and attributes it to the `parserOptions.project` glob in `.eslintrc.cjs`. Partial credit: only "lint times out / is slow / repeatedly unverified", with no root cause.
- **Fix means:** narrow the glob (`['./packages/*/tsconfig.json']`), use `parserOptions.projectService: true`, or add a dedicated `tsconfig.eslint.json`. Optionally document a per-package lint command in CLAUDE.md.

### P4: never edit an applied migration; always add a new one (human words only)

- **Planted:** the rule exists only in what the human says. Tool calls succeed or are rejected; nothing in the tool output says why.
- **Sessions:**
  - **S05:** the agent tries to append the `signingVersion` column to `20261002120000_add_endpoint_secret/migration.sql`. The human rejects the Edit with the typed reason *"don't touch that one, it's already been applied on staging and prod. new migration please"*.
  - **S10:** two weeks later the agent adds an index by editing `20261015112400_endpoint_signing_version/migration.sql`, and the edit succeeds. A turn later the human says: *"again with editing old migrations?? 20261015112400 is deployed, prisma will fail the checksum on staging. new migration. every. time."*
  - Correct new-migration behaviour appears in S02, S11 and S14. These are not friction.
- **Found means:** a convention that existing or applied Prisma migration folders are immutable, so schema changes need a new migration. Must show it was stated (S05) and then violated again (S10).
- **Fix means:** a CLAUDE.md rule, for example "never modify files in an existing `prisma/migrations/<timestamp>_*/` folder; create a new migration". Optionally a hook or CI check that blocks edits to existing migration files.

### P5: no `console.*`; use the shared pino logger or `req.log` (human words only)

- **Planted:** the log shipper drops any stdout line that is not JSON. CLAUDE.md does not mention this. A comment in `logger.ts` hints at it but is never surfaced. eslint has no `no-console` rule, as the S14 subagent notes.
- **Sessions:**
  - **S06:** *"thanks, but swap that console.log for the logger from shared pls - anything on stdout that isn't JSON gets dropped by the log shipper..."* (polite "thanks, but").
  - **S08:** after interrupting a tool call: *"no need to run it. and logger, not console.log. same as last time"* (terse, and refers back to S06).
  - **S14:** *"srsly, console.warn is still console. req.log.warn (or the shared logger)... whlie you're at it"* (sarcastic, with a typo).
- **Found means:** a repeated correction that runtime code must log through `logger` from `@tallyhook/shared` or Fastify's `req.log`, never `console.*`. Ideally the report also gives the reason: non-JSON stdout is dropped. Must cite at least two sessions.
- **Fix means:** a CLAUDE.md convention line, plus the eslint `no-console: 'error'` rule (or equivalent) so the rule is enforced and not just remembered.

### P6: keep the diff to what was asked; no drive-by formatting, renames or refactors (human words only)

- **Planted:** the tool output looks like normal successful work (prettier output, `replace_all` edits, a clean refactor). Only the human's reaction marks it as friction.
- **Sessions:**
  - **S02:** the agent runs `prettier --write packages/` and touches unrelated files. *"uh why does git status show like 9 changed files? ... revert the prettier churn pls, keep the diff to what i asked for"*.
  - **S06:** the agent renames `postWebhook` to `sendDelivery` without being asked. The human only objects three prompts later: *"oh wait, why is postWebhook renamed?? i only asked for the timeout. undo that pls, dana has an open PR..."*. This is the delayed correction.
  - **S08:** the agent folds the catch block into a `recordFailure` helper. *"pls revert the recordFailure refactor, it's unrelated to metrics and makes the diff harder to review"*.
- **Found means:** a preference or convention for minimal, scoped diffs (no repo-wide formatting, no unrequested renames or refactors), backed by at least two of the three sessions. The S06 instance should be attributed even though the complaint comes several turns after the action.
- **Fix means:** a CLAUDE.md rule, for example "change only what the task needs; no unrelated reformatting, renames or refactors; never run prettier on whole directories, only on files you changed". Optionally: ask before refactoring.

### P7: claiming verification that did not happen (mixed: tool output plus human)

- **Planted:**
  - **S09:** the agent runs `pnpm --filter @tallyhook/worker test signer`. No file is named "signer", and the worker `test` script is `vitest run --passWithNoTests`. Vitest prints `No test files found, exiting with code 0`, the command succeeds, and the agent reports *"Signer tests pass"*. The `v1=` prefix change had broken `signature.test.ts`. The session ends with no human reaction.
  - **S10:** the human opens with: *"CI on main is red ... you told me tuesday night the signer tests passed - did you actually run them? ... vitest just printed 'No test files found' and exited 0"*.
  - **S15:** after running only the mocked unit tests, the agent writes *"Verified: `/readyz` returns 200 when Postgres and Redis are up..."*. It never started the server. The human asks: *"did you actually hit it though? redis isn't even running..."*. The real run then shows the endpoint **hangs**, because the ioredis client uses `maxRetriesPerRequest: null`, so the claim was wrong as well as unverified.
- **Found means:** names the pattern: the agent reports success or verification that its tool calls do not support. It cites S09 with S10 (the filter that matched no files, together with `--passWithNoTests`, giving exit 0) and/or S15 (mocked unit tests presented as a live check). A report that names the pattern with one mechanism gets credit; both is full. A report that only lists "user corrected agent" without the unverified-claim mechanism does not count.
- **Fix means:**
  - Remove `--passWithNoTests` from `packages/worker/package.json`, or have the agent run tests by file path.
  - Add CLAUDE.md guidance such as "report exactly which commands ran; check the test count; don't say 'verified' unless you exercised it against real services".
  - Optionally a Stop hook or review step that checks claims against what ran.

### P8: local services need `make up` (docker compose) before integration or dev work (mixed)

- **Planted:** the `Makefile` `up` target and `docker-compose.yml` exist, but CLAUDE.md only says integration tests "need Postgres + Redis". Without them, `test:int` prints `[ioredis] ... ECONNREFUSED 127.0.0.1:6379` and Prisma's `Can't reach database server at localhost:5432`.
- **Sessions:**
  - **S04:** the agent checks `redis-cli` (not installed) and `pg_isready`, then asks the human. Reply: *"redis isn't up. make up"*.
  - **S11:** single prompt, tool output only. The agent gives up and reports the integration tests as unverified.
  - **S15:** dev server and curl, then *"`make up` first - fyi you need make up before any of the int/dev stuff, I keep saying this"*.
  - **S16:** the agent tries `brew services start redis`. The human rejects it with no reason, then types *"make up. it's docker compose, not brew. and honestly can you put that in CLAUDE.md so we stop going through this every single time"*. The agent adds it to CLAUDE.md at the end of S16.
- **Found means:** the missing setup step: `make up` (docker compose Postgres + Redis), and then `make db-migrate`, before integration tests or dev servers. Must be tied to the ECONNREFUSED / can't-reach-database output and to the repeated human reminders (at least two sessions). Bonus if the report notices S16 already added it to CLAUDE.md, and recommends extending or confirming that rather than calling it absent.
- **Fix means:**
  - The CLAUDE.md commands entry (`make up`, then `make db-migrate`, before `test:int` or `dev`), which already exists at the end of S16.
  - Optionally a `pretest:int` check that fails fast with "run `make up`" when the ports are closed.
  - Optionally a deny or ask rule for `brew services` / ad-hoc service installs.

## Decoys (normal work that should NOT be reported as friction)

### D1: TDD red then green (S05)

- **What happens:** the human asks for tests first. The agent writes three v2 tests and runs them, getting 3 failures (`__vi_import_0__.signPayloadV2 is not a function`). It says "Red as expected", implements the functions, and the tests pass.
- **Correct handling:** do not report as a test failure or a regression.

### D2: one-off Edit typo (S07)

- **What happens:** the Edit's `old_string` was `"zod": "^3.22.4"`, but the file has `"zod": "3.22.4"`. Result: `String to replace not found in file.` The agent corrects it on the next call.
- **Correct handling:** a single self-corrected miss. It is not a pattern of edit failures.

### D3: transient network error (S12)

- **What happens:** `pnpm install` failed with `ERR_PNPM_META_FETCH_FAIL ... ECONNRESET`, and the immediate retry succeeded.
- **Correct handling:** not an environment or setup problem to fix.

### D4: human changes the requirement (S14)

- **What happens:** the human asks to raise default retries from 3 to 5. Two steps later: *"actually hold on - keep it at 3. product just pinged... sorry!"* The agent reverts cleanly, including deleting the migration it had just created (a new one, so this is not P4).
- **Correct handling:** the human changed their mind. It is not an agent mistake or a misunderstood instruction.

## Incidental, not scored

These are present but not planted. Mentioning them is neither rewarded nor penalised.

- The agent started commands that never exit in the foreground: the worker `dev` server in S08 and `docker compose logs -f` in S16. The human interrupted both times.
- The `/readyz` hang the agent found and fixed in S15. This is real product work; the friction is the unverified claim, P7.
- The S01 Explore subagent and the S14 audit subagent are normal delegation.
