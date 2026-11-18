# blind4 answer key (kilnlog)

**Provenance.** An independent author wrote this dataset blind, before either tool (`/insights` or the
transcript analyser) was run on it. The author did not read either tool's source, tests, docs or
prompts, or any other dataset's answer key. The only outside input was the JSONL line shape, copied from
the first lines of one blind2 session file. The planted items reflect what friction looks like in a
small real Rust repo. None of them was chosen to suit a particular detector.

**Contents.**
- `repo/` is the repository snapshot before S01: 33 files. It is a cargo workspace with `kiln-core`
  (lib), `kilnctl` (CLI) and `kiln-hub` (axum 0.8 + sqlx/sqlite service).
- `make_sessions.py` is a deterministic, stdlib-only generator. All file tools in it run against an
  in-memory copy of `repo/` that changes across the sessions, so Read output, Edit errors, Grep hits and
  compiler line numbers agree with each other.
- `sessions/<sessionId>.jsonl` holds 15 main sessions. Two Task subagent sidechains are under
  `sessions/<sid>/subagents/`.
- `session-index.tsv` is a copy of the index below.

Assume the user's local time equals the UTC timestamps.

## Session index

| # | sessionId | start (UTC) | branch | shape | task |
|---|-----------|-------------|--------|-------|------|
| S01 | 32b0e700-a6fe-526a-aa97-f323aaa18736 | 2026-10-06 09:10 | feat/cones-list | single prompt | `kilnctl cones` table |
| S02 | 8958b03d-b4b2-5cf1-8247-1596753a3399 | 2026-10-08 10:20 | feat/ramp-validation | multi-turn | ramp-rate validation |
| S03 | a9cddce3-0d40-5d25-948b-630abad2e2fb | 2026-10-09 15:05 | main | single prompt | release binary size |
| S04 | 00ee659a-539c-5471-a620-9b17e43c05c1 | 2026-10-13 19:40 | feat/slow-cool | multi-turn | Glaze6SlowCool preset (TDD) |
| S05 | 354341df-d2af-50a1-9d51-0cdfc302a6c6 | 2026-10-15 11:30 | feat/plan-json | multi-turn | `plan --json` |
| S06 | cd8e4133-5d05-5fe2-a023-8464be57d35b | 2026-10-17 14:00 | feat/firing-notes | single prompt + typed rejection | notes column in hub |
| S07 | 7bff9e60-361d-595a-a0c7-bea2613d197f | 2026-10-20 09:45 | feat/export-csv | multi-turn | CSV export (core + hub) |
| S08 | a30749e4-5fff-5214-8c34-e5da0de2dfd1 | 2026-10-22 16:20 | chore/clap-bump | single prompt, background cmd | clap bump + release build |
| S09 | 051a0402-c618-50d1-8086-f40cb584a0ab | 2026-10-27 10:05 | feat/import | multi-turn + interrupt | `firings import` |
| S10 | db777cda-6c26-5992-95b5-602476a954ca | 2026-10-29 21:15 | refactor/cone-peak-name | multi-turn + subagent | rename `cone_to_celsius` |
| S11 | 942f228b-1eeb-5278-8ace-a9590e1662f2 | 2026-11-02 11:00 | feat/total-hold | multi-turn + interrupt | `total_hold`, cones_v1 check |
| S12 | 739859b7-9334-520d-a5af-e1edc819dfb8 | 2026-11-04 14:30 | feat/kiln-index | multi-turn + untyped rejection | firings index |
| S13 | 5d2b53e7-d55b-5bfb-962f-a3a740454386 | 2026-11-06 10:40 | release/v0.4.0 | single prompt + subagent + background | v0.4.0 release prep |
| S14 | 32dd3638-3d1b-5399-88eb-c4b0fae7dbdd | 2026-11-10 18:30 | feat/import-dry-run | multi-turn | `import --dry-run` |
| S15 | 8d518568-e464-51a3-b692-005aadc9f16f | 2026-11-13 09:20 | chore/clippy | single prompt | clippy fixes |

Subagents:
- S10 `agent-a3f81c2e`: search for `cone_to_celsius` usages.
- S13 `agent-b72d0e95`: compare the commits to CHANGELOG.

## Planted friction

