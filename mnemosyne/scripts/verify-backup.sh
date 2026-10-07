#!/bin/bash
# =============================================================================
# verify-backup.sh
# =============================================================================
# Verifies the most recent backup created by backup-services.sh.
#
# Checks performed:
#   Archives    — all expected .tar.gz and .tar files exist and are readable
#   Databases   — Vaultwarden SQLite integrity check; Nextcloud MariaDB dump
#                 header validation
#   Disk space  — backup SSD and data SSD usage against warn/critical thresholds
#   Last run    — start and finish timestamps from the backup log
#
# The script automatically finds and checks the most recent backup directory.
# No arguments required.
#
# Usage:
#   sudo /usr/local/bin/verify-backup.sh
#
# Run after backup in a single command:
#   sudo /usr/local/bin/backup-services.sh && sudo /usr/local/bin/verify-backup.sh
#
# Remote execution via SSH (used by verify-backup.py on Windows):
#   ssh youruser@192.168.1.10 "sudo /usr/local/bin/verify-backup.sh"
#
# For SSH remote execution without a password prompt, add to sudoers:
#   youruser ALL=(ALL) NOPASSWD: /usr/local/bin/verify-backup.sh
#
# Exit codes:
#   0 — all checks passed (warnings are non-fatal)
#   1 — one or more checks failed
# =============================================================================


# ── Configuration ──────────────────────────────────────────────────────────────

BACKUP_DIR="/mnt/backup"           # Must match backup-services.sh
LOG="/var/log/backup-services.log" # Log file written by backup-services.sh

# Disk usage thresholds (percentage)
# Percentage thresholds still apply to the Codex data SSD (458 GB), where a
# percentage is a meaningful unit.
WARN_THRESHOLD=70
CRIT_THRESHOLD=85

# The backup drive is judged in ABSOLUTE free space, not percent. It is a
# 3.6 TB HDD since the 2026-08 migration, and on that size a percentage says
# nothing useful: 90% used still leaves ~360 GB, which is five more nightly
# runs. What matters is "how many more backups fit", not "how full is it".
#
# One nightly run writes roughly 73 GB (Nextcloud ~29 GB + Immich ~44 GB),
# and a full 7-day retention window is around 510 GB.
BACKUP_WARN_FREE_GB=500    # ~1 retention cycle left — weeks of runway, act calmly
BACKUP_CRIT_FREE_GB=200    # ~3 nightly runs left — the next one may not fit

# Minimum number of members an archive must contain to count as real content.
# A tar.gz of an empty directory is roughly 85-105 bytes, opens cleanly, and
# lists exactly one entry ("./"). Existence and readability alone therefore
# prove nothing — four services reported OK for nine nights on exactly such
# archives after their bind mounts moved out from under the backup script.
MIN_ARCHIVE_ENTRIES=2

# Listing is bounded to MIN_ARCHIVE_ENTRIES members rather than the whole
# archive, so the content assertion costs the same on a 70 GB tar as on a 2 KB
# one: tar stops at the first members and SIGPIPE ends the read. Cheap enough
# that --quick uses it too, which removes the size heuristic entirely — a
# legitimately small archive (a few hundred bytes) cannot be told apart from an
# empty one by size alone.

# Offsite freshness. The restic run is decoupled (restic-offsite.service) and
# writes restic_offsite.prom. Textfile-collector metrics do not disappear when
# their producer stops — they freeze and keep reporting the last known good
# state, so a stale success timestamp is indistinguishable from a fresh one
# unless the age is checked explicitly.
OFFSITE_WARN_HOURS=30
OFFSITE_CRIT_HOURS=72
TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"

# ── Flags ──────────────────────────────────────────────────────────────────────
QUIET=false
ONLY=""
TARGET_DATE=""
QUICK=false

for arg in "$@"; do
  case "$arg" in
    --quiet)       QUIET=true ;;
    --quick)       QUICK=true ;;
    --only=*)      ONLY="${arg#--only=}" ;;
    --date=*)      TARGET_DATE="${arg#--date=}" ;;
    *)
      echo "Unknown argument: $arg"
      echo ""
      echo "Usage: verify-backup.sh [options]"
      echo "  --date=<YYYY-MM-DD>  Verify a specific backup instead of the latest"
      echo "  --only=<service>     Verify a single service only"
      echo "                       Services: vaultwarden, caddy, calibre, calibre-web,"
      echo "                                 kosync, syncthing, aegis, gitea, nextcloud,"
      echo "                                 ghost, immich, grafana, jobiris,"
      echo "                                 gitea-runner, exporters, stacks"
      echo "  --quick              Only check file existence and size — skip tar integrity"
      echo "                       Suitable for automated post-backup runs via cron"
      echo "  --quiet              Only print failures and warnings"
      exit 1
      ;;
  esac
