# Answer key: tidewater blind set

> **Provenance.** Written by an independent author (a separate model session) told not to
> read agent-friction's code, tests, docs or examples, and committed before agent-friction or
> `/insights` was run on these sessions. `make_sessions.py` regenerates `sessions/` byte for
> byte. Nothing here was changed after the runs.

Repository: `tidewater`, a Python pipeline that ingests tide-gauge readings, runs QC, resamples, and loads into TimescaleDB.
Session cwd: `/work/tidewater`. `repo/` is the snapshot taken before the first session. The generator replays all 12
sessions in date order against one shared virtual file system, so later sessions see earlier edits. Files that
sessions create (for example `tests/test_csv_drop.py` and `migrations/003_battery_mv.sql`) are not in `repo/`.

Sessions are referred to by their first 8 uuid characters:

| # | id | date | branch | task |
|---|----|------|--------|------|
| S1 | 0b6f3c1e | 09-02 | feat/station-datum | datum column in CLI; CSV `level_m` |
| S2 | 1c7a4d2f | 09-04 | feat/sensor-serial | optional `sensor_serial` on Reading |
| S3 | 2d8b5e30 | 09-09 | feat/spike-flag | spike QC flag |
| S4 | 3e9c6f41 | 09-15 | fix/noaa-retry-after | honour Retry-After |
| S5 | 4fad7052 | 09-22 | feat/battery-mv | `battery_mv` end to end + migration |
| S6 | 50be8163 | 09-29 | feat/backfill-since | `--since`, min-level filter |
| S7 | 61cf9274 | 10-02 | feat/hourly-gaps | `hourly_means` min_count |
| S8 | 72d0a385 | 10-07 | chore/spike-index | partial index + threshold 250 |
| S9 | 83e1b496 | 10-12 | chore/schema-sync | regen schema, `is_flagged`, flaky test |
| S10 | 94f2c5a7 | 10-15 | perf/csv-drop | speed up `parse_file` |
| S11 | a5a3d6b8 | 10-19 | feat/feet-stations | `mm_from_ft` + `level_ft` column |
| S12 | b6b4e7c9 | 10-20 | feat/daily-export | export-daily (CSV, then Parquet) |

Most events are ordinary successful work: reads, edits, and green `make test` runs.

---

## Planted items

### P1: Hand-editing the generated `tidewater/models/generated.py`
- **Planted:** `generated.py` is produced from `schema/readings.avsc` by `tools/codegen.py` (`make schema`). `make lint`
  runs `check-generated`, which diffs the file against the codegen output. CLAUDE.md says not to edit it by hand, but
  agents still go straight to the dataclass whenever they add a field or helper.
  - S2: the agent edits `generated.py`. `make lint` fails ("stale or hand-edited; run 'make schema'"), and the agent
    redoes the change in the schema.
  - S5: the agent's Edit to `generated.py` is **rejected** by the user ("Not that file… It's even in CLAUDE.md").
  - S9: the agent reads `generated.py`, runs `make schema` (which reorders fields because the schema gained `quality`),
    then tries an Edit with stale text. The result is **old_string not found**. The retried edit succeeds, but lint
    fails, and the user says "don't hand-edit generated.py at all, ever".
- **Sessions:** S2, S5, S9.
- **Found means:** names `tidewater/models/generated.py` (or "the generated Reading model") as the target. It must also
  say the file is codegen output from `schema/readings.avsc` / `make schema`, so hand edits get rejected, fail
  `check-generated`, or are overwritten and miss on regeneration. Pointing out that this is already in CLAUDE.md and
  agents still miss it is a plus.
- **Fix means:** any guard that acts before the edit, for example:
  - a permission deny / PreToolUse hook on Edit/Write to `tidewater/models/generated.py`, pointing to the schema plus
    `make schema`;
  - a more prominent instruction (top of CLAUDE.md, or the file header naming the schema and the command);
  - a documented home for helpers (`tidewater/models/__init__.py`), or codegen support for extra methods.
  Saying only "read CLAUDE.md" is not a fix, because it is already there.

### P2: Bare `pytest` fails `test_resample.py` because the machine is not on UTC
- **Planted:** `daily_bins` uses `datetime.fromtimestamp` (local time). The dev machine is on Atlantic time (AST/ADT).
  The Makefile's `test` target sets `TZ=UTC`, so `make test` passes while bare `pytest` / `python -m pytest` fails two
  tests: `test_daily_bins_align_to_midnight` and `test_daily_bins_split_at_midnight`. Nothing documents this apart
  from a Makefile comment.
  - S1 and S7: the agent spends several calls proving the failures are pre-existing (git stash, re-run, `date`).
  - S4 and S10: the agent recognizes the failures faster but still runs bare pytest first.
  - The S7 human: "that's been like that forever".
