#!/bin/bash
# Deploys pump-alerts.yml from Gitea to Prometheus.
# Validates rules with promtool before deploying — aborts on invalid YAML.

GITEA_TOKEN=$(grep PUMP_GITEA_TOKEN /home/youruser/stacks/monitoring/.env | cut -d= -f2)
REPO_URL="https://${GITEA_TOKEN}@git.home/${GITEA_USER}/pump_alerts "
REPO_DIR="/tmp/pump-alerts-repo"
OUTFILE="/home/youruser/stacks/monitoring/prometheus/pump-alerts.yml"
TMPFILE="/tmp/pump-alerts-check.yml"
LOG="/var/log/webhook-handler.log"

echo "=== pump-alerts deploy: $(date) ===" >> "$LOG"

# Pull or clone repo
if [ -d "$REPO_DIR/.git" ]; then
    cd "$REPO_DIR" && GIT_SSL_NO_VERIFY=true git pull >> "$LOG" 2>&1
else
    GIT_SSL_NO_VERIFY=true git clone "https://${GITEA_TOKEN}@git.home/..." 2>/dev/null
fi

# Validate before deploying — copy to temp location inside Prometheus container
cp "$REPO_DIR/alerts.yml" "$TMPFILE"
docker cp "$TMPFILE" prometheus:/tmp/pump-alerts-check.yml >> "$LOG" 2>&1

if ! docker exec prometheus promtool check rules /tmp/pump-alerts-check.yml >> "$LOG" 2>&1; then
    echo "VALIDATION FAILED — aborting deploy, keeping current pump-alerts.yml" >> "$LOG"
    echo "=== pump-alerts deploy aborted: $(date) ===" >> "$LOG"
    exit 1
fi

echo "Validation passed" >> "$LOG"

# Deploy validated file
cp "$REPO_DIR/alerts.yml" "$OUTFILE" && echo "Copied pump-alerts.yml" >> "$LOG"

# Reload Prometheus
docker exec prometheus wget -qO- --post-data="" http://localhost:9090/-/reload >> "$LOG" 2>&1
echo "Prometheus reloaded" >> "$LOG"
echo "=== pump-alerts deploy finished: $(date) ===" >> "$LOG"