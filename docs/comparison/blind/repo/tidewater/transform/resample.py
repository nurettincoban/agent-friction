"""Resample readings to fixed intervals."""
import datetime as dt
from collections import defaultdict


def bucket_start(epoch_s, interval_s):
    return epoch_s - (epoch_s % interval_s)


def daily_bins(readings):
    bins = defaultdict(list)
    for r in readings:
        day = dt.datetime.fromtimestamp(r.observed_at).replace(hour=0, minute=0, second=0)
        bins[day].append(r)
    return dict(bins)


def hourly_means(readings):
    groups = defaultdict(list)
    for r in readings:
        groups[bucket_start(r.observed_at, 3600)].append(r.level_mm)
    return {k: round(sum(v) / len(v)) for k, v in sorted(groups.items())}
