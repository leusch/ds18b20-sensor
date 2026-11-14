import json
import os
import re
from datetime import datetime, timedelta

import pytest
import serial
from fastapi.testclient import TestClient

import server
from conftest import A, B, add_sensor

DEFAULT_NAMES_FILE = server.NAMES_FILE  # captured before the autouse fixture redirects it


class StopLoop(Exception):
    """Raised by fakes to leave an endless loop under test."""


# ---------------------------------------------------------------- is_fresh

def test_is_fresh_with_recent_reading():
    assert server.is_fresh(add_sensor(A)) is True


def test_is_fresh_includes_the_exact_age_limit(monkeypatch):
    sensor = {"mono": 100.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0 + server.MAX_AGE_S)
    assert server.is_fresh(sensor) is True
    monkeypatch.setattr(server.time, "monotonic", lambda: 100.0 + server.MAX_AGE_S + 0.001)
    assert server.is_fresh(sensor) is False


def test_is_fresh_with_old_reading():
    assert server.is_fresh(add_sensor(A, age=server.MAX_AGE_S + 1)) is False


# ------------------------------------------------------------ process_line

@pytest.mark.parametrize(
    "value, expected",
    [("21.50", 21.5), ("-3.25", -3.25), ("20", 20.0)],
)
def test_process_line_stores_reading_under_the_sensor_address(value, expected):
    server.process_line(f"Sensor 1 [{A}]: {value} C\n")
    sensor = server.sensors[A]
    assert sensor["temperature_c"] == expected
    assert sensor["measured_at"] is not None
    assert len(sensor["history"]) == 0
    assert sensor["history"].maxlen == server.HISTORY_CAP
    assert server.is_fresh(sensor) is True


def test_process_line_tracks_each_sensor_separately():
    server.process_line(f"Sensor 1 [{A}]: 21.50 C\n")
    server.process_line(f"Sensor 2 [{B}]: 18.00 C\n")
    server.process_line(f"Sensor 1 [{A}]: 22.00 C\n")
    assert {k: v["temperature_c"] for k, v in server.sensors.items()} == {A: 22.0, B: 18.0}


def test_process_line_keeps_the_history_of_a_known_sensor():
    history = add_sensor(A)["history"]
    history.append((datetime.now(server.TZ), 1.0))
    server.process_line(f"Sensor 1 [{A}]: 21.50 C\n")
    assert server.sensors[A]["history"] is history
    assert len(history) == 1


def test_process_line_timestamp_carries_the_configured_time_zone():
    server.process_line(f"Sensor 1 [{A}]: 21.50 C\n")
    measured_at = datetime.fromisoformat(server.sensors[A]["measured_at"])
    assert measured_at.utcoffset() == datetime.now(server.TZ).utcoffset()


@pytest.mark.parametrize(
    "line",
    [
        "DS18B20 sensors found: 2\n",
        "No sensor detected - check wiring and pull-up resistor\n",
        f"Sensor 1 [{A}]: read error\n",
        f"Sensor 1 [{A}]: abc C\n",
        "Sensor 1 [28FF]: 21.50 C\n",
        f"Sensor 1 [{A.lower()}]: 21.50 C\n",
        "Sensor 1 [28FF641E8D3C0A12FF]: 21.50 C\n",
        "Temperature: 21.50 C\n",
        "",
    ],
)
def test_process_line_ignores_other_lines(line):
    server.process_line(line)
    assert server.sensors == {}


def test_process_line_keeps_previous_reading_on_garbage(fresh_reading):
    server.process_line("garbage\n")
    assert server.sensors[A]["temperature_c"] == 21.5


# ------------------------------------------------------------- take_sample

def test_take_sample_without_sensors_does_nothing():
    server.take_sample()
    assert server.sensors == {}


def test_take_sample_with_stale_reading_leaves_gap(stale_reading):
    server.take_sample()
    assert len(server.sensors[A]["history"]) == 0


