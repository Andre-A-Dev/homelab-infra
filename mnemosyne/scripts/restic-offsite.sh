#!/bin/bash
# =============================================================================
# restic-offsite.sh
# =============================================================================
# Daily offsite backup of the local tarball tree (/mnt/backup) to the Hetzner
# Storage Box via restic. Runs as a systemd oneshot service, NOT interactively:
# no spinner, no colored TTY output — structured stdout goes to the journal.
#
# Responsibilities (deliberately narrow):
#   1. restic backup   — snapshot the local tarballs (dedup + incremental)
#   2. restic forget   — apply snapshot retention (NO prune — see maintenance)
#   3. write metrics   — Prometheus textfile collector, atomically
#
# Explicitly NOT done here:
#   - prune (heavy on the Pi; separate weekly restic-maintenance.sh)
#   - producing the tarballs (that's backup-services.sh, which triggers this)
#
# Exit codes:
#   0 — backup and forget both succeeded
#   1 — configuration / preflight error (nothing ran)
#   2 — backup failed (metrics still written)
#   3 — backup succeeded but forget failed (data is safe, retention is not)
#
# 2 and 3 are separate because they need different responses: 2 means no new
# offsite copy exists, 3 means the copy exists but old snapshots are piling up.
# Conflating them meant a six-week retention outage looked exactly like a
# failed backup.
# =============================================================================

set -uo pipefail

# ── Load configuration ──────────────────────────────────────────────────────
ENV_FILE="/etc/restic/restic-offsite.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: environment file not found: $ENV_FILE" >&2
  exit 1
fi
set -a
# shellcheck source=/dev/null
# The directive has to sit directly above `source` — it binds to the next
# command, and it was previously placed above `set -a`, where it did nothing.
source "$ENV_FILE"
set +a

# Export the SFTP command so restic's sftp backend uses our explicit ssh
# invocation instead of any user-context ssh config alias.
export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE RESTIC_CACHE_DIR
RESTIC_OPTS=(-o "sftp.command=${RESTIC_SFTP_COMMAND}")

METRICS_FILE="${RESTIC_METRICS_DIR}/restic_offsite.prom"

# One lock for every process that touches this repository — backup AND
# maintenance. Previously each script used its own lock file while
# restic-maintenance.sh claimed in a comment to share this one, so the two
# never actually excluded each other. The unit files papered over that with
# Conflicts=, which "resolves" an overlap by KILLING the running backup: the
# 2026-08-20 run took 6h10m, so a nightly run starting at 02:16 would still be
# going when Sunday's 05:00 maintenance fired.
LOCK_FILE="/var/run/restic-repo.lock"

# ── Concurrency guard ───────────────────────────────────────────────────────
# A slow backup still uploading when the next trigger (or fallback timer) fires
# must not start a second restic against the same repo.
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo "ERROR: another restic process holds the repo lock — exiting." >&2
  exit 1
fi

# ── Metric state ────────────────────────────────────────────────────────────
RUN_TS=$(date +%s)
EXIT_CODE=0
DURATION=0
FILES_NEW=0
FILES_CHANGED=0
BYTES_ADDED=0
SNAPSHOT_COUNT=0
BACKUP_OK=0
FORGET_OK=0

# ── Preflight: is the repository reachable and initialized? ──────────────────
echo "[$(date '+%F %T')] restic-offsite starting"
# stderr is NOT suppressed: when this fails, the reason is the single most
# useful line in the whole run. Only stdout goes to /dev/null.
if ! restic "${RESTIC_OPTS[@]}" snapshots --no-lock --last >/dev/null; then
  echo "ERROR: repository not reachable or not initialized: $RESTIC_REPOSITORY" >&2
  echo "       Run the one-time 'restic init' first (see setup notes)." >&2
  EXIT_CODE=1
  # Fall through to write metrics so the failure is visible in Prometheus.
fi

# ── Clear stale repository locks ────────────────────────────────────────────
# A restic process killed mid-run leaves a lock in the repo. backup and
# snapshots do not need an exclusive lock and keep working, so only forget
# fails — which is why a lock from 2026-07-11 blocked every retention run for
# six weeks without anything turning red.
#
# `restic unlock` (without --remove-all) removes only STALE locks: those whose
# owning process is gone, or that stopped being refreshed. Live locks are left
# alone. Combined with the flock above, no other local restic can be running
# at this point anyway.
if [ "$EXIT_CODE" -eq 0 ]; then
  if ! restic "${RESTIC_OPTS[@]}" unlock; then
    echo "WARNING: could not clear stale locks — forget may fail below" >&2
  fi
fi

# ── Backup ──────────────────────────────────────────────────────────────────
if [ "$EXIT_CODE" -eq 0 ]; then
  START=$(date +%s)
  echo "[$(date '+%F %T')] backing up ${RESTIC_BACKUP_SOURCE}"

  # --json emits a final summary object we parse for metrics. Capture stdout;
  # let stderr flow to the journal for live diagnostics.
  SUMMARY=$(restic "${RESTIC_OPTS[@]}" backup "$RESTIC_BACKUP_SOURCE" \
    --tag offsite --tag automated \
    --json 2> >(cat >&2) | tail -n 1)
  BACKUP_RC=$?
  DURATION=$(( $(date +%s) - START ))

  if [ "$BACKUP_RC" -eq 0 ] && [ -n "$SUMMARY" ]; then
    BACKUP_OK=1
    # Parse the summary JSON with python3 (always present on this stack; avoids
    # a jq dependency). Missing fields default to 0 rather than aborting.
    read -r FILES_NEW FILES_CHANGED BYTES_ADDED < <(
      echo "$SUMMARY" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get("files_new", 0), d.get("files_changed", 0), d.get("data_added", 0))
