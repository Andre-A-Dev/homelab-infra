#!/bin/bash
# =============================================================================
# container-update-metrics.sh
# Prometheus Textfile Collector exporter for Docker container update status.
#
# Writes metrics to Node Exporter's textfile_collector directory.
# Node Exporter picks them up automatically on the next scrape.
#
# Metrics exposed:
#   container_image_update_available   — 1=update available, 0=up to date
#   container_image_check_status       — 0=up_to_date 1=update_available
#                                        2=local_build 3=error
#   container_update_check_timestamp_seconds — Unix time of last run
#   container_update_check_duration_seconds  — runtime of last check
#
# Dependencies: skopeo, docker
#   Install: sudo apt install skopeo
#
# Triggered by: systemd timer (container-update-metrics.timer)
#   Recommended interval: every 2 hours
#   (skopeo fetches manifest metadata only — no layer downloads)
#
# Installation:
#   sudo ln -s ~/homelab-infra/mnemosyne/scripts/container-update-metrics.sh \
#              /usr/local/bin/container-update-metrics.sh
# =============================================================================

set -uo pipefail

# --- Output path --------------------------------------------------------------
TEXTFILE_DIR="/var/lib/node_exporter/textfile_collector"
OUTPUT_FILE="${TEXTFILE_DIR}/container_updates.prom"
TMP_FILE="${OUTPUT_FILE}.tmp"

# --- Auto-detect host architecture --------------------------------------------
case "$(uname -m)" in
    aarch64) HOST_ARCH="arm64" ;;
    armv7l)  HOST_ARCH="arm"   ;;
    x86_64)  HOST_ARCH="amd64" ;;
    *)       HOST_ARCH="$(uname -m)" ;;
esac

# =============================================================================
# check_deps — abort early if required tools are missing
# =============================================================================
check_deps() {
    local missing=()
    command -v skopeo >/dev/null 2>&1 || missing+=("skopeo")
    command -v docker >/dev/null 2>&1 || missing+=("docker")

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "Error: missing dependencies: ${missing[*]}" >&2
        echo "Install with: sudo apt install ${missing[*]}" >&2
        exit 1
    fi

    if [[ ! -d "$TEXTFILE_DIR" ]]; then
        echo "Error: textfile_collector directory not found: ${TEXTFILE_DIR}" >&2
        echo "Create with: sudo mkdir -p ${TEXTFILE_DIR}" >&2
        exit 1
    fi
}

# =============================================================================
# get_local_digest IMAGE
# Returns the manifest digest from Docker's RepoDigests (sha256:...).
# =============================================================================
get_local_digest() {
    local image="$1"
    docker image inspect "$image" \
        --format '{{range .RepoDigests}}{{.}}{{"\n"}}{{end}}' 2>/dev/null \
        | head -1 \
        | cut -d'@' -f2
}

# =============================================================================
# get_remote_digest IMAGE
# Fetches the raw manifest from the registry via skopeo (no layer download).
# sha256 of the raw bytes matches Docker's RepoDigests entry exactly.
# =============================================================================
get_remote_digest() {
    local image="$1"
    local tmpfile
    tmpfile=$(mktemp) || return 1

    if ! skopeo inspect --raw "docker://${image}" >"$tmpfile" 2>/dev/null; then
        rm -f "$tmpfile"
        return 1
    fi

    local digest="sha256:$(sha256sum "$tmpfile" | cut -d' ' -f1)"
    rm -f "$tmpfile"
    echo "$digest"
}

# =============================================================================
# get_compose_path CONTAINER_NAME
# Returns the docker-compose.yml path via Docker Compose label.
# =============================================================================
get_compose_path() {
    local container="$1"
    local workdir
    workdir=$(docker inspect "$container" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
        2>/dev/null)
    [[ -n "$workdir" ]] && echo "${workdir}/docker-compose.yml" || echo ""
}

# =============================================================================
# escape_label VALUE
# Escapes backslashes, double quotes and newlines for Prometheus label values.
# =============================================================================
escape_label() {
    local val="$1"
    val="${val//\\/\\\\}"   # backslash → \\
    val="${val//\"/\\\"}"   # " → \"
    val="${val//$'\n'/\\n}" # newline → \n
    echo "$val"
}

