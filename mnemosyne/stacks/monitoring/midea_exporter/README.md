# Midea Exporter

A small Prometheus exporter for Midea WiFi air conditioners (device type `0xAC`,
e.g. the Midea Portasplit) registered in the MSmartHome app. It reads device
**state** over the local LAN protocol via [`msmart-ng`](https://github.com/mill1000/midea-msmart)
and exposes it as Prometheus metrics.

After a one-time cloud handshake to fetch credentials, the exporter runs
**fully local** — the device can be firewalled off the internet entirely.

## How it works

Midea V3 devices need a `token` + `key` pair to authenticate locally. Two
quirks make manual setup painful:

1. **Two credential pairs.** The device exposes a `little`- and a `big`-endian
   pair. Only one answers state queries; the other authenticates fine but then
   ignores every request.
2. **Exact device id required.** A wrong id passes authentication but every
   subsequent query times out.

To avoid both, the exporter uses `Discover.discover_single()` on first run. It
tries the pairs, keeps the one that responds, and returns the correct id. The
result is cached to disk, so the cloud is contacted **once**:

```
cold start (no cache)  -> discover_single() -> write device_creds.json
every start after      -> load device_creds.json -> local auth, no cloud
```

## Prerequisites

- Device must be a type `0xAC` air conditioner with the WiFi module, registered
  in MSmartHome / NetHome Plus.
- Internet access for the device **during the first run only** (to fetch
  credentials). Block it afterwards if you like — the cached creds keep working.
- A persistent volume for the credential cache.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MIDEA_IP` | *(required)* | Device IP (reserve a static lease) |
| `MIDEA_REGION` | `DE` | Built-in cloud credential region (`DE`/`KR`/`US`) |
| `MIDEA_ACCOUNT_EMAIL` | *(empty)* | Optional own MSmartHome login, if built-in creds fail |
| `MIDEA_ACCOUNT_PASSWORD` | *(empty)* | Password for the above |
| `MIDEA_CACHE` | `/data/device_creds.json` | Cache path (mount a volume here) |
| `MIDEA_DEVICE_NAME` | `portasplit` | Value of the `device` metric label |
| `MIDEA_ACCOUNT` | `home` | Value of the `account` metric label |
| `POLL_INTERVAL` | `30` | Seconds between polls |
| `EXPORTER_PORT` | `9116` | Metrics port |
| `OP_TIMEOUT` | `15` | Per-call timeout (seconds) |
| `REBUILD_BACKOFF` | `4` | Pause before reconnecting (lets the device free its slot) |

## Deployment

```yaml
midea-exporter:
  build: ./midea_exporter
  container_name: midea-exporter
  environment:
    - MIDEA_IP=192.168.1.x
    - MIDEA_REGION=DE
    - MIDEA_DEVICE_NAME=portasplit
    - MIDEA_CACHE=/data/device_creds.json
    - EXPORTER_PORT=9116
  volumes:
    - /mnt/codex/midea-exporter:/data
  ports:
    - "9116:9116"
  restart: unless-stopped
  networks:
    - monitoring
```

```bash
docker compose build --no-cache midea-exporter
docker compose up -d --force-recreate midea-exporter
docker logs midea-exporter --follow
```

Expected cold-start log:

```
no cached creds -- running one-time cloud discovery (needs internet)
cached working creds for device id=153931629512914 -> /data/device_creds.json
first read ok: power=True target=20.0 indoor=23.0 outdoor=38.0 mode=2
```

## Metrics

All metrics carry the labels `account` and `device`.

| Metric | Type | Description |
|---|---|---|
| `midea_device_online` | gauge | Device reachable (1/0) |
| `midea_power_state` | gauge | Powered on (1/0) |
| `midea_target_temperature_celsius` | gauge | Target temperature |
| `midea_indoor_temperature_celsius` | gauge | Indoor coil-side sensor (under-reports room temp) |
| `midea_outdoor_temperature_celsius` | gauge | Device-side sensor (not ambient/outdoor) |
| `midea_fan_speed` | gauge | Fan speed (raw value, e.g. 80 = HIGH) |
| `midea_operational_mode` | gauge | Mode (1=auto 2=cool 3=dry 4=heat 5=fan) |

## Notes & gotchas

- **No energy data.** This device returns `None` for all power/energy fields
  over LAN. Use a smart plug for kWh.
- **Single connection slot.** The device occasionally drops its one connection;
  the exporter does one in-place retry before reconnecting. Occasional
  `refresh failed -- retrying once` warnings are normal.
- **Port 9116**, because 9115 is taken by the blackbox exporter in this stack.
- **Reset credentials**: delete `device_creds.json` and restart to force a fresh
  discovery (requires internet again for that run).
- `indoor` / `outdoor` only populate when the unit is powered on.

## Files

```
midea_exporter/
├── midea_exporter.py   # exporter
├── Dockerfile          # pins msmart-ng + prometheus_client
└── README.md           # this file
```
