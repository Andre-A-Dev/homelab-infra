# Hephaestus

Raspberry Pi 3B attached to a **Viessmann Vitotronic 200 KW2** gas heating
controller via a KW2 USB-to-serial adapter. Its sole purpose is heating
monitoring and remote control — it is not a general-purpose Docker host.

## Architecture

```
Vitotronic 200 KW2
      │  KW2 serial
  /dev/ttyUSB0
      │
  vcontrold  ────────────────────────────────────────────────────────────────
  127.0.0.1:3002   (one connection at a time — serial protocol limitation)
      │
      ├── vclient (CLI)
      │       │
      │       ├── viessmann-exporter.sh  (cron, every ~30 s)
      │       │       └── writes /var/lib/node_exporter/textfile_collector/viessmann.prom
      │       │
      │       ├── viessmann-api.py  (systemd, port 8081)
      │       │       └── HTTP API called by Grafana panels to SET parameters
      │       │
      │       └── hephaestus-display.sh  (systemd, /dev/tty1 → fb1)
      │               └── 3-page ANSI dashboard on the 3.5" TFT
      │
  node-exporter  (Docker, port 9100)
          └── scrapes textfile_collector/ for Prometheus
```

## Components

| File | Role |
|---|---|
| `systemd/vcontrold.service` | Starts vcontrold, bound to `/dev/ttyUSB0` via `Requires=` |
| `vcontrold/vcontrold.xml` | Device ID `2098` (Vitotronic 200 KW2), port 3002, serial config |
| `vcontrold/vito.xml` | Command definitions for the KW2 protocol |
| `scripts/viessmann-exporter.sh` | Reads all metrics in one vclient call, emits Prometheus textfile |
| `scripts/viessmann-api.py` | Flask API with bearer-token auth for write operations from Grafana |
| `scripts/hephaestus-display.sh` | Framebuffer dashboard: Heating → Network → Services, 10 s/page |
| `systemd/viessmann-api.service` | Runs the Flask API as `youruser`, `Requires=vcontrold.service` |
| `systemd/hephaestus-display.service` | Runs the display loop as root (needs fb/tty access) |
| `monitoring/docker-compose.yml` | node-exporter with `textfile_collector` volume |

## Setup

### vcontrold

Install from source or package, then deploy config:

```bash
sudo cp vcontrold/vcontrold.xml /etc/vcontrold/vcontrold.xml
sudo cp vcontrold/vito.xml      /etc/vcontrold/vito.xml
sudo cp systemd/vcontrold.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vcontrold
```

Verify: `vclient -h 127.0.0.1:3002 -c getTempA` should return the outdoor temperature.

### Prometheus exporter (cron)

```bash
sudo cp scripts/viessmann-exporter.sh /usr/local/bin/
sudo mkdir -p /var/lib/node_exporter/textfile_collector
# Add to root crontab:
# * * * * * /usr/local/bin/viessmann-exporter.sh
```

### Control API

Create `scripts/.env`:

```
VIESSMANN_API_TOKEN=<secret>
```

```bash
sudo cp systemd/viessmann-api.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now viessmann-api
```

API runs on port `8081`. All write endpoints require `Authorization: Bearer <token>`.

| Endpoint | Method | Body |
|---|---|---|
| `/status` | GET | — returns all current values |
| `/health` | GET | — liveness check |
| `/log` | GET | — last 200 control actions |
| `/set/neigung` | POST | `{"value": 1.3}` — heating curve slope (0.2–3.5) |
| `/set/niveau` | POST | `{"value": 3.0}` — heating curve level (−13–13) |
| `/set/betriebsart` | POST | `{"mode": "hww"}` — operating mode |
| `/set/ww_soll` | POST | `{"value": 55.0}` — hot water setpoint (40–60 °C) |
| `/set/raum_nor_m1` | POST | `{"value": 20.0}` — normal room setpoint (15–22 °C) |
| `/set/raum_red_m1` | POST | `{"value": 16.0}` — reduced room setpoint (10–20 °C) |

Valid `betriebsart` modes: `ww`, `red`, `norm`, `hww`, `abschalt`.

### TFT display

```bash
sudo cp scripts/hephaestus-display.sh /usr/local/bin/
sudo cp systemd/hephaestus-display.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now hephaestus-display
```

Requires `con2fbmap` (part of `fbset`) to map tty1 to fb1.

### node-exporter

```bash
cd monitoring
docker compose up -d
```

## Key constraints

- **vcontrold is single-connection.** The KW2 serial protocol cannot multiplex.
  The API uses a threading lock and retries with 12 s backoff to avoid collisions
  with the exporter cron. Do not add additional vclient callers without
  coordinating with this lock.
- **`-j` flag converts enums to raw bytes.** `getBetriebArtM1` and
  `getBetriebArtM2` must be fetched without `-j` to get the human-readable
  string (`H+WW`, `NORM`, etc.). The API handles this automatically.
- **KW2 only accepts 0.1-step values.** Neigung and Niveau are rounded to one
  decimal before being sent.
- **vcontrold must be running for the API to start.** The systemd unit uses
  `Requires=vcontrold.service`.
