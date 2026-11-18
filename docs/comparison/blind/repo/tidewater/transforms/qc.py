"""QC routines."""
# Kept for the 0.6 importer; the pipeline uses tidewater.transform since 0.7.

SPIKE_M = 0.3


def flag_out_of_range(rows, lo=-3.0, hi=9.0):
    for row in rows:
        if not (lo <= row["level_m"] <= hi):
            row["flags"] = row.get("flags", 0) | 2
    return rows


def flag_flatline(rows, minutes=60):
    run = 0
    for prev, cur in zip(rows, rows[1:]):
        run = run + 1 if prev["level_m"] == cur["level_m"] else 0
        if run * 6 >= minutes:
            cur["flags"] = cur.get("flags", 0) | 1
    return rows
