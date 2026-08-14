#!/usr/bin/env bash
#
# network-quality.sh — validate a link's throughput, latency and stability.
#
# Runs FROM the host under test TOWARD an iperf3 server. Designed for the
# cellar move: run it once against Mnemosyne from Daidalos (validates the
# switch), and once from an upstairs wired host (validates the WLAN-mesh hop).
# Same script, different PATH_LABEL — the metrics carry the label so both
# paths land in Prometheus side by side.
#
# Requires on this host : iperf3, python3, ethtool, iputils-ping
# Requires on the target: iperf3 -s   (systemd unit or `iperf3 -s -D`)
#
# Config: /etc/network-quality.conf  (chmod 600, holds the ntfy topic)
#
set -Eeuo pipefail

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
CONF="/etc/network-quality.conf"
if [[ -r "$CONF" ]]; then
  # shellcheck disable=SC1090
  source "$CONF"
else
  echo "FATAL: $CONF not found. Copy network-quality.conf there (chmod 600)." >&2
  exit 1
fi

: "${IPERF_TARGET:?set IPERF_TARGET in $CONF}"
: "${PATH_LABEL:=uplink}"
: "${IFACE:=auto}"
: "${MIN_THROUGHPUT_MBPS:=100}"
: "${MAX_RETR:=50}"
: "${MAX_LOSS_PCT:=0.1}"
: "${MAX_RTT_AVG_MS:=25}"
: "${MAX_MDEV_MS:=10}"
: "${PING_COUNT:=3000}"
: "${PING_INTERVAL:=0.2}"
: "${IPERF_DURATION:=30}"
: "${TEXTFILE_DIR:=/var/lib/node_exporter/textfile_collector}"
: "${NTFY_URL:=}"

# ----------------------------------------------------------------------------
# Output helpers — colored, ANSI-safe, spinner per step
# ----------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_OK=$'\033[0;32m'; C_FAIL=$'\033[0;31m'
  C_WARN=$'\033[0;33m'; C_INFO=$'\033[0;36m'; C_DIM=$'\033[2m'
else
  C_RESET=""; C_OK=""; C_FAIL=""; C_WARN=""; C_INFO=""; C_DIM=""
fi

SPIN='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
spinner() {
  # spinner "message" -- runs the following command in $@ after --
  local msg="$1"; shift
  [[ "$1" == "--" ]] && shift
  local logf; logf="$(mktemp)"
  ( "$@" >"$logf" 2>&1 ) &
  local pid=$! i=0
  if [[ -t 1 ]]; then
    while kill -0 "$pid" 2>/dev/null; do
      printf "\r ${C_INFO}%s${C_RESET} %s" "${SPIN:i++%${#SPIN}:1}" "$msg"
      sleep 0.1
    done
  else
    printf " .. %s\n" "$msg"
    wait "$pid" || true
  fi
  wait "$pid"; local rc=$?
  SPIN_LOG="$(cat "$logf")"; rm -f "$logf"
  return $rc
}
ok()   { printf "\r ${C_OK}✔${C_RESET} %s%*s\n"   "$1" 10 ""; }
fail() { printf "\r ${C_FAIL}�’${C_RESET} %s%*s\n" "$1" 10 ""; }
note() { printf "   ${C_DIM}%s${C_RESET}\n" "$1"; }
hr()   { printf "${C_DIM}%s${C_RESET}\n" "────────────────────────────────────────────────────────"; }

# ----------------------------------------------------------------------------
# State — metrics are accumulated, then written atomically at the end
# ----------------------------------------------------------------------------
declare -A M          # metric_name -> value
FAILURES=()           # human-readable failure reasons for ntfy
OVERALL=1             # 1 = pass, 0 = fail

record() { M["$1"]="$2"; }
check() {
  # check "name" "value" "op" "threshold" "human label"
  local name="$1" val="$2" op="$3" thr="$4" label="$5"
  local pass
  pass=$(python3 -c "print(1 if ($val $op $thr) else 0)" 2>/dev/null || echo 0)
  if [[ "$pass" == "1" ]]; then
    ok "$label: ${C_OK}$val${C_RESET} (${op} $thr)"
  else
    fail "$label: ${C_FAIL}$val${C_RESET} (want ${op} $thr)"
    FAILURES+=("$label = $val (want $op $thr)")
    OVERALL=0
  fi
}

# ----------------------------------------------------------------------------
# 0. Interface autodetect (Pi 5 on Trixie is eth0 OR end0)
# ----------------------------------------------------------------------------
detect_iface() {
  if [[ "$IFACE" != "auto" ]]; then echo "$IFACE"; return; fi
  ip -o -4 route show to default 2>/dev/null | awk '{print $5; exit}'
}

