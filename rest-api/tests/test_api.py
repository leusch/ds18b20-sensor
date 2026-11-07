import re
from datetime import datetime, timedelta

import pytest
import serial
from fastapi.testclient import TestClient

import server


class StopLoop(Exception):
    """Raised by fakes to leave an endless loop under test."""


# ---------------------------------------------------------------- is_fresh

def test_is_fresh_without_reading():
    assert server.is_fresh() is False


def test_is_fresh_is_false_without_value_even_if_timestamp_is_recent():
    server.latest["mono"] = server.time.monotonic()
    assert server.is_fresh() is False


def test_is_fresh_with_recent_reading(fresh_reading):
    assert server.is_fresh() is True


def test_is_fresh_includes_the_exact_age_limit(monkeypatch):
    server.latest.update(temperature_c=21.5, mono=100.0)
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0 + server.MAX_AGE_S)
    assert server.is_fresh() is True
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0 + server.MAX_AGE_S + 0.001)
    assert server.is_fresh() is False


def test_is_fresh_with_old_reading(stale_reading):
    assert server.is_fresh() is False


# ------------------------------------------------------------ process_line

@pytest.mark.parametrize(
    "line, expected",
    [
        ("Temperature: 21.50 C\n", 21.5),
        ("Temperature: -3.25 C\n", -3.25),
        ("Temperature: 20 C\n", 20.0),
    ],
)
def test_process_line_stores_reading(line, expected):
    server.process_line(line)
    assert server.latest["temperature_c"] == expected
    assert server.latest["measured_at"] is not None
    assert server.is_fresh() is True


def test_process_line_timestamp_carries_the_configured_time_zone():
    server.process_line("Temperature: 21.50 C\n")
    measured_at = datetime.fromisoformat(server.latest["measured_at"])
    assert measured_at.utcoffset() == datetime.now(server.TZ).utcoffset()


@pytest.mark.parametrize(
    "line",
    [
        "DS18B20 sensors found: 1\n",
        "No sensor detected - check wiring and pull-up resistor\n",
        "",
        "Temperature: abc C\n",
    ],
)
def test_process_line_ignores_other_lines(line):
    server.process_line(line)
    assert server.latest["temperature_c"] is None
    assert server.latest["measured_at"] is None


def test_process_line_keeps_previous_reading_on_garbage(fresh_reading):
    server.process_line("garbage\n")
    assert server.latest["temperature_c"] == 21.5


# ------------------------------------------------------------- take_sample

def test_take_sample_without_reading_leaves_gap():
    server.take_sample()
    assert len(server.history) == 0


def test_take_sample_with_stale_reading_leaves_gap(stale_reading):
    server.take_sample()
    assert len(server.history) == 0


def test_take_sample_stores_value_rounded_to_the_minute(fresh_reading):
    server.take_sample()
    assert len(server.history) == 1
    ts, value = server.history[0]
    assert value == 21.5
    assert ts.second == 0 and ts.microsecond == 0


def test_take_sample_keeps_recent_entries(fresh_reading):
    now = datetime.now(server.TZ)
    server.history.append((now - timedelta(hours=23), 10.0))
    server.take_sample()
    assert [v for _, v in server.history] == [10.0, 21.5]


def test_take_sample_discards_entries_older_than_24_hours(fresh_reading):
    now = datetime.now(server.TZ)
    server.history.append((now - timedelta(hours=30), 1.0))
    server.history.append((now - timedelta(hours=25), 2.0))
    server.history.append((now - timedelta(hours=23), 3.0))
    server.take_sample()
    assert [v for _, v in server.history] == [3.0, 21.5]


def test_take_sample_discards_entry_exactly_at_cutoff(fresh_reading):
    now = datetime.now(server.TZ).replace(second=0, microsecond=0)
    server.history.append((now - timedelta(hours=24), 1.0))
    server.history.append((now - timedelta(hours=24) + timedelta(minutes=1), 2.0))
    server.take_sample()
    assert [v for _, v in server.history] == [2.0, 21.5]


# ----------------------------------------------------------------- sampler

def test_sampler_waits_for_next_full_minute_then_samples(monkeypatch):
    sleeps, samples = [], []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopLoop

    monkeypatch.setattr(server.time, "sleep", fake_sleep)
    monkeypatch.setattr(server.time, "time", lambda: 1000.25)  # 40.25 s into the minute
    monkeypatch.setattr(server, "take_sample", lambda: samples.append(1))
    with pytest.raises(StopLoop):
        server.sampler()
    assert len(samples) == 1
    assert sleeps == [19.75, 19.75]


# ------------------------------------------------------------ read_serial

class FakeSerial:
    """Context manager that yields prepared lines, then fails like an unplugged cable."""

    def __init__(self, lines):
        self.lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def readline(self):
        if not self.lines:
            raise serial.SerialException("device disconnected")
        return self.lines.pop(0)


