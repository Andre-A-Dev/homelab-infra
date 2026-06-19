#!/bin/bash
# hephaestus-display.sh
# Framebuffer status dashboard for Hephaestus (3.5" TFT on /dev/fb1 → tty1)
# Cycles through pages: Heating → Network → Services (10s each)
# Output is hardcoded to /dev/tty1 — safe to run via SSH

# --- Configuration ---
VCLIENT_HOST="127.0.0.1:3002"
PAGE_DURATION=10        # seconds per page
TAILSCALE_IP="100.x.x.x"
TTY="/dev/tty1"

# ANSI color codes
RESET="\e[0m"
BOLD="\e[1m"
RED="\e[31m"
GREEN="\e[32m"
YELLOW="\e[33m"
CYAN="\e[36m"
DIM="\e[2m"
BG_DARK="\e[40m"

# Display dimensions (127 cols × 90 rows)
COLS=150
ROWS=90

# --- Output helper: all output goes to tty1 ---
out() { printf "$@" > "$TTY"; }

# --- UI helpers ---
clear_tty()  { out "\e[2J\e[H"; }
hide_cursor(){ out "\e[?25l"; }
show_cursor(){ out "\e[?25h"; }

# Print centered text (plain, no escape codes in the string itself)
center() {
    local text="$1"
    local color="${2:-}"
    local len=${#text}
    local pad=$(( (COLS - len) / 2 ))
    out "%${pad}s${color}${text}${RESET}\n" ""
}

# Full-width divider
divider() {
    out "${DIM}"
    printf '─%.0s' $(seq 1 $COLS) > "$TTY"
    out "${RESET}\n"
}

# Padded label/value row
row() {
    local label="$1"
    local value="$2"
    local color="${3:-$WHITE}"
    out "  ${DIM}%-14s${RESET} ${color}${BOLD}%s${RESET}\n" "$label" "$value"
}

# Status dot
ok_dot()   { out "${GREEN}●${RESET}"; }
fail_dot() { out "${RED}✗${RESET}"; }

# --- Service checks ---
svc_status() {
    systemctl is-active --quiet "$1" 2>/dev/null && echo "${GREEN}● running${RESET}" || echo "${RED}✗ down${RESET}"
}

container_status() {
    docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -q true \
        && echo "${GREEN}● running${RESET}" || echo "${RED}✗ down${RESET}"
}

# --- Data collection ---
collect_heating() {
    TEMP_OUTDOOR=$(vclient -h "$VCLIENT_HOST" -c "getTempA"         2>/dev/null | awk '{print $1}')
    TEMP_BOILER=$(vclient  -h "$VCLIENT_HOST" -c "getTempKist"      2>/dev/null | awk '{print $1}')
    TEMP_BOILER_T=$(vclient -h "$VCLIENT_HOST" -c "getTempKsoll"    2>/dev/null | awk '{print $1}')
    TEMP_WW=$(vclient      -h "$VCLIENT_HOST" -c "getTempWWist"     2>/dev/null | awk '{print $1}')
    TEMP_WW_T=$(vclient    -h "$VCLIENT_HOST" -c "getTempWWsoll"    2>/dev/null | awk '{print $1}')
    BURNER=$(vclient       -h "$VCLIENT_HOST" -c "getBrennerStatus" 2>/dev/null | awk '{print $1}')
    PUMP_H=$(vclient       -h "$VCLIENT_HOST" -c "getPumpeStatusM1" 2>/dev/null | awk '{print $1}')
    PUMP_S=$(vclient       -h "$VCLIENT_HOST" -c "getPumpeStatusSp" 2>/dev/null | awk '{print $1}')
    FAULT=$(vclient        -h "$VCLIENT_HOST" -c "getStatusStoerung" 2>/dev/null | awk '{print $1}')
    MODE=$(vclient         -h "$VCLIENT_HOST" -c "getBetriebArtM1"  2>/dev/null | awk '{print $1}')
}

collect_network() {
    WLAN_IP=$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
    WLAN_SIG=$(iwconfig wlan0 2>/dev/null | awk -F'=' '/Signal level/{print $3}' | awk '{print $1}')
    TS_IP=$(tailscale ip 2>/dev/null | head -1)
    if [ "$TS_IP" = "$TAILSCALE_IP" ]; then
        TS_STATE="${GREEN}connected${RESET}  ${DIM}(${TAILSCALE_IP})${RESET}"
    else
        TS_STATE="${RED}disconnected${RESET}"
    fi
    ping -c1 -W1 192.168.1.x &>/dev/null \
        && GW_STATE="${GREEN}reachable${RESET}" \
        || GW_STATE="${RED}unreachable${RESET}"
}

# --- Pages ---

page_heating() {
    local burner_str pump_h pump_s fault_str mode_color

    [ "$BURNER" = "1" ]   && burner_str="${RED}${BOLD}ACTIVE${RESET}"  || burner_str="${DIM}off${RESET}"
    [ "$PUMP_H"  = "1" ]  && pump_h="${GREEN}on${RESET}"               || pump_h="${DIM}off${RESET}"
    [ "$PUMP_S"  = "1" ]  && pump_s="${GREEN}on${RESET}"               || pump_s="${DIM}off${RESET}"
    [ "$FAULT"   = "1" ]  && fault_str=" ${RED}${BOLD}[FAULT]${RESET}" || fault_str=""

    # Color mode by operating state
    case "$MODE" in
        H+WW|NORM) mode_color="$GREEN" ;;
        WW)        mode_color="$CYAN"  ;;
        RED)       mode_color="$YELLOW";;
        ABSCHALT)  mode_color="$DIM"   ;;
        *)         mode_color="$WHITE" ;;
    esac

    clear_tty
    out "\n"
    center "HEPHAESTUS — HEATING" "$BOLD$YELLOW"
    out "\n"
    row "Mode"          "${mode_color}${MODE:-n/a}${RESET}${fault_str}"
    out "\n"
    row "Burner"        "$burner_str"
    out "\n"
    row "Outdoor"       "${TEMP_OUTDOOR:-n/a} °C"       "$CYAN"
    row "Boiler actual" "${TEMP_BOILER:-n/a} °C"        "$WHITE"
    row "Boiler target" "${TEMP_BOILER_T:-n/a} °C"      "$DIM"
    row "HotWater act." "${TEMP_WW:-n/a} °C"            "$WHITE"
    row "HotWater tgt." "${TEMP_WW_T:-n/a} °C"         "$DIM"
    out "\n"
    out "  ${DIM}Pump heating :${RESET}  ${pump_h}     ${DIM}Pump storage :${RESET}  ${pump_s}\n"
    out "\n"
    render_footer 1 3
}

