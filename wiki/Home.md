# homelab-infra Wiki

Infrastructure-as-code for a privacy-first Raspberry Pi homelab. Self-hosted alternatives to cloud services across multiple hosts, managed via Docker Compose and automated with a Git-based deploy pipeline.

---

## Hosts

| Host | Device | Role |
|---|---|---|
| **Mnemosyne** | Raspberry Pi 5 (8 GB, arm64) | Primary server -- all Docker stacks |
| **Boreas** | Raspberry Pi 3B | DNS + network services (home) |
| **Zephyros** | Raspberry Pi 3B+ | DNS + reverse proxy (secondary location) |
| **Hephaestus** | Raspberry Pi 3B | Viessmann heating integration |
| **Astraeus(NX)** | Desktop PC (Ryzen 9 9950X3D, RTX 5080) | Primary workstation, dual-boot Windows 11 / CachyOS |
| **Proteus** | Acer Switch Alpha 12 (N16P3) | CachyOS |

---

## Services at a glance

**Externally accessible**
- [Nextcloud](https://cloud.yourdomain.dedyn.io) -- file sync and sharing
- [Ghost](https://blog.yourdomain.dedyn.io) -- public blog

**Internal (LAN + Tailscale)**
- Vaultwarden -- password manager
- Immich -- photo management
- Grafana / Prometheus / Alertmanager -- monitoring
- Calibre-Web + KOSync -- ebook library
- Gitea -- self-hosted Git
- Ghostwrite / GhostProxy -- writing tools
- Wakapi -- coding time tracker
- Aether -- weather console (Netatmo/Tado/Shelly + forecast)
- Jobiris -- job board monitor
- Carousel -- Markdown -> Instagram carousel slides
- Homepage -- homelab dashboard
- Syncthing -- Obsidian vault sync
- Viessmann Control API -- heating integration (Hephaestus)

All internal services run behind Caddy with an internal CA. No plaintext secrets in tracked files.

---

## Wiki pages

| Page | Contents |
|---|---|
| [[Architecture]] | Design decisions and guiding principles |
| [[Services]] | Full service and port reference for all hosts |
| [[Runbook]] | Operational procedures and common commands |

---

## Repository layout

```
homelab-infra/
├── shared/             # Multi-host scripts, hostname-dispatched
├── scripts/            # Script for Git management (sanitize, audit, sync Gitea <> GitHub)
├── mnemosyne/          # Docker stacks, scripts, systemd units
├── boreas/             # Pi-hole exporter
├── zephyros/           # Pi-hole exporter, Caddy reverse proxy
├── hephaestus/         # vcontrold, Viessmann exporter, Flask API
├── astraeus/           # Windows diagnostics scripts
├── astraeus_nx/        # CachyOS dotfiles, SSH config
├── clusters/           # k3s/Flux GitOps learning environment (daidalos) -- separate from the Compose stacks above
└── wiki/               # Documentation
```

---

## Key principles

**No complexity without concrete benefit.** Every added service or tool must justify its maintenance cost.

**Data sovereignty first.** Vaultwarden, Nextcloud, Gitea, and Immich exist specifically to eliminate cloud dependencies.

**No auto-updates.** Diun sends notifications; updates happen deliberately after changelog review.

**arm64 everywhere.** All images and dependencies must run on Raspberry Pi hardware.

---

## Deploy pipeline

Changes committed on Windows push to a self-hosted Gitea instance. A webhook on Mnemosyne pulls and restarts only affected stacks. GitHub Actions mirrors the same validation checks on this public repo.

```
commit → Gitea → webhook → git pull → docker compose up -d
                         ↘ CI validation (YAML, Prometheus rules, shellcheck)
```

See [[Runbook]] for operational procedures and [[Architecture]] for design rationale.
