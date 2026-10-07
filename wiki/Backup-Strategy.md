# Backup Strategy

## Principle: 3-2-1

**3** copies of the data -- **2** different media -- **1** copy offsite. Until all three conditions are met, it is not a backup.

| Copy | Medium | Location |
|---|---|---|
| Live data | NVMe SSD (`/mnt/codex`, `/mnt/vault`) | Mnemosyne |
| Local backup | WD My Passport USB SSD (`/mnt/backup`) | Mnemosyne, same room |
| Offsite backup | restic repository on a Hetzner Storage Box | Hetzner, Germany/Finland |

---

## What Gets Backed Up

`backup-services.sh` defines every source in one `SOURCE_PATHS` map. A drift check compares that map against the bind mounts of all running containers and raises `BackupMountDrift` when a mount under `/mnt/codex` or `/mnt/vault` is neither backed up nor explicitly excluded.

| Service | Source | Method | Priority |
|---|---|---|---|
| Vaultwarden | `/mnt/vault/vaultwarden/data/` | `sqlite3 .backup` + tar | Critical |
| Caddy TLS | `/mnt/vault/caddy/` | tar | High |
| Nextcloud DB | `nextcloud-db` container | `mariadb-dump` in maintenance mode | Critical |
| Nextcloud files | `/mnt/codex/nextcloud/data/` | tar `--sort=name`, uncompressed | High |
| Immich DB | `immich-db` container | `pg_dumpall` | High |
| Immich uploads | `/mnt/codex/immich/upload/` | tar `--sort=name`, uncompressed | High |
| Ghost DB | `ghost-db` container (MySQL 8.0) | `mysqldump` | High |
| Ghost content | `/mnt/codex/ghost/content/` | tar | High |
| Gitea | `/mnt/codex/gitea/data/` | container stopped, tar | High |
| Gitea Act Runner | `/mnt/codex/gitea/runner/` | tar | Medium |
| Syncthing vault | `/mnt/codex/syncthing/obsidian/` | tar | High |
| Syncthing config | `~/.local/state/syncthing/` | tar | High |
| Aegis 2FA export | `/mnt/codex/syncthing/aegis/` | tar | Critical |
| Grafana | `/mnt/codex/grafana/` | container stopped, tar | Medium |
| JobIris | `/mnt/vault/jobiris/` | tar | Medium |
| Calibre library | `/mnt/codex/calibre-library/` | tar | Medium |
| Calibre-Web config | `/mnt/codex/calibre-web-config/` | tar | Medium |
| KOSync | `/mnt/codex/kosync/data/` | tar | Low |
| Exporter tokens | Tado, Midea, Netatmo token directories | tar | Medium |
| Stack configs | `~/stacks/` | tar (follows the symlink) | Critical |

**Deliberately not backed up**

| Data | Reason |
|---|---|
| Prometheus TSDB | Expendable -- time series refill within hours of a rebuild. Removed from the backup 2026-08-19. |
| Loki, Alloy | Log storage and collector state, rebuilt on start |
| Alertmanager | Silences and notification state, rebuilt from config; a lost silence expires anyway |
| Carousel jobs | Regenerable render artefacts |
| Wakapi | **Accepted loss.** Not regenerable, but judged not worth a backup step (decided 2026-08-30). |

**Stack configs are critically underrated.** The `docker-compose.yml` and `Caddyfile` are small files, but without them a restore takes hours instead of minutes.

**Databases are dumped, never tar'd live.** Running `tar` on a live database directory produces corrupt backups. Nextcloud is put into maintenance mode for its dump, with a `trap ... EXIT` so maintenance mode is always disabled even if the script crashes. Gitea and Grafana have no online backup API and use SQLite, so their containers are stopped briefly instead.

**Large archives are uncompressed and deterministic.** Photos and videos do not benefit from gzip. `--sort=name` keeps member order stable between nights, so restic deduplicates the near-identical `nextcloud-data.tar` and `immich-upload.tar` to near-zero upload instead of re-sending ~73 GB.

