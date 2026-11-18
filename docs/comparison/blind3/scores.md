# blind3 scores (tallyhook)

**How the rubric was applied.** I graded each report against `blind3/answer-key.md` and checked its claims against `blind3/sessions/` (all 16 transcripts, read in full) and `blind3/repo/`. A Found score of 2 needs the target and the mechanism from "Found means". Where the key says "must", that element is required:
- P1: at least two of S01, S02 and S11.
- P4: the rule was stated in S05 and violated again in S10.
- P5: at least two sessions.
- P8: tied to the ECONNREFUSED output and to human reminders in at least two sessions.

For P7 I read "one mechanism gets credit" as enough for a 2, and I note the missing half. A report that quotes the human stating the mechanism on the right target counts as naming it. A fix gets Y only when it is concrete and meets "Fix means". Hedged or conditional fixes ("measure first, then if...", "cheap candidate to try") get N, and so does a CLAUDE.md note alone where the key requires automation (P1). A decoy counts only if the report proposes it as a problem to fix. For a tool's table rows, a row on the right target scores 1, and rows are never treated as proposals.

Counting rules for contradicted statements:
- Session counts are judged against the scope the report claims. A, B and G say they analysed 9 of the 16 sessions, so I did not penalise them for undercounting. C, D, E and F say they cover all 16 ("2 of 16 sessions"), so an omitted session counts against them.
- Wrong line numbers in `repo/` count as contradicted.
- Paraphrases put in quotation marks as the human's words count as contradicted.

## Totals

| Report | Found (of 16) | Fixes (of 8) | Decoys raised as a fix (of 4) | Statements the transcripts contradict |
|---|---|---|---|---|
| A | 12 | 5 | 0 | 3 |
| B | 13 | 5 | 0 | 2 |
| C | 15 | 7 | 0 | 5 |
| D | 5 | 1 | 0 | 3 |
| E | 15 | 7 | 0 | 1 |
| F | 16 | 8 | 0 | 4 |
| G | 11 | 5 | 0 | 4 |

## Planted items

Each cell gives the Found score, the fix verdict, and a short quote from the report.

