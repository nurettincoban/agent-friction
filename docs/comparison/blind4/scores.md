# blind4 (kilnlog) scores

Graded against `docs/comparison/blind4/answer-key.md`. Claims were checked against the 15 session transcripts, the two subagent sidechains and `blind4/repo/`. Reports are graded only on what they say. I did not try to work out which tool produced which report.

**How the rubric was applied.**
- **Found.** 2 needs the target and the mechanism from "Found means". 1 is the target or the symptom without the mechanism. 0 means absent.
- **Fix.** Y needs a concrete fix that meets "Fix means". A generic "state the boundary" or "document it" with no rule written out is N, as is a conditional "check whether..." prompt.
- **Decoys.** A decoy counts only when it is proposed as something to fix. Table rows and neutral mentions do not count.
- **P8.** 2 needs the narrow search to be tied to the delegated or subagent search and to the main agent trusting its "no other usages" report. A report that says only "Claude searched only kiln-core" names the scope, not the subagent or trust mechanism, so it scores 1.
- **P1.** 2 needs the `.env` `DATABASE_URL`, or a database that does not exist, set against the `SQLX_OFFLINE` / `.sqlx` cache that the repo already has. Saying only "sqlx needs a live DB, set up offline mode" scores 1, especially when the report thinks the cache still has to be created.
- **Contradicted statements.** A statement counts when the transcripts or the repo show it is false: session counts, order of events, who said what, or file and line references. Model-estimated satisfaction and outcome labels are not counted.

## Totals

| Report | Found (of 16) | Fixes (of 8) | Decoys raised as a fix (of 4) | Statements the transcripts contradict |
|---|---|---|---|---|
| A | 15 | 8 | 0 | 1 |
| B | 15 | 8 | 0 | 1 |
| C | 6 | 0 | 1 | 3 |
| D | 13 | 7 | 0 | 3 |
| E | 11 | 5 | 0 | 3 |
| F | 11 | 6 | 0 | 2 |
| G | 11 | 6 | 0 | 4 |

## Per planted item

Each cell gives the Found score, the Fix verdict and a short quote.

