# Answer key: ferrydesk (blind set 2)

> **Provenance.** Written by a second independent author (a separate model session) told not
> to read agent-friction's code, tests, docs, examples or the first blind set, and committed
> before agent-friction or `/insights` was run on these sessions. agent-friction's changes
> after the first blind set were finished before this key was read by anyone working on it.
> Nothing here was changed after the runs.

Repo: `ferrydesk`. It is a Go 1.23 HTTP/JSON booking backend for ferry sailings, with chi, pgx, sqlc and golang-migrate.
cwd in transcripts: `/work/ferrydesk`. Snapshot before the first session: `repo/`.
The sessions come from `make_sessions.py`, which writes 13 main transcripts to `sessions/`. Two of them also have a Task
subagent sidechain under `sessions/<sid>/subagents/`.

## Session index

| Key | Session id | Branch | Shape |
|-----|-----------|--------|-------|
| S01 | 1687e1bc-ecf5-5453-99ad-7427eca72e4d | feat/remaining-seats | single prompt |
| S02 | 84a5b3a0-f1ed-5a6c-8d56-869ae9828b17 | fix/hold-expiry | 4 human turns |
| S03 | 40d8ce86-743e-5528-a353-4820eebd611b | feat/child-fares | 3 human turns |
| S04 | 7631305f-d0e5-5137-911c-a5bd3be12a9e | feat/refund-reason | 2 turns + rejection with reason |
| S05 | 5cc47008-c461-5020-ae56-3811b9a26b9e | chore/errcheck-api | single prompt |
| S06 | 9edec11d-3708-557f-a228-a7824c216492 | main | 3 turns |
| S07 | 90d6e85c-d933-583a-b817-1b1f7f94442f | feat/group-discount | Task subagent, rejection + interrupt marker |
| S08 | 24cbd043-fa9d-5534-9b29-3b4a8722446d | fix/cancel-twice-409 | single prompt + rejection with reason |
| S09 | 47d3ec4a-5bcc-5d59-bde0-85a6ed6fa20a | feat/sailing-search | single prompt |
| S10 | 3d08923d-167c-5159-a7c7-b3ec00cb43db | feat/waitlist | 4 turns |
| S11 | 63fda14f-2b34-568c-8920-3ce46e7bdd64 | feat/fare-preview | 4 turns, user interrupt |
| S12 | ff5b646d-5f92-51b2-9506-585f14495961 | chore/seed-refresh | Task subagent, two rejections |
| S13 | 224ca764-7cf3-5344-895e-2221c9b7aead | fix/debug-logging | 4 turns, user interrupt |

## Planted items

### P1: `go test ./...` always fails on the Postgres integration tests (tool output only)
- **Planted:** `internal/store/postgres_test.go` opens a real Postgres at `FERRYDESK_DATABASE_URL`, or localhost:5432 when that is unset. It has no build tag and does not skip when the database is missing; it calls `t.Fatalf` instead. Every full test run fails with `--- FAIL: TestCreateAndGetBooking` / `TestActiveHolds`, `dial tcp 127.0.0.1:5432: connect: connection refused`, and `FAIL .../internal/store`, Exit code 1. Agents then re-run a subset of packages. `make test` is just `go test ./...`. No human ever mentions it.
- **Sessions:** S01, S03, S09, S11.
- **Found means:** names `internal/store` (postgres_test.go) or the store integration tests, and the mechanism: they need a live Postgres, are not gated, and fail with connection refused, so the default `go test ./...` / `make test` is always red locally.
- **Fix means:** gate them behind `//go:build integration` or `t.Skip` when no DB/env var is set (or use testcontainers), and add a `make test-unit`/`test-integration` split. Documenting the split for agents (CLAUDE.md/README) also counts.

### P2: edits to the 1,170-line `internal/api/handlers.go` keep failing (tool output only)
- **Planted:** every public and admin handler sits in one file (`handlers.go`, about 1,170 lines), and each one repeats the same error-response boilerplate: `if err != nil { writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"}) ... }`, the `invalid id` parse block, the `not found` block and `w.WriteHeader(http.StatusNoContent)`. Agents read partial ranges and then their `Edit` fails, either with `Found N matches of the string to replace, but replace_all is false` (40, 18 and 8 matches) or with `String to replace not found in file`, because the snippet was recalled from a different handler or used spaces instead of tabs. Each failure forces a re-read with an offset and a retry.
- **Sessions:** S01 (multi-match), S02 (not found), S05 (multi-match, two not found), S08 (multi-match, then not found).
- **Found means:** names `internal/api/handlers.go` specifically, and the mechanism: one huge file with near-identical repeated blocks makes old_string ambiguous or missed, so Edits fail repeatedly. A bare count of "Edit failures" without the file or the duplication cause scores 1.
- **Fix means:** split handlers.go per resource (sailings/bookings/admin/reports) and pull the repeated responses into helpers (`badRequest`, `internalError`, `notFound`, the `adminError` that S05 started), so each edit site is unique and short.

