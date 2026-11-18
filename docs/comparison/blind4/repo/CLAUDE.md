# Kilnlog

Tools for planning and logging kiln firings at the studio.

## Layout
- `crates/kiln-core` — library: Orton cone table, firing schedules, unit conversion
- `crates/kilnctl` — CLI (clap derive). Subcommands live in `src/commands/`
- `crates/kiln-hub` — small axum + sqlite service the kiln-room tablet talks to

## Commands
- Build: `cargo build --workspace`
- Test: `cargo test --workspace`
- Lint: `cargo clippy --workspace --all-targets -- -D warnings`
- Format: `cargo fmt --all`
- Run the CLI: `cargo kc plan --preset glaze6`

## Conventions
- Temperatures are °C internally (u16). Convert to °F only when printing.
- Ramp rates are °C per hour; negative means controlled cooling.
- Keep axum handlers thin; schedule logic belongs in kiln-core.
- Errors: `thiserror` in libraries, `anyhow` in binaries.
- Run `cargo fmt` before you finish.
