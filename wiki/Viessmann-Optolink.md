# Viessmann Heating Integration via Optolink

Local integration of a Viessmann oil boiler into a Prometheus/Grafana monitoring stack -- no cloud, no subscription, no Vitoconnect required.

The setup uses a USB Optolink adapter connected to the boiler's infrared interface, `vcontrold` as the protocol daemon, and a shell script exporter that writes Prometheus metrics via the Node Exporter textfile collector. A Flask API enables read/write access to heating parameters directly from Grafana.

## Architecture

```
Vitotronic KW2
      │ IR (880 nm)
USB Optolink adapter (ch341 chip)
      │ USB → /dev/ttyUSB0
vcontrold :3002 (TCP, localhost only)
      │
viessmann-exporter.sh (cron, every minute)
      │ textfile collector (.prom)
Node Exporter :9100
      │
Prometheus on Mnemosyne (60s scrape interval)
      │
Grafana
```

The control API runs alongside the exporter:

```
Grafana HTML panel
      │ HTTPS
viessmann.home (Caddy reverse proxy, token injected)
      │ HTTP :8081
Flask API (viessmann-api.py)
      │
vcontrold :3002
      │
Vitotronic KW2
```

---

## Hardware

**Adapter:** Viessmann USB Optolink cable (part number 7438374). The adapter plugs into the infrared port on the boiler's front panel -- no opening the unit, no electrical connection.

**Alternative:** Community-built optolink adapters (~50 €) work identically for KW protocol devices and are well-tested.

The adapter is recognized by the Linux kernel as `ch341-uart`:

```bash
dmesg | grep ch341
# Expected: ch341-uart converter now attached to ttyUSB0
```

---

## Protocol: KW vs. P

The Vitotronic 200 KW2 uses the older KW serial protocol, not the bidirectional P protocol of newer controllers. Key implications:

- Each data point is queried **sequentially** -- a full exporter run takes ~30 seconds
- Prometheus scrape interval is set to 60s accordingly
- Write commands require a threading lock to avoid conflicts with the exporter cron
- Timer schedules (`getTimerM1Mo`, etc.) are available but not queried by default -- they would push a single run to several minutes

---

## vcontrold installation

`vcontrold` is not in the Debian repositories -- build from source:

```bash
sudo apt install -y build-essential cmake libxml2-dev

cd ~
git clone https://github.com/openv/vcontrold.git
cd vcontrold
mkdir build && cd build

# Disable man pages (rst2man not required)
cmake .. -DMANPAGES=OFF
make
sudo make install
```

### Configuration

```bash
sudo mkdir -p /etc/vcontrold

# Use KW protocol configs -- not the 300/ directory (that's for P protocol)
sudo cp ~/vcontrold/xml/kw/vcontrold.xml /etc/vcontrold/
sudo cp ~/vcontrold/xml/kw/vito.xml /etc/vcontrold/
```

Edit `/etc/vcontrold/vcontrold.xml` -- two changes required:

```xml
<!-- Serial device -->
<tty>/dev/ttyUSB0</tty>

<!-- Device ID for Vitotronic 200 KW2 -->
<device ID="2098"/>
```

```bash
sudo touch /var/log/vcontrold.log
sudo chmod 666 /var/log/vcontrold.log
```

### systemd service

```ini
[Unit]
Description=vcontrold - Viessmann heating control daemon
After=network.target
After=dev-ttyUSB0.device
Requires=dev-ttyUSB0.device

[Service]
Type=simple
ExecStart=/usr/local/sbin/vcontrold -n -x /etc/vcontrold/vcontrold.xml -d /dev/ttyUSB0
Restart=on-failure
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable vcontrold
sudo systemctl start vcontrold
```

### Verify connection

```bash
# Identify device
vclient -h 127.0.0.1:3002 -c getDevType
# Expected: V200KW2 ID=2098 Protokoll:KW

# Read multiple values
vclient -h 127.0.0.1:3002 -c "getTempA,getTempKist,getBetriebArtM1"
```

The `-j` flag outputs all values numerically. Avoid it for enum types like `getBetriebArtM1` -- they return raw bytes (`0.0`) instead of readable strings (`H+WW`).

---

## Available data points (KW2)

| Command | Description |
|---|---|
| `getTempA` | Outside temperature |
| `getTempKist` / `getTempKsoll` | Boiler temperature actual / setpoint |
| `getTempWWist` / `getTempWWsoll` | Hot water actual / setpoint |
| `getTempVLsollM1` | Flow temperature setpoint heating circuit 1 |
| `getBrennerStatus` | Burner on/off (0/1) |
| `getBrennerStarts` | Total burner starts |
| `getBrennerStunden1` | Burner hours stage 1 |
| `getPumpeStatusM1` | Heating circuit pump status |
| `getPumpeStatusSp` | Storage pump status |
| `getBetriebArtM1` | Operating mode (WW / RED / NORM / H+WW / ABSCHALT) |
| `getNeigungM1` | Heating curve slope |
| `getNiveauM1` | Heating curve level |
| `getStatusStoerung` | Fault status (0=OK, 1=fault) |
| `getError0`–`getError9` | Error history (10 entries) |
| `getSystemTime` | Controller system time |

---

## Prometheus exporter

The exporter script (`viessmann-exporter.sh`) queries vcontrold and writes a `.prom` file for the Node Exporter textfile collector:

```bash
# Output location
/var/lib/node_exporter/textfile_collector/viessmann.prom

# Verify output
cat /var/lib/node_exporter/textfile_collector/viessmann.prom | grep -v "^#"
```

