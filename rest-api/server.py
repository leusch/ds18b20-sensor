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

PATTERN = re.compile(r"Temperature: (-?\d+(?:\.\d+)?)")
latest = {"temperature_c": None, "measured_at": None, "mono": 0.0}

# History is kept in memory only: one value per minute, 24 hours. Nothing is written to disk.
HISTORY_HOURS = 24
history = deque(maxlen=HISTORY_HOURS * 60 * 2)  # (datetime, temperature); safety cap with room for manual values
history_lock = threading.Lock()


def is_fresh():
    """True if a reading exists and is not older than MAX_AGE_S."""
    return latest["temperature_c"] is not None and time.monotonic() - latest["mono"] <= MAX_AGE_S


def process_line(line):
    """Parse one line from the Arduino and remember the reading if it contains one."""
    m = PATTERN.search(line)
    if m:
        latest["temperature_c"] = float(m.group(1))
        latest["measured_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        latest["mono"] = time.monotonic()


def take_sample():
    """Append the current reading to the history and drop entries older than 24 hours."""
    if not is_fresh():
        return  # no fresh value (Arduino disconnected): leave a gap instead of a stale value
    now = datetime.now(TZ).replace(second=0, microsecond=0)
    with history_lock:
        history.append((now, latest["temperature_c"]))
        cutoff = now - timedelta(hours=HISTORY_HOURS)
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


@app.get("/temperature")
def temperature():
    if latest["temperature_c"] is None:
        raise HTTPException(503, "No reading received yet")
    if not is_fresh():
        raise HTTPException(503, f"Last reading older than {MAX_AGE_S:.0f} s - Arduino disconnected?")
    return {"temperature_c": latest["temperature_c"], "timestamp": latest["measured_at"]}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/measure")
def measure():
    """Store the current reading in the history right away, in addition to the per-minute sample."""
    if not is_fresh():
        raise HTTPException(503, "No current reading - Arduino disconnected?")
    now = datetime.now(TZ).replace(microsecond=0)
    with history_lock:
        history.append((now, latest["temperature_c"]))
    return {"timestamp": now.isoformat(timespec="seconds"), "temperature_c": latest["temperature_c"]}


@app.get("/history")
def get_history():
    with history_lock:
        return [{"timestamp": t.isoformat(timespec="seconds"), "temperature_c": v} for t, v in history]


@app.get("/", include_in_schema=False)
def chart():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