done


# ── Colors ─────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

PASS=0
FAIL=0
WARN=0
SKIP=0

# ── Duration tracking ──────────────────────────────────────────────────────────
CHECK_START_TIME=0

format_duration() {
  local secs="$1"
  if [ "$secs" -ge 60 ]; then
    echo "$(( secs / 60 ))m $(( secs % 60 ))s"
  else
    echo "${secs}s"
  fi
}


# ── Spinner ────────────────────────────────────────────────────────────────────
# Same spinner pattern as backup-services.sh. Runs in a background subshell
# while a check executes; stopped and line cleared before result is printed.

SPINNER_PID=""
SPINNER_CHARS="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

spinner_start() {
  local msg="$1"
  CHECK_START_TIME=$(date +%s)
  (
    local i=0
    local start_time
    start_time=$(date +%s)
    while true; do
      local char="${SPINNER_CHARS:$((i % ${#SPINNER_CHARS})):1}"
      local elapsed=$(( $(date +%s) - start_time ))
      local time_str
      if [ "$elapsed" -ge 60 ]; then
        time_str="$(( elapsed / 60 ))m $(( elapsed % 60 ))s"
      else
        time_str="${elapsed}s"
      fi
      printf "\r  ${CYAN}%s${RESET}  %s... %s" "$char" "$msg" "$time_str"
      sleep 0.5
      ((i++))
    done
  ) &
  SPINNER_PID=$!
  disown "$SPINNER_PID"
}

spinner_stop() {
  if [ -n "$SPINNER_PID" ]; then
    kill "$SPINNER_PID" 2>/dev/null
    wait "$SPINNER_PID" 2>/dev/null
    printf "\r\033[K"
    SPINNER_PID=""
  fi
}


# ── Helper functions ───────────────────────────────────────────────────────────

# Each helper stops the spinner before printing so the result line
# replaces the spinner cleanly.
ok()      { local e=$(( $(date +%s) - CHECK_START_TIME )); spinner_stop; [ "$QUIET" = false ] && echo -e "  ${GREEN}OK${RESET}    $1 ($(format_duration $e))"; ((PASS++)); }
fail()    { local e=$(( $(date +%s) - CHECK_START_TIME )); spinner_stop; echo -e "  ${RED}FAIL${RESET}  $1 ($(format_duration $e))"; ((FAIL++)); }
warn()    { local e=$(( $(date +%s) - CHECK_START_TIME )); spinner_stop; echo -e "  ${YELLOW}WARN${RESET}  $1 ($(format_duration $e))"; ((WARN++)); }
info()    { [ "$QUIET" = false ] && echo -e "  ${CYAN}....${RESET}  $1"; }
skipped() { local e=$(( $(date +%s) - CHECK_START_TIME )); spinner_stop; [ "$QUIET" = false ] && echo -e "  ${CYAN}SKIP${RESET}  $1 ($(format_duration $e))"; ((SKIP++)); }

# Every service this script knows how to verify, recorded as should_check is
# called. Used by check_coverage() below to prove that nothing declared in
# backup-services.sh is missing a check here.
CHECKED_SERVICES=""

# Returns 0 (true) if SERVICE should be checked given the --only flag.
should_check() {
  local service="$1"
  case " $CHECKED_SERVICES " in
    *" $service "*) ;;
    *) CHECKED_SERVICES="$CHECKED_SERVICES $service" ;;
  esac
  [ -z "$ONLY" ] || [ "$ONLY" = "$service" ]
}

