from tidewater.models import Reading
from tidewater.transform import qc


def r(t, mm):
    return Reading(station_id="X", observed_at=t, level_mm=mm, source="test")


def test_out_of_range():
    out = qc.flag_out_of_range([r(0, 100), r(360, 12000)])
    assert out[0].qc_flags == 0
    assert out[1].qc_flags & qc.FLAG_OUT_OF_RANGE


def test_flatline():
    rows = [r(i * 360, 500) for i in range(12)]
    out = qc.flag_flatline(rows, minutes=60)
    assert out[-1].qc_flags & qc.FLAG_FLATLINE
    assert out[0].qc_flags == 0
