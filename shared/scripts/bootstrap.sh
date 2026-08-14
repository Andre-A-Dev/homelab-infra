#!/usr/bin/env bash
#
# bootstrap.sh — Provision a fresh Debian 13 node as a k3s cluster node
#
# Target : Daidalos (Dell OptiPlex 7070 Micro, i5-9500T, UHD 630)
# Repo   : homelab-infra/bootstrap/
# Usage  : sudo ./bootstrap.sh
#
# Idempotent: safe to re-run. Every step checks its own state first.
#
# What it does:
#   1. Full system update
#   2. Base packages (curl, git, vim, htop, smartmontools)
#   3. sudo group membership for $TARGET_USER
#   4. Disable swap (Kubernetes requirement)
#   5. Intel VAAPI drivers (QuickSync for Immich ML/transcode)
#   6. open-iscsi + nfs-common (Longhorn prerequisites)
#   7. k3s server WITHOUT traefik/servicelb (we bring ingress-nginx + MetalLB)
#   8. kubectl access for $TARGET_USER (no sudo needed for kubectl)
#   9. Raspberry Pi OS style prompt + ls colors for $TARGET_USER
#
# What it deliberately does NOT do:
#   - SSH hardening (PasswordAuthentication no) — do that manually AFTER
#     key auth has proven stable across reboots. Lesson learned.
#   - Flux bootstrap — that is a separate, one-time step tied to Gitea
#     credentials; see the printed next-steps at the end.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TARGET_USER="${TARGET_USER:-youruser}"
K3S_VERSION="${K3S_VERSION:-}"   # empty = latest stable channel

log()  { echo -e "\033[1;32m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bootstrap]\033[0m $*"; }

# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo ./bootstrap.sh" >&2
    exit 1
fi

if ! id "$TARGET_USER" &>/dev/null; then
    echo "User '$TARGET_USER' does not exist. Set TARGET_USER=<name> and re-run." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# 1. System update
# ---------------------------------------------------------------------------
log "Updating system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -qq

# ---------------------------------------------------------------------------
# 2. Base packages
# ---------------------------------------------------------------------------
log "Installing base packages..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    curl git vim htop sudo smartmontools ca-certificates gnupg

# ---------------------------------------------------------------------------
# 3. sudo membership
# ---------------------------------------------------------------------------
if id -nG "$TARGET_USER" | grep -qw sudo; then
    log "User '$TARGET_USER' already in sudo group."
else
    log "Adding '$TARGET_USER' to sudo group..."
    usermod -aG sudo "$TARGET_USER"
    warn "Group change takes effect on next login of $TARGET_USER."
fi

# ---------------------------------------------------------------------------
# 4. Disable swap (k8s requirement)
# ---------------------------------------------------------------------------
if [[ -n "$(swapon --show)" ]]; then
    log "Disabling swap..."
    swapoff -a
else
    log "Swap already off."
fi
if grep -qE '^[^#].*\bswap\b' /etc/fstab; then
    log "Removing swap entries from /etc/fstab..."
    sed -i.bak '/\bswap\b/s/^/#/' /etc/fstab
fi

# ---------------------------------------------------------------------------
# 5. Intel VAAPI drivers (QuickSync — Immich ML / transcode via /dev/dri)
# ---------------------------------------------------------------------------
if ! grep -q non-free-firmware /etc/apt/sources.list /etc/apt/sources.list.d/*.list 2>/dev/null; then
    warn "Enabling contrib non-free non-free-firmware components..."
    sed -i 's/^\(deb.*main\)$/\1 contrib non-free non-free-firmware/' /etc/apt/sources.list
    apt-get update -qq
fi
log "Installing Intel VAAPI drivers..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    intel-media-va-driver-non-free vainfo intel-gpu-tools
if [[ -e /dev/dri/renderD128 ]]; then
    log "iGPU render node present: /dev/dri/renderD128 ✓"
else
    warn "/dev/dri/renderD128 not found — check BIOS iGPU setting."
fi

# ---------------------------------------------------------------------------
# 6. Longhorn prerequisites
# ---------------------------------------------------------------------------
log "Installing open-iscsi + nfs-common (Longhorn prerequisites)..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq open-iscsi nfs-common
systemctl enable --now iscsid

# ---------------------------------------------------------------------------
# 7. k3s server (no traefik, no servicelb — we bring our own)
# ---------------------------------------------------------------------------
if systemctl is-active --quiet k3s; then
    log "k3s already installed and running."
else
    log "Installing k3s server (traefik + servicelb disabled)..."
    export INSTALL_K3S_EXEC="server --disable traefik --disable servicelb --write-kubeconfig-mode 640"
    [[ -n "$K3S_VERSION" ]] && export INSTALL_K3S_VERSION="$K3S_VERSION"
    curl -sfL https://get.k3s.io | sh -
fi

# ---------------------------------------------------------------------------
# 8. kubectl access for the user (no sudo for daily kubectl)
# ---------------------------------------------------------------------------
USER_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
KUBE_DIR="$USER_HOME/.kube"
if [[ ! -f "$KUBE_DIR/config" ]]; then
    log "Setting up kubeconfig for $TARGET_USER..."
    mkdir -p "$KUBE_DIR"
    cp /etc/rancher/k3s/k3s.yaml "$KUBE_DIR/config"
    chown -R "$TARGET_USER":"$TARGET_USER" "$KUBE_DIR"
    chmod 600 "$KUBE_DIR/config"
else
    log "kubeconfig for $TARGET_USER already present."
fi
# make kubectl find it (idempotent)
BASHRC="$USER_HOME/.bashrc"
if ! grep -q 'KUBECONFIG=' "$BASHRC"; then
    echo 'export KUBECONFIG=$HOME/.kube/config' >> "$BASHRC"
fi

# ---------------------------------------------------------------------------
# 9. Raspberry Pi OS style prompt (managed block, idempotent)
# ---------------------------------------------------------------------------
MARKER="# >>> bootstrap.sh prompt >>>"
if ! grep -qF "$MARKER" "$BASHRC"; then
    log "Adding Raspberry Pi OS style prompt to $BASHRC..."
    cat >> "$BASHRC" << 'EOF'

# >>> bootstrap.sh prompt >>>
PS1='${debian_chroot:+($debian_chroot)}\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
if [ -x /usr/bin/dircolors ]; then
    eval "$(dircolors -b)"
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
fi
alias ll='ls -alF'
alias la='ls -A'
# <<< bootstrap.sh prompt <<<
EOF
else
    log "Prompt block already present."
fi

# ---------------------------------------------------------------------------
# Done — status + next steps
# ---------------------------------------------------------------------------
log "Waiting for node to become Ready..."
sleep 5
k3s kubectl get node || true

cat << 'EOF'

============================================================
 bootstrap.sh finished.

 Verify:
   kubectl get nodes          # STATUS should be Ready
   vainfo                     # should list VAProfile entries (QuickSync OK)

 Next steps (manual, one-time):
   1. flux bootstrap against your Gitea repo:
        flux bootstrap git \
          --url=ssh://git@<mnemosyne>:2222/youruser/homelab-infra.git \
          --branch=main \
          --path=clusters/daidalos
   2. Commit a throwaway hello-world manifest, push,
      watch: flux get kustomizations --watch
   3. SSH hardening (PasswordAuthentication no) — only after
      key auth survives a few reboots.
============================================================
EOF