# Check a backup archive for existence and readability.
# Handles both .tar.gz (compressed) and .tar (uncompressed) formats.
#
# In --quick mode: only checks file existence and non-zero size.
# Skips tar -tf / tar -tzf entirely — fast enough for automated post-backup runs.
#
# In full mode: opens the archive and reads the file list without extracting.
# A non-zero exit from tar indicates the archive is corrupt or truncated.
#
# If the archive is missing but a .SKIPPED marker file exists, the step was
# intentionally skipped by backup-services.sh because no files had changed.
# The marker contains the date of the last real archive — we verify that
# instead and report the result as SKIP (counts as PASS, not FAIL).
check_archive() {
  local label="$1"
  local file="$2"
  local marker="${file}.SKIPPED"

  spinner_start "Checking $label"

  if [ -f "$file" ]; then
    local size
    size=$(du -sh "$file" | cut -f1)
    local bytes
    bytes=$(stat -c%s "$file")

    if [ "$bytes" -eq 0 ]; then
      fail "$label — file is empty"
      return
    fi

    # Open the archive, verify it is readable, and verify it actually contains
    # something. The read is bounded to MIN_ARCHIVE_ENTRIES members.
    local entries=0
    if [[ "$file" == *.tar.gz ]]; then
      entries=$(tar -tzf "$file" 2>/dev/null | head -"$MIN_ARCHIVE_ENTRIES" | wc -l)
    elif [[ "$file" == *.tar ]]; then
      entries=$(tar -tf "$file" 2>/dev/null | head -"$MIN_ARCHIVE_ENTRIES" | wc -l)
    fi

    if [ "$entries" -eq 0 ]; then
      fail "$label — archive corrupt or unreadable"
      return
    elif [ "$entries" -lt "$MIN_ARCHIVE_ENTRIES" ]; then
      fail "$label — archive contains only ${entries} entr(ies), ${bytes} bytes"
      return
    fi

    if [ "$QUICK" = true ]; then
      ok "$label (${size}, quick)"
    else
      ok "$label (${size})"
    fi

  elif [ -f "$marker" ]; then
    # Skipped case: no changes detected, backup-services.sh wrote a marker
    local ref_date
    ref_date=$(cat "$marker" | tr -d '[:space:]')
    local ref_file
    ref_file="$BACKUP_DIR/$ref_date/$(basename "$file")"

    if [ -z "$ref_date" ]; then
      fail "$label — .SKIPPED marker is empty (reference date missing)"
      return
    elif [ ! -f "$ref_file" ]; then
      fail "$label — skipped, but referenced archive not found: $ref_date/$(basename "$file")"
      return
    fi

    local ref_size
    ref_size=$(du -sh "$ref_file" | cut -f1)

    local ref_bytes
    ref_bytes=$(stat -c%s "$ref_file")

    # Verify the referenced archive is readable AND non-empty — same bounded
    # check as above, in both quick and full mode.
    local ref_entries=0
    if [[ "$ref_file" == *.tar.gz ]]; then
      ref_entries=$(tar -tzf "$ref_file" 2>/dev/null | head -"$MIN_ARCHIVE_ENTRIES" | wc -l)
    elif [[ "$ref_file" == *.tar ]]; then
      ref_entries=$(tar -tf "$ref_file" 2>/dev/null | head -"$MIN_ARCHIVE_ENTRIES" | wc -l)
    fi

    if [ "$ref_entries" -eq 0 ]; then
      fail "$label — skipped, but referenced archive is corrupt: $ref_date/$(basename "$file")"
    elif [ "$ref_entries" -lt "$MIN_ARCHIVE_ENTRIES" ]; then
      fail "$label — skipped, but referenced archive is empty: $ref_date (${ref_bytes} bytes)"
    else
      # How close is this skip chain to the retention cliff? backup-services.sh
      # bounds it via MAX_SKIP_DAYS, but if that guard is ever bypassed the
      # reference gets pruned and the chain points at nothing.
      local ref_age_days=0
      local ref_epoch
      ref_epoch=$(date -d "$ref_date" +%s 2>/dev/null)
      [ -n "$ref_epoch" ] && ref_age_days=$(( ( $(date +%s) - ref_epoch ) / 86400 ))

      if [ "$ref_age_days" -ge 6 ]; then
        warn "$label — skipped, reference is ${ref_age_days}d old and near the retention cliff: $ref_date"
      else
        skipped "$label — no changes, last backup: $ref_date (${ref_size})"
      fi
    fi

  else
    fail "$label — file not found: $(basename "$file")"
  fi
}


# ── Header ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}=== Backup Verification — $(date '+%Y-%m-%d %H:%M') ===${RESET}"
echo ""

# Verify the backup drive is accessible before doing anything else
if ! mountpoint -q "$BACKUP_DIR"; then
  echo -e "${RED}ERROR: $BACKUP_DIR is not mounted. Is the WD My Passport connected?${RESET}"
  exit 1
fi

