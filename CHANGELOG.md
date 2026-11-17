# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). The JSON output has its own `schema_version`.

## [0.1.0] - Unreleased

### Added

- `agent-friction scan`: friction map of a repository from Claude Code transcripts (including
  subagents and sessions started in a parent directory), the generic JSONL format, and git
  history. Output as terminal, Markdown, JSON, or a self-contained interactive HTML treemap.
- 18 deterministic signals, from failed edits and commands to human corrections, declined
  calls, retry loops, no-convergence, other-tool failures and git rework (flaky tests are told
  apart within command failures), each documented with its rule, attribution and known false
  positives in `docs/signals.md`.
- Scores from severity, recurrence across episodes of work, and consistency in the sessions
  that touched a target. Weak evidence on its own stays in the incident band. Confidence levels
  and a floor estimate of agent time lost.
- Recommendations that name the fix (the working command, the setup step, the file to split,
  the boundary to state). They say when AGENTS.md, CLAUDE.md or another instruction file
  already covers it.
- Command verdicts that understand pipes, chains, background commands, setup steps,
  environment failures and normal red-then-green work, which is not counted.
- `agent-friction show`: the score breakdown, recommendations and evidence for one file,
  directory or command.
- `agent-friction compare`: before/after comparison per exposed session, with `replaced`, `enforced`,
  `resolved`, `inactive` and `too-little-data` verdicts, and a `--fail-on-regression` gate.
- `agent-friction sources`: list the transcripts that would be analysed, and why any were not.
- `scan --fail-on SCORE` gate. Exit codes: 0 ok, 1 error, 2 usage, 3 gate.
- Redaction of secrets in evidence excerpts. `--no-excerpts` removes all transcript text and
  pseudonymizes project-specific command, tool and path names.
- JSON output, `schema_version: 1`, documented field by field in `docs/schema.md`. The Python
  API is `agent_friction.analyze(AnalyzeOptions(...))`.

[0.1.0]: https://github.com/nurettincoban/agent-friction/commits/main
