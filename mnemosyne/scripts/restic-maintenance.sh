#!/bin/bash
# =============================================================================
# restic-maintenance.sh
# =============================================================================
# Weekly heavy maintenance, split off from the daily backup because both steps
# are resource-intensive and must NOT sit in the nightly critical path:
#
#   1. restic prune  — reclaim space from forgotten snapshots. RAM/I/O heavy;
#                      on a Pi this can take a while and should run at most
#                      weekly, never nightly.
#   2. restic check  — structural integrity, PLUS a rotating 10% data read that
#                      actually pulls packs back from Hetzner to catch silent
#                      bitrot. Structural check alone can't detect on-disk
#                      corruption at the provider; only re-reading data can.
#
# Exit codes:
#   0 — prune and check both clean
#   1 — configuration / preflight error
#   2 — prune or check reported a problem (metrics still written)
# =============================================================================

set -uo pipefail

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

export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE RESTIC_CACHE_DIR
RESTIC_OPTS=(-o "sftp.command=${RESTIC_SFTP_COMMAND}")

METRICS_FILE="${RESTIC_METRICS_DIR}/restic_maintenance.prom"

# The comment here used to claim this shared a lock namespace with the daily
# backup. It did not — restic-offsite.sh used /var/run/restic-offsite.lock and
# this used /var/run/restic-maintenance.lock, so the two never excluded each
# other at all. Corrected 2026-08-20: one lock file for every process that
# touches this repository.
#
# This also removes the need for Conflicts=restic-offsite.service in the unit,
# which "resolved" an overlap by killing the running backup.
LOCK_FILE="/var/run/restic-repo.lock"

exec 201>"$LOCK_FILE"
if ! flock -n 201; then
  echo "ERROR: another restic process holds the repo lock (backup still running?) — exiting." >&2
  exit 1
fi

RUN_TS=$(date +%s)
EXIT_CODE=0
PRUNE_OK=0
CHECK_OK=0
DURATION=0
START=$(date +%s)

echo "[$(date '+%F %T')] restic-maintenance starting"

# ── Prune ───────────────────────────────────────────────────────────────────
echo "[$(date '+%F %T')] pruning (reclaiming space from forgotten snapshots)"
# stderr NOT suppressed — see restic-offsite.sh for why.
if restic "${RESTIC_OPTS[@]}" prune >/dev/null; then
  PRUNE_OK=1
  echo "[$(date '+%F %T')] prune OK"
else
  echo "ERROR: restic prune failed" >&2
  EXIT_CODE=2
fi

# ── Check (structure + rotating 10% data read) ──────────────────────────────
# --read-data-subset=2% pulls a rotating fiftieth of the pack files back from
# the Storage Box and verifies them against their hashes. Over ~1 year the whole
# repo is verified, spreading the bandwidth cost instead of downloading
# everything at once.
#
# Reduced from 10% on 2026-08-21. At 10% the run pulled ~71 GiB in one stream
# and died with `ssh exit status 255`, which restic reports as "repository is
# damaged" — it is not: a structural check and a 1% data check both passed
# cleanly. Measured: 1% = 84 packs = 7m51s, so 10% was ~80 minutes of sustained
# load on a link that does not survive it. A check that completes beats a more
# thorough one that always fails.
echo "[$(date '+%F %T')] checking repository (structure + 2% data)"
if restic "${RESTIC_OPTS[@]}" check --read-data-subset=2% >/dev/null; then
  CHECK_OK=1
  echo "[$(date '+%F %T')] check OK"
else
  echo "ERROR: restic check reported problems — investigate immediately" >&2
  EXIT_CODE=2
fi

DURATION=$(( $(date +%s) - START ))

# ── Write metrics atomically ────────────────────────────────────────────────
mkdir -p "$RESTIC_METRICS_DIR"
TMP_METRICS="${METRICS_FILE}.$$"
{
  echo "# HELP restic_maintenance_last_run_timestamp Unix time of the last maintenance run"
  echo "# TYPE restic_maintenance_last_run_timestamp gauge"
  echo "restic_maintenance_last_run_timestamp $RUN_TS"

  echo "# HELP restic_maintenance_prune_ok 1 if the last prune succeeded"
  echo "# TYPE restic_maintenance_prune_ok gauge"
  echo "restic_maintenance_prune_ok $PRUNE_OK"

  echo "# HELP restic_maintenance_check_ok 1 if the last integrity check passed"
  echo "# TYPE restic_maintenance_check_ok gauge"
  echo "restic_maintenance_check_ok $CHECK_OK"

  echo "# HELP restic_maintenance_duration_seconds Duration of the last maintenance run"
  echo "# TYPE restic_maintenance_duration_seconds gauge"
  echo "restic_maintenance_duration_seconds $DURATION"

  echo "# HELP restic_maintenance_exit_code Exit code of the last maintenance run (0=ok)"
  echo "# TYPE restic_maintenance_exit_code gauge"
  echo "restic_maintenance_exit_code $EXIT_CODE"
} > "$TMP_METRICS"
mv "$TMP_METRICS" "$METRICS_FILE"

echo "[$(date '+%F %T')] restic-maintenance finished (exit $EXIT_CODE)"
exit "$EXIT_CODE"