# Find the target backup directory — either the date specified via --date
# or the most recent date-stamped directory if no flag was given.
if [ -n "$TARGET_DATE" ]; then
  LATEST="$BACKUP_DIR/$TARGET_DATE/"
  DATE="$TARGET_DATE"
  if [ ! -d "$LATEST" ]; then
    echo -e "${RED}ERROR: No backup found for date: $TARGET_DATE${RESET}"
    exit 1
  fi
else
  LATEST=$(ls -td "$BACKUP_DIR"/[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]/ 2>/dev/null | head -1)
  if [ -z "$LATEST" ]; then
    echo -e "${RED}ERROR: No backup directories found in $BACKUP_DIR${RESET}"
    exit 1
  fi
  DATE=$(basename "$LATEST")
fi
echo -e "  Backup date : ${BOLD}${DATE}${RESET}"
echo -e "  Backup path : ${LATEST}"
echo -e "  Backup size : $(du -sh "$LATEST" | cut -f1)"
[ "$QUICK" = true ] && echo -e "  ${CYAN}⚠ --quick: skipping tar integrity checks${RESET}"
echo ""


# ── Archive integrity ──────────────────────────────────────────────────────────
# Each call opens the archive and reads the file list without extracting.
# A non-zero exit from tar indicates the archive is corrupt or truncated.

echo -e "${BOLD}[ Archives ]${RESET}"
should_check "vaultwarden"  && check_archive "Vaultwarden data"    "$LATEST/vaultwarden-data.tar.gz"
should_check "caddy"        && check_archive "Caddy TLS"           "$LATEST/caddy-data.tar.gz"
should_check "calibre"      && check_archive "Calibre Library"     "$LATEST/calibre-library.tar.gz"
should_check "calibre-web"  && check_archive "Calibre-Web config"  "$LATEST/calibre-web-config.tar.gz"
should_check "kosync"       && check_archive "KOSync"              "$LATEST/kosync-data.tar.gz"
should_check "syncthing"    && check_archive "Syncthing vault"     "$LATEST/syncthing-obsidian.tar.gz"
should_check "syncthing"    && check_archive "Syncthing config"    "$LATEST/syncthing-config.tar.gz"
should_check "aegis"        && check_archive "Aegis 2FA backup"    "$LATEST/aegis-backup.tar.gz"
should_check "gitea"        && check_archive "Gitea"               "$LATEST/gitea-data.tar.gz"
should_check "ghost"        && check_archive "Ghost content"        "$LATEST/ghost-content.tar.gz"
should_check "nextcloud"    && check_archive "Nextcloud data"      "$LATEST/nextcloud-data.tar"
should_check "immich"       && check_archive "Immich upload"        "$LATEST/immich-upload.tar"
should_check "grafana"      && check_archive "Grafana"             "$LATEST/grafana-data.tar.gz"
# Prometheus is intentionally not backed up (removed 2026-08-19) — the hot tar
# of a live WAL always exits 1, and the data is expendable. No check here.
should_check "jobiris"      && check_archive "JobIris"              "$LATEST/jobiris.tar.gz"
should_check "gitea-runner" && check_archive "Gitea Act Runner"     "$LATEST/gitea-runner.tar.gz"
should_check "exporters"    && check_archive "Exporter tokens"      "$LATEST/exporter-tokens.tar.gz"
should_check "stacks"       && check_archive "Stack configs"       "$LATEST/stacks-config.tar.gz"
echo ""


# ── Database checks ────────────────────────────────────────────────────────────

echo -e "${BOLD}[ Databases ]${RESET}"

# Vaultwarden SQLite: PRAGMA integrity_check returns "ok" for a healthy
# database. Any other output indicates corruption. Uses the backup copy,
# not the live database.
if should_check "vaultwarden"; then
  spinner_start "Checking Vaultwarden SQLite"
  VW_DB="$LATEST/vaultwarden-db.sqlite3"
  if [ ! -f "$VW_DB" ]; then
    fail "Vaultwarden SQLite — file not found"
  elif sqlite3 "$VW_DB" "PRAGMA integrity_check;" 2>/dev/null | grep -q "^ok$"; then
    ok "Vaultwarden SQLite — integrity ok ($(du -sh "$VW_DB" | cut -f1))"
  else
    fail "Vaultwarden SQLite — integrity check failed"
  fi
fi

