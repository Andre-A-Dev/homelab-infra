#!/usr/bin/env bash
# =============================================================================
# system-update.sh — Manual System Update & Cleanup
# =============================================================================
# Detects the package manager (apt / pacman+paru) and runs a full update cycle:
#   apt:    update → dist-upgrade → autoremove → autoclean
#   pacman: paru -Syu (AUR + official) → orphan removal → pkg cache trim
#
# Additionally:
#   - Per-step timing
#   - Systemd failed unit check (post-update)
#   - Log file at /var/log/system-update.log
#   - Prometheus textfile metric → /var/lib/node_exporter/textfile_collector/
#   - ntfy push notification on completion
#
# Usage:
#   system-update              # full run
#   system-update --dry-run    # show what would happen, no changes
#
# Configuration:
#   /etc/system-update.conf   host-specific config (not in Git)
#
#   Supported keys:
#     NTFY_TOPIC=<topic>        ntfy topic name (notifications disabled if unset)
#     NTFY_SERVER=<url>         ntfy server (default: https://ntfy.sh)
# =============================================================================

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────

# Load host-specific config if present.
# /etc/system-update.conf is not tracked in Git — put secrets and
# host-specific values there. Same pattern as /etc/backup-secrets.conf.
CONFIG_FILE="/etc/system-update.conf"
if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck source=/dev/null
    source "$CONFIG_FILE"
fi

# Defaults — can be overridden in /etc/system-update.conf
NTFY_TOPIC="${NTFY_TOPIC:-}"
NTFY_SERVER="${NTFY_SERVER:-https://ntfy.sh}"

LOG_FILE="/var/log/system-update.log"
TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
TEXTFILE="${TEXTFILE_DIR}/system_update.prom"

# ── Colors & Formatting ───────────────────────────────────────────────────────

BOLD="\e[1m"
RESET="\e[0m"
RED="\e[31m"
GREEN="\e[32m"
YELLOW="\e[33m"
CYAN="\e[36m"
WHITE="\e[97m"
DIM="\e[2m"

# ── Flags & State ─────────────────────────────────────────────────────────────

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# Global counters — populated during the run, used in summary + ntfy
PKGS_UPGRADED=0
FAILED_UNITS=()
EXIT_CODE=0
REBOOT_REQUIRED=false

# Step timing — associative array: step_label → elapsed seconds
declare -A STEP_TIMES
CURRENT_STEP_LABEL=""
CURRENT_STEP_START=0

# ── Logging ───────────────────────────────────────────────────────────────────

# Write to both stdout and log file. Strip ANSI codes for the log file.
_log_raw() {
    local line="$1"
    # Print to terminal as-is (with colors)
    echo -e "$line"
    # Strip ANSI escape codes before writing to log file
    echo -e "$line" | sed 's/\x1b\[[0-9;]*m//g' >> "$LOG_FILE" 2>/dev/null || true
}

log() { _log_raw "$1"; }

# ── Print Helpers ─────────────────────────────────────────────────────────────

print_header() {
    # Fixed-width box: 48 display columns total.
    # Content area between ║ chars: 46 cols (2 leading spaces + 44 for title+pad).
    # printf "%-Ns" pads by bytes, not display chars — multi-byte UTF-8 chars
    # (em dash = 3 bytes / 1 col) cause short padding. We compensate by counting
    # the byte-vs-char difference and adding it to the field width.
    local title="$1"
    local target=44  # display columns for title + trailing spaces
    local byte_len char_len extra pad_width padded
    byte_len=$(printf '%s' "$title" | wc -c)
    # wc -m is locale-dependent and unreliable for multi-byte chars on some systems.
    # Python3 len() always counts Unicode code points (= display chars) correctly.
    if command -v python3 &>/dev/null; then
        char_len=$(python3 -c "import sys; print(len(sys.argv[1]))" "$title")
    else
        char_len=$(printf '%s' "$title" | wc -m)
    fi
    extra=$(( byte_len - char_len ))          # extra bytes from multi-byte chars
    pad_width=$(( target + extra ))           # inflate field width to compensate
    padded=$(printf "%-${pad_width}s" "$title")
    log ""
    log "${BOLD}${CYAN}╔══════════════════════════════════════════════╗${RESET}"
    log "${BOLD}${CYAN}║${RESET}  ${BOLD}${WHITE}${padded}${RESET}${BOLD}${CYAN}║${RESET}"
    log "${BOLD}${CYAN}╚══════════════════════════════════════════════╝${RESET}"
    log ""
}

