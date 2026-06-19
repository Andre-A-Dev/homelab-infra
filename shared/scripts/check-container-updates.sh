#!/bin/bash
# =============================================================================
# check-container-updates.sh
# Check all running Docker containers for available image updates.
#
# How it works:
#   - Fetches the manifest digest from the registry via skopeo (metadata only,
#     no image layers are downloaded)
#   - Compares the remote digest against the locally stored digest
#   - Prints a color-coded status table with compose file paths for updates
#
# Dependencies: skopeo, docker
#   Install: sudo apt install skopeo
#
# Usage:
#   check-container-updates.sh            # check all running containers
#   check-container-updates.sh --quiet    # show only containers with updates
#
# Installation:
#   sudo ln -s ~/homelab-infra/mnemosyne/scripts/check-container-updates.sh \
#              /usr/local/bin/check-container-updates.sh
# =============================================================================

set -uo pipefail

# --- CLI flags ----------------------------------------------------------------
QUIET=false
for arg in "$@"; do
    [[ "$arg" == "--quiet" ]] && QUIET=true
done

# --- Colors -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# --- Counters / result tracking -----------------------------------------------
COUNT_OK=0
COUNT_UPDATE=0
COUNT_SKIP=0

# Associative array: container name → compose working dir
declare -A UPDATES_MAP

# --- Column widths (pure ASCII — avoids printf width miscounting) -------------
COL_NAME=26
COL_IMAGE=44

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
        echo -e "${RED}Error: missing dependencies: ${missing[*]}${NC}"
        echo -e "Install with:  sudo apt install ${missing[*]}"
        exit 1
    fi
}

