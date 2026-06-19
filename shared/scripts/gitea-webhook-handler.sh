#!/bin/bash
# Shared webhook handler — runs on Boreas, Hephaestus, and Zephyros.
# Pulls latest changes from Gitea and executes host-specific actions.
# Mnemosyne uses its own handler at mnemosyne/scripts/gitea-webhook-handler.sh

set -euo pipefail

REPO_DIR="$HOME/homelab-infra"
LOG_FILE="/var/log/webhook-handler.log"
HOST=$(hostname | tr '[:upper:]' '[:lower:]')

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [$HOST] $*" | tee -a "$LOG_FILE"
}

log "INFO: Webhook received — starting pull"

cd "$REPO_DIR"

BEFORE=$(git rev-parse HEAD)
git pull --rebase >> "$LOG_FILE" 2>&1
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    log "INFO: Already up to date — nothing to do"
    exit 0
fi

CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
log "INFO: Changed files:"
echo "$CHANGED" | while read -r f; do log "  $f"; done

case "$HOST" in

  boreas)
    if echo "$CHANGED" | grep -q '^boreas/'; then
        if echo "$CHANGED" | grep -q 'pihole6-exporter.service'; then
            log "INFO: Reloading pihole6-exporter"
            sudo systemctl daemon-reload
            sudo systemctl restart pihole6-exporter \
              && log "INFO: pihole6-exporter restarted" \
              || log "ERROR: pihole6-exporter restart failed"
        fi
    fi
    ;;

  hephaestus)
    if echo "$CHANGED" | grep -q '^hephaestus/'; then
        if echo "$CHANGED" | grep -q 'vcontrold.service'; then
            log "INFO: Reloading vcontrold"
            sudo systemctl daemon-reload
            sudo systemctl restart vcontrold \
              && log "INFO: vcontrold restarted" \
              || log "ERROR: vcontrold restart failed"
        fi
    fi
    ;;

  zephyros)
    if echo "$CHANGED" | grep -q '^zephyros/'; then
        if echo "$CHANGED" | grep -q 'Caddyfile'; then
            log "INFO: Reloading Caddy"
            docker exec caddy caddy reload --config /etc/caddy/Caddyfile \
              && log "INFO: Caddy reloaded" \
              || log "ERROR: Caddy reload failed"
        fi
    fi
    ;;

  *)
    log "WARNING: Unknown host '$HOST' — only git pull performed"
    ;;

esac

log "INFO: Done"