| Item | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| **P1** Prisma codegen | **2 / N**: "run `npx prisma generate`. The client is gitignored and is often stale or missing … Two other sessions hit a stale or missing Prisma client". Pagination and disable-endpoint work are named, which identifies S01 and S02. Fix is CLAUDE.md and a /verify skill only. | **2 / N**: "The Prisma client is gitignored and is often missing or stale"; "came up twice" (cursor pagination and disable endpoint). Fix is CLAUDE.md, a skill, and a `make verify` aimed at integration testing; nothing hooks generation into install or typecheck. | **2 / Y**: "`./generated/prisma` … git-ignored (`.gitignore:5`). Nothing generates it on install … needed again after every edit to `schema.prisma`" (S01, S02). Fix: `"postinstall": "prisma generate"`. | **1 / N**: row "pnpm typecheck 2 2 18s ◐ command-failure×2". Symptom only. | **2 / Y**: S01, S02 and S11, with TS2307 and stale `EndpointUpdateInput` errors; "there is no `postinstall`". Fix: root `typecheck` runs `db:generate` first. | **2 / Y**: "git-ignored … written only by `prisma generate` … missing or stale and `tsc` fails" (S01, S02). Fix: root `typecheck` runs `db:generate` first. | **1 / N**: "After schema changes, or if the Prisma client looks stale or missing, run `prisma generate`"; "came up in two sessions". No mechanism (gitignored, nothing regenerates it). |
| **P2** quietHours clock test | **0 / N**: only "also flagged an unrelated failing test", in a praise context. | **1 / N**: "fix anything they reveal, including the unrelated failing quietHours unit test". Names the target, gives no mechanism. | **2 / Y**: "calls `isQuietHours(DEFAULT_QUIET_WINDOW)` with no date … uses `new Date()` … 22:00–07:00 … started at 22:35, 22:48 and 06:21". Fix pins a `Date`. | **1 / N**: row "pnpm test 3 3 n/a ○ command-failure×3". | **2 / Y**: same mechanism; adds "deterministic, not flaky" and quotes the agent's "flaky" dismissals. Fix pins a `Date`. | **2 / Y**: "The three sessions started at 22:35, 22:48 and 06:21 UTC: all inside the window". Fix pins a `Date`. | **0 / N**: not mentioned. |
| **P3** lint glob | **0 / N**: not mentioned. | **0 / N**: not mentioned. | **1 / N**: S03 and S07 timeouts. Cause marked "Unconfirmed"; it blames whole-workspace type-aware linting, and the glob is only "the first thing to narrow" if a measurement shows lint is slow (conditional). | **1 / N**: row "pnpm lint 2 2 1m ◐ command-failure×2". | **1 / N**: S03, S07 and S12 all named. Calls the glob "the obvious suspect" but argues "that alone does not explain it". The fix is a "cheap candidate to try" (hedged). | **2 / Y**: "`project: ['./**/tsconfig.json']` … walks every `node_modules` directory". Fix: `['./packages/*/tsconfig.json']`. | **0 / N**: not mentioned. |
| **P4** applied migrations | **2 / Y**: "never edit deployed migrations" listed among the rules "you keep restating"; "you called that out as a repeated mistake". Fix: CLAUDE.md "## Database migrations" plus a PreToolUse hook for existing migration files. | **2 / Y**: "Claude kept forgetting rules you had already taught it, such as … never editing deployed migrations". Fix: CLAUDE.md plus a hook. The hook is PostToolUse, so it fires after the edit has been made. | **2 / Y**: quotes from S05 and S10. Fix: CLAUDE.md plus a PreToolUse hook on committed migrations. | **1 / N**: "migration.sql: State the boundary", quoting S05 only. Nothing on S10, and the fix is generic ("State the boundary … in AGENTS.md"). | **2 / Y**: S05 and S10; "I'll leave applied migrations alone; two weeks later … again". Fix: hook plus CLAUDE.md. | **2 / Y**: S05 and S10. Fix: CLAUDE.md plus a PreToolUse hook. | **2 / Y**: "You kept re-teaching … adding new migrations instead of editing deployed ones". Fix: CLAUDE.md plus a PreToolUse hook. |
| **P5** console.* | **2 / Y**: 3 sessions, "the log shipper silently drops non-JSON output". Fix: CLAUDE.md plus a `no-console.sh` hook. | **2 / Y**: 3 sessions named (maxRetries warning, counters, startup). Fix: CLAUDE.md plus a hook. | **2 / Y**: S06, S08 and S14 quoted. Fix: `'no-console': 'error'` plus CLAUDE.md. | **0 / N**: not mentioned; the header claims "0 human corrections". | **2 / Y**: S06, S08 and S14. Fix: `no-console` plus CLAUDE.md. | **2 / Y**: S06, S08 and S14. Fix: `no-console` plus CLAUDE.md. | **2 / Y**: three sessions, "prod log shipper drops non-JSON". Fix: CLAUDE.md plus a hook. |
| **P6** scoped diffs | **2 / Y**: rename, recordFailure and prettier. Fix: CLAUDE.md "## Scope discipline … format only the lines you changed". | **2 / Y**: all three. Fix: CLAUDE.md "Scope discipline". | **2 / Y**: S02, S06 and S08 quoted. Fix: CLAUDE.md, "never `pnpm format` or `prettier --write` on a directory". | **0 / N**: absent. The S06 correction appears only as an ".env.example probable-correction" row. | **2 / Y**: S02, S06 and S08. Fix: CLAUDE.md. | **2 / Y**: S02, S06 and S08. Fix: CLAUDE.md. | **2 / Y**: all three. Fix: CLAUDE.md "## Scope of changes". |
| **P7** unverified claims | **2 / Y**: "vitest filter matched zero files"; "/readyz passed mocked unit tests but hung". Fix: CLAUDE.md "## Verification" plus a suggested Stop hook. | **2 / Y**: both instances. Fix: CLAUDE.md "Verification" plus a Stop hook suggestion. | **2 / Y**: S09/S10 with `--passWithNoTests`, plus S15. Fix: remove the flag, plus CLAUDE.md. | **0 / N**: absent. | **2 / Y**: S09/S10 with `--passWithNoTests`. Leaves out S15 (it puts S15 under "not a problem"). Fix: remove the flag, plus run tests by file path. | **2 / Y**: S09/S10 with the flag, plus S15. Fix: remove the flag, plus CLAUDE.md. | **2 / Y**: both instances. Fix: CLAUDE.md "## Verifying work" plus a Stop hook suggestion. |
| **P8** `make up` | **2 / Y**: services down as the cause of the requeue failure; "In at least 3 sessions … needed reminding"; notes Claude "documented them in CLAUDE.md". Fix: `make up` then `make db-migrate`. | **2 / Y**: "you keep having to remind Claude to run `make up` first"; notes the CLAUDE.md update. Fix: CLAUDE.md "Local environment". | **2 / Y**: ECONNREFUSED and can't-reach-database errors in S04, S11 and S16; human quotes from S04, S15 and S16. Fix: CLAUDE.md, a `test-int` target, and healthchecks. Says the S16 line "is still missing". | **1 / Y**: ECONNREFUSED in 3 sessions, but a human reminder from S16 only (and only as a "rejection reason"); the "must" on repeated reminders is not met. Fix: "Run `make up` before `pnpm test:int`" (no `db-migrate`). | **2 / Y**: same evidence, and notes "The agent in S16 did add a line to CLAUDE.md, but the CLAUDE.md in this checkout does not have it" (bonus). | **2 / Y**: same evidence, plus "Hook timed out". Says the S16 line "is still not there". | **2 / Y**: "Most failures came from Postgres and Redis not running"; reminders across sessions; notes the CLAUDE.md addition and recommends extending it (bonus). |
| **Found / Fixes** | **12 / 5** | **13 / 5** | **15 / 7** | **5 / 1** | **15 / 7** | **16 / 8** | **11 / 5** |