### P1: kiln-hub cannot compile under the documented test command (tool output only)
- **What.** `CLAUDE.md` says the test command is `cargo test --workspace`. The committed `.env` sets
  `DATABASE_URL=sqlite://data/kilnhub.db`. The sqlx query macros read `.env` at compile time, so they
  go online and try to open that file. The file does not exist in a fresh checkout because
  `data/*.db` is gitignored and only `just db-setup` creates it. Every compile of kiln-hub then fails
  with `error returned from database: (code: 14) unable to open database file` on each `sqlx::query_as!`.
  - The repo has the fix already. The `justfile` `test` and `lint` recipes, and the `env` block in
    `.github/workflows/ci.yml`, set `SQLX_OFFLINE=true`, which uses the committed `.sqlx/` cache. The
    `test` recipe even carries the comment "no database needed".
  - `CLAUDE.md` mentions neither `just test` nor `SQLX_OFFLINE`, and `release-build` does not set it.
- **Behaviour.** No human ever mentions it.
  - The agent runs bare `cargo test --workspace` (or `cargo build` of the hub) again and again. Each
    time it shrugs the failure off as "unrelated" and narrows to `-p kiln-core [-p kilnctl]`, so hub
    changes go untested.
  - S06 diagnoses part of the cause, but the result is never written down anywhere. S13 finds the
    `SQLX_OFFLINE` workaround and suggests it for one recipe only.
- **Sessions.**
  - Bare workspace test fails: S02, S05, S09, S12.
  - Hub build fails: S06, S07, S10, and S13 (release build).
  - In S06 the offline attempt fails too, because the changed queries are missing from the cache.
- **Found means.**
  - Names kiln-hub's sqlx compile-time failure, "unable to open database file", as a recurring cause
    of failed test/build commands across sessions.
  - Ties it to `DATABASE_URL` in `.env` pointing at a database that does not exist, versus
    `SQLX_OFFLINE=true` / the `.sqlx` cache.
  - Notes that the documented command differs from `just test` / CI.
  - Notes the effect: the hub goes untested and is narrowed away every time.
  - "Tests sometimes fail in hub" with no mechanism counts as partial.
- **Fix means.** Any one of these:
  - Change CLAUDE.md's test/lint/build guidance to `just test` / `SQLX_OFFLINE=true`.
  - Add `[env] SQLX_OFFLINE = "true"` to `.cargo/config.toml`.
  - Remove `DATABASE_URL` from the compile-time `.env`, or document `just db-setup`.
  - Plus: make `release-build` set `SQLX_OFFLINE`.

### P2: wall-clock-dependent core test fails in evening sessions (tool output only)
- **What.**
  - `kiln-core/src/schedule.rs::tests::eta_same_day_for_short_bisque` uses `Local::now()` and asserts
    that a 6h14m fast bisque ends on the same calendar date it starts. The assertion is false after
    about 17:46 local time.
  - The failure output shows it directly: `left: <tomorrow>` / `right: <today>`.
- **Behaviour.**
  - It fails only in evening sessions and passes in every morning or afternoon run (S02, S05, S07, S09,
    S11, S15).
  - Each time the agent calls it "flaky", "pre-existing" or "unrelated" and moves on.
  - In S14 it re-runs the test, sees the same failure, and still leaves it. Because cargo stops at the
    first failing package, S14 also has to run the kilnctl tests separately.
- **Sessions.** S04 (19:40), S10 (21:15), S14 (18:30, fails twice).
- **Found means.**
  - Names `eta_same_day_for_short_bisque` (or "the ETA same-day test") as recurring.
  - Identifies the time-of-day / `Local::now()` dependence, or at least that it fails consistently in
    evening runs with a next-day date. Not random flakiness.
  - Notes that the agent dismissed it as flaky without investigating.
- **Fix means.**
  - Make the test deterministic: a fixed start time (`Local.with_ymd_and_hms(..)` or a naive
    datetime), or assert on duration instead of on the date.
  - Or record the known issue in CLAUDE.md until it is fixed.

### P3: release builds exceed the 2-minute Bash timeout (tool output only)
- **What.**
  - `[profile.release]` in the root `Cargo.toml` uses `lto = "fat"` and `codegen-units = 1`.
  - Release builds take about 4 minutes (kilnctl) and 6.5 minutes (workspace).
  - `just check-size` and `just release-build` both run release builds, and CLAUDE.md says nothing
    about it.
