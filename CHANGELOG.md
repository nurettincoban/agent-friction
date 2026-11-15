# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). The JSON output has its own `schema_version`.

## [Unreleased]

## [0.1.0] - 2026-11-15

### Added

- `agent-friction scan`: friction map of a repository from Claude Code transcripts and git
  history, as terminal, Markdown, JSON, or a self-contained interactive HTML treemap.
- `agent-friction show`: the score breakdown, recommendations, and evidence for one file,
  directory, or command.
- `agent-friction compare`: before/after comparison normalized per session, with a
  `--fail-on-regression` gate.
- `agent-friction sources`: list the transcripts that would be analysed.
- Generic JSONL transcript format for other agents.
- Redaction of secrets in evidence excerpts, and `--no-excerpts`.

[Unreleased]: https://github.com/nurettincoban/agent-friction/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nurettincoban/agent-friction/releases/tag/v0.1.0
