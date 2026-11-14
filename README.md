# Temperature Sensor

One or more DS18B20 temperature sensors on an Arduino Nano, exposed over HTTP.

```
DS18B20 --> Arduino Nano --USB serial--> Docker container (FastAPI) --> JSON API + chart page
```

- All DS18B20 sensors share one 1-Wire data line (D2). The Arduino rescans the bus every second, so sensors you plug in or remove while it runs are picked up. There is no fixed sensor limit; in practice cable length and supply power are the limit.
- The Arduino prints one reading per sensor and second on the serial port. Each sensor is identified by its unique 64-bit address.
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
DS18B20 sensors found: 2
Sensor 1 [28FF641E8D3C0A12]: 21.50 C
Sensor 2 [28AA000000000001]: 18.00 C
```

`sensors found: 0` or `No sensor detected` means a wiring or pull-up problem. `read error` for a single sensor means that sensor has a bad connection; the others keep working. Sensor numbers can change when the bus order changes, the address in brackets does not.

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
| `SENSOR_NAMES_FILE` | `environment:` | `/config/sensor_names.json` | Path of the names file inside the container. |
| `SERIAL_BAUD` | `environment:` | `9600` | Must match the firmware. |
| `MAX_AGE_S` | `environment:` | `15` | A reading older than this counts as stale (Arduino disconnected). |

The service listens on port **8000**. Change the left side of `ports:` if it is taken.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Chart page (HTML) with current value and min/max per sensor and a 24 h chart with one line per sensor. Works on mobile and desktop. |
| `GET` | `/sensors` | All sensors seen since the service started, with current value and online state. |
| `GET` | `/sensors/{id}/temperature` | Latest reading of one sensor. |
| `GET` | `/sensors/{id}/history` | Stored values of one sensor for the last 24 h, oldest first. |
| `POST` | `/sensors/{id}/measure` | Store that sensor's current reading in its history immediately, in addition to the per-minute sample. |
| `GET` | `/temperature` | Like `/sensors/{id}/temperature` for the **default sensor** (see below). |
| `GET` | `/history` | Like `/sensors/{id}/history` for the default sensor. |
| `POST` | `/measure` | Like `/sensors/{id}/measure` for the default sensor. |
| `GET` | `/health` | Liveness check, always `{"ok": true}` while the service runs. |
| `GET` | `/docs` | Interactive API documentation (Swagger UI). |

`{id}` is the sensor address from the serial output, e.g. `28FF641E8D3C0A12`. An unknown id returns **404**.

The default sensor is the one with the lowest address among all sensors seen so far. The three endpoints without `/sensors/...` exist for single-sensor setups and older clients; with several sensors use the `/sensors/...` endpoints.

`GET /sensors`

```json
[
  {"id": "28AA000000000001", "name": null, "online": true,  "temperature_c": 18.0, "timestamp": "2026-01-01T12:00:01+00:00"},
  {"id": "28B87D76E0013C86", "name": "Bedroom/Office", "online": false, "temperature_c": null, "timestamp": "2026-01-01T11:58:40+00:00"}
]
```

`name` comes from `rest-api/config/sensor_names.json` (address → display name, see below). Sensors not listed there have `name: null` and show up as `Sensor …<last 4 digits>` on the chart page.

An offline sensor (no reading for `MAX_AGE_S`) has `temperature_c: null`; `timestamp` is its last reading.

`GET /sensors/{id}/temperature` and `GET /temperature`

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

Errors: the temperature and measure endpoints return **503** with `{"detail": "..."}` if no reading has been received yet or the last one of that sensor is older than `MAX_AGE_S`.

History behavior:

- One value per sensor is stored at every full minute, timestamped to that minute.
- While a sensor or the Arduino is disconnected nothing is stored for it, which leaves a gap. Old values are never repeated.
- Entries older than 24 hours are permanently deleted.

## Sensor names

Names live in `rest-api/config/sensor_names.json`:

```json
{
  "28B87D76E0013C86": "Bedroom/Office",
  "28C19676E0013C22": "Living room"
}
```

`docker-compose.yml` mounts the `config/` directory read-only into the container. The file is re-read on every `GET /sensors`, so after editing it **no restart or rebuild is needed**; reload the chart page. A missing or invalid file simply means no names. Find the address of a new sensor in the serial output or in `GET /sensors`.

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