- **Sessions:** S1, S4, S7, S10.
- **Found means:** names the target: `tests/test_resample.py` / `daily_bins` failing under plain `pytest` while
  `make test` passes. It must also give the mechanism: a timezone dependency, where the Makefile sets `TZ=UTC` (or
  `daily_bins` uses local time). Reporting "resample tests fail sometimes" without the TZ or make-vs-pytest
  distinction scores 1.
- **Fix means:** any of:
  - make `daily_bins` UTC-aware (`fromtimestamp(..., tz=timezone.utc)`);
  - force `TZ=UTC` for every pytest invocation (conftest `os.environ["TZ"]` plus `time.tzset()`, or pytest
    `env`/ini);
  - document "run tests via `make test`" in CLAUDE.md.
  The best fix is the code or conftest change, so bare pytest works.

### P3: Misleading legacy package `tidewater/transforms/` next to the real `tidewater/transform/`
- **Planted:** `tidewater/transforms/qc.py` (plural) is an old dict-based copy that nothing imports. It has a
  plausible-looking `SPIKE_M = 0.3` constant and the same `flag_*` function names. Grep and Glob list it first.
  - S3: the agent adds `flag_spikes` there. The tests (which import `tidewater.transform.qc`) fail with
    AttributeError, and the agent reverts.
  - S8: the agent edits `SPIKE_M` there to change the threshold, and the user corrects it ("That's the old package…
    no s").
- **Sessions:** S3, S8.
- **Found means:** names `tidewater/transforms/` (or `transforms/qc.py`) as a dead or legacy look-alike of
  `tidewater/transform/qc.py` that agents edit by mistake. "Agents edited the wrong file" with no target scores 1.
- **Fix means:** any of:
  - delete `tidewater/transforms/`, or move it under an obviously legacy name (e.g. `tidewater/_legacy_v06/`);
  - add a deprecation note at its top, or a CLAUDE.md line saying the pipeline uses `tidewater/transform/` only.

### P4: Water levels are integer millimetres everywhere (restated in varied words)
- **Planted:** an unwritten convention. Every level value and every level knob inside the pipeline is an `int` in mm,
  and conversions happen once at the ingest edge via `transform/units.py`. CLAUDE.md doesn't state it; only the
  `units.py` docstring hints at it. Agents keep introducing floats or metres, and the human corrects them in
  different words each time:
  - S1: "`float(row["level_m"]) * 1000` hands a float to `level_mm`… That field is an int everywhere. There's already
    `units.mm_from_m`".
  - S3: "threshold in mm please, as an int… Nothing in transform/ should be dividing by 1000."
  - S6: "Why is `--min-level` a float in metres? Levels are integer mm end to end in this codebase, including the
    knobs."
  - S11: "don't route feet through metres… I don't want a float metre value existing in the ingest path, even
    transiently… round once."
- **Sessions:** S1, S3, S6, S11.
- **Found means:** states the convention itself: levels and thresholds are integer millimetres, with no float metres
  internally and conversion only through `units.*` at ingest. It must also note that the human keeps restating it
  across sessions. A score of 2 requires naming the unit/int rule. "The user corrected the agent several times about
  types" scores 1.
- **Fix means:** any of:
  - add the rule to CLAUDE.md (e.g. "all levels/thresholds are `int` mm; convert once at ingest with
    `tidewater/transform/units.py`; CLI and config knobs use `_mm` names");
  - optionally enforce it with a lint rule or test, such as a type check that `level_mm` is an int, or a ban on
    `/ 1000` in `transform/`.

### P5: Flaky wall-clock test `test_retry_backoff_is_bounded` "fixed" wrongly
- **Planted:** `tests/test_noaa_client.py::test_retry_backoff_is_bounded` really sleeps 0.1 + 0.2 s and asserts that
  elapsed time is under 0.35 s, so it flakes on loaded machines.
  - S4: the test fails in `make test` (0.4127 < 0.35). The agent widens the bound to 1.0, claims to have fixed the
    flake, and the user has it reverted.
  - S9: it failed in CI again. The agent tries `@pytest.mark.skip`, the user **rejects** it ("No skipping, and no
    bumping the number either"), and the right fix is to inject `sleep` into `noaa._get`.
- **Sessions:** S4, S9.
- **Found means:** names this test (or the NOAA retry/backoff test) and its mechanism: real `time.sleep` plus a
  wall-clock bound. It must also note that agents responded by loosening the threshold or skipping the test.
- **Fix means:** any of:
  - make the delay function injectable, or use a fake clock, and assert the requested delays rather than elapsed
    time (what S9 finally did);
  - add guidance such as "don't skip or loosen flaky timing tests; remove the real-clock dependency".
  Since S9 fixed the code, a fix that only says "it's fixed now" earns no fix credit. Noting the agent behaviour
  pattern does.

### P6: Slow `make test-all` run when it isn't needed
- **Planted:** `make test-all` starts docker TimescaleDB and runs integration tests. It takes about 6 minutes, longer
  than the Bash tool's 2-minute default timeout. Agents reach for it on changes the unit suite covers:
  - S2: it times out after 2 minutes; the user says "`make test` was enough".
  - S6: the user **interrupts** it ("it's the 6-minute docker run").
  - S10: it times out, then is re-run with `timeout: 600000` and takes 371.88 s; the user says "please don't kick off
    `test-all` for every little change".
- **Sessions:** S2, S6, S10.
- **Found means:** names `make test-all` and that it is slow (about 6 minutes, docker), times out under the default
  Bash timeout, and is unnecessary for most changes, with users pushing back. A report of "a command timed out" alone
  scores 1.
- **Fix means:** document in CLAUDE.md when `test-all` is warranted (warehouse or migration changes) and that
  `make test` is the default. It could also say to pass a long timeout or run in background when it is needed, or to
  provide a faster targeted integration target.

### P7: Agent claims success without verifying after its last change
- **Planted:** in each case the agent reports "done" based on a check that predates the final edit, or with no check
  at all. The human then finds it broken:
  - S5: after the tests ran, the agent edits `warehouse.write_readings` (adds a column but not a placeholder) and says
    "All tests pass". Staging then fails with "6 placeholders but 7 parameters".
  - S8: the agent writes `migrations/004_spike_idx.sql`, says "it's applied", and changes a test, having run neither
    `make migrate` nor tests. The user: "Applied where?"
  - S12: the agent rewrites `export.py` to Parquet and says "Done" without running it. `pyarrow` is not a dependency,
    so the command fails with ModuleNotFoundError.
- **Sessions:** S5, S8, S12.
- **Found means:** identifies a pattern of premature or unverified success claims: final edits after the last test
  run, claimed actions never executed, and humans asking "did you run it?". To score 2, the report must give at least
  two concrete instances or the mechanism: no re-verification after the final edit, and unit tests that don't cover
  warehouse, migrations, or the CLI. Naming the theme without instances scores 1.
- **Fix means:** any of:
  - a CLAUDE.md rule: re-run the relevant check after the last edit and before claiming done; for DB-touching
    changes, run `make migrate` / the integration test or say explicitly that it wasn't run;
  - a Stop hook that requires a test run after the last edit.
  Adding unit coverage for `write_readings` (e.g. a placeholder/column count test) also counts.

---

## Decoys (not repository problems; raising one as a problem to fix is a false positive)

- **D1: one-off PyPI read timeout (S7).** `pip install -e '.[dev]'` fails once with
  `HTTPSConnectionPool(host='files.pythonhosted.org'…): Read timed out` and succeeds on the immediate retry. This is
  transient network trouble, not a repo issue. The S8 `make migrate` "Connection refused" (DB not started) is also
  part of P7's recovery and is not a separate item.
- **D2: red-then-green TDD loop (S11).** The agent writes `test_mm_from_ft`, runs `make test` (Exit code 2,
  NotImplementedError, expected), implements it, and goes green. This is normal workflow.
- **D3: human changes their mind (S12).** "Actually, scratch CSV… make it Parquet. Sorry for the churn." The rewrite is
  a requirements change, not friction. (The unverified Parquet claim that follows belongs to P7.)
- **D4: one typo command (S3).** `mkae test` gives "command not found" (exit 127), once.

---

## Scoring rubric

For each planted item:

**Found score**
- **2:** names the specific target *and* the mechanism, as described in "Found means".
- **1:** symptom or count only (e.g. "Edit failed 3 times", "user rejected a tool call", "tests failed"), or the right
  theme on the wrong target. For example, "agents edit generated files" pointing at `transforms/qc.py`, or "flaky
  tests" pointing at `test_resample.py`.
- **0:** missed.

**Fix** (scored separately, yes/no): yes when the report proposes an actionable change matching "Fix means", either a
concrete CLAUDE.md line, hook, permission rule, code or test change, or file move/delete. Generic advice ("be
careful", "read the docs") is no.

**False positives:** count each decoy raised as a problem to fix. Also count anything else presented as a recurring
repo problem that isn't one, such as treating the S8 connection-refused as a separate DB-config bug.

**Notes for graders**
- P2 and P5 both involve test failures but have different targets and mechanisms. A report that merges them into
  "flaky tests" earns at most 1 on each.
- Mentions of rejection or interruption count toward P1, P5 or P6 only when tied to their target. "Users rejected 2
  tool calls and interrupted 1" with no targets scores 1 on at most one item.
- Max found score is 14 (7 x 2); max fix score is 7.