| Item | A | B | C | D |
|---|---|---|---|---|
| **P1** hub sqlx / `.env` vs `SQLX_OFFLINE` | **2 / Y**: "`.env:3` sets `DATABASE_URL=...` … only `just test`, `just lint` … and CI … set `SQLX_OFFLINE=true`"; "the 're-run that passed' was each time a narrower command". Fix: `.cargo/config.toml` `[env] SQLX_OFFLINE = "true"` plus a CLAUDE.md note. | **2 / Y**: same mechanism, with quotes of "I couldn't compile kiln-hub here" from S06, S07 and S12. Fix: `[env] SQLX_OFFLINE`, which also makes `release-build`/`hub` work, plus CLAUDE.md. | **1 / N**: "`cargo test` looks flaky … The tool said: `unable to open database file` (4 of 9 events, 4 sessions)". Symptom only, and mislabelled as flakiness. Fix: "Fix or quarantine the flaky test … re-run once". | **2 / Y**: the same `.env` / `.gitignore` / justfile / CI analysis; "It is not flaky, as the scan guessed". Fix: `[env] SQLX_OFFLINE` plus CLAUDE.md. |
| **P2** evening ETA test | **2 / Y**: "starts a 374-minute firing at `Local::now()` … fails whenever the tests run after 17:46 … 19:40, 21:15 and 18:30"; "Each agent set it aside". Fix: a fixed `Utc.with_ymd_and_hms` start. | **2 / Y**: the same mechanism, with the three left/right date pairs; "called it 'flaky' (S04) … 'Leaving it alone' (S14)". Fix: a fixed start time. | **0 / N**: not mentioned. Item 1's "flaky" is the P1 database error. | **0 / N**: dismissed as "S04 and S14 `cargo test -p kiln-core` failures: tests written first, red then green". |
| **P3** release build > 2 min | **2 / Y**: "`lto = "fat"` and `codegen-units = 1`. In S13 the full release build took 6m 31s". Fix: CLAUDE.md "take up to 7 minutes (fat LTO): run them in the background or with a 10-minute timeout". | **2 / Y**: "fat LTO … `Command timed out after 2m 0.0s` (S03, S08, S13)". Fix: the same CLAUDE.md line. | **1 / N**: rows "just check-size … command-failure×1" and "just release-build … ×1". No timeout and no profile. | **2 / Y**: "`lto = "fat"` … takes longer than the agent's default 2-minute limit". Fix: a CLAUDE.md 10-minute timeout or background run. |
| **P4** no unwrap/expect in core | **2 / Y**: quotes from S02, S07 and S11; "does not forbid panics, and nothing checks it". Fix: `#![cfg_attr(not(test), deny(clippy::unwrap_used, clippy::expect_used))]` plus CLAUDE.md. | **2 / Y**: the three quotes and the three concrete call sites. Fix: the lint plus CLAUDE.md. | **1 / N**: it quotes "again with the .expect( in core... we talked about this", but only as part of a one-off `routes.rs` correction whose "rule" is `without_v07_checks`. | **2 / Y**: the three quotes; "not written down and not enforced". Fix: the lint plus CLAUDE.md. |
| **P5** never edit applied migration | **2 / Y**: both quotes, with the 0002 edit declined twice. Fix: a CLAUDE.md rule plus `permissions.deny` on the existing files. | **2 / Y**: both quotes. Fix: a CLAUDE.md rule plus a tested PreToolUse hook. | **2 / N**: "declined the agent's action 2 times in 2 sessions", with both quotes. Fix: "State the boundary explicitly … in AGENTS.md, or encode it as a permission rule". Generic, with no rule written. | **2 / Y**: both quotes. Fix: a CLAUDE.md rule plus `permissions.deny`. |
| **P6** CHANGELOG for CLI changes | **2 / Y**: quotes from S05, S09 and S14. Fix: CLAUDE.md "Any user-visible change … gets a line under `## [Unreleased]`". | **2 / Y**: the three quotes. Fix: a CLAUDE.md line. | **0 / N**: not mentioned. The S09 message appears only as untargeted "probable-correction" rows. | **2 / Y**: the three quotes. Fix: a CLAUDE.md line. |
| **P7** S07 false "works" | **2 / Y**: "S07: the agent reported done; the hub panicked on start", which it ties to the kiln-core-only narrowing (P1); `{id}` vs the scan's `without_v07_checks`. Fix: the P1 fix plus "build and test the whole workspace … start it once (`just hub`)". | **2 / Y**: "S07 added the route `/firings/:id/export`; axum 0.8 … panics … had run only `cargo test -p kiln-core`". Fix: the P1 fix, CLAUDE.md "axum 0.8: path captures are `{id}`", and a `router_builds` test. | **1 / N**: it quotes "you said it works but hub panics on start" (truncated), but its rule is "use `without_v07_checks`". No mechanism, and the fix is wrong. | **2 / Y**: "S07: 'you said it works but hub panics on start' … (last run: `cargo test -p kiln-core`)"; `:id` vs `{id}`. Fix: CLAUDE.md "run `cargo test --workspace` … start it once with `just hub`" plus the P1 fix. |
| **P8** S10 subagent narrow search | **1 / Y**: "S10: a rename was checked with `cargo test -p kiln-core` only and broke the other two crates". No subagent and no scope. Fix: "build and test the whole workspace, not one crate". | **1 / Y**: "Why the search subagent in S10 reported 'No other usages found' is unconfirmed". It names the subagent but not the kiln-core-scoped Grep. Fix: "A kiln-core API change touches kilnctl and kiln-hub too" plus `cargo test --workspace`. | **0 / N**: not mentioned. | **1 / Y**: "S10 … (the agent's last run was `cargo test -p kiln-core`)". No subagent. Fix: "run `cargo test --workspace`, not only `-p kiln-core`". |
| **Total** | **15 / 8** | **15 / 8** | **6 / 0** | **13 / 7** |