# Ghost MySQL dump: ghost-db runs mysql:8.0, so the header reads "MySQL dump",
# not "MariaDB dump" like nextcloud-db. Same validation shape as Nextcloud below. Note the marker is
# ghost-content.tar.gz.SKIPPED — backup-services.sh writes one marker per
# service, not one per artefact, so a skipped Ghost step is resolved through
# the content archive's marker.
if should_check "ghost"; then
  spinner_start "Checking Ghost DB dump"
  GH_DB="$LATEST/ghost-db.sql"
  GH_MARKER="$LATEST/ghost-content.tar.gz.SKIPPED"
  if [ -f "$GH_DB" ]; then
    if [ ! -s "$GH_DB" ]; then
      fail "Ghost DB dump — file is empty"
    elif head -3 "$GH_DB" | grep -q "MySQL dump"; then
      ok "Ghost DB dump — valid MySQL dump ($(du -sh "$GH_DB" | cut -f1))"
    else
      fail "Ghost DB dump — missing MySQL header (possibly corrupt)"
    fi
  elif [ -f "$GH_MARKER" ]; then
    ref_date=$(cat "$GH_MARKER" | tr -d '[:space:]')
    ref_db="$BACKUP_DIR/$ref_date/ghost-db.sql"
    if [ -z "$ref_date" ]; then
      fail "Ghost DB dump — .SKIPPED marker is empty"
    elif [ ! -f "$ref_db" ]; then
      fail "Ghost DB dump — skipped, but referenced dump not found: $ref_date/ghost-db.sql"
    elif head -3 "$ref_db" | grep -q "MySQL dump"; then
      skipped "Ghost DB dump — no changes, last backup: $ref_date ($(du -sh "$ref_db" | cut -f1))"
    else
      fail "Ghost DB dump — skipped, but referenced dump is corrupt: $ref_date"
    fi
  else
    fail "Ghost DB dump — file not found"
  fi
fi

# Immich PostgreSQL dump: pg_dumpall writes "PostgreSQL database cluster dump"
# in its header — a plain pg_dump of a single database says "PostgreSQL
# database dump" instead, so this also catches the wrong dump command.
# The marker is immich-upload.tar.SKIPPED: backup-services.sh writes one
# .SKIPPED per service, not one per artefact.
if should_check "immich"; then
  spinner_start "Checking Immich DB dump"
  IM_DB="$LATEST/immich-db.sql"
  IM_MARKER="$LATEST/immich-upload.tar.SKIPPED"
  if [ -f "$IM_DB" ]; then
    if [ ! -s "$IM_DB" ]; then
      fail "Immich DB dump — file is empty"
    elif head -5 "$IM_DB" | grep -q "PostgreSQL database cluster dump"; then
      ok "Immich DB dump — valid pg_dumpall dump ($(du -sh "$IM_DB" | cut -f1))"
    else
      fail "Immich DB dump — missing pg_dumpall header (possibly corrupt)"
    fi
  elif [ -f "$IM_MARKER" ]; then
    ref_date=$(tr -d '[:space:]' < "$IM_MARKER")
    ref_db="$BACKUP_DIR/$ref_date/immich-db.sql"
    if [ -z "$ref_date" ]; then
      fail "Immich DB dump — .SKIPPED marker is empty"
    elif [ ! -f "$ref_db" ]; then
      fail "Immich DB dump — skipped, but referenced dump not found: $ref_date/immich-db.sql"
    elif head -5 "$ref_db" | grep -q "PostgreSQL database cluster dump"; then
      skipped "Immich DB dump — no changes, last backup: $ref_date ($(du -sh "$ref_db" | cut -f1))"
    else
      fail "Immich DB dump — skipped, but referenced dump is corrupt: $ref_date"
    fi
  else
    fail "Immich DB dump — file not found"
  fi
fi

