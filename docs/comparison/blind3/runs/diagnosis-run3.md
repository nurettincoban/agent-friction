# Diagnosis: tallyhook

From `.agent-friction/evidence.md` (16 sessions, S01-S16, 2026-10-06 to 2026-11-13), the
transcripts behind it, and the repository as checked out now. `node_modules` is not installed
in this checkout, so nothing below was re-run; mechanisms were confirmed by reading the files
and the recorded tool output.

## 1. Agents do not know Postgres and Redis come from `make up`

**Seen in:** S04, S11, S15, S16 (4 of 16 sessions) · `pnpm --filter @tallyhook/api test:int` failed in S04, S11 and S16; the human had to say "make up" in S04, S15 and S16
**What happens:** `test:int` (`packages/api/package.json:11`) runs `test/requeue.int.test.ts`, which talks to the real Postgres and Redis from `docker-compose.yml`. Nothing starts them: the only way is `make up` (`Makefile:4-5`), followed by `make db-migrate` (`Makefile:10-11`) on a fresh database. With them down the run burns ~25s and exits 1. Agents then guess: `redis-cli ping` (exit 127) and `pg_isready` in S04, `brew services start redis` in S16 (declined), or give up and report "unverified" (S11).
**Evidence:**
- `[ioredis] Unhandled error event: Error: connect ECONNREFUSED 127.0.0.1:6379` and `` Can't reach database server at `localhost:5432` `` in S04, S11, S16
- S04: "redis isn't up. make up"
- S15: "yes. `make up` first - fyi you need make up before any of the int/dev stuff, I keep saying this"
- S16: "make up. it's docker compose, not brew. and honestly can you put that in CLAUDE.md so we stop going through this every single time"
**Why it keeps happening:** Not written down. `CLAUDE.md:14` says only "(need Postgres + Redis)"; it never names `make up`, `make db-migrate` or docker compose. The agent in S16 did add a line to CLAUDE.md, but the CLAUDE.md in this checkout does not have it.
**Fix:**
- `CLAUDE.md`, replace line 14 with:
  ```markdown
  - `make up` - start local Postgres + Redis (docker compose). Required before `test:int`, `pnpm dev`, or anything else that talks to the DB or the queue. Never start them another way (no brew, no local installs).
  - `make db-migrate` - apply migrations; run it after `make up` on a fresh database.
  - `pnpm --filter @tallyhook/api test:int` - integration tests; run `make up && make db-migrate` first.
  ```
- Optional, removes the ordering problem: a `Makefile` target
  ```make
  test-int: up db-migrate
  	pnpm --filter @tallyhook/api test:int
  ```
  (add `test-int` to `.PHONY`; `docker compose up -d` does not wait for Postgres to accept connections, so add `--wait` plus healthchecks in `docker-compose.yml` if the first run races.)
**Confidence:** high. Same error lines in three sessions, `test:int` passed right after `make up` + `make db-migrate` in S04 and S16, and the human asked for exactly this line.

## 2. `pnpm typecheck` fails until someone runs `prisma generate`

**Seen in:** S01, S02, S11 (3 of 16 sessions) · `pnpm typecheck` exit 2 each time (the evidence file counts S01 and S02; S11 is in the transcript at lines 20-37)
**What happens:** `packages/shared/src/db.ts:1` imports the Prisma client from `./generated/prisma`, which is git-ignored (`.gitignore:5`) and produced only by `pnpm --filter @tallyhook/shared db:generate` (`packages/shared/package.json:10`). Nothing runs it: there is no `postinstall`, and the root `typecheck` is just `pnpm -r typecheck` (`package.json:7`). So on a fresh clone the module is missing (S01), and after any edit to `schema.prisma` the generated types are stale and the new column "does not exist" (S02, S11). In S11 the agent first rebuilt `shared` and re-ran typecheck before finding the cause.
**Evidence:**
- S01: `src/db.ts(1,30): error TS2307: Cannot find module './generated/prisma' or its corresponding type declarations.`
- S02 and S11: type errors on `EndpointUpdateInput` for a column that was already in `schema.prisma`; typecheck passed right after `db:generate` in all three sessions.
**Why it keeps happening:** The default command is incomplete, and CLAUDE.md does not mention `db:generate` at all (`CLAUDE.md:12` and `:17` tell agents to run `pnpm typecheck`).
**Fix:**
- `package.json:7`, so the client is always current (generation took ~100ms in the sessions):
  ```diff
  -    "typecheck": "pnpm -r typecheck",
  +    "typecheck": "pnpm --filter @tallyhook/shared db:generate && pnpm -r typecheck",
  ```
