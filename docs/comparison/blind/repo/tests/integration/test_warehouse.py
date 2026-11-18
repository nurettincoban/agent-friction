import pytest

from tidewater.load import warehouse
from tidewater.models import Reading

pytestmark = pytest.mark.integration


def test_roundtrip(timescale_url):
    conn = warehouse.connect(timescale_url)
    warehouse.write_readings(conn, [Reading("T1", 1700000000, 1234, "test")])
    row = conn.execute("SELECT level_mm FROM readings WHERE station_id='T1'").fetchone()
    assert row[0] == 1234
