# Temperature Sensor

A DS18B20 temperature sensor on an Arduino Nano, exposed over HTTP.

```
DS18B20 --> Arduino Nano --USB serial--> Docker container (FastAPI) --> JSON API + chart page
```

- The Arduino prints one reading per second on the serial port.
- The API service reads it, stores one value per minute for the last 24 hours **in memory only**, and serves it over HTTP.
- Nothing is written to disk. Restarting the container clears the history.

## Repository layout

```
firmware/      Arduino sketch (firmware.ino)
rest-api/      FastAPI service, chart page, Dockerfile, docker-compose.yml
```

## Hardware

| DS18B20 wire (typical colors) | Connect to |
|---|---|
| Red (VCC) | 5V |
| Black (GND) | GND |
| Yellow (data) | D2 |

Add a **4.7 kΩ pull-up resistor between the data line (D2) and 5V**. Without it the sensor is not detected. Wire colors are not standardized; check your sensor's datasheet. Reversed power makes the sensor hot and destroys it, so wire it with USB unplugged.

## Install

### 1. Flash the firmware

Requires [arduino-cli](https://arduino.github.io/arduino-cli/) on the machine the Arduino is plugged into, and permission to use the serial port (on Linux: be in the `dialout` group).

```bash
arduino-cli core install arduino:avr
arduino-cli lib install OneWire DallasTemperature
arduino-cli compile --fqbn arduino:avr:nano:cpu=atmega328old firmware
arduino-cli upload -p /dev/ttyUSB0 --fqbn arduino:avr:nano:cpu=atmega328old firmware
```

Nanos with the old bootloader need `cpu=atmega328old`. If the upload fails with `programmer is not responding`, try `cpu=atmega328` instead.

Check the output (9600 baud):

```
DS18B20 sensors found: 1
Temperature: 21.50 C
```

`sensors found: 0` or `No sensor detected` means a wiring or pull-up problem.

### 2. Run the API with Docker

Connect the Arduino to the host that runs Docker, then:

```bash
cd rest-api
ls /dev/serial/by-id/          # find your device
```

Put your device path into `docker-compose.yml` under `devices:` (the left side of the mapping), then:

```bash
docker compose up -d --build
curl http://localhost:8000/temperature
```

Configuration:

| Setting | Where | Default | Meaning |
|---|---|---|---|
| `SERIAL_GID` | shell / `.env` | `46` | Group ID of the serial device on the host (`stat -L -c %g <device>`). The container runs as a non-root user and needs this group. |
| `TZ` | shell / `.env` | `UTC` | Time zone of the timestamps in API responses. |
| `SERIAL_PORT` | `environment:` | `/dev/ttyUSB0` | Serial port inside the container. |
| `SERIAL_BAUD` | `environment:` | `9600` | Must match the firmware. |
| `MAX_AGE_S` | `environment:` | `15` | A reading older than this counts as stale (Arduino disconnected). |

The service listens on port **8000**. Change the left side of `ports:` if it is taken.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Chart page (HTML) with current value, min/max and a 24 h chart. Works on mobile and desktop. |
| `GET` | `/temperature` | Latest reading. |
| `GET` | `/history` | Stored values of the last 24 h, oldest first. |
| `POST` | `/measure` | Store the current reading in the history immediately, in addition to the per-minute sample. |
| `GET` | `/health` | Liveness check, always `{"ok": true}` while the service runs. |
| `GET` | `/docs` | Interactive API documentation (Swagger UI). |

`GET /temperature`

```json
{"temperature_c": 21.5, "timestamp": "2026-01-01T12:00:01+00:00"}
```

`GET /history`

```json
[
  {"timestamp": "2026-01-01T11:59:00+00:00", "temperature_c": 21.44},
  {"timestamp": "2026-01-01T12:00:00+00:00", "temperature_c": 21.5}
]
```

`POST /measure` returns the stored entry in the same format as one history item.

Errors: `/temperature` and `/measure` return **503** with `{"detail": "..."}` if no reading has been received yet or the last one is older than `MAX_AGE_S`.

History behavior:

- One value is stored at every full minute, timestamped to that minute.
- While the Arduino is disconnected nothing is stored, which leaves a gap. Old values are never repeated.
- Entries older than 24 hours are permanently deleted.

## Use it from other software

Replace `<host>` with the address of the machine running the container.

**curl / shell**

```bash
curl -s http://<host>:8000/temperature
curl -s http://<host>:8000/temperature | python3 -c "import sys,json; print(json.load(sys.stdin)['temperature_c'])"
```

**Python**

```python
import requests

r = requests.get("http://<host>:8000/temperature", timeout=5)
r.raise_for_status()
print(r.json()["temperature_c"])
```

**JavaScript (browser or Node)**

```js
const res = await fetch("http://<host>:8000/temperature");
const { temperature_c, timestamp } = await res.json();
```

Calling the API from a web page on another origin is blocked by the browser, because the service does not send CORS headers. Proxy it, or serve your page from the same host.

**Home Assistant** (`configuration.yaml`)

```yaml
sensor:
  - platform: rest
    name: Room temperature
    resource: http://<host>:8000/temperature
    value_template: "{{ value_json.temperature_c }}"
    unit_of_measurement: "°C"
    device_class: temperature
    scan_interval: 60
```

**Embed the chart**

```html
<iframe src="http://<host>:8000/" style="border:0;width:100%;height:520px"></iframe>
```

## Tests

Requires Python 3.10+, `python3-venv` and system tzdata. Linux/macOS (WSL on Windows). No Arduino or Docker needed.

```bash
cd rest-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

.venv/bin/python -m pytest              # unit tests, fails below 100% coverage
PYTHON=.venv/bin/python ./check.sh      # unit tests + mutation tests (mutmut)
```

Only `server.py` is tested and mutated, not `index.html`.