# Nextcloud MariaDB dump: check that the file is non-empty and starts with
# the expected MariaDB dump header. An empty file or wrong header indicates
# the dump failed (wrong password, container not running, etc.).
# If a .SKIPPED marker exists, the Nextcloud step was skipped entirely —
# verify the referenced dump instead.
if should_check "nextcloud"; then
  spinner_start "Checking Nextcloud DB dump"
  NC_DB="$LATEST/nextcloud-db.sql"
  NC_DB_MARKER="${NC_DB}.SKIPPED"
  if [ -f "$NC_DB" ]; then
    if [ ! -s "$NC_DB" ]; then
      fail "Nextcloud DB dump — file is empty"
    elif head -3 "$NC_DB" | grep -q "MariaDB dump"; then
      ok "Nextcloud DB dump — valid MariaDB dump ($(du -sh "$NC_DB" | cut -f1))"
    else
      fail "Nextcloud DB dump — missing MariaDB header (possibly corrupt)"
    fi
  elif [ -f "$NC_DB_MARKER" ]; then
    ref_date=$(cat "$NC_DB_MARKER" | tr -d '[:space:]')
    ref_db="$BACKUP_DIR/$ref_date/nextcloud-db.sql"
    if [ -z "$ref_date" ]; then
      fail "Nextcloud DB dump — .SKIPPED marker is empty"
    elif [ ! -f "$ref_db" ]; then
      fail "Nextcloud DB dump — skipped, but referenced dump not found: $ref_date/nextcloud-db.sql"
    elif head -3 "$ref_db" | grep -q "MariaDB dump"; then
      skipped "Nextcloud DB dump — no changes, last backup: $ref_date ($(du -sh "$ref_db" | cut -f1))"
    else
      fail "Nextcloud DB dump — skipped, but referenced dump is corrupt: $ref_date"
    fi
  else
    fail "Nextcloud DB dump — file not found"
  fi
fi
echo ""


# ── Disk space ─────────────────────────────────────────────────────────────────
# Checks both the backup drive and the data drive. Warns at WARN_THRESHOLD%,
# fails at CRIT_THRESHOLD%. The ntfy disk-space alert script runs separately
# at 08:00 via cron — these checks are the quarterly manual verification.

# ── Check coverage ─────────────────────────────────────────────────────────────
# Cross-checks this script against backup-services.sh: every service declared
# in VALID_SERVICES there must have at least one check here.
#
# This exists because of the 2026-08-30 incident. A patch to add Ghost was
# built from a copy of this file that predated the Immich checks, so
# check_archive "Immich upload" and the Immich DB dump check simply vanished.
# A 43 GB archive and a 126 MB dump were produced correctly every night and
# verified by nobody, while the summary read "All checks passed". Absence of a
# check looked exactly like a passed check.
#
# A missing check is a FAIL, not a warning: an unverified backup is an
# assumption, and the whole point of this script is to not make those.

# Exported as backup_verify_uncovered_services so the gap can be alerted on
# directly, not only via the aggregate fail count.
UNCOVERED_SERVICES=0

echo -e "${BOLD}[ Check Coverage ]${RESET}"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-/usr/local/bin/backup-services.sh}"
if [ ! -f "$BACKUP_SCRIPT" ]; then
  fail "Coverage — cannot read $BACKUP_SCRIPT to compare against"
else
  DECLARED=$(grep -m1 '^VALID_SERVICES=' "$BACKUP_SCRIPT" \
             | sed 's/^VALID_SERVICES="//; s/"$//')
  if [ -z "$DECLARED" ]; then
    fail "Coverage — no VALID_SERVICES found in $BACKUP_SCRIPT"
  else
    MISSING=""
    for svc in $DECLARED; do
      case " $CHECKED_SERVICES " in
        *" $svc "*) ;;
        *) MISSING="$MISSING $svc" ;;
      esac
    done
    if [ -n "$MISSING" ]; then
      UNCOVERED_SERVICES=$(echo "$MISSING" | wc -w)
      fail "Coverage — backed up but never verified:$MISSING"
    else
      ok "Coverage — all $(echo "$DECLARED" | wc -w) backed-up service(s) have a check"
    fi
  fi
fi
echo ""


# ── Offsite freshness ──────────────────────────────────────────────────────────
# Reads restic_offsite.prom rather than calling restic: the repo lives behind an
# SSH connection to Hetzner and this script must stay fast and offline-safe.
# The point of this check is the timestamp, not the exit code — a frozen file
# reporting exit_code 0 from six weeks ago looks identical to a healthy one.

echo -e "${BOLD}[ Offsite ]${RESET}"
OFFSITE_PROM="$TEXTFILE_DIR/restic_offsite.prom"
if [ ! -f "$OFFSITE_PROM" ]; then
  fail "Offsite — no metrics file found: $OFFSITE_PROM"
