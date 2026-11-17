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
   discarded at this step and never reach the output. Of the agent's prose
   two things are kept: *when* it was written (a marker that the agent was
   active, used for time estimates) and whether the last message before a
   human reply offered something or asked for a choice (in its final
   paragraph, or with a question/choice tool). Of the text of an edit only hashes are kept, plus
   one bit: whether it only added text. Harness and IDE blocks inside user
   turns (`<system-reminder>`, `<ide_opened_file>`, ...) are stripped, and a
   message from another agent (`<teammate-message>`, `<agent-message>`) only
   marks a new turn. Of a tool result's `permissionDecision` one bit is kept:
   whether the permission settings let the call through or a person did.
3. **Detect.** Deterministic rules turn events into friction events
   ([signal catalog](signals.md)). Git history adds `human-rework`. An event
   a detector cannot handle is skipped and counted
   (`sources[].skipped_events`); it never ends the scan.
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
- **A directory inside a git repository** (one package of a monorepo,
  `agent-friction scan packages/web`) is only that directory. Git reports the
  worktrees of the *enclosing* repository; what counts as the scanned
  directory is the same relative path inside each of them
  (`<worktree>/packages/web`, where it exists), never a worktree's root. A
  session started in the parent counts only if it touched something under
  the scanned directory, and only those calls count: `README.md` of the
  monorepo and the neighbouring package are outside, and what went wrong
  there is in `totals.events_outside_repo`.
