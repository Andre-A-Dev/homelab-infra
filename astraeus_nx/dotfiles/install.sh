#!/usr/bin/env bash
# install.sh — symlinks dotfiles from this repo into their real locations.
# Idempotent: safe to re-run. Backs up any pre-existing real file first.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="$HOME/.dotfiles-backup/$(date +%Y%m%d-%H%M%S)"

# format: "repo-relative-path:target-path"
LINKS=(
  "openrgb/OpenRGB.json:$HOME/.config/OpenRGB/OpenRGB.json"
  "openrgb/sizes.ors:$HOME/.config/OpenRGB/sizes.ors"
  "openrgb/profiles/Off.orp:$HOME/.config/OpenRGB/Off.orp"
  "openrgb/profiles/Auralis_Ice.orp:$HOME/.config/OpenRGB/Auralis_Ice.orp"
  "openrgb/plugins-settings:$HOME/.config/OpenRGB/plugins/settings"

  "coolercontrol/CoolerControl.conf:$HOME/.config/org.coolercontrol.CoolerControl/CoolerControl.conf"

  "systemd-user/openrgb.service:$HOME/.config/systemd/user/openrgb.service"

  "kde/kglobalshortcutsrc:$HOME/.config/kglobalshortcutsrc"
  "kde/kwinrc:$HOME/.config/kwinrc"

  "../../shared/dotfiles/fish/config.fish:$HOME/.config/fish/config.fish"
  "../../shared/dotfiles/fish/functions/sysup.fish:$HOME/.config/fish/functions/sysup.fish"
)

link_one() {
  local src dst
  src="$REPO_ROOT/$1"
  dst="$2"

  if [ ! -e "$src" ]; then
    echo "SKIP   (missing in repo) $1"
    return
  fi

  if [ -L "$dst" ] && [ "$(readlink -f "$dst")" = "$(readlink -f "$src")" ]; then
    echo "OK     $dst"
    return
  fi

  if [ -e "$dst" ] || [ -L "$dst" ]; then
    mkdir -p "$BACKUP_DIR/$(dirname "${dst#"$HOME"/}")"
    mv "$dst" "$BACKUP_DIR/${dst#"$HOME"/}"
    echo "BACKUP $dst -> $BACKUP_DIR/${dst#"$HOME"/}"
  fi

  mkdir -p "$(dirname "$dst")"
  ln -s "$src" "$dst"
  echo "LINK   $dst -> $src"
}

for entry in "${LINKS[@]}"; do
  link_one "${entry%%:*}" "${entry#*:}"
done

echo
echo "Done. Backups (if any) are in: $BACKUP_DIR"