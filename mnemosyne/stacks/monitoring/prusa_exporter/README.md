# prusa-exporter

A small, read-only Prometheus exporter for a **Prusa MK4S** (and other PrusaLink
printers) via the local **PrusaLink** API. It scrapes `GET /api/v1/status` on
every Prometheus scrape (pull model), so each scrape reflects the printer's live
state — temperatures, printer state, and job progress.

No control endpoints are used: the exporter can read the printer, never move it.

## Why

Part of a self-hosted homelab monitoring stack (Prometheus + Grafana). The goal
is to observe the printer as just another monitored node — environment sensor for
the room, this exporter for the machine — without depending on any cloud service.
PrusaLink is local and works offline; Prusa Connect (cloud) is not required.

Notifications on print done/fail are intentionally **out of scope** — the Prusa
mobile app already covers that, so duplicating it would add noise without benefit.

## Metrics

All series carry a `printer` label (default `pygmalion`).

| Metric | Type | Notes |
| --- | --- | --- |
| `prusa_up` | gauge | `1` if PrusaLink was reachable and returned valid JSON this scrape, else `0`. |
| `prusa_printer_state{state="..."}` | gauge | State enum: `1` for the active state, `0` for the rest. |
| `prusa_temp_nozzle_celsius` | gauge | Nozzle temperature (°C). |
| `prusa_target_nozzle_celsius` | gauge | Nozzle target temperature (°C). |
| `prusa_temp_bed_celsius` | gauge | Heatbed temperature (°C). |
| `prusa_target_bed_celsius` | gauge | Heatbed target temperature (°C). |
| `prusa_axis_z_mm` | gauge | Z axis height (mm). |
| `prusa_flow_percent` | gauge | Flow rate (%). |
| `prusa_speed_percent` | gauge | Speed factor (%). |
| `prusa_fan_hotend_rpm` | gauge | Hotend fan (RPM). |
| `prusa_fan_print_rpm` | gauge | Print (part) fan (RPM). |
| `prusa_job_progress_percent` | gauge | Print progress (%). Present only while a job is running. |
| `prusa_job_time_remaining_seconds` | gauge | Estimated time remaining (s). Job only. |
| `prusa_job_time_printing_seconds` | gauge | Elapsed print time (s). Job only. |

On an unreachable printer the exporter emits only `prusa_up 0` and nothing else,
so the dashboard shows a gap instead of a stale-but-green value.

Known states in the enum: `IDLE`, `READY`, `PRINTING`, `PAUSED`, `FINISHED`,
`STOPPED`, `ATTENTION`, `ERROR`, `BUSY`. Any live state not in this list is added
on the fly, so new firmware states won't silently disappear.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `PRUSA_HOST` | *(required)* | Printer IP or host (e.g. `192.168.178.xx`, `pygmalion.home`). Append `:PORT` only for a non-default PrusaLink port. |
| `PRUSA_API_KEY` | *(auth)* | PrusaLink API key. Sent as the `X-Api-Key` header. Preferred if set. |
| `PRUSA_PASSWORD` | *(auth)* | PrusaLink password. Used for HTTP Digest auth if no API key is set. |
| `PRUSA_USERNAME` | `maker` | Digest-auth username (hardcoded to `maker` on current firmware). |
| `PRINTER_NAME` | `pygmalion` | Value for the `printer` label. |
| `EXPORTER_PORT` | `9118` | Port to serve `/metrics` on. |
| `SCRAPE_TIMEOUT` | `5` | HTTP timeout in seconds. |

Provide **one** of `PRUSA_API_KEY` or `PRUSA_PASSWORD`.

### Get the credentials

On the built-in PrusaLink (MK4S etc.) the web UI has **no Settings tab** — the
credentials live on the printer. On the printer's touchscreen:
**Settings → Network → PrusaLink**. There you'll find the `maker` username, a
15-character password, and (depending on firmware) a separate API key. The
printer's IP is shown on the same screen.

### Verify the API first

Which auth works depends on firmware, so test both and use whichever returns JSON
(never build on unverified identifiers):

```bash
# Option A: API key header
curl -s -H "X-Api-Key: $PRUSA_API_KEY" http://$PRUSA_HOST/api/v1/status | jq

# Option B: HTTP Digest auth (maker + password)
curl -s --digest -u maker:$PRUSA_PASSWORD http://$PRUSA_HOST/api/v1/status | jq
```

## Deploy

Intended to run inside the monitoring stack so Prometheus scrapes it over the
shared Docker network. Secrets come from an SOPS-decrypted `.env` at deploy time
— never commit the plaintext key.

`.env` (decrypted from `.env.sops`):

```dotenv
PRUSA_HOST=192.168.178.xx
# Provide ONE of the two (whichever your firmware accepted in the curl test):
PRUSA_API_KEY=<your-prusalink-api-key>
# PRUSA_PASSWORD=<your-prusalink-password>
```

Then add the service (see `docker-compose.yml`) and bring it up:

```bash
docker compose up -d --build prusa-exporter
```

Adjust the external network name in `docker-compose.yml` to match your monitoring
network.

### Prometheus scrape config

```yaml
  - job_name: prusa
    scrape_interval: 30s
    static_configs:
      - targets: ["prusa-exporter:9118"]
        labels:
          printer: pygmalion
```

### Grafana

Import `pygmalion-dashboard.json` and select your Prometheus data source. Panels:
printer state timeline, nozzle/bed temperatures (actual vs. target), print
progress, reachability, Z height.

## Notes

- **Label convention.** This exporter uses `printer="pygmalion"`. If you prefer
  consistency with `account` / `device` labels used elsewhere, rename it in the
  script, the scrape config, and the dashboard queries.
- **Read-only by design.** Only `GET /api/v1/status` is called. Runs as `nobody`
  in the container.
- **Firmware drift.** If a future PrusaLink release changes field names, re-run
  the `curl` check above and adjust the key names in `prusa_exporter.py`.

## Files

| File | Purpose |
| --- | --- |
| `prusa_exporter.py` | The exporter. |
| `requirements.txt` | Pinned Python dependencies. |
| `Dockerfile` | Container image (slim, non-root). |
| `docker-compose.yml` | Service definition for the monitoring stack. |
| `pygmalion-dashboard.json` | Grafana dashboard (import into Grafana). |