| Item | E | F | G |
|---|---|---|---|
| **P1** hub sqlx | **1 / N**: "kiln-hub can't build in your environment, a whole crate goes unverified". No sqlx or `.env` mechanism; it suggests "a Dockerfile or devcontainer with the missing system dependencies". Its CLAUDE.md says "kiln-hub cannot be built in this environment". | **1 / N**: "kiln-hub's sqlx macros also can't compile in Claude's environment … Setting up sqlx offline mode would let Claude actually build kiln-hub". It thinks `.sqlx` must still be generated, and does not mention `.env` or the doc vs `just test` / CI mismatch. Fix: only a conditional "Check whether kiln-hub can build with SQLX_OFFLINE=true". | **1 / N**: "kiln-hub also can't compile without a local database … set up sqlx offline mode". Same gaps as F. Fix: a "Help me set up sqlx offline mode" prompt; its CLAUDE.md still says `cargo test --workspace`. |
| **P2** evening ETA test | **1 / N**: only inside a paste prompt, "pre-existing failures such as eta_same_day_for_short_bisque". | **1 / Y**: "flagged an unrelated failing test (eta_same_day_for_short_bisque)". No time-of-day cause. Fix: CLAUDE.md "Known pre-existing failure: `eta_same_day_for_short_bisque` … Mention it, but don't try to fix it unless asked". This records the issue, which the key accepts. | **1 / Y**: CLAUDE.md "Known pre-existing failure: `eta_same_day_for_short_bisque`. Note it, but don't treat it as a regression", plus a prompt to investigate and fix it. No mechanism. |
| **P3** release build > 2 min | **0 / N** | **0 / N** | **0 / N** |
| **P4** no unwrap/expect | **2 / Y**: "the user had to restate their rule that kiln-core must never panic … 'same as last time'". Fix: CLAUDE.md plus `#![deny(clippy::unwrap_used, clippy::expect_used)]`. | **2 / Y**: "Claude used unwrap or expect in kiln-core in at least three sessions". Fix: CLAUDE.md plus `cfg_attr(not(test), deny(...))`. | **2 / Y**: "'same as last time' when an `unwrap` appeared". Fix: CLAUDE.md plus `[lints.clippy] unwrap_used = "deny"`. |
| **P5** migrations | **2 / Y**: "Claude first edited an already-shipped migration, a mistake the user said had happened before". Fix: CLAUDE.md plus a PreToolUse hook. | **2 / Y**: "Claude edited migration 0002 instead of adding 0005, repeating a mistake". Fix: a CLAUDE.md Database migrations section. | **2 / Y**: "'same thing as before' when Claude edited the already-shipped 0002 migration". Fix: CLAUDE.md plus a `protect-migrations.sh` PreToolUse hook. |
| **P6** CHANGELOG | **2 / Y**: "repeatedly forgotten changelog entries … 'you forgot changelog again'". Fix: CLAUDE.md "needs one entry under `## Unreleased`". | **2 / Y**: "entries skipped on some changes". Fix: a CLAUDE.md CHANGELOG section. | **2 / Y**: "leaving out the changelog". Fix: a CLAUDE.md Changelog section. |
| **P7** S07 false "works" | **2 / Y**: "Claude said the CSV schedule export worked without building or running kiln-hub … axum 0.7-style `:id`". Fix: CLAUDE.md "Never claim kiln-hub changes work … axum 0.8 route syntax: `/{id}`". | **2 / Y**: the same. Fix: "Never claim a kiln-hub change 'works' unless it was actually built and run … `/{id}`". | **2 / Y**: "declared victory after testing only kiln-core. The hub then panicked … `:id`". Fix: the same CLAUDE.md lines plus a router-construction test. |
| **P8** subagent narrow search | **1 / Y**: "Claude's first pass searched only kiln-core". No subagent and no trusted report. Fix: CLAUDE.md "search all crates (`rg <name>` at the repo root)". | **1 / Y**: "Claude only searched kiln-core". Fix: CLAUDE.md "must be searched across ALL crates". | **1 / Y**: "Claude searched only kiln-core". Fix: CLAUDE.md "search and update ALL crates, then run `cargo build --workspace`". |
| **Total** | **11 / 5** | **11 / 6** | **11 / 6** |

## Decoys

| Decoy | A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|---|
| D1 TDD red-green (S04) | not raised (dismissed) | not raised (dismissed) | not raised | not raised (dismissed) | not raised (praised) | not raised (praised) | not raised (praised) |
| D2 network failure (S01) | not raised (dismissed) | not raised (dismissed) | not raised (hidden in `cargo build` row) | not raised (dismissed) | not raised | not raised | not raised |
| D3 Fahrenheit change of mind (S11) | not raised (dismissed) | not raised (dismissed) | **raised**: "What to fix first 4. crates/kilnctl/src/cli.rs: Check whether this correction is a standing rule … 'keep celsius default'" | not raised (dismissed) | not raised (neutral) | not raised (neutral) | not raised |
| D4 expected-empty search (S11) | not raised (dismissed) | not raised (dismissed) | not raised (one "search-miss" row) | not raised (dismissed) | not raised (praised) | not raised (praised) | not raised (praised) |
| **Raised as a fix** | 0 | 0 | 1 | 0 | 0 | 0 | 0 |

## Statements the transcripts contradict