### P3: agents edit already-applied migrations in place (human words only)
- **Planted:** the repo has no note that migrations are immutable once released, and nothing (no CLAUDE.md, CONTRIBUTING, CI check) enforces it. Agents change the newest existing migration instead of adding a new numbered file. The tools all succeed; only the human says why it is wrong: "don't change 0004. that's been applied on staging and everyone's laptop ... breaks the checksum" and "nope, 0007 went out with last week's release ... (honestly we should write this rule down somewhere)".
- **Sessions:** S04 (0004_add_refunds), S10 (0007_refund_reason).
- **Found means:** names `migrations/` and the rule: applied migrations must not be edited, and a new numbered migration must be added. It must also say that agents repeatedly violate this because it isn't written down or checked.
- **Fix means:** write the rule down where agents read it (CLAUDE.md/CONTRIBUTING/README "Migrations" section), and optionally add a CI check that rejects diffs to existing `migrations/*.sql` files.

### P4: money must stay integer cents, but agents keep using floats (human words only, subtle)
- **Planted:** `internal/pricing/money.go` defines `type Cents int64` and `ApplyBasisPoints` (round half up), and the API emits `*_cents` integers. That convention is visible only in a code comment. It isn't documented anywhere an agent would look first, and nothing like a linter or forbidigo rule stops float use in `pricing`. Agents keep reaching for float math:
  - S03 makes `Fare` return float64.
  - S07 writes `Cents(math.Round(float64(total) * 0.85))`.
  - S11 returns `"total": float64(total)/100` dollars.
  Humans correct each one ("everything money-related here is int64 cents", "no floats in money math please, we've been through this", "why dollars? every other endpoint returns cents as an int").
- **Sessions:** S03, S07, S11.
- **Found means:** names the cents/`pricing.Cents` convention (pricing package / money handling, or API `total_cents`) and the mechanism: agents repeatedly use float64 or dollars for money and humans repeatedly correct them. "User corrected the agent several times" without the money/cents target scores 1.
- **Fix means:** document the convention (CLAUDE.md/CONTRIBUTING: "all money is `pricing.Cents`; percentages via `ApplyBasisPoints`/`Discount{Bps}`; API fields are `*_cents` ints"), and optionally add a lint rule banning `float64` in `internal/pricing`.

### P5: `make lint` cannot run, because `bin/golangci-lint` is never installed (tool output only)
- **Planted:** the `lint` target in the `Makefile` calls `$(GOLANGCI)` = `bin/golangci-lint` and does not depend on `tools`, so on a fresh checkout it fails with `make: bin/golangci-lint: No such file or directory` / `make: *** [Makefile:14: lint] Error 127` (Exit code 2). Agents fall back to `go vet`, `gofmt -l`, or a slow `go run ...golangci-lint@v1.61.0`. Only CI runs `make tools` first. Humans never mention the binary; S05's human mentions CI lint findings, which is a different thing.
- **Sessions:** S01, S05, S07, S09.
- **Found means:** names `make lint` / the Makefile `lint` target and the mechanism: it calls a repo-local `bin/golangci-lint` that is never installed because `lint` doesn't depend on `tools`, so it fails with exit 127/2.
- **Fix means:** make `lint` depend on `$(GOLANGCI)` with a rule that installs it (or `go run` the pinned version, or use `go tool` in go.mod), and/or document `make tools` as a setup step.

### P6: agents run the HTTP server in the foreground and it hangs until timeout (tool output and human words)
- **Planted:** the only documented way to exercise the API is `make run` / `go run ./cmd/ferrydesk`, which never exits. There's no smoke-test script, documented background-run recipe or httptest harness for end-to-end checks. Agents run it in the foreground three times:
  - S02: `go run ./cmd/ferrydesk` times out after 2m.
  - S06: `FD_PORT=9090 go run` times out after 20s.
  - S11: `make run` times out after 2m. The user then interrupts and says "stop running the server in the foreground, it never exits".
  S02's human says "yeah it's a server, it doesn't exit lol".
- **Sessions:** S02, S06, S11.
- **Found means:** names the server entrypoint (`make run` / `go run ./cmd/ferrydesk`) and the mechanism: it is a long-running process started in the foreground, so the Bash call times out, and agents have no documented non-blocking way to verify endpoints.
- **Fix means:** document how to verify changes (httptest-based handler tests, or "start the server in the background and curl /healthz"), or add a `make smoke` target that starts the server, curls it and stops it. Agent instructions saying "never run the server in the foreground" also count.

