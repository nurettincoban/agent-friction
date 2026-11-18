# Diagnosis: kilnlog

From `.agent-friction/evidence.md` (15 sessions, S01–S15, 2026-10-06 to 2026-11-13), checked
against the transcripts and this repository. Nothing below has been applied.

Not test-built: this checkout has no `Cargo.lock` and its pinned cargo 1.82.0 cannot resolve
today's crates.io, so none of the Rust changes below were compiled here. The hook script in
problem 6 was run and behaves as described.

## 1. kiln-hub does not compile with the documented commands

**Seen in:** S02, S05, S06, S07, S09, S10, S12, S13 (8 of 15 sessions) · `cargo test --workspace` failed in S02, S05, S09, S12; `cargo build -p kiln-hub` / `--workspace` / `--release --workspace` failed in S06, S07, S10, S13
**What happens:** `.env:3` sets `DATABASE_URL=sqlite://data/kilnhub.db`, the sqlx query macros read it at compile time, and `data/` is empty in a fresh checkout (`.gitignore:2` ignores the db), so every `sqlx::query_as!` in `crates/kiln-hub/src/routes.rs` fails. `justfile:8`, `justfile:12` and `.github/workflows/ci.yml:9` avoid this with `SQLX_OFFLINE=true` and the committed `.sqlx/` cache, but `CLAUDE.md:11-12` tell agents to run plain `cargo build --workspace` / `cargo test --workspace`, and `justfile` `hub`, `check-size` and `release-build` do not set it either.
**Evidence:** `error: error returned from database: (code: 14) unable to open database file` (S02, S05, S06, S07, S09, S10, S12, S13). It is not flaky, as the scan's advice says: the "re-run that passed" was each time a narrower command, `cargo test -p kiln-core` or `-p kiln-core -p kilnctl`, that leaves the hub out. So the hub went unbuilt and unrun: S06 "I couldn't compile kiln-hub here", S07 "kiln-hub doesn't compile in my environment, so the route was never exercised", S12 "kiln-hub doesn't compile here". Only S13 found `SQLX_OFFLINE`, after about 4 minutes of release build.
**Why it keeps happening:** the default command in CLAUDE.md is broken on a fresh checkout; the working setting lives only in two just recipes and CI.
**Fix:**
- `.cargo/config.toml`, append (cargo passes it to rustc, where the macros run; an explicit `SQLX_OFFLINE=false` still wins, and `cargo sqlx prepare` sets its own):
  ```toml
  [env]
  SQLX_OFFLINE = "true"
  ```
  Then the `SQLX_OFFLINE=true` prefixes at `justfile:8` and `justfile:12` are redundant and `release-build` / `hub` work too.
- `CLAUDE.md`, under Commands:
  ```markdown
  - kiln-hub builds from the committed `.sqlx/` query cache (no database needed). After changing a
    query in `crates/kiln-hub` or adding a migration, run `just db-setup && just sqlx-prepare`
    (needs sqlx-cli) and commit `.sqlx/`. If sqlx-cli is missing, say so; don't report hub work as done.
  ```
**Confidence:** high on the cause (same error in 8 sessions; `.env`, `data/`, justfile and CI read). Medium on the `[env]` fix: standard cargo behaviour, not built here. S06 shows the remaining gap: with offline mode a changed query fails with `` `SQLX_OFFLINE=true` but there is no cached data for this query `` until `.sqlx/` is regenerated, and `which sqlx` found nothing in that environment.

## 2. `unwrap` / `expect` written into kiln-core

**Seen in:** S02, S07, S11 (3 of 15 sessions) · corrected by the human each time
**What happens:** agents add a panicking call in the library (`checked_abs().expect(..)` in `Schedule::validate`, S02; `writeln!(..).expect(..)` in `to_csv`, S07; `Duration::try_minutes(..).unwrap()` in `total_hold`, S11) and the human asks for it to be removed.
**Evidence:** S02: "pls no unwrap/expect in kiln-core, it's a library — return KilnError. the cli can unwrap if it wants, core never panics" · S07: "again with the .expect( in core... we talked about this. KilnError or make it infallible." · S11: "thanks, but could you do it without the unwrap? same as last time."
**Why it keeps happening:** not written down. `CLAUDE.md:21` says only "Errors: `thiserror` in libraries, `anyhow` in binaries", and nothing lints for it (no `clippy::unwrap_used` anywhere; `crates/kiln-core/src` has no unwrap/expect today, so turning the lint on breaks nothing).
**Fix:**
- `crates/kiln-core/src/lib.rs`, first line (CI already runs clippy with `-D warnings`):
  ```rust
  #![cfg_attr(not(test), deny(clippy::unwrap_used, clippy::expect_used))]
  ```
