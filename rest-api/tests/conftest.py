import os
import sys
import time
from collections import deque

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


A = "28FF641E8D3C0A12"
B = "28AA000000000001"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test starts without any sensor and without a names file."""
    server.sensors.clear()
    monkeypatch.setattr(server, "NAMES_FILE", str(tmp_path / "sensor_names.json"))
    yield


@pytest.fixture
def client():
    # No context manager: the lifespan (and with it the background threads) is not started.
    return TestClient(server.app)


def add_sensor(sensor_id, value=21.5, age=0.0, measured_at="2026-01-01T12:00:00+00:00"):
    """Register a sensor whose last reading is `age` seconds old."""
    server.sensors[sensor_id] = {
        "temperature_c": value,
        "measured_at": measured_at,
        "mono": time.monotonic() - age,
        "history": deque(maxlen=server.HISTORY_CAP),
    }
    return server.sensors[sensor_id]


@pytest.fixture
def fresh_reading():
    add_sensor(A)


@pytest.fixture
def stale_reading():
    add_sensor(A, age=server.MAX_AGE_S + 1)
