# Restore

How to recover services from a backup snapshot created by `backup-services.sh`. All restore operations go through `restore-services.sh` -- an interactive script that selects a snapshot, toggles individual services, verifies the required files exist, and requires explicit confirmation before touching any live data.

---

## Before you start

```bash
# 1. Connect the WD My Passport USB SSD
ls /mnt/backup/                    # should list YYYY-MM-DD directories

# 2. Confirm the backup drive is mounted
mountpoint /mnt/backup             # should print "... is a mountpoint"

# 3. Confirm Docker is running
docker info >/dev/null && echo ok
```

If `/mnt/backup` is not mounted:

```bash
sudo mount /dev/disk/by-uuid/<uuid> /mnt/backup
# or trigger via fstab:
sudo mount -a
```

---

## Step 1 -- Verify the backup first

Run `verify-backup.sh` before any restore. It checks archive integrity, database validity, disk space, and last backup timestamps -- without extracting or modifying anything.

```bash
# Full verification (reads every archive with tar -tzf)
sudo /usr/local/bin/verify-backup.sh

# Quick check -- existence + size only, no tar integrity scan
sudo /usr/local/bin/verify-backup.sh --quick

# Verify a specific snapshot instead of the latest
sudo /usr/local/bin/verify-backup.sh --date=2026-03-15

# Check a single service only
sudo /usr/local/bin/verify-backup.sh --only=vaultwarden
```

The script exits 0 if all checks pass (warnings are non-fatal). Exit code 1 means at least one check failed -- do not restore from a snapshot that fails verification without understanding why.

`verify-backup.sh` also writes `backup_verify.prom` to the Node Exporter textfile collector so the last verify result is visible in Grafana.

**Checks performed:**

| Check | What it does |
|---|---|
| Archive integrity | `tar -tzf` reads the full file list without extracting |
| Vaultwarden SQLite | `PRAGMA integrity_check` on the backup copy |
| Nextcloud DB dump | Validates MariaDB dump header |
| Disk space | Warn ≥ 70%, fail ≥ 85% on both SSDs |
| Last backup run | Reads start + finish timestamps from the backup log |

---

## Step 2 -- Run the restore

```bash
sudo /usr/local/bin/restore-services.sh
```

The script is fully interactive:

1. **Snapshot selection** -- lists all available `YYYY-MM-DD` directories, newest first, with sizes. Default is the latest.
2. **Service toggle menu** -- numbered list of all services. Toggle individual services or press `a` to select/deselect all. Press Enter to confirm.
3. **File pre-check** -- verifies all required backup files are present (follows `.SKIPPED` markers to older snapshots) before touching any live data.
4. **Confirmation prompt** -- shows the selected snapshot and services. Type `yes` to proceed.
5. **Restore execution** -- each service is restored in sequence with a spinner and per-step timing.
6. **Summary** -- pass/fail count and log path on exit.

Logs are written to `/var/log/restore-services.log`.

### Restoreable services

| # | Service | Backup files |
|---|---|---|
| 1 | Vaultwarden | `vaultwarden-data.tar.gz`, `vaultwarden-db.sqlite3` |
| 2 | Caddy TLS certificates | `caddy-data.tar.gz` |
| 3 | Calibre Library | `calibre-library.tar.gz` |
| 4 | Calibre-Web config | `calibre-web-config.tar.gz` |
| 5 | KOSync | `kosync-data.tar.gz` |
| 6 | Syncthing | `syncthing-obsidian.tar.gz`, `syncthing-config.tar.gz` |
| 7 | Aegis 2FA backup | `aegis-backup.tar.gz` |
| 8 | Gitea | `gitea-data.tar.gz` |
| 9 | Nextcloud | `nextcloud-data.tar`, `nextcloud-db.sql` |
| 10 | Grafana | `grafana-data.tar.gz` |
| 11 | Prometheus | `prometheus-data.tar.gz` |
| 12 | Stack configs | `stacks-config.tar.gz` |

---

## Special handling per service

### Vaultwarden

The SQLite database (`vaultwarden-db.sqlite3`) is restored first -- this is the critical file containing all vault entries. The full data directory (attachments, sends, organization data) follows. The container is stopped before restore and restarted after.

### Nextcloud

Most complex restore in the set. The DB container stays up during the entire operation; only the app container stops.

1. Maintenance mode is enabled via `occ` before the app stops
2. File data is extracted from `nextcloud-data.tar` (uncompressed -- photos and videos are already compressed)
3. MariaDB dump is piped directly into the running database container
4. App container restarts, maintenance mode is disabled

