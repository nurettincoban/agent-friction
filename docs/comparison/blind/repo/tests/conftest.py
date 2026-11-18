import pytest


def pytest_addoption(parser):
    parser.addoption("--timescale-url", default=None)


@pytest.fixture
def timescale_url(request):
    url = request.config.getoption("--timescale-url")
    if not url:
        pytest.skip("no --timescale-url")
    return url
