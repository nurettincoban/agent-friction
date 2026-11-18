# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/). The JSON output has its own `schema_version`.

## [0.1.0] - Unreleased

### Added

- Measured token cost: the model's usage as Claude Code records it in each transcript, summed
  over the turns counted against each file and command (the stretches behind its time lost),
  with cache reads shown apart. JSON `tokens_lost` on every hotspot, command and directory, and
  `totals.tokens_lost` / `totals.tokens_total`; a "Cost of … so far" line under each item of "What to fix
  first"; tokens per session in `compare`. Which turns belong to which problem is
  agent-friction's assignment; the counts are the transcripts'.
- A diagnosis step in the Claude Code plugin: `/agent-friction:scan` hands the session
  everything the scan saw and has it find each cause in the repository and write
  `.agent-friction/diagnosis.md` with an exact fix (steps in `plugin/diagnose.md`);
  `/agent-friction:fix` works from it. On two new held-out datasets it found 13 to 16 of 16
  planted problems against 11 to 13 for `/insights` (docs/comparison.md).
- `scan -f investigate`: every session, every human turn and the recurring error lines, laid
  out for a diagnosis.
- JSON: `signatures` (the error lines that recur in a hotspot's output, with `output_events`)
  and `conversation` (every message a human typed after the agent started, with the agent's
  last calls before it; only with excerpts, never in the HTML map). "What to fix first" quotes
  the top error line ("The tool said: …").
- A command that says it is listening and then times out is recognised as a server, whatever
  its name; advice resting on tool failures of one target in 3+ sessions is no longer thin.
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
- "Ready to paste" in every report: the AGENTS.md lines and `.claude/settings.json` deny rules
  the advice names, filled in from the evidence, each deny rule replayed against the sessions
  (`paste` in the JSON). A generated file's advice names the source and command its header
  gives.
- A correction is stated as the rule the human gave ("use `PaymentClient`, not
  `legacy_charge`"), and a weakly worded one about code the agent had just added counts once
  the agent takes that code out. New advice `say-how-to-verify` when the agent said it was done
  and the human found it failing. With only thin evidence, reports say "Nothing to fix yet".
- `docs/comparison.md` re-measured: an answer key committed before the runs, three `/insights`
  runs per dataset, two graders; then on two held-out datasets written blind by independent
  authors. On the first, agent-friction scored 5 of 14 to `/insights`' 13 to 14; after the
  changes below, 12 of 14 there (tuned on it) and 9 of 16 on the second, untouched set, to
  `/insights`' 11.
- Corrections in everyday words ("Thanks. One thing though: ...", "Not that file", "Please
  revert the timing bound change", "Please don't kick off `test-all`"); a correction that names
  a command the agent had just run is that command's; a refusal's reason may come in the next
  message; a failure reported right after the agent said it was done is a correction, and from
  two sessions on "Agents say the work is done before checking it" is one item for the
  repository, first in "What to fix first".
- A command that fails and then passes with a variable set (`TZ=UTC pytest`) or as another
  runner (`make test`), nothing changed, in two sessions: advice to use the working form.
  `git stash && pytest; git stash pop` is no longer counted as a change.

[0.1.0]: https://github.com/nurettincoban/agent-friction/commits/main
