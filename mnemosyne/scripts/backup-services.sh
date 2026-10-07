#!/bin/bash

# ── Configuration ──────────────────────────────────────────────────────────────
BACKUP_DIR="/mnt/backup"

DATE=$(date +%Y-%m-%d)
RETENTION_DAYS=7                  # Reduced from 14 — 7 days is enough for a homelab
# Abort before starting if less than this many GB are free. One nightly run
# writes roughly 73 GB (Nextcloud ~29 + Immich ~44), so the old value of 40 was
# smaller than a single run: the preflight would pass and the backup would then
# run out of space halfway through, leaving a truncated archive behind.
# 150 GB is two full runs of headroom on the 3.6 TB HDD.
MIN_FREE_GB=150
# Percentage warning after cleanup. Kept as a coarse second signal only — on a
# 3.6 TB drive 85% still leaves ~540 GB, so this fires long after
# MIN_FREE_GB would have. The absolute check above is the one that matters.
# (The comment used to say "Abort"; it only ever warned.)
MAX_USAGE_PERCENT=85
LOG="/var/log/backup-services.log"

# Offsite backup is handled by a separate, decoupled restic service
# (restic-offsite.service), triggered at the end of this script on success.
# It is NOT an inline step here — this keeps the slow, network-bound offsite
# transfer out of the local backup's critical path and gives it independent
# success/failure tracking. See restic-offsite.sh and SETUP_Offsite.md.

# Tracks the last successful backup timestamp per service.
# Stored on the local filesystem (not the external SSD) so it's always available.
# Used to skip backups when no files have changed since the last run.
# Note: the backup SSD is exFAT — no hardlinks or symlinks available.
# Skipped steps write a .SKIPPED marker file containing the date of the last
# real archive so verify-backup.sh can look it up.
TIMESTAMP_DIR="/var/lib/backup-timestamps"

# A skip chain must never outlive the retention window. If the last real archive
# for a service is older than this, change detection is overridden and a full
# backup is taken — otherwise the cleanup step eventually deletes the archive the
# .SKIPPED markers point at, leaving a chain of markers referencing nothing.
# That happened to calibre-library in August 2026: five consecutive markers, zero
# archives. Two days of headroom below RETENTION_DAYS.
MAX_SKIP_DAYS=$(( RETENTION_DAYS - 2 ))

# Minimum number of entries a freshly written archive must contain. An archive of
# an empty directory is a valid, readable, ~85-byte tar.gz with exactly one entry
# ("./") — indistinguishable from success unless the content is asserted.
MIN_ARCHIVE_ENTRIES=2

# ── Backup source paths ────────────────────────────────────────────────────────
# Single source of truth for every path this script archives. Declared here so
# check_mount_drift() can compare them against what the running containers
# actually use. Changing a stack's mount without changing this map is the failure
# this section exists to make loud.
declare -A SOURCE_PATHS=(
  [vaultwarden]="/mnt/vault/vaultwarden/data"
  [caddy]="/mnt/vault/caddy"
  [calibre]="/mnt/codex/calibre-library"
  [calibre-web]="/mnt/codex/calibre-web-config"
  [kosync]="/mnt/codex/kosync/data"
  [syncthing]="/mnt/codex/syncthing/obsidian"
  [aegis]="/mnt/codex/syncthing/aegis"
  [gitea]="/mnt/codex/gitea/data"
  [ghost]="/mnt/codex/ghost/content"
  [nextcloud]="/mnt/codex/nextcloud/data"
  [immich]="/mnt/codex/immich/upload"
  [grafana]="/mnt/codex/grafana"
  [jobiris]="/mnt/vault/jobiris"
  [gitea-runner]="/mnt/codex/gitea/runner"
  [tado]="/mnt/codex/tado-exporter"
  [midea]="/mnt/codex/midea-exporter"
  [netatmo]="/mnt/codex/netatmo-exporter"
)

# Bind mounts that are intentionally not tar'd because they are captured by
# another mechanism (a database dump). Everything else under /mnt/codex or
# /mnt/vault that a running container binds is expected to be covered.
DRIFT_IGNORE=(
  "/mnt/codex/nextcloud/db"    # captured by mariadb-dump
  "/mnt/codex/immich/db"       # captured by pg_dumpall
  "/mnt/codex/ghost/db"        # captured by mariadb-dump
  "/mnt/codex/prometheus"      # not backed up by design — see the Prometheus
                               # section below for the full reasoning
  "/mnt/codex/loki"            # log storage; regenerates as logs come in
  "/mnt/codex/alloy"           # collector state; rebuilt on start
  "/mnt/codex/alertmanager"    # silences and notification state; rebuilt from
                               # the config, and a lost silence expires anyway
  "/mnt/codex/carousel/jobs"   # regenerable job artefacts
  "/mnt/codex/wakapi"          # DELIBERATE ACCEPTED LOSS, not regenerable.
                               # SQLite with years of coding statistics —
                               # decided 2026-08-30 that it is not worth a
                               # backup step. Revisit if that changes.
)

