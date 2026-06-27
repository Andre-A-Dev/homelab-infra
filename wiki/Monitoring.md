# Monitoring

Prometheus scrapes metrics from all hosts and exporters. Grafana visualises them. Alertmanager routes firing alerts to ntfy. Everything runs in the `monitoring` stack on Mnemosyne.

---

## Stack components

| Component | Role | Port |
|---|---|---|
| Prometheus | Metrics storage + alerting engine | `9090` |
| Grafana | Dashboards + visualisation | `3000` (internal), `grafana.home` |
| Alertmanager | Alert routing → ntfy | `9093`, `alertmanager.home` |
| Node Exporter | System metrics (Mnemosyne) | `9100` |
| cAdvisor | Container metrics | `8080` |
| Blackbox Exporter | HTTP uptime probes | `9115` |
| Nextcloud Exporter | Nextcloud metrics | `9205` |
| Netatmo Exporter | Weather station | `9210` |
| Fritz Exporter | FritzBox home network | `9787` |
| Tado Exporter | Tado heating | `9100` |
| Shelly Exporter | Shelly smart plugs | `9117` |

All components share the `monitoring` internal Docker network. Prometheus, Grafana, and Alertmanager additionally join `caddy_proxy` to be reachable via Caddy.

---

## Scrape topology

```
Prometheus (Mnemosyne :9090)
│
├── Mnemosyne (local)
│   ├── node-exporter :9100  (+ textfile collector)
│   ├── cadvisor :8080
│   ├── nextcloud-exporter :9205
│   ├── netatmo-exporter :9210
│   ├── fritz-exporter :9787
│   ├── tado-exporter :9100
│   ├── blackbox-exporter :9115
│   ├── shelly-exporter :9117
│   ├── gitea :3000  (/metrics)
│   └── wakapi :3000  (/api/metrics)
│
├── Boreas (via LAN)
│   ├── node-exporter :9100
│   └── pihole6-exporter :9666
│
├── Zephyros (via Tailscale)
│   ├── node-exporter :9100
│   ├── pihole6-exporter :9666
│   ├── fritz-exporter :9787
│   └── fritz-exporter-lua :9042
│
├── Hephaestus (via LAN)
│   └── node-exporter :9100  (+ textfile collector: Viessmann)
│
└── Astraeus (via LAN)
    ├── windows-exporter :9182
    └── nvidia-exporter

Prometheus --[alerts]--> Alertmanager --[webhook]--> ntfy
Grafana --> Prometheus
```

---

## Textfile collector pattern

For metrics that cannot be scraped live, shell scripts write `.prom` files to `/var/lib/node_exporter/textfile_collector/`. Node Exporter picks them up on the next scrape without any additional process running.

All scripts write atomically: output goes to a `.tmp` file first, then `mv` replaces the target in one operation so Node Exporter never reads a partially written file.

| Source | Script | Trigger | Output file | Interval |
|---|---|---|---|---|
| Pi 5 fan level + CPU temp | `fan-metrics.sh` | systemd timer | `fan.prom` | 30s |
| Tailscale status | `tailscale-metrics.sh` | cron | `tailscale.prom` | -- |
| Container image update status | `container-update-metrics.sh` | systemd timer | `container_updates.prom` | 24h |
| Backup results | `backup-services.sh` | cron (daily 02:00) | `backup.prom` | daily |
| Viessmann heating | `viessmann-exporter.sh` (Hephaestus) | cron (every minute) | `viessmann.prom` | 60s |

### Container update metrics

`container-update-metrics.sh` uses `skopeo` to fetch the manifest digest from the registry without downloading layers, then compares it against the locally running image digest. Emits:

```
container_image_update_available{container="...",image="...",compose_path="..."} 0|1
container_image_check_status{...}  # 0=up_to_date 1=update_available 2=local_build 3=error
container_update_check_timestamp_seconds
container_update_check_duration_seconds
```

Dependency: `sudo apt install skopeo`

### Fan metrics (Mnemosyne only)

The Pi 5 pwm-fan driver exposes a 0–4 level, not RPM (no tachometer). The script reads from `/sys/class/thermal/` and emits `node_fan_level`, `node_fan_level_ratio`, and `node_cpu_temperature_celsius`.

```bash
# Check current values
cat /var/lib/node_exporter/textfile_collector/fan.prom
```