A `trap` ensures maintenance mode is always lifted even if the restore fails mid-way. If data and DB archives come from different snapshots (possible after a partial backup failure), the script warns but continues -- a version mismatch is better than no restore.

### Named Docker volumes (Calibre-Web config, KOSync)

Named volumes cannot be restored with a plain `tar` on the host path. The script uses a temporary Alpine container with the volume and the backup directory both mounted:

```bash
docker run --rm \
  -v <volume-name>:/volume \
  -v /mnt/backup/<date>:/backup \
  alpine sh -c "rm -rf /volume/* && tar -xzf /backup/<archive>.tar.gz -C /volume"
```

### Aegis 2FA backup

No container to stop -- Aegis backups are encrypted JSON files stored in the Syncthing-watched folder. Restoring them places the file back in the watched directory; Syncthing syncs it back to Android on the next connection.

### Stack configs

`~/stacks/` is a symlink to `~/homelab-infra/mnemosyne/stacks/`. The restore script extracts the archive to `/` -- `tar` follows the symlink and writes to the actual repo path. This overwrites the live working tree. The symlink must be in place before restoring.

> After restoring stack configs, run `git status` in the repo to review what changed relative to the remote.

---

## Scenario playbooks

### Scenario A -- Single service failure

*"Vaultwarden database corrupt, everything else running"*

```bash
sudo /usr/local/bin/verify-backup.sh --only=vaultwarden
sudo /usr/local/bin/restore-services.sh
# → select latest snapshot
# → toggle: 1 (Vaultwarden only)
# → confirm
```

Expected downtime: 5–15 minutes.

### Scenario B -- SSD failure (data loss, hardware intact)

*"Codex SSD dead. Mnemosyne boots, Docker runs, but all service data is gone."*

1. Replace SSD, format, mount at `/mnt/codex`
2. Recreate required directories (Docker will handle the rest on first start):

```bash
sudo mkdir -p /mnt/codex/{docker,containerd,nextcloud/{data,db},gitea/data,immich/{upload,db},ghost/content,grafana,prometheus,alertmanager,wakapi,calibre-library,syncthing,netatmo-exporter,tado-exporter}
```

3. Restore all services:

```bash
sudo /usr/local/bin/restore-services.sh
# → select latest snapshot
# → press 'a' to select all
# → confirm
```

4. Restart all stacks:

```bash
for stack in ~/stacks/*/; do
  (cd "$stack" && docker compose up -d)
done
```

Expected downtime: 2–3 hours depending on Nextcloud data volume.

### Scenario C -- Complete hardware failure

*"Pi hardware dead. New Pi ordered and arrived."*

1. Flash SD card, reinstall OS, configure SSD mounts
2. Clone the repo: `git clone <gitea-url> ~/homelab-infra`
3. Restore symlinks: `ln -s ~/homelab-infra/mnemosyne/stacks ~/stacks`
4. Copy `.env` files to each stack directory (from a separate secret store or by recreating them)
5. Follow Scenario B from step 3

Expected downtime: ~1 day (delivery) + 2–3 hours rebuild.

---

## After restore

```bash
# Check all containers came back up
docker ps --format "table {{.Names}}\t{{.Status}}"

# Nextcloud -- re-scan files after restore
docker exec -u www-data nextcloud php occ files:scan --all

# Vaultwarden -- verify login works in a browser before closing the terminal
# https://vault.home

# Caddy -- TLS certificates restored; no reload needed (container restart handles it)
docker logs caddy --tail 20

# Gitea -- verify repositories and settings
# https://git.home

# Check restore log for any warnings
tail -50 /var/log/restore-services.log
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/mnt/backup` not mounted | USB SSD not connected or mount failed | Connect drive; `sudo mount -a` |
| `file not found` during restore | Archive missing and no `.SKIPPED` marker | That service was never backed up to that snapshot; try an older date |
| `.SKIPPED` marker but referenced archive missing | Older snapshot was pruned | Pick an earlier snapshot that still has the archive |
| Nextcloud restore fails mid-way | DB container not running | `docker start nextcloud-db` then re-run restore |
| Maintenance mode left on after failed restore | `trap` did not fire | `docker exec -u www-data nextcloud php occ maintenance:mode --off` |
| Named volume restore fails | Volume does not exist | `docker volume create <name>` then re-run restore |
| Stack configs restore overwrites repo changes | Expected -- archive is the backup copy | After restore, `git diff` and `git stash` if needed |
| `sqlite3: command not found` | sqlite3 not installed (verify-backup only) | `sudo apt install sqlite3` |
