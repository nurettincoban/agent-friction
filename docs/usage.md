# Using agent-friction

The [README](../README.md) has the short version. This page has the rest: what the report
looks like, every command, how to apply the fixes, Windows notes, privacy and limitations.

- [Example output](#example-output)
- [Commands](#commands)
- [Apply the fixes](#apply-the-fixes)
- [Windows](#windows)
- [What it detects](#what-it-detects)
- [Privacy](#privacy)
- [Supported agents](#supported-agents)
- [Limitations](#limitations)

## Example output

```console
$ agent-friction demo
agent-friction · shopfront
6 sessions · 74 tool calls · 23 friction events (+10 weak) · ~9m lost · 5 human corrections · 2026-10-26 → 2026-11-12
tokens: 43k (+588k cache reads) in the turns counted as lost, of 143k (+1.9M cache reads) in all sessions

What to fix first
  1. src/shopfront/api/schema_gen.py: Generated file: point agents at its source
     ...agents ran into trouble editing it directly 7 times in 4 sessions. Its header names the fix...
  2. pytest: Tell agents to use `make test`
     `pytest` failed 7 times in 4 sessions; 5 times the agent then got the same job done with
     `make test`, and `pytest` did not work later in those sessions.
     Cost so far: 8.2k tokens (+111k cache reads) and ~3m in the agent turns counted against it,
     over 4 sessions.
  3. src/shopfront/checkout/service.py: Write down the rule the human keeps repeating
     A human corrected the agent here 3 times in 3 sessions...

Ready to paste
  AGENTS.md (or CLAUDE.md):
    - Use `make test`, not `pytest`.
    - In `src/shopfront/checkout/service.py`: use `PaymentClient`, not `legacy_charge`.
  .claude/settings.json, under "permissions" -> "deny":
    "Edit(/src/shopfront/api/schema_gen.py)"

Files where agents struggle
  score            path                                   sess events    lost  top signals
  █████▋      56.4 src/shopfront/api/schema_gen.py           4     11      4m  ● edit-miss×6, user-correction×2
  ████▋       46.5 src/shopfront/checkout/service.py         3      5      2m  ● user-correction×3, edit-revert×1
```

That output is real, abridged: `agent-friction demo` scans the synthetic sessions in [`tests/fixtures`](https://github.com/nurettincoban/agent-friction/tree/main/tests/fixtures).
See the full example as [Markdown](https://github.com/nurettincoban/agent-friction/blob/main/examples/shopfront.md), [JSON](https://github.com/nurettincoban/agent-friction/blob/main/examples/shopfront.json),
or [interactive HTML](https://nurettincoban.github.io/agent-friction/demo/).

## Commands

```sh
cd your-repo
agent-friction demo                          # the bundled example, nothing to set up
agent-friction scan                          # friction map in the terminal
agent-friction scan --since 30d -o map.html  # interactive treemap, one file
agent-friction scan -o friction.md           # Markdown for an issue or a PR
agent-friction show src/api/client.py        # why one file scores high, with evidence
agent-friction show "npm test"               # the same for a command, or a directory
agent-friction sources                       # which transcripts it found
agent-friction scan -f prompt                # the fixes, as a task for your agent
agent-friction scan -f investigate           # all the evidence, laid out for a diagnosis
agent-friction scan --since before.json      # only what came after a saved scan
```

By default it finds Claude Code sessions for the repo (its subdirectories, its git worktrees, and
sessions started in a parent directory that name it by its absolute path)
under `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR`). Point it elsewhere with `--transcripts`.
If the sessions were recorded where the repo lived at another path (a teammate's laptop, a CI
runner, an old checkout), add `--repo-alias /that/path`.

### It reads what you already told your agents

Before it suggests a line for `AGENTS.md`, it checks `AGENTS.md`, `CLAUDE.md` (including nested
ones), `.github/copilot-instructions.md`, `.cursorrules`, `.cursor/rules/` and the deny rules in
`.claude/settings.json`. If the advice is already written down and agents still trip over it, it
says so ("`AGENTS.md:3` already names `make test`, yet agents run `pytest`"), asks you to
confirm the cited line really says this, and then suggests enforcing it (a hook, a permission
rule, a wrapper) instead of adding another line. A line that only mentions the command or file
in passing is cited as "also mentioned", and the usual advice stays. See
[What the repository already says](https://github.com/nurettincoban/agent-friction/blob/main/docs/methodology.md#what-the-repository-already-says).

Dependencies and build output (`node_modules`, `.venv`, `site-packages`, git-ignored directories)
are left out of the map, since agents reading library code is not a problem in your code.

### The improvement loop

The point is not the report, it is the fix. Measure, change something, measure again:

```sh
agent-friction scan --until 2026-11-01 -f json -o before.json
# ...add the missing line to AGENTS.md, split the file, add the script...
agent-friction scan --since 2026-11-01 -f json -o after.json
agent-friction compare before.json after.json
```

`--since before.json` is a shorthand for "after the last event in that saved scan": the later
scan then holds only events the earlier one did not see, which is what `compare` needs. It
also reads the transcripts the same way, unless you say otherwise: the `--transcripts` and
`--repo-alias` the saved scan used, and `--no-git` or `--no-excerpts` if it had them. A scan
made with `--no-excerpts` records only that those options were given, not their paths, so
pass them again; the command says so.

`compare` measures each file and command against the sessions that actually touched it (events per
10 exposed sessions) and gives each one a verdict:

| Verdict | Meaning |
|---|---|
| `regressed` / `new` | More friction per exposed session than before, or friction that was not there |
| `improved` | Less friction per exposed session, by more than noise |
| `resolved` | Agents still touched it afterwards, with no friction |
| `replaced` | The fix was "use `make test`, not `pytest`": agents stopped running the old command, and the recommended one ran cleanly |
| `enforced` | A hook or deny rule added on the advice now refuses the call, and agents go another way |
| `inactive` | Nobody touched it afterwards. Often the expected outcome, but no evidence either way |
| `too-little-data` | Too few sessions touched it on one side to tell |

It ignores changes too small to mean anything, and refuses to compare scans of different
repositories, scans whose time windows overlap, or a `--no-excerpts` scan with one made without
it. The details are in
[docs/methodology.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/methodology.md).

### One package of a monorepo

`agent-friction scan packages/web` maps only that directory: paths are relative to it, and
friction elsewhere in the repository is left out, even from
sessions that started at the repository root. Instruction files above the package (`AGENTS.md`,
`CLAUDE.md` up to the repository root) still count as guidance agents were given.

### For a team, and in CI

Transcripts live on the machines where the agents ran (`~/.claude/projects/<encoded path>/` for
Claude Code), not in the repository, so a CI runner has none of its own. To map a whole team:

1. Each developer copies their project's transcript directory to a shared place. It holds
   prompts and command output, so treat it like any log of internal work, or share
   `agent-friction scan --no-excerpts -f json` results instead when only the numbers matter.
2. Scan the pooled directory, naming every path the repository was checked out at:

```sh
agent-friction scan --transcripts pooled/ \
  --repo-alias /home/ana/src/shop --repo-alias /Users/ben/work/shop --repo-alias 'C:\dev\shop'
```

If you leave an alias out, the error lists the directories the sessions ran in and suggests the
`--repo-alias` to add.

Both gates exit with code 3, so they can fail a job wherever transcripts are available (a
scheduled job over the pooled directory, or a developer's machine before a release):

```sh
agent-friction scan --transcripts pooled/ --repo-alias /home/ana/src/shop --since 30d --fail-on 45
agent-friction compare before.json after.json --fail-on-regression
```

`--allow-empty` makes "nothing to analyse" a report instead of an error, for jobs that may run
before any transcripts arrive.

Exit codes: `0` ok, `1` error (for example no transcripts found), `2` bad arguments, `3` gate
tripped.

## Apply the fixes

A report is only useful once its advice is in the repository. Two ways to get it there:

**With Claude Code**, install the plugin once. In Claude Code:

```text
/plugin install agent-friction --marketplace nurettincoban/agent-friction
```

On Claude Code older than 2.1.275, add the marketplace first, then install from it:

```text
/plugin marketplace add nurettincoban/agent-friction
/plugin install agent-friction@agent-friction
```

From a shell, `claude plugin marketplace add nurettincoban/agent-friction` and
`claude plugin install agent-friction@agent-friction` do the same (add `--scope project` to
share it with everyone who works in the repository). The plugin carries its own copy of
agent-friction, so there is nothing else to install; it needs Python 3.10 or newer.

Then, inside a repository:

- `/agent-friction:scan` maps the friction, then **diagnoses** it: the session reads
  everything the scan saw (`scan -f investigate`: every session, every human turn, the error
  lines that keep recurring), opens the files the evidence points at, and writes
  `.agent-friction/diagnosis.md`: each problem with the sessions it cost, the cause it found
  in the repository, and an exact fix, plus what it looked at and judged normal work. The
  steps it follows are in [plugin/diagnose.md](https://github.com/nurettincoban/agent-friction/blob/main/plugin/diagnose.md).
  It saves the scan and an interactive map there too, and changes nothing else.
- `/agent-friction:fix` works from that diagnosis (or makes one), checks each problem against
  the repository, shows the changes it proposes with the evidence behind each one (the change
  that removes the cause, and the line for your instruction file), and applies only the ones
  you approve.
  Run it again later and it first tells you what went down since the last time.
- `/agent-friction:compare` reads only the sessions since the last saved scan
  (`scan --since that-scan.json`) and compares the two, file by file and command by command.
  Run it after a week or two of agent work.

`scan` and `fix` accept scan options, such as `/agent-friction:scan --since 30d`; `compare`
takes two saved scans, or none to use the latest. The scan files quote transcript text, so
each command asks whether to add `.agent-friction/` to `.gitignore` while it is not ignored.
`compare` waits for at least 3 sessions after the scan before it reports verdicts.

If you installed the command line tool instead, `agent-friction install-skill` puts the fix
workflow alone into `~/.claude/skills` (or `--project .` for this repository), as
`/agent-friction-fix`.

**With any agent**, print the fixes as a task and paste it into a session:

```sh
agent-friction scan -f prompt > fix-friction.md
agent-friction scan --from before.json -f prompt   # from a saved `-f json` scan
```

The task lists the advice that rests on enough events, strongest first, with the counts and a
few cited events behind each item, and tells the agent to check every claim, make the smallest
change, propose everything before writing, and wait for approval. Advice on thin evidence is
listed separately, as something not to act on yet.

For a diagnosis like the plugin's, give the agent `agent-friction scan -f investigate` and
the steps in [plugin/diagnose.md](https://github.com/nurettincoban/agent-friction/blob/main/plugin/diagnose.md).
The scan itself stays deterministic and offline; the diagnosis is the agent's reading of it.

## Windows

Git Bash rewrites arguments that look like POSIX paths before a Windows program sees them:
`--repo-alias /home/dev/shop` arrives as `C:/Program Files/Git/home/dev/shop`. agent-friction
recognizes that form, uses the path you meant, and says so. To stop Git Bash from rewriting
paths at all, run `export MSYS_NO_PATHCONV=1` first (or prefix the one command with it).
PowerShell and `cmd` do not rewrite arguments.

## What it detects

Each signal is a deterministic rule over the transcript, documented with its exact definition,
attribution rule, and known false positives in [docs/signals.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/signals.md).
A "friction event" is one firing of a signal, so the derived signals add to the count: four
failed runs of the same line in a row are four `command-failure` events plus one `retry-loop`. Time is not
double-counted that way; each minute is claimed once.

| Signal | What happened |
|---|---|
| `user-correction` | The human corrected the agent ("no, use X", "I said…", "you broke…") and the agent went back to the work it had just done |
| `probable-correction` | Words that sound like a correction, without behaviour to back them up (weighted low, never quoted) |
| `retry-loop` | The same command line, with identical arguments, failed 3+ times with no change in between |
| `tool-rejected` | The human declined a tool call (and often said why) |
| `permission-denied` | A permission rule, hook, or safety check refused a tool call before it ran |
| `user-interrupt` | The human stopped the agent mid-action |
| `human-rework` | A human rewrote lines an agent commit had just written, or committed a fix/revert of it (git) |
| `edit-miss` | An edit could not find its target text, or found it more than once |
| `edit-revert` | An edit undid an earlier edit in the same session (not one a human's correction asked for) |
| `command-failure` | A command failed because of setup, invocation, or the environment (a service down, credentials, an unset variable); a red test the agent then fixes is not counted, one that passed only after a setup step is |
| `no-convergence` | The agent kept editing and re-running the same check, and it never went green |
| `edit-thrash` | One file edited 6 or more times in one turn while something kept failing |
| `stale-edit` | An edit refused because the file changed or was never read |
| `path-miss` | The agent read or edited a path that does not exist |
| `search-miss` | A search in a file or directory came back empty |
| `file-too-large` | The agent could not read a file because it is too large for its context |
| `read-loop` | The same part of a file read 3 or more times without being edited |
| `tool-failure` | Another tool (an MCP server, `WebFetch`, a subagent launch) returned an error, and the same tool failed again elsewhere in the scan |

### Reading the report

- **Score (0-100).** Severity × how often it **recurs** across episodes of work × how
  **consistently** it happens in the sessions that touch the target at all, so a busy file is not
  penalized for being busy. Under 20 is an incident, 20-45 a pattern, above 45 a reliable
  struggle. Every score can be reproduced by hand from its `score_detail` in `-f json`.
- **What is not counted.** Red-then-green test runs with a fix in between are normal work. Weak
  signals on their own (re-reads, many edits, an empty search) stay in the incident band; only a
  search for the same thing that keeps coming back empty, or a command still red at the end of
  session after session, is lifted out of it. The headline says how many events are weak.
- **Episodes.** A long-running session (`claude --continue`) counts once for every time a human
  picked it up again after two hours or more of silence. Subagents belong to the episode of the
  agent that started them.
- **Time lost** is a floor estimate of agent time from timestamps. A correction claims only the
  rework it caused. Time a person spends is never counted: not the wait before a reply, and not
  a permission prompt that stood open. Where Claude Code records how a call was approved, a
  person-approved call claims only what followed its result; where it does not say, call to
  result counts up to 30 seconds, or the command's usual run time in that session if longer. No
  single event claims more than 5 minutes (a no-convergence stretch, 15). `n/a` means the
  evidence carries no time estimate.
- **Tokens** are measured, not estimated: the model's usage as Claude Code writes it into each
  transcript, summed over the responses inside the stretches counted as time lost (for a failed
  run alone, the response that read its output). Which turns belong to which problem is
  agent-friction's assignment; the counts in them are the transcript's. Cache reads are shown
  apart (`6.2k tokens (+79k cache reads)`): they re-read the conversation so far and cost a
  fraction of fresh input, so one sum would mostly measure how long the session already was.
  Transcripts that record no usage (the generic format) show no token counts.
- **Confidence** (low / medium / high) depends on how many sessions back a hotspot and whether an
  independent second kind of evidence agrees.

All of it is in
[docs/methodology.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/methodology.md).

## Privacy

Transcripts contain your code and sometimes your secrets, so:

- Everything runs locally. No network calls, ever. The HTML report loads nothing external and
  ships a Content-Security-Policy that blocks every network load (`default-src 'none'`; its own
  inline script and styles are the only code it runs).
- Excerpts are redacted (API keys and tokens, `Authorization` headers, private keys, common
  password forms, credentials in URLs and `curl -u`, emails, high-entropy strings, your home
  directory) and truncated. Thinking blocks and assistant prose are never included.
- With excerpts on (the default), the JSON scan and `-f investigate` also list every message a
  human typed in these sessions, redacted, so that a diagnosis can read them; the HTML map does
  not carry that list.
- `--no-excerpts` removes all transcript text: prompts, quoted corrections, search patterns,
  command lines, and output. What remains is paths, counts, timestamps, and normalized command
  names built only from publicly known words (`git commit`, `npm test`, `python -m pytest`).
  Project-specific names (your own scripts, make targets, tools) become short hashes such as
  `make <target:37bd73>`: stable, so grouping still works, but a pseudonym rather than
  anonymous, since someone who guesses the name can confirm it.
- Even with `--no-excerpts`, reports always show your repository's file paths, its directory
  name, and when sessions happened; the JSON also records the repository's absolute path
  (`repo.root`, with your home directory masked as `~`), and, without `--no-excerpts`, the
`--transcripts` and `--repo-alias` paths it was given (`settings.inputs`). Those are the map itself. A path the agent guessed that
  never existed becomes a hash too (`src/<path:3f9a1c>`), since it may name something private.
- Session ids are taken from the transcript file name (a UUID for Claude Code), and are shown
  even with `--no-excerpts`. If you pool transcripts under descriptive file names, rename them
  before sharing a report.

Redaction is pattern-based: it catches secrets that look like secrets, not an opaque password
typed as a bare argument. Skim a report before you share it outside your team, or use
`--no-excerpts`.
See [SECURITY.md](https://github.com/nurettincoban/agent-friction/blob/main/SECURITY.md).

## Supported agents

| Agent | Support |
|---|---|
| Claude Code | Native, auto-discovered, including subagents and resumed sessions |
| Any other agent | Convert to the [generic JSONL format](https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md#generic-transcript-format): one event per line, a few fields |
| Git history | Any agent that marks its commits (`Co-Authored-By: Claude`, Codex, Copilot, Cursor, Aider, …). Without any transcripts, run `scan --allow-empty` for a git-only map |

Native adapters for other agents are welcome; see [CONTRIBUTING.md](https://github.com/nurettincoban/agent-friction/blob/main/CONTRIBUTING.md).

## Output contract

`scan -f json` produces a versioned document (`schema_version: 1`) described field by field in
[docs/schema.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md). Build dashboards or bots on it: within a schema version fields are only
added, never removed or re-typed. Until 1.0 the project is alpha, so new fields can still arrive
in any release; from 0.1.0 on, each one is listed in the CHANGELOG.

## Limitations

- A command run in the background is judged by what the agent later reads of it (`BashOutput`,
  a task notification). One that is never polled and never reports back has no outcome.
- Heuristics, not ground truth. A red test that the agent then fixes is normal work, not
  friction, and the rules try hard not to count it; they will sometimes be wrong both ways.
  `show` exists so you can check.
- A correction is counted only when the human's words address the agent's work and behaviour
  backs it up: they name a file the agent just changed (with an edit, a shell command or a
  subagent), or the agent goes back to one. Messages typed while the agent is still working are
  read as well. Anything else that merely sounds like a correction is "probable": shown as
  evidence, weighted lower, never quoted, and excluded from the headline count. It is
  English-only and still a heuristic, so treat the count as a floor.
- Time lost is a floor, not a measurement of human time.
- Failures of tools that are neither file, search nor shell tools (MCP servers, `WebFetch`, a
  subagent launch) have no file or command to pin to. They are counted as weak `tool-failure`
  events, listed under "Not tied to a file or command" and in `tool_failures` in the JSON, and
  only once the same tool has failed at least twice in the scan.
- Calls that acted outside the repository (a command that `cd`s elsewhere) are left out; the
  report says how many declined, denied or failed ones there were.
- One session is a lead, not a pattern: a row seen in a single session gets advice only when it
  scores 20 or more on its own (one long struggle) or a human said the rule in their own words.
- When a command's exit status is hidden (`pytest | tail`, `cmd; echo done`), the verdict comes
  from its output. If the filter cut the summary line away, the run counts as neither red nor green.
- Changes made by commands are inferred, not seen. `sed -i`, formatters, `--fix`, codemods,
  `mv`/`cp`/`rm` and inline scripts that write files count as edits; any other command that is
  neither a look nor a check counts as "may have changed something", enough to end a retry loop
  or make the next green run a fix. Changes made outside the transcript (your editor, another
  agent) are missed, so a check someone else fixed can still show as failing. Agents that edit
  mostly through shell commands also leave fewer traces of edit friction.
- Remote CI is not inspected. An agent that pushes, watches CI go red (`gh run watch`), fixes
  and pushes again leaves no failing local command, so that loop is not counted.

Reports of wrong or missed hotspots are the most valuable contribution you can make:
[open one here](https://github.com/nurettincoban/agent-friction/issues/new?template=false_positive.yml).