**A (1)**
1. "`just release-build` (`justfile:35`)". The justfile has 33 lines, and `release-build` is at lines 32-33.

**B (1)**
1. "Only S13 found `SQLX_OFFLINE`". S06 found it as well: after reading `.env`, the agent ran `SQLX_OFFLINE=true cargo build -p kiln-hub` (14:02:30). That attempt failed because the cache was missing the changed queries. B describes this attempt itself later in the same section.

**C (3)**
1. "`cargo test` fails and then passes on a re-run with nothing changed in between". The re-run was never the same command. Each time it was narrowed to `-p kiln-core` or `-p kiln-core -p kilnctl` (S02, S05, S09).
2. "The rule, as they put it: use `without_v07_checks`". The human never said this. The phrase comes from the axum panic message that the human pasted in S07, and the fix that was applied used `{id}`.
3. The header says "2 human corrections". The transcripts hold at least seven correcting messages: S02, S05, S07, S09, S10, S11 ×2 and S14.

**D (3)**
1. "`CLAUDE.md:20` says '`thiserror` in libraries…'". It is line 21. Line 20 is "Keep axum handlers thin".
2. "`CLAUDE.md:17` already says °C". Line 17 is `## Conventions`, and the °C rule is on line 18.
3. "S04 and S14 `cargo test -p kiln-core` failures: tests written first, red then green". S14 was not test-first, and both of its failures are `eta_same_day_for_short_bisque` (left 2026-11-11, right 2026-11-10). S04's second failure is the same test, which the agent called "flaky".

**E (3)**
1. "kiln-core Schedule Presets & Validation (~2 sessions: ramp-rate validation and Glaze6SlowCool) … In both cases Claude's first versions used expect/unwrap". S04 (Glaze6SlowCool) has no unwrap or expect and no such correction.
2. "In 4 sessions (total_hold, ramp validation, CSV export, and others) the user had to ask Claude to remove unwrap or expect". It was 3 sessions (S02, S07, S11), and the report itself says "three separate sessions" elsewhere.
3. "The test-first sessions (Glaze6SlowCool, ramp validation) … were fully achieved with little friction". The ramp-validation session (S02) was not test-first. The prompt was "add an error variant and tests", and that session drew the unwrap/expect correction.

**F (2)**
1. "After the user pointed out the missing CHANGELOG entry for the CSV firings import, a later request … `kilnctl plan` JSON output got three extra entries". The order is reversed. The plan `--json` session is S05 (2026-10-15), and the import session with "you forgot chnagelog again" is S09 (2026-10-27).
2. "Twice, kiln-hub changes shipped unverified because sqlx macros couldn't compile". It happened 4 times: kiln-hub was changed and never compiled in S06, S07, S10 and S12.

**G (4)**
1. "When work is clean (the slow-cool preset, ramp-rate validation, …) you simply acknowledge it" and "Your test-first sessions (slow-cool preset, ramp-rate validation) went smoothly". S02 (ramp-rate) was not test-first and needed the unwrap/expect correction. G reports that correction itself elsewhere.
2. "In at least three sessions (total_hold, CSV export, ramp-rate validation) Claude added unwrap/expect after being told not to". In S02 (ramp-rate) the expect came before the rule was ever stated, because S02 is where the human first stated it.
3. "Two or three sessions ended with kiln-hub unverified". It was 4: S06, S07, S10 and S12.
4. "the Task tool … which you have used only once so far". Task was used twice, in S10 (`cone_to_celsius` search) and S13 (CHANGELOG comparison).

## Real findings not in the key, and misleading content

**A**
- Correct extras:
  - `sqlx::migrate!` at `main.rs:16` rejects a changed checksum on an applied migration, which is why editing 0002 would break the studio box.
  - It warns that the scan's "ready to paste" line `without_v07_checks` is wrong and should not be pasted.
  - It reports that S06 also lacked `sqlx-cli`.
- Misleading: nothing material.

**B**
- Correct extras:
  - The same `migrate!` checksum point.
  - A `router_builds` unit test so that bad axum routes fail in `cargo test`.
  - The `[env]` setting also fixes `release-build` and `hub`, and makes the justfile prefixes redundant.
  - The migration hook is scoped to committed files, so new migrations stay allowed.
  - The `without_v07_checks` warning.
- Misleading: nothing material.

**C**
- Correct extras: none.
- Misleading:
  - It frames P1 as flakiness and advises "re-run once before anything is changed". That entrenches the narrowing habit.
  - The "Ready to paste" line "use `without_v07_checks`" would put axum's escape hatch into CLAUDE.md as a rule, which is the opposite of the actual fix (`{id}`).
  - It raises the Fahrenheit change of mind as a possible standing rule.

