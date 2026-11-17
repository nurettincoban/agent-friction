# Score sheets for the `/insights` comparison

Two graders scored each item against the [answer key](answer-key.md): **A**, one of the
authors, and **B**, an independent model grader given only the key, the saved reports and the
transcripts. Each cell is score (2 found, 1 partial, 0 missed) and whether the fix offered
works as given (Y/N; "-" where none is expected). `/insights` columns are runs 1, 2, 3.
Where the graders differ, both are shown as A | B.

## Shopfront

| Item | `/insights` run 1 | run 2 | run 3 | agent-friction |
| --- | --- | --- | --- | --- |
| S1 | 2, Y | 2, Y | 2, Y | 2, Y |
| S2 | 2, Y | 2, Y | 2, Y | 2, Y |
| S3 | 1, N (advises a Stop hook that runs `pytest`) | 1, N | 1, N (advises running `pytest`) | 2, Y |
| S4 | 0, N | 0, N | 0, N | 2, Y (generic: "say where it is or that it does not exist") |
| S5 | 1, N | 1, N | 1, N | 2, Y |
| S6 | 2, Y | 2, Y | 2, Y | 2, Y |
| S7 (coverage) | not read | not read | not read | read |
| D1 decoy | not raised | not raised | not raised | not raised (a low-confidence row only) |
| D2 decoy | not raised | not raised | not raised | not raised ("Not counted") |

## Harness blocks

| Item | `/insights` run 1 | run 2 | run 3 | agent-friction |
| --- | --- | --- | --- | --- |
| H1 | 1, Y (`gh run watch`; also "backoff sleeps") | 1, Y (also "an `until` loop with `sleep 30`") | 1, Y (same) | 2, Y |
| H2 | 1 \| 2 (the count "User Rejected: 2" only) | 1 \| 2 | 1 \| 2 | 2 |
| D3 decoy | not raised | not raised | not raised | not raised (a lead under "Nothing to fix yet") |

H2 was settled at 2 by the key's wording ("counts or names the declines"). With grader A's
1, `/insights` would score 10 of 16 in each run instead of 11.

## Statements the sessions contradict

Grader B listed them per run; grader A checked each against the transcripts. Those kept are
in [comparison.md](../comparison.md#statements-the-sessions-contradict); one (the refunds
"built on `payments/legacy.py`") was dropped there as close to the human's own words. None
were found in agent-friction's output.
