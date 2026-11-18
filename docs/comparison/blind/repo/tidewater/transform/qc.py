"""Quality-control flags for readings."""
from dataclasses import replace

FLAG_FLATLINE = 1 << 0
FLAG_OUT_OF_RANGE = 1 << 1

LEVEL_MIN_MM = -3000
LEVEL_MAX_MM = 9000


def flag_out_of_range(readings):
    out = []
    for r in readings:
        if not (LEVEL_MIN_MM <= r.level_mm <= LEVEL_MAX_MM):
            r = replace(r, qc_flags=r.qc_flags | FLAG_OUT_OF_RANGE)
        out.append(r)
    return out


def flag_flatline(readings, minutes=60):
    out = []
    run_start = None
    for prev, cur in zip([None] + readings[:-1], readings):
        if prev is not None and prev.level_mm == cur.level_mm:
            run_start = run_start or prev.observed_at
            if cur.observed_at - run_start >= minutes * 60:
                cur = replace(cur, qc_flags=cur.qc_flags | FLAG_FLATLINE)
        else:
            run_start = None
        out.append(cur)
    return out
