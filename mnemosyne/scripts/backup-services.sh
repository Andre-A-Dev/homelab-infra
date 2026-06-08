#!/bin/bash

# ── Configuration ──────────────────────────────────────────────────────────────
BACKUP_DIR="/mnt/backup"

DATE=$(date +%Y-%m-%d)
RETENTION_DAYS=7                  # Reduced from 14 — 7 days is enough for a homelab
MIN_FREE_GB=40                    # Abort if less than this many GB are free before starting
MAX_USAGE_PERCENT=85              # Abort if disk usage exceeds this % after cleanup
LOG="/var/log/backup-services.log"

# Tracks the last successful backup timestamp per service.
# Stored on the local filesystem (not the external SSD) so it's always available.
# Used to skip backups when no files have changed since the last run.
# Note: the backup SSD is exFAT — no hardlinks or symlinks available.
# Skipped steps write a .SKIPPED marker file containing the date of the last
# real archive so verify-backup.sh can look it up.
TIMESTAMP_DIR="/var/lib/backup-timestamps"

# ── Flags ──────────────────────────────────────────────────────────────────────
FORCE=false
DRY_RUN=false
NO_CLEANUP=false
OVERWRITE=false
ONLY=""          # If set, only the named service will be backed up

# ── Duration tracking ──────────────────────────────────────────────────────────
# STEP_START_TIME is set by step() at the start of each step.
# CURRENT_SERVICE is set manually before each service block for Prometheus labeling.
# STEP_DURATIONS accumulates per-service runtimes for the .prom output.
STEP_START_TIME=0
CURRENT_SERVICE=""
declare -A STEP_DURATIONS  # service -> seconds
declare -A STEP_STATUSES   # service -> 0 (ok) | 1 (fail) | 2 (skip)
declare -A ARCHIVE_SIZES   # service -> bytes
SKIPPED_TOTAL=0

# Formats seconds as "Xm Ys" (>= 60s) or "Xs" (< 60s).
format_duration() {
  local secs="$1"
  if [ "$secs" -ge 60 ]; then
    echo "$(( secs / 60 ))m $(( secs % 60 ))s"
  else
    echo "${secs}s"
  fi
}
for arg in "$@"; do
  case "$arg" in
    --force)           FORCE=true ;;
    --dry-run)         DRY_RUN=true ;;
    --no-cleanup)      NO_CLEANUP=true ;;
    --overwrite)       OVERWRITE=true ;;
    --only=*)          ONLY="${arg#--only=}" ;;
    --retention=*)     RETENTION_DAYS="${arg#--retention=}" ;;
    *)
      echo "Unknown argument: $arg"
      echo ""
      echo "Usage: backup-services.sh [options]"
      echo "  --force              Ignore change detection — back up all services"
      echo "  --dry-run            Show what would run without writing anything"
      echo "  --no-cleanup         Skip the retention cleanup step"
      echo "  --overwrite          Overwrite today's backup if it already exists"
      echo "  --only=<service>     Back up a single service only"
      echo "                       Services: vaultwarden, caddy, calibre, calibre-web,"
      echo "                                 kosync, syncthing, aegis, gitea, nextcloud,"
      echo "                                 grafana, prometheus, stacks"
      echo "  --retention=<days>   Override the default retention period"
      exit 1
      ;;
  esac
done

# Load Nextcloud DB password from stack .env
ENV_NEXTCLOUD="/home/youruser/stacks/nextcloud/.env"
START_TIME=$(date +%s)

if [ ! -f "$ENV_NEXTCLOUD" ]; then
  echo "ERROR: Nextcloud .env not found: $ENV_NEXTCLOUD"
  exit 1
fi
# shellcheck source=/dev/null
source "$ENV_NEXTCLOUD"

# ── Colors ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ── Step counter ───────────────────────────────────────────────────────────────
TOTAL_STEPS=13
CURRENT_STEP=0
ERRORS=0

# ── Spinner ────────────────────────────────────────────────────────────────────
SPINNER_PID=""
SPINNER_MSG=""
SPINNER_CHARS="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