# ── Flags ──────────────────────────────────────────────────────────────────────
FORCE=false
DRY_RUN=false
NO_CLEANUP=false
OVERWRITE=false
NO_OFFSITE=false
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
    --no-offsite)      NO_OFFSITE=true ;;
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
      echo "  --no-offsite         Skip triggering the restic offsite service"
      echo "  --only=<service>     Back up a single service only"
      echo "                       Services: vaultwarden, caddy, calibre, calibre-web,"
      echo "                                 kosync, syncthing, aegis, gitea, nextcloud,"
      echo "                                 ghost, immich, grafana, jobiris,"
      echo "                                 gitea-runner, exporters, stacks"
      echo "  --retention=<days>   Override the default retention period"
      exit 1
      ;;
  esac
done

# Recomputed here because --retention= is parsed above and MAX_SKIP_DAYS is
# derived from it. Floor of 1 so --retention=1 or =2 cannot produce 0 or a
# negative value, which would force a full backup on every single run.
MAX_SKIP_DAYS=$(( RETENTION_DAYS - 2 ))
[ "$MAX_SKIP_DAYS" -lt 1 ] && MAX_SKIP_DAYS=1

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
TOTAL_STEPS=17
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
# Case-insensitive: a typo'd --only=Vaultwarden must still match "vaultwarden",
# not silently skip every service while reporting a clean run.
should_run() {
  local service="$1"
  [ -z "$ONLY" ] || [ "${ONLY,,}" = "${service,,}" ]
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
  return "${PIPESTATUS[0]}"
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
  return "$exit_code"
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


# Returns 0 (true) if SOURCE exists as a directory and is not empty.
# Every service block calls this before archiving. Without it, a renamed,
# unmounted or migrated source produces a valid archive of nothing and the run
# reports success — the exact failure that went unnoticed for nine nights when
# four services moved from named volumes to bind mounts.
assert_source() {
  local service="$1"
  local path="$2"

  if [ "$DRY_RUN" = true ]; then
    log_file "  [dry-run] would assert source: $path"
    return 0
  fi
  if [ ! -d "$path" ]; then
    fail "$service — source path does not exist: $path"
    return 1
  fi
  if [ -z "$(ls -A "$path" 2>/dev/null)" ]; then
    fail "$service — source path is empty: $path"
    return 1
  fi
  return 0
}

# Returns 0 (true) if FILE exists and contains at least MIN_ARCHIVE_ENTRIES
# members. tar exiting 0 only proves it wrote a syntactically valid archive, not
# that it wrote any data. The listing is bounded to MIN_ARCHIVE_ENTRIES members
# so the check costs the same on a 70 GB .tar as on a 2 KB one — tar stops at
# the first members and SIGPIPE ends the read.
assert_archive() {
  local label="$1"
  local file="$2"

  if [ "$DRY_RUN" = true ]; then
    log_file "  [dry-run] would assert archive: $file"
    return 0
  fi
  if [ ! -f "$file" ]; then
    fail "$label — archive was not created: $(basename "$file")"
    return 1
  fi

  local entries
  case "$file" in
    *.tar.gz) entries=$(tar -tzf "$file" 2>/dev/null | head -"$MIN_ARCHIVE_ENTRIES" | wc -l) ;;
    *.tar)    entries=$(tar -tf  "$file" 2>/dev/null | head -"$MIN_ARCHIVE_ENTRIES" | wc -l) ;;
    *)        entries=$MIN_ARCHIVE_ENTRIES ;;
  esac

  if [ "$entries" -lt "$MIN_ARCHIVE_ENTRIES" ]; then
    fail "$label — archive contains only $entries entr(ies): $(basename "$file") ($(stat -c%s "$file") bytes)"
    return 1
  fi
  log_file "  Archive assertion passed for $label (>= $entries entries)"
  return 0
}

# Returns 0 (true) if files under PATH have changed since the last successful
# backup of SERVICE, or if no timestamp exists yet (first run).
#
# ARCHIVE_NAME is optional. When given, the skip chain is also bounded: if the
# last real archive is missing or close to falling out of the retention window,
# change detection is overridden so a fresh full archive is written before the
# old one is pruned.
has_changed() {
  local service="$1"
  local path="$2"
  local archive_name="$3"
  local ts_file="$TIMESTAMP_DIR/$service"

  if [ "$FORCE" = true ]; then
    log_file "  --force: skipping change detection for $service"
    return 0
  fi

  # A vanished or emptied source must never read as "nothing changed".
  # find on a nonexistent path returns zero results, which is numerically
  # identical to "no modifications" — return "changed" so the service block
  # runs assert_source and turns this into a hard FAIL instead of a silent skip.
  if [ ! -d "$path" ] || [ -z "$(ls -A "$path" 2>/dev/null)" ]; then
    log_file "  Source missing or empty for $service ($path) — not treating as unchanged"
    return 0
  fi

  if [ ! -f "$ts_file" ]; then
    log_file "  No timestamp found for $service — treating as changed (first run)"
    return 0
  fi

  # Bound the skip chain against the retention window.
  if [ -n "$archive_name" ]; then
    local last_date
    last_date=$(last_real_backup_date "$archive_name")
    if [ -z "$last_date" ]; then
      log_file "  No real archive left for $archive_name — forcing full backup"
      return 0
    fi
    local last_epoch age_days
    last_epoch=$(date -d "$last_date" +%s 2>/dev/null)
    if [ -z "$last_epoch" ]; then
      log_file "  Unparsable reference date '$last_date' for $archive_name — forcing full backup"
      return 0
    fi
    age_days=$(( ( $(date +%s) - last_epoch ) / 86400 ))
    if [ "$age_days" -ge "$MAX_SKIP_DAYS" ]; then
      log_file "  Last real $archive_name is ${age_days}d old (limit ${MAX_SKIP_DAYS}d) — forcing full backup"
      return 0
    fi
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
  local label="$1"
  local archive_name="$2"
  local last_date
  last_date=$(last_real_backup_date "$archive_name")

  # A marker pointing at nothing is worse than no marker: verify-backup.sh
  # reports SKIP (counted as a pass) while no archive exists anywhere. If the
  # chain has no anchor, this is a failure, not a skip.
  if [ -z "$last_date" ]; then
    fail "$label — skip requested but no real archive of $archive_name exists in $BACKUP_DIR"
    return
  fi

  spinner_stop
  echo -e "  ${YELLOW}⊘ SKIP${RESET}  $label — no changes since last backup ($(format_duration $elapsed))"
  echo "  ⊘ SKIP  $label — no changes since last backup ($(format_duration $elapsed))" >> "$LOG"
  echo "$last_date" > "$BACKUP_DIR/$DATE/${archive_name}.SKIPPED"
  log_file "  Last real archive: $last_date/$archive_name"

  if [ -n "$CURRENT_SERVICE" ]; then
    STEP_DURATIONS["$CURRENT_SERVICE"]=$elapsed
    STEP_STATUSES["$CURRENT_SERVICE"]=2
  fi
  SKIPPED_TOTAL=$(( SKIPPED_TOTAL + 1 ))
}

