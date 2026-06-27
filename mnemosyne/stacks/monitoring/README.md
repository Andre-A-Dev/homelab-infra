# monitoring

Central observability stack for the homelab. Runs on Mnemosyne and scrapes
metrics from all nodes and services across the network.

## Services

| Container | Image | Port | Role |
|---|---|---|---|
| `prometheus` | `prom/prometheus` | 9090 | TSDB, alerting engine |
| `alertmanager` | `prom/alertmanager` | 9093 | Alert routing → ntfy.sh |
| `grafana` | `grafana/grafana` | 3000 | Dashboards |
| `node-exporter` | `prom/node-exporter` | 9100 | Mnemosyne host metrics + textfile |
| `cadvisor` | `gcr.io/cadvisor/cadvisor` | — | Docker container metrics |
| `blackbox-exporter` | `prom/blackbox-exporter` | 9115 | HTTP uptime probes |
| `nextcloud-exporter` | `xperimental/nextcloud-exporter` | 9205 | Nextcloud metrics |
| `netatmo-exporter` | `xperimental/netatmo-exporter` | 9210 | Netatmo weather station |
| `tado-exporter` | `adventuresintech/tado-prometheus-exporter` | 9100 | Tado thermostat |
| `fritz-exporter` | `pdreker/fritz_exporter` | 9787 | FritzBox TR-064 (home) |
| `meross-exporter` | local build | 9114 | Meross smart plugs |
| `shelly-exporter` | local build | 9117 | Shelly plugs + H&T sensors |
| `midea-exporter` | local build | 9116 | Midea air conditioner |

`hue-exporter` is present in the compose file but commented out.

Grafana, Prometheus, and Alertmanager are on both the `monitoring` and
`caddy_proxy` networks so Caddy can reverse-proxy them at `*.home`.

## Prometheus scrape targets

| Job | Target(s) | Interval | Notes |
|---|---|---|---|
| `prometheus` | localhost:9090 | 15s | Self-scrape |
| `node-exporter` | Mnemosyne, Boreas (LAN), Hephaestus (LAN), Zephyros (Tailscale) | 15s | |
| `cadvisor` | cadvisor:8080 | 15s | |
| `nextcloud` | nextcloud-exporter:9205 | 15s | |
| `netatmo` | netatmo-exporter:9210 | 5m | Netatmo API is rate-limited |
| `tado` | tado-exporter:9100 | 1m | |
| `pihole` | Boreas (LAN), Zephyros (Tailscale) :9666 | 30s | 25s timeout |
| `blackbox-external` | cloud.yourdomain.dedyn.io | 15s | TLS via Let's Encrypt |
| `blackbox-internal` | vault/git/grafana/… .home | 15s | TLS via Caddy internal CA |
| `windows` | Astraeus (LAN), desktop (LAN) :9182 | 15s | windows_exporter |
| `fritzbox` | fritz-exporter:9787 (home), Zephyros:9787 (remote) | 60s | TR-064 |
| `fritzbox-lua` | Zephyros:9042 (remote) | 60s | DECT + CPU/temp via Lua |
| `gitea` | gitea:3000 | 15s | |
| `meross` | meross-exporter:9114 | 30s | |
| `shelly` | shelly-exporter:9117 | 30s | |
| `wakapi` | wakapi:3000 `/api/metrics` | 15s | API key passed as query param |
| `midea` | midea-exporter:9116 | 30s | |

Remote nodes (Boreas, Hephaestus, Zephyros) are scraped directly by IP.
Zephyros targets use the Tailscale IP (`100.y.y.y`). There is no service
discovery — adding a new target requires editing `prometheus/prometheus.yml`.

## Alertmanager

`alertmanager.yml.tmpl` is a template, not valid YAML on its own. The
container entrypoint runs `sed` to substitute four `NTFY_*_TOPIC` placeholders
with the actual ntfy.sh topic names from the environment, then writes the result
to `/tmp/alertmanager.yml` before starting the daemon. Topic names are secrets
— they live in `.env` and are never committed.

### Alert routing

All alerts → ntfy.sh via webhook. Four channels:

| Receiver | Trigger | Repeat |
|---|---|---|
| `mnemosyne-warning` | default (anything not matched below) | 4h |
| `mnemosyne-critical` | `severity=critical` | 1h |
| `pump-warning` | `device=pump` | 4h |
| `pump-critical` | `device=pump` + `severity=critical` | 1h |

Inhibition rules:
- `critical` suppresses `warning` for the same `alertname`
- `NodeDown` suppresses `PiholeDown` for the same `instance` (avoids double-alerting when a Pi-hole host goes offline)

**Backup mute window:** `ServiceDown` for Nextcloud is silenced on Sundays
03:45–05:30 because `restore-services.sh` stops the Nextcloud container during
the weekly backup.

### Alert rule files

- `prometheus/alerts.yml` — committed, covers all standard alerts (node, Pi-hole, backup, services, etc.)
- `prometheus/pump-alerts.yml` — **managed externally**. Deployed by
  `mnemosyne/scripts/pump-alerts-deploy.sh` from a separate private Gitea repo
  (`pump_alerts`). Editing this file directly will be overwritten on the next
  webhook trigger.

## Grafana

Grafana depends on Prometheus via `condition: service_healthy` — it will not
start until Prometheus passes its health check.

Dashboards are provisioned from `grafana/dashboards/*.json` via
`grafana/provisioning/`. To export the live dashboards back to the repo:

```bash
~/homelab-infra/mnemosyne/scripts/export-grafana-dashboards.sh
```

Data volume: `/mnt/codex/grafana` (persists across container restarts and
recreates).

## Persistent volumes

| Container | Host path | Contents |
|---|---|---|
| `prometheus` | `/mnt/codex/prometheus` | TSDB (30-day retention) |
| `alertmanager` | `/mnt/codex/alertmanager` | Alert state |
| `grafana` | `/mnt/codex/grafana` | Dashboards, users, datasource config |
| `netatmo-exporter` | `/mnt/codex/netatmo-exporter` | OAuth token cache |
| `tado-exporter` | `/mnt/codex/tado-exporter` | Auth token cache |
| `midea-exporter` | `/mnt/codex/midea-exporter` | Device credentials cache |

## Custom exporters

Each has its own subdirectory and README:

- [`meross_exporter/`](meross_exporter/README.md) — Meross smart plugs (cloud API)
- [`shelly_exporter/`](shelly_exporter/README.md) — Shelly plugs, H&T Gen3, BLU H&T
- [`midea_exporter/`](midea_exporter/README.md) — Midea/Comfee air conditioner (LAN)

## Startup

```bash
cd mnemosyne/stacks/monitoring
docker compose up -d
```

Grafana will not serve until Prometheus is healthy (~15 s). The meross-exporter
health check has a 60 s start period to allow device discovery to complete.
