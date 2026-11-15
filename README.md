# agent-friction

[![CI](https://github.com/nurettincoban/agent-friction/actions/workflows/ci.yml/badge.svg)](https://github.com/nurettincoban/agent-friction/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/nurettincoban/agent-friction/blob/main/LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)

**Find the places in your codebase where coding agents keep getting stuck, and get the receipts.**

Your agent fails `pytest` because this repo needs `make test`. It burns three edits on a generated
file it should never touch. You type "no, use `PaymentClient`" for the fourth time this month.
Each one is small. Together they are where your agent time goes, and nobody writes them down.

`agent-friction` reads the transcripts your coding agent already leaves on disk, finds those
recurring failures, pins them to files, directories, and commands, and tells you what to change,
with the evidence quoted so you can check it yourself.

![Interactive friction map of the example repository](https://raw.githubusercontent.com/nurettincoban/agent-friction/main/docs/assets/friction-map.png)

```console
$ agent-friction scan
agent-friction · shopfront
6 sessions · 74 tool calls · 35 friction events · ~28m lost · 5 human corrections

Files where agents struggle
  score            path                                   sess events    lost  top signals
  █████▋      56.8 src/shopfront/api/schema_gen.py           4     12     11m  ● edit-miss×6, user-correction×3
  ████        39.8 src/shopfront/checkout/service.py         3      6     19m  ● user-correction×3, edit-revert×2

Commands that keep failing
  █████       50.5 pytest                                    4      8      6m  ● command-failure×7, retry-loop×1

What to fix first
  1. src/shopfront/api/schema_gen.py: Generated file: point agents at its source
     ...agents ran into trouble editing it directly 7 times in 4 sessions. Say in AGENTS.md
     which source to change and which command regenerates it...
  2. pytest: Tell agents to use `make test`
     `pytest` failed 7 times in 4 sessions; 7 times the agent then got the same job done with
     `make test`, and `pytest` did not work later in those sessions.
  3. src/shopfront/checkout/service.py: Write down the rule the human keeps repeating
     ...In their words: "No, I said use PaymentClient, not the legacy module. [...]"
```

That output is real: it comes from the synthetic sessions in [`tests/fixtures`](https://github.com/nurettincoban/agent-friction/tree/main/tests/fixtures).
See the full example as [Markdown](https://github.com/nurettincoban/agent-friction/blob/main/examples/shopfront.md), [JSON](https://github.com/nurettincoban/agent-friction/blob/main/examples/shopfront.json),
or [interactive HTML](https://github.com/nurettincoban/agent-friction/blob/main/examples/shopfront.html) (download it and open it in a browser).

## Why this exists

Agent observability tools tell you about **sessions**: tokens, cost, latency. Linters and code
health tools tell you about **code**. Nothing connects the two, so the questions a team actually
has go unanswered:

- *Which parts of our codebase are hard for agents, and why?*
- *What should go in `AGENTS.md` / `CLAUDE.md`, and what is already wasted effort?*
- *We fixed it. Did it actually help?*

`agent-friction` answers those with a map keyed by **your repo's paths**, ranked by **recurrence
across sessions** (one bad session is noise, the same failure in five sessions is a pattern), and
backed by **evidence** you can trace to a transcript line.

## Install

Requires Python 3.10+. No dependencies. Not on PyPI yet, so install from GitHub:

```sh
pipx install git+https://github.com/nurettincoban/agent-friction
# or run it without installing:
uvx --from git+https://github.com/nurettincoban/agent-friction agent-friction scan
```

## Use

```sh
cd your-repo
agent-friction scan                        # friction map in the terminal
agent-friction scan --since 30d -o map.html  # interactive treemap, one self-contained file
agent-friction scan -o friction.md         # Markdown for an issue or a PR
agent-friction show src/api/client.py      # why one file scores high, with evidence
agent-friction show "npm test"             # the same for a command, or a directory
agent-friction sources                     # which transcripts it found
```

By default it finds Claude Code sessions for the repo (its subdirectories, its git worktrees, and
sessions started in a parent directory that worked inside it)
under `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR`). Point it elsewhere with `--transcripts`.
If the sessions were recorded where the repo lived at another path (a teammate's laptop, a CI
runner, an old checkout), add `--repo-alias /that/path`.

### The improvement loop

The point is not the report, it is the fix. Measure, change something, measure again:

```sh
agent-friction scan --until 2026-11-01 -f json -o before.json
# ...add the missing line to AGENTS.md, split the file, add the script...
agent-friction scan --since 2026-11-01 -f json -o after.json
agent-friction compare before.json after.json
```

`compare` measures each file and command against the sessions that actually touched it (events per
10 exposed sessions), so a file nobody worked on afterwards shows up as "no activity", not as
"resolved". It ignores changes too small to mean anything, warns when either side has too few
sessions, and refuses to compare scans of different repositories.

### In CI

Both gates exit with code 3, so they can fail a job:

```sh
agent-friction scan --since 30d --fail-on 45              # any hotspot scoring 45 or more
agent-friction compare before.json after.json --fail-on-regression
```

Exit codes: `0` ok, `1` error (for example no transcripts found), `2` bad arguments, `3` gate
tripped.

## What it detects

Each signal is a deterministic rule over the transcript, documented with its exact definition,
attribution rule, and known false positives in [docs/signals.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/signals.md).

| Signal | What happened |
|---|---|
| `user-correction` | The human corrected the agent ("no, use X", "I said…", "you broke…") |
| `retry-loop` | The same failing command was re-run without a change in between |
| `tool-rejected` | The human declined a tool call (and often said why) |
| `user-interrupt` | The human stopped the agent mid-action |
| `human-rework` | A human rewrote lines an agent commit had just written, or committed a fix/revert of it (git) |
| `edit-miss` | An edit could not find its target text, or found it more than once |
| `edit-revert` | An edit undid an earlier edit in the same session |
| `command-failure` | A command failed because of setup or invocation, not just a red test |
| `edit-thrash` | One file edited many times in one session |
| `stale-edit` | An edit refused because the file changed or was never read |
| `path-miss` | The agent read or edited a path that does not exist |
| `search-miss` | A search in a file or directory came back empty |
| `read-loop` | The same file re-read many times without being edited |

Scores are 0-100, explainable, and reproducible by hand from three factors: how severe the
friction is, how often it **recurs** across sessions (one bad afternoon is not a pattern), and how
**consistently** it happens in the sessions that touch that file or command at all (so a busy
file is not penalized just for being busy). Roughly: under 20 is an incident, 20-45 a pattern,
above 45 a reliable struggle. Red-then-green test runs with a fix in between are normal work and
are not counted. Time lost is a conservative estimate from timestamps. Confidence (low / medium /
high) depends on how many sessions and how many kinds of evidence back a hotspot. All of it is in
[docs/methodology.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/methodology.md).

## Privacy

Transcripts contain your code and sometimes your secrets, so:

- Everything runs locally. No network calls, ever. The HTML report loads nothing external and
  ships a strict Content-Security-Policy.
- Excerpts are redacted (API keys and tokens, `Authorization` headers, private keys, common
  password forms, credentials in URLs and `curl -u`, emails, high-entropy strings, your home
  directory) and truncated. Thinking blocks and assistant prose are never included.
- `--no-excerpts` removes all transcript text: prompts, quoted corrections, search patterns,
  command lines, and output. What remains is paths, counts, timestamps, and normalized command
  names built only from publicly known words (`git commit`, `npm test`, `python -m pytest`).
  Project-specific names (your own scripts, make targets, tools) become short hashes such as
  `make <target:37bd73>`: stable, so grouping still works, but a pseudonym rather than
  anonymous, since someone who guesses the name can confirm it.

Redaction is pattern-based: it catches secrets that look like secrets, not an opaque password
typed as a bare argument. Skim a report before you share it outside your team, or use
`--no-excerpts`.
See [SECURITY.md](https://github.com/nurettincoban/agent-friction/blob/main/SECURITY.md).

## Supported agents

| Agent | Support |
|---|---|
| Claude Code | Native, auto-discovered, including subagents and resumed sessions |
| Anything else | Convert to the [generic JSONL format](https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md#generic-transcript-format): one event per line, a few fields |
| Git history | Any agent that marks its commits (`Co-Authored-By: Claude`, Codex, Copilot, Cursor, Aider, …) |

Native adapters for other agents are welcome; see [CONTRIBUTING.md](https://github.com/nurettincoban/agent-friction/blob/main/CONTRIBUTING.md).

## Output contract

`scan -f json` produces a versioned document (`schema_version: 1`) described field by field in
[docs/schema.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md). Build dashboards or bots on it; fields are only ever added
within a schema version.

## Limitations

- Heuristics, not ground truth. A red test that the agent then fixes is normal work, not
  friction, and the rules try hard not to count it; they will sometimes be wrong both ways.
  `show` exists so you can check.
- Correction detection is English-only and deliberately conservative: it misses indirect or
  softened corrections rather than mislabel ordinary follow-ups, so treat the count as a floor.
- Time lost is a floor, not a measurement of human time.

Reports of wrong or missed hotspots are the most valuable contribution you can make:
[open one here](https://github.com/nurettincoban/agent-friction/issues/new?template=false_positive.yml).

## Contributing

`make install && make check`. See [CONTRIBUTING.md](https://github.com/nurettincoban/agent-friction/blob/main/CONTRIBUTING.md) for the ground rules (zero
dependencies, every heuristic documented and tested in both directions, deterministic output).

## License

[MIT](https://github.com/nurettincoban/agent-friction/blob/main/LICENSE)
