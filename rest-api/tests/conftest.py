import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """Every test starts without a reading and with an empty history."""
    server.latest.update(temperature_c=None, measured_at=None, mono=0.0)
    server.history.clear()
    yield


@pytest.fixture
def client():
    # No context manager: the lifespan (and with it the background threads) is not started.
    return TestClient(server.app)


@pytest.fixture
def fresh_reading():
    server.latest.update(temperature_c=21.5, measured_at="2026-01-01T12:00:00+00:00", mono=time.monotonic())


@pytest.fixture
def stale_reading():
    server.latest.update(
        temperature_c=21.5,
        measured_at="2026-01-01T12:00:00+00:00",
        mono=time.monotonic() - server.MAX_AGE_S - 1,
    )
