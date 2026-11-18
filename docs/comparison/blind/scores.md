# Blind set scores: tidewater

Graded against `answer-key.md` (P1–P7, D1–D4, rubric). Found score is 2/1/0; fix is yes/no. Session claims were checked against `sessions/*.jsonl`.

**How the rubric was applied.** A 2 needs the target and the mechanism from "Found means". Where the key says a report "must" state something, that element is required. For P1 that is the codegen source (`schema/readings.avsc` / `make schema`), and the Avro schema counts. Facets the key lists without a "must" are noted when missing but do not cost the point. An example is P6's 2-minute Bash timeout, which no report mentions.

## Totals

| Report | Found (of 14) | Fixes (of 7) | Decoys raised as a fix (of 4) | Statements the transcripts contradict |
|---|---|---|---|---|
| insights run 1 | 14 | 7 | 0 | 3 |
| insights run 2 | 14 | 7 | 0 | 3 |
| insights run 3 | 13 | 7 | 0 | 3 |
| agent-friction (before) | 5 | 1 | 0 | 5 |

## Planted items

Each cell gives the score, the fix verdict, and the report's own words.

| Item | insights run 1 | insights run 2 | insights run 3 | agent-friction |
|---|---|---|---|---|
| **P1** generated.py | **2 / fix Y**: "hand-edited the generated Reading dataclass and models file … despite CLAUDE.md saying they are overwritten from the Avro schema". Fix: a PreToolUse hook (`grep -qE '…generated|models\\.py'`, which matches the real path), and a more prominent CLAUDE.md block saying to put `is_flagged` "in a separate, non-generated module". | **2 / fix Y**: "hand-edited the generated models file first … only noticed after lint failed … redirect it to the schema". Fix: the CLAUDE.md section "Generated code — NEVER hand-edit". Its hook glob `*/generated/*\|*_generated.py` does **not** match `tidewater/models/generated.py`, so the yes rests on the CLAUDE.md block. | **2 / fix Y**: "first edited the generated models file. Lint flagged it, and the edit would have been overwritten on the next regen". Fix: a "PreToolUse hook on Edit/Write that rejects paths under generated dirs … pointing to the schema and regen command". | **1 / fix Y**: "Generated file: telling agents was not enough … agents ran into trouble editing it directly". Names the file and the existing CLAUDE.md lines, but not the source or the regen command, and not what goes wrong. Fix: "deny direct edits with a permission rule, or add a pre-edit hook". |
| **P2** TZ / bare pytest | **2 / fix Y**: "TZ-dependent daily_bins failures"; "`make test` with TZ=UTC is the right test loop". Fix: "Default to `make test` (unit tests, sets TZ=UTC)". | **2 / fix Y**: "daily_bins timezone failures"; "`make test`, which already handles TZ=UTC". Fix: a CLAUDE.md "Default: run `make test` (it sets TZ=UTC)", plus an agent to "fix the root cause … without forcing TZ=UTC". | **2 / fix Y**: "the local non-UTC timezone also keeps surfacing as an environment trap"; "daily_bins timestamp failures". Fix: "fix the root cause of the daily_bins timestamp failures so both pass, without forcing UTC", plus a `make test-tz` target. | **1 / fix N**: a low-confidence row, "`pytest` … 1 … command-failure×1". Symptom only: no test name, no TZ. |
| **P3** transforms/ | **2 / fix Y**: "`tidewater/transform/` is the live package. `tidewater/transforms/` is obsolete legacy code, so never edit it". Fix: that CLAUDE.md line, plus a hook. | **2 / fix Y**: "QC lives in `tidewater/transform/qc.py`. … `transforms/` package is DEAD legacy code". Fix: CLAUDE.md, plus a `*/transforms/*` hook. | **2 / fix Y**: "edited the dead `tidewater/transforms/qc.py` before finding the real `tidewater/transform/qc.py`". Fix: a hook on `tidewater/transforms/`, plus an "edit here, never there" map. | **1 / fix N**: "tidewater/transforms/qc.py 2 2 1m ◐ edit-revert×2". Names the target but never says it is legacy or a look-alike. |
| **P4** int mm | **2 / fix Y**: "All levels, thresholds and config knobs are **integer millimetres** … Always convert using the helpers in `units`"; "the user had to correct each one". Fix: that CLAUDE.md block. | **2 / fix Y**: "All levels, thresholds and CLI knobs are `int` mm … Never convert through metres"; "corrections came up in 4 sessions". Fix: CLAUDE.md. | **2 / fix Y**: "integer mm everywhere, always use units.mm_from_*"; "Units were a running theme … the user also had to push back". Fix: a CLAUDE.md "Conventions" section. | **0 / fix N**: not mentioned. The header says "0 human corrections". |
| **P5** flaky retry test | **2 / fix Y**: "loosened the bound on the unrelated test_retry_backoff_is_bounded … tried to skip a flaky test"; "fix the root cause … (e.g. inject a sleep function)". Fix: "Do not loosen, skip or xfail unrelated or flaky tests". | **2 / fix Y**: "tried to skip a flaky retry test and … loosened an unrelated test's timing bound"; "make the retry test deterministic with an injected clock". Fix: "Fix flaky tests at the root cause. Never skip them or loosen timing bounds." | **1 / fix Y**: "loosened `test_retry_backoff_is_bounded` without being asked … tried to skip a flaky test". Names the test and the agent behaviour, but not the mechanism: nothing on real `sleep` or the wall clock. Fix: "never skip, loosen … tests". | **1 / fix N**: "tests/test_noaa_client.py 1 1 n/a ○ tool-rejected×1". Rejection on the target, with no mechanism and no loosen/skip pattern. |
| **P6** make test-all | **2 / fix Y**: "reached for the 6-minute docker-backed suite when `make test` was enough". Fix: "Do NOT run `make test-all` … unless … the change touches warehouse SQL". Does not mention the timeout. | **2 / fix Y**: "runs the 6-minute docker-backed test-all suite for small changes … You had to stop it". Fix: a CLAUDE.md testing default, plus a Bash hook. Does not mention the timeout. | **2 / fix Y**: "running the 6-minute docker full suite for tiny changes"; "run integration only when DB or warehouse code changes". Fix: a test policy, plus a hook. Does not mention the timeout. | **1 / fix N**: "make test-all 2 2 6m ◐ command-failure×2". Failure counts only: no docker, not "unneeded", no pushback. |
| **P7** unverified "done" | **2 / fix Y**: three instances (pyarrow, migration, 6 placeholders for 7 params). Fix: a "Done means verified" CLAUDE.md block, plus "a Stop hook could require a passing `make test`". | **2 / fix Y**: the pyarrow, migration and placeholder instances. Fix: "you must have run the relevant command AFTER your last edit", plus a Stop hook. | **2 / fix Y**: "'All tests pass' after a warehouse edit it never ran … Parquet export was declared 'Done' without being run". Fix: a Stop hook, plus a verify loop. | **0 / fix N**: no pattern. The "python3 -m tidewater.cli … command-failure×1" row is the S12 symptom, without the claim. |
| **Total** | **14 / 7 fixes** | **14 / 7 fixes** | **13 / 7 fixes** | **5 / 1 fix** |

