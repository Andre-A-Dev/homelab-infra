#!/bin/bash
# =============================================================================
# restic-unit-metrics.sh  →  /usr/local/bin/restic-unit-metrics.sh
# =============================================================================
# Records the UNIT-level outcome of a restic service run, as seen by systemd.
#
# Why this exists
# ---------------
# On 2026-08-20 restic-offsite.service failed with 203/EXEC — the script was
# not executable, so systemd could not spawn it at all. restic_offsite.prom
# still reported exit_code 0 and a success timestamp from 2026-07-08, because
# the script writes its own metrics and never ran to write anything else.
#
# A process that reports its own health can only report failures it survives
# long enough to observe. Anything that kills it before its first line — a
# missing executable bit, a failed mount, a hardening violation, an OOM kill,
# a timeout — is invisible to it by construction.
#
# systemd sees all of those. ExecStopPost= runs even when ExecStart= never
# started, so this is the one vantage point that cannot be blinded.
#
# Deliberately writes a SEPARATE .prom file rather than updating the script's
# own: two writers on one file race, and the whole point is that this observer
# stays independent of the thing it observes.
#
# Usage (from the unit, which provides $SERVICE_RESULT / $EXIT_STATUS):
#   ExecStopPost=/usr/local/bin/restic-unit-metrics.sh restic_offsite
#
# systemd sets:
#   SERVICE_RESULT  success | exit-code | signal | timeout | oom-kill | ...
#   EXIT_CODE       exited | killed        (unset when ExecStart never ran)
#   EXIT_STATUS     numeric status or signal name
# =============================================================================

set -uo pipefail

METRIC_PREFIX="${1:-restic_offsite}"
METRICS_DIR="${RESTIC_METRICS_DIR:-/var/lib/node_exporter/textfile_collector}"
METRICS_FILE="${METRICS_DIR}/${METRIC_PREFIX}_unit.prom"

RESULT="${SERVICE_RESULT:-unknown}"
STATUS="${EXIT_STATUS:-}"
NOW=$(date +%s)

# EXIT_STATUS is a number when the process exited, a signal name when it was
# killed, and empty when ExecStart never spawned. Normalise to a number so the
# metric stays a usable gauge; the textual detail lives in the result label.
case "$STATUS" in
  ''|*[!0-9]*) STATUS_NUM=-1 ;;
  *)           STATUS_NUM="$STATUS" ;;
esac

if [ "$RESULT" = "success" ]; then
  SUCCESS=1
else
  SUCCESS=0
fi

mkdir -p "$METRICS_DIR"

# Write to a temp file then mv — node_exporter must never read a half-written
# .prom. mv within the same filesystem is atomic.
TMP=$(mktemp "${METRICS_FILE}.XXXXXX") || exit 0
{
  echo "# HELP ${METRIC_PREFIX}_unit_last_finish_timestamp Unix time systemd last saw this unit finish (any outcome)"
  echo "# TYPE ${METRIC_PREFIX}_unit_last_finish_timestamp gauge"
  echo "${METRIC_PREFIX}_unit_last_finish_timestamp ${NOW}"
  echo "# HELP ${METRIC_PREFIX}_unit_success 1 if systemd reported SERVICE_RESULT=success, else 0"
  echo "# TYPE ${METRIC_PREFIX}_unit_success gauge"
  echo "${METRIC_PREFIX}_unit_success ${SUCCESS}"
  echo "# HELP ${METRIC_PREFIX}_unit_exit_status Numeric exit status (-1 when the process never spawned or was signalled)"
  echo "# TYPE ${METRIC_PREFIX}_unit_exit_status gauge"
  echo "${METRIC_PREFIX}_unit_exit_status ${STATUS_NUM}"
  echo "# HELP ${METRIC_PREFIX}_unit_result systemd SERVICE_RESULT, as a label"
  echo "# TYPE ${METRIC_PREFIX}_unit_result gauge"
  echo "${METRIC_PREFIX}_unit_result{result=\"${RESULT}\"} 1"
} > "$TMP"

chmod 644 "$TMP"
mv -f "$TMP" "$METRICS_FILE"

# Never fail: an ExecStopPost that exits non-zero marks an otherwise successful
# unit as failed. The observer must not be able to break the thing it observes.
exit 0