Cronjob (every minute):

```
* * * * * /usr/local/bin/viessmann-exporter.sh
```

Prometheus scrapes Node Exporter on Hephaestus at 60s intervals -- matching the ~30s exporter runtime with a safety margin.

Alert rules for the heating circuit pump (`pump-alerts.yml` in the monitoring stack) fire when `getPumpeStatusM1` reports the pump as inactive during heating mode. Alerts route through Alertmanager to a dedicated ntfy topic separate from general infrastructure alerts.

---

## Control API

`viessmann-api.py` is a Flask application that exposes read/write endpoints for heating parameters. It runs as a systemd service and is proxied through Caddy.

**The API is disabled by default.** Enable it only when needed:

```bash
# Enable
sudo systemctl enable viessmann-api
sudo systemctl start viessmann-api

# Disable (removes write access)
sudo systemctl stop viessmann-api
sudo systemctl disable viessmann-api
```

Monitoring via the textfile collector runs independently -- observability is never tied to write-access infrastructure.

### Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Status check |
| `/status` | GET | All current values in one request |
| `/log` | GET | Change history, newest first (max 200 entries) |
| `/set/neigung` | POST | Heating curve slope (0.2–3.5, step 0.1) |
| `/set/niveau` | POST | Heating curve level (-13–13, step 1) |
| `/set/betriebsart` | POST | Operating mode (ww / red / norm / hww / abschalt) |
| `/set/ww_soll` | POST | Hot water setpoint (40–60°C) |
| `/set/raum_nor_m1` | POST | Normal room setpoint M1 (15–22°C) |
| `/set/raum_red_m1` | POST | Reduced room setpoint M1 (10–20°C) |

```bash
# Test endpoints
curl -s https://viessmann.home/status | python3 -m json.tool

# Set heating curve slope
curl -s -X POST https://viessmann.home/set/neigung \
  -H "Content-Type: application/json" \
  -d '{"value": 1.3}'

# Set operating mode
curl -s -X POST https://viessmann.home/set/betriebsart \
  -H "Content-Type: application/json" \
  -d '{"mode": "hww"}'
```

### Concurrency and retry logic

vcontrold accepts only one connection at a time. The exporter cron holds the connection for ~30 seconds every minute.

- Set commands use a `threading.Lock` -- a second parallel request is immediately rejected with `"Another command is already running"`
- On failure, the API retries up to 3 times with 12s intervals (covers the exporter window)
- Read requests (`/status`) run without a lock -- they are non-critical

### Heating curve adjustment vs. direct mode switching

The KW2 state machine does not always respond predictably to remote `setBetriebArtM1` commands. The heating circuit pump may not activate without a reset cycle. For this reason the recommended approach is:

**Preferred:** adjust `Neigung` and `Niveau` -- these are passive register values the KW2 reads on its next cycle, no state transition required.

**Avoid in normal operation:** direct mode switching via `setBetriebArtM1`.

If a mode change is necessary and the pump does not start:

```bash
# Reset cycle
vclient -h 127.0.0.1:3002 -c "setBetriebArtM1 ABSCHALT"
sleep 10
vclient -h 127.0.0.1:3002 -c "setBetriebArtM1 H+WW"
```

### Token authentication

Caddy injects the API token for all requests to `viessmann.home` -- the token is never visible in the browser or in Grafana panel HTML:

```caddy
viessmann.home {
    tls internal
    reverse_proxy http://<hephaestus-ip>:8081 {
        header_up Authorization "Bearer {env.VIESSMANN_API_TOKEN}"
    }
}
```

---

## Grafana integration

Four HTML panels in the heating dashboard -- type **Text**, mode **HTML**.

| Panel | Function |
|---|---|
| Operating mode | 5 buttons for Betriebsart, active mode highlighted |
| Heating curve | Set Neigung and Niveau |
| Temperature setpoints | Hot water setpoint, room setpoints normal/reduced |
| Change log | Change history from `/log`, auto-refresh 60s |

Required Grafana environment variable:

```yaml
GF_PANELS_DISABLE_SANITIZE_HTML=true
```

All panels call `/status` on load to populate input fields with current values. The set buttons are disabled for the duration of each request to prevent duplicate submissions.

After setting a value, the API reads it back from the controller (`vclient_get`) -- the displayed value is the one actually confirmed by the KW2.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `getDevType` returns `UNKNOWN` | Wrong device ID | Check `<device ID="2098"/>` in vcontrold.xml |
| `TTY Net: No connection to 127.0.0.1:3002` | vcontrold not running | `sudo systemctl start vcontrold` |
| vcontrold won't start after reboot | USB adapter not detected in time | Add `After=dev-ttyUSB0.device` + `Requires=dev-ttyUSB0.device` to unit file |
| Metrics empty, `bc: command not found` | bc not installed | `sudo apt install bc` |
| `Unauthorized` in Grafana panel | Caddy not injecting token | `docker exec caddy env \| grep VIESSMANN` |
| `Another command is already running` | Two set requests in parallel | Wait -- only one set command at a time |
| `vclient error` when setting value | Exporter cron holds the connection | Retry logic handles this automatically (3× with 12s) |
| `-0.1°C` for flow setpoint | Heating circuit not currently active | Normal behavior during hot water operation |
| HTTP 400 from API | Value out of range | Neigung: 0.2–3.5, Niveau: -13–13, WW: 40–60°C |