# ── Mount drift detection ──────────────────────────────────────────────────────
# Compares every persistent bind mount of every running container against
# SOURCE_PATHS. Anything not covered is reported and counted.
#
# This exists because of the August 2026 incident: calibre-web, kosync, grafana
# and prometheus were migrated from named volumes to bind mounts. This script
# kept archiving the orphaned volumes, which docker silently re-creates empty on
# demand, so tar succeeded every night on an empty directory and the run reported
# "all steps completed successfully" for nine consecutive days.
#
# Reported as a warning rather than a hard failure on purpose: adding a new stack
# is a normal event and must not block the nightly backup or the offsite trigger.
# The count is exported as backup_uncovered_mounts so Alertmanager owns the
# escalation. Change to fail() if a blocking gate is preferred.
UNCOVERED_MOUNTS=0

check_mount_drift() {
  local containers c src decl svc covered ignored ign

  containers=$(docker ps --format '{{.Names}}' 2>/dev/null)
  [ -z "$containers" ] && return 0

  for c in $containers; do
    while read -r src; do
      [ -z "$src" ] && continue

      # Persistent data lives under these two roots only. Config bind mounts
      # from the git working tree are covered by the "stacks" archive.
      case "$src" in
        /mnt/codex/*|/mnt/vault/*) ;;
        *) continue ;;
      esac

      # Single-file binds (certificates, config files) are not backup targets.
      [ -d "$src" ] || continue

      ignored=false
      for ign in "${DRIFT_IGNORE[@]}"; do
        if [ "$src" = "$ign" ] || [[ "$src" == "$ign"/* ]]; then
          ignored=true
          break
        fi
      done
      [ "$ignored" = true ] && continue

      covered=false
      for svc in "${!SOURCE_PATHS[@]}"; do
        decl="${SOURCE_PATHS[$svc]%/}"
        if [ "$src" = "$decl" ] || [[ "$src" == "$decl"/* ]]; then
          covered=true
          break
        fi
      done

      if [ "$covered" = false ]; then
        echo -e "  ${YELLOW}⚠ DRIFT${RESET}  ${c}: ${src} — not covered by any backup source"
        echo "  ⚠ DRIFT  ${c}: ${src} — not covered by any backup source" >> "$LOG"
        UNCOVERED_MOUNTS=$(( UNCOVERED_MOUNTS + 1 ))
      fi
    done < <(docker inspect "$c" \
      --format '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{"\n"}}{{end}}{{end}}' 2>/dev/null)
  done

  # An orphaned named volume left over from a bind-mount migration is the same
  # class of problem seen from the other side — docker re-creates it empty on
  # demand, so nothing ever errors.
  local vol
  for vol in $(docker volume ls -q 2>/dev/null); do
    case "$vol" in
      calibre-web-config|kosync-data|grafana-data|prometheus-data)
        echo -e "  ${YELLOW}⚠ DRIFT${RESET}  orphaned named volume still present: ${vol}"
        echo "  ⚠ DRIFT  orphaned named volume still present: ${vol}" >> "$LOG"
        ;;
    esac
  done
}

# Marks an entire step as skipped because --only excluded it.
# Does not count as PASS, FAIL, or a change-detection skip — just informational.
skipped_service() {
  spinner_stop
  echo -e "  ${CYAN}⊘ SKIP${RESET}  $1 — excluded by --only=${ONLY}"
  echo "  ⊘ SKIP  $1 — excluded by --only=${ONLY}" >> "$LOG"
}

# ── Validate --only against known services ──────────────────────────────────
# A typo here (Vaultwarden vs vaultwarden, or a plain misspelling) must not
# silently match nothing and skip every single service while still reporting
# a clean run — that happened, and it's the most dangerous failure mode of
# all: zero backups taken, exit code 0, "all steps completed successfully".
VALID_SERVICES="vaultwarden caddy calibre calibre-web kosync syncthing aegis gitea ghost nextcloud immich grafana jobiris gitea-runner exporters stacks"
if [ -n "$ONLY" ]; then
  MATCH=false
  for svc in $VALID_SERVICES; do
    [ "${ONLY,,}" = "$svc" ] && MATCH=true && break
  done
  if [ "$MATCH" = false ]; then
    echo -e "${RED}${BOLD}  ERROR: --only=${ONLY} is not a known service.${RESET}"
    echo -e "${RED}  Valid values: ${VALID_SERVICES}${RESET}"
    echo "  ERROR: --only=${ONLY} is not a known service." >> "$LOG"
    exit 1
  fi
fi

# ── Concurrency guard ────────────────────────────────────────────────────────
# Prevents two invocations from running at once — e.g. a nightly run that ran
# long overlapping with the next cron tick. Offsite is now a separate restic
# service with its own lock, so this guard only protects the local backup.
LOCK_FILE="/var/run/backup-services.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
  echo -e "${RED}${BOLD}  ERROR: Another instance of backup-services.sh is already running.${RESET}"
  echo "  ERROR: Lock held on $LOCK_FILE — exiting without starting a second run." >> "$LOG"
  exit 1
fi

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

# Compare live container mounts against the declared backup sources
echo ""
echo -e "  ${CYAN}Checking mount drift against declared backup sources...${RESET}"
echo "  Checking mount drift..." >> "$LOG"
check_mount_drift
if [ "$UNCOVERED_MOUNTS" -eq 0 ]; then
  echo -e "  ${GREEN}No uncovered persistent mounts.${RESET}"
  echo "  No uncovered persistent mounts." >> "$LOG"
else
  echo -e "  ${YELLOW}${UNCOVERED_MOUNTS} persistent mount(s) have no backup coverage.${RESET}"
  echo "  ${UNCOVERED_MOUNTS} persistent mount(s) have no backup coverage." >> "$LOG"
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
[ "$NO_OFFSITE" = true ] && echo -e "  ${CYAN}⚠ --no-offsite: restic offsite trigger will be skipped${RESET}"
[ -n "$ONLY" ]           && echo -e "  ${CYAN}⚠ --only=${ONLY}: all other services will be skipped${RESET}"
log "=== Backup started: $(date) ==="


# ── VAULT ──────────────────────────────────────────────────────────────────────

step "Vaultwarden"
CURRENT_SERVICE="vaultwarden"
if ! should_run "vaultwarden"; then skipped_service "Vaultwarden"
else
  # Exit codes captured separately: the previous version tested $? after the tar
  # only, so a failed sqlite3 .backup was reported as success.
  if assert_source "Vaultwarden" "${SOURCE_PATHS[vaultwarden]}"; then
    run sqlite3 /mnt/vault/vaultwarden/data/db.sqlite3 \
      ".backup $BACKUP_DIR/$DATE/vaultwarden-db.sqlite3"
    DB_RC=$?
    run_tar tar -czf "$BACKUP_DIR/$DATE/vaultwarden-data.tar.gz" \
      "${SOURCE_PATHS[vaultwarden]}/"
    TAR_RC=$?

    if [ "$DB_RC" -ne 0 ]; then
      fail "Vaultwarden — sqlite3 .backup failed (exit: $DB_RC)"
    elif [ "$TAR_RC" -ne 0 ]; then
      fail "Vaultwarden — tar failed (exit: $TAR_RC)"
    elif assert_archive "Vaultwarden" "$BACKUP_DIR/$DATE/vaultwarden-data.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/vaultwarden-data.tar.gz"
      ok "Vaultwarden saved"
    fi
  fi
fi

step "Caddy TLS certificates"
CURRENT_SERVICE="caddy"
if ! should_run "caddy"; then skipped_service "Caddy TLS certificates"
else
  if assert_source "Caddy" "${SOURCE_PATHS[caddy]}"; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/caddy-data.tar.gz" \
      "${SOURCE_PATHS[caddy]}/"
    if [ $? -ne 0 ]; then
      fail "Caddy failed"
    elif assert_archive "Caddy" "$BACKUP_DIR/$DATE/caddy-data.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/caddy-data.tar.gz"
      ok "Caddy saved"
    fi
  fi
fi


# ── CODEX ──────────────────────────────────────────────────────────────────────

step "Calibre Library"
CURRENT_SERVICE="calibre"
if ! should_run "calibre"; then
  skipped_service "Calibre Library"
elif has_changed "calibre" "${SOURCE_PATHS[calibre]}" "calibre-library.tar.gz"; then
  if assert_source "Calibre Library" "${SOURCE_PATHS[calibre]}"; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/calibre-library.tar.gz" \
      "${SOURCE_PATHS[calibre]}/"
    if [ $? -ne 0 ]; then
      fail "Calibre Library failed"
    elif assert_archive "Calibre Library" "$BACKUP_DIR/$DATE/calibre-library.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/calibre-library.tar.gz"
      ok "Calibre Library saved"
      mark_backed_up "calibre"
    fi
  fi
else
  skip "Calibre Library" "calibre-library.tar.gz"
fi

# Migrated 2026-08-19 from the named volume "calibre-web-config" to the bind
# mount the container has actually used since 2026-08-10. The old form was
# `docker run -v calibre-web-config:/volume ... tar`, which can never fail:
# docker creates the named volume on demand if it is absent, so tar always
# found a directory, always exited 0, and always produced an 85-byte archive.
step "Calibre-Web Config"
CURRENT_SERVICE="calibre-web"
if ! should_run "calibre-web"; then skipped_service "Calibre-Web Config"
else
  if assert_source "Calibre-Web config" "${SOURCE_PATHS[calibre-web]}"; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/calibre-web-config.tar.gz" \
      "${SOURCE_PATHS[calibre-web]}/"
    if [ $? -ne 0 ]; then
      fail "Calibre-Web config failed"
    elif assert_archive "Calibre-Web config" "$BACKUP_DIR/$DATE/calibre-web-config.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/calibre-web-config.tar.gz"
      ok "Calibre-Web config saved"
    fi
  fi
fi

# Migrated 2026-08-19 from the named volume "kosync-data" — same reasoning as
# Calibre-Web above. app.db is SQLite but sees no concurrent writes at 02:00
# (KOReader syncs on demand from a single device), so no container stop.
step "KOSync"
CURRENT_SERVICE="kosync"
if ! should_run "kosync"; then skipped_service "KOSync"
else
  if assert_source "KOSync" "${SOURCE_PATHS[kosync]}"; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/kosync-data.tar.gz" \
      "${SOURCE_PATHS[kosync]}/"
    if [ $? -ne 0 ]; then
      fail "KOSync failed"
    elif assert_archive "KOSync" "$BACKUP_DIR/$DATE/kosync-data.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/kosync-data.tar.gz"
      ok "KOSync saved"
    fi
  fi
fi

step "Syncthing"
CURRENT_SERVICE="syncthing"
if ! should_run "syncthing"; then skipped_service "Syncthing"
else
  # Both archives asserted individually: the previous version tested $? after
  # the config tar only, so a failed vault tar was reported as success.
  if assert_source "Syncthing vault" "${SOURCE_PATHS[syncthing]}"; then
    # The vault archive. Previously this tar was accidentally overwritten by
    # the config tar below — both wrote to syncthing-config.tar.gz, so
    # syncthing-obsidian.tar.gz silently stopped being produced and
    # assert_archive kept passing against the previous day's file.
    run_tar tar -czf "$BACKUP_DIR/$DATE/syncthing-obsidian.tar.gz" \
      "${SOURCE_PATHS[syncthing]}/"
    VAULT_RC=$?

    # index-v*.db is Syncthing's LevelDB sync state: 14 MB of derived data
    # against 36 KB of actual config. It is written continuously, so tar hits
    # "file changed as we read it" and exits 1 — the same race that took
    # Prometheus out of this backup on 2026-08-19.
    #
    # Syncthing rebuilds the index on startup when it is missing, so nothing
    # is lost: config.xml plus the certificates are what a restore needs.
    # syncthing.lock excluded too — a restored lock file can block startup.
    #
    # Patterns need the */ prefix: tar matches the stored path
    # (home/youruser/.local/state/syncthing/index-v0.14.0.db), not the
    # basename, so a bare 'index-v*.db' silently matched nothing.
    run_tar tar -czf "$BACKUP_DIR/$DATE/syncthing-config.tar.gz" \
      --exclude='*/index-v*.db' \
      --exclude='*/syncthing.lock' \
      /home/youruser/.local/state/syncthing/
    CONF_RC=$?

    if [ "$VAULT_RC" -ne 0 ] || [ "$CONF_RC" -ne 0 ]; then
      fail "Syncthing failed (vault:$VAULT_RC config:$CONF_RC)"
    elif assert_archive "Syncthing vault" "$BACKUP_DIR/$DATE/syncthing-obsidian.tar.gz" \
      && assert_archive "Syncthing config" "$BACKUP_DIR/$DATE/syncthing-config.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/syncthing-obsidian.tar.gz"
      ok "Syncthing saved"
    fi
  fi
fi

step "Aegis 2FA backup"
CURRENT_SERVICE="aegis"
if ! should_run "aegis"; then skipped_service "Aegis 2FA backup"
else
  if assert_source "Aegis 2FA backup" "${SOURCE_PATHS[aegis]}"; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/aegis-backup.tar.gz" \
      "${SOURCE_PATHS[aegis]}/"
    if [ $? -ne 0 ]; then
      fail "Aegis failed"
    elif assert_archive "Aegis 2FA backup" "$BACKUP_DIR/$DATE/aegis-backup.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/aegis-backup.tar.gz"
      ok "Aegis saved"
    fi
  fi
fi

step "Gitea"
CURRENT_SERVICE="gitea"
if ! should_run "gitea"; then
  skipped_service "Gitea"
elif has_changed "gitea" "${SOURCE_PATHS[gitea]}" "gitea-data.tar.gz"; then
  # gitea.db is Gitea's built-in SQLite database, actively written by the
  # running container (e.g. the Act Runner on every push/webhook). A raw tar
  # read of a live SQLite file races with concurrent writes — "file changed
  # as we read it". Unlike Vaultwarden (sqlite3 .backup, safe against
  # concurrent writers), Gitea has no such API exposed, so the container is
  # stopped briefly instead. Acceptable: Gitea is not internet-facing and has
  # no other consumers at 2am.
  if assert_source "Gitea" "${SOURCE_PATHS[gitea]}"; then
    run docker stop gitea
    run_tar tar -czf "$BACKUP_DIR/$DATE/gitea-data.tar.gz" \
      "${SOURCE_PATHS[gitea]}/"
    TAR_RC=$?
    # Restarted before the assertion so a failed assertion can never leave
    # Gitea stopped.
    run docker start gitea

    if [ "$TAR_RC" -ne 0 ]; then
      fail "Gitea failed"
    elif assert_archive "Gitea" "$BACKUP_DIR/$DATE/gitea-data.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/gitea-data.tar.gz"
      ok "Gitea saved"
      mark_backed_up "gitea"
    fi
  fi
else
  skip "Gitea" "gitea-data.tar.gz"
fi

# Ghost — girlfriend's blog. Two artefacts, same reasoning as Nextcloud and
# Immich: the database is DUMPED, never tar'd. ghost-db is a live MySQL
# instance; a raw tar of its datadir while it is running produces a
# guaranteed-corrupt backup that only reveals itself at restore time.
#
# Added 2026-08-23. Until then this stack had no backup at all — it was the
# highest-priority entry on the mount-drift list because the data is not mine
# to lose.
step "Ghost (DB dump + content)"
CURRENT_SERVICE="ghost"
if ! should_run "ghost"; then
  skipped_service "Ghost"
elif has_changed "ghost" "${SOURCE_PATHS[ghost]}" "ghost-content.tar.gz"; then
  # Sourced locally rather than globally so a missing Ghost .env only fails
  # this step, not unrelated --only runs. Same pattern as Immich.
  ENV_GHOST="/home/youruser/stacks/ghost/.env"
  if [ ! -f "$ENV_GHOST" ]; then
    fail "Ghost — .env not found: $ENV_GHOST"
  elif assert_source "Ghost content" "${SOURCE_PATHS[ghost]}"; then
    # shellcheck source=/dev/null
    source "$ENV_GHOST"

    # mysqldump, not mariadb-dump: ghost-db runs mysql:8.0, unlike
    # nextcloud-db. Database name and user are hardcoded as "ghost" in the
    # stack's docker-compose.yml (MYSQL_DATABASE / MYSQL_USER), not taken from
    # .env — only the password is a variable.
    #
    # --single-transaction gives a consistent snapshot of the InnoDB tables
    # without locking them, so the blog stays writable for the duration of the
    # dump. Without it mysqldump takes a read lock across all tables.
    #
    # stderr kept separate so warnings never land inside the SQL file.
    docker exec ghost-db mysqldump \
      --single-transaction \
      -u ghost -p"$GHOST_DB_PASSWORD" ghost \
      > "$BACKUP_DIR/$DATE/ghost-db.sql" \
      2> >(strip_ansi >> "$LOG")
    DB_EXIT=$?

    if [ "$DB_EXIT" -ne 0 ] || [ ! -s "$BACKUP_DIR/$DATE/ghost-db.sql" ]; then
      log_file "  Ghost DB dump failed (exit: $DB_EXIT)"
    else
      DB_SIZE=$(du -sh "$BACKUP_DIR/$DATE/ghost-db.sql" | cut -f1)
      log_file "  Ghost DB dump size: $DB_SIZE"
    fi

    # Content is themes, images and uploads — compressible, so gzip unlike the
    # photo/video archives.
    run_tar tar -czf "$BACKUP_DIR/$DATE/ghost-content.tar.gz" \
      "${SOURCE_PATHS[ghost]}/"
    TAR_EXIT=$?

    if [ "$DB_EXIT" -ne 0 ] || [ "$TAR_EXIT" -ne 0 ]; then
      fail "Ghost — DB dump or content archive failed (db:$DB_EXIT tar:$TAR_EXIT)"
    elif assert_archive "Ghost content" "$BACKUP_DIR/$DATE/ghost-content.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/ghost-content.tar.gz"
      ok "Ghost saved"
      mark_backed_up "ghost"
    fi
  fi
else
  skip "Ghost" "ghost-content.tar.gz"
fi

step "Nextcloud (maintenance mode + DB dump + files)"
CURRENT_SERVICE="nextcloud"

if ! should_run "nextcloud"; then
  skipped_service "Nextcloud"
elif ! has_changed "nextcloud" "${SOURCE_PATHS[nextcloud]}" "nextcloud-data.tar"; then
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

  # Uncompressed for speed — photos/videos are already compressed, gzip gives no benefit.
  # --sort=name gives deterministic member order so restic can deduplicate this
  # 73 GB archive across snapshots; without it, shifting byte offsets defeat dedup.
  run_tar tar --sort=name -cf "$BACKUP_DIR/$DATE/nextcloud-data.tar" \
    "${SOURCE_PATHS[nextcloud]}/"

  run_visible docker exec -u www-data nextcloud php occ maintenance:mode --off
  trap - EXIT

  if [ "$DB_EXIT" -ne 0 ]; then
    fail "Nextcloud files saved but DB dump failed"
  elif assert_archive "Nextcloud data" "$BACKUP_DIR/$DATE/nextcloud-data.tar"; then
    record_archive_size "$BACKUP_DIR/$DATE/nextcloud-data.tar"
    ok "Nextcloud saved"
    mark_backed_up "nextcloud"
  fi
fi


# ── IMMICH ─────────────────────────────────────────────────────────────────────
# Previously documented as backed up (16_Immich.md, "status: Done") but never
# actually implemented — Immich had zero backup coverage. This closes that gap.

step "Immich"
CURRENT_SERVICE="immich"
if ! should_run "immich"; then
  skipped_service "Immich"
elif has_changed "immich" "${SOURCE_PATHS[immich]}" "immich-upload.tar"; then
  # Sourced locally rather than globally (unlike ENV_NEXTCLOUD above) so a
  # missing Immich .env only fails this step, not unrelated --only runs.
  ENV_IMMICH="/home/youruser/stacks/immich/.env"
  if [ ! -f "$ENV_IMMICH" ]; then
    fail "Immich — .env not found: $ENV_IMMICH"
  else
    # shellcheck source=/dev/null
    source "$ENV_IMMICH"

    # PostgreSQL dump — mirrors the Nextcloud pattern: dump instead of
    # raw-copying live DB files, which would risk a torn/inconsistent backup.
    # Container name is "immich-db" per the stack's docker-compose.yml.
    docker exec immich-db pg_dumpall -U "$DB_USERNAME" \
      > "$BACKUP_DIR/$DATE/immich-db.sql" \
      2> >(strip_ansi >> "$LOG")
    DB_EXIT=$?

    if [ "$DB_EXIT" -ne 0 ] || [ ! -s "$BACKUP_DIR/$DATE/immich-db.sql" ]; then
      log_file "  Immich DB dump failed (exit: $DB_EXIT)"
    else
      DB_SIZE=$(du -sh "$BACKUP_DIR/$DATE/immich-db.sql" | cut -f1)
      log_file "  Immich DB dump size: $DB_SIZE"
    fi

    # Uncompressed + deterministic order — same reasoning as nextcloud-data.tar:
    # photos/videos are already compressed (gzip gains nothing), and --sort=name
    # keeps restic's deduplication stable across nightly snapshots.
    # encoded-video/ is derived data Immich regenerates on demand — excluded,
    # same as documented in 16_Immich.md.
    run_tar tar --sort=name -cf "$BACKUP_DIR/$DATE/immich-upload.tar" \
      --exclude='encoded-video' \
      "${SOURCE_PATHS[immich]}/"
    TAR_EXIT=$?

    if [ "$DB_EXIT" -ne 0 ] || [ "$TAR_EXIT" -ne 0 ]; then
      fail "Immich — DB dump or upload archive failed (db:$DB_EXIT tar:$TAR_EXIT)"
    elif assert_archive "Immich upload" "$BACKUP_DIR/$DATE/immich-upload.tar"; then
      record_archive_size "$BACKUP_DIR/$DATE/immich-upload.tar"
      ok "Immich saved"
      mark_backed_up "immich"
    fi
  fi
else
  skip "Immich" "immich-upload.tar"
fi


# ── MONITORING ─────────────────────────────────────────────────────────────────

# Migrated 2026-08-19 from the named volume "grafana-data" to the bind mount in
# use since 2026-08-10. The container is stopped for the duration of the tar:
# grafana.db is SQLite and is written live (sessions, annotations, dashboard
# saves). Same reasoning as Gitea — Grafana exposes no online backup API, and a
# raw tar of a live SQLite file races with writers. Prometheus keeps scraping
# while Grafana is down; only the UI is briefly unavailable at 02:00.
step "Grafana"
CURRENT_SERVICE="grafana"
if ! should_run "grafana"; then skipped_service "Grafana"
else
  if assert_source "Grafana" "${SOURCE_PATHS[grafana]}"; then
    run docker stop grafana
    run_tar tar -czf "$BACKUP_DIR/$DATE/grafana-data.tar.gz" \
      "${SOURCE_PATHS[grafana]}/"
    TAR_RC=$?
    run docker start grafana

    if [ "$TAR_RC" -ne 0 ]; then
      fail "Grafana failed"
    elif assert_archive "Grafana" "$BACKUP_DIR/$DATE/grafana-data.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/grafana-data.tar.gz"
      ok "Grafana saved"
    fi
  fi
fi

# Prometheus is deliberately NOT backed up. Removed 2026-08-19.
#
# The TSDB writes its WAL continuously, so a hot tar reliably exits 1 with
# "file changed as we read it" (observed on /mnt/codex/prometheus/wal/00001845).
# A step that fails every night permanently blocks the offsite trigger, which
# only fires on ERRORS -eq 0 — reproducing the exact stall that left this
# homelab without an offsite copy for 40 days.
#
# The three ways out were: suppress the warning, stop the container during the
# tar, or drop the service. Suppressing keeps shipping 2.5 GB per night of data
# that XX_Backup-Strategie.md already classifies as expendable, and restic
# deduplicates TSDB blocks poorly because compaction rewrites them. Stopping
# tears a multi-minute hole in every time series, including the backup metrics
# used to monitor this script. Dropping it is the only option with no ongoing
# cost, and it matches what the documentation already recommended.
#
# Grafana stays in the backup — dashboards, users and annotations are not
# regenerable. Prometheus data refills within hours of a rebuild.


# ── STACK CONFIGS ──────────────────────────────────────────────────────────────

# JobIris — job application tracking. Lives under /mnt/vault because it holds
# personal data. Small, changes rarely, no database to dump.
step "JobIris"
CURRENT_SERVICE="jobiris"
if ! should_run "jobiris"; then skipped_service "JobIris"
else
  if assert_source "JobIris" "${SOURCE_PATHS[jobiris]}"; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/jobiris.tar.gz" \
      "${SOURCE_PATHS[jobiris]}/"
    if [ $? -ne 0 ]; then
      fail "JobIris failed"
    elif assert_archive "JobIris" "$BACKUP_DIR/$DATE/jobiris.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/jobiris.tar.gz"
      ok "JobIris saved"
    fi
  fi
fi

# Gitea Act Runner — registration state (.runner) and the SSH keys the runner
# uses. Losing this means re-registering the runner against Gitea by hand and
# regenerating keys; small file, cheap insurance.
step "Gitea Act Runner"
CURRENT_SERVICE="gitea-runner"
if ! should_run "gitea-runner"; then skipped_service "Gitea Act Runner"
else
  if assert_source "Gitea Act Runner" "${SOURCE_PATHS[gitea-runner]}"; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/gitea-runner.tar.gz" \
      "${SOURCE_PATHS[gitea-runner]}/"
    if [ $? -ne 0 ]; then
      fail "Gitea Act Runner failed"
    elif assert_archive "Gitea Act Runner" "$BACKUP_DIR/$DATE/gitea-runner.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/gitea-runner.tar.gz"
      ok "Gitea Act Runner saved"
    fi
  fi
fi

# Exporter token stores — tado, midea and netatmo each cache an OAuth token or
# session against a third-party API. A few KB in total, but losing them means
# re-authenticating against three vendor portals by hand. Grouped into one
# archive because they are tiny and always change together (or not at all).
step "Exporter tokens"
CURRENT_SERVICE="exporters"
if ! should_run "exporters"; then skipped_service "Exporter tokens"
else
  EXPORTER_OK=true
  for svc in tado midea netatmo; do
    assert_source "Exporter tokens ($svc)" "${SOURCE_PATHS[$svc]}" || EXPORTER_OK=false
  done

  if [ "$EXPORTER_OK" = true ]; then
    run_tar tar -czf "$BACKUP_DIR/$DATE/exporter-tokens.tar.gz" \
      "${SOURCE_PATHS[tado]}/" \
      "${SOURCE_PATHS[midea]}/" \
      "${SOURCE_PATHS[netatmo]}/"
    if [ $? -ne 0 ]; then
      fail "Exporter tokens failed"
    elif assert_archive "Exporter tokens" "$BACKUP_DIR/$DATE/exporter-tokens.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/exporter-tokens.tar.gz"
      ok "Exporter tokens saved"
    fi
  fi
fi

step "Stack configs"
CURRENT_SERVICE="stacks"
if ! should_run "stacks"; then skipped_service "Stack configs"
else
  # -h dereferences symlinks: ~/stacks points into ~/homelab-infra/mnemosyne/stacks/,
  # and GNU tar archives the link itself even with a trailing slash. Verified
  # 2026-08-19: without -h the archive was 146 bytes containing a single entry,
  # and had been that way undetected since the symlink was introduced. The data
  # was never actually lost — the target is version-controlled in Gitea, which
  # is backed up — but the documented restore path did not work.
  if assert_source "Stack configs" "/home/youruser/stacks"; then
    run_tar tar -czhf "$BACKUP_DIR/$DATE/stacks-config.tar.gz" \
      /home/youruser/stacks/
    if [ $? -ne 0 ]; then
      fail "Stack configs failed"
    elif assert_archive "Stack configs" "$BACKUP_DIR/$DATE/stacks-config.tar.gz"; then
      record_archive_size "$BACKUP_DIR/$DATE/stacks-config.tar.gz"
      ok "Stack configs saved"
    fi
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
  echo "# HELP backup_uncovered_mounts Persistent container bind mounts with no backup coverage"
  echo "# TYPE backup_uncovered_mounts gauge"
  echo "backup_uncovered_mounts $UNCOVERED_MOUNTS"
  echo "# HELP backup_max_skip_days Maximum age a skip-chain reference archive may reach"
  echo "# TYPE backup_max_skip_days gauge"
  echo "backup_max_skip_days $MAX_SKIP_DAYS"
  # Offsite metrics are no longer emitted here — the decoupled restic service
  # owns them (restic_offsite.prom). Keeping them here would produce stale,
  # misleading values since this script no longer performs the offsite transfer.
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

# ── Trigger offsite (restic, decoupled) ─────────────────────────────────────
# Fire-and-forget: restic runs as its own systemd service so its success or
# failure is tracked independently (restic_offsite.prom) and never affects this
# script's exit code. --no-block returns immediately; the daily fallback timer
# (restic-offsite.timer) covers the case where this trigger is ever missed.
# Only triggered on a clean local backup — no point shipping a broken snapshot.
# --dry-run must also suppress this: run()/run_tar() report success without
# writing anything, so ERRORS stays 0 in dry-run mode too — without this check,
# a "risk-free" dry-run would still kick off a real, multi-hour restic backup.
if [ "$DRY_RUN" = true ]; then
  log "  Offsite: skipped (--dry-run)"
elif [ "$NO_OFFSITE" != true ] && [ "$ERRORS" -eq 0 ]; then
  if systemctl start --no-block restic-offsite.service 2>/dev/null; then
    log "  Offsite: triggered restic-offsite.service"
  else
    log "  Offsite: WARN — could not trigger restic-offsite.service (check: systemctl status restic-offsite)"
  fi
elif [ "$NO_OFFSITE" = true ]; then
  log "  Offsite: skipped (--no-offsite)"
else
  log "  Offsite: NOT triggered — local backup had $ERRORS error(s), not shipping a broken snapshot"
fi

[ "$ERRORS" -eq 0 ] && exit 0 || exit 1