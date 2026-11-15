# Schemas

1. [Analysis result](#analysis-result) — what `analyze()` returns and
   `--format json` (`-f json`) prints.
2. [Generic event format](#generic-event-format) — the JSONL input for
   transcripts from agents other than Claude Code.

## Analysis result

```python
from agent_friction.engine import analyze, AnalyzeOptions

result = analyze(AnalyzeOptions(repo=Path(".")))
```

The result is a plain `dict`, JSON-serializable and deterministic: the same
inputs and the same `now` give byte-identical JSON. `schema_version` is `1`;
fields may be added within a version, never removed or re-typed.

### `AnalyzeOptions`

| Field | Default | Meaning |
| --- | --- | --- |
| `repo` | required | Repository root. |
| `transcripts` | `None` | Files or directories (searched recursively for `*.jsonl`). `None` auto-discovers Claude Code transcripts for the repo, its subdirectories, worktrees and parent directories (see methodology, Discovery). The format of each file is detected from its first lines. |
| `since`, `until` | `None` | Only events inside this window (naive datetimes are UTC). |
| `include_git` | `True` | Add `human-rework` from `git log`. Silently unavailable outside a git repo. |
| `include_excerpts` | `True` | `False` removes everything verbatim: excerpts become `null` and recommendations stop quoting prompts, search patterns, error lines and reasons. Paths and normalized command names remain. |
| `max_evidence` | `5` | Evidence items per hotspot. |
| `max_hotspots` | `50` | Maximum entries in each of `hotspots`, `commands`, `directories`. |
| `now` | `None` | Fixed `generated_at`, for reproducible output. |
| `excerpt_chars` | `240` | Maximum excerpt length. |
| `repo_aliases` | `()` | Other absolute paths that are this repo, e.g. where it lived on the machine that recorded the transcripts. |
| `rework_window_days` | `14` | Window for `human-rework`. |
| `agent_commit_patterns` | `None` | Regexes (case-insensitive, multiline) matched against `author: Name <email>\n<commit message>`; replaces the defaults. |
| `git_max_commits` | `5000` | Newest commits read from the current branch. |

`analyze` raises `agent_friction.engine.NoTranscriptsError` (a subclass of
`agent_friction.errors.AgentFrictionError`) when no transcript file is found.
`agent_friction.discovery.discover_transcripts(repo)` returns the files that
auto-discovery would use.

### Top level

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | int | `1`. |
| `tool` | object | `{"name": "agent-friction", "version": str}`. |
| `generated_at` | str | ISO-8601 UTC (`...Z`). |
| `repo` | object | `root` (home directory shown as `~`) and `name`. |
| `window` | object | See below. |
| `sources` | list | One entry per input, see below. |
| `totals` | object | See below. |
| `signals` | list | The full catalog with counts, including zeros. |
| `hotspots` | list | File hotspots, sorted by score (desc), events (desc), path. |
| `commands` | list | Command hotspots, same shape, `kind: "command"`, `path` is the normalized command. |
| `directories` | list | Directories that events were attributed to directly (searches scoped to a directory), same shape, `kind: "dir"`, `path` without trailing slash, `"."` for the repo root. *Addition to the base contract.* |
| `tree` | object | Directory roll-up of all file and directory events. |
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
| `kind` | `"claude-code"`, `"generic"` or `"git"`. |
| `path` | Claude Code project directory, generic file, or repo root for git (home as `~`). |
| `sessions` | Distinct sessions with activity in the repo (for git: agent commits in the window). |
| `skipped_lines` | Lines that were not valid JSON objects or lacked required structure. |
| `ignored_lines` | Valid lines of a type that is not analysed (attachments, summaries, ...). |
| `files` | Transcript files read (not for git). |
| `sessions_outside_repo` | Sessions dropped because they never touched this repo (not for git). |
| `available`, `note` | git only: whether history could be read, and why not. |

### `totals`

| Field | Meaning |
| --- | --- |
| `sessions` | Distinct transcript sessions analysed (subagents belong to their parent; git commits are not counted). |
| `tool_calls` | Tool calls in those sessions. |
| `friction_events` | All detected events, equal to the sum of `signals[].count`. |
| `est_minutes_lost` | Union of all claimed intervals, in minutes (1 decimal). |
| `corrections` | Number of `user-correction` events. |
| `files_affected` | File hotspots before `max_hotspots` truncation. |
| `commands_affected`, `directories_affected` | Likewise. |
| `unattributed_events` | Events that could not be tied to a path or command. |

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
that is present with `include_excerpts=False`); `dir` keys are directories
that were the scope of a search. Agent commits from git count for files.

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
| `sessions` | int | Distinct sessions (and agent commits) with an event here. |
| `exposure_sessions` | int | Distinct sessions (and agent commits) in which the agent touched this target at all; never less than `sessions`. |
| `friction_rate` | float | `sessions / exposure_sessions`, two decimals, 0-1. |
| `events` | int | Friction events attributed here. |
| `est_minutes_lost` | float | Union of these events' intervals. |
| `signals` | object | `{signal_id: count}`, most frequent first. |
| `confidence` | str | `"low"`, `"medium"` or `"high"`. |
| `why` | str | One sentence: counts, friction sessions out of exposure sessions, top signals, reason for the confidence. |
| `recommendations` | list | `{id, title, detail, signals}`; `signals` lists the signal ids the rule is based on. May be empty. |
| `evidence` | list | Up to `max_evidence` items, chronological. |
| `score_detail` | object | The score's factors, each 0-1: `severity`, `recurrence`, `consistency` (`score` is 100 x their product, up to rounding); `mean_session_points`; `intensity` (sum of session points); and `sessions`: per session `session_id`, `weighted_events`, `points`. |
| `file` | object | File hotspots only: `exists`, `lines` (`null` if missing or over 5 MB), `generated`. |

### `evidence[]`

| Field | Meaning |
| --- | --- |
| `signal` | Signal id. |
| `session_id` | Transcript session, or `git:<sha>` of the agent commit. |
| `timestamp` | ISO-8601 UTC; `null` only for generic events without `ts`. |
| `summary` | What happened, built from structure only: tool name, repo-relative path, normalized command name, counts, commit hashes. Never contains prompts, search patterns, command arguments or output. |
| `excerpt` | Redacted, truncated transcript text (error output, the command, the human's words, the search pattern), or `null`. Always `null` with `include_excerpts=False`. May contain newlines. |
| `source` | Transcript file (home as `~`) or `"git"`. |
| `line` | 1-based line number in `source`; `0` for git. |

### `tree`

Recursive nodes: `name`, `path` (`""` for the root), `kind` (`"dir"` or
`"file"`), `score`, `events`, `sessions`, `exposure_sessions`,
`friction_rate`, `est_minutes_lost`, `children` (sorted by score desc, then
path; empty for files). A directory's numbers cover every event beneath it,
each counted once; its `exposure_sessions` counts every session that touched
anything beneath it. Commands are not in the tree.

## Generic event format

To analyse sessions from another agent (Codex, Cursor, Aider, your own
harness), convert its logs to this format: UTF-8 JSONL, one event per line, in
chronological order. Pass the file with `transcripts=[...]`; it is recognised
by the presence of `session` and a string `kind`. One file may hold several
sessions. There is no dedicated adapter for other agents yet.

Common fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `session` | yes | Session id (string or number). |
| `kind` | yes | `"tool_call"`, `"tool_result"`, `"user_message"`, `"interrupt"` or (optional) `"assistant_message"`. Other kinds are ignored. |
| `ts` | no | ISO-8601 timestamp or epoch seconds. Without it no time is estimated. |
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

`tool_result`:

| Field | Meaning |
| --- | --- |
| `id` | Required. The call's id. |
| `ok` | `false` marks a failure. If omitted, the call failed when `error` is set or `exit_code` is non-zero. |
| `exit_code` | Shell exit status. |
| `error` | Optional class: `edit_not_found`, `edit_ambiguous`, `not_read`, `stale`, `missing`, `rejected`, `interrupted`, `exit`, `error`. If omitted on a failure it is inferred from `output` using Claude Code's error wording. |
| `output` | Tool output or error text (used for excerpts and to classify command failures). |
| `matches` | Number of search results; `0` marks a search miss (alternatively `"empty": true`). |
| `reason` | What the human said when declining (`error: "rejected"`). |

`user_message` needs `text`. `interrupt` needs nothing else.

`assistant_message` (optional) carries the agent's prose in `text`. Nothing
of it is stored or emitted; it is used for one thing only: if the agent's
last message before a `user_message` ends with a question mark, a reply that
opens with "No, ..." is read as an answer rather than a correction. Leave
these events out and every such reply counts as a correction.

```jsonl
{"session":"s1","ts":"2026-11-05T16:00:00Z","kind":"user_message","text":"Add a sku field"}
{"session":"s1","ts":"2026-11-05T16:00:10Z","kind":"tool_call","id":1,"op":"edit","path":"src/models.py","old":"class Item:","new":"class Item:\n    sku: str","cwd":"/work/shop"}
{"session":"s1","ts":"2026-11-05T16:00:12Z","kind":"tool_result","id":1,"ok":false,"error":"edit_not_found","output":"SEARCH block did not match"}
{"session":"s1","ts":"2026-11-05T16:01:00Z","kind":"tool_call","id":2,"op":"shell","command":"pytest -q"}
{"session":"s1","ts":"2026-11-05T16:01:04Z","kind":"tool_result","id":2,"exit_code":4,"output":"ModuleNotFoundError: No module named 'shop'"}
{"session":"s1","ts":"2026-11-05T16:06:40Z","kind":"user_message","text":"That's wrong, revert that"}
```

A working example is `tests/fixtures/transcripts/other-agents/aider-2026-11-05.jsonl`.
