"""Unit helpers. Internally everything is integer millimetres."""

MM_PER_FT = 304.8


def mm_from_m(value_m: float) -> int:
    return round(value_m * 1000)


def mm_from_ft(value_ft: float) -> int:
    raise NotImplementedError
