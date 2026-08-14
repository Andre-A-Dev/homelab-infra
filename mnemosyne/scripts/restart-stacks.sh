#!/bin/bash
#
# restart-stacks.sh - Cleanly (re)start all Docker Compose stacks on Mnemosyne
#
# Why this exists: a bare `for stack in ~/stacks/*/; do docker compose up -d; done`
# has no dependency ordering and swallows per-stack errors in a wall of build/pull
# output. This script enforces ordering, verifies prerequisites, and prints a
# clear pass/fail summary instead of making you scroll back through history.
#
# Usage:
#   restart-stacks.sh                  # normal restart, 'up -d' (picks up config/image changes, no forced churn)
#   restart-stacks.sh --only=immich    # restart a single stack
#   restart-stacks.sh --dry-run        # show what would happen, change nothing
#   restart-stacks.sh --force-recreate # 'up -d --force-recreate' - for disaster recovery, not routine use
#
set -uo pipefail

STACKS_DIR="$HOME/stacks"
LOG_FILE="/var/log/restart-stacks.log"
NTFY_TOPIC="mnemosyne-updates-XXXXXX"   # <- match your existing ntfy topic

# Stacks that MUST come first because others depend on their network/services.
# 'monitoring' and 'ghost' each create a network of their own (referenced as
# 'external: true' by other stacks) - they must start before anything that
# depends on them. 'caddy' has no owned network but almost everything routes
# through it, so it stays first by convention.
#
# NOTE: if you add a new stack that owns a network other stacks reference
# externally, add it here AND to KNOWN_NETWORK_OWNERS below.
PRIORITY_STACKS=("caddy" "monitoring" "ghost")

# Networks referenced as 'external: true' that are actually created by another
# stack's own (non-external) 'networks:' block - NOT orphans. Creating these
# manually with 'docker network create' would collide with Compose later,
# since Compose expects to own/create them itself. These are handled by
# ordering (see PRIORITY_STACKS), not by pre-creation.
declare -A KNOWN_NETWORK_OWNERS=(
  ["monitoring"]="monitoring"
  ["ghost_ghost_internal"]="ghost"
)

# Stacks intentionally paused - skipped unless explicitly requested via --only
PAUSED_STACKS=("solar")

# Default is the gentle 'up -d': picks up new images/config, leaves healthy
# containers alone. --force-recreate is opt-in, not default - it tears down
# and rebuilds every container regardless of whether anything changed, which
# is the right hammer for disaster recovery (like today) but overkill for
# routine restarts.
FORCE_RECREATE=""
DRY_RUN=false
ONLY_STACK=""

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --force-recreate) FORCE_RECREATE="--force-recreate" ;;
    --only=*) ONLY_STACK="${arg#*=}" ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

notify() {
  local title="$1" message="$2" priority="${3:-default}"
  curl -s -H "Title: $title" -H "Priority: $priority" -d "$message" \
    "https://ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1 || true
}

# --- Preflight checks -------------------------------------------------------

log "=== restart-stacks.sh started ==="

if ! systemctl is-active --quiet docker; then
  log "FATAL: docker.service is not active. Aborting."
  notify "Mnemosyne: Stack-Restart fehlgeschlagen" "docker.service läuft nicht" urgent
  exit 1
fi

if ! mountpoint -q /mnt/codex; then
  log "FATAL: /mnt/codex is not mounted. Aborting - starting stacks now would write to the wrong path."
  notify "Mnemosyne: Stack-Restart fehlgeschlagen" "/mnt/codex nicht gemountet" urgent
  exit 1
fi

