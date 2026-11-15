# Contributing

Thanks for helping make coding agents less frustrating to work with.

## Setup

You need [uv](https://docs.astral.sh/uv/). Then:

```sh
git clone https://github.com/nurettincoban/agent-friction
cd agent-friction
make install   # uv sync --group dev
make check     # ruff + mypy --strict + pytest
```

Run the CLI from your checkout with `uv run agent-friction scan /path/to/repo`.

## Ground rules

- **Zero runtime dependencies.** The tool must stay `pipx install`-and-go. Dev tools are fine.
- **Every heuristic is documented and tested.** A new or changed signal needs an entry in
  [`docs/signals.md`](docs/signals.md), any scoring change goes in
  [`docs/methodology.md`](docs/methodology.md), and both need positive *and* negative tests.
  False positives erode trust faster than missed signals, so lean conservative.
- **Output is a contract.** The JSON result is versioned (`schema_version`). Adding fields is
  fine (document them in [`docs/schema.md`](docs/schema.md)); renaming or removing them needs a
  version bump.
- **Determinism.** Same input, same output, byte for byte. Sort everything; never depend on
  set or dict ordering you did not create.
- **Privacy.** Never commit real transcripts. Fixtures are hand-written and synthetic. Anything
  that ends up in a report goes through `redact.py`.

## Adding support for another agent

The quickest path is a converter to the generic JSONL format described in
[`docs/schema.md`](docs/schema.md#generic-transcript-format). A native adapter lives in
`src/agent_friction/adapters/` and turns a transcript into the normalized events in
`model.py`; look at the Claude Code adapter for the pattern. Please include a small synthetic
fixture and tests.

## Example reports

`examples/` is generated from the synthetic fixtures by `make examples`. CI fails if it is
out of date, so regenerate it when you change scoring or rendering.

## Releasing (maintainers)

1. Move "Unreleased" entries in `CHANGELOG.md` under a new version heading.
2. Bump `__version__` in `src/agent_friction/__init__.py`.
3. Tag `vX.Y.Z` and push the tag. The release workflow publishes to PyPI with trusted
   publishing and creates the GitHub release.