- `CLAUDE.md`, under Commands:
  ```markdown
  - `pnpm --filter @tallyhook/shared db:generate` - regenerate the Prisma client (git-ignored, in `packages/shared/src/generated/`). Run it after every change to `schema.prisma`; stale types look like "property does not exist" errors in api/worker.
  ```
**Confidence:** high.

## 3. `quietHours.test.ts` fails every night, and agents wave it off as flaky

**Seen in:** S04, S12, S13 (3 of 16 sessions) · `pnpm test` exit 1 each time
**What happens:** `packages/worker/src/quietHours.test.ts:17` calls `isQuietHours(DEFAULT_QUIET_WINDOW)` with no date, so `quietHours.ts:10` uses `new Date()`. The default window is 22:00-07:00 (`quietHours.ts:8`), so the test fails whenever it runs in that window. The three failures started at 22:35, 22:48 and 06:21 UTC; every daytime run of the same test passed (e.g. S03 09:50, S07 10:32, S11 15:01). Each time the agent reported the required `pnpm test` as red and moved on: "looks like a pre-existing flaky test" (S04), "unrelated to this change" (S12), "looks flaky, so I left it alone" (S13).
**Evidence:** `× isQuietHours > does not hold deliveries for a freshly created endpoint` / `→ expected true to be false // Object.is equality` in S04, S12, S13.
**Why it keeps happening:** The test depends on the wall clock; it is deterministic, not flaky. It also makes `CLAUDE.md:17` ("Run `pnpm typecheck && pnpm test` before saying a change is done") impossible to satisfy for a third of the day.
**Fix:**
- `packages/worker/src/quietHours.test.ts:17`:
  ```diff
  -    expect(isQuietHours(DEFAULT_QUIET_WINDOW)).toBe(false);
  +    expect(isQuietHours(DEFAULT_QUIET_WINDOW, new Date('2026-01-01T12:00:00'))).toBe(false);
  ```
  If the intent really is "a new endpoint is never held", that is a product question about `isQuietHours`, not something this test can assert at 23:00.
**Confidence:** high for the mechanism (code read; failure times all inside the window, pass times all outside). The machine's time zone is not recorded; the UTC timestamps fit.

## 4. `console.*` instead of the shared logger

**Seen in:** S06, S08, S14 (3 of 16 sessions) · corrected by the human each time
**What happens:** Agents add `console.log` / `console.warn` for new log lines (`worker/src/index.ts` in S06 and S08, `api/src/routes/endpoints.ts` in S14). Production logging is pino JSON on stdout (`packages/shared/src/logger.ts:3-7`); the log shipper drops anything that is not JSON.
**Evidence:**
- S06: "swap that console.log for the logger from shared pls - anything on stdout that isn't JSON gets dropped by the log shipper, so that line would just vanish in prod"
- S08: "no need to run it. and logger, not console.log. same as last time"
- S14: "srsly, console.warn is still console. req.log.warn (or the shared logger). can you also grep for any other console.* in api + worker src whlie you're at it"
**Why it keeps happening:** Not written down and not enforced. `CLAUDE.md:19-26` has no logging rule, and `.eslintrc.cjs:16-19` has no `no-console` (the S14 agent grepped for it and found nothing). There is no `console.*` in `packages/` today, so a rule can go in without cleanup.
**Fix:**
- `.eslintrc.cjs`, in `rules`:
  ```diff
  +    'no-console': 'error',
  ```
  (only bites once `pnpm lint` finishes, see problem 6; it does work with the per-directory `pnpm exec eslint` agents fall back to.)
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Logging: never `console.*`. Use `req.log` in API routes and `logger` from `@tallyhook/shared` elsewhere; the log shipper drops any stdout line that is not JSON.
  ```
**Confidence:** high.

## 5. Changes nobody asked for end up in the diff

**Seen in:** S02, S06, S08 (3 of 16 sessions) · the human asked for a revert each time
**What happens:** After finishing the task the agent "tidies": `pnpm exec prettier --write packages/` over the whole tree (S02), renaming `postWebhook` to `sendDelivery` (S06), extracting a `recordFailure` helper (S08). For the prettier case there is a concrete cause in the repo: the tree is not prettier-clean (`prettier --check .` today flags `.eslintrc.cjs`, `packages/api/test/requeue.int.test.ts` and `packages/worker/src/index.ts`; S02 rewrote more), and the only formatting command offered is `"format": "prettier --write ."` (`package.json:10`), so any whole-tree format touches unrelated files.
**Evidence:**
- S02: "uh why does git status show like 9 changed files? i asked for the disable endpoint. revert the prettier churn pls, keep the diff to what i asked for"
- S06: "oh wait, why is postWebhook renamed?? i only asked for the timeout. undo that pls, dana has an open PR touching deliver.ts and this will conflict for no reason"
- S08: "pls revert the recordFailure refactor, it's unrelated to metrics and makes the diff harder to review. just add the record() calls inline"
**Why it keeps happening:** Not written down. `CLAUDE.md:26` gives the prettier settings but not the scope, and nothing says diffs must stay minimal.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Keep the diff to what was asked: no renames, refactors or cleanups of code the task does not need. Suggest them in the summary instead.
  - Format only the files you changed (`pnpm exec prettier --write <files>`). Never run `pnpm format` or prettier over a whole directory.
  ```
