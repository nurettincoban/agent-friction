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
- `agent-friction demo`: scan the bundled example repository and its sessions, with nothing to
  set up (`make demo` writes `demo.html`).
- `scan -f prompt`: the recommendations as a task for any coding agent, with the evidence behind
  each item and rules to check each claim and wait for approval. `scan --from saved.json`
  renders a saved scan in any format.
- A Claude Code plugin, installable from this repository as a marketplace
  (`/plugin install agent-friction --marketplace nurettincoban/agent-friction`), with
  `/agent-friction:scan`, `/agent-friction:fix` and `/agent-friction:compare`. It runs its own
  copy of the package with any Python 3.10+, so nothing else needs installing.
- `scan --since saved.json`: only events after the last one in a saved scan, read with the
  same `--transcripts`, `--repo-alias`, `--no-git` and `--no-excerpts` it used (now recorded
  in `settings.inputs`), so the two can be compared without overlap. `compare` notes when
  only one side read git history.
- `agent-friction install-skill`: a Claude Code skill (`/agent-friction-fix`) that scans,
  proposes each fix with its evidence, applies only what you approve, and compares against the
  last scan next time.
- Recommendations carry `events`; advice on fewer than 5 events is marked `"evidence": "thin"`
  and kept out of "What to fix first" while there is other advice. Refusals by the agent tool's
  own rules (Claude Code's `Blocked: sleep ...`, the auto-mode classifier) are reported as such,
  with what the refusal says to do instead, in a section of their own ("The agent tool's own
  rules", `agent_tool` in the JSON), and count in no hotspot, score, total, gate or comparison.
- "What to fix first" leads the terminal, Markdown and HTML reports. The headline counts strong
  events and names weak ones beside them ("22 friction events (+11 weak)").
- Git Bash on Windows: a `--repo-alias` that Git Bash rewrote (`C:/Program Files/Git/home/...`)
  is recognized and restored, with a note about `MSYS_NO_PATHCONV=1`.
- `docs/comparison.md`: agent-friction and Claude Code's `/insights` run on the same sessions.
- `scan --fail-on SCORE` gate. Exit codes: 0 ok, 1 error, 2 usage, 3 gate.
- Redaction of secrets in evidence excerpts. `--no-excerpts` removes all transcript text and
  pseudonymizes project-specific command, tool and path names.
- JSON output, `schema_version: 1`, documented field by field in `docs/schema.md`. The Python
  API is `agent_friction.analyze(AnalyzeOptions(...))`.
- Project website at https://nurettincoban.github.io/agent-friction/ (served from `docs/`),
  with the example report as a live demo. `make examples` regenerates the demo too.

[0.1.0]: https://github.com/nurettincoban/agent-friction/commits/main