- **Worktrees.** The worktrees `git worktree list` reports are roots like the
  repository, including those it calls prunable (the directory is gone, git's
  record is not). A worktree kept *inside* the repository,
  `<repo>/.claude/worktrees/<name>/` (Claude Code's own) or
  `<repo>/.worktrees/<name>/`, is recognised by its path alone, whether or
  not it still exists: such worktrees are removed after a merge, and their
  sessions would otherwise key every file as
  `.claude/worktrees/<name>/src/app.py` and split one hotspot into several.
  **Limit:** a worktree that lived *outside* the repository and was removed
  with `git worktree remove` leaves nothing to ask git about. Its sessions
  are not read. `discovery.removed_sibling_sessions` finds the likely cases
  by name alone (session files recorded in a directory beside the
  repository called `<repo name>-…`, `<repo name>.…` or `<repo name>_…`
  that no longer exists) so that the CLI can name them; passing that
  directory as `--repo-alias` includes them (an alias takes part in
  discovery like a root). A removed worktree with an unrelated name, or
  somewhere else, is invisible.
- Directories for **parent directories** of the repo are scanned too, because
  starting the agent in `~` or a monorepo root is common. A session from
  there is read only if it (or one of its subagent transcripts) contains the
  repo's path, and then only its events inside the repo count.
- Directories filed under **another name for the repo** are used as well: a
  session started through a symbolic link is stored under the link's
  spelling. For each remaining project directory one transcript is opened to
  read its working directory; if that directory exists here and its real
  path is the repository's (or lies inside it), the directory is the repo's.

## Sessions

- `<id>.jsonl` is session `<id>`. `<id>/subagents/*.jsonl` belongs to session
  `<id>`: subagents do not inflate session counts.
- Lines with a `uuid` already seen in another file are skipped (resumed
  sessions replay earlier lines).
- **What is read from a Claude Code transcript:** `user` and `assistant`
  lines, and one kind of `attachment` line: a `queued_command`, which is how
  a message typed *while the agent is working* is recorded when it reaches
  the agent (`{"type": "attachment", "attachment": {"type":
  "queued_command", "prompt": ...}}`, the prompt a string or a list of
  content blocks). It is read as a human message at that point of the turn,
  so "No, revert that" typed mid-turn is a correction like any other, and
  taking the change back is following it, not an `edit-revert`. The same
  record carries what the harness and other agents queue. Those are not the
  human: an `origin.kind` other than `user`/`human` (`task-notification`,
  `coordinator`, `peer`, ...), a `commandMode` other than `prompt`, `isMeta`,
  or text that is a system block (`<system-reminder>`, `<task-notification>`,
  ...). A queued `<task-notification>` is still read for how a background
  command ended. Where the same text is recorded both as a queued message
  and as a `user` line within 10 minutes (`REPLAY_SECONDS`), it was said
  once. Every other line type is counted in `ignored_lines`.
- A session with no activity in the repo (no working directory inside it, no
  path that resolves into it, and no command that `cd`s into it) is left out and reported as
  `sessions_outside_repo`.
- For `human-rework`, an agent commit made during an analysed transcript
  session (first event to 10 minutes after the last) belongs to that session.
  Any other agent commit with rework acts as a session of its own
  (`git:<sha>`): it counts towards a hotspot's `sessions` and
  `exposure_sessions` and is reported as `agent_commits`, but not towards
  `totals.sessions` and not towards confidence.

### Episodes

Recurrence is the heart of the score, and a session id is a poor unit for
it: `claude --continue` keeps one id alive for days. Ten tasks on ten days
in one session id are ten independent occasions on which the same friction
happened, not one bad afternoon. So recurrence is counted over **episodes of
work**:

- Within a session id, a new episode starts at a **human prompt that follows
  2 hours or more without any event** in that session
  (`EPISODE_IDLE_SECONDS`). An overnight break, a lunch break of two hours,
  a session picked up the next week: each starts a new episode. Calendar
  days are not used: timestamps are UTC, and midnight UTC falls in the
  middle of somebody's working day.
- Only a human prompt can start one. An agent that works on its own for
  five hours, however long its commands take, is in one episode.
- **A headless session** has no prompt to go by: one that nobody typed into
  after its opening prompt (a scheduled run resumed the next day, a
  container that was restarted). There the agent's next message or tool
  call after 2 hours or more of silence starts a new episode. A tool result
  never does: a result that arrives three hours after its call is the
  command finishing.
- **Subagents never start an episode** and are never episodes of their own.
  Their prompts are written by the agent that spawned them, and seventeen
  subagents hitting the same wall in one afternoon are one coordinator's one
  attempt, not seventeen independent observations. Their events belong to
  whichever episode of the parent session they fall in.
- A session that was never left idle is one episode, so for most sessions
  nothing changes.

Episodes are what the score's *recurrence* and *consistency*, the
confidence levels and every "in N or more sessions" threshold (a setup step
in 2, a check that stays red in 3, a flaky test in 3, a stopped command in
3, a search pattern in 2) count. Wherever this document says "sessions" for
those, read "episodes". What is **displayed** stays the session:
`sessions`, `exposure_sessions`, `friction_rate` and the wording of
recommendations count session ids, and `episodes` / `exposure_episodes`
stand beside them in the JSON (`totals.episodes` for the whole scan). When
they differ, the hotspot's `why` says so ("in the only session that touched
it (in 10 separate episodes of work)").

## Attribution

**Paths.** Absolute paths are matched lexically against the repo root, its
symlink-resolved form, its git worktrees and any `repo_aliases`; below a
root, a leading `.claude/worktrees/<name>/` or `.worktrees/<name>/` is
dropped (a nested worktree is the repository, see Discovery). A session
started *through* a symbolic link to the repository (`~/proj` for
`/data/proj`) recorded the link's spelling: where a session's working
directory exists on this machine and its real path is the repository's or
lies inside it, that spelling becomes one more root (`PathResolver.adopt`),
and Claude Code project directories filed under it are discovered when the
real path is scanned. Nothing else is resolved through the file system. Relative
paths are resolved against the session's working directory. Paths outside the
repo and inside `.git` are dropped. Symlinks in the target path are not
followed, and files need not exist any more.

**Commands.** A command line is reduced to a short key so that variants group
together, and so that the key can be shown even when excerpts are off:

1. **Split into simple commands.** Heredoc bodies are dropped (they are data).
   The line is split at `;`, newlines, `&&`, `||`, `|` and `&`; subshell
   parentheses and shell keywords (`if`, `then`, `!`, `for ... do`, `while`,
   `done`, `fi`, ...) are looked through, and `bash -c '<script>'` is replaced
   by the commands of its script. Redirections, leading `VAR=value`
   assignments and wrappers (`sudo`, `env`, `time`, `timeout N`, `nohup`,
   `xargs`, `nice`, `watch`, ...; also by absolute path, `/usr/bin/time -v`;
   when the wrapper itself is what the shell cannot find, the row is still
   the command behind it, with "`time` is not installed where agents run")
   are stripped from each command, together
   with the wrapper's own options and their values (`WRAPPER_VALUE_OPTIONS`:
   `sudo -u alice pytest` -> `pytest`, `xargs -n 1 pytest` -> `pytest`).
   Programs that start another command with something added to its
   environment are looked through in the same way (`ENV_LAUNCHERS`): `dotenv
   run -- pytest`, `dotenv -e .env -- npm test`, `doppler run -- npm test`,
   `op run --env-file=.env -- make test`, `direnv exec . pytest`, `xvfb-run -a
   npm test`, `script -q /dev/null pytest` and `script -qc 'pytest -q'
   /dev/null` are about the command they start. Doing their own business
   (`op item get`, `doppler secrets`, `direnv allow`) they keep their name.
   A key never comes from data: an armored block passed as an argument
   (`-----BEGIN CERTIFICATE----- ... -----END CERTIFICATE-----`) is one
   opaque word however many lines it has, a word that begins with `-` or `=`
   is never a command, and where a quote opens and never closes only the
   first line is read (the rest is most likely the argument's other lines).
   One thing is remembered about the redirections: a command whose standard
   output and standard error both go to `/dev/null` is *silenced*, and
   nothing in the line's output is taken for its verdict.
2. **Pick the command that names the line:** the **last significant** one.
   Setup (`cd`, `export`, `source`, `echo`, ...), housekeeping (`mkdir`,
   `touch`, `rm`, `cp`, `mv`, ...) and read-only inspection (`cat`, `tail`,
   `grep`, ...) are passed over, so `mkdir -p out && pytest tests/x.py | tail
   -5` is `pytest` and `echo hi | python -m json.tool` is `python -m
   json.tool`. If nothing significant is left, the line is named after what
   was inspected (`grep -rn x src | head` is `grep`).
3. **Name the executable.** Executables under `bin/`, `.venv/`,
   `node_modules/` or given by absolute path become their basename;
   `python3`, `python3.12` become `python`; `./scripts/build.sh` becomes
   `scripts/build.sh`. That is the key red and green runs are matched by.
   The *name shown* in the result keeps `python3` where that is what was
   typed (`python3 -m pytest`, `python3 scripts/seed.py`; not for `-c` and
   stdin snippets): on a machine without `python`, "run `python -m pytest`"
   would be the wrong thing to write down, so the two are separate rows.
4. **Keep arguments only where they are known to be names, not data:**
   - a script run through a shell is the script: `bash scripts/run.sh`, `sh
     -x ./scripts/run.sh` and `scripts/run.sh` are all `scripts/run.sh`;
   - interpreters keep `-m module` or the script path (`python -m pytest`,
     `python scripts/gen.py`), plus the subcommand of a script that is itself
     a dispatcher (`python manage.py migrate`, `php artisan migrate`);
   - inline code has no name: `python -c '...'`, `node -e '...'`, `ruby -e`,
     `perl -e`, `bun -e` are `<program> <inline>`, and a script on standard
     input (`python - <<EOF`) is `<program> <stdin>`. These keys never get a
     "document the command" recommendation;
   - tools on a fixed allowlist of multi-command tools (`SUBCOMMAND_TOOLS`:
     `git`, `npm`, `pnpm`, `yarn`, `uv`, `cargo`, `go`, `make`, `docker`,
     `kubectl`, `gh`, `terraform`, `aws`, ...) keep **one** lower-case
     subcommand word. Options in front of it are skipped, together with their
     value where the tool defines one (`make -j4 test` -> `make test`, `git -C
     /repo commit` -> `git commit`, `pnpm --filter web test` -> `pnpm test`,
     `kubectl -n prod get pods` -> `kubectl get`, `uv run --group dev pytest`
     -> `uv run pytest`);
   - a **launcher** runs another command inside an environment, and its key
     is the launcher followed by the key of what it runs, by these same
     rules (`LAUNCHERS`): `uv run`, `poetry run`, `pipenv run`, `hatch run`,
     `pdm run`, `rye run`, `conda run` (and `mamba`, `micromamba`), `bundle
     exec`, `npx`, `npm exec`, `pnpm exec` / `dlx`, `yarn exec` / `dlx`,
     `bunx`, `bun x`, `uvx`, and `bun run <file>`. The launcher's own
     options are skipped with their values (`uv run --with rich --python
     3.12`, `conda run -n dev`, `npx -p typescript`). So `uv run python
     scripts/seed.py` and `uv run flask db upgrade` are `uv run python
     scripts/seed.py` and `uv run flask db`, not one `uv run python` /
     `uv run flask` for everything; `bundle exec rails test` is `bundle exec
     rails test`, `npx tsx scripts/a.ts` is `npx tsx scripts/a.ts`, `uv run
     pytest -q` stays `uv run pytest`. A launched command has the role of
     what it runs, and it can stand in for the same command run bare
     (`is_replacement`);
   - one further word is kept only after a script runner or command group of
     that tool (`CONTINUING_SUBCOMMANDS`: `npm run build`, `bun run build`,
     `docker compose up`, `gh pr create`). A workspace selector that reads
     as a directory is where the command runs, not part of its name: `pnpm
     --filter web test`, `pnpm --filter=web test`, `pnpm -F web test` and
     `yarn workspace web test` are `pnpm test` / `yarn test` run in that
     package. Where the package lives is asked of the repository: `web/` if
     that directory exists (`pnpm test (web/)`, exactly as `npm --prefix web
     test` is `npm test (web/)`); otherwise the one directory called `web`
     up to three levels down, past hidden, third-party and build directories
     (`pnpm test (packages/web/)`); and with none or several candidates no
     directory at all (`pnpm test`): a package name is not a path. A scoped package name (`@acme/web`), a pattern (`web...`,
     `!web`) or a git range (`[main]`) does not say where: `--filter` is then
     dropped like any option, and `yarn workspace @acme/web <script>` keeps
     both words (the workspace name is project-specific and becomes a
     placeholder without excerpts). `pip3` and `pip3.12` are `pip`, as
     `python3` is `python`, and what Windows appends to a program's name is
     dropped (`npm.cmd test` -> `npm test`, `python.exe -m pytest` -> `python -m pytest`);
   - **every other program is its executable name alone**: `./run s3cret` is
     `run`, `mytool acme-codename --x` is `mytool`, `ssh host cmd` is `ssh`,
     `docker run <image>` is `docker run`;
   - make-style targets and package scripts are names the project chose, so
     they are kept in any case: `make deploy-ZebraCustomer`, `just Deploy`,
     `npm run ZebraCustomer:build` (`TARGET_TOOLS`; without excerpts they
     become placeholders like every project-specific word);
   - **a command run inside a container is keyed as the command itself:**
     `docker compose exec web pytest -q`, `docker compose run --rm api
     pytest`, `docker exec -it shop_web_1 pytest` and `kubectl exec deploy/web
     -- pytest` are all `pytest`. Where a check runs does not change what it
     is, and it is the same job as a bare `pytest` for red-fix-green and
     replacement purposes. Without an inner command (`docker compose exec
     web`) the key stays `docker compose exec`. The *name shown* keeps the
     container call, so that a run in a container and a run on the host are
     separate rows: `docker compose exec web pytest` (the compose service is
     named: it is part of the project; without excerpts it is a
     placeholder), `docker compose run api pytest`, `docker exec pytest` and
     `kubectl exec pytest` (a container or pod name is often an id of the
     moment, and is left out).
   - **a single word or quoted string of more than 64 KB is data**, not a
     command name or a path (`MAX_WORD_CHARS`): it is replaced by `LONG-DATA`
     before the line is split, also when its quote never closes. Splitting
     such a word is quadratic (400 KB took ten seconds).

5. **Sanitize.** A key is cut at the first word that is a number or that the
   redactor would change (`make 2` is `make`; a key that would start with
   such a word is discarded). Every key passes through the redactor.

   A shell variable is followed only if the same line assigns it a plain
   value on its own (`A=scripts/x.sh; $A` is `scripts/x.sh`; `SP=scripts &&
   node $SP/shot.js` is `node scripts/shot.js`). Nothing is evaluated: a
   prefix assignment (`A=x make $A`), a computed value (`A=$(pwd)`) or a
   variable from the environment stays unknown and is written `<variable>`
   in the key (`$RUNNER test` is `<variable>`, `node $SP/shot.js` is `node
   <variable>/shot.js`), so that such lines share one row instead of one
   per variable name.

6. **Say where, for tools that work on the directory they are in.** For
   build, test, lint and package tools (`DIRECTORY_TOOLS`: `npm`, `pnpm`,
   `yarn`, `make`, `cargo`, `go`, `uv`, `pytest`, `tox`, ..., and any key
   with a role), the key shown is followed by the repo-relative directory the
   step ran in when that is not the repo root: `cd web && npm test`, and `npm
   test` in a session started in `web/`, are both `npm test (web/)`. `git
   push`, `curl` or `docker ps` are the same thing wherever they run and get
   no suffix. The directory is a repo path, so it stays readable without
   excerpts. In a repo where agents `cd tests && pytest` in some sessions and
   run `pytest tests` from the root in others, this splits one command into
   two rows; that is the price of not merging the packages of a monorepo.

A key therefore consists of an executable name plus, at most, a subcommand of
an allowlisted tool, a `make`/`npm run`-style target or script name, or an
interpreter's script path or module. Free-form arguments, option values, host
names, URLs and environment assignments are never part of a key.

**Keys without excerpts.** Executable, script, target and module names are
still chosen by the project (`python deploy_acme_customer.py`, `make
deploy-customer-acme`). With `--no-excerpts` every word of a key
that is not in a fixed public vocabulary (well-known programs, their common
subcommands, and generic target names such as `test`, `build`, `lint`;
`PUBLIC_VOCABULARY` in `attribution.py`) is replaced by a placeholder with
the first 6 hex digits of its SHA-256: `python <script:93d162>`, `make
<target:37bd73>`, `<tool:373279>`. The same name always maps to the same
placeholder, so hotspots still group and two reports can still be compared;
`pytest`, `npm test`, `git commit` stay readable, and so do two kinds of
word the report shows anyway: a path of a file in the repository
(`scripts/deploy.sh`) and the repository's own name (a project called
`shopfront` whose CLI is `shopfront`: the name is in every report's title;
`-`, `_` and `.` are not told apart). With excerpts on, keys are shown as
they are.

**Roles.** For recommendations a key has a role if one of its words says what
job it does (`ROLE_WORDS`): test (`pytest`, `make test`, `npm run
test:unit`), build, lint, type-check, format. Install and environment
commands (`pip install`, `npm ci`, `uv sync`, `docker compose up`, ...) are
*setup commands*. A command can stand in for another only if it has the same
role or wraps the same tool.