## Decoys

| Decoy | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| D1 TDD red (S05) | not raised | not raised | not raised | not raised | not raised | not raised | not raised |
| D2 zod Edit typo (S07) | not raised | not raised | dismissed explicitly ("One-off") | listed, not raised: a low-confidence row, "edit-miss×1" | dismissed explicitly | dismissed explicitly | not raised |
| D3 ECONNRESET (S12) | not raised | not raised | dismissed ("transient") | listed, not raised: a low-confidence row, "pnpm install … command-failure×1" | dismissed | dismissed | not raised |
| D4 retry default reversal (S14) | neutral ("bumping and then reverting") | neutral ("You also change direction freely") | dismissed ("product changed its mind") | listed, not raised: edit-revert rows on `schema.prisma` and `retry.ts` | dismissed | dismissed | neutral |
| **Raised as a fix** | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Statements the transcripts or repo contradict

I spot-checked at least 8 concrete claims per report. Only the false ones are listed.

**A (3)**
1. "Claude used console.warn, then console.log in two more sessions". The order is reversed: console.log came first in S06 (Oct 19) and S08 (Oct 23), and console.warn last in S14 (Nov 10).
2. "after repeatedly trying to start services outside docker compose". There was one such attempt (S16, `brew services`). In S04 the agent only probed with `redis-cli` and `pg_isready`.
3. "which you said 'keeps happening'" and "The user said this 'keeps happening'" are presented as quotes. The human never said this. The actual words were "every single time" (S16) and "I keep saying this" (S15).

**B (2)**
1. "Claude used console.warn for the high-maxRetries warning, then console.log for delivery counters and webhook startup output". The closing anecdote repeats this order ("then console.log again for startup output"). The real order is startup console.log (S06), counters console.log (S08), then console.warn (S14).
2. "these rules live only in your head … not in CLAUDE.md … including 'run `make up` first.'" In S16 the agent added `make up` and `make db-migrate` to CLAUDE.md. The report says so itself elsewhere ("Claude also updated CLAUDE.md to document the setup").

**C (5)**
1. Lint: "`Command timed out after 2m 0.0s` both times" (S03, S07). S07 ran with `timeout: 600000` and timed out after 10m.
2. Lint "Seen in: S03, S07 (2 of 16 sessions)". S12 also ran `pnpm lint` in the background for about 4.5 minutes with no output and killed it.
3. Typecheck "Seen in: S01, S02 (2 of 16 sessions)". S11 failed typecheck twice for the same stale-client reason.
4. "`CLAUDE.md:13` says the integration tests 'need Postgres + Redis'". That text is on line 14.
5. "`CLAUDE.md:25` states the prettier settings". They are on line 26.