# =============================================================================
# print_row NAME IMAGE STATUS_TEXT STATUS_COLOR [PREFIX]
# Prints one fixed-width table row.
#
# Emoji are NOT inside printf format strings — printf counts bytes, not
# display columns, so emoji break alignment. Status is appended via echo -e.
# =============================================================================
print_row() {
    local name="$1"
    local image_display="$2"
    local status_text="$3"
    local status_color="$4"
    local prefix="${5:-  }"

    local name_col="${name:0:${COL_NAME}}"
    local image_col="${image_display:0:${COL_IMAGE}}"
    [[ ${#name}          -gt $COL_NAME  ]] && name_col="${name:0:$((COL_NAME-1))}…"
    [[ ${#image_display} -gt $COL_IMAGE ]] && image_col="${image_display:0:$((COL_IMAGE-1))}…"

    printf "  ${BOLD}%-${COL_NAME}s${NC}  ${DIM}%-${COL_IMAGE}s${NC}  " \
        "$name_col" "$image_col"
    echo -e "${status_color}${prefix}${status_text}${NC}"
}

# =============================================================================
# get_compose_path CONTAINER_NAME
# Returns the docker-compose.yml path for a container via Docker labels.
# Docker Compose sets com.docker.compose.project.working_dir on every
# container it manages — this is the stack directory.
# Returns empty string for containers not managed by Compose.
# =============================================================================
get_compose_path() {
    local container="$1"
    local workdir
    workdir=$(docker inspect "$container" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' \
        2>/dev/null)

    if [[ -n "$workdir" ]]; then
        echo "${workdir}/docker-compose.yml"
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
#
# Digest computation matches Docker's RepoDigests:
#   Multi-arch:  skopeo returns manifest list   → sha256 = list digest  ✓
#   Single-arch: skopeo returns single manifest → sha256 = image digest ✓
#
# Uses a temp file to preserve exact bytes (shell vars strip trailing newlines).
# =============================================================================
get_remote_digest() {
    local image="$1"
    local tmpfile
    tmpfile=$(mktemp) || return 1

    # --raw: manifest metadata only, no layers downloaded.
    # No --override-arch: we want the manifest list digest (= RepoDigests level).
    if ! skopeo inspect --raw "docker://${image}" >"$tmpfile" 2>/dev/null; then
        rm -f "$tmpfile"
        return 1
    fi

    local digest="sha256:$(sha256sum "$tmpfile" | cut -d' ' -f1)"
    rm -f "$tmpfile"
    echo "$digest"
}

# =============================================================================
# check_container NAME IMAGE
# =============================================================================
check_container() {
    local name="$1"
    local image="$2"

    # Bare image ID — container started without a named registry tag
    if [[ "$image" == sha256:* ]]; then
        $QUIET || print_row "$name" "(no registry tag)" "no tag" "$BLUE" "- "
        ((COUNT_SKIP++)) || true
        return
    fi

    local local_digest
    local_digest=$(get_local_digest "$image")

    # No RepoDigests at all → definitely a local build (never pulled/pushed)
    if [[ -z "$local_digest" ]]; then
        $QUIET || print_row "$name" "$image" "local build" "$DIM" "  "
        ((COUNT_SKIP++)) || true
        return
    fi

    local remote_digest
    if ! remote_digest=$(get_remote_digest "$image") || [[ -z "$remote_digest" ]]; then
        # skopeo failed — distinguish local build from genuine registry error.
        #
        # Local Compose builds have image names with no slash and no colon:
        #   monitoring-meross-exporter  ← local (compose-generated, no registry)
        #   ghostproxy-ghostproxy       ← local
        #
        # Real registry images always have a colon (tag) or slash (namespace):
        #   caddy:latest                ← Docker Hub official
        #   pdreker/fritz_exporter:latest ← Docker Hub namespaced
        if [[ "$image" != */* && "$image" != *:* ]]; then
            $QUIET || print_row "$name" "$image" "local build" "$DIM" "  "
        else
            $QUIET || print_row "$name" "$image" "registry error" "$RED" "! "
        fi
        ((COUNT_SKIP++)) || true
        return
    fi

    if [[ "$local_digest" == "$remote_digest" ]]; then
        ((COUNT_OK++)) || true
        $QUIET || print_row "$name" "$image" "up to date" "$GREEN" "v "
    else
        ((COUNT_UPDATE++)) || true
        # Store compose path alongside container name for the footer
        local compose_path
        compose_path=$(get_compose_path "$name")
        UPDATES_MAP["$name"]="$compose_path"

        # Always show update rows regardless of --quiet
        print_row "$name" "$image" "UPDATE AVAILABLE" "$YELLOW" "^ "
        printf "       ${DIM}local  %-19s${NC}\n" "${local_digest:7:19}…"
        printf "       ${DIM}remote %-19s${NC}\n" "${remote_digest:7:19}…"
    fi
}

# =============================================================================
# print_header
# =============================================================================
print_header() {
    $QUIET && return
    local width=$(( COL_NAME + COL_IMAGE + 22 ))
    local sep; sep=$(printf '%*s' "$width" '' | tr ' ' '-')
    echo
    echo -e "${BOLD}  Docker Image Update Check${NC}  ${DIM}$(date '+%Y-%m-%d %H:%M:%S')  |  ${HOST_ARCH}${NC}"
    echo -e "  ${DIM}${sep}${NC}"
    printf "  ${BOLD}%-${COL_NAME}s  %-${COL_IMAGE}s  %s${NC}\n" "CONTAINER" "IMAGE" "STATUS"
    echo -e "  ${DIM}${sep}${NC}"
    echo
}

# =============================================================================
# print_footer
# =============================================================================
print_footer() {
    local total=$(( COUNT_OK + COUNT_UPDATE + COUNT_SKIP ))
    local width=$(( COL_NAME + COL_IMAGE + 22 ))
    local sep; sep=$(printf '%*s' "$width" '' | tr ' ' '-')
    echo
    echo -e "  ${DIM}${sep}${NC}"
    printf "  ${GREEN}v  Up to date:${NC}              %d\n" "$COUNT_OK"
    printf "  ${YELLOW}^  Update available:${NC}        %d\n" "$COUNT_UPDATE"
    printf "  ${DIM}   Skipped (local/no tag):${NC}    %d\n" "$COUNT_SKIP"
    printf "  ${DIM}   Total:${NC}                     %d\n" "$total"
    echo -e "  ${DIM}${sep}${NC}"

    if [[ ${#UPDATES_MAP[@]} -gt 0 ]]; then
        echo
        echo -e "  ${YELLOW}${BOLD}Updates available:${NC}"

        # Sort container names for consistent output
        local sorted_names
        sorted_names=$(printf '%s\n' "${!UPDATES_MAP[@]}" | sort)

        while IFS= read -r container; do
            local compose_path="${UPDATES_MAP[$container]}"
            if [[ -n "$compose_path" ]]; then
                printf "  ${YELLOW}^${NC}  %-28s ${DIM}%s${NC}\n" "$container" "$compose_path"
            else
                printf "  ${YELLOW}^${NC}  %s\n" "$container"
            fi
        done <<< "$sorted_names"

        echo
        echo -e "  ${DIM}Update one service:${NC}"
        echo -e "  ${DIM}  cd <path above>  →  remove /docker-compose.yml${NC}"
        echo -e "  ${DIM}  docker compose pull <service> && docker compose up -d <service>${NC}"
        echo -e "  ${DIM}  docker image prune -f${NC}"
    fi
    echo
}

# =============================================================================
# main
# =============================================================================
main() {
    check_deps
    print_header

    local containers
    containers=$(docker ps --format '{{.Names}}|{{.Image}}' | sort) || {
        echo -e "${RED}Error: could not list Docker containers${NC}"
        exit 1
    }

    if [[ -z "$containers" ]]; then
        echo -e "  ${DIM}No running containers found.${NC}"
    else
        while IFS='|' read -r name image; do
            check_container "$name" "$image"
        done <<< "$containers"
    fi

    print_footer
}

main "$@"