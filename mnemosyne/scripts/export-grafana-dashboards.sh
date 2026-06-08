#!/bin/bash
# Exports all Grafana dashboards as JSON files

source /home/youruser/stacks/monitoring/.env

GRAFANA_URL="https://grafana.home"
GRAFANA_USER="admin"
GRAFANA_PASS="${GF_SECURITY_ADMIN_PASSWORD}"
OUTPUT_DIR="$(dirname "$0")/../stacks/monitoring/grafana/dashboards"

mkdir -p "$OUTPUT_DIR"

# Fetch all dashboard UIDs
DASHBOARDS=$(curl -sk "${GRAFANA_URL}/api/search?type=dash-db" \
  -u "${GRAFANA_USER}:${GRAFANA_PASS}" | \
  python3 -c "import sys,json; [print(d['uid']+'|'+d['title']) for d in json.load(sys.stdin)]")

while IFS='|' read -r uid title; do
  # Sanitize filename
  filename=$(echo "$title" | tr ' /' '_' | tr -d '!:&')
  
  curl -sk "${GRAFANA_URL}/api/dashboards/uid/${uid}" \
    -u "${GRAFANA_USER}:${GRAFANA_PASS}" | \
    python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps(d['dashboard'], indent=2))" \
    > "${OUTPUT_DIR}/${filename}.json"
  
  echo "Exported: ${title}"
done <<< "$DASHBOARDS"

echo "Done — $(ls "$OUTPUT_DIR"/*.json | wc -l) dashboards exported to $OUTPUT_DIR"