**Same job means same ecosystem, same place.** `pytest` and `npm test` are
both test runners, and neither says anything about the other's code. A role
match therefore also needs the same *tool family* (`TOOL_FAMILIES`,
`tool_family`): python (`pytest`, `tox`, `nox`, `python -m ...`, `uv run
...`, `poetry run ...`, `ruff`, `mypy`, `black`, ...), js (`npm`, `pnpm`,
`yarn`, `bun`, `npx ...`, `jest`, `vitest`, `tsc`, `eslint`, `prettier`,
...), rust (`cargo ...`), go, ruby, php, jvm, dotnet, c, shell. Make-style
wrappers (`make test`, `just lint`, `task build`, `rake`, `pre-commit run`)
and the repository's own scripts (`./scripts/test.sh`) belong to no family
and fit any: `make lint` can stand in for `ruff check` and for `npx eslint`;
`npx eslint` never stands in for `ruff check`, nor `npx tsc` for `mypy`,
`prettier` for `black`, `npm run build` for `cargo build`.

Directories count too. A green run of *another* command is about a red run
only if it ran in the same directory of the repository; a wrapper or an
aggregate check also when it ran in a directory above (`make test` at the
root after `cd backend && pytest`). The *same* command pairs with itself in
the same directory, above it, or below it (red at the root, green in
`web/`: "run it in `web/`"), never side by side: `cd frontend && make
build` going green says nothing about `cd backend && make build`. One
failure is not about a place at all: a program that was not found. `cd
src; pytest` answered with "not recognized" and `python -m pytest` passing
from the root is the same tool started another way, and the advice names
it ("Tell agents to use `python -m pytest`"); a tool of another family
passing elsewhere (`cd web && npm test`) is still not a replacement.

The same test decides three things: whether a command "worked instead"
(`use-the-working-command`), whether a red run that goes green under
another runner with nothing changed is the wrong runner (`green_as`), and
whether red, edit, green is the development loop and dropped. So edit,
`pytest` red, edit, `npm test` green leaves the `pytest` run on the record:
it never went green. The same holds for the rule that a red type-check or
lint run is closed by any green check after an edit: only a check of the
same family, in the same place.

More examples: `CI=1 npm test -- foo` -> `npm test`;
`cat <<EOF > x.py ... EOF` + `python x.py` -> `python x.py`;
`if ! make lint; then exit 1; fi` -> `make lint`;
`for t in a b; do pytest $t || exit 1; done` -> `pytest`;
`touch x && uv pip install -e . && python -m pytest | tail -5` -> `python -m pytest`.

Files are attributed from a command line only if they are arguments of the
command that names the line. When a human declines a chain of commands, the
event is keyed by the step they most plausibly objected to (see
[`tool-rejected`](signals.md)).

### Third-party and build directories

Friction inside other people's code or build output is not something the
repository's instructions can fix, and it must not outrank first-party
files. Files and directories are set aside when

- a component of their path is one of `node_modules`, `bower_components`,
  `site-packages`, `dist-packages`, `vendor`, `.venv`, `venv`, `.tox`,
  `.nox`, `__pycache__`, `.git`, `.mypy_cache`, `.pytest_cache`,
  `.ruff_cache`, `.gradle`, `.next`, `.nuxt`, `.eggs` or ends in
  `.egg-info` (`THIRD_PARTY_DIRS`), wherever it occurs;
- or they lie in a directory that `.gitignore` rules match (one `git
  check-ignore` call per scan, only when git is in use and the repository
  is a git repository). An ignored *file* in a tracked directory (`.env`)
  is still the repository's own;
- or, when git cannot be asked (`--no-git`, no repository), they lie in a
  directory named `dist`, `build` or `target` (`BUILD_DIRS`). With git, those
  three names are judged by `.gitignore` alone, so a tracked `src/build/`
  package stays first-party.

Such paths are in no hotspot, directory row, tree node or exposure count,
and get no recommendation. An event that was only about such paths is
dropped and counted in `totals.third_party_events`; an event that also has
a first-party file or a command keeps those. Commands are never set aside:
`node_modules/.bin/jest` failing is a failure of `jest`.

### Calls that acted outside the repository

A session often works on more than this repository: `cd ../other && make`,
a script under `/tmp`, a file in the home directory. A call that was
declined, denied or failed while acting outside (the step ran in a directory
outside, a housekeeping command only named paths outside, a script that lives
outside, a file tool pointed at a path outside) says nothing about this
repository and produces no event. It is counted, so that the headline can
be checked against the transcript: `totals.events_outside_repo` is the
number of such calls (each call once; read-only inspection commands,
interrupted calls and calls the harness never started are not in it).
When the scanned directory is one package of a larger git repository,
`totals.events_elsewhere_in_repo` says how many of them acted inside that
same repository (a sibling package, the monorepo's root): every place the
call went to that is not the scanned directory lies in the enclosing
repository or one of its worktrees. It is a subset of `events_outside_repo`
and 0 when the scanned directory is the repository's root or git is not
available. On a
machine where most permission denials are for commands run elsewhere, this
is where they went.

### Tools that are not files or commands

A failing MCP tool, web fetch or sub-agent call belongs to no file, command
or directory. From the second failure of the same tool in the scan these are
`tool-failure` events: weak, unattributed, claiming no time, counted in
`totals.tool_failures` and listed per tool in the top-level `tool_failures`
(see the [catalog](signals.md)).

### One command, several directories

A check command is keyed by where it runs (`pytest`, `pytest (src/)`,
`pytest (tests/)`), because where it runs is part of how it is run, and each
key is scored and advised on its own. So that one habit spread over three
directories is not read as three small problems, every command row whose
command also appears under another directory carries `family`: the bare
command, the rows that belong together, and the distinct events and sessions
of all of them. Nothing is merged, and no score or recommendation is
computed for the family.

## Exposure

Friction only means something relative to how often the agent goes somewhere.
For every target the engine records its **exposure**: the set of sessions in
which the agent touched it at all, with or without friction.

- **file:** a read, edit or write of the file, a search scoped to it, or a
  failed command or correction attributed to it;
- **command:** the command ran (read-only inspection such as `ls`, `cat`,
  `git status` is not tracked);
- **directory:** a search scoped to the directory. Tree nodes use every
  session that touched anything beneath the directory.

Agent commits from git never add exposure by themselves. Only an agent commit
with rework that falls outside every analysed session counts, as one friction
"session" on the file it reworked.

`exposure_sessions` is the size of that set and
`friction_rate = sessions with friction / exposure_sessions`. The report's
top-level `exposure` object lists the count for every target touched in the
window, including the ones with no friction, so that two reports can be
compared fairly (a hotspot that vanished because nobody went there is not a
hotspot that got fixed).

## Score (0-100)

For one hotspot (file, directory or command):

```
event value      = severity(signal) x weight x share
session points   = log2(1 + sum of event values in that session)
mean points      = mean of session points over the sessions with friction

severity    = 1 - exp(-mean points / 1.5)                      how bad, when it happens
recurrence  = min(1, log2(1 + friction sessions) / 3)          how often it has happened
consistency = 0.4 + 0.6 x friction sessions / (exposure sessions + 1)
                                                               how reliably it happens
score       = 100 x severity x recurrence x consistency
```

- **severity(signal)** is the per-signal constant from the [catalog](signals.md).
- **weight** is 1 except that an ordinary `command-failure` uses 0.3 (one
  that passed only after a setup step counts 1, like setup/invocation and
  environment failures).
- **Weak evidence is capped.** If every event on a target is of a weak kind
  (`edit-thrash`, `read-loop`, `search-miss`, `tool-failure`, `probable-correction`, or an
  *ordinary* `command-failure`; see the [catalog](signals.md); also a `path-miss` on a routinely probed optional file, and `no-convergence` events that all come from one session), the score is capped at **19**, below the band where
  a single real incident lands, `score_detail.capped` is true, and no
  recommendation is made. Such a target can be a lead; it cannot be an alarm
  or fail a build on its own. This is what keeps `git stash pop` with nothing
  stashed, a red `mypy` run or ten unrelated `python -c` snippets from adding
  up to "a command that keeps failing": it takes a setup/invocation error,
  an environment failure, a failure that a setup step fixed, a retry loop or
  a human reaction to lift a command out of the cap. One more thing does: a
  command that failed 2 or more times in a session and was still red when
  the session ended, in 3 or more sessions (`RECURRING_RED_MIN_SESSIONS`).
  Those failures are no longer weak, and each claims its own run time.
  Likewise a `search-miss` for the same pattern in 2+ episodes by agents
  that wanted a hit is lifted out of the cap (see "What lifts it" under
  `search-miss` in the [catalog](signals.md)); `edit-thrash` and `read-loop`
  on their own stay capped however often they recur.
- **Ordinary failures next to real evidence.** When a target has both, an
  ordinary `command-failure` counts in full only in an episode that has
  other evidence on that target too. Episodes that contain nothing but
  ordinary failures cannot lift the score past the same **19**: the score is
  the smaller of the plain formula and `max(19, the formula without those
  episodes' events)`, and `score_detail.ordinary_limited` is true when that
  made a difference (the factors in `score_detail` are then those of the
  plain formula, and `score` is lower than their product). One invocation
  error plus eight red test runs in other sessions is one invocation error,
  not a command that fails everywhere. The `why` sentence of such a row
  says how many of its events are ordinary failures.
- **share** is 1 except when one correction is split over several files.
- **log2 per session** gives diminishing returns: a session contributes one
  more point each time its friction doubles, so one pathological session
  cannot dominate.
- **severity** depends on the *mean* over sessions, not the sum, so it does
  not grow with the amount of transcript history. One edit miss per session
  (1.58 points) gives 0.65; a correction plus two edit misses (3.17 points)
  gives 0.88.
- **recurrence** is logarithmic and reaches 1 at 7 friction sessions: 1
  session = 0.33, 2 = 0.53, 3 = 0.67, 5 = 0.86. A single session can never
  score above 33 x consistency, however bad it was.
- **consistency** is where exposure enters. It runs from 0.4 (friction in a
  vanishing fraction of the sessions that went there) to just under 1
  (every time). The `+ 1` is one imaginary clean session, so that "1 of 1" is
  not treated as "always". The floor of 0.4 keeps a file that hurts in 10
  sessions out of 200 on the list, but below one that hurts in 5 out of 5:

  | Friction (one edit miss each) | Score |
  | --- | --- |
  | 5 of 5 sessions | 50.6 |
  | 10 of 200 sessions | 28.0 |
  | 1 of 1 session | 15.2 |
  | 1 of 20 sessions | 9.3 |

- The score is absolute, not relative to other hotspots, and it does not
  saturate: with thousands of sessions a place where every session has one
  read loop scores about 32, and it approaches 100 only as *every* visit goes
  *badly*. It is comparable across runs and across repositories of different
  size.

**Git can only add.** `human-rework` events take part in the formula like any
other event, but the score of a hotspot is never lower than the score computed
from its transcript events alone: adding a mildly-weighted rework "session"
would otherwise pull the mean down.

The output includes `exposure_sessions`, `friction_rate` and `score_detail`
(the three factors, the mean and the per-session values) so the arithmetic can
be checked: `score = 100 x severity x recurrence x consistency` up to
rounding. Directory nodes in the tree are scored with the same formula over
all events beneath them, each event counted once, against all sessions that
touched anything beneath them.

Rough reading: below 20 is a single incident or a rare one; 20-45 is a
pattern worth a line in AGENTS.md; above 45 is a place where agents reliably
struggle. Read the score together with `friction_rate` and the confidence.

### The repository's base rate

An edit that does not find its text, or a write refused because the file was
not read or had changed, happens at some rate wherever agents edit files.
Sixty healthy sessions over fifteen files with a 6% chance of a miss before
each edit put two or three misses on most files; that is one fact about the
repository, not a pattern in each file. So for the two mechanical signals
that have a meaningful denominator, `edit-miss` and `stale-edit`
(`scoring.BASELINE_SIGNALS`), a file is singled out only if it shows clearly
more than the rest (`scoring.baseline`):

- A **visit** is one session (episode) touching one file. For each file with
  such events, the null hypothesis is the rate of the *other* files: visits
  with the signal / visits, and events / visits. Neither is taken below
  **5%** (`BASELINE_FLOOR`), and the floor alone is used when the other files
  add up to fewer than 20 visits (`BASELINE_MIN_VISITS`): a small or quiet
  repository is judged against one visit in twenty. Advice for a file that
  was singled out this way says that there was nothing to compare it with
  (`Baseline.sparse`, `no_comparison` on its events), not that it misses
  "more often than the rest of this repository".
- Two tails are computed: binomial, for this many visits with the signal
  among the file's visits, and Poisson, for this many events (forty misses
  in one session are one visit, and not background). The smaller, doubled, is
  the file's p-value.