**D (3)**
1. The header says "0 human corrections". Humans corrected the agent in S02, S05, S06, S08, S10, S14, S15 and S16.
2. "pnpm typecheck 2 sessions, 2 events". It failed in 3 sessions (S01, S02 and S11), 4 times in all.
3. "pnpm lint 2 sessions". S12 ran it too, about 4.5 minutes, then killed. (Report D's "Not counted" note may be where S12 went, but the row states 2 sessions.)

**E (1)**
1. "`console.log` … (`worker/src/index.ts` in S06 and S08 …)". In S08 the console.log was in `packages/worker/src/metrics.ts`, not `index.ts`.

**F (4)**
1. Lint evidence: "`Command timed out after 2m 0.0s` (S03, S07)". S07 timed out after 10m.
2. Lint "Seen in: S03, S07 (2 of 16 sessions)". This leaves out S12.
3. Typecheck "Seen in: S01, S02 (2 of 16 sessions)". This leaves out S11.
4. "the root `format` script is `prettier --write .` (`package.json:9`)". It is on line 10; line 9 is `lint`.

**G (4)**
1. "Claude used console.warn, then console.log in later sessions". The order is reversed, as for A.
2. "Claude edited a deployed migration more than once". There was one edit, in S10. The S05 attempt was rejected before anything was written.
3. "You corrected Claude on `make up` vs brew in several sessions". Brew came up only in S16.
4. "the user asked for a maxRetries > 5 warning to go through the request logger". The request (S14) was only "DO add a warning log"; the request logger came up only in the later "srsly" correction.

## Extra findings (correct, not in the key) and misleading content

- **A, B, G:**
  - Each flags never-exiting commands (`docker compose logs -f`) and running the worker unprompted. Both are correct, but the key lists them as incidental.
  - Each describes the `/readyz` hang caused by `maxRetriesPerRequest: null` correctly.
  - Each repeats the human's "about 9 files" for the prettier run. Prettier changed 11 files, 7 of them outside the feature; "about 9 unrelated files" overstates this slightly. Not counted.
  - B's migration guard is a PostToolUse hook, so the deployed migration has already been edited when it fires. "Rejects edits" overstates what it does.
  - A claims "every session ended fully achieved", which sits badly with S09 (a false pass, and CI went red). Whether S09 was among its 9 sessions is unknown.
- **C:**
  - Correct extras: problem 9 on foreground never-exit commands (incidental; S08 and S16 interruptions quoted accurately), the requeue test lines 19 and 23 at 101 columns against `printWidth: 100` (checked in the repo), and `docker-compose.yml` having no healthchecks for `--wait` (checked).
  - Misleading: it says the S16 CLAUDE.md line "is still missing" without saying the agent wrote it in S16. A reader could add it twice or think S16 failed.
  - Its lint advice ("run it with a 5-minute timeout") would still have failed in S07 (10 minutes).
- **D:**
  - "0 human corrections" is badly misleading, because most of the friction in this dataset is human corrections.
  - P2, P5, P6 and P7 are missing entirely, and P3 appears only as a row.
  - The brew item quotes the later typed message as the "Reasons given" for the rejection, but the rejection itself carried no reason.
- **E:**
  - Correct extras:
    - S09 started at 23:05, inside the quiet window.
    - It confirms S12 lint from the transcript even though its own scan set S12 aside.
    - It says `docker compose up -d` does not wait for Postgres.
    - It points out that typescript-eslint excludes `**/node_modules/**` from `project` globs by default (`projectFolderIgnore`). That is a plausible real-world objection to the key's P3 mechanism. The report still offers the right glob change, but only as a hedged "candidate", so it gets Found 1 and fix N under the strict reading.
  - Misleading: it files S15 under "not a problem" ("no standing rule"), so a reader would miss the mocked-tests-as-verified half of P7.
- **F:**
  - Correct extras: "Hook timed out in 10000ms" in S04, S11 and S16 (checked); `docker-compose.yml` has no healthchecks; prettier tree drift.
  - It says the `./**/tsconfig.json` glob "walks every node_modules directory, a documented typescript-eslint performance problem". That matches the key, but it may be wrong about current typescript-eslint defaults (see E). Its fix is correct either way.
  - Like C, it says the S16 CLAUDE.md line "is still not there" without saying the agent added it.

## Grader notes

- **E and F:** E has the fewest errors (1 contradicted statement) and catches the S16 CLAUDE.md bonus. F is the only report with full Found and Fix marks. Both copy the same evidence slips: S11 dropped from typecheck and S12 dropped from lint.
- **C and F:** both wrongly report the S07 lint timeout as 2 minutes.
- **A, B and G** (the 9-session runs): all three miss P2 and P3 almost entirely. These are the tool-output-only items, which need the failure output and the wall clock to see. All three get the order of the console.* corrections backwards.
- **G's P1 = 1** is a strict call. It tells agents to run `prisma generate` but never says why the client goes missing (it is gitignored and nothing regenerates it). A and B do say this.
- **P3 for C and E:** both have the glob in hand. C makes the change conditional on measuring first, and E argues the glob is not enough on its own. Under "hedged or conditional = N", neither gets the fix. A more lenient reading would give E a Y.
- **D's P8 = 1** applies the key's "must" on repeated human reminders. The ECONNREFUSED evidence is correct and spans 3 sessions.

## Which report is which

The grader saw the reports as A-G, shuffled, without knowing which tool made which. Unblinded after grading:

| Report | Made by |
|---|---|
| A | /insights run 1 → runs/insights-run1.txt |
| B | /insights run 3 → runs/insights-run3.txt |
| C | /agent-friction:scan diagnosis run 1 → runs/diagnosis-run1.md |
| D | agent-friction scan (CLI, deterministic) → agent-friction.txt |
| E | /agent-friction:scan diagnosis run 3 → runs/diagnosis-run3.md |
| F | /agent-friction:scan diagnosis run 2 → runs/diagnosis-run2.md |
| G | /insights run 2 → runs/insights-run2.txt |