- **Behaviour.** The first attempt in each session uses the default Bash timeout and dies with
  `Command timed out after 2m 0.0s`. The agent then retries with `timeout: 600000` (S03) or with
  `run_in_background` plus BashOutput polling (S08, S13). This wastes 2 minutes each time, and no
  human comments.
- **Sessions.** S03, S08, S13. All three are single-prompt sessions.
- **Found means.**
  - Names the repeated release-build timeouts (`just check-size` / `cargo build --release` /
    `just release-build`).
  - Attributes them to the fat-LTO / single-codegen-unit release profile, or at least to "release build
    takes over 2 minutes", not to random hangs.
- **Fix means.**
  - Document in CLAUDE.md that release builds take about 4 to 7 minutes: run them in the background or
    with a 10-minute timeout.
  - Or add a faster profile (for example `release-fast` with thin LTO) for local size checks.

### P4: "no unwrap/expect in kiln-core" convention (human words only)
- **What.** A library-crate convention that is not in CLAUDE.md: kiln-core never panics. Return
  `KilnError` or use infallible code, never `unwrap`/`expect`. No tool or lint enforces it.
- **Sessions.**
  - S02, first statement: "pls no unwrap/expect in kiln-core, it's a library — return KilnError. the cli
    can unwrap if it wants, core never panics".
  - S07, repeated with irritation: "again with the .expect( in core... we talked about this".
  - S11, indirect and polite: "thanks, but could you do it without the unwrap? same as last time".
- **Found means.**
  - Identifies the recurring correction about `unwrap`/`expect` in kiln-core (library code) across at
    least two sessions.
  - Recognises it as a standing project convention the agent keeps forgetting.
- **Fix means.** Add it to CLAUDE.md, ideally with the reason and the CLI exception. Enforcing it with
  `#![deny(clippy::unwrap_used, clippy::expect_used)]` in `kiln-core/src/lib.rs` (outside tests) is a
  stronger fix.

### P5: never edit an applied migration (human words, delivered via rejections)
- **What.** The agent tries to change `crates/kiln-hub/migrations/0002_firings.sql`, a migration that
  has already shipped, instead of adding a new numbered migration.
- **Sessions.**
  - S06: the Edit is rejected with a typed reason: "no — never edit a migration that's already been
    applied, the studio box ran 0002 months ago. add a new one".
  - S12: the same mistake for an index. The Edit is rejected without a reason (`[Request interrupted by
    user for tool use]`), then: "same thing as before.. new file pls, we never touch old migrations once
    they shipped".
- **Found means.** Names the rule "add a new migration, don't modify existing/applied ones (0002)", and
  notes that it was violated again in a later session after being stated.
- **Fix means.** Add the rule to CLAUDE.md, for example "migrations are append-only; add
  `NNNN_description.sql`". A PreToolUse hook or permission deny on edits to existing migration files is
  a stronger fix.

### P6: CHANGELOG entry for every CLI change (human words only)
- **What.** The user wants a line under `## [Unreleased]` in `CHANGELOG.md` whenever the CLI changes.
  CLAUDE.md does not say so.
- **Sessions.**
  - S05, stated in non-native phrasing: "please you always add line in CHANGELOG under Unreleased when
    you change the CLI, I forget it otherwise".
  - S09, with a typo: "you forgot chnagelog again".
  - S14, terse: "nice. changelog?".
  - S11 also changes CLI output (the holds line) with no entry, and nobody remarks on it. The S13
    subagent later finds it missing among other entries.
- **Found means.** Identifies the repeated reminder to update CHANGELOG.md (Unreleased) after CLI or
  user-facing changes, across sessions.
- **Fix means.** Add it to CLAUDE.md, for example: "Any user-visible change to kilnctl/kiln-hub: add a
  line under `## [Unreleased]` in CHANGELOG.md".

### P7: claimed success without running the affected code (mixed)
- **What.** In S07 the agent adds the hub route as `"/firings/:id/export"`. That is axum 0.7 syntax, and
  axum 0.8 panics on it at router build. The agent runs only `cargo test -p kiln-core` and reports
  "Done. The export endpoint is wired up and tests pass."
  - **Tool side:** no hub build or run took place. P1 makes the hub impossible to compile in the
    agent's environment, and the agent never mentions that before claiming success.
  - **Human side:** the user pastes the startup panic: "you said it works but hub panics on start …
    did you actually run it?"