---

## Failure Scenarios

### Scenario A -- Single service failure
*"Vaultwarden database corrupt, everything else running"*

Stop the affected service, restore from the local backup with `restore-services.sh`, restart. Expected downtime: 5–15 minutes.

### Scenario B -- SSD failure
*"Pi won't boot, hardware otherwise intact"*

Replace SSD, reinstall OS, restore all services from the local backup. Expected downtime: 2–3 hours.

### Scenario C -- Complete hardware failure
*"Pi hardware dead, replacement needed"*

New Pi, reinstall OS, restore all services from the local backup. Expected downtime: ~1 day (delivery time).

### Scenario D -- Site loss
*"Pi and backup SSD gone at the same time"*

Theft, fire, water damage -- anything that takes out one room. The backup SSD sits physically next to Mnemosyne, so only the offsite copy survives. This is the scenario the offsite layer exists for, not drive failure alone.

Restore path: pull the tarball tree out of the restic repository onto a new drive mounted at `/mnt/backup`, then follow the normal restore procedure. See [Offsite restore](#offsite-restore-scenario-d) below.

---

## Backup Layers

### Layer 1 -- Local backup (daily, 02:00)

`backup-services.sh` archives all service data to the USB SSD at `/mnt/backup`. Each run creates a dated directory `/mnt/backup/<YYYY-MM-DD>/`. Archives older than **7 days** are deleted automatically (`--retention=<days>` overrides this).

It runs as `backup-services.service`, started by `backup-services.timer` at 02:00 (`Persistent=true`, so a night missed while the Pi was off is caught up on boot). Moved from cron to systemd on 2026-09-03 for three reasons: the output lands in the journal and therefore in Loki, `ExecStopPost=` records the unit outcome even when the script never starts, and manual test runs (`systemd-run`) now match the real execution path.

Unchanged services are skipped via change detection and leave a `.SKIPPED` marker pointing to the last real archive -- required because exFAT supports neither hardlinks nor symlinks.

The Nextcloud database password is not stored in the script. It is loaded from `/etc/backup-secrets.conf` (mode `600`, root-only), which must be created manually on a new system:

```bash
echo 'NEXTCLOUD_DB_PW="your_password"' | sudo tee /etc/backup-secrets.conf
sudo chmod 600 /etc/backup-secrets.conf
```

### Layer 2 -- Offsite backup (restic, daily)

restic snapshots the **local tarball tree** (`/mnt/backup`), not the live service data. The tarballs are already application-consistent (maintenance mode, database dumps), so the offsite copy inherits that consistency without any extra coordination.

**Trigger.** On a run with zero errors, `backup-services.sh` starts `restic-offsite.service` with `systemctl start --no-block` and exits. A broken local backup is never shipped offsite. `restic-offsite.timer` fires at 06:00 as a **fallback only**, for nights where the local backup did not run or the trigger was missed -- the failure mode that went unnoticed for three nights with the previous rclone setup.

**What the daily run does.** `restic-offsite.sh` runs `restic backup` (incremental, deduplicated) followed by `restic forget` with the retention policy below. It does **not** prune -- that is RAM- and I/O-heavy on the Pi and runs weekly.

| Retention | Snapshots kept |
|---|---|
| Daily | 7 |
| Weekly | 4 |
| Monthly | 6 |

Because restic deduplicates, monthly snapshots cost almost nothing extra over daily ones -- identical blocks are shared.

**Weekly maintenance.** `restic-maintenance.service` (Sundays 05:00) runs `restic prune` to reclaim space, then `restic check --read-data-subset=2%`, which pulls a rotating 2% of the pack files back from the Storage Box and verifies them against their hashes. Over roughly a year the whole repository is re-read. 10% was tried first and reliably killed the SSH connection mid-transfer (~71 GiB in one stream); a check that completes beats a more thorough one that always fails.

**Mutual exclusion.** Backup and maintenance both `flock` the same `/var/run/restic-repo.lock`; the second process exits immediately instead of running against a repository in use. This replaced a `Conflicts=` directive that resolved overlaps by killing the running backup.

**Encryption.** restic encrypts all data and metadata client-side before upload. Hetzner only ever sees ciphertext. The repository password lives in `/root/.config/restic/password` and is the single point of failure: lose it and the offsite copy is unrecoverable. It is stored in Vaultwarden **and** in one offline copy outside the house.

**Why restic replaced rclone crypt.** The previous design synced the tarball tree with `rclone sync` through a `crypt` remote as an inline step of `backup-services.sh`. Two problems forced the switch: without deduplication every night re-uploaded the full ~73 GB Nextcloud tarball, which a home uplink cannot sustain, and as an inline step an offsite failure was buried in the local backup's log and metrics. restic deduplicates, keeps versioned snapshots instead of a single mirror, and can verify the remote data itself (`check --read-data-subset`). The old `hetzner-crypt:` remote has been purged. Full setup and cutover notes: `mnemosyne/scripts/SETUP_Offsite.md` in the repository.

### Layer 3 -- System image (monthly, manual)

`rpi-clone` creates a full SD card / SSD image to a second drive. Covers the OS and all configuration outside the data directories.

---

## Backup Medium

The local backup target is a USB SSD mounted at `/mnt/backup` (exFAT). The `nofail` mount option is required -- without it, a missing drive blocks the boot process.

```
/etc/fstab entry:
UUID=<uuid>  /mnt/backup  exfat  defaults,nofail,uid=1000,gid=1000,umask=022  0  0
```

`backup-services.service` declares `RequiresMountsFor=/mnt/backup /mnt/codex /mnt/vault` and `restic-offsite.service` declares `RequiresMountsFor=/mnt/backup`, so neither starts against an empty mount point.

---

## Schedule

| What | When | Mechanism |
|---|---|---|
| Local backup | Daily 02:00 | `backup-services.timer` → `backup-services.service` |
| Offsite backup | After a successful local backup | `backup-services.sh` → `restic-offsite.service` |
| Offsite fallback | Daily 06:00 | `restic-offsite.timer` (no-op if the trigger already ran) |
| Backup verification | Daily 04:00 | Gitea Action `backup-verify.yml` runs `verify-backup.sh` on Mnemosyne via SSH |
| Disk space check (ntfy alert) | Daily 08:00 | Host cron (not in this repo) |
| restic prune + check | Sundays 05:00 | `restic-maintenance.timer` |
| System image (rpi-clone) | Monthly | Manual |
| Restore test | Quarterly | Manual |

```bash
systemctl list-timers 'backup-*' 'restic-*'
```

---

## Monitoring

Every stage writes Prometheus metrics to the node-exporter textfile collector, and each systemd unit additionally records its own outcome via `ExecStopPost=/usr/local/bin/restic-unit-metrics.sh`. That second writer exists because a script that cannot start cannot report that it did not start -- on 2026-08-20 three `203/EXEC` failures went unnoticed while the script's own metrics kept reporting a six-week-old success.

| File | Written by | Key metrics |
|---|---|---|
| `backup.prom` | `backup-services.sh` | `backup_last_success_timestamp`, `backup_last_exit_code`, `backup_step_status`, `backup_archive_size_bytes`, `backup_uncovered_mounts` |
| `backup_verify.prom` | `verify-backup.sh` | `backup_verify_last_run_timestamp`, `backup_verify_fail_count` |
| `backup_services_unit.prom` | `restic-unit-metrics.sh` | `backup_services_unit_success` |
| `restic_offsite.prom` | `restic-offsite.sh` | `restic_offsite_last_success_timestamp`, `restic_offsite_exit_code`, `restic_offsite_forget_ok`, `restic_offsite_bytes_added`, `restic_offsite_snapshot_count` |
| `restic_offsite_unit.prom` | `restic-unit-metrics.sh` | `restic_offsite_unit_success` |
| `restic_maintenance.prom` | `restic-maintenance.sh` | `restic_maintenance_prune_ok`, `restic_maintenance_check_ok` |
| `restic_maintenance_unit.prom` | `restic-unit-metrics.sh` | `restic_maintenance_unit_success` |

Alert rules live in `mnemosyne/stacks/monitoring/prometheus/backup-alerts.yml`. The most important ones:

| Alert | Fires when | Severity |
|---|---|---|
| `BackupStale` | No successful local backup for 30 h | critical |
| `BackupUnitFailed` | `backup-services.service` failed at unit level | critical |
| `BackupArchiveSuspiciouslySmall` | An archive is under 1 KiB | critical |
| `BackupMountDrift` | A container bind mount is not covered by the backup | warning |
| `BackupVerifyFailed` / `BackupVerifyStale` | Verification failed / did not run for 30 h | critical / warning |
| `OffsiteBackupStale` | No successful offsite backup for 72 h | critical |
| `OffsiteBackupFailed` | `restic backup` failed (exit code 2) | critical |
| `OffsiteRetentionFailed` | `restic forget` failed -- data is safe, retention is not | warning |
| `MaintenanceStale` / `MaintenanceUnitFailed` | Weekly prune/check missing or failed | warning |
| `BackupDiskFillingUp` / `BackupDiskCritical` | Under 500 GB / 200 GB free on the backup SSD | warning / critical |

---

## Restore

A backup that has never been restored is not a backup. After any significant change to the backup script or data layout, a restore test should be performed on a non-production system or by restoring a single non-critical service.

### Local restore (scenarios A–C)

`restore-services.sh` handles service restores from `/mnt/backup`: it selects a snapshot, toggles individual services, checks that the required files exist, and touches nothing until you type `yes`. See [Restore](Restore) and the [Runbook](Runbook) for per-service details.

### Offsite restore (scenario D)

Prerequisites, all of which must be available **without** Mnemosyne:

| Needed | Where it lives |
|---|---|
| restic repository password | Vaultwarden (cached in every client app) + offline copy |
| Storage Box SSH access | Key `~/.ssh/hetzner_storagebox` is lost with the Pi -- add a new public key via the Hetzner console |
| Repository location | `RESTIC_REPOSITORY` from `restic-offsite.env.example`, Storage Box user from the Hetzner console |

```bash
# 1. New drive mounted at /mnt/backup, restic installed
sudo apt install restic -y

# 2. Recreate config and password (values from Vaultwarden / Hetzner console)
sudo mkdir -p /etc/restic /root/.config/restic
sudo cp restic-offsite.env.example /etc/restic/restic-offsite.env   # then edit
sudo nano /root/.config/restic/password && sudo chmod 600 /root/.config/restic/password

# 3. Load the config and list snapshots
sudo -i
set -a; source /etc/restic/restic-offsite.env; set +a
restic -o "sftp.command=${RESTIC_SFTP_COMMAND}" snapshots --tag offsite

# 4. Restore the tarball tree. Snapshots store absolute paths, so
#    --target / writes back to /mnt/backup/<YYYY-MM-DD>/...
restic -o "sftp.command=${RESTIC_SFTP_COMMAND}" restore latest --tag offsite --target /
```

Then continue with `restore-services.sh` as for a local restore. To pull only one day instead of the whole retention window, add `--include /mnt/backup/<YYYY-MM-DD>` -- but follow any `.SKIPPED` markers in that directory and include the dates they point to as well.

For a non-destructive check of the offsite copy without restoring anything, mount the repository read-only:

```bash
restic -o "sftp.command=${RESTIC_SFTP_COMMAND}" mount /mnt/restic-browse
tar -tf /mnt/restic-browse/snapshots/latest/mnt/backup/*/nextcloud-data.tar | head
fusermount -u /mnt/restic-browse
```