print_step() {
    # Save label and start time for per-step duration tracking
    CURRENT_STEP_LABEL="$1"
    CURRENT_STEP_START=$(date +%s)
    log "${BOLD}${CYAN}▶  $1${RESET}"
}

# Call after each step to record and display elapsed time
print_step_done() {
    local label="${1:-$CURRENT_STEP_LABEL}"
    local elapsed=$(( $(date +%s) - CURRENT_STEP_START ))
    STEP_TIMES["$label"]=$elapsed
    log "${DIM}   ↳ done in ${elapsed}s${RESET}"
}

print_ok()        { log "${GREEN}✔  $1${RESET}"; }
print_warn()      { log "${YELLOW}⚠  $1${RESET}"; }
print_error()     { log "${RED}✖  $1${RESET}"; }
print_info()      { log "${DIM}   $1${RESET}"; }
print_separator() { log "${DIM}────────────────────────────────────────────────${RESET}"; }

# ── Command Runner ────────────────────────────────────────────────────────────

# run <label> <cmd> [args...]
# Executes the command, logging stdout/stderr to both terminal and log file.
# In dry-run mode, only prints what would be run.
run() {
    local label="$1"
    shift
    if $DRY_RUN; then
        log "${YELLOW}[dry-run]${RESET} ${DIM}$*${RESET}"
        return 0
    fi
    print_info "$ $*"
    echo | tee -a "$LOG_FILE" > /dev/null
    # Run the command; tee output to log file (strip ANSI for log)
    if ! "$@" 2>&1 | tee >(sed 's/\x1b\[[0-9;]*m//g' >> "$LOG_FILE"); then
        EXIT_CODE=1
        print_error "Command failed: $*"
        return 1
    fi
}

# ── Detection ─────────────────────────────────────────────────────────────────

detect_package_manager() {
    if command -v apt &>/dev/null; then
        echo "apt"
    elif command -v pacman &>/dev/null; then
        echo "pacman"
    else
        echo "unknown"
    fi
}

detect_reboot_required() {
    # apt-based: kernel update sets this flag file
    if [[ -f /var/run/reboot-required ]]; then
        REBOOT_REQUIRED=true
        return 0
    fi
    # Arch: running kernel version vs installed package version
    if command -v pacman &>/dev/null; then
        local running installed
        running=$(uname -r)
        installed=$(pacman -Q linux-cachyos linux-cachyos-bore linux linux-lts 2>/dev/null \
            | awk '{print $2}' | head -1 || true)
        if [[ -n "$installed" && "$running" != *"$installed"* ]]; then
            REBOOT_REQUIRED=true
            return 0
        fi
    fi
    return 1
}

# ── Systemd Failed Units ──────────────────────────────────────────────────────

check_failed_units() {
    print_separator
    print_step "Checking for failed systemd units"

    local raw
    # --no-legend removes the header/footer; --quiet suppresses "0 loaded units"
    raw=$(systemctl list-units --state=failed --no-legend 2>/dev/null || true)

    if [[ -z "$raw" ]]; then
        print_ok "No failed systemd units"
    else
        print_warn "Failed units detected:"
        # Extract unit name: output format is "  ● unit.service  loaded failed failed  Desc"
        # grep -oP matches the name directly after the bullet — immune to column shifts
        while IFS= read -r unit; do
            [[ -z "$unit" ]] && continue
            FAILED_UNITS+=("$unit")
            log "  ${RED}${unit}${RESET}"
        done < <(echo "$raw" | grep -oP '^\s*â\s+\K\S+' ||                   echo "$raw" | grep -oP '^\s*●\s+\K\S+' || true)
        log ""
        log "  ${DIM}Run: systemctl status <unit> for details${RESET}"
    fi

    print_step_done "systemd failed units"
}

