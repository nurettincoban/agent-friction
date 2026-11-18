# Diagnosis: kilnlog

From `.agent-friction/evidence.md` (15 sessions, 158 tool calls, 2026-10-06 to 2026-11-13),
checked against the repository as it is now. Nothing was built while diagnosing: this
machine has no crates downloaded and neither `just` nor `sqlx` installed, so the mechanisms
below are confirmed by reading the files, not by reproducing the failures.

## 1. The documented build and test commands cannot compile kiln-hub

**Seen in:** S02, S05, S09, S12 (`cargo test --workspace`) and S06, S07, S10, S13
(`cargo build`) (8 of 15 sessions)
**What happens:** `.env:3` sets `DATABASE_URL=sqlite://data/kilnhub.db`, and the sqlx
`query_as!` macros in `crates/kiln-hub/src/routes.rs:34,55,69` read it at compile time. The
database file does not exist in a checkout (`data/` holds only `.gitkeep`; `.gitignore:2`
ignores `data/*.db`), so the macros fail. The repository already has the answer, the
committed `.sqlx/` cache with `SQLX_OFFLINE=true`, but only `just test` / `just lint`
(`justfile:8,12`) and CI (`.github/workflows/ci.yml:9`) set it. `CLAUDE.md:11-13` tells
agents to run the bare `cargo build --workspace` / `cargo test --workspace` / `cargo clippy`.
**Evidence:** `error: error returned from database: (code: 14) unable to open database file`
(4 `cargo test` events in 4 sessions, 4 `cargo build` events in 4 sessions), followed by
`` error: could not compile `kiln-hub` (bin "kiln-hub" test) due to 3 previous errors ``.
**Why it keeps happening:** the docs name a command that is broken by default. It is not
flaky, as the scan guessed: the "re-run that passed" was a narrower command
(`cargo test -p kiln-core`, `cargo test -p kiln-core -p kilnctl`) that skips kiln-hub.
That workaround has its own cost: with the workspace never compiled, breakage outside
kiln-core went unseen and the human found it instead:
- S10: "build is broken: ``` error[E0432]: unresolved import `kiln_core::cone_to_celsius` --> crates/kilnctl/src/commands/cone.rs:2:17 […] didnt you check the cli crate? and hub uses it too" (the agent's last run was `cargo test -p kiln-core`)
- S07: "you said it works but hub panics on start: […] did you actually run it?" (last run: `cargo test -p kiln-core`)
**Fix:**
- `.cargo/config.toml`, append, so that every cargo invocation uses the committed cache:
  ```toml
  [env]
  SQLX_OFFLINE = "true"
  ```
  (`cargo sqlx prepare` sets `SQLX_OFFLINE=false` for its own run and cargo's `[env]` does
  not override a variable that is already set, so `just sqlx-prepare` should keep working.
  Check that once after the change.)
- `CLAUDE.md`, under Commands:
  ```markdown
  - kiln-hub's SQL is checked at compile time against the committed `.sqlx/` cache. After
    adding or changing a `sqlx::query!`/`query_as!` or a migration, run
    `just db-setup && just sqlx-prepare` (needs sqlx-cli) and commit `.sqlx/`.
  - Before saying a change is done, run `cargo test --workspace`, not only `-p kiln-core`.
    After touching kiln-hub routes, also start it once with `just hub`.
  ```
**Confidence:** high for the cause (the three files agree, and the error is sqlite's
"cannot open" code); medium for the `[env]` fix until `just sqlx-prepare` is run once
with it. The later failures with "4 previous errors" (S07, S09, S10, S12, S13) probably
also include queries added in those sessions that were not yet in `.sqlx/`; the evidence
does not show those error lines.

## 2. `unwrap` / `expect` keeps appearing in kiln-core

**Seen in:** S02, S07, S11 (3 of 15 sessions)
**What happens:** the agent writes `.unwrap()` or `.expect(` in the library, says it is done,
and the human sends it back.
**Evidence:**
- S02: "pls no unwrap/expect in kiln-core, it's a library — return KilnError. the cli can unwrap if it wants, core never panics"
- S07: "again with the .expect( in core... we talked about this. KilnError or make it infallible."
- S11: "thanks, but could you do it without the unwrap? same as last time."
**Why it keeps happening:** not written down and not enforced. `CLAUDE.md:20` says
"`thiserror` in libraries, `anyhow` in binaries", which does not say "never panic". Nothing
in `crates/kiln-core/src/lib.rs` or the clippy command rejects it. The crate has no
`unwrap`/`expect` today, so a lint can go in without other changes.
**Fix:**
- `crates/kiln-core/src/lib.rs`, after the `//!` doc comment (line 4):
  ```rust
  #![cfg_attr(not(test), deny(clippy::unwrap_used, clippy::expect_used))]
  ```
  The lint step in CI and in `CLAUDE.md:13` then fails on it.
- `CLAUDE.md`, under Conventions:
  ```markdown
  - kiln-core never panics: no `unwrap()`/`expect()` outside tests. Return `KilnError` or
    make the function infallible. Clippy enforces this.
  ```
**Confidence:** high.

## 3. CLI changes land without a CHANGELOG line

**Seen in:** S05, S09, S14 (3 of 15 sessions)
**What happens:** the agent changes kilnctl's interface and finishes without touching
`CHANGELOG.md`; the human asks for it each time.
**Evidence:**
- S05: "looks good. please you always add line in CHANGELOG under Unreleased when you change the CLI, I forget it otherwise and then release notes are empty"
- S09: "you forgot chnagelog again"
- S14: "nice. changelog?"
**Why it keeps happening:** not written down. `CLAUDE.md` does not mention the changelog;
`CHANGELOG.md:5` has the `## [Unreleased]` heading the human means, and it is empty.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Any change to kilnctl's commands, flags or output, or to kiln-hub's API, gets a line
    under `## [Unreleased]` in `CHANGELOG.md` in the same change.
  ```
  (The human said "the CLI"; `CHANGELOG.md:3` covers kiln-hub too, so the line includes it.
  Drop that part if it is not wanted.)
**Confidence:** high.

## 4. Release builds are cut off at the two-minute command limit

**Seen in:** S03 (`just check-size`), S08 (`cargo build --release -p kilnctl`), S13
(`just release-build`) (3 of 15 sessions)
**What happens:** `Cargo.toml:27-32` builds release with `lto = "fat"` and
`codegen-units = 1`, which takes longer than the agent's default 2-minute limit for a
shell command. The build is killed and started again.
**Evidence:** `Command timed out after 2m 0.0s` in all three.
**Why it keeps happening:** the profile is deliberate ("we squeeze them", `Cargo.toml:26`)
and nothing tells the agent the build is slow.
**Fix:**
- `CLAUDE.md`, under Commands:
  ```markdown
  - Release builds (`just check-size`, `just release-build`, `cargo build --release`) use
    fat LTO and take several minutes: run them with a 10-minute timeout or in the background.
  ```
**Confidence:** high for the cause; how long the build really takes was not measured here.

## 5. Agents edit a migration that has already shipped

**Seen in:** S06, S12 (2 of 15 sessions) · Edit on
`crates/kiln-hub/migrations/0002_firings.sql` declined both times
**What happens:** asked to add a column (S06) or an index (S12) on `firings`, the agent
edits the migration that created the table. `crates/kiln-hub/src/main.rs:16` runs
`sqlx::migrate!`, which refuses to start against a database whose applied migration has a
different checksum, so on the studio box this would have broken the hub.
**Evidence:**
- S06: "no — never edit a migration that's already been applied, the studio box ran 0002 months ago. add a new one"
- S12: "same thing as before.. new file pls, we never touch old migrations once they shipped"
**Why it keeps happening:** not written down, not enforced.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Never edit a file in `crates/kiln-hub/migrations/` once it is committed: the studio box
    has applied them. Schema changes go in a new `NNNN_name.sql`.
  ```
- `.claude/settings.json` (new file), so the boundary holds; add each migration when it ships:
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
**Confidence:** high.

## Looked at, not a problem

- The scan's ready-to-paste line "In `crates/kiln-hub/src/routes.rs`: use
  `without_v07_checks`": do not paste it. That phrase is from axum's panic message quoted
  in S07, not a rule from the human. The route was written as `:id`; axum 0.8 wants `{id}`,
  as `routes.rs:15,17` already do. It happened once, and running the hub (problem 1) catches it.
- S11 "keep celsius default, they can pass --fahrenheit": the human changed their mind
  within the session and took it back. `CLAUDE.md:17` already says °C.
- S09 interruption ("make it `kilnctl firings import`"): a changed requirement.
- S04 "can kilnctl plan use it too?": a follow-up request.
- S04 and S14 `cargo test -p kiln-core` failures: tests written first, red then green.
- S01 `cargo build`: `Could not resolve host: index.crates.io` while adding comfy-table.
  One network failure, once.
- Edit misses in `routes.rs` (S06, two matches) and `commands/firings.rs` (S15): one each,
  in files of 79 lines and less.
- S11 search for `cones_v1` found nothing: that was the answer the human asked for.
- `crates/kilnctl/src/cli.rs`, `main.rs`, `commands/mod.rs`, `schedule.rs`: flagged only
  through the S09 changelog message and the S09/S11 changes of mind above.

## Measure it

After the fixes, `/agent-friction:compare` in a week or two shows whether these sessions'
failures went down.