- Optional, removes the churn at the source: one human-made commit that runs `pnpm format` on main, so that formatting a touched file never changes unrelated lines.
**Confidence:** high for the convention (three humans' requests, verbatim); medium that the unformatted tree is what triggered S02 (the tree has changed since).

## 6. `pnpm lint` never finishes

**Seen in:** S03, S07, S12 (3 of 16 sessions) · timed out at 2 minutes (S03), at 10 minutes (S07), killed after ~4.5 minutes in the background (S12; set aside by the scan as normal work, checked in the transcript)
**What happens:** `pnpm lint` is `eslint . --ext .ts --max-warnings 0` (`package.json:9`) with type-aware rules over every tsconfig in the workspace (`.eslintrc.cjs:6-8`, `project: ['./**/tsconfig.json']`). It printed nothing in any of the three sessions; linting `packages/worker/src` alone took about 3 minutes in S03. Each session ended with lint reported as unverified, including S07 where the human had asked for "everything still passes incl lint".
**Evidence:** `Command timed out after 2m 0.0s` (S03), `Command timed out after 10m 0.0s` (S07).
**Why it keeps happening:** Unconfirmed. I could not run eslint here (no `node_modules`). The `./**/tsconfig.json` glob is the obvious suspect, but typescript-eslint skips `**/node_modules/**` in project globs by default, so that alone does not explain it. `CLAUDE.md:15` lists `pnpm lint` with no hint that it is slow.
**Fix:**
- Find the cause first, once: `TIMING=1 pnpm exec eslint packages/shared/src --ext .ts --debug` and look at where the time goes (program creation per file is the usual culprit).
- Cheap candidate to try while there, `.eslintrc.cjs:7`:
  ```diff
  -    project: ['./**/tsconfig.json'],
  +    project: ['./packages/*/tsconfig.json'],
  ```
- Until it is fixed, `CLAUDE.md`, replace line 15 with:
  ```markdown
  - `pnpm lint` - currently does not finish within 10 minutes. Lint only the directories you changed, in the background or with a 5-minute timeout: `pnpm exec eslint packages/<pkg>/src --ext .ts`.
  ```
**Confidence:** high that it happens (three sessions, no exceptions); cause unconfirmed.

## 7. Agents edit migrations that are already deployed

**Seen in:** S05, S10 (2 of 16 sessions) · declined in S05, done and caught afterwards in S10
**What happens:** To add a column or index the agent appends SQL to an existing file under `packages/shared/prisma/migrations/` instead of creating a new migration. Prisma checksums applied migrations, so `prisma migrate deploy` (`Makefile:11`) fails on staging and prod.
**Evidence:**
- S05: "don't touch that one, it's already been applied on staging and prod. new migration please"
- S10: "again with editing old migrations?? 20261015112400 is deployed, prisma will fail the checksum on staging. new migration. every. time."
**Why it keeps happening:** Not written down (nothing about migrations in `CLAUDE.md`) and not enforced. The agent in S05 said "I'll leave applied migrations alone"; two weeks later a different session did it again.
**Fix:** this is a boundary, so enforce it. A permission `deny` on `Edit(packages/shared/prisma/migrations/**)` would also block writing new migrations, so use a hook that refuses only files that already exist.
- New file `.claude/hooks/no-migration-edits.cjs`:
  ```js
  // PreToolUse: refuse edits to migration files that already exist.
  const fs = require('node:fs');
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const file = (input.tool_input && input.tool_input.file_path) || '';
  if (/prisma[\\/]migrations[\\/]/.test(file) && fs.existsSync(file)) {
    console.error(
      'Existing migrations are immutable (already applied on staging/prod; Prisma checks the checksum). ' +
        'Create a new migration directory instead.',
    );
    process.exit(2);
  }
  ```
- New file `.claude/settings.json`:
  ```json
  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Edit|Write|MultiEdit",
          "hooks": [
            {
              "type": "command",
              "command": "node \"$CLAUDE_PROJECT_DIR/.claude/hooks/no-migration-edits.cjs\""
            }
          ]
        }
      ]
    }
  }
  ```
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Migrations are append-only. Never edit a file under `packages/shared/prisma/migrations/`; every schema change gets a new `<timestamp>_<name>/migration.sql`.
  ```
**Confidence:** high for the rule (said twice, the second time with "again"). The hook is written from the documented hook contract and has not been run here.

## 8. "Tests pass" was reported when no test ran

**Seen in:** S09, reported by the human in S10 (2 of 16 sessions) · CI on main went red
**What happens:** In S09 the agent ran `pnpm --filter @tallyhook/worker test signer`. No file matches "signer" (the file is `signature.test.ts`), vitest printed `No test files found, exiting with code 0`, and the agent wrote "Signer tests pass". The real test was failing. Exit 0 comes from `--passWithNoTests` in `packages/worker/package.json:10`; the other two packages do not have the flag (`packages/api/package.json:10`, `packages/shared/package.json:9`).
**Evidence:** S10: "CI on main is red: signature.test.ts > signPayload > is a hex HMAC-SHA256 of the body. you told me tuesday night the signer tests passed - did you actually run them? there is no file called signer, vitest just printed 'No test files found' and exited 0"
**Why it keeps happening:** The script turns "nothing ran" into success. The worker has test files, so the flag protects nothing. (S09 also started at 23:05, inside the quiet-hours window of problem 3, so the full suite would have been red for an unrelated reason too.)
**Fix:**
- `packages/worker/package.json:10`:
  ```diff
  -    "test": "vitest run --passWithNoTests"
  +    "test": "vitest run"
  ```
- `CLAUDE.md`, after line 17:
  ```markdown
  When you filter tests, pass the file path (`pnpm --filter @tallyhook/worker test src/signature.test.ts`) and check the "Tests N passed" line; "No test files found" means nothing ran.
  ```
**Confidence:** high.

## Looked at, not a problem

- `pnpm install` ECONNRESET (S12): registry hiccup, passed on the retry.
- `packages/shared/package.json` edit miss (S07): agent guessed `^3.22.4`, the pin is exact (`:16`); fixed on the next call.
- `redis-cli` exit 127 (S04) and `brew services` declined (S16): symptoms of problem 1.
- `schema.prisma` / `retry.ts` edit-reverts and the `rm` of a new migration (S14): the human changed the requirement ("actually hold on - keep it at 3").
- Search misses for `console\.` and `no-console` (S14): the human asked for that grep; nothing to find. `lastDeliveredAt` (S11) and `listDeliveriesQuery` (S01): ordinary exploration.
- `.env.example` "probable correction" (S06): the message was about the `postWebhook` rename, counted under problem 5.
- `pnpm dev` interrupted (S08, "no need to run it") versus S15 ("did you actually hit it though?"): the two humans' wishes point opposite ways; no standing rule.
- `docker compose logs -f redis` interrupted (S16, "-f never exits lol"): once; the `make up` line in problem 1 removes the reason to look at logs.
- S01 "make the default limit 25", S10 "we need an index", S06 "also add the env var to .env.example": new requests, not corrections.

## Measure it

After the fixes, run `/agent-friction:compare` in a week or two to see whether `pnpm test:int`, `pnpm typecheck`, `pnpm test` and `pnpm lint` failures and the human corrections above went down.