# ── APT Update Cycle ──────────────────────────────────────────────────────────

run_apt_updates() {
    print_step "Refreshing package index"
    run "apt update" sudo apt update
    print_step_done "apt update"
    log ""

    print_separator

    # Capture upgrade count before running dist-upgrade
    print_step "Packages available for upgrade"
    local upgradable
    upgradable=$(apt list --upgradable 2>/dev/null | grep -v "^Listing" || true)
    PKGS_UPGRADED=$(echo "$upgradable" | grep -c '/' || true)

    if [[ -z "$upgradable" || "$PKGS_UPGRADED" -eq 0 ]]; then
        print_ok "System is already up to date"
        PKGS_UPGRADED=0
    else
        log "${WHITE}${upgradable}${RESET}"
        log ""
    fi
    print_step_done "package list"
    log ""

    print_separator

    # dist-upgrade resolves new/removed dependencies; plain upgrade silently skips those
    print_step "Installing upgrades (dist-upgrade)"
    run "dist-upgrade" sudo apt dist-upgrade -y
    print_step_done "dist-upgrade"
    log ""

    print_separator

    print_step "Removing orphaned packages (autoremove)"
    run "autoremove" sudo apt autoremove -y
    print_step_done "autoremove"
    log ""

    print_separator

    print_step "Cleaning package cache (autoclean)"
    run "autoclean" sudo apt autoclean -y
    print_step_done "autoclean"
    log ""
}

# ── PACMAN / PARU Update Cycle ────────────────────────────────────────────────

run_pacman_updates() {
    # paru must NOT run as root — it builds AUR packages as the calling user
    if command -v paru &>/dev/null && [[ "$EUID" -eq 0 ]]; then
        print_warn "paru cannot run as root. Re-run without sudo on AstraeusNX."
        EXIT_CODE=1
        return 1
    fi

    if command -v paru &>/dev/null; then
        print_step "Updating system + AUR packages (paru -Syu)"
        run "paru -Syu" paru -Syu --noconfirm
        # paru doesn't print a clean count — approximate from pacman log
        PKGS_UPGRADED=$(grep -c "upgraded" /var/log/pacman.log 2>/dev/null | tail -1 || echo 0)
    else
        print_step "Updating system packages (pacman -Syu)"
        run "pacman -Syu" sudo pacman -Syu --noconfirm
        PKGS_UPGRADED=$(grep -c "upgraded" /var/log/pacman.log 2>/dev/null | tail -1 || echo 0)
    fi
    print_step_done "pacman/paru upgrade"
    log ""

    print_separator

    # Orphans: installed packages no longer required by anything
    print_step "Checking for orphaned packages"
    local orphans
    orphans=$(pacman -Qdtq 2>/dev/null || true)
    if [[ -z "$orphans" ]]; then
        print_ok "No orphaned packages found"
    else
        log "${YELLOW}Orphaned packages:${RESET}"
        log "$orphans"
        log ""
        # shellcheck disable=SC2086  # intentional word splitting for package list
        run "remove orphans" sudo pacman -Rns --noconfirm $orphans
    fi
    print_step_done "orphan check"
    log ""

    print_separator

    # Keep 2 most recent cached versions per package (requires pacman-contrib)
    print_step "Trimming package cache (paccache -rk2)"
    if command -v paccache &>/dev/null; then
        run "paccache" sudo paccache -rk2
    else
        print_info "paccache not found — install pacman-contrib for cache trimming"
        print_info "sudo pacman -S pacman-contrib"
    fi
    print_step_done "paccache"
    log ""
}

# ── Prometheus Textfile Metric ────────────────────────────────────────────────