# ----------------------------------------------------------------------------
# 1. Physical layer — link speed, duplex, NIC error counters
# ----------------------------------------------------------------------------
step_phy() {
  local iface; iface="$(detect_iface)"
  [[ -z "$iface" ]] && { fail "no default-route interface found"; OVERALL=0; return; }
  note "interface under test: $iface"

  # Link speed (host <-> switch; NOT the mesh hop — see header)
  local speed duplex
  speed="$(ethtool "$iface" 2>/dev/null  | awk -F': ' '/Speed/  {gsub(/Mb\/s/,"",$2); print $2+0; exit}')"
  duplex="$(ethtool "$iface" 2>/dev/null | awk -F': ' '/Duplex/ {print $2; exit}')"
  record "network_quality_link_speed_mbps" "${speed:-0}"
  if [[ "${speed:-0}" -ge 1000 && "$duplex" == "Full" ]]; then
    ok "link (host↔switch): ${speed}Mb/s ${duplex}"
  else
    fail "link (host↔switch): ${speed:-?}Mb/s ${duplex:-?} — expected 1000 Full"
    FAILURES+=("link speed ${speed:-?}Mb/s ${duplex:-?}")
    OVERALL=0
  fi

  # NIC error counters straight from sysfs — driver-agnostic
  local rxerr txerr rxdrop
  rxerr="$(cat "/sys/class/net/$iface/statistics/rx_errors"  2>/dev/null || echo 0)"
  txerr="$(cat "/sys/class/net/$iface/statistics/tx_errors"  2>/dev/null || echo 0)"
  rxdrop="$(cat "/sys/class/net/$iface/statistics/rx_dropped" 2>/dev/null || echo 0)"
  record "network_quality_nic_rx_errors" "$rxerr"
  record "network_quality_nic_tx_errors" "$txerr"
  if [[ "$rxerr" -eq 0 && "$txerr" -eq 0 ]]; then
    ok "NIC error counters: rx=$rxerr tx=$txerr drop=$rxdrop"
  else
    fail "NIC errors present: rx=$rxerr tx=$txerr — cabling/EMI, not congestion"
    FAILURES+=("NIC errors rx=$rxerr tx=$txerr")
    OVERALL=0
  fi
}

# ----------------------------------------------------------------------------
# 2. Throughput — down and up, with retransmit count
# ----------------------------------------------------------------------------
iperf_json() {
  # $1 = extra args (e.g. -R). Emits: "<mbps> <retransmits>"
  local extra="${1:-}"
  local json
  if ! json="$(iperf3 -c "$IPERF_TARGET" -t "$IPERF_DURATION" -P 4 $extra -J 2>/dev/null)"; then
    echo "0 999999"; return
  fi
  python3 - "$json" <<'PY'
import sys, json
try:
    d = json.loads(sys.argv[1])
    mbps = d["end"]["sum_received"]["bits_per_second"] / 1e6
    retr = d["end"]["sum_sent"].get("retransmits", 0)
    print(f"{mbps:.1f} {retr}")
except Exception:
    print("0 999999")
PY
}

step_throughput() {
  local down up dmbps dretr umbps uretr

  if spinner "iperf3 download (${IPERF_DURATION}s, 4 streams) …" -- true; then :; fi
  down="$(iperf_json "")";      dmbps="${down%% *}";  dretr="${down##* }"
  up="$(iperf_json "-R")";      umbps="${up%% *}";    uretr="${up##* }"

  record "network_quality_throughput_mbps{direction=\"down\"}" "$dmbps"
  record "network_quality_throughput_mbps{direction=\"up\"}"   "$umbps"
  record "network_quality_retransmits{direction=\"down\"}"     "$dretr"
  record "network_quality_retransmits{direction=\"up\"}"       "$uretr"

  check "throughput_down" "$dmbps" ">=" "$MIN_THROUGHPUT_MBPS" "throughput ↓"
  check "throughput_up"   "$umbps" ">=" "$MIN_THROUGHPUT_MBPS" "throughput ↑"
  check "retransmits_down" "$dretr" "<=" "$MAX_RETR" "retransmits ↓"
  check "retransmits_up"   "$uretr" "<=" "$MAX_RETR" "retransmits ↑"
}

