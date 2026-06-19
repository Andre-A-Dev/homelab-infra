# shared/scripts

Generic maintenance scripts shared across all hosts. Each script is symlinked
to `/usr/local/bin/` on every host that uses it.

---

## Overview

| Script | Purpose | Hosts |
|---|---|---|
| `system-update.sh` | Manual system update & cleanup | all |
| `check-container-updates.sh` | Check running containers for image updates | Mnemosyne |
| `gitea-webhook-handler.sh` | Pull repo changes and run host-specific actions | Boreas, Hephaestus, Zephyros |
| `gitea-pull.sh` | Pull latest repo changes unattended (cron) | Boreas, Hephaestus, Zephyros |

---

## system-update.sh

Manual update cycle for the host system. Detects the package manager
automatically (`apt` or `pacman`/`paru`).

**apt:** `update` → `dist-upgrade` → `autoremove` → `autoclean`  
**pacman/paru:** `paru -Syu` → orphan removal → `paccache -rk2`

After the package cycle: checks for failed systemd units, detects reboot
requirement, writes Prometheus textfile metrics, sends an ntfy notification.

```bash
system-update              # full run
system-update --dry-run    # show what would happen, no changes
```

**Config:** `/etc/system-update.conf` (not in Git, create on each host)

```bash
sudo install -m 644 -o "$(id -un)" /dev/null /etc/system-update.conf
nano /etc/system-update.conf
```

```bash
NTFY_TOPIC=your-topic-name
# NTFY_SERVER=https://ntfy.sh
```

**Outputs:**
- Log: `/var/log/system-update.log`
- Metrics: `/var/lib/node_exporter/textfile_collector/system_update.prom`

---

## check-container-updates.sh

Checks all running Docker containers for available image updates without
downloading image layers. Uses `skopeo` to fetch the remote manifest digest
and compares it against the locally stored digest.

**Dependency:** `skopeo` — `sudo apt install skopeo`

```bash
check-container-updates            # check all running containers
check-container-updates --quiet    # show only containers with updates
```

---

## gitea-webhook-handler.sh

Triggered by a Gitea webhook after every push. Pulls the latest changes and
executes host-specific actions based on which files changed.

Dispatches by hostname:

| Host | Action |
|---|---|
| Boreas | Restarts `pihole6-exporter` if its service file changed |
| Hephaestus | Restarts `vcontrold` if its service file changed |
| Zephyros | Reloads Caddy if `Caddyfile` changed |

Called by `adnanh/webhook` on port 9000. Logs to `/var/log/webhook-handler.log`.

> Mnemosyne uses its own handler at `mnemosyne/scripts/gitea-webhook-handler.sh`.

---

## gitea-pull.sh

Pulls the latest `homelab-infra` changes from Gitea via `git pull --rebase`.
Runs unattended via cron on Boreas, Hephaestus, and Zephyros as a fallback
alongside the webhook handler.

Logs to syslog (`logger`) — only on error or actual changes, silent on
"Already up to date".

---

## Installation

```bash
# Symlink to /usr/local/bin (run on each host)
sudo ln -sf ~/homelab-infra/shared/scripts/system-update.sh /usr/local/bin/system-update
sudo ln -sf ~/homelab-infra/shared/scripts/check-container-updates.sh /usr/local/bin/check-container-updates

# system-update: create log file with correct ownership (once per host)
sudo install -m 644 -o "$(id -un)" /dev/null /var/log/system-update.log
```