def test_read_serial_processes_lines_until_the_port_fails(monkeypatch):
    opened = {}

    def fake_open(port, baud, timeout):
        opened.update(port=port, baud=baud, timeout=timeout)
        return FakeSerial([b"DS18B20 sensors found: 1\n", b"\xff\xfeTemperature: 19.50 C\n"])

    monkeypatch.setattr(server.serial, "Serial", fake_open)
    with pytest.raises(serial.SerialException):
        server.read_serial()
    assert server.latest["temperature_c"] == 19.5
    assert opened == {"port": server.PORT, "baud": server.BAUD, "timeout": 2}


# ------------------------------------------------------------------ reader

def test_reader_retries_after_serial_and_os_errors(monkeypatch):
    errors = [serial.SerialException("gone"), OSError("no such device"), None, OSError("again")]
    sleeps = []

    def fake_read_serial():
        error = errors.pop(0)
        if error is not None:
            raise error

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise StopLoop

    monkeypatch.setattr(server, "read_serial", fake_read_serial)
    monkeypatch.setattr(server.time, "sleep", fake_sleep)
    with pytest.raises(StopLoop):
        server.reader()
    assert sleeps == [2, 2, 2]


# -------------------------------------------------- start_background / app

def test_start_background_starts_reader_and_sampler_as_daemon_threads(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, daemon):
            self.target, self.daemon = target, daemon

        def start(self):
            started.append((self.target, self.daemon))

    monkeypatch.setattr(server.threading, "Thread", FakeThread)
    server.start_background()
    assert started == [(server.reader, True), (server.sampler, True)]


def test_lifespan_starts_background_work_once(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "start_background", lambda: calls.append(1))
    with TestClient(server.app) as c:
        assert c.get("/health").status_code == 200
        assert calls == [1]


# ------------------------------------------------------- GET /temperature

def test_temperature_503_without_reading(client):
    r = client.get("/temperature")
    assert r.status_code == 503
    assert r.json() == {"detail": "No reading received yet"}


def test_temperature_503_when_reading_is_stale(client, stale_reading):
    r = client.get("/temperature")
    assert r.status_code == 503
    assert "older than" in r.json()["detail"]


def test_temperature_returns_latest_reading(client, fresh_reading):
    r = client.get("/temperature")
    assert r.status_code == 200
    assert r.json() == {"temperature_c": 21.5, "timestamp": "2026-01-01T12:00:00+00:00"}


# ----------------------------------------------------------- GET /health

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ------------------------------------------------------------ POST /measure

def test_measure_503_without_reading(client):
    r = client.post("/measure")
    assert r.status_code == 503
    assert len(server.history) == 0


def test_measure_503_when_reading_is_stale(client, stale_reading):
    r = client.post("/measure")
    assert r.status_code == 503
    assert len(server.history) == 0


def test_measure_stores_value_immediately(client, fresh_reading):
    r = client.post("/measure")
    assert r.status_code == 200
    body = r.json()
    assert body["temperature_c"] == 21.5
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", body["timestamp"])
    assert len(server.history) == 1


def test_measure_can_store_several_values_in_the_same_minute(client, fresh_reading):
    client.post("/measure")
    client.post("/measure")
    assert len(server.history) == 2


def test_measure_does_not_support_get(client):
    assert client.get("/measure").status_code == 405


# ------------------------------------------------------------ GET /history

def test_history_is_empty_initially(client):
    r = client.get("/history")
    assert r.status_code == 200
    assert r.json() == []


def test_history_lists_entries_oldest_first(client):
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=server.TZ)
    server.history.append((t0, 20.0))
    server.history.append((t0 + timedelta(minutes=1), 20.5))
    r = client.get("/history")
    assert r.json() == [
        {"timestamp": "2026-01-01T12:00:00+00:00", "temperature_c": 20.0},
        {"timestamp": "2026-01-01T12:01:00+00:00", "temperature_c": 20.5},
    ]


def test_history_contains_values_stored_by_measure(client, fresh_reading):
    client.post("/measure")
    assert [e["temperature_c"] for e in client.get("/history").json()] == [21.5]


# -------------------------------------------------------------------- GET /

def test_chart_page_is_served_as_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "<svg" in r.text
    assert "Measure now" in r.text


def test_chart_page_is_not_part_of_the_openapi_schema(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths
    assert set(paths) == {"/temperature", "/health", "/measure", "/history"}


def test_history_is_capped_at_twice_the_per_minute_rate():
    t0 = datetime(2026, 1, 1, tzinfo=server.TZ)
    for i in range(3000):
        server.history.append((t0 + timedelta(seconds=i), float(i)))
    assert len(server.history) == 24 * 60 * 2
    assert server.history[0][1] == 3000 - 24 * 60 * 2