else
  OFFSITE_LAST=$(awk '/^restic_offsite_last_success_timestamp /{print $2}' "$OFFSITE_PROM" | tail -1)
  OFFSITE_RC=$(awk '/^restic_offsite_exit_code /{print $2}' "$OFFSITE_PROM" | tail -1)

  if [ -z "$OFFSITE_LAST" ] || [ "$OFFSITE_LAST" = "0" ]; then
    fail "Offsite — no successful restic run recorded"
  else
    OFFSITE_AGE_H=$(( ( $(date +%s) - ${OFFSITE_LAST%.*} ) / 3600 ))
    OFFSITE_WHEN=$(date -d "@${OFFSITE_LAST%.*}" '+%Y-%m-%d %H:%M' 2>/dev/null)
    if [ "$OFFSITE_AGE_H" -ge "$OFFSITE_CRIT_HOURS" ]; then
      fail "Offsite — last success ${OFFSITE_AGE_H}h ago (${OFFSITE_WHEN})"
    elif [ "$OFFSITE_AGE_H" -ge "$OFFSITE_WARN_HOURS" ]; then
      warn "Offsite — last success ${OFFSITE_AGE_H}h ago (${OFFSITE_WHEN})"
    else
      ok "Offsite — last success ${OFFSITE_AGE_H}h ago (${OFFSITE_WHEN}, exit ${OFFSITE_RC:-?})"
    fi
  fi

  # The timer is the only thing that guarantees the offsite run happens at all
  # when the backup script is invoked with --no-offsite.
  if ! systemctl is-enabled restic-offsite.timer >/dev/null 2>&1; then
    fail "Offsite — restic-offsite.timer is not enabled"
  fi
fi

# Mount coverage, written by backup-services.sh during preflight.
BACKUP_PROM="$TEXTFILE_DIR/backup.prom"
if [ -f "$BACKUP_PROM" ]; then
  UNCOVERED=$(awk '/^backup_uncovered_mounts /{print $2}' "$BACKUP_PROM" | tail -1)
  if [ -n "$UNCOVERED" ] && [ "$UNCOVERED" != "0" ]; then
    warn "Coverage — ${UNCOVERED} persistent container mount(s) have no backup source"
  elif [ -n "$UNCOVERED" ]; then
    ok "Coverage — all persistent container mounts are covered"
  fi
fi
echo ""


# ── Disk space ─────────────────────────────────────────────────────────────────
# NOTE: /mnt/vault and /mnt/codex are the same ext4 filesystem (same UUID,
# mounted twice). Only one is checked here because there is only one device.
# The tiering implied by the two paths is naming, not redundancy.

echo -e "${BOLD}[ Disk Space ]${RESET}"

spinner_start "Checking Backup HDD"
BACKUP_USAGE=$(df --output=pcent "$BACKUP_DIR" | tail -1 | tr -dc '0-9')
BACKUP_AVAIL=$(df -h --output=avail "$BACKUP_DIR" | tail -1 | tr -d ' ')
BACKUP_TOTAL=$(df -h --output=size "$BACKUP_DIR" | tail -1 | tr -d ' ')
BACKUP_FREE_GB=$(df --output=avail --block-size=G "$BACKUP_DIR" | tail -1 | tr -dc '0-9')

if [ "$BACKUP_FREE_GB" -lt "$BACKUP_CRIT_FREE_GB" ]; then
  fail "Backup HDD: only ${BACKUP_AVAIL} free of ${BACKUP_TOTAL} (${BACKUP_USAGE}% used) — under ${BACKUP_CRIT_FREE_GB}G, the next run may not fit"
elif [ "$BACKUP_FREE_GB" -lt "$BACKUP_WARN_FREE_GB" ]; then
  warn "Backup HDD: ${BACKUP_AVAIL} free of ${BACKUP_TOTAL} (${BACKUP_USAGE}% used) — under ${BACKUP_WARN_FREE_GB}G, roughly one retention cycle left"
else
  ok "Backup HDD: ${BACKUP_AVAIL} free of ${BACKUP_TOTAL} (${BACKUP_USAGE}% used)"
fi

spinner_start "Checking Codex SSD"
CODEX_USAGE=$(df /mnt/codex | awk 'NR==2 {print $5}' | tr -d '%')
CODEX_AVAIL=$(df -h /mnt/codex | awk 'NR==2 {print $4}')
CODEX_TOTAL=$(df -h /mnt/codex | awk 'NR==2 {print $2}')
if [ "$CODEX_USAGE" -ge "$CRIT_THRESHOLD" ]; then
  fail "Codex SSD:  ${CODEX_USAGE}% used — ${CODEX_AVAIL} of ${CODEX_TOTAL} free"
elif [ "$CODEX_USAGE" -ge "$WARN_THRESHOLD" ]; then
  warn "Codex SSD:  ${CODEX_USAGE}% used — ${CODEX_AVAIL} of ${CODEX_TOTAL} free"