---

## Adding a new scrape target

1. Add a job to `mnemosyne/stacks/monitoring/prometheus/prometheus.yml`:

```yaml
- job_name: "my-service"
  static_configs:
    - targets: ["container-name:port"]
      labels:
        hostname: "mnemosyne"
  relabel_configs:
    - source_labels: [hostname]
      target_label: instance
```

2. If the exporter runs in a Docker container on Mnemosyne, add it to the `monitoring` network in its `docker-compose.yml`. Prometheus resolves container names via the shared network -- no IP address needed.

3. Reload Prometheus config without restart:

```bash
curl -X POST http://localhost:9090/-/reload
```

Or restart the stack if the change includes new containers:

```bash
cd ~/stacks/monitoring && docker compose up -d
```

4. Verify the target is scraped:

```bash
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data['data']['activeTargets']:
    print(t['labels'].get('job'), t['labels'].get('instance'), t['health'])
"
```

---

## Dashboards

Dashboard JSON files are versioned in `mnemosyne/stacks/monitoring/grafana/dashboards/`. The naming convention `NN_NN_Title` groups dashboards by category.

| File | Dashboard |
|---|---|
| `01_01_Homelab_Overview` | Overview of all hosts and services |
| `01_02_Windows_PCs` | Windows system metrics (AstraeusNX) |
| `01_03_Node_Exporter` | Per-host system metrics |
| `01_04_Pi_hole` | Pi-hole DNS stats (Boreas + Zephyros) |
| `01_05_Prometheus` | Prometheus self-monitoring |
| `01_06_Tailscale` | Tailscale connection status |
| `01_07_FritzExporter` | FritzBox network metrics |
| `01_08_Gitea` | Gitea repository metrics |
| `01_10_Shelly` | Shelly smart plug power metrics |
| `02_01_KlimaHeizung` | Combined climate + heating overview |
| `02_02_Tado` | Tado heating |
| `02_03_Netatmo` | Netatmo weather station |
| `02_04_Heizung` | Viessmann boiler (+ control panels) |
| `02_05_Pumpe_Keller` | Cellar drainage pump power + alerts |
| `03_01_Netatmo_Ext` | Netatmo extended view |

> `01_09_Meross.json` is a stale dashboard left from before the Shelly migration -- safe to delete from Grafana.

### Exporting dashboards

Run after making changes in the Grafana UI:

```bash
~/homelab-infra/mnemosyne/scripts/export-grafana-dashboards.sh
```

The script fetches all dashboards via the Grafana API (reads `GF_SECURITY_ADMIN_PASSWORD` from the monitoring `.env`) and writes them as JSON to the dashboards directory. Commit and push from Windows.

> Grafana provisioning is not yet configured -- dashboards are not auto-loaded from the JSON files on stack start. Changes must be exported manually after editing in the UI.

---

## Alert rules

### System alerts (`alerts.yml`)

| Alert | Condition | Severity |
|---|---|---|
| `DiskSpaceWarning` | Root partition > 80% for 5 min | warning |
| `DiskSpaceCritical` | Root partition > 90% for 2 min | critical |
| `DataDiskWarning` | `/mnt/codex` or `/mnt/vault` > 75% for 5 min | warning |
| `DataDiskCritical` | `/mnt/codex` or `/mnt/vault` > 90% for 2 min | critical |
| `HighMemoryUsage` | RAM > 90% for 5 min | warning |
| `HighCpuLoad` | CPU > 80% for 15 min | warning |
| `NodeDown` | Node Exporter unreachable for 2 min | critical |
| `ServiceDown` | Blackbox HTTP probe fails for 2 min | critical |
| `ServiceSlowResponse` | HTTP response > 2s for 5 min | warning |
| `PiholeDown` | Pi-hole exporter unreachable for 2 min | critical |
| `PiholeHighBlockRate` | Block rate > 50% for 10 min | warning |

### Pump alerts (`pump-alerts.yml`)

Monitors the cellar drainage pump via a Shelly smart plug (`device="Pumpe-Keller"`).

| Alert | Condition | Severity |
|---|---|---|
| `PumpBlocked` | Power draw > 350 W for 5 min | critical |
| `PumpInactive` | Power draw < 1 W for 2 h | warning |
| `PumpPlugOffline` | Shelly device unreachable for 5 min | critical |
| `PumpLowDailyConsumption` | Max draw < 10 W in last 20 h (after 20:00) | warning |

