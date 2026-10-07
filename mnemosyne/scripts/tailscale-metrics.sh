#!/bin/bash
# =============================================================================
# tailscale-metrics.sh
# =============================================================================
# Exports Tailscale's internal metrics for the node_exporter textfile collector.
# Runs from cron twice a minute (once plain, once with a 30s sleep).
# =============================================================================

set -uo pipefail

METRICS_DIR="/var/lib/node_exporter/textfile_collector"
TMP="${METRICS_DIR}/tailscale.prom.tmp"
OUT="${METRICS_DIR}/tailscale.prom"

# `sudo cmd > file` performs the redirect in THIS shell, not under sudo — it
# only ever worked because the invoking user happens to have write access to
# the collector directory. Piping into `sudo tee` puts the privileged write
# where it belongs (SC2024).
if ! sudo tailscale debug metrics | sudo tee "$TMP" > /dev/null; then
  echo "ERROR: tailscale debug metrics failed — leaving previous .prom intact" >&2
  rm -f "$TMP"
  exit 1
fi

# An empty result would otherwise replace a good file with nothing, and the
# textfile collector would happily serve zero metrics without complaining.
if [ ! -s "$TMP" ]; then
  echo "ERROR: tailscale debug metrics produced no output — not replacing $OUT" >&2
  rm -f "$TMP"
  exit 1
fi

# mv within one filesystem is atomic: node_exporter never sees a half-written
# file.
sudo mv "$TMP" "$OUT"