### P7: the only DB setup path is destructive, and agents keep reaching for it (rejections with human reasons)
- **Planted:** `make db-reset` → `scripts/reset-db.sh` runs `docker compose down -v`, which deletes the `pgdata` volume holding the developer's curated seed and QA data. There's no separate test or scratch database and no non-destructive setup target. Agents reach for it, or for raw `docker compose down -v`, and each attempt is rejected:
  - S04: `make db-reset`, rejected with the reason "db-reset wipes my local data ... just migrate up".
  - S08: `docker compose down -v && ...`, rejected with the reason "please stop wiping the db volume ... use a separate one".
  - S12: `./scripts/reset-db.sh`, rejected with no reason, followed by an interrupt marker and then "not that script!! it does docker compose down -v and I lose everything".
- **Sessions:** S04, S08, S12.
- **Found means:** names `make db-reset` / `scripts/reset-db.sh` / `docker compose down -v` and the mechanism: it is destructive to the shared local DB volume, and agents keep proposing it because it is the obvious "get a clean DB" path.
- **Fix means:** provide a non-destructive path, such as a separate test database or compose service, or a `make test-db` that does createdb/migrate/seed on a throwaway DB (the recipe S12 ended up using). Alternatively guard or rename the reset target (confirmation prompt, `make db-nuke`) and document "never reset the dev DB" for agents. A deny rule in permissions for `docker compose down -v` / `reset-db.sh` also counts.

### P8: docs say `FD_*` env vars, but the code reads `FERRYDESK_*` (tool output and human words, subtle)
- **Planted:** `internal/config/config.go` has `envPrefix = "FERRYDESK_"`, while the `README.md` configuration table and `.env.example` document `FD_PORT`, `FD_DATABASE_URL`, `FD_LOG_LEVEL` and `FD_HOLD_TTL`. Unknown variables are silently ignored. The symptoms differ in each session:
  - S06: the server log says `addr=:8080` despite `FD_PORT=9090`, and the human asks "why is it still on 8080?".
  - S10: the human reports that Priya set `FD_DATABASE_URL` and kept hitting the default DB.
  - S13: `FD_LOG_LEVEL=debug` produces no debug logs, and the human says "ugh, that prefix again".
  Fixes are partial and land on different branches: S06 fixes only .env.example, S10 fixes the README, and S13's branch still has the old text.
- **Sessions:** S06, S10, S13.
- **Found means:** names the env var prefix mismatch (README/.env.example `FD_` vs `FERRYDESK_` in `internal/config`) and the fact that wrong names are silently ignored.
- **Fix means:** align the docs and `.env.example` with the code, accept `FD_` as an alias, and/or make `config.Load` warn or fail on `FD_*` (unknown prefixed) variables.

## Decoys (not repository problems; raising one as a thing to fix is a false positive)

- **D1: one-off command typo (S09).** `go biuld ./...` gives `go biuld: unknown command`, Exit code 2. The agent corrects it on the next call. It happens once, and the cause is the agent, not the repo.
- **D2: user interrupt for a change of priorities (S13).** The user interrupts ("actually park that, payments just pinged about refunds on prod") and asks an unrelated question. The answer (no 24h cancel cutoff) is explicitly a product decision the human will take elsewhere. Neither the interrupt nor the missing cutoff is repo friction to fix.
- **D3: expected red test (S02).** `go test ./internal/booking/ -run TestExpired` fails with Exit code 1 because the agent wrote a regression test before the fix (TDD); it passes after the fix. This is correct workflow, not a failing command.
- **D4: user declines `git push` (S07, S12).** Push is rejected in two sessions: once with no reason and then "I'll push it myself once I've looked at the diff", once with the reason "commit is fine but don't push, I push my own branches". This is a personal review/ownership preference and harmless. At most it is a permissions-preference note, not a repository defect. Treating it as a recurring repo problem to fix is a false positive. Saying "don't push without asking" in agent instructions is acceptable as an aside but earns no credit.

Other noise that is not planted: the S03 `go build` type error and the S04 `undefined: pgtype` error, both caused by the agent's own in-progress edits and fixed at once; the S05 `which golangci-lint` exit 1 (part of P5, not separate); and the S06 20s timeout (counted under P6).

## Rubric

Score each planted item separately:

- **2:** names the specific target (file, target, convention) and the mechanism, as given under "Found means".
- **1:** symptom or count only (e.g. "many Edit failures", "tests fail often", "user corrected the agent several times"), or the right theme on the wrong target (e.g. blames sqlc-generated `internal/db` for the edit failures, or calls P7 "Docker is flaky").
- **0:** missed.

**Fix** is scored separately for each item, yes or no: does the report propose a remedy matching "Fix means", or an equivalent?

**False positives:** each decoy (D1-D4) reported as a repository problem to fix counts as one false positive. Mentioning one neutrally, or explicitly dismissing it, is fine.

Max found score: 16 (8 items × 2). Max fix score: 8.
