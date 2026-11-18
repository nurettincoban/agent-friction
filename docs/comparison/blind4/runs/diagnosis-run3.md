# Diagnosis: kilnlog

From `.agent-friction/evidence.md` (15 sessions, 2026-10-06 to 2026-11-13), the transcripts
themselves, and this repository. Nothing here has been applied. No fix below was built or run:
this machine cannot download crates, so `cargo` does not get as far as compiling.

## 1. The documented build and test commands cannot compile kiln-hub

**Seen in:** S02, S05, S06, S07, S09, S10, S12, S13 (8 of 15 sessions) · `cargo test --workspace`,
`cargo build --workspace`, `cargo build -p kiln-hub` and `just release-build` failed each time
**What happens:** `.env:3` sets `DATABASE_URL=sqlite://data/kilnhub.db`. The `sqlx::query_as!`
macros in `crates/kiln-hub/src/routes.rs:34,55,69` read it at compile time and try to open that
database, which is git-ignored (`.gitignore`: `/data/*.db`) and absent from a fresh checkout. The
repository already has the answer, a committed `.sqlx/` query cache, but only `just test`,
`just lint` (`justfile:8,12`) and CI (`.github/workflows/ci.yml:9`) set `SQLX_OFFLINE=true`.
`CLAUDE.md:11-13` tells agents to run the bare `cargo build --workspace` / `cargo test --workspace`
/ `cargo clippy`, and `just release-build` (`justfile:35`) does not set it either.
**Evidence:** `error: error returned from database: (code: 14) unable to open database file`
(S02, S05, S06, S07, S09, S10, S12, S13). In S13 the agent found the cause itself:
`SQLX_OFFLINE=true cargo build --release --workspace` then finished.
**Why it keeps happening:** CLAUDE.md contradicts the justfile and CI. This is not a flaky test,
although the scan calls it one: the "re-run that passed" was each time a narrower command
(`cargo test -p kiln-core` or `-p kiln-core -p kilnctl`) that leaves kiln-hub out. That habit has
a cost of its own:
- S06, S07 and S12 changed kiln-hub and ended without ever compiling it.
- S07: the agent reported done; the hub panicked on start (human: "you said it works but hub
  panics on start […] did you actually run it?").
- S10: a rename was checked with `cargo test -p kiln-core` only and broke the other two crates
  (human: "didnt you check the cli crate? and hub uses it too").
- S06: after a query changed, offline mode failed too (`` `SQLX_OFFLINE=true` but there is no
  cached data for this query ``), and `sqlx-cli`, needed by `just sqlx-prepare`, was not installed.
**Fix:**
- `.cargo/config.toml`, add (so every cargo command uses the committed cache; a value set in the
  shell still wins, so `just sqlx-prepare` is unaffected):
  ```toml
  [env]
  SQLX_OFFLINE = "true"
  ```
- `CLAUDE.md`, under Commands:
  ```markdown
  - kiln-hub's SQL is checked at compile time against the committed `.sqlx/` cache
    (`SQLX_OFFLINE=true`, set in `.cargo/config.toml`). After changing a query or a migration,
    run `just db-setup && just sqlx-prepare` (needs sqlx-cli, see README) and commit `.sqlx/`.
  - Before saying a change is done, build and test the whole workspace, not one crate. After
    changing kiln-hub routes, start it once (`just hub`) and hit the route.
  ```
**Confidence:** high for the cause (read in `.env`, `justfile`, `ci.yml`, and S13 shows the
setting working); medium for the `[env]` fix, which I could not build here.

## 2. Agents forget the CHANGELOG entry for CLI changes

**Seen in:** S05, S09, S14 (3 of 15 sessions)
**What happens:** A CLI change is finished without a line under `## [Unreleased]`
(`CHANGELOG.md:5`), and the human has to ask for it.
**Evidence:** S05: "please you always add line in CHANGELOG under Unreleased when you change the
CLI, I forget it otherwise and then release notes are empty" · S09: "you forgot chnagelog again"
· S14: "nice. changelog?"
**Why it keeps happening:** Not written down: `CLAUDE.md` does not mention the changelog.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Any user-visible change to kilnctl or kiln-hub gets a line under `## [Unreleased]` in
    `CHANGELOG.md`, in the same change.
  ```
**Confidence:** high. The human asked for CLI changes; extending it to kiln-hub is my reading of
`CHANGELOG.md:3` ("All notable changes to kilnctl and kiln-hub").

## 3. `unwrap` / `expect` keeps appearing in kiln-core

**Seen in:** S02, S07, S11 (3 of 15 sessions)
**What happens:** New kiln-core code panics on error instead of returning `KilnError`
(`crates/kiln-core/src/error.rs:4`).
**Evidence:** S02: "pls no unwrap/expect in kiln-core, it's a library — return KilnError. the cli
can unwrap if it wants, core never panics" · S07: "again with the .expect( in core... we talked
about this. KilnError or make it infallible." · S11: "could you do it without the unwrap? same as
last time."
**Why it keeps happening:** `CLAUDE.md:21` only says "`thiserror` in libraries", which does not
forbid panics, and nothing checks it. kiln-core has no `unwrap(`/`expect(` today, so a lint can be
switched on without other changes.
**Fix:**
- `crates/kiln-core/src/lib.rs`, after the doc comment (line 5); CI's clippy with `-D warnings`
  then fails on it:
  ```rust
  #![cfg_attr(not(test), deny(clippy::unwrap_used, clippy::expect_used))]
  ```
- `CLAUDE.md`, replace line 21:
  ```markdown
  - Errors: `thiserror` in libraries, `anyhow` in binaries. kiln-core never panics: no
    `unwrap`/`expect` outside tests, return `KilnError` (clippy enforces it).
  ```
**Confidence:** high.

## 4. A kiln-core test fails every evening

**Seen in:** S04, S10, S14 (3 of 15 sessions) · `cargo test -p kiln-core` failed 4 times
**What happens:** `eta_same_day_for_short_bisque` (`crates/kiln-core/src/schedule.rs:109-114`)
starts a 374-minute firing at `Local::now()` and asserts it ends the same day. It fails whenever
the tests run after 17:46 local time. The three sessions started at 19:40, 21:15 and 18:30.
**Evidence:** S14: `assertion `left == right` failed  left: 2026-11-11  right: 2026-11-10`; the
same in S04 and S10. Each agent set it aside (S04: "it looks flaky"; S10 and S14: pre-existing and
unrelated).
**Why it keeps happening:** The test depends on the wall clock, and nobody was asked to fix it. In
S10 the real breakage (problem 1) was hidden behind this "known" red test.
**Fix:**
- `crates/kiln-core/src/schedule.rs`: line 88 `use chrono::Local;` → `use chrono::Utc;`, and line
  111:
  ```rust
  let start = Utc.with_ymd_and_hms(2026, 1, 15, 8, 0, 0).unwrap();
  ```
**Confidence:** high (the dates in the failures and the session times match the 374 minutes
asserted at `schedule.rs:93`).

## 5. Release builds run past the 2-minute command limit

**Seen in:** S03, S08, S13 (3 of 15 sessions) · `just check-size`, `cargo build --release -p
kilnctl`, `just release-build`
**What happens:** `Cargo.toml:27-29` builds release with `lto = "fat"` and `codegen-units = 1`.
In S13 the full release build took 6m 31s; the default limit for a command is 2 minutes.
**Evidence:** `Command timed out after 2m 0.0s` (S03, S08, S13). Each time the agent ran it again
and it finished, so the cost is one lost build per session.
**Why it keeps happening:** Not written down. The profile is deliberate (`Cargo.toml:26`), so it
should stay.
**Fix:**
- `CLAUDE.md`, under Commands:
  ```markdown
  - Release builds (`just release-build`, `just check-size`) take up to 7 minutes (fat LTO): run
    them in the background or with a 10-minute timeout.
  ```
**Confidence:** medium: three sessions, one retry each.

## 6. Agents edit a migration that has already been applied

**Seen in:** S06, S12 (2 of 15 sessions) · Edit of `crates/kiln-hub/migrations/0002_firings.sql`
declined both times
**What happens:** Asked for a new column (S06) or an index (S12) on `firings`, the agent edits the
`CREATE TABLE` in 0002. The hub runs `sqlx::migrate!` at start (`crates/kiln-hub/src/main.rs:16`),
which refuses to start when an applied migration's content has changed, and the studio box applied
0002 months ago.
**Evidence:** S06: "no — never edit a migration that's already been applied, the studio box ran
0002 months ago. add a new one" · S12: "same thing as before.. new file pls, we never touch old
migrations once they shipped"
**Why it keeps happening:** Not written down anywhere (`CLAUDE.md`, `README.md`).
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Never edit a file in `crates/kiln-hub/migrations/` once it is committed: the studio box has
    applied it. Schema changes go in a new `NNNN_name.sql`.
  ```
- `.claude/settings.json` (new file), so the boundary holds; add each new migration to the list:
  ```json
  {
    "permissions": {
      "deny": [
        "Edit(/crates/kiln-hub/migrations/0001_init.sql)",
        "Edit(/crates/kiln-hub/migrations/0002_firings.sql)",
        "Edit(/crates/kiln-hub/migrations/0003_kiln_capacity.sql)"
      ]
    }
  }
  ```
**Confidence:** high for the rule; the deny-rule path syntax is from memory, check it once.

## Looked at, not a problem

- **The scan's "ready to paste" line (`routes.rs`: use `without_v07_checks`): do not paste it.**
  That phrase is from the axum panic message the human quoted in S07. The right form is the
  `{id}` capture the file already uses (`routes.rs:15,17`); the S07 agent fixed it that way. It
  happened once and problem 1's "start the hub" line covers it.
- S09 "make it `kilnctl firings import`" and S11 "keep celsius default": the human changed their
  mind; both interruptions on `crates/kilnctl/src/cli.rs` come from these.
- S04 "can kilnctl plan use it too?": a follow-up request.
- S01 `cargo build`: `Could not resolve host: index.crates.io`, a network failure that passed on
  retry.
- S06 edit miss in `routes.rs` (the same SELECT list is at lines 36 and 71) and S15 edit miss in
  `commands/firings.rs`: one each.
- S11 search for `cones_v1` found nothing: that was the answer wanted.
- S04 first red test run and S15 clippy failure: the work asked for (tests first; fix clippy).
- `commands/mod.rs`, `main.rs`, `schedule.rs` "probable correction" in S09: the changelog reminder
  of problem 2, counted against every file edited before it.

## Measure it

After the fixes, `/agent-friction:compare` in a week or two shows whether these failures went down.