spinner_start() {
  SPINNER_MSG="$1"
  local start_time
  start_time=$(date +%s)
  (
    local i=0
    while true; do
      local char="${SPINNER_CHARS:$((i % ${#SPINNER_CHARS})):1}"
      local elapsed=$(( $(date +%s) - start_time ))
      local time_str
      if [ "$elapsed" -ge 60 ]; then
        time_str="$(( elapsed / 60 ))m $(( elapsed % 60 ))s"
      else
        time_str="${elapsed}s"
      fi
      printf "\r  ${CYAN}%s${RESET}  %s... %s" "$char" "$SPINNER_MSG" "$time_str"
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
    printf "\r\033[K"   # Clear spinner line
    SPINNER_PID=""
  fi
}

# ── Helpers ────────────────────────────────────────────────────────────────────

# Records the size of a backup archive in bytes for Prometheus.
# Called after a successful tar or docker volume backup.
# Accepts a file path; silently skips if the file does not exist (dry-run).
record_archive_size() {
  local file="$1"
  if [ -n "$CURRENT_SERVICE" ] && [ -f "$file" ]; then
    ARCHIVE_SIZES["$CURRENT_SERVICE"]=$(stat -c%s "$file")
  fi
}

# Returns 0 (true) if SERVICE should be backed up given the --only flag.
# When --only is not set all services run. When set only the matching service runs.
should_run() {
  local service="$1"
  [ -z "$ONLY" ] || [ "$ONLY" = "$service" ]
}

# Strip ANSI color codes before writing to log file
strip_ansi() {
  sed -r 's/\x1B\[[0-9;]*[mGKHF]//g'
}

# Log to file only (silent on terminal — used during spinner)
log_file() {
  echo "$1" | strip_ansi >> "$LOG"
}

# Print to terminal and log file
log() {
  echo "$1" | tee >(strip_ansi >> "$LOG")
}

# Run command — output to log only (terminal shows spinner instead)
run() {
  if [ "$DRY_RUN" = true ]; then
    log_file "  [dry-run] $*"
    return 0
  fi
  "$@" >> "$LOG" 2>&1
  return $?
}

# Run tar — output to log only, suppress leading-slash notice
run_tar() {
  if [ "$DRY_RUN" = true ]; then
    log_file "  [dry-run] $*"
    return 0
  fi
  "$@" 2>&1 | grep -v "Removing leading" | strip_ansi >> "$LOG"
  return ${PIPESTATUS[0]}
}

# Run command with visible output on terminal (for status messages like maintenance mode)
run_visible() {
  spinner_stop
  if [ "$DRY_RUN" = true ]; then
    echo -e "  ${CYAN}[dry-run]${RESET} $*"
    log_file "  [dry-run] $*"
    spinner_start "$SPINNER_MSG"
    return 0
  fi
  "$@" 2>&1 | tee >(strip_ansi >> "$LOG")
  local exit_code=${PIPESTATUS[0]}
  spinner_start "$SPINNER_MSG"
  return $exit_code
}

ok() {
  local elapsed=$(( $(date +%s) - STEP_START_TIME ))
  spinner_stop
  echo -e "  ${GREEN}✓ OK${RESET}   $1 ($(format_duration $elapsed))"
  echo "  ✓ OK   $1 ($(format_duration $elapsed))" >> "$LOG"
  if [ -n "$CURRENT_SERVICE" ]; then
    STEP_DURATIONS["$CURRENT_SERVICE"]=$elapsed
    STEP_STATUSES["$CURRENT_SERVICE"]=0
  fi
}

fail() {
  local elapsed=$(( $(date +%s) - STEP_START_TIME ))
  spinner_stop
  echo -e "  ${RED}✗ FAIL${RESET} $1 ($(format_duration $elapsed))"
  echo "  ✗ FAIL $1 ($(format_duration $elapsed))" >> "$LOG"
  ERRORS=$((ERRORS + 1))
  if [ -n "$CURRENT_SERVICE" ]; then
    STEP_DURATIONS["$CURRENT_SERVICE"]=$elapsed
    STEP_STATUSES["$CURRENT_SERVICE"]=1
  fi
}

step() {
  STEP_START_TIME=$(date +%s)
  ((CURRENT_STEP++))
  local label="$1"
  local prefix="[${CURRENT_STEP}/${TOTAL_STEPS}] ${label} "
  local pad_width=$(( 62 - ${#prefix} ))
  [ "$pad_width" -lt 1 ] && pad_width=1
  local pad
  pad=$(printf '%0.s─' $(seq 1 "$pad_width"))
  echo ""
  echo -e "${CYAN}${BOLD}[${CURRENT_STEP}/${TOTAL_STEPS}]${RESET} ${BOLD}${label}${RESET} ${CYAN}${pad}${RESET}"
  echo "" >> "$LOG"
  echo "[${CURRENT_STEP}/${TOTAL_STEPS}] ${label} ${pad}" >> "$LOG"
  spinner_start "$label"
}

# Print a centered line inside a 54-char wide box
box_line() {
  local text="$1"
  local color="$2"
  local inner=54
  local pad_total=$(( inner - ${#text} ))
  local pad_left=$(( pad_total / 2 ))
  local pad_right=$(( pad_total - pad_left ))
  local l r
  l=$(printf '%*s' "$pad_left" '')
  r=$(printf '%*s' "$pad_right" '')
  if [ -n "$color" ]; then
    printf "${BOLD}║${RESET}%s${color}%s${RESET}%s${BOLD}║${RESET}\n" "$l" "$text" "$r"
  else
    printf "${BOLD}║${RESET}%s%s%s${BOLD}║${RESET}\n" "$l" "$text" "$r"
  fi
  printf "║%s%s%s║\n" "$l" "$text" "$r" >> "$LOG"
}

# Print a left-aligned content line inside the box
summary_line() {
  local text="$1"
  local color="$2"
  local inner=54
  local pad_width=$(( inner - ${#text} - 2 ))
  local pad
  pad=$(printf '%*s' "$pad_width" '')
  if [ -n "$color" ]; then
    printf "${BOLD}║${RESET}  ${color}%s${RESET}%s${BOLD}║${RESET}\n" "$text" "$pad"
  else
    printf "${BOLD}║${RESET}  %s%s${BOLD}║${RESET}\n" "$text" "$pad"
  fi
  printf "║  %-*s║\n" "$(( inner - 2 ))" "$text" >> "$LOG"
}


# Returns 0 (true) if files under PATH have changed since the last successful
# backup of SERVICE, or if no timestamp exists yet (first run).
has_changed() {
  local service="$1"
  local path="$2"
  local ts_file="$TIMESTAMP_DIR/$service"

  if [ "$FORCE" = true ]; then
    log_file "  --force: skipping change detection for $service"
    return 0
  fi

  if [ ! -f "$ts_file" ]; then
    log_file "  No timestamp found for $service — treating as changed (first run)"
    return 0
  fi

  local count
  count=$(find "$path" -newer "$ts_file" -type f 2>/dev/null | wc -l)
  log_file "  Changed files since last $service backup: $count"
  [ "$count" -gt 0 ]
}

# Records a successful backup timestamp for SERVICE.
# The verify script and future has_changed calls use this file.
mark_backed_up() {
  local service="$1"
  mkdir -p "$TIMESTAMP_DIR"
  touch "$TIMESTAMP_DIR/$service"
}

# Finds the most recent backup directory that contains a real (non-skipped) copy
# of ARCHIVE_NAME and returns that directory's date string.
last_real_backup_date() {
  local archive_name="$1"
  find "$BACKUP_DIR" -maxdepth 2 -name "$archive_name" \
    | sort -r | head -1 | xargs -I{} dirname {} 2>/dev/null | xargs basename 2>/dev/null
}

# Marks a step as skipped — no changes detected since the last backup.
# Writes a .SKIPPED marker so verify-backup.sh knows where to find the last
# real archive (required because exFAT does not support hardlinks or symlinks).
skip() {
  local elapsed=$(( $(date +%s) - STEP_START_TIME ))
  spinner_stop
  local label="$1"
  local archive_name="$2"
  local last_date
  last_date=$(last_real_backup_date "$archive_name")
  echo -e "  ${YELLOW}⊘ SKIP${RESET}  $label — no changes since last backup ($(format_duration $elapsed))"
  echo "  ⊘ SKIP  $label — no changes since last backup ($(format_duration $elapsed))" >> "$LOG"
  if [ -n "$last_date" ]; then
    echo "$last_date" > "$BACKUP_DIR/$DATE/${archive_name}.SKIPPED"
    log_file "  Last real archive: $last_date/$archive_name"
  fi
  if [ -n "$CURRENT_SERVICE" ]; then
    STEP_DURATIONS["$CURRENT_SERVICE"]=$elapsed
    STEP_STATUSES["$CURRENT_SERVICE"]=2
  fi
  SKIPPED_TOTAL=$(( SKIPPED_TOTAL + 1 ))
}

# Marks an entire step as skipped because --only excluded it.
# Does not count as PASS, FAIL, or a change-detection skip — just informational.
skipped_service() {
  spinner_stop
  echo -e "  ${CYAN}⊘ SKIP${RESET}  $1 — excluded by --only=${ONLY}"
  echo "  ⊘ SKIP  $1 — excluded by --only=${ONLY}" >> "$LOG"
}

# ── Pre-flight checks ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
box_line "Mnemosyne Backup — $(date '+%Y-%m-%d %H:%M')"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
{
  echo ""
  echo "╔══════════════════════════════════════════════════════╗"
  printf "║%s║\n" "$(printf '%-54s' "  Mnemosyne Backup — $(date '+%Y-%m-%d %H:%M')")"
  echo "╚══════════════════════════════════════════════════════╝"
} >> "$LOG"

# Check backup drive is mounted
if ! mountpoint -q "$BACKUP_DIR"; then
  echo -e "${RED}${BOLD}  ERROR: $BACKUP_DIR is not mounted.${RESET}"
  echo -e "${RED}  Is the WD My Passport connected?${RESET}"
  echo "  ERROR: $BACKUP_DIR is not mounted." >> "$LOG"
  exit 1
fi

# Check Docker is running
if ! docker info >/dev/null 2>&1; then
  echo -e "${RED}${BOLD}  ERROR: Docker is not running.${RESET}"
  echo "  ERROR: Docker is not running." >> "$LOG"
  exit 1
fi

# Check Vaultwarden database exists before attempting backup
if [ ! -f "/mnt/vault/vaultwarden/data/db.sqlite3" ]; then
  echo -e "${RED}${BOLD}  ERROR: Vaultwarden database not found.${RESET}"
  echo "  ERROR: Vaultwarden database not found." >> "$LOG"
  exit 1
fi

# ── Early cleanup — run BEFORE the disk space check so old backups are removed first
# This means a full disk won't block a new backup as long as there's room after pruning
echo ""
echo -e "  ${CYAN}Pruning backups older than ${RETENTION_DAYS} days...${RESET}"
find "$BACKUP_DIR" -maxdepth 1 -type d -name '????-??-??' -mtime +"$RETENTION_DAYS" -exec rm -rf {} \;
echo "  Pruned old backups." >> "$LOG"

# ── Disk space check — abort early rather than writing a corrupt half-backup
FREE_GB=$(df --output=avail -BG "$BACKUP_DIR" | tail -1 | tr -d 'G ')
USAGE_PCT=$(df "$BACKUP_DIR" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')

echo -e "  Disk: ${FREE_GB}G free, ${USAGE_PCT}% used"
echo "  Disk: ${FREE_GB}G free, ${USAGE_PCT}% used" >> "$LOG"

if [ "$FREE_GB" -lt "$MIN_FREE_GB" ]; then
  echo -e "${RED}${BOLD}  ERROR: Not enough free space on $BACKUP_DIR.${RESET}"
  echo -e "${RED}  ${FREE_GB}G free — need at least ${MIN_FREE_GB}G. Aborting to avoid corrupt backup.${RESET}"
  echo "  ERROR: Only ${FREE_GB}G free (minimum: ${MIN_FREE_GB}G). Aborting." >> "$LOG"

  # Write a failure metric so Prometheus/Grafana/Alertmanager pick this up
  TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
  {
    echo "# HELP backup_last_success_timestamp Unix timestamp of last successful backup run"
    echo "# TYPE backup_last_success_timestamp gauge"
    echo "backup_last_success_timestamp 0"
    echo "# HELP backup_last_exit_code Exit code of last backup (0 = success)"
    echo "# TYPE backup_last_exit_code gauge"
    echo "backup_last_exit_code 1"
    echo "# HELP backup_disk_free_gb Free space on backup disk in GB at time of last run"
    echo "# TYPE backup_disk_free_gb gauge"
    echo "backup_disk_free_gb ${FREE_GB}"
    echo "# HELP backup_disk_usage_percent Disk usage percent on backup disk at time of last run"
    echo "# TYPE backup_disk_usage_percent gauge"
    echo "backup_disk_usage_percent ${USAGE_PCT}"
    echo "# HELP backup_duration_seconds Duration of last backup run in seconds"
    echo "# TYPE backup_duration_seconds gauge"
    echo "backup_duration_seconds 0"
  } > "$TEXTFILE_DIR/backup.prom"

  exit 1
fi

# Also warn (but don't abort) if usage is above the configured threshold
if [ "$USAGE_PCT" -ge "$MAX_USAGE_PERCENT" ]; then
  echo -e "  ${YELLOW}⚠ WARNING: Backup disk at ${USAGE_PCT}% — approaching capacity.${RESET}"
  echo "  WARNING: Backup disk at ${USAGE_PCT}% after cleanup." >> "$LOG"
fi

# Check if today's backup already exists — abort unless --overwrite is set.
# Without this guard, a second run would silently overwrite archives that may
# already be intact, risking a corrupt partial backup if the second run fails.
if [ -d "$BACKUP_DIR/$DATE" ] && [ "$OVERWRITE" = false ] && [ "$DRY_RUN" = false ]; then
  echo -e "${YELLOW}${BOLD}  WARNING: Backup for $DATE already exists.${RESET}"
  echo -e "${YELLOW}  Use --overwrite to replace it, or --only=<service> to add a missing archive.${RESET}"
  echo "  WARNING: Backup for $DATE already exists. Aborting." >> "$LOG"
  exit 1
fi

# Create backup directory for today
if ! mkdir -p "$BACKUP_DIR/$DATE"; then
  echo -e "${RED}${BOLD}  ERROR: Failed to create backup directory.${RESET}"
  echo "  ERROR: Failed to create backup directory." >> "$LOG"
  exit 1
fi

log "  Backup directory: $BACKUP_DIR/$DATE"
[ "$FORCE" = true ]      && echo -e "  ${YELLOW}⚠ --force: change detection disabled — all services will be backed up${RESET}"
[ "$DRY_RUN" = true ]    && echo -e "  ${CYAN}⚠ --dry-run: no data will be written${RESET}"
[ "$NO_CLEANUP" = true ] && echo -e "  ${CYAN}⚠ --no-cleanup: retention cleanup skipped${RESET}"
[ "$OVERWRITE" = true ]  && echo -e "  ${YELLOW}⚠ --overwrite: existing backup for $DATE will be replaced${RESET}"
[ -n "$ONLY" ]           && echo -e "  ${CYAN}⚠ --only=${ONLY}: all other services will be skipped${RESET}"
log "=== Backup started: $(date) ==="


# ── VAULT ──────────────────────────────────────────────────────────────────────

step "Vaultwarden"
CURRENT_SERVICE="vaultwarden"
if ! should_run "vaultwarden"; then skipped_service "Vaultwarden"
else
  run sqlite3 /mnt/vault/vaultwarden/data/db.sqlite3 \
    ".backup $BACKUP_DIR/$DATE/vaultwarden-db.sqlite3"
  run_tar tar -czf "$BACKUP_DIR/$DATE/vaultwarden-data.tar.gz" \
    /mnt/vault/vaultwarden/data/
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/vaultwarden-data.tar.gz"
    ok "Vaultwarden saved"
  else
    fail "Vaultwarden failed"
  fi
fi

step "Caddy TLS certificates"
CURRENT_SERVICE="caddy"
if ! should_run "caddy"; then skipped_service "Caddy TLS certificates"
else
  run_tar tar -czf "$BACKUP_DIR/$DATE/caddy-data.tar.gz" \
    /mnt/vault/caddy/
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/caddy-data.tar.gz"
    ok "Caddy saved"
  else
    fail "Caddy failed"
  fi
fi


# ── CODEX ──────────────────────────────────────────────────────────────────────

step "Calibre Library"
CURRENT_SERVICE="calibre"
if has_changed "calibre" /mnt/codex/calibre-library/; then
  run_tar tar -czf "$BACKUP_DIR/$DATE/calibre-library.tar.gz" \
    /mnt/codex/calibre-library/
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/calibre-library.tar.gz"
    ok "Calibre Library saved"
    mark_backed_up "calibre"
  else
    fail "Calibre Library failed"
  fi
else
  skip "Calibre Library" "calibre-library.tar.gz"
fi

step "Calibre-Web Config (Docker Volume)"
CURRENT_SERVICE="calibre-web"
if ! should_run "calibre-web"; then skipped_service "Calibre-Web Config"
else
  run docker run --rm \
    -v calibre-web-config:/volume \
    -v "$BACKUP_DIR/$DATE":/backup \
    alpine tar -czf /backup/calibre-web-config.tar.gz -C /volume .
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/calibre-web-config.tar.gz"
    ok "Calibre-Web config saved"
  else
    fail "Calibre-Web config failed"
  fi
fi

step "KOSync (Docker Volume)"
CURRENT_SERVICE="kosync"
if ! should_run "kosync"; then skipped_service "KOSync"
else
  run docker run --rm \
    -v kosync-data:/volume \
    -v "$BACKUP_DIR/$DATE":/backup \
    alpine tar -czf /backup/kosync-data.tar.gz -C /volume .
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/kosync-data.tar.gz"
    ok "KOSync saved"
  else
    fail "KOSync failed"
  fi
fi

step "Syncthing"
CURRENT_SERVICE="syncthing"
if ! should_run "syncthing"; then skipped_service "Syncthing"
else
  run_tar tar -czf "$BACKUP_DIR/$DATE/syncthing-obsidian.tar.gz" \
    /mnt/codex/syncthing/obsidian/
  run_tar tar -czf "$BACKUP_DIR/$DATE/syncthing-config.tar.gz" \
    /home/youruser/.local/state/syncthing/
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/syncthing-obsidian.tar.gz"
    ok "Syncthing saved"
  else
    fail "Syncthing failed"
  fi
fi

step "Aegis 2FA backup"
CURRENT_SERVICE="aegis"
if ! should_run "aegis"; then skipped_service "Aegis 2FA backup"
else
  run_tar tar -czf "$BACKUP_DIR/$DATE/aegis-backup.tar.gz" \
    /mnt/codex/syncthing/aegis/
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/aegis-backup.tar.gz"
    ok "Aegis saved"
  else
    fail "Aegis failed"
  fi
fi

step "Gitea"
CURRENT_SERVICE="gitea"
if has_changed "gitea" /mnt/codex/gitea/data/; then
  run_tar tar -czf "$BACKUP_DIR/$DATE/gitea-data.tar.gz" \
    /mnt/codex/gitea/data/
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/gitea-data.tar.gz"
    ok "Gitea saved"
    mark_backed_up "gitea"
  else
    fail "Gitea failed"
  fi
else
  skip "Gitea" "gitea-data.tar.gz"
fi

step "Nextcloud (maintenance mode + DB dump + files)"
CURRENT_SERVICE="nextcloud"

if ! has_changed "nextcloud" /mnt/codex/nextcloud/data/; then
  skip "Nextcloud" "nextcloud-data.tar"
  # DB dump is tightly coupled to the file backup — skip both together.
  # The last real DB dump is in the same directory as the last real data archive.
  skip "Nextcloud DB" "nextcloud-db.sql"
else
  # trap ensures maintenance mode is disabled even if the script crashes
  trap 'spinner_stop; \
        docker exec -u www-data nextcloud php occ maintenance:mode --off >> "$LOG" 2>&1; \
        echo -e "  ${YELLOW}⚠ Maintenance mode force-disabled by trap${RESET}"; \
        echo "  ⚠ Maintenance mode force-disabled by trap" >> "$LOG"' EXIT

  run_visible docker exec -u www-data nextcloud php occ maintenance:mode --on

  # DB dump — stderr separate to avoid polluting the SQL file with warning messages
  docker exec nextcloud-db mariadb-dump \
    -u nextcloud -p"$MYSQL_PASSWORD" nextcloud \
    > "$BACKUP_DIR/$DATE/nextcloud-db.sql" \
    2> >(strip_ansi >> "$LOG")
  DB_EXIT=$?

  if [ "$DB_EXIT" -ne 0 ] || [ ! -s "$BACKUP_DIR/$DATE/nextcloud-db.sql" ]; then
    log_file "  DB dump failed (exit: $DB_EXIT)"
  else
    DB_SIZE=$(du -sh "$BACKUP_DIR/$DATE/nextcloud-db.sql" | cut -f1)
    log_file "  DB dump size: $DB_SIZE"
  fi

  # Uncompressed for speed — photos/videos are already compressed, gzip gives no benefit
  run_tar tar -cf "$BACKUP_DIR/$DATE/nextcloud-data.tar" \
    /mnt/codex/nextcloud/data/

  run_visible docker exec -u www-data nextcloud php occ maintenance:mode --off
  trap - EXIT

  if [ "$DB_EXIT" -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/nextcloud-data.tar"
    ok "Nextcloud saved"
    mark_backed_up "nextcloud"
  else
    fail "Nextcloud files saved but DB dump failed"
  fi
fi


# ── MONITORING ─────────────────────────────────────────────────────────────────

step "Grafana (Docker Volume)"
CURRENT_SERVICE="grafana"
if ! should_run "grafana"; then skipped_service "Grafana"
else
  run docker run --rm \
    -v grafana-data:/volume \
    -v "$BACKUP_DIR/$DATE":/backup \
    alpine tar -czf /backup/grafana-data.tar.gz -C /volume .
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/grafana-data.tar.gz"
    ok "Grafana saved"
  else
    fail "Grafana failed"
  fi
fi

step "Prometheus (Docker Volume)"
CURRENT_SERVICE="prometheus"
if ! should_run "prometheus"; then skipped_service "Prometheus"
else
  run docker run --rm \
    -v prometheus-data:/volume \
    -v "$BACKUP_DIR/$DATE":/backup \
    alpine tar -czf /backup/prometheus-data.tar.gz -C /volume .
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/prometheus-data.tar.gz"
    ok "Prometheus saved"
  else
    fail "Prometheus failed"
  fi
fi


# ── STACK CONFIGS ──────────────────────────────────────────────────────────────

step "Stack configs"
CURRENT_SERVICE="stacks"
if ! should_run "stacks"; then skipped_service "Stack configs"
else
  run_tar tar -czf "$BACKUP_DIR/$DATE/stacks-config.tar.gz" \
    /home/youruser/stacks/
  if [ $? -eq 0 ]; then
    record_archive_size "$BACKUP_DIR/$DATE/stacks-config.tar.gz"
    ok "Stack configs saved"
  else
    fail "Stack configs failed"
  fi
fi


# ── CLEANUP — second pass to catch today's run pushing usage over threshold ───
# The early cleanup removed old dirs; this final find is a safety net only.
step "Cleanup — verify retention (${RETENTION_DAYS}-day window)"
if [ "$NO_CLEANUP" = true ]; then
  spinner_stop
  echo -e "  ${CYAN}⊘ SKIP${RESET}  Cleanup — disabled by --no-cleanup"
  echo "  ⊘ SKIP  Cleanup — disabled by --no-cleanup" >> "$LOG"
else
  find "$BACKUP_DIR" -maxdepth 1 -type d -name '????-??-??' -mtime +"$RETENTION_DAYS" -exec rm -rf {} \;
  ok "Cleanup done"
fi


# ── Summary ────────────────────────────────────────────────────────────────────
BACKUP_SIZE=$(du -sh "$BACKUP_DIR/$DATE" | cut -f1)
BACKUP_USAGE=$(df "$BACKUP_DIR" | awk 'NR==2 {print $5}')
FREE_GB_FINAL=$(df --output=avail -BG "$BACKUP_DIR" | tail -1 | tr -d 'G ')

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
box_line "Summary"
echo -e "${BOLD}╠══════════════════════════════════════════════════════╣${RESET}"
{
  echo ""
  echo "╔══════════════════════════════════════════════════════╗"
  echo "║                     Summary                         ║"
  echo "╠══════════════════════════════════════════════════════╣"
} >> "$LOG"

summary_line "Finished : $(date '+%Y-%m-%d %H:%M')"
summary_line "Size     : ${BACKUP_SIZE}"
summary_line "Drive    : ${BACKUP_USAGE} used (${FREE_GB_FINAL}G free)"
if [ "$ERRORS" -eq 0 ]; then
  summary_line "Errors   : ${ERRORS}"
else
  summary_line "Errors   : ${ERRORS}" "$RED"
fi

echo -e "${BOLD}╠══════════════════════════════════════════════════════╣${RESET}"
echo "╠══════════════════════════════════════════════════════╣" >> "$LOG"

if [ "$ERRORS" -eq 0 ]; then
  box_line "✓ All ${TOTAL_STEPS} steps completed successfully" "$GREEN"
else
  box_line "✗ ${ERRORS} step(s) failed — check the log" "$RED"
fi

echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
echo "╚══════════════════════════════════════════════════════╝" >> "$LOG"
echo "" >> "$LOG"
log "=== Backup finished: $(date) ==="

# ── Prometheus Textfile Metrics ────────────────────────────────────────────────
TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

{
  echo "# HELP backup_last_success_timestamp Unix timestamp of last successful backup run"
  echo "# TYPE backup_last_success_timestamp gauge"
  echo "backup_last_success_timestamp $(date +%s)"
  echo "# HELP backup_last_run_timestamp Unix timestamp when the last backup run started"
  echo "# TYPE backup_last_run_timestamp gauge"
  echo "backup_last_run_timestamp $START_TIME"
  echo "# HELP backup_last_exit_code Exit code of last backup (0 = success)"
  echo "# TYPE backup_last_exit_code gauge"
  echo "backup_last_exit_code $ERRORS"
  echo "# HELP backup_disk_free_gb Free space on backup disk in GB at time of last run"
  echo "# TYPE backup_disk_free_gb gauge"
  echo "backup_disk_free_gb ${FREE_GB_FINAL}"
  echo "# HELP backup_disk_usage_percent Disk usage percent on backup disk at time of last run"
  echo "# TYPE backup_disk_usage_percent gauge"
  echo "backup_disk_usage_percent $(df "$BACKUP_DIR" | awk 'NR==2 {gsub(/%/,"",$5); print $5}')"
  echo "# HELP backup_duration_seconds Total duration of last backup run in seconds"
  echo "# TYPE backup_duration_seconds gauge"
  echo "backup_duration_seconds $DURATION"
  echo "# HELP backup_skipped_total Number of services skipped in the last run (no changes detected)"
  echo "# TYPE backup_skipped_total gauge"
  echo "backup_skipped_total $SKIPPED_TOTAL"
  echo "# HELP backup_step_duration_seconds Duration of each backup step in seconds"
  echo "# TYPE backup_step_duration_seconds gauge"
  for service in "${!STEP_DURATIONS[@]}"; do
    echo "backup_step_duration_seconds{service=\"${service}\"} ${STEP_DURATIONS[$service]}"
  done
  echo "# HELP backup_step_status Status of each backup step (0=ok, 1=fail, 2=skip)"
  echo "# TYPE backup_step_status gauge"
  for service in "${!STEP_STATUSES[@]}"; do
    echo "backup_step_status{service=\"${service}\"} ${STEP_STATUSES[$service]}"
  done
  echo "# HELP backup_archive_size_bytes Size of each backup archive in bytes"
  echo "# TYPE backup_archive_size_bytes gauge"
  for service in "${!ARCHIVE_SIZES[@]}"; do
    echo "backup_archive_size_bytes{service=\"${service}\"} ${ARCHIVE_SIZES[$service]}"
  done
} > "$TEXTFILE_DIR/backup.prom"

[ "$ERRORS" -eq 0 ] && exit 0 || exit 1