page_network() {
    clear_tty
    out "\n"
    center "HEPHAESTUS — NETWORK" "$BOLD$YELLOW"
    out "\n"
    row "WLAN IP"    "${WLAN_IP:-none}"        "$GREEN"
    row "Signal"     "${WLAN_SIG:-n/a} dBm"   "$WHITE"
    row "Gateway"    "$GW_STATE"
    out "\n"
    row "Tailscale"  "$TS_STATE"
    out "\n"
    render_footer 2 3
}

page_services() {
    clear_tty
    out "\n"
    center "HEPHAESTUS — SERVICES" "$BOLD$YELLOW"
    out "\n"
    out "  ${DIM}vcontrold     ${RESET}  $(svc_status vcontrold)\n"
    out "\n"
    out "  ${DIM}viessmann-api ${RESET}  $(svc_status viessmann-api)\n"
    out "\n"
    out "  ${DIM}node-exporter ${RESET}  $(container_status node-exporter)\n"
    out "\n"
    local uptime_str load cpu_temp
    uptime_str=$(uptime -p 2>/dev/null | sed 's/up //')
    load=$(cut -d' ' -f1 /proc/loadavg)
    cpu_temp=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)
    row "Uptime"    "$uptime_str"   "$DIM"
    row "Load"      "$load"         "$DIM"
    row "CPU temp"  "${cpu_temp:-n/a}" "$DIM"
    out "\n"
    render_footer 3 3
}

render_footer() {
    local page="$1" total="$2"
    local timestamp
    timestamp=$(date '+%d.%m.%Y  %H:%M:%S')
    out "  ${DIM}Page ${page}/${total}   ${timestamp}   next in ${PAGE_DURATION}s${RESET}\n"
}

# --- Main loop ---
main() {
    hide_cursor
    trap 'show_cursor; exit 0' INT TERM

    # Map tty1 to framebuffer fb1
    con2fbmap 1 1 2>/dev/null

    local page=1
    while true; do
        case $page in
            1)
                collect_heating
                page_heating
                ;;
            2)
                collect_network
                page_network
                ;;
            3)
                page_services
                ;;
        esac

        sleep "$PAGE_DURATION"
        page=$(( page % 3 + 1 ))
    done
}

main
