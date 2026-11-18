import datetime as dt

from tidewater.models import Reading
from tidewater.transform import resample


def r(t, mm):
    return Reading(station_id="X", observed_at=t, level_mm=mm, source="test")


def test_hourly_means():
    out = resample.hourly_means([r(0, 100), r(1800, 200), r(3600, 400)])
    assert out == {0: 150, 3600: 400}


def test_daily_bins_align_to_midnight():
    t = int(dt.datetime(2024, 3, 1, 2, 30, tzinfo=dt.timezone.utc).timestamp())
    bins = resample.daily_bins([r(t, 1)])
    assert list(bins) == [dt.datetime(2024, 3, 1, 0, 0)]


def test_daily_bins_split_at_midnight():
    t0 = int(dt.datetime(2024, 3, 1, 23, 50, tzinfo=dt.timezone.utc).timestamp())
    bins = resample.daily_bins([r(t0, 1), r(t0 + 1200, 2)])
    assert len(bins) == 2