---

## Alertmanager

### Routing

```
Alert fires
│
├── device="pump" → pump-warning (repeat 4h)
│   └── severity="critical" → pump-critical (repeat 1h)
│
├── severity="critical" → mnemosyne-critical (repeat 1h)
│
└── everything else → mnemosyne-warning (repeat 4h)
```

### ntfy topics

Four ntfy topics cover the two dimensions (infrastructure vs. pump) × (warning vs. critical):

| Variable | Receiver | Used for |
|---|---|---|
| `NTFY_PUMP_CRITICAL` | `pump-critical` | PumpBlocked, PumpPlugOffline |
| `NTFY_PUMP_WARNING` | `pump-warning` | PumpInactive, PumpLowDailyConsumption |
| `NTFY_MNEMOSYNE_CRITICAL` | `mnemosyne-critical` | NodeDown, ServiceDown, disk critical |
| `NTFY_MNEMOSYNE_WARNING` | `mnemosyne-warning` | disk warning, high CPU/RAM, slow response |

Topic names are injected via environment variables at container startup -- `sed` rewrites the template into `/tmp/alertmanager.yml`. The actual topic names never appear in the committed file.

### Inhibition rules

- **Critical suppresses warning** for the same `alertname` -- avoids double notifications at different severities
- **NodeDown suppresses PiholeDown** for the same `instance` -- when a Pi-hole host goes offline entirely, the Pi-hole alert is redundant

### Mute windows

| Window | Schedule | Suppresses |
|---|---|---|
| Backup window | Sunday 03:45–05:30 | `ServiceDown` for Nextcloud (Nextcloud is stopped during backup) |

---

## Useful commands

```bash
# Check all scrape target health
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data['data']['activeTargets']:
    print(t['labels'].get('job'), t['labels'].get('instance'), t['health'], t.get('lastError',''))
"

# Check firing alerts
curl -s http://localhost:9090/api/v1/alerts | python3 -m json.tool

# Reload Prometheus config (no restart needed)
curl -X POST http://localhost:9090/-/reload

# Check Alertmanager firing alerts
curl -s http://localhost:9093/api/v2/alerts | python3 -m json.tool

# View active silences
curl -s http://localhost:9093/api/v2/silences | python3 -m json.tool

# Silence an alert via CLI
curl -s -X POST http://localhost:9093/api/v2/silences \
  -H "Content-Type: application/json" \
  -d '{
    "matchers": [{"name":"alertname","value":"ServiceDown","isRegex":false}],
    "startsAt": "2026-01-01T00:00:00Z",
    "endsAt": "2026-01-01T04:00:00Z",
    "createdBy": "admin",
    "comment": "planned maintenance"
  }'

# Verify textfile collector output
ls -lh /var/lib/node_exporter/textfile_collector/
cat /var/lib/node_exporter/textfile_collector/fan.prom
cat /var/lib/node_exporter/textfile_collector/backup.prom

# Grafana logs
cd ~/stacks/monitoring && docker compose logs grafana --tail 30

# Prometheus storage usage
curl -s http://localhost:9090/api/v1/status/tsdb | python3 -m json.tool
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Target shows `down` in Prometheus | Exporter not running or wrong port | Check container logs; verify port in `prometheus.yml` |
| Target shows `down` for remote host | Network unreachable (Tailscale for Zephyros) | `tailscale ping <ip>` from Mnemosyne |
| `connection refused` for container target | Container not on `monitoring` network | Add `monitoring` network to the exporter's compose file |
| No alert fired despite condition met | `for:` duration not yet elapsed | Check pending alerts at `http://localhost:9090/api/v1/alerts` |
| Alert fires but no ntfy notification | Wrong topic name or Alertmanager misconfigured | `docker logs alertmanager --tail 30`; check template rendering |
| Alertmanager config rejected on start | `sed` template error | `docker logs alertmanager --tail 20` for parse error detail |
| Grafana shows no data after scrape change | Datasource query references old label | Update the panel query in Grafana UI |
| `fan.prom` stale | fan-metrics timer stopped | `systemctl status fan-metrics.timer` |
| `container_updates.prom` missing | `skopeo` not installed | `sudo apt install skopeo` |