- **Echo.** S10 repeats the pattern in a smaller form. The agent says the rename is done after testing
  only kiln-core. See P8.
- **Sessions.** S07 is primary. S10 is secondary.
- **Found means.**
  - Names the S07 false "tests pass"/"works" claim for the export endpoint.
  - Notes that only kiln-core tests ran and the hub was never compiled or run, and that the user found
    the axum `:id` vs `{id}` route panic.
  - Linking it to P1 (the hub can't build, so it gets skipped silently) is a bonus.
- **Fix means.**
  - CLAUDE.md or a working rule: don't claim a change works for a crate that wasn't built or tested;
    say explicitly what was not verified.
  - Make the hub verifiable (P1 fix).
  - Note axum 0.8's `{param}` route syntax in CLAUDE.md.

### P8: subagent search too narrow; main agent trusted it (mixed, subagent)
- **What.** In S10 the main agent delegates "find every usage of `cone_to_celsius` in the workspace" to
  a Task subagent (`agent-a3f81c2e`).
  - The subagent runs a single Grep with `path: /work/kilnlog/crates/kiln-core`, then reports "All
    usages … are in `crates/kiln-core` … No other usages found".
  - The main agent renames on that basis without checking. It runs only the kiln-core tests and
    declares the rename done.
  - kilnctl (`commands/cone.rs`, `commands/plan.rs`) and kiln-hub (`routes.rs`) still use the old name.
- **Tool side:** the sidechain's Grep path argument. **Human side:** the user pastes `error[E0432]:
  unresolved import kiln_core::cone_to_celsius` and asks "didnt you check the cli crate? and hub uses it
  too".
- **Sessions.** S10, main transcript plus sidechain.
- **Found means.**
  - Names the S10 rename breakage.
  - Attributes it to the subagent's search being scoped to kiln-core only, despite a workspace-wide
    prompt, and to the main agent accepting the "no other usages" report without verification.
  - "Rename broke build" without the subagent/scope mechanism counts as partial.
- **Fix means.**
  - For renames, grep the whole workspace (or use `cargo check --workspace`) before declaring done, and
    verify subagent search results.
  - Keep `cargo build -p kilnctl` / the P1 fix in the done-checklist.
  - Optionally a CLAUDE.md note that kiln-core's public API is used by both binaries.

## Decoys (should NOT be reported as friction)

### D1: red-then-green TDD (S04)
The user asks for tests first. The agent adds `glaze6_slow_cool_has_controlled_cooling`, runs it, and
gets the expected `E0599 no variant ... Glaze6SlowCool` compile failure. It then implements the preset
and the test passes. This is a planned failure. (The other failure in the same run is P2, which is
separate.)

### D2: transient network failure (S01)
The first `cargo build -p kilnctl` fails with `Could not resolve host: index.crates.io` while fetching
the new comfy-table dependency. A plain retry succeeds and the failure never recurs.

### D3: the human changes their mind (S11)
The user asks for Fahrenheit as the default. The agent starts the change, the user interrupts, then
says "actually no sorry, keep celsius default … revert that pls", and the agent reverts. This is a
normal change of plan, not a correction of agent behaviour. The interrupt is the user's own redirect.

### D4: exploratory search that finds nothing, as expected (S11)
"can you double check nothing still reads cones_v1.csv?" returns `No files found`, which is the desired
answer.
- Similar expected-empty searches: S02 (no other `unwrap`/`expect` left in core) and S07 (no other
  `:`-style routes).
- S10's final grep is also expected-empty, but it belongs to P8's recovery and is not a decoy.

### Other ordinary noise (not planted)
- S06: an Edit fails with `Found 2 matches of the string to replace` and is redone with `replace_all`.
- S15: an Edit fails with `String to replace not found in file.` because the agent guessed the line
  layout; it re-reads and fixes it.
- S09: the user interrupts to rename the subcommand to `firings import` (a scope clarification).
- S15: ordinary clippy errors fixed on first pass. Release-build smoke tests, TodoWrite use, and the
  `sqlite3` checks in S12 are normal work.
- S13: the background build's BashOutput reports `exit_code 0` even though kiln-hub failed, because the
  command was piped through `tail`. The agent read the output correctly. It is part of P1/P3, not a
  separate item.