- Files are selected by the Benjamini-Hochberg procedure at 0.05
  (`BASELINE_ALPHA`), so that testing many files does not by itself produce
  outliers.

Examples against the floor: 2 visits with a miss out of 4, or 4 of 5, are
above; 3 of 30 is not; one miss in a file's only visit is not; three misses
in that one visit are.

Events on a file that is *not* above the rate are marked `within_baseline`
and are weak evidence from then on: they still count in the totals and in
the score, but a file with nothing else is capped at 19, is "low"
confidence and gets no advice, and its misses feed no recommendation.
Generated files are exempt (one failed edit of generated output is already
the pattern). When a signal has 5+ events in 3+ files and 3+ sessions, over
at least 20 file visits (`REPO_WIDE_MIN_VISITS`; a share of fewer visits is a
handful of events, not a rate), the
result's top-level `recommendations` carries one repository-level item,
`note-the-background-rate`: "Edit misses run at 8% of file visits across the
repository", the counts, and the files that are above it.

**Not covered:** `path-miss` and `search-miss` have no such denominator (the
target of a miss is the thing that is not there, so every visit to it is a
miss); they keep their own rules (probes and search misses are weak, advice
needs the same path or pattern in 2+ sessions). **Limits:** the test assumes
visits are alike; a file that is only ever read dilutes nothing but its own
rate, while a file agents edit ten times per visit has more chances to miss
than one edited once, and may be called above for that reason alone. With
few visits nothing can be told apart from 5%, so a real but mild problem in
a rarely touched file stays below the line until it recurs.

## Estimated time lost

Each event may claim a wall-clock interval from transcript timestamps.
Timestamps that cannot be right are treated as missing: anything before the
year 2000 (a zeroed clock) or more than a year ahead of today (a unit
mix-up). Epoch numbers are read as seconds, milliseconds, microseconds or
nanoseconds according to their size.

| Event | Interval |
| --- | --- |
| `edit-miss`, `stale-edit` | one round trip; extended to the edit of the same file that lands only if every tool call in between failed too |
| `command-failure` (setup/invocation) | one round trip; extended to the end of the command that recovered it (the same command succeeding, or a replacement doing the same job, within the next 5 shell calls of the turn, read-only inspection such as `ls`, `which` or `cat` not counted) only if every tool call in between failed too |
| `command-failure` (ordinary) | none |
| `path-miss`, `file-too-large` | one round trip (a `path-miss` that is a routine probe for an optional file: none) |
| `permission-denied` | the refusal itself: from the call to its result. A refusal that comes back in 0.2 s costs 0.2 s; what the agent thought about before its next call is its next step, not time the refusal took |
| any interval that starts at a call | starts later where a person had to approve the call: see "A person's time at a permission prompt" below |
| `tool-rejected` | from the human's answer to the next event in the transcript; the time the permission prompt was open (call to result) is a human's time and is never counted |
| `search-miss`, `probable-correction`, `tool-failure` | none: weak signals never claim time |
| `edit-revert` | from the original edit to the revert if both are in the same turn; otherwise one round trip |
| `user-correction` | the rework only: from the agent's first tool call after the correction to its last change to the files the correction was about, in that turn; one round trip (that first call) if it does not return to those files; nothing if the session ends there. A `probable-correction` claims nothing |
| `user-interrupt` | the call that was cut short: from its start to the interrupt |
| `no-convergence` | the red runs of the stretch (call to result) and the edits between them, at most 15 minutes, and only when the command or file has such events in 2+ sessions; otherwise none |
| `retry-loop`, `edit-thrash`, `read-loop`, `human-rework` | none (no defensible estimate) |

**One round trip** runs from the call to the agent's *next activity* (its next
tool call or its next message). If the next thing in the transcript is a
human message, the agent's turn was over: only the call itself (call to
result) is claimed, never the time until the human replied. A failure that
the agent reports and then waits an hour on costs seconds, not an hour.

**An idle gap is not a round trip.** Where the agent's next activity comes
more than 5 minutes after the call's result (`IDLE_GAP_SECONDS`), nobody was
working in between: a closed laptop, a session resumed the next morning. Only
the call itself is claimed then. This matters most for transcripts without
agent messages (the generic format may hold tool calls only), where the next
activity is the next tool call, however much later it came. The same holds
in the middle of a turn and for every other interval (a failure until the
command that resolved it, an edit until its revert, the rework after a
correction): no interval reaches across a silence of more than 5 minutes
between two transcript events. What came before the silence is claimed, or,
if the interval starts at it, what came after. The time between a call and
its own result is the command running and is never a silence.

**As soon as anything succeeds, the clock stops.** A failed edit followed by
eight green test cycles on another file and, much later, an unrelated edit of
the first file cost one round trip, not the minutes in between: that time was
productive. Only an unbroken run of failures (miss, miss, miss, then the edit
lands) is counted as one stretch.

Every interval is capped at **5 minutes** (`no-convergence`: 15). `est_minutes_lost` for a hotspot,
a tree node or the whole report is the length of the **union** of its
intervals (merged per transcript stream), so overlapping events are never
counted twice.

**A person's time at a permission prompt is never the agent's.** Between a
call and its result lies the tool's own run time and, where somebody had to
approve the call first, the time the prompt stood open. Only the first is
claimed, wherever an interval starts at a call (a round trip, a red run):

- Claude Code records how each call was let through (`permissionDecision` on
  the result line). `"source": "config"` is the permission settings, the mode
  or a hook deciding at once: call to result is the tool's time and counts.
  Any other source is a person answering: nothing before the result is
  claimed, only result to next activity.
- Where the transcript does not say (older Claude Code versions, generic
  logs without `prompted`), call to result counts **up to the larger of 30
  seconds (`UNATTENDED_RUN_SECONDS`) and the median call-to-result time of
  the same command's (or tool's) other runs in the session**. A `terraform
  plan` that failed 270 s after it was called, in a session where it
  otherwise takes 20 s, claims 30 s of them plus the time to the next
  activity.

**Weak evidence claims no time.** Search misses, probable corrections,
ordinary command failures, probes for optional files, and a `no-convergence`
stretch seen in a single session are leads. They can be listed and scored
(under the cap), but they add nothing to `est_minutes_lost`, in a row or in
the totals: a capped row always shows 0.

This is an estimate of agent wall-clock time that demonstrably went into
failed calls or into redoing work. It is not a measure of human time, and it
is a **floor** throughout: a failure costs one round trip unless an unbroken
recovery is visible, and a correction costs the follow-up it caused, never
the turn it corrects (twenty clean edits and a green test run followed by
"not quite, rename the helper" cost the rename, not the twenty edits). The
human's own time, the agent's reading and thinking before a failed call, and
everything that went wrong without leaving one of these traces are not in it.
Because intervals are not divided by share, minutes of different
hotspots can overlap; only `totals.est_minutes_lost` is free of double
counting. Events without timestamps claim nothing.