## Decoys

| Decoy | run 1 | run 2 | run 3 | agent-friction |
|---|---|---|---|---|
| D1 PyPI timeout (S7) | not raised. Only an "Environment Issue 1" count. | not raised. Only a count. | not raised. Only a count. | listed, not raised: a low-confidence "pip install … ○" row in the struggle table, not under "What to fix first". |
| D2 red-then-green TDD (S11) | not raised | not raised | not raised | not raised. It falls under "Not counted: 9 failed runs of normal work". |
| D3 CSV to Parquet change of mind (S12) | not raised | not raised | not raised as a problem. Described as "You also change direction mid-task". | not raised |
| D4 `mkae` typo (S3) | not raised | not raised | not raised | listed, not raised: a low-confidence "mkae … ○" row. |
| **Raised as a fix** | 0 | 0 | 0 | 0 |

Borderline, not counted: runs 2 and 3 call the S3 `pipeline.toml` change from 250 to 300 an unasked out-of-scope edit. Run 2 also turns it into a CLAUDE.md rule ("don't change … config values"). The agent did change the config without being asked, so this is a real single event, not a key decoy. What the transcripts contradict is the claim that the user rejected it or had it reverted (see below).

## Statements the transcripts contradict

**insights run 1**
1. "In 4 sessions Claude ran the slow docker suite when `make test` was enough." The sessions show `make test-all` in 3 sessions only: S2, S6 and S10.
2. "(the 250 mm default and later the configurable flag_spikes set to 300)". The order is reversed. 300 came first, in S3 on 09-09. 250 came later, in S8 on 10-07.
3. "none of these conventions seem to be written down where Claude reliably sees them" and "no explicit instructions were captured". The codegen rule is in CLAUDE.md lines 12–15, and the S2 agent read it. The report contradicts itself here: elsewhere it says "despite CLAUDE.md saying…".

