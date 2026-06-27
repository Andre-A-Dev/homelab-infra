# Runbook

Common operational procedures. No explanations — just the commands.

---

## Update a Stack

```bash
cd ~/stacks/<stack>
docker compose pull
docker compose up -d
docker compose logs -f --tail 50
```

---

## Reload Caddy Config

Use after editing the Caddyfile. If the editor saves atomically (new file + rename), the bind-mount inode breaks — `reload` will silently read the stale config. Use `restart` instead.

```bash
# Safe for all cases
cd ~/stacks/caddy && docker compose down && docker compose up -d

# Only if you're certain the file was edited in-place (e.g. sed -i)
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
docker logs caddy --tail 20
```

---

## Add a New Internal Service to Caddy

1. Add a block to `~/stacks/caddy/Caddyfile`:

```
service.home {
  tls internal
  reverse_proxy <container-name>:<internal-port>
}
```

2. Add the service to the `caddy_proxy` network in its `docker-compose.yml`:

```yaml
networks:
  caddy_proxy:
    external: true
```

3. Add a DNS record in Pi-hole: **Local DNS → DNS Records** → `service.home` → `192.168.1.10`

4. Restart Caddy:

```bash
cd ~/stacks/caddy && docker compose down && docker compose up -d
```

---

## Run Backup Manually

```bash
sudo /usr/local/bin/backup-services.sh
tail -50 /var/log/backup-services.log
```

Back up a single service (e.g. after a config change mid-week):

```bash
sudo /usr/local/bin/backup-services.sh --only=vaultwarden
```

Force all services regardless of change detection:

```bash
sudo /usr/local/bin/backup-services.sh --force --overwrite
```

---

## Verify Backup

Full integrity check (opens every archive, runs SQLite and MariaDB checks):

```bash
sudo /usr/local/bin/verify-backup.sh
```

Quick check — existence and size only, skips `tar -tzf` (faster, used by cron):

```bash
sudo /usr/local/bin/verify-backup.sh --quick
```

Verify a specific date instead of the latest:

```bash
sudo /usr/local/bin/verify-backup.sh --date=2026-06-01
```

Check backup health via Prometheus metrics (non-zero = problem):

```bash
cat /var/lib/node_exporter/textfile_collector/backup.prom | grep -v "^#"
cat /var/lib/node_exporter/textfile_collector/backup_verify.prom | grep -v "^#"
```

---

## Restore from Backup

> Requires the WD My Passport to be mounted at `/mnt/backup`. Check with
> `mountpoint -q /mnt/backup && echo mounted || echo NOT MOUNTED`.

Interactive restore — presents a snapshot selector and per-service toggle menu.
No data is touched until you type `yes`:

```bash
sudo /usr/local/bin/restore-services.sh
```

Notable per-service behavior:

- **Nextcloud** — DB container stays up for the SQL import; only the app
  container is stopped. Maintenance mode is toggled around the restore.
- **Calibre** — `calibre-web` is stopped during library restore to avoid
  read/write conflicts.
- **Stack configs** — extracted via the `~/stacks/` symlink, which overwrites
  the homelab-infra working tree.
- **Skipped snapshots** — if a service has no archive on the selected date, the
  script follows the `.SKIPPED` marker to the last real archive automatically.

### Restore a single service

At the service selection menu, toggle only the service you need — press its
number, then Enter to confirm.

### After a Nextcloud restore

Run a file scan to sync the DB with the restored data:

```bash
docker exec -u www-data nextcloud php occ files:scan --all
docker exec -u www-data nextcloud php occ maintenance:mode --off  # if still on
```

### After restoring stack configs

The homelab-infra working tree has been overwritten. Restart affected stacks:

```bash
cd ~/stacks/<affected-stack> && docker compose up -d
```

---

## Export Grafana Dashboards

Run after making changes to dashboards in the Grafana UI:

```bash
~/homelab-infra/mnemosyne/scripts/export-grafana-dashboards.sh
```

Then commit and push the updated JSON files from Windows.

---

## Import Caddy Root Certificate on a New Device

Extract the certificate from the running container:

```bash
docker cp caddy:/data/caddy/pki/authorities/local/root.crt ~/caddy-root.crt
```

**Windows:** Double-click `caddy-root.crt` → Install → Local Machine → Trusted Root Certification Authorities.

**Android:** Copy via Syncthing or ADB → Settings → Security → Install certificate → CA certificate. Use Brave or Chrome for `.home` access — Firefox on Android uses its own certificate store and ignores the system CA.

**CachyOS:** `trust extract-compat` after adding the cert to the system trust store. Electron apps (VSCodium, Bitwarden desktop) ignore the system CA — add to the NSS database: `certutil -d sql:$HOME/.pki/nssdb -A -t "CT,," -n "Caddy Local CA" -i ~/caddy-root.crt`.

---

## Change Pi-hole Password

```bash
# On Boreas
ssh youruser@192.168.1.11
sudo pihole setpassword

# On Zephyros (via Tailscale)
ssh youruser@100.y.y.y
sudo pihole setpassword
```

Update the exporter and restart:

```bash
sudo nano /etc/pihole6-exporter.env
sudo systemctl restart pihole6-exporter
sudo systemctl status pihole6-exporter
```

---

## Check Webhook Status

```bash
# Handler log
tail -30 /var/log/webhook-handler.log

# Webhook service
journalctl -u webhook -f
sudo systemctl status webhook
```

---

## Restart a Stuck Container

```bash
docker ps -a                          # find the container
docker restart <container-name>
docker logs <container-name> --tail 50
```

---

## Recover from Docker Layer Cache Corruption

Symptom: containers exit with code 255, `RWLayer ... is unexpectedly nil` in logs after an unclean reboot.

