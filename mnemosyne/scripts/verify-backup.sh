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
WARN_THRESHOLD=70
CRIT_THRESHOLD=85

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
      echo "                                 grafana, prometheus, stacks"
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

# Returns 0 (true) if SERVICE should be checked given the --only flag.
should_check() {
  local service="$1"
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

    if [ "$QUICK" = true ]; then
      # Quick mode: existence + non-zero size is sufficient
      ok "$label (${size}, quick)"
      return
    fi

    # Full mode: open the archive and verify it is readable
    if [[ "$file" == *.tar.gz ]] && tar -tzf "$file" > /dev/null 2>&1; then
      ok "$label (${size})"
    elif [[ "$file" == *.tar ]] && tar -tf "$file" > /dev/null 2>&1; then
      ok "$label (${size})"
    else
      fail "$label — archive corrupt or unreadable"
    fi

  elif [ -f "$marker" ]; then
    # Skipped case: no changes detected, backup-services.sh wrote a marker
    local ref_date
    ref_date=$(cat "$marker" | tr -d '[:space:]')
    local ref_file="$BACKUP_DIR/$ref_date/$(basename "$file")"

    if [ -z "$ref_date" ]; then
      fail "$label — .SKIPPED marker is empty (reference date missing)"
      return
    elif [ ! -f "$ref_file" ]; then
      fail "$label — skipped, but referenced archive not found: $ref_date/$(basename "$file")"
      return
    fi

    local ref_size
    ref_size=$(du -sh "$ref_file" | cut -f1)

    if [ "$QUICK" = true ]; then
      local ref_bytes
      ref_bytes=$(stat -c%s "$ref_file")
      if [ "$ref_bytes" -eq 0 ]; then
        fail "$label — skipped, but referenced archive is empty: $ref_date"
      else
        skipped "$label — no changes, last backup: $ref_date (${ref_size}, quick)"
      fi
      return
    fi

    # Full mode: verify the referenced archive
    if [[ "$ref_file" == *.tar.gz ]] && tar -tzf "$ref_file" > /dev/null 2>&1; then
      skipped "$label — no changes, last backup: $ref_date (${ref_size})"
    elif [[ "$ref_file" == *.tar ]] && tar -tf "$ref_file" > /dev/null 2>&1; then
      skipped "$label — no changes, last backup: $ref_date (${ref_size})"
    else
      fail "$label — skipped, but referenced archive is corrupt: $ref_date/$(basename "$file")"
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
should_check "nextcloud"    && check_archive "Nextcloud data"      "$LATEST/nextcloud-data.tar"
should_check "grafana"      && check_archive "Grafana"             "$LATEST/grafana-data.tar.gz"
should_check "prometheus"   && check_archive "Prometheus"          "$LATEST/prometheus-data.tar.gz"
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

echo -e "${BOLD}[ Disk Space ]${RESET}"

spinner_start "Checking Backup SSD"
BACKUP_USAGE=$(df "$BACKUP_DIR" | awk 'NR==2 {print $5}' | tr -d '%')
BACKUP_AVAIL=$(df -h "$BACKUP_DIR" | awk 'NR==2 {print $4}')
BACKUP_TOTAL=$(df -h "$BACKUP_DIR" | awk 'NR==2 {print $2}')
if [ "$BACKUP_USAGE" -ge "$CRIT_THRESHOLD" ]; then
  fail "Backup SSD: ${BACKUP_USAGE}% used — ${BACKUP_AVAIL} of ${BACKUP_TOTAL} free"
elif [ "$BACKUP_USAGE" -ge "$WARN_THRESHOLD" ]; then
  warn "Backup SSD: ${BACKUP_USAGE}% used — ${BACKUP_AVAIL} of ${BACKUP_TOTAL} free"
else
  ok "Backup SSD: ${BACKUP_USAGE}% used — ${BACKUP_AVAIL} of ${BACKUP_TOTAL} free"
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
TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
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