else
  ok "Codex SSD:  ${CODEX_USAGE}% used — ${CODEX_AVAIL} of ${CODEX_TOTAL} free"
fi
echo ""


# ── Last backup log ────────────────────────────────────────────────────────────
# Reads the start and finish timestamps written by backup-services.sh.
# Format in log: "=== Backup started: Sat 29 Mar 02:00:01 CET 2026 ==="

echo -e "${BOLD}[ Last Backup Run ]${RESET}"
if [ -f "$LOG" ]; then
  LAST_START=$(grep "Backup started" "$LOG" | tail -1 | sed 's/=== //g; s/ ===$//g')
  LAST_END=$(grep "Backup finished" "$LOG" | tail -1 | sed 's/=== //g; s/ ===$//g')
  if [ -n "$LAST_START" ]; then
    info "$LAST_START"
    info "$LAST_END"
  else
    warn "No completed backup run found in log"
  fi
else
  warn "Log file not found: $LOG"
fi
echo ""


# ── Summary ────────────────────────────────────────────────────────────────────

echo -e "${BOLD}[ Summary ]${RESET}"
echo -e "  Passed : ${GREEN}${PASS}${RESET}"
[ "$SKIP" -gt 0 ] && echo -e "  Skipped: ${CYAN}${SKIP}${RESET}"
[ "$WARN" -gt 0 ] && echo -e "  Warnings: ${YELLOW}${WARN}${RESET}"
[ "$FAIL" -gt 0 ] && echo -e "  Failed : ${RED}${FAIL}${RESET}"
echo ""

# ── Prometheus Textfile Metrics ────────────────────────────────────────────────
# Written after every run so Grafana/Alertmanager always has fresh verify state.
# Uses a separate file from backup-services.sh to avoid overwriting backup metrics.
# TEXTFILE_DIR is defined in the configuration block at the top.
VERIFY_END_TIME=$(date +%s)

{
  echo "# HELP backup_verify_last_run_timestamp Unix timestamp of the last verify run"
  echo "# TYPE backup_verify_last_run_timestamp gauge"
  echo "backup_verify_last_run_timestamp $VERIFY_END_TIME"
  echo "# HELP backup_verify_exit_code Exit code of last verify run (0 = all passed)"
  echo "# TYPE backup_verify_exit_code gauge"
  echo "backup_verify_exit_code $FAIL"
  echo "# HELP backup_verify_pass_count Number of checks that passed in the last verify run"
  echo "# TYPE backup_verify_pass_count gauge"
  echo "backup_verify_pass_count $PASS"
  echo "# HELP backup_verify_fail_count Number of checks that failed in the last verify run"
  echo "# TYPE backup_verify_fail_count gauge"
  echo "backup_verify_fail_count $FAIL"
  echo "# HELP backup_verify_skip_count Number of checks skipped (no changes) in the last verify run"
  echo "# TYPE backup_verify_skip_count gauge"
  echo "backup_verify_skip_count $SKIP"
  echo "# HELP backup_verify_uncovered_services Services in VALID_SERVICES with no check here"
  echo "# TYPE backup_verify_uncovered_services gauge"
  echo "backup_verify_uncovered_services $UNCOVERED_SERVICES"
  echo "# HELP backup_verify_warn_count Number of warnings in the last verify run"
  echo "# TYPE backup_verify_warn_count gauge"
  echo "backup_verify_warn_count $WARN"
  echo "# HELP backup_verify_quick Whether the last verify run used --quick mode (1) or full mode (0)"
  echo "# TYPE backup_verify_quick gauge"
  echo "backup_verify_quick $([ "$QUICK" = true ] && echo 1 || echo 0)"
} > "$TEXTFILE_DIR/backup_verify.prom"

# ── Final result ───────────────────────────────────────────────────────────────
if [ "$FAIL" -gt 0 ]; then
  echo -e "${RED}${BOLD}  ✗ Verification FAILED — $FAIL check(s) require attention${RESET}"
  echo ""
  exit 1
elif [ "$WARN" -gt 0 ]; then
  echo -e "${YELLOW}${BOLD}  ⚠ Verification PASSED with warnings${RESET}"
  echo ""
  exit 0
else
  if [ "$SKIP" -gt 0 ]; then
    echo -e "${GREEN}${BOLD}  ✓ All checks passed${RESET} ${CYAN}($SKIP skipped — no changes)${RESET}"
  else
    echo -e "${GREEN}${BOLD}  ✓ All checks passed${RESET}"
  fi
  echo ""
  exit 0
fi