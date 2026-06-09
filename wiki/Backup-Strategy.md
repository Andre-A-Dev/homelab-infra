# Backup Strategy

## Principle: 3-2-1

**3** copies of the data — **2** different media — **1** copy offsite. Until all three conditions are met, it is not a backup.

---

## What Gets Backed Up

| Service | Data path | Type | Priority |
|---|---|---|---|
| Vaultwarden | `/mnt/vault/vaultwarden/data/` | Host mount | Critical |
| Caddy TLS | `/mnt/vault/caddy/` | Host mount | High |
| Nextcloud files | `/mnt/codex/nextcloud/data/` | Host mount | High |
| Nextcloud DB | `/mnt/codex/nextcloud/db/` (MariaDB) | Host mount | Critical |
| Syncthing vault | `/mnt/codex/syncthing/obsidian/` | Host mount | High |
| Syncthing config | `~/.local/state/syncthing/` | Host path | High |
| Gitea | `/mnt/codex/gitea/data/` | Host mount | High |
| Calibre library | `/mnt/codex/calibre-library/` | Host mount | Medium |
| Calibre-Web config | Docker volume `calibre-web-config` | Named volume | Medium |
| KOSync | Docker volume `kosync-data` | Named volume | Low |
| Grafana | Docker volume `grafana-data` | Named volume | Medium |
| Prometheus | Docker volume `prometheus-data` | Named volume | Low |
| Stack configs | `~/stacks/` | Host path | Critical |

**Stack configs are critically underrated.** The `docker-compose.yml` and `Caddyfile` are small files, but without them a restore takes hours instead of minutes.

**Named Docker volumes** (`calibre-web-config`, `kosync-data`, `grafana-data`, `prometheus-data`) live under `/var/lib/docker/volumes/` and cannot be archived with a plain `tar` on the host path. The backup script handles them correctly via a temporary Alpine container.

**Nextcloud requires special handling.** MariaDB must not be backed up by running `tar` on the live database directory — this produces corrupt backups. The correct approach is maintenance mode + `mariadb-dump`, with a `trap ... EXIT` to ensure maintenance mode is always disabled even if the script crashes. File data is archived uncompressed (`.tar`) — photos and videos are already compressed and do not benefit from gzip.

**Prometheus data is expendable.** Time-series data refills within hours after a rebuild. Grafana dashboards and configuration, however, must be backed up.

---

## Failure Scenarios

### Scenario A — Single service failure
*"Vaultwarden database corrupt, everything else running"*

Stop the affected service, restore from backup, restart. Expected downtime: 5–15 minutes.

### Scenario B — SSD failure
*"Pi won't boot, hardware otherwise intact"*

Replace SSD, reinstall OS, restore all services from backup. Expected downtime: 2–3 hours.

### Scenario C — Complete hardware failure
*"Pi hardware dead, replacement needed"*

New Pi, reinstall OS, restore all services from backup. Expected downtime: ~1 day (delivery time).

---

## Backup Layers

### Layer 1 — Data backup (daily, automated, 02:00)

The backup script (`backup-services.sh`) archives all service data to a USB SSD mounted at `/mnt/backup`. Each run creates a dated directory. Archives older than 14 days are deleted automatically.

The database password is not stored in the script. It is loaded from `/etc/backup-secrets.conf` (mode `600`, root-only), which must be created manually on a new system:

```bash
echo 'NEXTCLOUD_DB_PW="your_password"' | sudo tee /etc/backup-secrets.conf
sudo chmod 600 /etc/backup-secrets.conf
```

### Layer 2 — Offsite backup (weekly, automated, Sunday 04:00)

`rclone` copies the latest backup directory to a cloud storage target (encrypted). This satisfies the "1 offsite copy" requirement of 3-2-1.

### Layer 3 — System image (monthly, manual)

`rpi-clone` creates a full SD card / SSD image to a second drive. Covers the OS and all configuration outside the data directories.

---

## Backup Medium

The local backup target is a USB SSD mounted at `/mnt/backup` (exFAT). The `nofail` mount option is required — without it, a missing drive blocks the boot process.

```
/etc/fstab entry:
UUID=<uuid>  /mnt/backup  exfat  defaults,nofail,uid=1000,gid=1000,umask=022  0  0
```

exFAT does not support hardlinks or symlinks. The backup script uses a `has_changed()` helper and `.SKIPPED` marker files to avoid re-archiving unchanged data on every run.

---

## Schedule

| What | Frequency | When | Medium |
|---|---|---|---|
| Data backup (script) | Daily | 02:00 | Backup SSD |
| Disk space check (ntfy alert) | Daily | 08:00 | — |
| Offsite backup (rclone) | Weekly | Sunday 04:00 | Cloud |
| System image (rpi-clone) | Monthly | Manual | Second SSD |
| Log review | Weekly | Manual | — |
| Restore test | Quarterly | Manual | — |

---

## Restore

A backup that has never been restored is not a backup. After any significant change to the backup script or data layout, a restore test should be performed on a non-production system or by restoring a single non-critical service.

The `restore-services.sh` script handles individual service restores. For named Docker volumes, it uses an Alpine container to unpack the archive into the volume before starting the stack.

See the [Runbook](Runbook) for per-service restore commands.