'
    )
    echo "[$(date '+%F %T')] backup OK — new:$FILES_NEW changed:$FILES_CHANGED added:${BYTES_ADDED}B in ${DURATION}s"
  else
    echo "ERROR: restic backup failed (rc=$BACKUP_RC)" >&2
    EXIT_CODE=2
  fi
fi

# ── Forget (snapshot retention, NO prune) ───────────────────────────────────
# forget only rewrites the snapshot list — cheap. The actual reclaiming of
# space (prune) is intentionally deferred to the weekly maintenance run, which
# is I/O- and RAM-heavy and should not sit in the daily critical path.
if [ "$BACKUP_OK" -eq 1 ]; then
  echo "[$(date '+%F %T')] applying retention (forget, no prune)"
  # stderr deliberately NOT suppressed. The previous `2>&1` here hid this for
  # six weeks:
  #   unable to create lock in backend: repository is already locked by PID …
  #   lock was created at 2026-07-11 02:55:35 (975h22m30s ago)
  #   the `unlock` command can be used to remove stale locks
  # A complete, self-explanatory error that named its own fix, written to
  # /dev/null every single night.
  if restic "${RESTIC_OPTS[@]}" forget \
      --tag offsite \
      --keep-daily "$RESTIC_KEEP_DAILY" \
      --keep-weekly "$RESTIC_KEEP_WEEKLY" \
      --keep-monthly "$RESTIC_KEEP_MONTHLY" >/dev/null; then
    FORGET_OK=1
    echo "[$(date '+%F %T')] forget OK"
  else
    echo "ERROR: restic forget failed — data is safe, retention is not" >&2
    [ "$EXIT_CODE" -eq 0 ] && EXIT_CODE=3
  fi
fi

# ── Snapshot count (for the metric) ─────────────────────────────────────────
SNAPSHOT_COUNT=$(restic "${RESTIC_OPTS[@]}" snapshots --no-lock --json \
  | python3 -c 'import sys,json;
try: print(len(json.load(sys.stdin)))
except Exception: print(0)')
[ -z "$SNAPSHOT_COUNT" ] && SNAPSHOT_COUNT=0

# ── Write Prometheus metrics atomically ─────────────────────────────────────
# Write to a temp file then mv — node_exporter must never read a half-written
# .prom. mv within the same filesystem is atomic.
mkdir -p "$RESTIC_METRICS_DIR"
TMP_METRICS="${METRICS_FILE}.$$"
{
  echo "# HELP restic_offsite_last_run_timestamp Unix time of the last offsite run (any outcome)"
  echo "# TYPE restic_offsite_last_run_timestamp gauge"
  echo "restic_offsite_last_run_timestamp $RUN_TS"

  echo "# HELP restic_offsite_last_success_timestamp Unix time of the last successful backup"
  echo "# TYPE restic_offsite_last_success_timestamp gauge"
  if [ "$BACKUP_OK" -eq 1 ]; then
    echo "restic_offsite_last_success_timestamp $RUN_TS"
  else
    # Preserve the previous success timestamp if one exists, so a single
    # failure does not blank out "when did offsite last actually work".
    PREV=$(grep '^restic_offsite_last_success_timestamp ' "$METRICS_FILE" 2>/dev/null | awk '{print $2}')
    echo "restic_offsite_last_success_timestamp ${PREV:-0}"
  fi

  echo "# HELP restic_offsite_forget_ok 1 if the last retention run succeeded"
  echo "# TYPE restic_offsite_forget_ok gauge"
  echo "restic_offsite_forget_ok $FORGET_OK"

  echo "# HELP restic_offsite_exit_code Exit code of the last run (0=ok, 2=backup failed, 3=forget failed)"
  echo "# TYPE restic_offsite_exit_code gauge"
  echo "restic_offsite_exit_code $EXIT_CODE"

  echo "# HELP restic_offsite_duration_seconds Duration of the last backup phase"
  echo "# TYPE restic_offsite_duration_seconds gauge"
  echo "restic_offsite_duration_seconds $DURATION"

  echo "# HELP restic_offsite_files_new Files new in the last snapshot"
  echo "# TYPE restic_offsite_files_new gauge"
  echo "restic_offsite_files_new $FILES_NEW"

  echo "# HELP restic_offsite_files_changed Files changed in the last snapshot"
  echo "# TYPE restic_offsite_files_changed gauge"
  echo "restic_offsite_files_changed $FILES_CHANGED"

  echo "# HELP restic_offsite_bytes_added Bytes actually uploaded (post-dedup) in the last run"
  echo "# TYPE restic_offsite_bytes_added gauge"
  echo "restic_offsite_bytes_added $BYTES_ADDED"

  echo "# HELP restic_offsite_snapshot_count Number of snapshots currently in the repo"
  echo "# TYPE restic_offsite_snapshot_count gauge"
  echo "restic_offsite_snapshot_count $SNAPSHOT_COUNT"
} > "$TMP_METRICS"
mv "$TMP_METRICS" "$METRICS_FILE"

echo "[$(date '+%F %T')] restic-offsite finished (exit $EXIT_CODE)"
exit "$EXIT_CODE"
