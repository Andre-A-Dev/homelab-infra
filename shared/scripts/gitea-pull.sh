#!/bin/bash
# Pulls latest changes from Gitea for homelab-infra repo.
# Runs unattended via cron — requires SSH key auth (no passphrase).

REPO_DIR="$HOME/homelab-infra"
LOG_TAG="gitea-pull"

cd "$REPO_DIR" || { logger -t "$LOG_TAG" "ERROR: repo dir not found: $REPO_DIR"; exit 1; }

RESULT=$(git pull --rebase 2>&1)
STATUS=$?

if [ $STATUS -ne 0 ]; then
    logger -t "$LOG_TAG" "ERROR: git pull failed: $RESULT"
elif echo "$RESULT" | grep -q "Already up to date"; then
    : # Nothing to log — no noise for the common case
else
    logger -t "$LOG_TAG" "INFO: pulled changes: $RESULT"
fi