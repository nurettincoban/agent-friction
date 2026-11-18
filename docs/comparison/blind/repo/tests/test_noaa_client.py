import time

import httpx
import respx

from tidewater.ingest import noaa


@respx.mock(base_url="https://example.test")
def test_parse_converts_to_mm(respx_mock):
    payload = {"data": [{"t_epoch": "1700000000", "v": "1.234"}]}
    out = noaa.parse("8443970", payload)
    assert out[0].level_mm == 1234


@respx.mock(base_url="https://example.test")
def test_retry_backoff_is_bounded(respx_mock):
    route = respx_mock.get("").mock(
        side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json={})]
    )
    start = time.monotonic()
    with httpx.Client(base_url="https://example.test") as client:
        noaa._get(client, {})
    elapsed = time.monotonic() - start
    assert route.call_count == 3
    assert elapsed < 0.35