# ----------------------------------------------------------------------------
# 3. Stability — loss, latency, jitter over a sustained ping run
# ----------------------------------------------------------------------------
step_stability() {
  local dur; dur="$(python3 -c "print(int($PING_COUNT*$PING_INTERVAL))")"
  note "ping run: $PING_COUNT packets @ ${PING_INTERVAL}s ≈ ${dur}s — run this during real cellar load"

  local out
  spinner "measuring loss / latency / jitter (~${dur}s) …" -- \
    ping -i "$PING_INTERVAL" -c "$PING_COUNT" "$IPERF_TARGET" || true
  out="$SPIN_LOG"

  local loss avg mdev maxr
  loss="$(echo "$out" | grep -oE '[0-9.]+% packet loss' | grep -oE '[0-9.]+' | head -1)"
  # rtt min/avg/max/mdev = a/b/c/d ms
  avg="$(echo  "$out" | awk -F'[/ ]' '/rtt|round-trip/ {print $(NF-4)}')"
  maxr="$(echo "$out" | awk -F'[/ ]' '/rtt|round-trip/ {print $(NF-3)}')"
  mdev="$(echo "$out" | awk -F'[/ ]' '/rtt|round-trip/ {print $(NF-2)}')"
  loss="${loss:-100}"; avg="${avg:-9999}"; maxr="${maxr:-9999}"; mdev="${mdev:-9999}"

  record "network_quality_packet_loss_percent" "$loss"
  record "network_quality_rtt_avg_ms"          "$avg"
  record "network_quality_rtt_max_ms"          "$maxr"
  record "network_quality_rtt_mdev_ms"         "$mdev"

  check "packet_loss" "$loss" "<=" "$MAX_LOSS_PCT"  "packet loss %"
  check "rtt_avg"     "$avg"  "<=" "$MAX_RTT_AVG_MS" "RTT avg (ms)"
  check "jitter"      "$mdev" "<=" "$MAX_MDEV_MS"    "jitter mdev (ms)"
  note "RTT max (p-high proxy): ${maxr} ms"
}

# ----------------------------------------------------------------------------
# 4. Prometheus textfile output — atomic write
# ----------------------------------------------------------------------------
write_metrics() {
  mkdir -p "$TEXTFILE_DIR"
  local tmp="$TEXTFILE_DIR/network_quality.prom.$$"
  {
    echo "# HELP network_quality_up 1 if all checks passed for this path"
    echo "# TYPE network_quality_up gauge"
    echo "network_quality_up{path=\"$PATH_LABEL\",target=\"$IPERF_TARGET\"} $OVERALL"
    echo "# HELP network_quality_last_run_timestamp_seconds Unixtime of last run"
    echo "# TYPE network_quality_last_run_timestamp_seconds gauge"
    echo "network_quality_last_run_timestamp_seconds{path=\"$PATH_LABEL\"} $(date +%s)"
    local k v base labels
    for k in "${!M[@]}"; do
      v="${M[$k]}"
      if [[ "$k" == *"{"* ]]; then
        base="${k%%\{*}"; labels="${k#*\{}"; labels="${labels%\}}"
        echo "# TYPE $base gauge"
        echo "${base}{path=\"$PATH_LABEL\",target=\"$IPERF_TARGET\",${labels}} $v"
      else
        echo "# TYPE $k gauge"
        echo "${k}{path=\"$PATH_LABEL\",target=\"$IPERF_TARGET\"} $v"
      fi
    done
  } > "$tmp"
  chmod 644 "$tmp"
  mv "$tmp" "$TEXTFILE_DIR/network_quality.prom"
}

# ----------------------------------------------------------------------------
# 5. ntfy on failure only — no noise on success
# ----------------------------------------------------------------------------
notify() {
  [[ "$OVERALL" -eq 1 || -z "$NTFY_URL" ]] && return 0
  local body; body="$(printf '%s\n' "${FAILURES[@]}")"
  curl -fsS \
    -H "Title: Network quality FAIL — path=$PATH_LABEL" \
    -H "Priority: high" \
    -H "Tags: warning,satellite" \
    -d "$body" \
    "$NTFY_URL" >/dev/null 2>&1 || note "ntfy delivery failed"
}

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
main() {
  hr
  printf " ${C_INFO}network-quality${C_RESET}  path=%s  target=%s\n" "$PATH_LABEL" "$IPERF_TARGET"
  hr
  step_phy
  step_throughput
  step_stability
  write_metrics
  notify
  hr
  if [[ "$OVERALL" -eq 1 ]]; then
    ok "ALL CHECKS PASSED — path '$PATH_LABEL' is valid"
    exit 0
  else
    fail "CHECKS FAILED (${#FAILURES[@]}) — path '$PATH_LABEL'"
    exit 1
  fi
}
main "$@"