- `CLAUDE.md`, under Conventions:
  ```markdown
  - kiln-core never panics: no `unwrap`/`expect` outside tests. Return `KilnError` or use an infallible form.
  ```
**Confidence:** high (the human's words in three sessions; CLAUDE.md and the source checked).

## 3. CHANGELOG not updated when the CLI changes

**Seen in:** S05, S09, S14 (3 of 15 sessions)
**What happens:** the agent changes `kilnctl` and reports done without touching `CHANGELOG.md`; the human asks for it afterwards.
**Evidence:** S05: "looks good. please you always add line in CHANGELOG under Unreleased when you change the CLI, I forget it otherwise and then release notes are empty" · S09: "you forgot chnagelog again" · S14: "nice. changelog?"
**Why it keeps happening:** not written down. CLAUDE.md does not mention the changelog; `CHANGELOG.md:5` has the `## [Unreleased]` section it belongs under.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Any change to kilnctl's commands, flags or output gets a line in `CHANGELOG.md` under `## [Unreleased]`, in the same change.
  ```
**Confidence:** high.

## 4. A core test fails every evening and agents wave it off

**Seen in:** S04, S10, S14 (3 of 15 sessions) · `cargo test -p kiln-core` failed, twice in S14
**What happens:** `eta_same_day_for_short_bisque` (`crates/kiln-core/src/schedule.rs:109-114`) starts from `Local::now()` and asserts the 374-minute fast bisque ends on the same date, so it fails whenever the tests run after 17:46 local time. The three sessions ran at 19:41, 21:16 and 18:30; the same test passed in every daytime session.
**Evidence:** S04: `left: 2026-10-14 right: 2026-10-13` · S10: `left: 2026-10-30 right: 2026-10-29` · S14: `left: 2026-11-11 right: 2026-11-10`. The agents called it "flaky" (S04), "pre-existing and unrelated" (S10), "Leaving it alone" (S14). In S10 that hid a really broken build, see problem 7.
**Why it keeps happening:** the test depends on the wall clock.
**Fix:**
- `crates/kiln-core/src/schedule.rs`, in `mod tests`: delete `use chrono::Local;` (line 88) and replace line 111
  ```rust
          let start = Local::now();
  ```
  with
  ```rust
          let start = chrono::Utc.with_ymd_and_hms(2026, 1, 15, 8, 0, 0).unwrap();
  ```
  (`TimeZone` is already in scope through `use super::*`.)
**Confidence:** high on the cause (assertion output and session times agree); the replacement line was not compiled here.

## 5. Release builds outlast the 2-minute command limit

**Seen in:** S03, S08, S13 (3 of 15 sessions) · `just check-size`, `cargo build --release -p kilnctl`, `just release-build`
**What happens:** `Cargo.toml` `[profile.release]` uses `lto = "fat"` and `codegen-units = 1`; the retry in S03 took `3m 47s` for kilnctl alone. The first attempt runs with the default timeout, is killed at 2 minutes, and the build starts again.
**Evidence:** `Command timed out after 2m 0.0s` (S03, S08, S13).
**Why it keeps happening:** not written down; the profile is deliberate ("we squeeze them"), so the note is the fix.
**Fix:**
- `CLAUDE.md`, under Commands:
  ```markdown
  - Release builds (`just check-size`, `just release-build`, any `cargo build --release`) take 4+ minutes (fat LTO). Run them in the background or with a 10-minute timeout.
  ```
**Confidence:** high.

## 6. Edits to a migration that has already shipped

**Seen in:** S06, S12 (2 of 15 sessions) · the human declined the Edit both times
**What happens:** asked for a new column (S06) or an index (S12), the agent edits `crates/kiln-hub/migrations/0002_firings.sql`. `crates/kiln-hub/src/main.rs:16` runs `sqlx::migrate!` at start, which rejects an applied migration whose content changed, so this would stop the hub on the studio box.
**Evidence:** S06: "no — never edit a migration that's already been applied, the studio box ran 0002 months ago. add a new one" · S12: "same thing as before.. new file pls, we never touch old migrations once they shipped"
**Why it keeps happening:** not written down, and nothing blocks it. A plain deny rule on the directory would also block creating new migrations, so it needs a hook.
**Fix:**
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Never edit an existing file in `crates/kiln-hub/migrations/`; schema changes go in a new numbered file.
  ```
- `.claude/hooks/shipped_migrations.py` (tested: blocks a committed migration, allows a new file and other paths; "committed" stands in for "shipped"):
  ```python
  #!/usr/bin/env python3
  """PreToolUse hook: refuse edits to a kiln-hub migration that is already committed."""
  import json, os, subprocess, sys

  root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
  path = (json.load(sys.stdin).get("tool_input") or {}).get("file_path") or ""
  rel = os.path.relpath(os.path.join(root, path), root).replace(os.sep, "/")
  if rel.startswith("crates/kiln-hub/migrations/") and rel.endswith(".sql"):
      committed = subprocess.run(
          ["git", "-C", root, "cat-file", "-e", f"HEAD:{rel}"],
          stderr=subprocess.DEVNULL,
      ).returncode == 0
      if committed:
          print(
              f"{rel} is a shipped migration and must not be edited. "
              "Add a new numbered file in crates/kiln-hub/migrations/ instead.",
              file=sys.stderr,
          )
          sys.exit(2)
  ```
- `.claude/settings.json` (the repository has no `.claude/` yet):
  ```json
  {
    "hooks": {
      "PreToolUse": [
        {
          "matcher": "Edit|Write|MultiEdit",
          "hooks": [
            { "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/shipped_migrations.py\"" }
          ]
        }
      ]
    }
  }
  ```
**Confidence:** high on the rule; the hook script was run on its own, not inside a Claude Code session.

## 7. "Done" after testing only kiln-core

**Seen in:** S07, S10 (2 of 15 sessions) · the human found the breakage
**What happens:** S07 added the route `/firings/:id/export`; axum 0.8 (`Cargo.toml` `axum = "0.8.1"`) panics on `:id` when the router is built, and the agent had run only `cargo test -p kiln-core`. S10 renamed `cone_to_celsius` in kiln-core, ran only `cargo test -p kiln-core`, and left `crates/kilnctl/src/commands/cone.rs` and `crates/kiln-hub/src/routes.rs:28` on the old name.
**Evidence:** S07: "you said it works but hub panics on start: … Path segments must not start with `:`. For capture groups, use `{capture}`. … did you actually run it?" · S10: "build is broken: … unresolved import `kiln_core::cone_to_celsius` … didnt you check the cli crate? and hub uses it too"
**Why it keeps happening:** mostly problem 1: the workspace build is red on every checkout, so agents fall back to the one crate that builds. S10 also had that evening's failing test (problem 4) to blame. Why the search subagent in S10 reported "No other usages found" is unconfirmed. The scan's ready-to-paste line "use `without_v07_checks`" is wrong: that is axum's escape hatch from the panic message, not what the human asked for; the routes use `{id}`.
**Fix:**
- Fixes 1 and 4 first; then `cargo test --workspace` is a usable check.
- `CLAUDE.md`, under Conventions:
  ```markdown
  - Before saying a change works, `cargo test --workspace` must pass, not just one crate. A kiln-core API change touches kilnctl and kiln-hub too.
  - axum 0.8: path captures are `{id}`, never `:id` (the router panics at startup).
  ```
- `crates/kiln-hub/src/routes.rs`, append, so a bad route fails in `cargo test` instead of on the studio box:
  ```rust
  #[cfg(test)]
  mod tests {
      #[tokio::test]
      async fn router_builds() {
          let pool = sqlx::sqlite::SqlitePoolOptions::new()
              .connect_lazy("sqlite::memory:")
              .expect("lazy pool");
          let _ = super::router(pool);
      }
  }
  ```
**Confidence:** medium. Both incidents are in the human's words; the link to problem 1 is the agent's own statement in S07; the test was not compiled here.

## Looked at, not a problem

- S09 "make it `kilnctl firings import`" and the interruption before it: a change of mind about the command shape.
- S11 "make plan show fahrenheit by default" then "keep celsius default … revert that pls": a change of mind; `CLAUDE.md:18` already says °C internally, °F only when printing.
- S04 "ok thx. can kilnctl plan use it too?": a follow-up request. The first red `cargo test` in S04 was the test written first, as asked.
- S01 `cargo build` "Could not resolve host: index.crates.io": one network failure, passed on retry.
- S06 edit miss in `routes.rs` (2 matches) and S15 edit miss in `commands/firings.rs`: one-offs, corrected on the next call.
- S11 search for `cones_v1` found nothing: that was the answer the human wanted.
- S15 clippy failure: the task itself ("ci is red on clippy").
- `crates/kilnctl/src/cli.rs`, `main.rs`, `commands/mod.rs`, `kiln-core/src/schedule.rs` "probable correction" in S09: that is the changelog reminder, problem 3, not a problem in those files.

## Measure it

After the fixes, `/agent-friction:compare` in a week or two shows whether these sessions' failures went down.
