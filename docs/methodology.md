# Methodology

How every number in an agent-friction report is computed. The goal is that a
skeptical reader can reproduce any score by hand from the evidence lines. All
constants live next to the code that uses them and are listed here.

## Pipeline

1. **Locate transcripts.** Claude Code project directories for the repo, its
   subdirectories, its git worktrees and its parent directories (or the paths
   you pass). See [Discovery](#discovery).
2. **Normalize.** Each transcript becomes a list of events: tool call, tool
   result, human message, interrupt. Thinking blocks and assistant prose are
   discarded at this step and never reach the output.
3. **Detect.** Deterministic rules turn events into friction events
   ([signal catalog](signals.md)). Git history adds `human-rework`.
4. **Attribute** each event to a file, a directory or a normalized command.
5. **Score, estimate time, grade confidence** per hotspot.
6. **Recommend** from the signal mix.
7. **Redact** every piece of transcript text that is emitted.

## Discovery

Claude Code keeps one directory per working directory under
`<config>/projects/`, named after the path with every non-alphanumeric
character replaced by `-`. `<config>` is `$CLAUDE_CONFIG_DIR` (a single
directory) if set, otherwise `~/.claude` and `~/.config/claude`.

- Directories for the repo root, its git worktrees and their subdirectories
  are used. Because the encoding is lossy (`/a/b-c` and `/a/b/c` collide),
  each session is checked against the working directory recorded inside it.
- Directories for **parent directories** of the repo are scanned too, because
  starting the agent in `~` or a monorepo root is common. A session from
  there is read only if it (or one of its subagent transcripts) contains the
  repo's path, and then only its events inside the repo count.

## Sessions

- `<id>.jsonl` is session `<id>`. `<id>/subagents/*.jsonl` belongs to session
  `<id>`: subagents do not inflate session counts.
- Lines with a `uuid` already seen in another file are skipped (resumed
  sessions replay earlier lines).
- A session with no activity in the repo (no working directory inside it and
  no path that resolves into it) is left out and reported as
  `sessions_outside_repo`.
- For `human-rework`, each agent commit acts as a session (`git:<sha>`). These
  count towards a hotspot's `sessions` but not towards `totals.sessions`.

## Attribution

**Paths.** Absolute paths are matched lexically against the repo root, its
symlink-resolved form, its git worktrees and any `repo_aliases`. Relative
paths are resolved against the session's working directory. Paths outside the
repo and inside `.git` are dropped. Symlinks in the target path are not
followed, and files need not exist any more.

**Commands.** A command line is reduced to a short key so that variants group
together:

1. Heredoc bodies are cut off; the last `;`/newline-separated statement is
   used, because its exit status is the one the shell reports.
2. In that statement, pipeline tails (`| tail -20`), redirections, leading
   `VAR=value` assignments and wrappers (`sudo`, `env`, `time`, `timeout N`,
   ...) are stripped. Of a `&&`/`||` chain, the **last significant** command
   names it: setup (`cd`, `export`, `source`, `echo`, ...), housekeeping
   (`mkdir`, `touch`, `rm`, `cp`, `mv`, ...) and read-only inspection steps
   are passed over, so `mkdir -p out && pytest tests/x.py` is `pytest`. If
   nothing significant is left, the last command is used.
3. Executables under `bin/`, `.venv/`, `node_modules/` or given by absolute
   path become their basename; `python3`, `python3.12` become `python`.
4. Interpreters keep `-m module` or the script path (`python -m pytest`,
   `python scripts/gen.py`). Common single-purpose tools (`pytest`, `ls`,
   `rm`, ...) keep no arguments. Otherwise one lower-case subcommand word is
   kept (`git commit`, `make test`, `cargo test`), and a second one after
   `run`, `exec`, `compose`, `pr`, ... (`npm run build`, `uv run pytest`).

Examples: `CI=1 npm test -- foo` -> `npm test`;
`cd api && pytest tests/x.py -x 2>&1 | tail` -> `pytest`;
`touch x && uv pip install -e . && python -m pytest | tail -5` -> `python -m pytest`.

## Score (0-100)

For one hotspot (file, directory or command):

```
event value      = severity(signal) x weight x share
session points   = log2(1 + sum of event values in that session)
intensity        = sum of session points over all sessions
score            = 100 x (1 - exp(-intensity / 10))
```

- **severity** is the per-signal constant from the [catalog](signals.md).
- **weight** is 1 except: `user-correction` uses its confidence (1.0, or 0.4
  for a "probable" correction), and an ordinary `command-failure` uses 0.3.
- **share** is 1 except when one correction is split over several files.
- **log2 per session** gives diminishing returns: a session contributes one
  more point each time its friction doubles, so one pathological session
  cannot dominate. **Summing over sessions** is linear, so recurrence is what
  moves the score: four sessions with one edit miss each (4 x log2(3) = 6.3)
  outrank one session with twelve (log2(25) = 4.6).
- The scale constant 10 means intensity 10 maps to a score of 63. The score is
  absolute, not relative to other hotspots, so it is comparable across runs.

The output includes `score_detail` (intensity and the per-session values) so
the arithmetic can be checked. Directory nodes in the tree are scored with the
same formula over all events beneath them, each event counted once.

Rough reading: below 15 is a single incident; 30-60 is a pattern worth a line
in AGENTS.md; above 60 is a place where agents reliably struggle.

## Estimated time lost

Each event may claim a wall-clock interval from transcript timestamps:

| Event | Interval |
| --- | --- |
| `edit-miss`, `stale-edit` | from the failed call until the next successful edit of the same file in the same turn; if there is none, until the next transcript event |
| `command-failure` (setup/invocation) | from the failed call until the end of the command that recovered it (same command succeeding, or another non-inspection command succeeding, within the next 5 shell calls of the turn); otherwise until the next event |
| `command-failure` (ordinary) | none |
| `path-miss`, `search-miss`, `tool-rejected` | from the call until the next event (one wasted round trip) |
| `edit-revert` | from the original edit to the revert |
| `user-correction`, `user-interrupt` | the agent's working time in the corrected turn (first tool call to last agent activity / the interrupt) |
| `retry-loop`, `edit-thrash`, `read-loop`, `human-rework` | none (no defensible estimate) |

Every interval is capped at **5 minutes**. `est_minutes_lost` for a hotspot,
a tree node or the whole report is the length of the **union** of its
intervals (merged per transcript stream), so overlapping events are never
counted twice. Human idle time between the agent finishing and the human
replying is not included.

This is an estimate of agent wall-clock time that demonstrably went into
failed calls or redirected work. It is not a measure of human time, and it is
conservative for failures (one round trip unless a recovery is visible) and
generous for corrections (the whole turn, up to the cap, is treated as
redirected). Because intervals are not divided by share, minutes of different
hotspots can overlap; only `totals.est_minutes_lost` is free of double
counting. Events without timestamps claim nothing.

## Confidence

| Level | Rule |
| --- | --- |
| `high` | 3 or more distinct sessions **and** 2 or more distinct signal types, not counting hotspots whose only signals are the weak ones (`read-loop`, `edit-thrash`) |
| `medium` | 2 or more distinct sessions |
| `low` | a single session |

Confidence says how sure we are that the hotspot is a recurring property of
the codebase rather than one bad day. It is independent of the score.

## Evidence selection

Up to `max_evidence` events per hotspot: signals are visited round-robin from
most to least severe; within a signal, events from sessions not shown yet come
first, then heavier and newer ones. The list is returned chronologically.
`line` is the 1-based line in the transcript file (the tool result or human
message that proves the event; 0 for git).

## Recommendations

Rules fire only at an evidence threshold (2 events unless noted), and quote
the evidence that triggered them.

| Id | Fires when |
| --- | --- |
| `edit-the-source-not-the-output` | the file looks generated (marker such as "DO NOT EDIT" / "generated by" in its first 2 KB, or a path like `*_pb2.py`, `*.min.js`, lock files, `dist/`) and has any edit-related or rework event |
| `split-or-anchor-large-file` | 2+ `edit-miss` and the file has 800+ lines |
| `make-edits-land` | 2+ `edit-miss` on a file that is neither large nor generated (wording depends on whether misses were mostly ambiguous or not-found) |
| `document-what-rewrites-this-file` | 2+ `stale-edit` of the "modified since read" kind |
| `path-agents-expect` | 2+ `path-miss` |
| `write-down-the-rule` | corrections whose attributed shares sum to 0.5 or more; quotes up to 3, unambiguous (confidence 1.0) ones only |
| `state-the-boundary` | 2+ `tool-rejected`, or 1 with a stated reason |
| `clarify-before-acting` | 2+ `user-interrupt` |
| `add-a-fast-check` | 2+ `edit-thrash` / `edit-revert` events |
| `learn-from-rework` | 2+ `human-rework` |
| `document-how-to-run` | 2+ ordinary command failures naming the file |
| `add-a-pointer` | 2+ `search-miss` in the file or directory; lists the patterns |
| `summarize-this-file` | 2+ `read-loop` |
| `use-the-working-command` | 2+ failures of a command were followed by the same successful other command |
| `document-the-command` | 2+ setup/invocation failures of a command (and no clear working alternative) |
| `make-the-failure-actionable` | 1+ `retry-loop` on a command |

## Privacy and redaction

Transcripts contain secrets. The engine therefore:

- never emits thinking blocks or assistant prose;
- keeps everything a human typed, searched for or saw printed (prompts,
  search patterns, command lines, command output, reasons for declining,
  commit subjects) in exactly two places: `excerpt` and the quotes inside
  recommendation details. Both disappear with `include_excerpts=False`.
  What remains then is structural: tool names, repo-relative paths, counts,
  commit hashes and **normalized command names** such as `pytest` or
  `npm run build` (the identity of a command hotspot; arguments are never
  part of it);
- redacts **before** truncating (default 240 characters), so a secret cannot
  be cut in half and slip through;
- applies these rules, in order: private-key blocks; AWS access keys; vendor
  API keys (Anthropic, OpenAI-style `sk-`, GitHub, GitLab, Slack, Google,
  Stripe, npm, Hugging Face); JWTs; `Authorization:` header values;
  `Bearer`/`Basic` credentials; credentials in URLs (`scheme://user:pass@`);
  `-u user:pass` / `--user`; `--password x` style flags; `password=`,
  `token:`, `api_key=`, `secret=` ... assignments; prose such as "the
  password is x"; e-mail addresses; hex strings of 32+ characters;
  mixed-case alphanumeric strings of 24+ characters with Shannon entropy of
  at least 4.0 bits per character; home directories (`/home/x`, `/Users/x`,
  `C:\Users\x`, the real home) become `~`.

Redaction is pattern matching and will miss secrets that look like ordinary
words. Review a report before publishing it, or use `include_excerpts=False`.
Claude Code's project directory names embed the working directory (including
a user name) and appear in `sources[].path` and `evidence[].source`.