```bash
cd ~/stacks/<stack>
docker compose down && docker compose up -d
```

If that fails (corruption extends into the content store):

```bash
docker system prune -af
docker compose pull
docker compose up -d
```

---

## Check All Prometheus Targets

```bash
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data['data']['activeTargets']:
    print(t['labels'].get('job'), t['labels'].get('instance'), t['health'], t.get('lastError',''))
"
```

---

## Mnemosyne Full Reboot

```bash
sudo reboot
```

Services with `restart: unless-stopped` come back automatically. Check afterwards:

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
systemctl status syncthing@youruser
systemctl status tailscaled
systemctl status webhook
systemctl status fan-metrics.timer
```

---

## Immich

### Check ML job queue

```bash
docker logs immich-machine-learning --tail 50
```

### Restart ML worker (if stuck)

```bash
cd ~/stacks/immich
docker compose restart immich-machine-learning
```

### Re-run Smart Search / Face Detection

Use the Immich web UI: **Administration → Jobs → Smart Search / Face Detection → Run All**.

> ML jobs run at concurrency 1. Let them complete overnight on the Pi 5 — do not raise concurrency.

### Check Immich database

```bash
docker exec -it immich-postgres psql -U postgres immich -c "\dt"
```

---

## Fan Monitoring (Mnemosyne)

### Check current fan level

```bash
cat /sys/class/thermal/cooling_device0/cur_state   # 0–4
cat /sys/class/thermal/thermal_zone0/temp           # millidegrees Celsius
```

### Check textfile collector output

```bash
cat /var/lib/node_exporter/textfile_collector/fan.prom
```

### Check systemd timer

```bash
systemctl status fan-metrics.timer
systemctl status fan-metrics.service
journalctl -u fan-metrics.service --since "1 hour ago"
```

### Run manually

```bash
sudo /usr/local/bin/fan-metrics.sh
```

---

## Shelly Exporter

### Check exporter health

```bash
curl -s http://localhost:9117/health
curl -s http://localhost:9117/metrics | grep shelly_device_online
```

### Restart

```bash
cd ~/stacks/monitoring
docker compose restart shelly-exporter
docker compose logs shelly-exporter -f --tail 30
```

### Add a new device

Edit `~/stacks/monitoring/.env` and append to `SHELLY_DEVICES`:

```
SHELLY_DEVICES=existing-device:192.168.x.x:2,new-device:192.168.x.x:3
```

Format: `name:host:gen` — gen `1` for Gen1, `2` or `3` for Gen2/3.

```bash
docker compose up -d
curl -s http://localhost:9117/metrics | grep shelly_device_online
```

---

## Alertmanager

### Check firing alerts

```bash
curl -s http://localhost:9093/api/v2/alerts | python3 -m json.tool
```

### Check Alertmanager status

```bash
cd ~/stacks/monitoring
docker compose logs alertmanager --tail 30
```

### Silence an alert (via UI)

Open `https://alertmanager.home` → **Silences** → **New Silence**. Set matcher, duration, and comment.

### Silence an alert (via CLI)

```bash
# List active silences
curl -s http://localhost:9093/api/v2/silences | python3 -m json.tool

# Delete a silence by ID
curl -X DELETE http://localhost:9093/api/v2/silences/<silence-id>
```

### Restart Alertmanager

```bash
cd ~/stacks/monitoring
docker compose restart alertmanager
docker compose logs alertmanager --tail 20
```

---

## Gitea Actions CI

### Check runner status

```bash
cd ~/stacks/gitea-runner
docker compose logs -f --tail 30
```

### Re-register runner (after token rotation)

```bash
cd ~/stacks/gitea-runner
docker compose down
# Update GITEA_RUNNER_REGISTRATION_TOKEN in .env
docker compose up -d
docker compose logs -f --tail 20
# Watch for: "runner registered successfully"
```

### Re-run a failed workflow

Gitea web UI → Repository → Actions → select failed run → **Re-run jobs**.

---

## Viessmann Heating

### Check vcontrold status

```bash
ssh youruser@192.168.1.13
sudo systemctl status vcontrold
sudo tail -30 /var/log/vcontrold.log
```

### Read current heating values

```bash
vclient -h 127.0.0.1:3002 -c "getTempA,getTempKist,getBetriebArtM1,getNeigungM1,getNiveauM1,getPumpeStatusM1,getBrennerStatus"
```

### Read all status values via API

```bash
curl -s https://viessmann.home/status | python3 -m json.tool
```

### Check Viessmann exporter output

```bash
cat /var/lib/node_exporter/textfile_collector/viessmann.prom | grep -v "^#"
```

### Restart vcontrold after USB adapter reconnect

```bash
sudo systemctl restart vcontrold
sudo systemctl status vcontrold
vclient -h 127.0.0.1:3002 -c getDevType
# Expected: V200KW2 ID=2098
```

### Enable / disable Viessmann Control API

```bash
# Enable
sudo systemctl enable viessmann-api
sudo systemctl start viessmann-api

# Disable (removes write access to heating)
sudo systemctl stop viessmann-api
sudo systemctl disable viessmann-api
```

> The API controls Neigung, Niveau and temperature setpoints. vcontrold and the Prometheus exporter continue running regardless — monitoring is unaffected.

### KW2 pump not starting after mode change

The KW2 state machine occasionally does not activate the heating circuit pump after a remote mode change. Workaround:

```bash
# Reset cycle via vclient
vclient -h 127.0.0.1:3002 -c "setBetriebArtM1 ABSCHALT"
sleep 10
vclient -h 127.0.0.1:3002 -c "setBetriebArtM1 H+WW"
```