def test_take_sample_stores_value_rounded_to_the_minute(fresh_reading):
    server.take_sample()
    history = server.sensors[A]["history"]
    assert len(history) == 1
    ts, value = history[0]
    assert value == 21.5
    assert ts.second == 0 and ts.microsecond == 0


def test_take_sample_stores_every_fresh_sensor_and_skips_stale_ones():
    add_sensor(A, 21.5)
    add_sensor(B, 18.0, age=server.MAX_AGE_S + 1)
    add_sensor("28CC000000000002", 5.0)
    server.take_sample()
    assert [v for _, v in server.sensors[A]["history"]] == [21.5]
    assert len(server.sensors[B]["history"]) == 0
    assert [v for _, v in server.sensors["28CC000000000002"]["history"]] == [5.0]


def test_take_sample_keeps_recent_entries(fresh_reading):
    now = datetime.now(server.TZ)
    server.sensors[A]["history"].append((now - timedelta(hours=23), 10.0))
    server.take_sample()
    assert [v for _, v in server.sensors[A]["history"]] == [10.0, 21.5]


def test_take_sample_discards_entries_older_than_24_hours(fresh_reading):
    now = datetime.now(server.TZ)
    history = server.sensors[A]["history"]
    history.append((now - timedelta(hours=30), 1.0))
    history.append((now - timedelta(hours=25), 2.0))
    history.append((now - timedelta(hours=23), 3.0))
    server.take_sample()
    assert [v for _, v in history] == [3.0, 21.5]


def test_take_sample_discards_entry_exactly_at_cutoff(fresh_reading):
    now = datetime.now(server.TZ).replace(second=0, microsecond=0)
    history = server.sensors[A]["history"]
    history.append((now - timedelta(hours=24), 1.0))
    history.append((now - timedelta(hours=24) + timedelta(minutes=1), 2.0))
    server.take_sample()
    assert [v for _, v in history] == [2.0, 21.5]


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
        return FakeSerial([b"DS18B20 sensors found: 1\n", b"\xff\xfeSensor 1 [" + A.encode() + b"]: 19.50 C\n"])

    monkeypatch.setattr(server.serial, "Serial", fake_open)
    with pytest.raises(serial.SerialException):
        server.read_serial()
    assert server.sensors[A]["temperature_c"] == 19.5
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
    assert r.json() == {"detail": "Last reading older than 15 s - sensor or Arduino disconnected?"}


def test_temperature_returns_latest_reading(client, fresh_reading):
    r = client.get("/temperature")
    assert r.status_code == 200
    assert r.json() == {"temperature_c": 21.5, "timestamp": "2026-01-01T12:00:00+00:00"}


def test_temperature_answers_for_the_lowest_sensor_address(client):
    add_sensor(B, 18.0)
    add_sensor(A, 21.5)
    assert B < A
    assert client.get("/temperature").json()["temperature_c"] == 18.0


# ----------------------------------------------------------- GET /health

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ------------------------------------------------------------ POST /measure

def test_measure_503_without_reading(client):
    r = client.post("/measure")
    assert r.status_code == 503
    assert r.json() == {"detail": "No reading received yet"}


def test_measure_503_when_reading_is_stale(client, stale_reading):
    r = client.post("/measure")
    assert r.status_code == 503
    assert r.json() == {"detail": "No current reading - sensor or Arduino disconnected?"}
    assert len(server.sensors[A]["history"]) == 0


def test_measure_stores_value_immediately(client, fresh_reading):
    r = client.post("/measure")
    assert r.status_code == 200
    body = r.json()
    assert body["temperature_c"] == 21.5
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", body["timestamp"])
    assert len(server.sensors[A]["history"]) == 1


def test_measure_can_store_several_values_in_the_same_minute(client, fresh_reading):
    client.post("/measure")
    client.post("/measure")
    assert len(server.sensors[A]["history"]) == 2


def test_measure_does_not_support_get(client):
    assert client.get("/measure").status_code == 405