## Known limitations

- **Heuristics, not understanding.** A correction is recognised by its opening
  words; a setup error by its output. Both directions of error exist and are
  listed per signal in the catalog.
- **Friction is not always the codebase's fault.** Some events reflect the
  agent, the prompt or the harness. The recurrence requirement behind
  "medium" and "high" confidence is the main defence; treat "low" as a lead.
- **Red tests are normal.** Ordinary command failures are down-weighted, not
  removed; a test-heavy session still adds a little score to `make test`.
- **Corrections are English-only.** Correction phrases are matched in
  English; corrections in other languages are missed (never misread).
- **Corrections are attributed by proximity.** If the human corrects
  something unrelated to the files just edited, the attribution is wrong.
- **Command grouping is coarse.** `pytest tests/a.py` and `pytest tests/b.py`
  are one hotspot; `pytest` and `python -m pytest` are two. When `a && b`
  fails we cannot tell which step failed and name the last significant one.
- **Corrections are under-counted on purpose.** Only unambiguous phrasing, or
  two weaker cues together, counts; polite, indirect and non-English
  corrections are missed.
- **Only what is recorded.** A session started above the repo is found only
  if it mentions the repo's absolute path. Rework that never reaches the
  current branch is invisible. Squashed merges lose agent
  trailers.
- **Time is wall-clock.** A machine that slept mid-call would inflate an
  interval; the 5-minute cap bounds the damage.
- **Encoded directory names are ambiguous.** `/a/b-c` and `/a/b/c` map to the
  same Claude Code directory; sessions are checked against their recorded
  working directory to tell them apart. Very long paths that Claude Code
  shortens with a hash are not matched.
- **Format drift.** The Claude Code transcript format is not a public,
  versioned contract. Unknown line types are ignored and malformed lines are
  counted (`skipped_lines`), never fatal.
