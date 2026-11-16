# Schemas

1. [Analysis result](#analysis-result) — what `analyze()` returns and
   `--format json` (`-f json`) prints.
2. [Comparison result](#comparison-result) — what `compare -f json` prints.
3. [Generic transcript format](#generic-transcript-format) — the JSONL input
   for transcripts from agents other than Claude Code.

## Analysis result

```python
from agent_friction.engine import analyze, AnalyzeOptions

result = analyze(AnalyzeOptions(repo=Path(".")))
```

The result is a plain `dict`, JSON-serializable and deterministic: the same
inputs and the same `now` give byte-identical JSON. `schema_version` is `1`;
fields may be added within a version, never removed or re-typed.

### `AnalyzeOptions`

`analyze` and `AnalyzeOptions` are the Python entry point the CLI is built on. They are
typed and covered by tests, but until 1.0 only the JSON output carries a stability
promise. Every option has a CLI flag: `transcripts` is `-t`, `since`/`until` are
`--since`/`--until`, `include_git=False` is `--no-git`, `include_excerpts=False` is
`--no-excerpts`, `max_evidence` is `--max-evidence`, `repo_aliases` is `--repo-alias`,
`rework_window_days` is `--rework-days`, `agent_commit_patterns` is `--agent-commit`
(added to the defaults), `bot_commit_patterns` is `--bot-author`, and
`allow_no_transcripts` is `--allow-empty`.

| Field | Default | Meaning |
| --- | --- | --- |
| `repo` | required | Repository root. |
| `transcripts` | `None` | Files or directories (searched recursively for `*.jsonl`). `None` auto-discovers Claude Code transcripts for the repo, its subdirectories, worktrees and parent directories (see methodology, Discovery). The format of each file is detected from its first lines. |
| `since`, `until` | `None` | Only **events** whose own timestamp lies inside this window: `since <= ts < until` (naive datetimes are UTC). `until` is exclusive, so a scan `until` a moment and a scan `since` the same moment never share an event. The filter is per event, not per session or file: a session that straddles a boundary contributes only its events inside it; an event without a timestamp inherits the previous event's. Events of the same session *before* `since` are still read as context (which files the agent had edited when a correction arrives, which run was red before an edit), and so are its events at or *after* `until` (whether a red run was then fixed, recovered, only waited on, or put right by a setup step), but nothing that happened outside the window is counted, exposed or listed. A scan `until` a moment plus a scan `since` it therefore add up to the full scan for what one session decides; verdicts that need several sessions (a flaky test, a wrong runner, a standing red test: 3 sessions) are reached within each window on its own. Git commits are filtered by author date. |
| `include_git` | `True` | Add `human-rework` from `git log`. Silently unavailable outside a git repo. |
| `include_excerpts` | `True` | `False` removes everything verbatim: excerpts become `null` and recommendations stop quoting prompts, search patterns, error lines and reasons. Repo paths remain, but a path the agent only *guessed* is not a repo path: from the first component that neither exists in the repository now nor was ever read, edited or written successfully in the transcripts, each component is shown as `<path:6 hex digits>` (`src/<path:1f0c2a>/<path:9b7e44>`; the same name always gives the same placeholder). The directory in a command key (`npm test (web/)`) is kept: the command ran there, so it existed; command keys keep only publicly known words, and project-specific names in them become hash placeholders such as `make <target:37bd73>` (methodology, Attribution). |
| `max_evidence` | `5` | Evidence items per hotspot. |
| `max_hotspots` | `None` | Maximum entries in each of `hotspots`, `commands`, `directories`; `None` lists every one. Nothing else depends on it: totals, the tree, `exposure` and `unattributed` are never truncated, and scores do not change with it. |
| `now` | `None` | Fixed `generated_at`, for reproducible output. |
| `excerpt_chars` | `240` | Maximum excerpt length. |
| `repo_aliases` | `()` | Other absolute paths that are this repo, e.g. where it lived on the machine that recorded the transcripts. Windows paths (with a drive letter) are compared case-insensitively, and when the repository or the session's working directory is such a path, the spellings Unix-style shells on Windows use for a drive (`/c/Users/...` in Git Bash, `/mnt/c/...` in WSL, `/cygdrive/c/...`) are read as that drive. |
| `rework_window_days` | `14` | Window for `human-rework`. |
| `agent_commit_patterns` | `None` | Regexes (case-insensitive, multiline) matched against `author: Name <email>\n<commit message>`; replaces the defaults. |
| `bot_commit_patterns` | `()` | Extra regexes, matched the same way against `author: Name <email>`, for automation whose commits are never human rework (added to `gitsignals.DEFAULT_BOT_PATTERNS`: `[bot]` authors, dependabot, renovate, pre-commit-ci, github-actions, ...). |
| `git_max_commits` | `5000` | Newest commits read from the current branch. |
| `allow_no_transcripts` | `False` | `True`: if no transcript is found but git history is readable (and `include_git` is on), analyse git history alone (`human-rework` only) instead of raising. |

`analyze` raises `agent_friction.engine.NoTranscriptsError` (a subclass of
`agent_friction.errors.AgentFrictionError`) when no transcript file is found.
`agent_friction.discovery.discover_transcripts(repo)` returns the files that
auto-discovery would use.

`agent_friction.engine.list_transcripts(options) -> list[Path]` returns
exactly the transcript files `analyze(options)` would read (explicit
`transcripts` expanded, or auto-discovery), and raises `NoTranscriptsError`
in the same cases: nothing found; a given path that does not exist; a given
*file* in which none of the first 200 lines is a JSON object.

`agent_friction.engine.inspect_transcripts(options) -> list[tuple[Path, int,
int]]` parses the same files and returns `(file, events, skipped_lines)` for
each: `events` is what the parser understood of the file before any filtering
by repository or time window, so `0` marks an empty, binary or foreign file.

### Top level

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | int | `1`. |
| `tool` | object | `{"name": "agent-friction", "version": str}`. |
| `generated_at` | str | ISO-8601 UTC (`...Z`). |
| `repo` | object | `root` (absolute; only the current user's own home directory is shown as `~`) and `name`. |
| `settings` | object | What kind of scan this is: `excerpts` (bool, `include_excerpts`), `git` (bool: git history was read and used), `key_mode` (`"plain"`, or `"pseudonymized"` when command keys carry hash placeholders, i.e. without excerpts). Reports whose `settings` differ should not be compared: their command keys or evidence base differ. *Addition to the base contract.* |
| `window` | object | See below. |
| `sources` | list | One entry per input, see below. |
| `totals` | object | See below. |
| `signals` | list | The full catalog with counts, including zeros. |
| `hotspots` | list | File hotspots, sorted by score (desc), events (desc), path. |
| `commands` | list | Command hotspots, same shape, `kind: "command"`, `path` is the normalized command ([forms](#forms-of-a-command-key)). |
| `directories` | list | Directories that events were attributed to directly (searches scoped to a directory), same shape, `kind: "dir"`, `path` without trailing slash, `"."` for the repo root. *Addition to the base contract.* |
| `tree` | object | Directory roll-up of all file and directory events. |
| `unattributed` | list | Evidence for the events counted in `totals.unattributed_events`: events that are in `friction_events` and the lost time but in no hotspot (an interrupt with no tool call to pin it on, a declined tool without a path, a correction with no file in reach). Items have the [`evidence[]`](#evidence) shape; at most 20 (`engine.MAX_UNATTRIBUTED_EVIDENCE`), chosen like a hotspot's evidence and listed chronologically, so the list can be shorter than the count. Empty when every event has a target. *Addition to the base contract.* |
| `exposure` | object | How many sessions touched each target, friction or not. See below. *Addition to the base contract.* |

### `window`

| Field | Meaning |
| --- | --- |
| `start` | `since` if given, else the first event's timestamp; `null` if there are no events. |
| `end` | `until` if given, else the last event's timestamp. |
| `since`, `until` | The options as given (`null` if unset). |
| `first_event`, `last_event` | Earliest and latest timestamp actually analysed. |

### `sources[]`

| Field | Meaning |
| --- | --- |
| `kind` | `"claude-code"`, `"generic"`, `"git"`, or `"unreadable"` for a transcript file that could not be read (see `error`). |
| `path` | `claude-code:<repo name>` for Claude Code transcripts (the project directory encodes an absolute path and is never shown), the file for generic transcripts (home as `~`; only `generic:<file name>` with `--no-excerpts`), or the repo root for git. |
| `sessions` | Distinct sessions with activity in the repo (for git: agent commits in the window). |
| `skipped_lines` | Lines that were not valid JSON objects or lacked required structure. |
| `skipped_events` | Events on which a detector raised an unexpected error. They are skipped; anything above 0 is a bug worth reporting. |
| `ignored_lines` | Valid lines of a type that is not analysed (attachments, summaries, ...). |
| `duplicate_lines` | Lines that an earlier file had already contributed (the same line uuid, or for generic files the same session, timestamp, kind and payload) and that were therefore not read again: a copied or re-exported log. Not for git. *Addition within schema version 1.* |
| `files` | Transcript files read (not for git). |
| `sessions_outside_repo` | Sessions dropped because they never touched this repo (not for git). |
| `available`, `note` | git only: whether history could be read, and why not. |
| `error` | `kind: "unreadable"` only: why the file could not be read, in fixed words: `"permission denied"`, `"file not found"`, `"is a directory"` or `"could not be read"`. One entry per file; `path` is the file name alone (its directory may name a user or a project), `files` is 1 and every count is 0. Nothing from such a file is in the result. *Addition to the base contract.* |

### `totals`

| Field | Meaning |
| --- | --- |
| `sessions` | Distinct transcript sessions analysed (subagents belong to their parent; git commits are not counted). |
| `episodes` | Episodes of work in those sessions: a session id counts once for every time it was picked up again after 2 hours or more of silence (methodology, Episodes). Equal to `sessions` when no session was. *Addition to the base contract.* |
| `tool_calls` | Tool calls in those sessions. |
| `tool_calls_in_repo` | Those of `tool_calls` that acted inside this repository: a file tool on a path in it, a shell command whose judged step runs in it, any other tool called from a working directory in it. Equal to `tool_calls` unless a session was started in a parent directory and also worked on neighbouring projects. This is the number to put next to the friction counts. Reported ends of background commands are in neither count. *Addition to the base contract.* |
| `friction_events` | All detected events, equal to the sum of `signals[].count`. |
| `est_minutes_lost` | Union of all claimed intervals, in minutes (1 decimal). |
| `dropped_red_runs` | Failed commands that were seen and deliberately **not** reported as friction: red runs of the development loop (red, edit, green), commands the agent was only waiting on (failed, then passed untouched), and red runs that went green after a one-off command. They are in no hotspot, signal count or `friction_events`; the number says how much was looked at and set aside. Mistyped commands are not in it: they stay events, as weak evidence. *Addition to the base contract.* |
| `dropped_minutes` | What those runs took, from call to result, in minutes (1 decimal). A sum of run times, not lost time: it is not part of `est_minutes_lost`. *Addition to the base contract.* |
| `corrections` | `user-correction` events: clear wording **and** corroborated by what the human named or the agent did next. A lower bound. |
| `probable_corrections` | `probable-correction` events (wording only, or two weak cues): a separate, weak signal. Listed as evidence, never quoted, never the basis of a recommendation. |
| `files_affected` | File hotspots before `max_hotspots` truncation. |
| `commands_affected`, `directories_affected` | Likewise. |
| `third_party_events` | Events set aside because everything they were about lies in third-party or build directories (`node_modules/`, `.venv/`, `vendor/`, git-ignored directories, ...; methodology, Attribution). They are in no hotspot, signal count or `friction_events`. *Addition to the base contract.* |
| `unattributed_events` | Events that could not be tied to a path or command. Their evidence is the top-level `unattributed` list. |

### `exposure`

```json
{
  "file": {"src/app.py": 7, "src/util.py": 2},
  "command": {"make test": 6, "pytest": 4},
  "dir": {"src": 3}
}
```

For every target the agent touched in the window, the number of distinct
sessions that touched it (methodology, [Exposure](methodology.md#exposure)),
whether or not any friction was recorded there. Keys within each object are
sorted. The list is not truncated by `max_hotspots`. Every hotspot's
`exposure_sessions` equals the entry for its `path` here. `file` keys are
repo-relative paths; `command` keys are normalized command keys (the same
strings as `commands[].path`, and the only thing derived from command lines
that is present with `--no-excerpts`); `dir` keys are directories
that were the scope of a search. Agent commits from git do not add exposure
(only a commit with rework outside every analysed session counts, for the
file it reworked).

Use it to compare two reports: a hotspot that is missing from the later one
was fixed only if its target still has exposure there.

### `signals[]`

`id`, `title`, `description`, `count`, plus `severity` (score weight), `why`
(why it indicates friction) and `attribution` (how it is tied to a path).

### Hotspot (`hotspots[]`, `commands[]`, `directories[]`)

| Field | Type | Meaning |
| --- | --- | --- |
| `path` | str | Repo-relative POSIX path, or the normalized command. |
| `kind` | str | `"file"`, `"command"` or `"dir"`. |
| `score` | float | 0-100, one decimal. See methodology. |
| `sessions` | int | Distinct sessions with an event here, including `agent_commits`. |
| `agent_commits` | int | How many of `sessions` are agent commits from git whose rework no analysed transcript session accounts for (0 without git). `sessions - agent_commits` is the number of transcript sessions. |
| `exposure_sessions` | int | Distinct sessions in which the agent touched this target at all, plus `agent_commits`; never less than `sessions`. |
| `friction_rate` | float | `sessions / exposure_sessions`, two decimals, 0-1. |
| `episodes`, `exposure_episodes` | int | The same two counts over episodes of work instead of session ids (plus `agent_commits`). These, not the session counts, are what `score_detail.recurrence`, `score_detail.consistency` and `confidence` are computed from; they equal `sessions` / `exposure_sessions` unless a long-lived session id was picked up again after a break. *Addition to the base contract.* |
| `events` | int | Friction events attributed here. |
| `est_minutes_lost` | float | Union of these events' intervals. |
| `signals` | object | `{signal_id: count}`, most frequent first. |
| `confidence` | str | `"low"`, `"medium"` or `"high"`. |
| `why` | str | One sentence: counts, friction sessions out of exposure sessions, top signals, reason for the confidence. |
| `recommendations` | list | `{id, title, detail, signals, documented_in}`; `signals` lists the signal ids the rule is based on. `documented_in` is a list of `{"file": "AGENTS.md", "line": 12}` (repo-relative path, 1-based line; at most 3; empty when nothing was found): places where the repository's instruction files already name what the recommendation is about, in which case `title` and `detail` are in their "already documented" form (methodology, Recommendations). The lines themselves are never quoted. *Addition to the base contract.* Two rules also name what they propose as data: a `use-the-working-command` recommendation that proposes another command has `replacement` (that command's normalized key, the same string as its key in `exposure.command`; absent when the advice is another program name or a variable in front of the same command), and `document-the-setup-step` has `setup` (the key of the setup command; several steps are joined with ` && `). Both are subject to `--no-excerpts` pseudonymization like any command key. May be empty. `title` and `detail` are Markdown: commands and quoted search patterns are inline code spans, fenced with more backticks than the value contains (a pattern ``a`b`` is written ```` ``a`b`` ````), so they stay valid whatever the transcript held. |
| `evidence` | list | Up to `max_evidence` items, chronological. |
| `score_detail` | object | The score's factors, each 0-1: `severity`, `recurrence`, `consistency` (`score` is 100 x their product, up to rounding); `mean_session_points`; `intensity` (sum of session points); `capped` (true when all evidence is weak and the score was limited to 19); and `sessions`: one entry per episode of work with `session_id`, `episode` (the session id for a session's first episode, `<session id>#<n>` for its n-th), `weighted_events`, `points`. |
| `file` | object | File hotspots only: `exists`, `lines` (`null` if missing or over 5 MB), `generated`. |

### `evidence[]`

| Field | Meaning |
| --- | --- |
| `signal` | Signal id. |
| `session_id` | Transcript session, or `git:<sha>` of the agent commit when no analysed session made it. |
| `timestamp` | ISO-8601 UTC; `null` only for generic events without `ts`. |
| `trailing` | `true` on a `command-failure` that is the last thing that happened in its session (no later tool call, no later human message): the session was still running or was abandoned, and nothing shows whether the failure was resolved. It counts like any other event. The key is absent otherwise. *Addition within schema version 1.* |
| `summary` | What happened, built from structure only: tool name (without excerpts, a name that is not a built-in tool of a known agent is shown as `<tool:hash>`, an MCP tool as `mcp__<server:hash>__<tool:hash>`), repo-relative path, normalized command name, counts, commit hashes. Never contains prompts, search patterns, command arguments or output. |
| `excerpt` | Redacted, truncated transcript text (error output, the command, the human's words, the search pattern), or `null`. Always `null` with `--no-excerpts`, and always `null` for `probable-correction` (a guess never quotes the human). May contain newlines. |
| `source` | Where to look the event up: `claude-code:<repo name>/<session file>.jsonl` (never the project directory), a generic transcript file as it was passed (home as `~`; a file that could only be named by an absolute path is named relative to the working directory or to the `--transcripts` directory it was found in, `logs/nightly/run.jsonl`, or else by its file name), or `"git"`. `null` for transcript evidence with `--no-excerpts`. |
| `line` | 1-based line number in `source` (int). For git evidence, the short hash of the human commit (str, 8 hex digits), so that `git:<line>` is something `git show` accepts. |
| `est_seconds_lost` | float. Seconds of agent time this event alone claims (the length of its interval, one decimal); `0.0` if it claims none. A hotspot's `est_minutes_lost` is the union of its events' intervals, so it equals the sum of these only when they do not overlap. *Addition within schema version 1.* |

### `tree`

Recursive nodes: `name`, `path` (`""` for the root), `kind` (`"dir"` or
`"file"`), `score`, `events`, `sessions`, `exposure_sessions`,
`friction_rate`, `est_minutes_lost`, `children` (sorted by score desc, then
path; empty for files). A directory's numbers cover every event beneath it,
each counted once; its `exposure_sessions` counts every session that touched
anything beneath it. Commands are not in the tree.

### Forms of a command key

A key is the program and the words that say what it does, without arguments:
`pytest`, `npm test`, `make check`, `uv run pytest`, `git push`. Three
additions:

| Form | Meaning |
| --- | --- |
| `python <inline>` | Code given on the command line: `python -c '...'`, `node -e '...'`, `ruby -e`, `perl -e`, `bun -e`, `php -r`. The interpreter's name is kept (`node <inline>`). |
| `python <stdin>` | An interpreter told to read its script from standard input (`-`): `python3 - <<'EOF' ... EOF`, `echo ... \| python -`, `node - < x.js` (`node <stdin>`). |
| `npm test (web/)` | A tool that works on the directory it is run in, run in a subdirectory of the repo: the repo-relative directory follows in parentheses, with a trailing slash. The suffix combines with the other two forms: `python <stdin> (web/)`, `python <inline> (tools/gen/)`. No suffix means the repo root. |

Without excerpts (`key_mode: "pseudonymized"`), project-specific words of a
key become hash placeholders (`make <target:37bd73>`); `<inline>`, `<stdin>`
and the directory suffix, a repo path like any other, are kept as they are.
Tool names in summaries are treated the same way: built-in tools (`Read`,
`Edit`, `Bash`, `Grep`, `WebFetch`, `Task`, ... and the generic format's
operation words) keep their names, any other name becomes `<tool:hash>`, and
an MCP tool `mcp__<server>__<tool>` becomes `mcp__<server:hash>__<tool:hash>`.

## Comparison result

`agent-friction compare before.json after.json -f json` prints one object:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | int | `1`, versioned like the analysis result. |
| `tool` | object | `name` and `version` of the tool that compared. |
| `before`, `after` | object | Each scan's `repo`, `window` and `sessions`. |
| `low_sample` | bool | Either side has fewer sessions than `--min-sessions`; the regression gate is then not applied. |
| `changes` | array | One entry per file, directory or command found in either scan, ordered by status, then by score. |

Each `changes[]` entry:

| Field | Type | Meaning |
| --- | --- | --- |
| `kind`, `path` | string | As in the analysis result. |
| `status` | string | `regressed`, `new`, `improved`, `resolved` (touched afterwards, no friction), `replaced` (not run afterwards, but the command the "before" scan recommended ran instead and failed less), `inactive` (not touched afterwards: no evidence either way), `too-little-data`, or `unchanged`. |
| `before_events`, `after_events` | int | Friction events on each side. |
| `before_exposure_sessions`, `after_exposure_sessions` | int | Sessions that touched the target on each side. |
| `before_events_per_10_sessions`, `after_events_per_10_sessions` | number or null | The rates the verdict is based on; `null` when nothing touched it. |
| `before_score`, `after_score` | number | Scores, for reference; verdicts follow the rates. |
| `replaced_by` | string or null | With `replaced`: the command agents ran instead. |
| `replacement_sessions`, `replacement_events` | int | With `replaced`: sessions that ran it afterwards, and its friction events there. |

## Generic transcript format

To analyse sessions from another agent (Codex, Cursor, Aider, your own
harness), convert its logs to this format: UTF-8 JSONL, one event per line, in
chronological order. A byte-order mark is accepted, and so are UTF-16 and
UTF-32 (with a mark, or recognisable by their first bytes), because that is
what PowerShell's `>` and `Out-File` write; the same holds for Claude Code
files. Anything else is read as UTF-8 with undecodable bytes replaced. Pass the file with `--transcripts FILE` on the command
line or `transcripts=[...]` in `AnalyzeOptions`; it is recognised by the
presence of `session` and a string `kind`. One file may hold several
sessions. There is no dedicated adapter for other agents yet.

Common fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `session` | yes | Session id (string or number). |
| `kind` | yes | `"tool_call"`, `"tool_result"`, `"user_message"`, `"interrupt"` or (optional) `"assistant_message"`. Other kinds are ignored. |
| `ts` | no | ISO-8601 timestamp or epoch seconds. Without it no time is estimated. A value that cannot be parsed, or that lies before the year 2000 (a zeroed clock, a counter), is treated as missing. |
| `cwd` | no | Working directory; relative paths resolve against it. Unknown means the repo root. |
| `subagent` | no | `true` for events of a sub-agent: its `user_message`s are not treated as human. |

`tool_call`:

| Field | Meaning |
| --- | --- |
| `id` | Required. Pairs the call with its result. |
| `op` | `"read"`, `"edit"`, `"write"`, `"shell"`, `"search"` or `"other"`. |
| `tool` | Free-form tool name for display (use `"Glob"` for file-name searches so the pattern's directory prefix is used as scope). |
| `path` | File for read/edit/write; search scope for search. |
| `command` | Shell command line (`shell`). |
| `pattern` | Search pattern (`search`). |
| `old`, `new` | Replaced and replacement text (`edit`), used to detect reverts. |
| `content` | Full content (`write`), used to detect reverts. |
| `range` | Any string identifying the part of the file read; equal values mean "same range". |
| `background` | `true` on a `shell` call that only *launches* the command (it keeps running while the agent goes on). Its own `tool_result` (same `id`), if there is one, is then just the acknowledgement of the launch: neither a success nor a failure. Say how the command ended with a later `tool_result` that has `of` instead of `id` (below). |

`tool_result`:

| Field | Meaning |
| --- | --- |
| `id` | Required (unless `of` is given). The call's id. |
| `of` | Instead of `id`: the `id` of a `background` shell call whose command has now **ended**. `exit_code`, `ok` and `output` then describe that end, and it is judged exactly like a foreground run of the command at this point of the transcript. Optional `status`: `"killed"`, `"stopped"`, `"cancelled"` or `"running"` mean there is no outcome to judge (the line is ignored). Only the first outcome of a launch counts; an `of` that names no background call is ignored. |
| `ok` | `false` marks a failure. If omitted, the call failed when `error` is set or `exit_code` is non-zero. |
| `exit_code` | Shell exit status. |
| `error` | Optional class: `edit_not_found`, `edit_ambiguous`, `not_read`, `stale`, `missing`, `rejected` (a human declined), `denied` (permission settings or a hook refused; `reason` may be `"hook"`), `interrupted`, `exit`, `not_run` (the harness never started the call: ignored), `too_large` (a read refused for size), `error`. If omitted on a failure it is inferred from `output` using Claude Code's error wording. |
| `output` | Tool output or error text (used for excerpts and to classify command failures). |
| `matches` | Number of search results; `0` marks a search miss (alternatively `"empty": true`). |
| `reason` | What the human said when declining (`error: "rejected"`). |

`user_message` needs `text`. `interrupt` needs nothing else.

A line that an earlier file of the same scan already contained (the same
JSON object: session, `ts`, kind and payload) is read once, so a copied or
re-exported log does not double its events. Identical lines within one file
are kept.

`assistant_message` (optional) carries the agent's prose in `text`. Nothing
of it is stored or emitted. It is used for two things: its `ts` marks that
the agent was active (a failed call is then timed up to the agent's next
message instead of only its own duration), and if the agent's last message
before a `user_message` ends by offering something or asking for a choice
("Should I ...?", "Do you want ...?", "A or B?"; not a closing "Anything
else?"), a reply that opens with "No, ...", "Not quite" or "Stop." is read as
an answer rather than a correction. A `tool_call` whose `tool` is a question tool (`ask_user`,
`AskUserQuestion`, ...) has the same effect.

```jsonl
{"session":"s1","ts":"2026-11-05T16:00:00Z","kind":"user_message","text":"Add a sku field"}
{"session":"s1","ts":"2026-11-05T16:00:10Z","kind":"tool_call","id":1,"op":"edit","path":"src/models.py","old":"class Item:","new":"class Item:\n    sku: str","cwd":"/work/shop"}
{"session":"s1","ts":"2026-11-05T16:00:12Z","kind":"tool_result","id":1,"ok":false,"error":"edit_not_found","output":"SEARCH block did not match"}
{"session":"s1","ts":"2026-11-05T16:01:00Z","kind":"tool_call","id":2,"op":"shell","command":"pytest -q"}
{"session":"s1","ts":"2026-11-05T16:01:04Z","kind":"tool_result","id":2,"exit_code":4,"output":"ModuleNotFoundError: No module named 'shop'"}
{"session":"s1","ts":"2026-11-05T16:06:40Z","kind":"user_message","text":"That's wrong, revert that"}
```

Paths in the example are relative to the working directory `/work/shop` given
in `cwd`. To try it as written against a checkout that lives elsewhere, say
where the transcript was recorded: `agent-friction scan --transcripts
example.jsonl --repo-alias /work/shop` (or `repo_aliases=["/work/shop"]`).
Without `cwd`, relative paths resolve against the repository root and no
alias is needed.

A working example is `tests/fixtures/transcripts/other-agents/aider-2026-11-05.jsonl`.