# ------------------------------------------------------------ GET /history

def test_history_is_empty_without_sensors(client):
    r = client.get("/history")
    assert r.status_code == 200
    assert r.json() == []


def test_history_is_empty_initially(client, fresh_reading):
    assert client.get("/history").json() == []


def test_history_lists_entries_oldest_first(client):
    history = add_sensor(A)["history"]
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=server.TZ)
    history.append((t0, 20.0))
    history.append((t0 + timedelta(minutes=1), 20.5))
    r = client.get("/history")
    assert r.json() == [
        {"timestamp": "2026-01-01T12:00:00+00:00", "temperature_c": 20.0},
        {"timestamp": "2026-01-01T12:01:00+00:00", "temperature_c": 20.5},
    ]


def test_history_contains_values_stored_by_measure(client, fresh_reading):
    client.post("/measure")
    assert [e["temperature_c"] for e in client.get("/history").json()] == [21.5]


def test_history_answers_for_the_lowest_sensor_address(client):
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=server.TZ)
    add_sensor(A)["history"].append((t0, 1.0))
    add_sensor(B)["history"].append((t0, 2.0))
    assert [e["temperature_c"] for e in client.get("/history").json()] == [2.0]


# ------------------------------------------------------------- GET /sensors

def test_sensors_is_empty_initially(client):
    r = client.get("/sensors")
    assert r.status_code == 200
    assert r.json() == []


def test_sensors_lists_all_sensors_sorted_by_address(client):
    add_sensor(A, 21.5, measured_at="2026-01-01T12:00:00+00:00")
    add_sensor(B, 18.0, measured_at="2026-01-01T12:00:05+00:00")
    assert client.get("/sensors").json() == [
        {"id": B, "name": None, "online": True, "temperature_c": 18.0, "timestamp": "2026-01-01T12:00:05+00:00"},
        {"id": A, "name": None, "online": True, "temperature_c": 21.5, "timestamp": "2026-01-01T12:00:00+00:00"},
    ]


def test_sensors_shows_offline_sensor_without_value(client):
    add_sensor(A, 21.5)
    add_sensor(B, 18.0, age=server.MAX_AGE_S + 1, measured_at="2026-01-01T11:59:00+00:00")
    by_id = {s["id"]: s for s in client.get("/sensors").json()}
    assert by_id[A]["online"] is True and by_id[A]["temperature_c"] == 21.5
    assert by_id[B] == {"id": B, "name": None, "online": False, "temperature_c": None, "timestamp": "2026-01-01T11:59:00+00:00"}


def write_names(content):
    with open(server.NAMES_FILE, "w", encoding="utf-8") as f:
        f.write(content)


def names_by_id(client):
    return {s["id"]: s["name"] for s in client.get("/sensors").json()}


def test_sensors_shows_the_names_from_the_file(client):
    write_names('{"%s": "Bedroom/Office", "%s": "Living room"}' % (A, B))
    add_sensor(A)
    add_sensor(B)
    add_sensor("28CC000000000002")
    assert names_by_id(client) == {A: "Bedroom/Office", B: "Living room", "28CC000000000002": None}


def test_names_file_is_reread_on_every_request(client):
    add_sensor(A)
    write_names('{"%s": "Old"}' % A)
    assert names_by_id(client) == {A: "Old"}
    write_names('{"%s": "New"}' % A)
    assert names_by_id(client) == {A: "New"}


def test_load_names_without_file_is_empty():
    assert server.load_names() == {}


@pytest.mark.parametrize("content", ["not json", "", '["a", "b"]', '"text"', "null"])
def test_load_names_ignores_broken_file(content):
    write_names(content)
    assert server.load_names() == {}


