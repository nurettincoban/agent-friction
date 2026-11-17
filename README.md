<h1>
  <img src="https://raw.githubusercontent.com/nurettincoban/agent-friction/main/docs/assets/logo.svg" width="40" height="40" alt="" align="top">
  agent-friction
</h1>

[![CI](https://github.com/nurettincoban/agent-friction/actions/workflows/ci.yml/badge.svg)](https://github.com/nurettincoban/agent-friction/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/nurettincoban/agent-friction/blob/main/LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)
![Dependencies: none](https://img.shields.io/badge/dependencies-none-brightgreen.svg)
[![Live demo](https://img.shields.io/badge/live_demo-open-b4462b.svg)](https://nurettincoban.github.io/agent-friction/demo/)

**Stop paying for the same agent mistake twice.**

Your coding agent runs `pytest` three times before it finds out this repo needs `make test`. It
hand-edits a generated file again. You type "no, use `PaymentClient`" for the fourth time this
month. Each of these costs a few turns, some tokens and a few minutes. Session after session,
they add up, and nothing tells you which ones.

`agent-friction` reads the transcripts your agent already leaves on disk, finds what keeps going
wrong **in this repository**, and gives you a short list of fixes, each with the evidence behind
it. Each fix removes a round trip that kept coming back:

- **Fewer wasted turns and tokens.** Every failed command, missed edit and retry is a turn the
  agent pays for. The report counts them per file and command, with an estimate of the agent
  time each one cost (time is estimated; tokens are not measured), so you can fix the costliest
  first.
- **Faster sessions.** With the missing line written down, the agent can get it right the first
  time: `make test`, not three failing `pytest` runs; the spec, not the generated file.
- **Fewer corrections from you.** The rule you keep repeating moves into `AGENTS.md` or
  `CLAUDE.md`, where every future session reads it.
- **A way to check that it worked.** `agent-friction compare` measures a later scan against the
  earlier one, file by file and command by command.

## Try it

```sh
uvx --from git+https://github.com/nurettincoban/agent-friction agent-friction scan
```

Run it inside a repository where you have used Claude Code: it finds the sessions on its own.
No sessions yet? `uvx --from git+https://github.com/nurettincoban/agent-friction agent-friction demo` scans a bundled example. Requires Python 3.10+, nothing
else; to install it, `pipx install git+https://github.com/nurettincoban/agent-friction`.

<a href="https://nurettincoban.github.io/agent-friction/demo/">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/nurettincoban/agent-friction/main/docs/assets/friction-map-dark.png">
    <img src="https://raw.githubusercontent.com/nurettincoban/agent-friction/main/docs/assets/friction-map.png" width="100%" alt="The HTML report for the bundled example: headline numbers, then What to fix first (point agents at the source of the generated schema_gen.py, use make test instead of pytest, write down the rule the human keeps repeating), then a treemap of files sized by time lost.">
  </picture>
</a>

<sub>The HTML report for the bundled example (`agent-friction demo -o demo.html`). <a href="https://nurettincoban.github.io/agent-friction/demo/">Open it live</a>.</sub>

## Apply the fixes

In Claude Code, install the plugin once:

```text
/plugin install agent-friction --marketplace nurettincoban/agent-friction
```

Then, inside any repository:

| Command | What it does |
| --- | --- |
| `/agent-friction:scan` | Maps the friction and lists what to fix first. Changes nothing. |
| `/agent-friction:fix` | Checks each finding against the repository, proposes the smallest change for each (a line in `AGENTS.md`, a hook, a deny rule) and applies only what you approve. |
| `/agent-friction:compare` | A week or two later: measures the sessions since that scan against it, so you see what went down. |

The plugin carries its own copy of agent-friction and needs only Python 3.10+. With any other
agent, `agent-friction scan -f prompt` prints the same fixes as a task to paste into a
session. Details: [docs/usage.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/usage.md#apply-the-fixes).

## How it differs from `/insights`

Claude Code's `/insights` and agent-friction answer different questions, and work well
together. We [ran both on the same sessions](https://github.com/nurettincoban/agent-friction/blob/main/docs/comparison.md):

| | `/insights` | agent-friction |
| --- | --- | --- |
| Question | How do you and Claude work together? | Where does this repository trip agents up? |
| Method | A model summarizes a sample of your recent sessions | Documented rules over every session that touched the repo; no model, no network |
| Evidence | Narrative with examples | Counts per file and command, each event traceable to a transcript line |
| Same input, same output | No | Yes, so you can compare before and after |
| The fix | CLAUDE.md sections, hooks and skills to copy | Lines and deny rules to paste, each deny rule replayed against your sessions |
| In our test (2 synthetic datasets we built, and tuned agent-friction on; 3 runs of `/insights` each) | 11 of 16 points on planted problems in every run; read 4 of 5 and 3 of 20 sessions | 16 of 16; read every session |
| Also covers, in our test | Prompting and verification habits, larger workflow ideas | `pytest` vs `make test`, empty searches, 280 refusals by Claude Code's own `sleep` rule |

## How it works

- **Deterministic signals.** Human corrections, declined and refused calls, failing commands,
  missed edits, retries, empty searches and more: each one a rule with a documented definition
  and known false positives ([signals](https://github.com/nurettincoban/agent-friction/blob/main/docs/signals.md)).
- **Pinned to your paths.** Each event is attributed to a file, directory or command, and ranked
  by how consistently it recurs across sessions, so one bad afternoon does not top the list
  ([methodology](https://github.com/nurettincoban/agent-friction/blob/main/docs/methodology.md)).
- **Honest about evidence.** Advice resting on fewer than 5 events is marked "thin evidence" and
  kept out of "What to fix first". A rule of the agent tool itself (like Claude Code refusing a
  foreground `sleep`) is reported apart and counts in no score: it is not your repository's. It reads your
  `AGENTS.md`, `CLAUDE.md` and settings first, and says when the advice is already written down.

## Privacy

Everything runs locally: no network calls, and the HTML report loads nothing. Excerpts are
redacted and truncated, and `--no-excerpts` removes all transcript text. Details in
[docs/usage.md](https://github.com/nurettincoban/agent-friction/blob/main/docs/usage.md#privacy) and [SECURITY.md](https://github.com/nurettincoban/agent-friction/blob/main/SECURITY.md).

## Learn more

- [Usage](https://github.com/nurettincoban/agent-friction/blob/main/docs/usage.md): every command, the improvement loop, teams and CI, Windows, what each signal means
- [Methodology](https://github.com/nurettincoban/agent-friction/blob/main/docs/methodology.md): how scores, time lost and recommendations are computed
- [Signals](https://github.com/nurettincoban/agent-friction/blob/main/docs/signals.md) and the [JSON schema](https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md)
- [Comparison with `/insights`](https://github.com/nurettincoban/agent-friction/blob/main/docs/comparison.md), with both reports
- [Website](https://nurettincoban.github.io/agent-friction/) and [live demo](https://nurettincoban.github.io/agent-friction/demo/)

Alpha. Claude Code is supported natively; other agents through a
[small JSONL format](https://github.com/nurettincoban/agent-friction/blob/main/docs/schema.md#generic-transcript-format). Wrong or missed hotspots are the
most useful report you can send:
[open one here](https://github.com/nurettincoban/agent-friction/issues/new?template=false_positive.yml).

## Contributing

`make install && make check`. See [CONTRIBUTING.md](https://github.com/nurettincoban/agent-friction/blob/main/CONTRIBUTING.md) for the ground rules (zero
dependencies, every heuristic documented and tested in both directions, deterministic output).

## License

[MIT](https://github.com/nurettincoban/agent-friction/blob/main/LICENSE)
