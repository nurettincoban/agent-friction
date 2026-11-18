"""Client for the NOAA CO-OPS datagetter API."""
import time

import httpx

from tidewater.models import Reading

DEFAULT_BACKOFF = (0.1, 0.2, 0.4)


class NoaaError(Exception):
    pass


def _get(client, params, backoff=DEFAULT_BACKOFF):
    for attempt, delay in enumerate((0.0,) + tuple(backoff)):
        if delay:
            time.sleep(delay)
        resp = client.get("", params=params)
        if resp.status_code < 500 and resp.status_code != 429:
            resp.raise_for_status()
            return resp.json()
    raise NoaaError(f"gave up after {attempt + 1} attempts")


def parse(station_id, payload):
    out = []
    for row in payload.get("data", []):
        # API returns metres as a string, e.g. "1.234"
        level_mm = round(float(row["v"]) * 1000)
        out.append(
            Reading(
                station_id=station_id,
                observed_at=int(row["t_epoch"]),
                level_mm=level_mm,
                source="noaa",
            )
        )
    return out


def fetch_range(station_id, cfg, begin=None, end=None):
    with httpx.Client(base_url=cfg["ingest"]["noaa_base_url"], timeout=30) as client:
        params = {
            "station": station_id,
            "product": "water_level",
            "datum": "MLLW",
            "units": "metric",
            "time_zone": "gmt",
            "format": "json",
        }
        if begin:
            params["begin_date"] = begin
        if end:
            params["end_date"] = end
        return parse(station_id, _get(client, params))