def test_load_names_reads_the_file_as_utf8_regardless_of_locale(monkeypatch):
    write_names('{"%s": "Café é"}' % A)
    seen = {}
    real_open = open

    def spy(path, *args, **kwargs):
        seen.update(kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(server, "open", spy, raising=False)
    assert server.load_names() == {A: "Café é"}
    assert seen == {"encoding": "utf-8"}


def test_load_names_keeps_only_text_values():
    write_names('{"%s": "Living room", "%s": 5, "28CC000000000002": null}' % (A, B))
    assert server.load_names() == {A: "Living room"}


def test_sensors_works_with_a_broken_names_file(client):
    write_names("{ oops")
    add_sensor(A)
    assert names_by_id(client) == {A: None}


def test_names_file_defaults_to_config_directory_next_to_the_server():
    assert DEFAULT_NAMES_FILE == os.path.join(os.path.dirname(os.path.abspath(server.__file__)), "config", "sensor_names.json")


def test_shipped_names_file_is_valid():
    with open(DEFAULT_NAMES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    assert data
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in data.items())


# ------------------------------------------ GET /sensors/{id}/temperature

def test_sensor_temperature_404_for_unknown_sensor(client, fresh_reading):
    r = client.get(f"/sensors/{B}/temperature")
    assert r.status_code == 404
    assert r.json() == {"detail": f"Unknown sensor {B}"}


def test_sensor_temperature_503_when_reading_is_stale(client):
    add_sensor(A)
    add_sensor(B, age=server.MAX_AGE_S + 1)
    assert client.get(f"/sensors/{A}/temperature").status_code == 200
    r = client.get(f"/sensors/{B}/temperature")
    assert r.status_code == 503
    assert "older than" in r.json()["detail"]


def test_sensor_temperature_returns_the_requested_sensor(client):
    add_sensor(A, 21.5)
    add_sensor(B, 18.0)
    r = client.get(f"/sensors/{B}/temperature")
    assert r.json() == {"temperature_c": 18.0, "timestamp": "2026-01-01T12:00:00+00:00"}


# ------------------------------------------------ GET /sensors/{id}/history

def test_sensor_history_404_for_unknown_sensor(client):
    assert client.get(f"/sensors/{A}/history").status_code == 404


def test_sensor_history_returns_only_that_sensors_entries(client):
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=server.TZ)
    add_sensor(A)["history"].append((t0, 1.0))
    add_sensor(B)["history"].append((t0, 2.0))
    assert client.get(f"/sensors/{A}/history").json() == [
        {"timestamp": "2026-01-01T12:00:00+00:00", "temperature_c": 1.0}
    ]
    assert [e["temperature_c"] for e in client.get(f"/sensors/{B}/history").json()] == [2.0]


# ----------------------------------------------- POST /sensors/{id}/measure

def test_sensor_measure_404_for_unknown_sensor(client):
    assert client.post(f"/sensors/{A}/measure").status_code == 404


def test_sensor_measure_503_when_reading_is_stale(client):
    add_sensor(A, age=server.MAX_AGE_S + 1)
    assert client.post(f"/sensors/{A}/measure").status_code == 503
    assert len(server.sensors[A]["history"]) == 0


def test_sensor_measure_stores_value_for_that_sensor_only(client):
    add_sensor(A, 21.5)
    add_sensor(B, 18.0)
    r = client.post(f"/sensors/{B}/measure")
    assert r.status_code == 200
    assert r.json()["temperature_c"] == 18.0
    assert [v for _, v in server.sensors[B]["history"]] == [18.0]
    assert len(server.sensors[A]["history"]) == 0


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
    assert set(paths) == {
        "/temperature", "/health", "/measure", "/history",
        "/sensors", "/sensors/{sensor_id}/temperature",
        "/sensors/{sensor_id}/history", "/sensors/{sensor_id}/measure",
    }


def test_history_is_capped_at_twice_the_per_minute_rate():
    history = add_sensor(A)["history"]
    t0 = datetime(2026, 1, 1, tzinfo=server.TZ)
    for i in range(3000):
        history.append((t0 + timedelta(seconds=i), float(i)))
    assert len(history) == 24 * 60 * 2
    assert history[0][1] == 3000 - 24 * 60 * 2
