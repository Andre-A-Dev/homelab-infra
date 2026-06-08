#!/bin/bash
# Runs after git pull — restarts only stacks with changed files

REPO="/home/youruser/homelab-infra"
STACKS_DIR="/home/youruser/stacks"
LOG="/var/log/webhook-handler.log"

echo "=== Webhook triggered: $(date) ===" >> "$LOG"

# Pull latest changes
cd "$REPO" || exit 1
git pull >> "$LOG" 2>&1

# Find changed files between previous and current HEAD
CHANGED=$(git diff --name-only HEAD@{1} HEAD 2>/dev/null)
echo "Changed files: $CHANGED" >> "$LOG"

# Extract unique stack names from changed paths
# Matches: mnemosyne/stacks/<stack>/...
STACKS=$(echo "$CHANGED" | grep "^mnemosyne/stacks/" | \
  grep -v "/grafana/dashboards/" | \
  grep "docker-compose\.yml" | \
  cut -d'/' -f3 | sort -u)

if [ -z "$STACKS" ]; then
  echo "No stack changes detected — nothing to restart" >> "$LOG"
  exit 0
fi

# Restart affected stacks
for stack in $STACKS; do
  STACK_DIR="$STACKS_DIR/$stack"
  if [ -f "$STACK_DIR/docker-compose.yml" ]; then
    echo "Restarting stack: $stack" >> "$LOG"
    docker compose -f "$STACK_DIR/docker-compose.yml" up -d >> "$LOG" 2>&1
    echo "Done: $stack" >> "$LOG"
  else
    echo "Skipping $stack — no docker-compose.yml found" >> "$LOG"
  fi
done

echo "=== Webhook handler finished: $(date) ===" >> "$LOG"
