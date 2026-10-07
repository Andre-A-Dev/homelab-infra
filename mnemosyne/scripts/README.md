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
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/restic-offsite.sh           /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/restic-maintenance.sh       /usr/local/bin/
sudo ln -sf ~/homelab-infra/mnemosyne/scripts/restic-unit-metrics.sh      /usr/local/bin/
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
exFAT). Runs as `backup-services.service`, started by `backup-services.timer`
at 02:00 (moved from cron on 2026-09-03 — see `../systemd/backup/`). A `flock`
on `/var/run/backup-services.lock` prevents overlapping runs.

```
--force              Ignore change detection — back up all services
--dry-run            Show what would run without writing anything
--only=<service>     Back up a single service
--overwrite          Replace today's backup if it already exists
--no-cleanup         Skip retention pruning
--no-offsite         Skip triggering the restic offsite service after backup
--retention=<days>   Override default 7-day retention
```

Services (`--only=` names): `vaultwarden`, `caddy`, `calibre`, `calibre-web`,
`kosync`, `syncthing`, `aegis`, `gitea`, `ghost`, `nextcloud`, `immich`,
`grafana`, `jobiris`, `gitea-runner`, `exporters`, `stacks`

Not backed up by design: Prometheus, Loki, Alloy, Alertmanager, Carousel jobs,
and Wakapi (accepted loss). All sources and exclusions are declared at the top
of the script (`SOURCE_PATHS`, `DRIFT_IGNORE`); a drift check flags any
container bind mount that is in neither list (`backup_uncovered_mounts`).

Change detection uses `find -newer <timestamp>`. Unchanged services are skipped
and leave a `.SKIPPED` marker pointing to the last real archive — required
because exFAT does not support hardlinks or symlinks. Nextcloud enters
maintenance mode for the duration of its backup and is archived with
`tar --sort=name` so the resulting tarball deduplicates cleanly across restic
snapshots. Writes `backup.prom` on every run (including failures) so
Alertmanager can fire if no successful backup is seen in 30 hours.

On a clean run (no errors), the script triggers `restic-offsite.service` via
`systemctl start --no-block` to ship the fresh tarballs offsite. Offsite is a
**separate, decoupled service** — it is not an inline step, has its own lock and
metrics (`restic_offsite.prom`), and its success or failure never affects this
script's exit code. `--no-offsite` skips the trigger; a broken local backup is
never shipped offsite.

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
Follows `.SKIPPED` markers to older snapshots. Runs daily at 04:00 as the
Gitea Action `backup-verify.yml`, which calls the script on Mnemosyne over SSH.
Writes `backup_verify.prom`.

### `restore-services.sh`

Interactive TUI restore. Presents a snapshot selector and per-service toggle
menu. No data is modified until the user types `yes` at the confirmation prompt.

Notable behaviour:
- Nextcloud: DB container stays up for the SQL import; only the app container is stopped
- Calibre: `calibre-web` is stopped during library restore to avoid read/write conflicts
- Stack configs: archive restores via the `~/stacks/` symlink, which overwrites the homelab-infra working tree

---

## Offsite backup (restic)

Offsite is handled by restic against a Hetzner Storage Box (SFTP), fully
decoupled from `backup-services.sh`. restic backs up the local tarball tree in
`/mnt/backup` — the tarballs are already application-consistent, and restic's
deduplication collapses the near-identical `nextcloud-data.tar` across days to
near-zero upload. Configuration lives in `/etc/restic/restic-offsite.env`
(mode 600); the repository password is in `/root/.config/restic/password` and
must exist as a backed-up copy in Vaultwarden **and** offline before first use —
lose it and the offsite repo is unrecoverable.

Both scripts read the shared env file and use an explicit `sftp.command` with a
spelled-out key path and host, because the root-owned systemd units do not
read `~/.ssh/config` (the same trap that once broke the old rclone offsite
step). Both also `flock` the same `/var/run/restic-repo.lock`, so backup and
maintenance can never run against the repository at the same time — the loser
exits immediately instead of being killed mid-transfer.

### `restic-offsite.sh`

Daily offsite backup: `restic unlock` (stale locks only) + `restic backup` +
`restic forget` (7 daily / 4 weekly / 6 monthly, no prune).
Runs as the `restic-offsite.service` oneshot, triggered by `backup-services.sh`
on success, with `restic-offsite.timer` as a 06:00 fallback. Writes
`restic_offsite.prom` (last-success timestamp, exit code, duration, bytes
actually uploaded post-dedup, snapshot count). Exit codes: `1` preflight error,
`2` backup failed, `3` backup ok but `forget` failed. Prune is deliberately excluded —
it is heavy on the Pi and belongs in the weekly maintenance run, not the nightly
path.

### `restic-maintenance.sh`

Weekly heavy maintenance: `restic prune` (reclaim space) + `restic check
--read-data-subset=2%` (structure plus a rotating 2% of the pack files pulled
back from the Storage Box to catch silent bitrot; the whole repository is
re-read over roughly a year). Reduced from 10% on 2026-08-21 — a 10% read
pulled ~71 GiB in one stream and reliably dropped the SSH connection. Runs as
`restic-maintenance.service`, driven by `restic-maintenance.timer` on Sundays at
05:00. Writes `restic_maintenance.prom`.

### `restic-unit-metrics.sh`

Records the **unit-level** outcome of a service as systemd sees it, called via
`ExecStopPost=` from `backup-services.service`, `restic-offsite.service` and
`restic-maintenance.service`. Writes a separate `<prefix>_unit.prom`
(`*_unit_success`, `*_unit_exit_status`, `*_unit_result`,
`*_unit_last_finish_timestamp`). It exists because a script that reports its
own health cannot report that it never started: on 2026-08-20 three `203/EXEC`
failures went unnoticed while the script's own metrics still showed a
six-week-old success. In `backup-services.service` the call is prefixed with
`-`, so a broken observer can never mark a successful backup as failed.

### Restore

restic makes restore-testing non-destructive: `restic mount` exposes the repo
read-only as a filesystem, so archive integrity can be verified
(`tar -tf .../nextcloud-data.tar`) without writing to any production path.
`restic dump <snapshot> <file>` streams a single file straight out of the repo —
e.g. piping a DB dump directly into `mariadb` with no local staging. Full setup
and init procedure: [`SETUP_Offsite.md`](SETUP_Offsite.md). Disaster restore from
the offsite copy: `wiki/Backup-Strategy.md` → *Offsite restore*.

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

## Scheduling reference

| Job | Schedule | Mechanism |
|---|---|---|
| `backup-services.sh` | Daily 02:00 | `backup-services.timer` (`../systemd/backup/`) |
| `restic-offsite.sh` | After a clean backup; fallback daily 06:00 | Triggered by `backup-services.sh`; `restic-offsite.timer` |
| `verify-backup.sh` | Daily 04:00 | Gitea Action `backup-verify.yml` (over SSH) |
| `restic-maintenance.sh` | Sundays 05:00 | `restic-maintenance.timer` |
| `fan-metrics.sh` | Every 30 s | `fan-metrics.timer` |
| `container-update-metrics.sh` | Daily | `container-update-metrics.timer` |
| `tailscale-metrics.sh` | Every 5 min | root cron |

```bash
systemctl list-timers 'backup-*' 'restic-*' 'fan-*' 'container-*'
```

The only remaining cron entry:

```cron
*/5 * * * * root /usr/local/bin/tailscale-metrics.sh
```

When enabling `backup-services.timer` on a host that still has the old
`0 2 * * * … backup-services.sh` cron line, remove that line — otherwise the
second run hits the lock and reports a spurious failure every night.
