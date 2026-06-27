# mnemosyne/scripts

Host scripts for Mnemosyne (Raspberry Pi 5). All are symlinked to
`/usr/local/bin/` and run as root unless noted.

Textfile-collector scripts write to
`/var/lib/node_exporter/textfile_collector/` where node-exporter picks them up
on the next scrape.

## Installation

```bash
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/backup-services.sh          /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/restore-services.sh         /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/verify-backup.sh            /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/fan-metrics.sh              /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/container-update-metrics.sh /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/tailscale-metrics.sh        /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/export-grafana-dashboards.sh /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/pump-alerts-deploy.sh       /usr/local/bin/
```

---

## Backup trio

These three scripts share the same flag conventions and form a single workflow.

### `backup-services.sh`

Nightly backup of all services to `/mnt/backup/<YYYY-MM-DD>/` (WD My Passport,
exFAT). Run from cron at 02:00.

```
--force              Ignore change detection — back up all services
--dry-run            Show what would run without writing anything
--only=<service>     Back up a single service
--overwrite          Replace today's backup if it already exists
--no-cleanup         Skip retention pruning
--retention=<days>   Override default 7-day retention
```

Services: `vaultwarden`, `caddy`, `calibre`, `calibre-web`, `kosync`,
`syncthing`, `aegis`, `gitea`, `nextcloud`, `grafana`, `prometheus`, `stacks`

Change detection uses `find -newer <timestamp>`. Unchanged services are skipped
and leave a `.SKIPPED` marker pointing to the last real archive — required
because exFAT does not support hardlinks or symlinks. Nextcloud enters
maintenance mode for the duration of its backup. Writes `backup.prom` on every
run (including failures) so Alertmanager can fire if no successful backup is
seen in 25 hours.

### `verify-backup.sh`

Verifies the most recent backup (or `--date=YYYY-MM-DD` for a specific one).

```
--date=<YYYY-MM-DD>  Verify a specific snapshot instead of the latest
--only=<service>     Verify a single service
--quick              File existence + size only — skip tar -tzf integrity check
--quiet              Print failures and warnings only
```

Checks: archive readability, Vaultwarden SQLite `PRAGMA integrity_check`,
Nextcloud MariaDB dump header, disk usage on `/mnt/backup` and `/mnt/codex`.
Follows `.SKIPPED` markers to older snapshots. `--quick` is used in the
automated post-backup cron run. Writes `backup_verify.prom`.

### `restore-services.sh`

Interactive TUI restore. Presents a snapshot selector and per-service toggle
menu. No data is modified until the user types `yes` at the confirmation prompt.

Notable behaviour:
- Nextcloud: DB container stays up for the SQL import; only the app container is stopped
- Calibre: `calibre-web` is stopped during library restore to avoid read/write conflicts
- Stack configs: archive restores via the `~/stacks/` symlink, which overwrites the homelab-infra working tree

---

## Textfile-collector metrics

### `fan-metrics.sh`

Reads Pi 5 fan level (0–4, `pwm-fan` driver — no RPM tachometer) and CPU
temperature from sysfs. Writes `fan.prom`. Run by `../systemd/fan-metrics.timer`
every 30 s.

### `container-update-metrics.sh`

Uses `skopeo inspect --raw` to fetch the registry manifest digest for each
running container without downloading image layers, and compares it against the
locally pulled digest. Writes `container_updates.prom`. Run by
`../systemd/container-update-metrics.timer` daily.

Requires `skopeo`:
```bash
sudo apt install skopeo
```

Status codes in metrics: `0` = up to date, `1` = update available,
`2` = local build (no registry), `3` = error.

### `tailscale-metrics.sh`

One-liner: dumps `tailscale debug metrics` atomically to `tailscale.prom`. Run
from cron.

---

## Operations

### `export-grafana-dashboards.sh`

Exports all Grafana dashboards as JSON to
`../stacks/monitoring/grafana/dashboards/`. Reads `GF_SECURITY_ADMIN_PASSWORD`
from `../stacks/monitoring/.env`. Run manually before committing dashboard
changes to the repo.

### `pump-alerts-deploy.sh`

Pulls the `pump_alerts` Gitea repo, validates the alert rules with
`promtool check rules` inside the running Prometheus container, then deploys
and hot-reloads Prometheus only if validation passes. Triggered by a Gitea
webhook — not run manually.

---

## Crontab reference

```cron
# Nightly backup at 02:00
0 2 * * * root /usr/local/bin/backup-services.sh >> /var/log/backup-services.log 2>&1

# Verify backup at 03:00 (after backup completes)
0 3 * * * root /usr/local/bin/verify-backup.sh --quick >> /var/log/backup-services.log 2>&1

# Tailscale metrics every 5 minutes
*/5 * * * * root /usr/local/bin/tailscale-metrics.sh
```

Fan and container-update metrics are handled by systemd timers in
`../systemd/` rather than cron.
