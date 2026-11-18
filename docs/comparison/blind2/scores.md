# Blind set 2 scores: ferrydesk

Graded against `answer-key.md` (P1–P8, D1–D4, rubric). Found score is 2/1/0; fix is yes/no. Session claims were checked against `sessions/*.jsonl`, including the two subagent sidechains.

**How the rubric was applied.** I used the same standards as `../blind/scores.md`. A 2 needs the target and the mechanism from "Found means". Where the key says a report "must" state something (P3: that agents repeatedly violate the rule because it isn't written down or checked), that element is required. Facets listed without a "must" are noted when missing but do not cost the point. For agent-friction, items under "What to fix first" and "Ready to paste" are proposals, and low-confidence table rows are listings. A report that quotes the human stating the mechanism, on the right target, is taken to name that mechanism. A decoy counts only if the report proposes it as a problem to fix.

**What the `/insights` runs read.** All three runs read the same 9 of 13 sessions: S02, S03, S04, S06, S07, S10, S11, S12 and S13. They did not read S01, S05, S08 or S09. Effect on each planted item:
- P1: read S03 and S11. In both, the agent says the `internal/store` failures need a live Postgres, so the evidence was available.
- P2: read only S02, which has one "String to replace not found". The multi-match failures (S01, S05, S08) are all in sessions the runs did not read.
- P5: read only S07 ("`make lint` can't run here, no golangci-lint binary"). S01, S05 and S09 were not read.
- P7: read S04 (`make db-reset` rejected with a reason) and S12. They did not read S08 (the `docker compose down -v` rejection).
- P3, P4, P6 and P8: every session was read.
- D1 (S09): not read.

agent-friction read all 13 sessions.

## Totals

| Report | Found (of 16) | Fixes (of 8) | Decoys raised as a fix (of 4) | Statements the transcripts contradict |
|---|---|---|---|---|
| insights run 1 | 11 | 5 | 1 | 4 |
| insights run 2 | 11 | 5 | 2 | 5 |
| insights run 3 | 11 | 4 | 1 | 4 |
| agent-friction | 9 | 2 | 1 | 3 |

## Planted items

Each cell gives the score, the fix verdict, and the report's own words.

| Item | insights run 1 | insights run 2 | insights run 3 | agent-friction |
|---|---|---|---|---|
| **P1** store integration tests / `go test ./...` | **0 / fix N**: not mentioned. The only trace is the generic "Command Failed 6" bucket. Its advice to "iterate against `go test ./...` until it passes" would never go green locally. | **0 / fix N**: not mentioned. The only trace is "Command Failed 6", and it says to "loop until `go test ./...` passes". | **0 / fix N**: not mentioned. The only trace is "Command Failed 6", and it says to "loop until `go test ./... && go vet ./...` passes". | **1 / fix N**: "go test 3 3 42s ◐ command-failure×3" (a table row). Symptom only: no `internal/store`, no Postgres, nothing on missing gating. |
| **P2** `internal/api/handlers.go` edit failures | **1 / fix N**: "TOOL ERRORS ENCOUNTERED … Edit Failed 1". A bare count, with no file and no cause. | **1 / fix N**: "Edit Failed 1". A bare count. | **1 / fix N**: "Edit Failed 1". A bare count. | **2 / fix Y**: "internal/api/handlers.go: Large file … 7 edits failed to locate the target in this 1174-line file (3 matched several places, 4 matched nothing) across 4 sessions". Fix: "Split it into smaller modules, or give repeated blocks unique nearby text". |
| **P3** editing applied migrations | **2 / fix Y**: "Claude edited migration 0007, which was already applied in prod, and later edited applied migration 0004 in place"; "these conventions live only in your head … Without anything written down". Fix: a CLAUDE.md "## Migrations … Never edit an existing migration file", plus a PreToolUse hook on `/migrations/`. | **2 / fix Y**: "Claude edited already-applied migrations twice (0004 and 0007), which does nothing for existing databases and breaks the checksum"; "none of these rules are written down anywhere Claude can see". Fix: a CLAUDE.md Migrations section, a hook, and a "guard test … that checksums existing migration files". | **2 / fix Y**: "Twice Claude changed migrations that had already shipped … A documented rule like 'never edit an existing migration; always add the next numbered file' would prevent this"; "the same handful of rules lives only in your head". Fix: a CLAUDE.md Migrations section, plus a hook. | **1 / fix N**: "migrations/0004_add_refunds.up.sql 1 1 n/a ○ user-correction×1", plus two "edit-revert×1" rows on the 0007 files. These are low-confidence rows on the target, with no rule and nothing on repetition. |
| **P4** integer cents | **2 / fix Y**: "Claude kept reaching for floats … even though your codebase clearly uses int64 Cents with ApplyBasisPoints"; "the user had to steer it back to integer cents three separate times". Fix: a CLAUDE.md "## Money … Never use float64 … `Discount{Bps: N}` … `total_cents`". | **2 / fix Y**: "Float math showed up three times: the fare preview returned dollars instead of total_cents, child fares used float64 …, and the group discount used floats". Fix: a CLAUDE.md Money section, plus a float-in-pricing guard test. | **2 / fix Y**: "Claude kept reaching for float math even though your codebase uses int64 cents and basis points throughout, so you had to correct it several times". Fix: a CLAUDE.md Money section, plus a "`grep -rn float64 internal/pricing`" check. | **1 / fix N**: "internal/pricing/fare.go 1 1 12s ○ user-correction×1" and "internal/booking/service.go … user-correction×1". These show corrections on the pricing code, with no cents or float mechanism. |
| **P5** `make lint` / `bin/golangci-lint` | **0 / fix N**: not mentioned. The only trace is "Command Failed 6". | **0 / fix N**: not mentioned. | **0 / fix N**: not mentioned. | **1 / fix N**: "`make lint` failed 4 times in 4 sessions". Names the target but not why it fails (no `bin/golangci-lint`, and `lint` doesn't depend on `tools`). Its fix, "use `go vet`, not `make lint`", is a workaround. It does not install the linter or document `make tools`. |
| **P6** foreground server | **2 / fix Y**: "Claude ran servers in the foreground until the command timed out"; "Never run the server in the foreground to verify behaviour, because it blocks until timeout". Fix: that CLAUDE.md block ("Write an `httptest`-based Go test instead, or start the server in the background"). | **2 / fix Y**: "it tends to launch the server directly, which never exits and hangs until the timeout". Fix: CLAUDE.md "## Running & Testing … If a live server is truly needed, start it in the background, curl it, then kill it." | **2 / fix Y**: "In three sessions Claude ran the server in the foreground until the command timed out". Fix: CLAUDE.md "## Testing & running", plus a hook that blocks "foreground server runs". | **1 / fix N**: "go run 2 2 1m ◐ command-failure×2" and "make run 1 1 36s ○ command-failure×1" (rows). These are failures only, with no timeout, no foreground, and no human quote. |
| **P7** destructive DB reset | **2 / fix Y**: "Claude ran a script that does `docker compose down -v` and you had to stop it before it destroyed your local database". Fix: "Never run `docker compose down -v` … Use a throwaway database", plus a Bash hook on `down -v`. Gives only the S12 instance and never mentions S04's `make db-reset`. | **2 / fix Y**: "nearly ran a `docker compose down -v` that would have wiped your data". Fix: "Validate seed or schema changes in a throwaway database, not my local dev DB", plus a hook. Gives only the S12 instance. | **2 / fix Y**: "ran a setup script that does `docker compose down -v` and nearly wiped your local DB". Fix: "Never run setup scripts that use `docker compose down -v` … use `createdb ferrydesk_scratch_<x>`", plus a hook. Gives only the S12 instance. | **2 / fix Y**: proposals 4 and 5, "docker compose down: State the boundary" and "make db-reset: State the boundary". They quote "please stop wiping the db volume … if you need a db use a separate one" and "db-reset wipes my local data, I've got QA bookings in there". Fix: "State the boundary … in AGENTS.md, or encode it as a permission rule". It treats the two as separate items and lists `scripts/reset-db.sh` only as a row. |
| **P8** `FD_` vs `FERRYDESK_` | **2 / fix Y**: "The README's `FD_` prefix is wrong and is ignored by the code"; "FD_LOG_LEVEL and FD_PORT had no effect". Fix: Agent D "updates the README and .env.example to match the real FERRYDESK_ config". | **2 / fix Y**: "`config.Load` only reads env vars with the `FERRYDESK_` prefix … The README's `FD_*` names are wrong". Fix: subagent (3) "update README/.env.example docs if needed", plus a docs-vs-config audit. This is borderline, because "if needed" hedges it. | **2 / fix N**: "debug logging silently didn't turn on … Both traced back to the documented FD_ prefix". No fix: Subagent D only lists mismatches and will "propose a fix without applying it". The CLAUDE.md line ("The README's `FD_` prefix is wrong") is a workaround for agents, and the report says the README was already "finally fixed". | **0 / fix N**: not mentioned in any row or proposal. |
| **Total** | **11 / 5 fixes** | **11 / 5 fixes** | **11 / 4 fixes** | **9 / 2 fixes** |

## Decoys

| Decoy | run 1 | run 2 | run 3 | agent-friction |
|---|---|---|---|---|
| D1 `go biuld` typo (S09) | not raised (session not read) | not raised (session not read) | not raised (session not read) | listed, not raised: a low-confidence row, "go biuld 1 1 n/a ○ command-failure×1". |
| D2 interrupt / 24h cutoff (S13) | not raised. Neutral: "answered a code question about whether Cancel enforces a 24h refund rule". | **raised**: "Several of your best sessions uncovered hidden mismatches … Cancel ignores the 24h refund rule", which feeds an audit that writes "a failing Go test" for each such gap (covering "refund windows"). The human called it "a product call". | not raised. Neutral: "confirmed that Cancel refunds the full total with no 24h cutoff". | not raised |
| D3 TDD red test (S02) | not raised | not raised | not raised | not raised. It is probably in "Not counted: 5 failed runs of normal work". |
| D4 declined `git push` (S07, S12) | **raised**: a "Git actions not matching your intent" friction section, a CLAUDE.md "## Git" block, and a PreToolUse hook to block "`git commit`/`git push` unless you ask". | **raised**: the "Unrequested destructive or irreversible actions" section, "## Git & Safety", a hook matching `git push`, and "Make the review/ship boundary explicit". | **raised**: "committed or pushed without being asked", "## Git & safety", a hook blocking `git push`, and "Be explicit about the finish line". | **raised**: "3. git push: State the boundary" under "What to fix first". |
| **Raised as a fix** | 1 | 2 | 1 | 1 |

D4 is counted for every report because each one gives it a section or a top proposal, with a hook or permission rule, and that goes beyond an aside in agent instructions. Run 2's D2 is a judgement call. It shows up only as an example in "On the Horizon", but it presents the missing cutoff as a code gap to test for. A lenient grader would not count it, which gives run 2 one decoy.

## Statements the transcripts contradict

**insights run 1**
1. "Claude edited migration 0007 … and later edited applied migration 0004 in place." The order is reversed. 0004 was edited in S04 on 10-12, and 0007 in S10 on 11-05.
2. "Claude ran a script that does `docker compose down -v`" (also "ran a `docker compose down -v` script without warning you"). In S12 the `./scripts/reset-db.sh` call was rejected at the prompt and never ran.
3. "During the seed.sql update, Claude committed without being asked and announced a push, so you had to clean up after it." The S12 user rejected only the push and said "commit is fine but don't push". The later commit was approved, and there was nothing to clean up.
4. "In four sessions (fare preview, group discount, child half fares, and the general pricing pattern) Claude reached for floats". It happened in three sessions (S03, S07, S11), and the report says "three separate times" elsewhere.

**insights run 2**
1. "Claude ran a load script that does `docker compose down -v` … It then committed without being asked." The script call was rejected, and the S12 commit had the user's explicit OK ("commit is fine").
2. "Claude committed or tried to push without being asked in 3 sessions". There were two sessions: S07 (commit and push in one rejected call) and S12.
3. "The FD_ vs FERRYDESK_ mismatch came up in 4 sessions". It came up in three: S06, S10 and S13. The report's own closing note says "three separate sessions".
4. "Cancel ignores the 24h refund rule". No such rule exists in the code or the docs. In S13 the human said "I thought we had a 24h rule … that's a product call".
5. "Twice Claude started the server in the foreground … Both times you redirected it to httptest-based tests." In S02 the user asked for "a test", and the agent wrote a unit test of `Hold.Expired`, not an httptest test. In S06 the agent moved to the background on its own, without being redirected.

**insights run 3**
1. "Claude ran a setup script that does `docker compose down -v`". It was rejected and never ran.
2. "It also announced a commit and push you never requested" and "It also committed without being asked". The user approved the S12 commit.
3. "In three sessions Claude ran the server in the foreground … You had to redirect it to write an httptest test or regression test instead." There was no redirect in S06, where the agent itself restarted the server in the background.
4. The pasted prompt says "migrations in db/migrations are append-only". The directory is `migrations/` at the repo root, as every Glob in S04, S10 and S12 shows. A hook or CLAUDE.md built from this prompt would point at the wrong path.

**agent-friction**
1. Header: "2 human corrections". Humans corrected the agent in at least 7 sessions: S02 ("just write a test"), S03 (float `Fare`), S04 (0004), S07 (float discount), S10 (0007), S11 (dollars, then the foreground server) and S12 ("not that script!!"). The report's own file table also shows three user-correction rows.
2. "go test 3 sess". `go test ./...` failed on `internal/store` in four sessions: S01, S03, S09 and S11. One may sit in "Not counted", but the row as shown understates it.
3. "internal/pricing/fare.go … 1 sess, user-correction×1". Float money in `fare.go` was corrected in two sessions, S03 and S07. S03's correction is filed under `internal/booking/service.go` instead. This is an undercount, not a false claim.

## Grader notes

- **Coverage drives the insights scores.** All three runs score the same 11 and miss the same three items: P1, P2 (beyond the bare count) and P5. Each of those is visible only in tool output. Each run did read at least one session with direct evidence for P1 (S03, S11) and P5 (S07), so the misses are not only a sampling effect. P2's multi-match failures sit entirely in sessions the runs skipped. The runs do well on every item carried by the human's words (P3, P4, P6, P7, P8).
- **agent-friction has the opposite profile.** It is the only report that finds P2, and it lists P1, P5 and P6 from tool output. But it gives no mechanism for any of them, so each scores 1. It misses P8 entirely, even though "why is it still on 8080?" (S06) and "ugh, that prefix again" (S13) were in sessions it read. It scores 1 on P3 and P4, which the human explained at length, because of the "2 human corrections" header and because those corrections appear only as low-confidence rows.
- **agent-friction's P5 advice is questionable.** "Use `go vet`, not `make lint`" would hide the S05 case, where CI's golangci-lint errcheck findings were the task, and `go vet` does not run errcheck. The agent in S05 used `go run …golangci-lint@v1.61.0` instead.
- **The P2 insights score is a judgement call.** "Edit Failed 1" comes from a generic tool-error histogram. The key's literal "bare count of Edit failures … scores 1" puts it at 1. A grader who requires the count to be presented as friction would give 0, which gives the insights runs 10 each.
- **The P8 fix is borderline for run 2 and denied for run 3.** Run 2 tells a subagent to update README and `.env.example`. Run 3 asks only for a mismatch report "without applying it" and believes the README is fixed. In the transcripts the S10 README fix landed on `feat/waitlist`, and S13 (`fix/debug-logging`) still finds `FD_` in the README. No report proposes making `config.Load` warn on `FD_*` variables or accept them as an alias.
- **P7 is credited without repetition.** No insights run connects S04's `make db-reset` rejection (read by all three) to S12. Each gives a single instance, so "agents keep proposing it" is missing. That facet has no "must", so it costs no point. agent-friction quotes both S04 and S08 but splits them into separate one-off items.
- **The three insights runs are near-identical** in content, stats and fixes. They differ only in run 2's D2 and run 3's P8 fix. Their contradicted claims repeat across runs: the down -v script "ran", the S12 commit was "unasked", and some session counts are inflated.
