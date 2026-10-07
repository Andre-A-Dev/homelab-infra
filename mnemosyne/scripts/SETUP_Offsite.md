# restic Offsite — One-Time Setup & Deployment

> **Status: deployed.** restic is the live offsite backup; the rclone remote
> `hetzner-crypt:` has been purged. This document is now the reference for
> setting the offsite backup up **from scratch** — on a new Mnemosyne, or after
> losing the repository. For day-to-day operation see `RUNBOOK.md` → *Offsite
> Backup*; for a disaster restore see `wiki/Backup-Strategy.md` → *Offsite
> restore*.
>
> Original precondition, kept for context: the cutover only happened after the
> 524 GB rclone backlog had finished and the local tarball restore had been
> validated (KOSync test).

---

## 0. Prerequisites

- `restic` installed: `sudo apt install restic -y` (verify `restic version` ≥ 0.16)
- The existing SSH key `~/.ssh/hetzner_storagebox` already works (proven today)
- `python3` present (already used across the stack)

---

## 1. The password — do this FIRST, before anything else

The repo password is the single point of failure. No password = no restore,
permanently. There is no recovery path. Create it, then back it up in **two**
places off this machine *before* the first backup runs.

```bash
sudo mkdir -p /root/.config/restic
# Generate a strong password and write it directly to the file:
openssl rand -base64 32 | sudo tee /root/.config/restic/password
sudo chmod 600 /root/.config/restic/password
```

Now copy that exact string into:
1. **Vaultwarden** — a secure note labelled "restic offsite repo password"
2. **One offline copy** — printed or on an encrypted USB stick kept elsewhere

Only after both copies exist should you continue.

---

## 2. Deploy the files

```bash
# Scripts → /usr/local/bin (symlinked from the repo, matching your convention)
sudo cp restic-offsite.sh restic-maintenance.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/restic-offsite.sh /usr/local/bin/restic-maintenance.sh

# Config
sudo mkdir -p /etc/restic
sudo cp restic-offsite.env.example /etc/restic/restic-offsite.env
sudo chmod 600 /etc/restic/restic-offsite.env
sudo nano /etc/restic/restic-offsite.env   # verify host, user, key path

# Cache directory (referenced by the units' ReadWritePaths)
sudo mkdir -p /var/cache/restic

# systemd units
sudo cp restic-offsite.service restic-offsite.timer \
        restic-maintenance.service restic-maintenance.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
```

---

## 3. Initialize the repository (one time only)

This creates the repo structure on the Storage Box. It reads the env file for
the location and password.

```bash
set -a; source /etc/restic/restic-offsite.env; set +a
restic -o "sftp.command=${RESTIC_SFTP_COMMAND}" init
```

Expected output: `created restic repository <id> at sftp:...`. If it says the
repo already exists, it was initialized before — do not re-init, that would
orphan the existing data.

---

## 4. First backup — run it manually, watch it

Do NOT let the timer fire the first run blind. Run it by hand so you see the
initial upload (this first snapshot uploads the real data; subsequent ones are
deltas only).

```bash
sudo systemctl start restic-offsite.service
# In another terminal, watch:
journalctl -u restic-offsite.service -f
```

The first run uploads the current tarballs in full. After it completes:

```bash
# Confirm the snapshot exists
set -a; source /etc/restic/restic-offsite.env; set +a
restic -o "sftp.command=${RESTIC_SFTP_COMMAND}" snapshots
```

---

## 5. Validate the restore BEFORE trusting it

This is the whole point of switching — restic makes restore-testing
non-destructive via a read-only mount. Do this before enabling the timers.

```bash
sudo mkdir -p /mnt/restic-browse
# Mount the repo read-only as a filesystem (needs fuse: apt install fuse3)
restic -o "sftp.command=${RESTIC_SFTP_COMMAND}" mount /mnt/restic-browse &

# In another terminal — browse and verify a tarball's integrity WITHOUT
# writing anything to production paths:
ls /mnt/restic-browse/snapshots/latest/mnt/backup/
tar -tf /mnt/restic-browse/snapshots/latest/mnt/backup/*/nextcloud-data.tar | head

# Unmount when done
fusermount -u /mnt/restic-browse
```

If `tar -tf` lists the archive contents cleanly, the offsite copy is intact and
restorable — proven without touching a single live service.

---

## 6. Enable the timers

Only after steps 4 and 5 pass:

```bash
sudo systemctl enable --now restic-offsite.timer
sudo systemctl enable --now restic-maintenance.timer
sudo systemctl list-timers | grep restic
```

---

## 7. Wire backup-services.sh to trigger restic (replaces the old rclone step)

**Status: already done.** The rclone offsite step, `OFFSITE_REMOTE`, and
`RCLONE_CONFIG_PATH` have been removed from `backup-services.sh`, and the
restic trigger below is already in place and committed
(`5e97428 Refactor backup system to use restic for offsite backups`). This
section is kept as a reference for what that change actually did — not as a
pending task.

```bash
# ── Trigger offsite (restic, decoupled) ─────────────────────────────────────
# Fire-and-forget: restic runs as its own service so its success/failure is
# tracked independently and never affects this script's exit code. The daily
# fallback timer covers the case where this trigger is missed.
if [ "$NO_OFFSITE" != true ] && [ "$ERRORS" -eq 0 ]; then
  systemctl start --no-block restic-offsite.service \
    && log "  Offsite: triggered restic-offsite.service" \
    || log "  Offsite: WARN — could not trigger restic (check systemctl)"
fi
```

The `--no-offsite` flag now skips the *trigger*, not an inline sync.

> **Correction — this originally said to run restic alongside the old rclone
> step during a transition period. That is not viable on a home uplink: both
> the old rclone catch-up and a first restic backup take multi-hour uploads of
> comparable size, and running them concurrently would have them fight over
> the same limited bandwidth rather than provide real redundancy. This was a
> planning mistake, not a tested recommendation — it has since been corrected
> to a clean cutover (see the Rollback section below for what the actual
> safety net is instead).**

---

## 8. Add one crucial change to the nextcloud tar (for dedup stability)

In backup-services.sh, the nextcloud data archive must pack in a deterministic
order or restic's deduplication breaks between runs:

```bash
# Before:
run_tar tar -cf "$BACKUP_DIR/$DATE/nextcloud-data.tar" /mnt/codex/nextcloud/data/
# After:
run_tar tar --sort=name -cf "$BACKUP_DIR/$DATE/nextcloud-data.tar" /mnt/codex/nextcloud/data/
```

`--sort=name` makes member order stable, so the 73 GB tarball deduplicates
cleanly across snapshots. Without it, shifting byte offsets defeat dedup and
every night uploads the full 73 GB anyway — the entire reason for switching.

---

## Rollback

The old rclone remote (`hetzner-crypt:`) has been **purged** — it is not a
fallback anymore. The actual safety net during the restic transition is
narrower than originally planned:

1. **The local tarballs on `/mnt/backup` are untouched** — restic reads them,
   never modifies or deletes the source. This is the real fallback: a local
   restore via `restore-services.sh` works regardless of restic's state,
   proven working end-to-end (see the Calibre restore test).
2. **No current offsite copy exists until step 4 (first restic backup)
   completes.** This is a real, accepted gap — see the note in step 7 above.
   Keep it short: do not delay step 3 (repo init) and step 4 (first backup)
   once you start this process.

If restic misbehaves after that, the local tarballs remain the recovery path;
reverting `backup-services.sh` to a pre-restic commit and re-provisioning an
rclone remote is possible but is a rebuild, not a one-line revert — the old
remote's data is gone. Nothing here is irreversible except losing the restic
repo password (step 1).