Every evidence item carries `est_seconds_lost`, the length of its own
interval (0 if it claims none), so a hotspot's minutes can be checked by
hand: they are the union of its events' intervals, which is the sum of those
seconds unless intervals overlap (and more than the listed items add up to
when `max_evidence` hides some events).

## Confidence

| Level | Rule |
| --- | --- |
| `high` | 3 or more distinct sessions **and** 2 or more distinct signal types. The weak kinds (`read-loop`, `edit-thrash`, `search-miss`, `probable-correction`, `tool-failure`) are never one of the two: they do not corroborate anything. A hotspot whose only evidence is weak (those, or ordinary command failures) is "low" |
| `medium` | 2 or more distinct sessions |
| `low` | a single session, **or** a target whose score is capped because all its evidence is weak (see Score), in however many sessions |

Ordinary command failures never count towards confidence: on a target that
also has other evidence, the sessions and signal types counted are those of
the other evidence. A real error in one session with red test runs in three
more is "low".

A signal that is computed from other events is not a second kind of evidence
next to them: `retry-loop` and `no-convergence` are derived from
`command-failure`, so a command with them has one signal type (`DERIVED_SIGNALS`).
For the same reason one stall is one event: a file edited round after round
while a check stays red gets a `no-convergence` event and no `edit-thrash`
event for that turn. A command that fails with
the same setup error in every session is therefore "medium" until something
independent (a correction, a declined call) is also seen.