# Networks declared 'external: true' in a compose file are NEVER created by
# Compose itself - they must already exist on the host. A docker data-root
# wipe deletes them along with everything else, and every dependent stack
# then fails with "network X declared as external, but could not be found" -
# silently, if you're not grepping for it in a loop. Discover every such
# network from the actual compose files instead of hardcoding names, so a
# newly added stack with a new external network doesn't bite us again.
# Tracks the most recently seen "  name:" key and prints it whenever an
# "external: true" line follows - works regardless of 2- vs 4-space indent.
mapfile -t EXTERNAL_NETWORKS < <(
  awk '
    /^[ \t]+[A-Za-z0-9_-]+:[ \t]*$/ {
      line = $0
      gsub(/^[ \t]+/, "", line)
      gsub(/:.*/, "", line)
      name = line
    }
    /external:[ \t]*true/ { print name }
  ' "$STACKS_DIR"/*/docker-compose.yml 2>/dev/null | sort -u
)

for net in "${EXTERNAL_NETWORKS[@]}"; do
  if docker network inspect "$net" >/dev/null 2>&1; then
    continue
  fi

  if [ -n "${KNOWN_NETWORK_OWNERS[$net]:-}" ]; then
    log "External network '$net' missing - owned by stack '${KNOWN_NETWORK_OWNERS[$net]}', will be created when that stack starts (see PRIORITY_STACKS)"
  else
    log "External network '$net' missing and has no known owner - creating it now (orphan network, e.g. caddy_proxy)"
    if ! $DRY_RUN; then
      docker network create "$net"
    fi
  fi
done

# --- Build the stack list ---------------------------------------------------

mapfile -t ALL_STACKS < <(find "$STACKS_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)

if [ -n "$ONLY_STACK" ]; then
  STACK_ORDER=("$ONLY_STACK")
else
  STACK_ORDER=()
  for p in "${PRIORITY_STACKS[@]}"; do
    STACK_ORDER+=("$p")
  done
  for s in "${ALL_STACKS[@]}"; do
    skip=false
    for p in "${PRIORITY_STACKS[@]}" "${PAUSED_STACKS[@]}"; do
      [ "$s" = "$p" ] && skip=true
    done
    $skip || STACK_ORDER+=("$s")
  done
fi

log "Stack order: ${STACK_ORDER[*]}"

# --- Start each stack, capturing pass/fail explicitly -----------------------

declare -A RESULT
FAILED=0

for stack in "${STACK_ORDER[@]}"; do
  compose_file="$STACKS_DIR/$stack/docker-compose.yml"

  if [ ! -f "$compose_file" ]; then
    log "SKIP  $stack (no docker-compose.yml found)"
    RESULT["$stack"]="skipped"
    continue
  fi

  log "--- Starting $stack ---"

  if $DRY_RUN; then
    log "DRY-RUN: would run 'docker compose -f $compose_file up -d $FORCE_RECREATE'"
    RESULT["$stack"]="dry-run"
    continue
  fi

  # Capture combined output per stack so a single failure is traceable
  # without scrolling through 20 stacks of build logs.
  output=$(docker compose -f "$compose_file" up -d $FORCE_RECREATE 2>&1)
  exit_code=$?

  if [ $exit_code -ne 0 ] || echo "$output" | grep -qiE "error|could not be found|not found"; then
    log "FAIL  $stack"
    log "$output" | sed 's/^/    /'
    RESULT["$stack"]="fail"
    FAILED=$((FAILED + 1))
  else
    log "OK    $stack"
    RESULT["$stack"]="ok"
  fi
done

# --- Summary -----------------------------------------------------------------

log "=== Summary ==="
for stack in "${STACK_ORDER[@]}"; do
  printf "  %-25s %s\n" "$stack" "${RESULT[$stack]:-unknown}" | tee -a "$LOG_FILE"
done

if [ "$FAILED" -gt 0 ]; then
  log "$FAILED stack(s) failed - check the log above for details."
  notify "Mnemosyne: Stack-Restart mit Fehlern" "$FAILED von ${#STACK_ORDER[@]} Stacks fehlgeschlagen" high
  exit 1
fi

log "All stacks started successfully."
notify "Mnemosyne: Stack-Restart erfolgreich" "${#STACK_ORDER[@]} Stacks neu gestartet" default
exit 0
