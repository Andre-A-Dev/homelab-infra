# Mnemosyne

Raspberry Pi 5 at **192.168.1.10** — the primary homelab server. Runs all
main Docker Compose stacks, receives Gitea webhooks for GitOps deployment, and
is the backup destination for the satellite nodes (Zephyros).

## Storage layout

| Mount | Device | Filesystem | Contents |
|---|---|---|---|
| `/mnt/codex` | Samsung SSD | ext4 | Gitea, Nextcloud, Calibre, Syncthing, Aegis, KOSync data |
| `/mnt/backup` | WD My Passport | exFAT | 7-day rolling service backups |
| `/mnt/vault` | — | ext4 | Vaultwarden data, Caddy TLS certificates |

exFAT on the backup drive means **no hardlinks or symlinks**. The backup scripts
work around this with `.SKIPPED` marker files (see below).

## Deployment

Stacks are deployed via a Gitea webhook that runs selective
`docker compose up -d` on push — never manually. The `~/stacks/` directory is a
symlink to `~/homelab-infra/mnemosyne/stacks/` so compose files are always the
checked-out repo working tree.

## `scripts/`

Scripts are symlinked to `/usr/local/bin/` on the host. All write Prometheus
textfile metrics to `/var/lib/node_exporter/textfile_collector/` where
node-exporter picks them up on the next scrape.

### Backup trilogy

These three scripts form a single workflow and share the same flag style.

**`backup-services.sh`** — nightly backup to `/mnt/backup/<YYYY-MM-DD>/`

| Flag | Effect |
|---|---|
| `--force` | Ignore change detection — back up all services unconditionally |
| `--dry-run` | Show what would run, write nothing |
| `--only=<service>` | Back up a single service (vaultwarden, caddy, calibre, calibre-web, kosync, syncthing, aegis, gitea, nextcloud, grafana, prometheus, stacks) |
| `--overwrite` | Replace today's backup if it already exists |
| `--retention=<days>` | Override the default 7-day retention |
| `--no-cleanup` | Skip the retention pruning step |

Change detection uses `find -newer <timestamp>` on the source directory.
Unchanged services write a `.SKIPPED` marker containing the date of the last
real archive. Nextcloud enters maintenance mode for the duration of its backup.
Writes `backup.prom` on every run (including failures) so Alertmanager can fire
if no successful backup is seen within 25 hours.

**`verify-backup.sh`** — verifies the most recent backup (or `--date=YYYY-MM-DD`)

Checks: archive readability (`tar -tzf`), Vaultwarden SQLite `PRAGMA integrity_check`,
Nextcloud MariaDB dump header, disk space on both SSDs. Follows `.SKIPPED`
markers to the referenced older archive. `--quick` skips `tar -tzf` and only
checks file existence — used in the automated post-backup cron. Writes
`backup_verify.prom`.

**`restore-services.sh`** — interactive TUI restore

Presents an interactive snapshot selector and service toggle menu. Stops
containers, restores archives, restarts — no data is touched before explicit
`yes` confirmation. Nextcloud: DB container stays up for the SQL import while
the app container is down. Stack configs archive restores to the `~/stacks/`
symlink target (homelab-infra working tree).

### Monitoring helpers

**`fan-metrics.sh`** — reads Pi 5 fan level (0–4, `pwm-fan` driver — no RPM
tachometer on Pi 5) and CPU temperature from sysfs, writes `fan.prom`. Run by
the `fan-metrics.timer` every 30 s.

**`container-update-metrics.sh`** — iterates all running containers, uses
`skopeo inspect --raw` to fetch the registry manifest digest without downloading
layers, compares against the locally pulled digest. Writes `container_updates.prom`.
Run by the `container-update-metrics.timer` daily (5 min after boot, then 24 h).
Requires `skopeo` (`sudo apt install skopeo`).

**`tailscale-metrics.sh`** — dumps `tailscale debug metrics` atomically to
`tailscale.prom`. Run from cron.

### Operations

**`export-grafana-dashboards.sh`** — exports all Grafana dashboards as JSON to
`stacks/monitoring/grafana/dashboards/`. Reads `GF_SECURITY_ADMIN_PASSWORD`
from `stacks/monitoring/.env`. Run manually before committing dashboard changes.

**`pump-alerts-deploy.sh`** — pulls the `pump_alerts` Gitea repo, validates the
alert rules with `promtool check rules` inside the running Prometheus container,
and reloads Prometheus only if validation passes. Triggered by a Gitea webhook.

## `systemd/`

Deploy with:
```bash
sudo cp mnemosyne/systemd/*.service mnemosyne/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fan-metrics.timer container-update-metrics.timer
```

| Unit | Trigger | Script |
|---|---|---|
| `fan-metrics.timer` | every 30 s, 15 s after boot | `fan-metrics.sh` |
| `container-update-metrics.timer` | daily, 5 min after boot | `container-update-metrics.sh` |

Both services are `Type=oneshot` — systemd considers them done when the script exits.