write_prometheus_metrics() {
    [[ ! -d "$TEXTFILE_DIR" ]] && return 0  # skip silently if node_exporter not present

    local ts
    ts=$(date +%s)
    local reboot_val=0
    $REBOOT_REQUIRED && reboot_val=1
    local failed_count=${#FAILED_UNITS[@]}

    # Write atomically via temp file to avoid partial reads by node_exporter
    local tmp
    tmp=$(mktemp)

    cat > "$tmp" << EOF
# HELP system_update_last_run_timestamp_seconds Unix timestamp of last system-update run
# TYPE system_update_last_run_timestamp_seconds gauge
system_update_last_run_timestamp_seconds{host="$(hostname)"} ${ts}

# HELP system_update_packages_upgraded_total Number of packages upgraded in last run
# TYPE system_update_packages_upgraded_total gauge
system_update_packages_upgraded_total{host="$(hostname)"} ${PKGS_UPGRADED}

# HELP system_update_exit_code Exit code of last system-update run (0=success)
# TYPE system_update_exit_code gauge
system_update_exit_code{host="$(hostname)"} ${EXIT_CODE}

# HELP system_update_reboot_required Whether a reboot is required after the last update (1=yes)
# TYPE system_update_reboot_required gauge
system_update_reboot_required{host="$(hostname)"} ${reboot_val}

# HELP system_update_failed_units_total Number of failed systemd units detected post-update
# TYPE system_update_failed_units_total gauge
system_update_failed_units_total{host="$(hostname)"} ${failed_count}
EOF

    if $DRY_RUN; then
        print_info "[dry-run] Would write Prometheus metrics to ${TEXTFILE}"
        rm -f "$tmp"
        return 0
    fi

    sudo mv "$tmp" "$TEXTFILE"
    sudo chmod 644 "$TEXTFILE"
    print_ok "Prometheus metrics written → ${TEXTFILE}"
}

# ── ntfy Notification ─────────────────────────────────────────────────────────

send_ntfy() {
    [[ -z "$NTFY_TOPIC" ]] && return 0  # disabled if no topic configured

    local host
    host=$(hostname)
    local status_icon priority tags

    if [[ $EXIT_CODE -eq 0 && ${#FAILED_UNITS[@]} -eq 0 ]]; then
        status_icon="✅"
        priority="default"
        tags="white_check_mark"
    else
        status_icon="⚠️"
        priority="high"
        tags="warning"
    fi

    # Build message body
    local body="${status_icon} ${host} — Update complete"$'\n'
    body+="Packages: ${PKGS_UPGRADED}"$'\n'

    if $REBOOT_REQUIRED; then
        body+="Reboot required: yes"$'\n'
    fi

    if [[ ${#FAILED_UNITS[@]} -gt 0 ]]; then
        body+="Failed units: ${FAILED_UNITS[*]}"$'\n'
    fi

    if $DRY_RUN; then
        print_info "[dry-run] Would send ntfy to ${NTFY_SERVER}/${NTFY_TOPIC}"
        return 0
    fi

    curl -s \
        -H "Title: system-update · ${host}" \
        -H "Priority: ${priority}" \
        -H "Tags: ${tags}" \
        -d "$body" \
        "${NTFY_SERVER}/${NTFY_TOPIC}" > /dev/null 2>&1 \
        && print_ok "ntfy notification sent → ${NTFY_SERVER}/${NTFY_TOPIC}" \
        || print_warn "ntfy notification failed (non-fatal)"
}

# ── Step Timing Summary ───────────────────────────────────────────────────────

print_timing_table() {
    if [[ ${#STEP_TIMES[@]} -eq 0 ]]; then return; fi

    log ""
    log "${BOLD}${WHITE}Step timings:${RESET}"
    for label in "${!STEP_TIMES[@]}"; do
        printf "${DIM}  %-36s ${WHITE}%3ds${RESET}\n" "${label}" "${STEP_TIMES[$label]}" \
            | tee -a "$LOG_FILE" > /dev/null
        echo -e "${DIM}  $(printf '%-36s' "${label}") ${WHITE}${STEP_TIMES[$label]}s${RESET}"
    done
}

# ── Log Init ──────────────────────────────────────────────────────────────────

init_log() {
    # Ensure log file exists and is writable by the calling user.
    # Use "sudo install" so root creates the file but ownership goes to the
    # current user — no sudo needed for logging on subsequent runs.
    if [[ ! -f "$LOG_FILE" ]]; then
        sudo install -m 644 -o "$(id -un)" /dev/null "$LOG_FILE" 2>/dev/null || {
            print_warn "Cannot create ${LOG_FILE} — logging disabled"
            LOG_FILE="/dev/null"
        }
    elif [[ ! -w "$LOG_FILE" ]]; then
        sudo chown "$(id -un)" "$LOG_FILE" 2>/dev/null || {
            print_warn "Cannot write to ${LOG_FILE} — logging disabled"
            LOG_FILE="/dev/null"
        }
    fi
    {
        echo ""
        echo "════════════════════════════════════════════════"
        echo "system-update  $(date '+%Y-%m-%d %H:%M:%S')  host=$(hostname)"
        echo "════════════════════════════════════════════════"
    } >> "$LOG_FILE"
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    local host pkg_manager start_time
    host=$(hostname)
    pkg_manager=$(detect_package_manager)
    start_time=$(date +%s)

    init_log

    print_header "System Update — ${host}"

    log "  ${DIM}Host:     ${WHITE}${host}${RESET}"
    log "  ${DIM}PM:       ${WHITE}${pkg_manager}${RESET}"
    log "  ${DIM}Date:     ${WHITE}$(date '+%Y-%m-%d %H:%M:%S')${RESET}"
    log "  ${DIM}Log:      ${WHITE}${LOG_FILE}${RESET}"
    [[ -n "$NTFY_TOPIC" ]] && log "  ${DIM}ntfy:     ${WHITE}${NTFY_SERVER}/${NTFY_TOPIC}${RESET}"
    $DRY_RUN && log "  ${YELLOW}Mode:     DRY-RUN — no changes will be made${RESET}"
    log ""

    # ── Package Updates ───────────────────────────────────────────────────────

    case "$pkg_manager" in
        apt)     run_apt_updates ;;
        pacman)  run_pacman_updates ;;
        *)
            print_error "No supported package manager found (apt / pacman)."
            EXIT_CODE=1
            ;;
    esac

    # ── Post-Update Checks ────────────────────────────────────────────────────

    check_failed_units
    detect_reboot_required || true

    # ── Summary ───────────────────────────────────────────────────────────────

    local end_time total_elapsed
    end_time=$(date +%s)
    total_elapsed=$(( end_time - start_time ))

    print_separator
    log ""

    print_timing_table

    log ""

    if [[ $EXIT_CODE -eq 0 ]]; then
        log "${BOLD}${GREEN}✔  Update complete${RESET}  ${DIM}(total: ${total_elapsed}s)${RESET}"
    else
        log "${BOLD}${RED}✖  Update finished with errors${RESET}  ${DIM}(total: ${total_elapsed}s)${RESET}"
    fi

    log ""

    if $REBOOT_REQUIRED; then
        log "${BOLD}${YELLOW}⚠  Reboot required${RESET} — a kernel or core library was updated."
        log "   ${DIM}Run: ${WHITE}sudo reboot${RESET}"
    else
        print_ok "No reboot required"
    fi

    if [[ ${#FAILED_UNITS[@]} -gt 0 ]]; then
        log ""
        print_warn "Failed systemd units (check manually):"
        for unit in "${FAILED_UNITS[@]}"; do
            log "  ${RED}${unit}${RESET}  ${DIM}→ systemctl status ${unit}${RESET}"
        done
    fi

    log ""

    # ── Prometheus + ntfy ─────────────────────────────────────────────────────

    write_prometheus_metrics
    send_ntfy

    log ""

    return $EXIT_CODE
}

main "$@"