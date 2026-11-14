import json
import os
import re
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import serial
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

PORT = os.environ.get("SERIAL_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("SERIAL_BAUD", "9600"))
MAX_AGE_S = float(os.environ.get("MAX_AGE_S", "15"))
TZ = ZoneInfo(os.environ.get("TZ", "UTC"))

# Display names by sensor address, read from a JSON file: {"<address>": "<name>", ...}.
# The file is re-read on every request, so edits apply without a restart.
NAMES_FILE = os.environ.get("SENSOR_NAMES_FILE", os.path.join(os.path.dirname(__file__), "config", "sensor_names.json"))

# One line per sensor, e.g. "Sensor 1 [28FF641E8D3C0A12]: 21.50 C". The address identifies the sensor.
PATTERN = re.compile(r"Sensor \d+ \[([0-9A-F]{16})\]: (-?\d+(?:\.\d+)?) C")

# History is kept in memory only: one value per minute, 24 hours. Nothing is written to disk.
HISTORY_HOURS = 24
HISTORY_CAP = HISTORY_HOURS * 60 * 2  # safety cap with room for manual values

# sensor id -> {"temperature_c", "measured_at", "mono", "history"}; history holds (datetime, temperature)
sensors = {}
sensors_lock = threading.Lock()


def is_fresh(sensor):
    """True if the sensor has a reading that is not older than MAX_AGE_S."""
    return time.monotonic() - sensor["mono"] <= MAX_AGE_S


def process_line(line):
    """Parse one line from the Arduino and remember the reading if it contains one."""
    m = PATTERN.search(line)
    if m:
        with sensors_lock:
            sensor = sensors.setdefault(m.group(1), {"history": deque(maxlen=HISTORY_CAP)})
            sensor["temperature_c"] = float(m.group(2))
            sensor["measured_at"] = datetime.now(TZ).isoformat(timespec="seconds")
            sensor["mono"] = time.monotonic()


def take_sample():
    """Append every current reading to its history and drop entries older than 24 hours."""
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    cutoff = now - timedelta(hours=HISTORY_HOURS)
    with sensors_lock:
        for sensor in sensors.values():
            if not is_fresh(sensor):
                continue  # no fresh value (sensor or Arduino disconnected): leave a gap instead of a stale value
            history = sensor["history"]
            history.append((now, sensor["temperature_c"]))
            # the entry appended above is newer than the cutoff, so this loop always stops
            while history[0][0] <= cutoff:
                history.popleft()  # permanently discard the oldest entries


def sampler():
    while True:
        # wait until the next full minute
        time.sleep(60 - time.time() % 60)
        take_sample()


def read_serial():
    with serial.Serial(PORT, BAUD, timeout=2) as s:
        while True:
            process_line(s.readline().decode(errors="ignore"))


def reader():
    while True:
        try:
            read_serial()
        except (serial.SerialException, OSError):
            time.sleep(2)  # cable unplugged or port gone: retry shortly


def start_background():
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()


@asynccontextmanager
async def lifespan(_app):
    start_background()
    yield


app = FastAPI(title="Temperature Sensor", lifespan=lifespan)


def load_names():
    """Sensor names from NAMES_FILE. A missing or broken file means no names, never an error."""
    try:
        with open(NAMES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def get_sensor(sensor_id):
    """Return the sensor or raise 404. Must be called with sensors_lock held."""
    if sensor_id not in sensors:
        raise HTTPException(404, f"Unknown sensor {sensor_id}")
    return sensors[sensor_id]


def default_sensor_id():
    """The sensor behind the single-sensor endpoints: the lowest address seen so far."""
    with sensors_lock:
        if not sensors:
            raise HTTPException(503, "No reading received yet")
        return min(sensors)


@app.get("/sensors")
def list_sensors():
    """All sensors seen since the service started. Offline sensors have no value, only their last timestamp."""
    names = load_names()
    with sensors_lock:
        return [
            {
                "id": sensor_id,
                "name": names.get(sensor_id),
                "online": is_fresh(s),
                "temperature_c": s["temperature_c"] if is_fresh(s) else None,
                "timestamp": s["measured_at"],
            }
            for sensor_id, s in sorted(sensors.items())
        ]


@app.get("/sensors/{sensor_id}/temperature")
def sensor_temperature(sensor_id: str):
    with sensors_lock:
        s = get_sensor(sensor_id)
        if not is_fresh(s):
            raise HTTPException(503, f"Last reading older than {MAX_AGE_S:.0f} s - sensor or Arduino disconnected?")
        return {"temperature_c": s["temperature_c"], "timestamp": s["measured_at"]}


@app.post("/sensors/{sensor_id}/measure")
def sensor_measure(sensor_id: str):
    """Store the sensor's current reading in its history right away, in addition to the per-minute sample."""
    with sensors_lock:
        s = get_sensor(sensor_id)
        if not is_fresh(s):
            raise HTTPException(503, "No current reading - sensor or Arduino disconnected?")
        now = datetime.now(TZ).replace(microsecond=0)
        s["history"].append((now, s["temperature_c"]))
        return {"timestamp": now.isoformat(timespec="seconds"), "temperature_c": s["temperature_c"]}


@app.get("/sensors/{sensor_id}/history")
def sensor_history(sensor_id: str):
    with sensors_lock:
        s = get_sensor(sensor_id)
        return [{"timestamp": t.isoformat(timespec="seconds"), "temperature_c": v} for t, v in s["history"]]


# The endpoints below predate multi-sensor support and answer for the default sensor.

@app.get("/temperature")
def temperature():
    return sensor_temperature(default_sensor_id())


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/measure")
def measure():
    return sensor_measure(default_sensor_id())


@app.get("/history")
def get_history():
    with sensors_lock:
        if not sensors:
            return []
    return sensor_history(default_sensor_id())


@app.get("/", include_in_schema=False)
def chart():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