# =============================================================================
# write_metric CONTAINER IMAGE STATUS_CODE COMPOSE_PATH
#
# Status codes:
#   0 = up to date
#   1 = update available
#   2 = local build  (not tracked, no registry)
#   3 = error        (registry unreachable or digest mismatch)
# =============================================================================
write_metric() {
    local container="$1"
    local image="$2"
    local status_code="$3"
    local compose_path="$4"

    local safe_container; safe_container=$(escape_label "$container")
    local safe_image;     safe_image=$(escape_label "$image")
    local safe_path;      safe_path=$(escape_label "$compose_path")

    local labels="container=\"${safe_container}\",image=\"${safe_image}\",compose_path=\"${safe_path}\""

    # 1 if update is available, 0 otherwise (local builds and errors are absent)
    if [[ "$status_code" -eq 0 || "$status_code" -eq 1 ]]; then
        echo "container_image_update_available{${labels}} ${status_code}" >> "$TMP_FILE"
    fi

    # Full status code for all containers (useful for dashboards and alerts)
    echo "container_image_check_status{${labels}} ${status_code}" >> "$TMP_FILE"
}

# =============================================================================
# check_container NAME IMAGE
# =============================================================================
check_container() {
    local name="$1"
    local image="$2"
    local compose_path
    compose_path=$(get_compose_path "$name")

    # Bare image ID — no registry tag to check
    if [[ "$image" == sha256:* ]]; then
        write_metric "$name" "$image" 3 "$compose_path"
        return
    fi

    local local_digest
    local_digest=$(get_local_digest "$image")

    # No RepoDigests → definitely a local build
    if [[ -z "$local_digest" ]]; then
        write_metric "$name" "$image" 2 "$compose_path"
        return
    fi

    local remote_digest
    if ! remote_digest=$(get_remote_digest "$image") || [[ -z "$remote_digest" ]]; then
        # No slash + no colon → compose-generated name, local build
        if [[ "$image" != */* && "$image" != *:* ]]; then
            write_metric "$name" "$image" 2 "$compose_path"
        else
            write_metric "$name" "$image" 3 "$compose_path"
        fi
        return
    fi

    if [[ "$local_digest" == "$remote_digest" ]]; then
        write_metric "$name" "$image" 0 "$compose_path"
    else
        write_metric "$name" "$image" 1 "$compose_path"
    fi
}

# =============================================================================
# main
# =============================================================================
main() {
    check_deps

    local start_time
    start_time=$(date +%s)

    # Write header to temp file (atomic swap at the end)
    cat > "$TMP_FILE" << 'HEADER'
# HELP container_image_update_available 1 if a newer image is available in the registry, 0 if up to date
# TYPE container_image_update_available gauge
# HELP container_image_check_status Update check status: 0=up_to_date 1=update_available 2=local_build 3=error
# TYPE container_image_check_status gauge
HEADER

    local containers
    containers=$(docker ps --format '{{.Names}}|{{.Image}}' | sort) || {
        echo "Error: could not list Docker containers" >&2
        rm -f "$TMP_FILE"
        exit 1
    }

    if [[ -n "$containers" ]]; then
        while IFS='|' read -r name image; do
            check_container "$name" "$image"
        done <<< "$containers"
    fi

    local end_time
    end_time=$(date +%s)
    local duration_seconds
    duration_seconds=$(( end_time - start_time ))

    # Append run metadata
    cat >> "$TMP_FILE" << FOOTER
# HELP container_update_check_timestamp_seconds Unix timestamp of the last completed check
# TYPE container_update_check_timestamp_seconds gauge
container_update_check_timestamp_seconds $(date +%s)
# HELP container_update_check_duration_seconds Duration of the last check run in seconds
# TYPE container_update_check_duration_seconds gauge
container_update_check_duration_seconds ${duration_seconds}
FOOTER

    # Atomic replace — Prometheus never reads a partially written file
    mv "$TMP_FILE" "$OUTPUT_FILE"
}

main "$@"