**D**
- Correct extras:
  - The `migrate!` checksum point.
  - The `without_v07_checks` warning.
  - A hedged note that post-S06 "4 previous errors" may include uncached queries.
- Misleading: it explicitly dismisses the evening test failure as TDD red-green. A reader would never fix P2.

**E**
- Correct extra: it notes, accurately, that S05's agent added unrequested changelog entries for earlier changes.
- Misleading:
  - It treats the S05 changelog entries as friction and proposes "Never add entries for other changes you infer". Those entries were real unlogged changes, and the S13 subagent later found more missing entries. The rule would make P6 worse.
  - It blames kiln-hub's build failure on "missing system dependencies" and a Dockerfile, and writes "kiln-hub cannot be built in this environment" into CLAUDE.md. That locks in P1 instead of fixing it.
  - Its `/finish` skill step 1 is `cargo test -p kiln-core -p kilnctl`, the exact narrowing that hides hub breakage.
  - Its hook greps `kiln-core/src`, but the path is `crates/kiln-core/src`, so the hook never matches.
  - Its P8 remedy, "Use an agent to find every reference", is the same delegation that failed in S10, and it adds no check of the agent's result.

**F**
- Correct extra: the same S05 changelog over-scoping observation.
- Misleading:
  - The same "never add other entries" rule.
  - It tells the user to run `cargo sqlx prepare` and commit `.sqlx`, but `.sqlx/` is already committed. The real cause is `.env`'s `DATABASE_URL` together with CLAUDE.md's bare commands.
  - Its CLAUDE.md says the ETA test "fails independently of current work … don't try to fix it", with no time-of-day cause.
  - Its `/finish` `rg` uses the wrong path, `kiln-core/src`.
  - It repeats the "use an agent to find every reference" advice.

**G**
- Correct extras: the same S05 observation, plus a router-construction integration test proposal.
- Misleading:
  - The same `cargo sqlx prepare` framing and the same over-scoped changelog rule.
  - Its "Known pre-existing failure … don't treat it as a regression" entry, with no cause given.
  - Its P8 remedy is again delegation to an agent, with no instruction to verify the result.

## Grader notes

- **A and B** both reach 15 of 16. Each loses one point on P8 only:
  - A never mentions the subagent.
  - B mentions the subagent but calls the cause "unconfirmed" and does not identify the kiln-core-scoped Grep.
  - Both are close to fully accurate on file and line facts. Each has one contradicted statement.
- **D** matches A and B on P1 and P3-P7, but explicitly misfiles P2 (the evening test) as TDD red-green. That costs 2 points and the fix, and D also has two off-by-one CLAUDE.md line citations.
- **E, F and G** read 9 of 15 sessions, according to their headers. They find all four human-voiced items (P4-P7) at full marks, but:
  - they miss P3 entirely;
  - they get only the symptom of P1 and P2;
  - they score 1 on P8, because they say "Claude searched only kiln-core" without the subagent or the trusted report.
- **The F/G P2 fix.** It counts only because the key accepts "record the known issue in CLAUDE.md". Their wording tells the agent not to fix the test.
- **C** finds P5 and the symptoms of P1, P3, P4 and P7. Its "What to fix first" list:
  - mislabels P1 as a flaky test;
  - pushes a wrong rule (`without_v07_checks`) as ready to paste;
  - raises the D3 change of mind;
  - gives only generic "state the boundary" advice, which scores N under this rubric. A report that quotes the human's rule but writes no rule of its own does not meet "Fix means".
- **P5 for C.** Found is 2, because C quotes the rule twice in two sessions, but Fix is N for the reason above.

## Which report is which

The grader saw the reports as A-G, shuffled, without knowing which tool made which. Unblinded after grading:

| Report | Made by |
|---|---|
| A | /agent-friction:scan diagnosis run 3 → runs/diagnosis-run3.md |
| B | /agent-friction:scan diagnosis run 2 → runs/diagnosis-run2.md |
| C | agent-friction scan (CLI, deterministic) → agent-friction.txt |
| D | /agent-friction:scan diagnosis run 1 → runs/diagnosis-run1.md |
| E | /insights run 1 → runs/insights-run1.txt |
| F | /insights run 3 → runs/insights-run3.txt |
| G | /insights run 2 → runs/insights-run2.txt |