Only transcript evidence counts here. Agent commits are not sessions and
`human-rework` is not a signal type for this purpose: git history can
corroborate a hotspot (it adds to the score), but it can never lift one to
"medium" or "high" on its own.

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
the evidence that triggered them. A target whose only evidence is weak (see
[Score](#score-0-100)) gets no recommendation at all, whatever the counts.

**Thin evidence.** Every recommendation carries `events`, the number of events
it rests on (for most rules, the target's events of the signals it is based
on; for the permission rules, the refusals it counts). With fewer than 5
(`THIN_EVIDENCE_EVENTS`) it is marked `"evidence": "thin"`: it is still listed
under its target, after the target's other advice, with "(thin evidence: 3
events)", but it is left out of "What to fix first" while there is any other
advice (with none other, the list shows it and says each item is a lead, not a
reason to change the repository), and `scan -f prompt` never lists it as
something to act on (it goes under "Not enough evidence yet"). Three interruptions of `ls` over hundreds of
runs are not a reason to change AGENTS.md. Never thin: advice grounded in a
fact about the repository or in a human's own words, whatever the count
(`edit-the-source-not-the-output`, `document-what-rewrites-this-file`,
`write-down-the-rule`, `name-the-missing-target`, `file-is-gone`,
`note-the-background-rate`, and `state-the-boundary` when the human gave a
reason).

| Id | Fires when |
| --- | --- |
| `edit-the-source-not-the-output` | the file is generated (see "Which files count as generated" below) and the transcripts show trouble editing it: 1+ `edit-miss`, `stale-edit`, `edit-revert` or corroborated `user-correction`. With a generated-file header the advice is plain ("Generated file: point agents at its source"); when only the name or the directory says so it is hedged ("Possibly a generated file: if so, ...", "judging only by its name; it carries no generated-file header, so check"). Git history alone never fires it: a lockfile bump is not an agent editing output by hand |
| `split-or-anchor-large-file` | 2+ `edit-miss` and the file has 800+ lines, or 2+ `file-too-large` reads (the file's line count is cited only if it is 800+ today) |
| `make-edits-land` | 2+ `edit-miss` (above the repository's base rate, see Score) on a file that is neither large nor generated. Mostly ambiguous: "Repeated blocks: make edit targets unique". Mostly not-found: "Agents misremember this file: check what rewrites it" **only with evidence of a rewrite** (a `stale-edit` on the file, or a miss before which a command that rewrites files, such as a formatter, a fixer, `sed -i` or a commit hook, ran since the agent last read or edited the file; `rewritten` on the event); otherwise the neutral "Edits to this file keep missing their target", which asserts no cause |
| `document-what-rewrites-this-file` | 2+ `stale-edit` of the "modified since read" kind |
| `path-agents-expect` | 2+ `path-miss` (a read, an edit, a test command pointed at a missing test file, or a `cd` into a missing directory) |
| `write-down-the-rule` | corroborated `user-correction` events whose attributed shares sum to 0.5 or more; quotes up to 3. "The rule the human keeps repeating" is claimed only when 2+ of the corrections share wording (two content words, or one distinctive word of 7+ letters, e.g. the same class name); otherwise the recommendation only asks whether the correction(s) reflect a standing rule. A `probable-correction` never counts. When an instruction file that applies here has a line with a name the human used in the correction (a class or function name such as `PaymentClient` or `schema_gen`, a file name or path; not the hotspot's own file), the advice takes its "already written" form instead: "The rule seems to be written down already: make it harder to miss" (`AGENTS.md:3` already says something about what the human corrected here ...; make it more prominent, move it closer to the code, or enforce it), with `documented_as: "instruction"` |
| `state-the-boundary` | 2+ `tool-rejected`, or 1 with a stated reason |
| `allow-or-document-the-boundary` | 2+ `permission-denied` that are not guardrail hits. When most are refusals by the agent tool itself (Claude Code's built-in "Blocked: sleep 30 followed by: ..." rule, the auto-mode classifier, a built-in safety check), the advice says so: "Built into the agent tool, not this repository", quotes what the refusal says to do instead, says that nothing in the repository needs to change, and carries `origin: "agent-tool"`. Such a rule is the same in every repository, so a hundred refusals of `sleep` are not a property of this one; it is reported because the round trips still cost time. These refusals are therefore reported apart: under "The agent tool's own rules (not counted above)" in every report and `agent_tool` in the JSON, and they are in no hotspot, score, headline count, `--fail-on` gate or comparison (`scoring.is_agent_tool_rule`: a `permission-denied` whose variant is `blocked`, `classifier` or `safety`). "Add an allow rule, or say it is off limits" only where nobody is known to have decided (a missing permission). Where a hook or a deny rule of `.claude/settings.json` refused the call, an allow rule is never suggested: "Tell agents this is off limits, or close the way around" when the agent then got the same thing done anyway (the same command in another form, the same file by another tool), "Tell agents this is off limits, and what to do instead" when the sessions end on the refusal |
| `the-guardrail-works` | 2+ guardrail hits (`guardrail_hits` on the row): a hook or a deny rule of the settings refused the call and the agent never got the refused thing to run afterwards. "The guardrail works; agents still try `pytest` first": names the command agents used instead where one of the same job followed (`replacement`), quotes the hook's own reason, and cites the instruction line that already names the alternative, if any. It is the one piece of advice a row of weak evidence gets; it never proposes an allow rule, a hook or a permission rule |
| `clarify-before-acting` | 2+ `user-interrupt`. On a command (one that humans stopped in 3+ sessions, see the signal) the advice is to say when it should be run and what to run instead |
| `add-a-fast-check` | 2+ `edit-revert` events with no check run between the edit and its undoing (see `state-the-constraint`), or `edit-thrash` events of turns that did not end on a successful command and ran no test, lint or type check (thrash next to a check was not for want of one). Not when the file has 2+ `no-convergence` events: a check that will not pass needs `explain-the-failing-check`, not another check. The text names only what was seen ("2 reverted edits in 2 sessions") |
| `state-the-constraint` | 2+ `edit-revert` where `add-a-fast-check` does not apply because a test, lint, type-check or build command ran between an edit and its undoing (`checked` on the event): the agents had a check and used it, and backed the change out. The advice is to write down what the file does not allow. Not when the file has 2+ `no-convergence` events |
| `learn-from-rework` | 2+ `human-rework` |
| `add-a-pointer` | 2+ `search-miss` in the file or directory, not counting searches for leftovers (`TODO`, `console.log`, conflict markers, ...), with other evidence or the same pattern missed in 2+ sessions by agents that went on looking for it (see the signal); lists the patterns |
| `summarize-this-file` | 2+ `read-loop`, next to other evidence |
| `use-the-working-command` | 2+ *setup/invocation* failures of a command were followed by the same successful command *doing the same job* (same role, or a wrapper of the same tool), in sessions where the failing command never worked afterwards. Never an install, version-control or housekeeping command. Also: 2+ `command not found` failures after which the same command ran under a versioned name of the program ("use `python3`, not `python`"), and 2+ setup/invocation failures after which the same command ran with a variable set in front of it and no file edited in between ("use `PYTHONPATH=src pytest`"; the value is shown only for a fixed set of variables such as `PYTHONPATH`, `NODE_ENV`, `DJANGO_SETTINGS_MODULE`, and only with excerpts, otherwise `NAME=...`) or with an option added ("use `npm install --legacy-peer-deps`"; only with excerpts). Also: ordinary failures after which another command for the same job passed with nothing changed, in 3+ sessions (`pnpm test` after a red `yarn test`: the wrong runner); never when the green run ran less than the red one (a path, a node id, `-k`, `--filter` ... the red line lacks, or under half its tests, unless the failing tests it named are in the selection) or when that runner went red in the session too. On Windows the launcher counts as a name of the program ("use `py`, not `python`") |
| `document-the-setup-step` | 2+ failures of a command that then passed after the same setup step with no file edited in between (`pip install`, `npm ci`, `docker compose up`, `cp .env.example .env`, or any other command that did so in 2+ sessions), or that then passed on a line that first activated an environment (`source .venv/bin/activate && pytest`, `poetry shell`, `conda activate <env>`): "run X before Y". Each failure is bound to the steps run between it and the next run of the same command; every step that helped 2+ times is listed, in order, with its own count ("run `pip install`, then `docker compose up -d` before `pytest`"), and named so that it can be run as written (`npx playwright install`, `go mod download`) |
| `document-the-environment` | 2+ *environment* failures of a command (and none of the above): a service that was not reachable ("say how to start PostgreSQL before `pytest`"; the service is named when the output identifies it), missing, expired or refused credentials (also a database, registry or cloud API that turns them down), an unset environment variable (named only with excerpts), a port that was already in use, a full disk, or a host name that could not be resolved (no network, or a host that is not allowed: "say what `curl` can reach from where agents work") |
| `document-the-command` | 2+ setup/invocation failures of a command (and none of the above). If the same command then worked in a subdirectory, the advice names it ("run `npm test` from `web/`"). A command that is keyed by where it ran (`pytest (src/)`) is titled "Document how to run `pytest` in `src/`"; a command whose name is a placeholder without excerpts is titled "Document how to run this command (its name is hidden without excerpts)". When every one of those failures was the shell not finding one and the same program, the advice names it instead: "`gh` is not installed where agents run" (install it there, or say what to use instead) |
| `name-the-missing-target` | 2+ setup/invocation failures of a command, most of which say that the make target or package script does not exist (`make: *** No rule to make target 'lint'.  Stop.`, npm's `Missing script`, `ERR_PNPM_NO_SCRIPT`, yarn's `Command "lint" not found`; a missing *prerequisite*, "needed by", is not this, and neither is an error that names another target than the one on the command line: `make test` answering "No rule to make target 'tst'" is a broken Makefile). Replaces `document-the-command`: there is nothing to document about running something that is not there. The same for a `just` recipe (`Justfile does not contain recipe`). The text lists up to 12 targets, scripts or recipes the Makefile, `package.json` or justfile (in the directory the command ran in) does define, and asks for an alias or a line saying what to run instead. If the file defines the missing name *now*, the advice is in the past tense instead: "`make typecheck` was missing in these sessions; it is defined now", with "re-scan with `--since <date>`" to confirm that the failures have stopped. Without excerpts neither the missing name nor the list is shown |
| `make-the-failure-actionable` | 1+ `retry-loop` on a command |
| `run-it-in-the-background` | 2+ times a server or watcher (`npm run dev`, `uvicorn`, `... --watch`) run in the foreground sat until the harness timed it out or a human had to stop it (a `user-interrupt` of that command); replaces `document-the-command` for those. No file is blamed and no time is claimed for a stopped server |
| `give-it-more-time` | 2+ harness timeouts of any other command (`npm run build` cut off after 2 minutes): say how long it takes, raise the timeout or run it in the background, or name a narrower command; replaces `document-the-command` for those. Not when agents then got the job done another way in 2+ sessions (the same command with a variable or an option in front, `CI=true npm test`, `npm test -- --watchAll=false`, or another command of the same job and family, quoted as typed where short: `npx vitest run`): the advice is then `use-the-working-command` with the most common working form, the others named after it, and no word about a longer timeout. And not for a watcher (next row) |
| `run-it-once-not-in-watch-mode` | 2+ harness timeouts of a command most of which were in watch mode: the output shows it had finished and was waiting for file changes ("Watch Usage", "Waiting for file changes", "watching for file changes", "press q to quit", ...; `WATCH_OUTPUT_RE`), or the line asked for it (`--watch`). With no working alternative seen: "`npm test` runs in watch mode by default", say how to run it once. More time would not help |
| `explain-the-failing-check` | 2+ `no-convergence` events from 2+ sessions, on the command ("Agents cannot get `pytest` to pass") or on a file edited during the rounds: document how to run and debug the check and what it expects |
| `say-if-it-is-known-broken` | A command lifted out of the weak class because it stays red: it failed 2+ times and was still red at the end, in 3+ sessions (and there is no `no-convergence` event on it): "`npm run e2e` keeps ending red": say that it is known to be broken and what to run instead, or fix it. Also: the same test named as failed in 3+ sessions, none of which made it pass, and at least once before the session had changed any file ("`tests/test_legacy.py::test_old` is red session after session": fix it, or mark it as a known failure and say so); this form is given once: on the test file's row where the failure is attributed to a file of the repository, and on the command's row only for a test that has no such file |
| `fix-the-flaky-test` | A test command failed and then passed on a plain re-run, with nothing changed in between, in 3+ sessions (variant `flaky`): "`npm test` looks flaky": fix or quarantine the flaky test, and say in AGENTS.md that a red run should be re-run once before anything is changed |
| `pull-before-pushing` | `git push` was rejected as non-fast-forward (the remote branch had commits the local one lacked) in 3+ sessions: "tell agents to bring the branch up to date before `git push`": say in AGENTS.md to fetch and rebase (or merge) before pushing, and whether a force-push is ever acceptable. No time is claimed |
| `push-with-upstream` | `git push` failed with "has no upstream branch" and went through with `--set-upstream` / `-u` in 3+ sessions: "tell agents to push a new branch with `git push -u origin HEAD`", or set `push.autoSetupRemote` where agents run. No time is claimed |
| `format-before-committing` | `git commit` was stopped by a pre-commit hook that rewrote files ("files were modified by this hook") and went through on the next attempt, in 3+ sessions: "run the formatter (`pre-commit run <hook id>`, or the hook's own command) before committing". The events are weak (score capped at 19, no time claimed); in fewer sessions they are the loop working and are not recorded |
| `file-is-gone` | a file hotspot whose file is not in the working tree **and** whose last commit in git deleted or renamed it (`file.removed_at`; needs git). Replaces every rule about what the file is like (`edit-the-source-not-the-output`, `split-or-anchor-large-file`, `make-edits-land`, `document-what-rewrites-this-file`, `path-agents-expect`, `add-a-fast-check`, `state-the-constraint`, `add-a-pointer`, `summarize-this-file`): "This file was renamed to `src/client.ts`" or "This file is no longer in the tree", with the date of that commit and `--since <date>` to leave the history out. What a human said (`write-down-the-rule`, `state-the-boundary`, ...) is kept below it. A path git never tracked, or a scan without git, is not "gone": nothing is known, and nothing changes |
| `note-the-background-rate` | repository-level only (top-level `recommendations`, never on a hotspot): `edit-miss` or `stale-edit` with 5+ events in 3+ files and 3+ sessions, over 20+ file visits. States the rate per file visit and names the files clearly above it (see "The repository's base rate"). Mentions a rewriting command only when misses followed one |

### Which files count as generated

A file counts as generated on one of three grounds (`recommend.file_facts`),
strongest first:

1. **A header that says so.** One of its first 10 lines is a *comment line*
   (it starts with `#`, `//`, `/*`, `*`, `<!--`, `--` or `;`) that carries
   `DO NOT EDIT` / `DO NOT MODIFY` (in capitals), `@generated`,
   `auto-generated` / `autogenerated`, or starts "Code generated by ...",
   "This file is generated by ...", "Generated by ..." (`generated_header`).
   In prose files (`.md`, `.rst`, `.txt`, `.adoc`) only an HTML comment
   counts: `#` is a heading there and `*` a bullet. Prose anywhere else does
   not count: a docstring saying "the summary is generated by the scan", a
   code generator holding the header as a string literal, a CONTRIBUTING.md
   asking not to edit the changelog by hand, a header below line 10.
2. **A name only tools write:** lockfiles, `*_pb2.py`, `*.pb.go`,
   `*.min.js`, `*.generated.*` and the like.
3. **An output directory that git does not track:** a path under `dist/`,
   `build/`, `out/`, `gen/`, `generated/`, `vendor/` or `node_modules/`
   counts only if git ignores it or does not have it. A tracked
   `build/package/Dockerfile`, `internal/gen/writer.go` or `cmd/out/main.go`
   is source like any other file.

What follows from each: the `edit-the-source-not-the-output` advice is plain
on ground 1 and hedged on 2 and 3. Human rework is set aside only on ground 2
(a lockfile bump or a regenerated client is a tool being re-run); a human
changing a file that merely *says* it is generated, or that sits in an output
directory, is counted like any other rework. How many rework events were set
aside is reported as `rework_set_aside` on the git source.

**Known false positives and misses:** a hand-written file whose first lines
quote such a header in a comment (a template for generated files, a test
fixture for a code generator) is taken as generated; a tool that puts its
notice in a docstring, in a string, or below a licence block longer than ten
lines is missed, as is a notice in another language or in lower-case "do not
edit". A hand-maintained file with a tool's name pattern (`*_pb2.py` written
by hand) gets the hedged advice, and human rework on it is not counted.

### What the repository already says

Most advice here ends in "say so in AGENTS.md". Where that has been done,
repeating it is wasted effort, so before a recommendation is given the
repository's instruction files are searched for what it is about
(`guidance.py`):

- **Files read:** `AGENTS.md`, `CLAUDE.md`, `CLAUDE.local.md`, `GEMINI.md`,
  `.github/copilot-instructions.md`, `.cursorrules` and `.windsurfrules` at
  the root; the files directly in `.cursor/rules/` and every `*.md` under
  `.claude/rules/` (at most 50 each); files that a `CLAUDE.md` or
  `CLAUDE.local.md` pulls in with an `@path/to/file.md` line (one level
  deep: imports of imported files are not followed; at most 20 per file;
  not inside code fences; only inside the repository); `AGENTS.md` and `CLAUDE.md` in every directory between the root
  and the hotspot (for `web/src/a.ts`: `web/` and `web/src/`; for a command
  keyed `npm test (web/)`: `web/`); and the `permissions.deny` rules of
  `.claude/settings.json` and `.claude/settings.local.json` (only the lines
  holding a deny rule; allow rules and other settings are not instructions).
  Files over 200 KB, binary files, and links that lead outside the
  repository are not read; a file reachable under two names is read once.
- **Above a scanned package.** When the scanned directory lies inside a git
  repository (`scan packages/web`), agents started there also load the
  instruction files above it. `AGENTS.md` and `CLAUDE.md` in every directory
  up to the repository root, and the full root list at the root itself (the
  directory holding `.git`), are read too, and cited relative to the scanned
  directory: `../../AGENTS.md:3`. Nothing above the repository root is read,
  nothing when the scanned directory is the root or is in no repository,
  and nothing when `repo_aliases` are given (the sessions were then recorded
  where this copy does not live, so what lies above the copy is not what
  those agents loaded).
- **What is looked for:** for a file, its repo-relative path or its file name
  (the path only for names such as `__init__.py` or `index.ts`); for a
  command, its key without the directory suffix (also for the command a
  rule proposes: `pnpm test (packages/web/)` is looked for as `pnpm test`,
  in the root files and in `packages/web/`; titles name the command as it
  is typed and the text says the directory); for advice that proposes
  something specific (`make test` instead of `pytest`, a setup step), that
  proposal. Matching is plain text on word boundaries: `pytest` is not
  found in `pytest-cov` or `pytest.ini`, `a.py` not in `data.py`. That makes
  a **mention**. For the "already documented" form more is required, an
  **instruction**: a command of several words (`make test`) must stand where
  a command stands (inside backticks, in a code fence, after a `$` prompt,
  or right after run / use / using / with / via / call / invoke / execute /
  try), so "We make test data with factories", "Please make test names
  descriptive" and "The CI runs make test on every PR" do not document
  `make test` (a single distinctive word such as `pytest` is enough as it
  is); and for a generated file the line that names it must also carry an
  instruction word (edit, change, modify, generated, regenerate, never,
  don't, do not, must not, overwrite, rewrite, touch, read-only), so "See
  src/schema_gen.py for an example of our style" does not count. For the
  two rules whose "already documented" form says that the files recommend
  something (`use-the-working-command`, `document-the-setup-step`), a line
  that names the proposal only to rule it out is not a mention: "Do not run
  `make test` locally", "never pytest directly", "`make test` was removed",
  "we no longer use make test" (a negation in the 40 characters before the
  name within its sentence, or "was removed / is deprecated / no longer",
  "is slow / is flaky / is too expensive" right after it). A setup step
  may be one bare word (`mytool`), so for `document-the-setup-step` every
  term must stand where a command stands (in backticks, in a code fence,
  after run/use/with/via), never in a Markdown heading: the title "#
  mytool" and "This repo is mytool, a widget library" document nothing.
  The repository's own name (its directory name) alone is never a mention
  of any kind, unless it stands where a command stands. Terms
  shorter than 4 characters, bare launchers (`make`, `npm`... alone) and
  pseudonymized keys are not looked for, so with `--no-excerpts` only paths
  and public command names can match.
- **What changes:** the recommendation gets `documented_in` (file and line,
  at most 3 places) and `documented_as`. If the files hold an *instruction*
  for what the rule would have proposed (`documented_as: "instruction"`),
  its title and text switch to a form that asks the reader to confirm it and
  then to enforce it: "`AGENTS.md:12` already names `make test`; agents
  still ran `pytest` and failed 7 times in 4 sessions. Check that the cited
  line tells agents to use `make test` here. If it does, another line will
  not help; the next step is to enforce it: a hook or permission rule that
  blocks `pytest`, or a wrapper ..." (`use-the-working-command`,
  `document-the-setup-step`, `edit-the-source-not-the-output`). If there is
  only a *mention* (`documented_as: "mention"`), and for other advice that
  amounts to writing something down (`DOCUMENTABLE` in `recommend.py`), the
  recommendation keeps its normal title and text and gains a neutral note:
  "(Also mentioned at `AGENTS.md:12`: check whether it already says this.
  If it does, enforce it with a hook, a permission rule or a wrapper rather
  than adding another line.)". If the files say the *opposite* of what the
  sessions show (`documented_as: "conflict"`): an instruction to run the
  very command that keeps failing ("Use `make test`, not `pytest`", "Always
  run tests with `make test`", a code fence that lists it), or a line that
  rules out the command agents fell back to ("Never run `pytest`
  directly"), `use-the-working-command` never proposes the contradicting
  line. It says: "`AGENTS.md:3` tells agents to run `make test`, which
  failed 4 times in 4 sessions; agents fell back to `pytest`. Make `make
  test` work where agents run, or change that line." (or "... tells agents
  not to run `pytest`, yet `make test` failed ..."), and has no
  `replacement`. Advice that is not about documentation
  (splitting a large file, learning from rework) is never changed.
- **What it does not know:** whether the line says the right thing. The
  context rules are about form, not meaning: "`make test` only works in CI"
  or "ask before you run `make test`" stands in command context without
  recommending it, and "schema_gen.py changed a lot last year" has an
  instruction word without instructing anything; both are taken as
  instructions (the residual false positive, which is why the text asks the
  reader to check the cited line instead of asserting what it says).
  Conversely an instruction split over two lines (the file named on one,
  "never edit it" on the next) is only a mention. Nor is it known whether
  the line was written before or
  after the sessions that were analysed; the text says so ("if that line is
  newer than these sessions, give it time first"). The instruction files
  are never quoted, with or without excerpts: only `file:line` is shown.

### Guardrails that hold

Advice such as "enforce it: a hook or permission rule that blocks `pytest`"
has to be something a repository can follow and then see closed. So a
refusal is read for who refused and what the agent did next:

- **Deliberate** is a refusal by a hook, or one that a `permissions.deny`
  rule of `.claude/settings.json` / `.claude/settings.local.json` matches
  (`Edit(src/gen/**)` covers Edit, Write and MultiEdit on files under it,
  matched like a gitignore line; `Bash(pytest:*)` covers a command line or a
  step of one that starts with `pytest`). A missing permission ("you haven't
  granted it yet") or a refusal no rule accounts for is not: nobody is known
  to have decided.
- A deliberate refusal is a **guardrail hit** when the agent never got the
  refused thing to run later in the session (the same command, or a change
  to the same file by any tool, `sed -i` included) and did something else
  afterwards. Where a successful command of the same job and family followed
  (`make test` after `pytest`), it is recorded as the alternative.
- A guardrail hit is weak evidence, like a red test: weight 0.3
  (`GUARDRAIL_SHARE`), no part in confidence, and on a row with nothing but
  weak evidence the score is capped at 19, below every band a threshold
  such as `--fail-on 45` would be set to. Each row carries the count as
  `guardrail_hits`, so a comparison can tell "still failing" from
  "enforced".
- The row gets `the-guardrail-works`, and nothing that tells the reader to
  add what is already there: no allow rule (it does not override a hook and
  would contradict a deny rule), no "enforce it with a hook or a permission
  rule" in the note about instruction files (dropped whenever a refusal on
  the row was deliberate, or the only place the subject is mentioned is the
  deny rule itself).
- If the session ends on the refusal, nobody knows what came next: it stays
  an ordinary `permission-denied`, with advice that still never proposes an
  allow rule. If the agent got the same thing done anyway, the guardrail
  leaks, and the advice says so.

### One session is a lead

A hotspot seen in a single transcript session with a score below 20 gets no
recommendation: one bad afternoon is something to look at, not grounds for
telling anyone to change their repository. The exception is advice that
rests on the human's own words in that session, which is passed on as the
question it is: `write-down-the-rule` (in its "check whether this is a
standing rule" form) and `state-the-boundary`.

## Privacy and redaction

Transcripts contain secrets. The engine therefore:

- never emits thinking blocks or assistant prose;
- keeps everything a human typed, searched for or saw printed (prompts,
  search patterns, command lines, command output, reasons for declining,
  commit subjects) in exactly two places: `excerpt` and the quotes inside
  recommendation details. Both disappear with `--no-excerpts`.
  What remains then is structural: tool names (built-in ones as they are;
  any other, such as an MCP server's or a plugin's, as `<tool:hash>` or
  `mcp__<server:hash>__<tool:hash>`), repo-relative paths, counts,
  commit hashes and **command keys**. Without excerpts a key contains only
  words from a fixed public vocabulary (`pytest`, `npm run build`, `git
  commit`); every project-specific name in it (an unknown executable, a
  script path, a make target, an npm script, a module) is replaced by a short
  hash placeholder such as `<target:37bd73>` (see
  [Attribution](#attribution)). Arguments are never part of a key in either
  mode. The same holds for the keys of the top-level `exposure` object. A
  6-digit hash of a guessable name can be confirmed by someone who already
  suspects the name; it is a pseudonym, not encryption;
- redacts **before** truncating (default 240 characters), so a secret cannot
  be cut in half and slip through;
- applies these rules, in order: private-key blocks; AWS access keys; vendor
  API keys (Anthropic, OpenAI-style `sk-`, GitHub, GitLab, Slack, Google,
  Stripe, npm, Hugging Face); JWTs; `Authorization:` header values;
  `Bearer`/`Basic` credentials; credentials in URLs (`scheme://user:pass@`);
  `-u user:pass` / `--user`; `--password x` style flags; `password=`,
  `token:`, `api_key=`, `secret=` ... assignments, including URL query
  parameters (`?token=`, `&sig=`); `Cookie:` / `Set-Cookie:` header values;
  connection-string fields (`AccountKey=`, `SharedAccessKey=`, `Password=`);
  `mysql -pSECRET` / `sshpass -p SECRET`; SendGrid keys (`SG.x.y`); prose
  such as "the password is x"; `docker login -p x`; assignments to names
  ending in `_KEY`, `_ID`, `_SALT` ... (`SECRET_KEY = '...'`); upper-case
  environment-style names with `SECRET`, `TOKEN`, `PASSWORD`, `PASSWD` or
  `KEY` anywhere in them (`SECRET_KEY_BASE=x`, `SIGNING_KEY=x`); `vault
  login X`, `htpasswd -b file user X`, `net use ... /user:u X`; e-mail
  addresses (not `user@8.8.4.4`: an IP host is not a mailbox); private
  network addresses (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16) and host
  names ending in `.internal`, `.local`, `.corp` or `.lan` (`db.internal`,
  `build01.corp`; not `settings.local.json` or `threading.local()`);
  `.netrc` entries (`machine h login u password X`, or a line that is only
  `password X`); Slack and Discord webhook URLs; `openssl ... -k X` and
  `-pass pass:X` / `-passin` / `-passout`;
  hex strings of 32+ characters, **except** strings of exactly 40 lower-case
  hex digits, which are git commit ids and are kept; bare 40-character
  base64 strings containing `/` or `+` (the shape of an AWS secret access
  key); mixed-case alphanumeric
  strings of 24+ characters with Shannon entropy of at least 4.0 bits per
  character; home directories (`/home/x`, `/Users/x`, `C:\Users\x`, the real
  home) become `~`.

**Card numbers.** A run of 13 to 19 digits, unbroken or in the groups cards
are printed in (4-4-4-4, 4-6-5; spaces or dashes), that passes the Luhn
check becomes `[REDACTED:card-number]`. Other long numbers (ids, epoch
timestamps) fail the check nine times out of ten and are left alone.

**Control characters.** Transcripts can contain terminal escape sequences
(`ESC[2J`, OSC title changes) and invisible direction marks (U+202E). ANSI
sequences, C0/C1 control characters other than newline and tab, bidi
controls and zero-width characters (U+200B-U+200F, U+2060, U+FEFF) are removed from every excerpt and, in a final pass, from every
string of the result: paths, command keys, summaries, recommendation text.

Redaction is pattern matching and will miss secrets that look like ordinary
words (a short password passed as a bare argument, an internal code name).
Review a report before publishing it, or use `include_excerpts=False`.
**Where the transcripts live is not shown.** Claude Code names a project
directory after the absolute working directory
(`-Users-alice-work-client-repo`), which gives away a user name and often a
client. That directory never appears in the result: a Claude Code source is
`claude-code:<repo name>` and evidence cites
`claude-code:<repo name>/<session file>.jsonl` plus the line. With
`include_excerpts=False`, `evidence[].source` is `null` (session id and line
identify the event). All other paths (`repo.root`, generic transcript files)
follow one rule: anything that looks like a home directory becomes `~`.

## Known limitations

- **Prompt time is known only where the transcript records it.** Without a
  `permissionDecision` (or `prompted`) the split between "the command ran"
  and "the prompt stood open" is a bound, not a measurement: a failing
  command that honestly ran for minutes, in a session with no other run of
  it to compare with, is counted at 30 seconds, and a prompt answered within
  30 seconds is counted as run time. A person-approved call claims nothing
  before its result, although the tool also ran for part of that time. Both
  err towards claiming less.

- **Background commands are only as visible as their reports.** A command
  launched in the background is judged from the first `BashOutput` /
  `TaskOutput` result or `<task-notification>` that says how it ended. If
  the agent never asks and no notification is recorded, the command has no
  outcome: it is neither a failure nor a success, and a server that crashed
  unnoticed leaves no event. A notification carries no output, so a failure
  known only from one is an ordinary failure with nothing to classify,
  unless the agent reads the log afterwards. A report with status `running`
  is not judged even if the output so far already shows an error, and a
  task the agent killed is never a failure, whatever it had printed. The
  time such a command took is not known and none is claimed for the run
  itself.

- **Heuristics, not understanding.** A correction is recognised by its
  wording plus what the agent does next; a setup error by its output. Both directions of error exist and are
  listed per signal in the catalog.
- **Exit statuses that are not the command's own.** `pytest | tail -5`,
  `make test; echo done` and `pytest || true` exit 0 whatever happened. Such
  a run is judged from its output (see `command-failure` in the catalog); so
  is `pytest; which time` with a non-zero exit, whose status is that of the
  question asked last. When a later step of the line prints a file that
  nothing earlier on the line names (`make build; cat docs/troubleshooting.md`),
  the output is not judged at all: an error in it may be the document's. What
  remains: a failure whose telling lines were cut off by the filter (`| tail
  -1` on a tool that prints its verdict earlier, `| grep -c`), or printed in
  a shape that is not on the list, is "unknown" and counts as neither red nor
  green, so that failure is missed; output redirected to a file and read
  back with a separate `cat` is not connected to the command; `; echo $?`
  is not parsed; and a script that prints "3 failed" as ordinary content
  behind a pipe is read as red.
- **One command key per directory, by name.** `npm test (web/)` is told apart
  from `npm test (api/)` only in what is shown and scored. A green run in one
  package still closes a red-fix-green loop that was opened in another.
- **Where a command runs is followed lexically.** `cd`, `pushd` and
  directory options (`git -C`, `make -C`, `npm --prefix`, `uv --directory`)
  are resolved from the text of the command line. A `cd` through a variable,
  a symlink or a shell function is not seen: such a command is judged by the
  event's recorded working directory.
- **Bots are recognised by name.** A CI formatter that commits under a
  person's name counts as that person unless `bot_commit_patterns` says
  otherwise.
- **Friction is not always the codebase's fault.** Some events reflect the
  agent, the prompt or the harness. The recurrence requirement behind
  "medium" and "high" confidence is the main defence; treat "low" as a lead.
- **Red tests are normal.** A red run that is followed by a change and a green
  run of the same job is not counted, and ordinary failures never produce a
  recommendation. Other ordinary failures are down-weighted, not removed: a
  session that ends red, or a flaky test, still adds a little score. A retry
  loop needs three identical failing invocations with no change, and no
  command that may have changed something, in between, and is dropped as
  *waiting* if the same invocation then passes untouched (which also hides a
  flaky test that fails twice in a row).
- **Only tool calls are seen.** Edit signals (`edit-miss`, `edit-revert`,
  `edit-thrash`, `stale-edit`) come from the harness's edit and write tools.
  An agent that changes files mostly through the shell (heredocs, `python -
  <<EOF` scripts, `sed`) leaves no such events: its files look quiet, and
  only its command failures and the humans' reactions are visible. Such
  commands are known to be changes (next point), but not which lines of
  which file they changed.
- **A resumed session is one session.** When a transcript replays the lines
  of an earlier one under a new session id (Claude Code's resume), both files
  count as the earlier session. A session continued without replayed lines
  counts as a new one.
- **Edit reverts cannot see intent.** An exact undo is counted unless it
  removes lines that were only added and were run successfully once (a debug
  print). A deliberate experiment that replaces code and is taken back is
  counted although nothing went wrong.
- **Changes made by shell commands are assumed, not seen.** `sed -i`,
  formatters, `--fix` flags, codemods, `mv`/`cp`/`rm`/`git mv` and inline
  code that calls a write function count as edits. Any other command that
  ran and is neither a look nor a check (`python tools/fix_imports.py`, `npm
  install`, `git commit`) counts as "may have changed something": enough to
  end a retry loop, never enough to name a file. A red run that goes green
  after such commands alone is kept when the command was a setup step, or
  when the same pair recurs in two sessions; otherwise it is dropped, which
  errs towards forgiving: a one-off script that really was the missing
  setup step is not reported until it happens again. It still misses changes made
  outside the transcript (the human's editor, another agent or subagent, a
  watcher) and changes made by commands named like checks (`pytest
  --snapshot-update`, a `make test` that regenerates files): there, red
  runs stay on the record and three in a row are a retry loop. See
  `retry-loop` in [signals](signals.md).
- **Time needs agent activity.** One round trip is measured to the agent's
  next tool call or message. In the generic format, without
  `assistant_message` events, a failure that ends a turn claims only the
  duration of the call.
- **Exposure is per session, not per call.** A file read once and a file
  edited fifty times in a session both count as one exposure. Exposure of a
  command hotspot ignores read-only inspection commands.
- **Chains of commands.** Which step of `a && b` failed is inferred from the
  output. If nothing recognisable is printed the event is marked ambiguous
  and blames no file.
- **Replacement commands are inferred from names.** `make test` is taken to
  do the job of `pytest` because both are test runners by name; a target
  called `make check` is not recognised as one.
- **Git rework is circumstantial.** Overlapping line ranges show that a human
  changed what the agent wrote, not why. It weighs little and never raises
  confidence.
- **Corrections are English-only.** Correction phrases are matched in
  English; corrections in other languages are missed (never misread).
- **Corrections are attributed by proximity.** A certain correction belongs
  to the changed file the human names or the agent goes back to; if the human
  meant something else in the same breath, the attribution is wrong.
- **Command grouping is coarse.** `pytest tests/a.py` and `pytest tests/b.py`
  are one hotspot; `pytest` and `python -m pytest` are two; every use of a
  tool that is not on the subcommand allowlist is one hotspot (`mytool build`
  and `mytool deploy` are both `mytool`). When `a && b` fails we cannot tell
  which step failed unless the output shows it (a target or script the
  runner names, a check's own summary, or the runner's generic line, in that
  order; see `command-failure`). The shell is tokenized,
  not interpreted: aliases, functions, `eval` and command substitution are not
  followed.
- **Corrections are under-counted on purpose.** `totals.corrections` counts
  only messages whose wording addresses the agent's work **and** that are
  corroborated by behaviour (the human names a file the agent had just
  changed, or the agent goes back to one). Everything else that sounds like a
  correction is "probable": listed, weighted 0.4, never quoted, not in the
  headline count. Polite, indirect and non-English corrections, and
  corrections of something done with a command rather than an edit, are
  missed. The count is a lower bound, and it is still a heuristic: see the
  false positives in the catalog.
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
  counted (`skipped_lines`), never fatal. An event that makes a detector
  raise is skipped and counted (`skipped_events`).
