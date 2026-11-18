from tidewater.transform import units


def test_mm_from_m():
    assert units.mm_from_m(1.2345) == 1234
    assert isinstance(units.mm_from_m(0.5), int)