**insights run 2**
1. "You reject out-of-scope edits, such as … changing a config value from 250 to 300 unasked." In S3 the user never objected to the config change.
2. "It forced TZ=UTC to hide timezone regressions … The user had to revert or correct each of these." The daily_bins failures were pre-existing, not regressions: the S7 user said "that's been like that forever". The user also endorsed TZ=UTC ("Run through `make test` or set TZ=UTC") and never reverted it.
3. "none of these conventions are written anywhere Claude reliably reads". The codegen rule is in CLAUDE.md, and the S2 agent read it there. The report again contradicts itself ("even though CLAUDE.md already mentions this").

**insights run 3**
1. "For battery_mv and sensor_serial, Claude first edited the generated models file. Lint flagged it". In S5 (battery_mv) the user rejected the Edit. Lint did not flag it, and the edit was never written. Lint flagged S2 and S9.
2. "When it loosened an unrelated timing bound or changed a config value unasked, you had it reverted". The timing bound was reverted in S4. The S3 config change was never reverted.
3. "Claude edited the dead `tidewater/transforms/qc.py` … That cost you a correction round each time." In S3 the agent found the mistake itself, through a failing test, and reverted it. The user's S3 correction was about units, not the package. Only S8 had a user correction about the package.

**agent-friction**
1. Header: "0 human corrections". The user corrected the agent in at least 10 sessions: S1 (float), S2 (test-all), S3 (mm), S4 (revert the bound), S5 (generated.py, placeholders), S6 (test-all, min-level), S8 (transforms, "Applied where?"), S9 (generated.py, skip), S11 (feet via metres) and S12 (pyarrow).
2. "agents ran into trouble editing it directly once in 1 session". The trouble came in 3 sessions: S2 (lint fail), S5 (rejected) and S9 (old_string miss, then lint fail). The report's own table also says "2 sess, 2 events".
3. "tidewater/cli.py … user-interrupt×1". The S6 interrupt was on the `make test-all` Bash call ("Not that one, it's the 6-minute docker run"), not on cli.py. That also leaves the `make test-all` row at 2 sessions instead of 3.
4. "`pytest` … 1 sess". A bare `pytest` failed on the resample tests in S1, S4 and S10, and `python -m pytest` failed in S7: 4 sessions in all. Some of these may sit in the "Not counted: 9 failed runs of normal work" line, but the row as shown understates it.
5. The generated.py row says "edit-miss×1, tool-rejected×1". It leaves out the two `make lint` check-generated failures in S2 and S9, which are the clearest evidence for this item. This is an undercount, not a false claim; it is listed because the report's headline rests on it.

## Grader notes

- The insights runs are close to the ceiling. All three name every planted item with its target, and each offers a CLAUDE.md rule or hook for all seven. Run 3 drops one point on P5 because it never gives the real-sleep / wall-clock mechanism. Run 3 has no "Suggested CLAUDE.md Additions" or "Features to Try" section, so its fixes come from the inline advice in "Where Things Go Wrong" and the "On the Horizon" prompts. These are concrete enough to count.
- Fix quality varies even where credit was given. Run 2's hook glob would not block `tidewater/models/generated.py`, so its P1 credit rests on the CLAUDE.md block. Run 1's hook pattern `models\.py` is broad but does match. No insights run names `make schema`: run 2 writes "`<fill in: e.g. make gen>`", and run 3 says "the regen command".
- No report mentions that `make test-all` exceeds the Bash tool's 2-minute default timeout. Under a stricter reading that makes this a required facet, every report would lose 1 on P6: insights 13/13/12, agent-friction unchanged at 5 (its P6 is already 1).
- The P1 score for agent-friction is a judgement call. It names the target, says the file is generated, and notes that CLAUDE.md already covers it (the key counts this as a plus). It never names the source or the command, as the key's "must" requires, so I gave it 1. A more lenient grader would give 2, for a total of 6.
- agent-friction lists the decoys D1 and D4 only as low-confidence rows, outside "What to fix first", so neither counts as a false positive. It also has no unit-convention item (P4) and no unverified-completion item (P7). Both are themes carried by what the human said, and its "0 human corrections" header shows that signal was not captured.
- The insights runs' false or garbled claims are mostly about attribution: which session, what order, and whether the user objected. None of them changes a found score.
