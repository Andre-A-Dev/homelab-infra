# Services Reference

All internal `.home` domains require the Caddy root certificate to be imported on the client. External access from remote the home network requires Tailscale, or — for services proxied via Zephyros — only the Zephyros CA certificate on the accessing device.

---

## Mnemosyne — `192.168.1.10` · Tailscale `100.x.x.x`

| Service | URL / Address | Port | TLS | External (Tailscale) |
|---|---|---|---|---|
| Vaultwarden | `https://vault.home` | `443` | Internal CA | ✅ |
| Nextcloud | `https://cloud.yourdomain.dedyn.io` | `443` | Let's Encrypt | ✅ public |
| Gitea (web) | `https://git.home` | `443` | Internal CA | ✅ |
| Gitea (SSH) | `ssh://git@192.168.1.10` | `2222` | — | ✅ |
| Ghost | `https://blog.yourdomain.dedyn.io` | `443` | Let's Encrypt | ✅ public |
| Ghostwrite | `https://ghostwrite.home` | `443` | Internal CA | ✅ |
| GhostProxy | `https://ghostproxy.home` | `443` | Internal CA | ✅ |
| Immich | `https://immich.home` | `443` | Internal CA | ✅ (`http://100.x.x.x:2283`) |
| Homepage | `https://homepage.home` | `443` | Internal CA | ✅ |
| Grafana | `https://grafana.home` | `443` | Internal CA | ✅ |
| Prometheus | `https://prometheus.home` | `443` | Internal CA | ✅ |
| Calibre-Web | `https://calibre.home` | `443` | Internal CA | ✅ |
| KOSync | `https://kosync.home` | `443` | Internal CA | ✅ |
| Wakapi | `https://wakapi.home` | `443` | Internal CA | ✅ |
| Jobiris | `https://jobiris.home` | `443` | Internal CA | ✅ |
| Syncthing | `https://syncthing.home` | `443` | Internal CA | ✅ |
| Viessmann | `https://viessmann.home` | `443` | Internal CA | ✅ |
| Alertmanager | `https://alertmanager.home` | `443` | Internal CA | ✅ |
| Caddy (HTTP) | — | `80` | — | — |
| Webhook listener | — | `9000` | — | — |

### Monitoring exporters (internal only)

| Exporter | Port | Notes |
|---|---|---|
| Node Exporter | `9100` | System metrics |
| Blackbox Exporter | `9115` | HTTP uptime checks |
| Alertmanager | `9093` | Alert routing → ntfy |
| Netatmo Exporter | `9210` | Weather station |
| Fritz Exporter (home) | `9787` | FritzBox home network |
| Tado Exporter | `9100` | Heating metrics |
| Nextcloud Exporter | `9205` | Nextcloud metrics |
| cAdvisor | `8080` | Container metrics |
| Shelly Exporter | `9117` | Shelly smart plugs (Gen1 + Gen2/3) |

> Prometheus (`9090`) and all exporters are internal only. Never expose these ports externally.

---

## Boreas — `192.168.1.11` (home network)

| Service | URL / Address | Port | Notes |
|---|---|---|---|
| Pi-hole | `http://192.168.1.11/admin` | `80` | DNS filtering, v6 |
| Unbound | — | `5335` | Upstream for Pi-hole only |
| Node Exporter | — | `9100` | Scraped by Prometheus on Mnemosyne |
| Pi-hole Exporter | — | `9666` | systemd service, scraped by Prometheus |

---

## Zephyros — `192.168.1.11`* (remote network) · Tailscale `100.y.y.y`

> \* Zephyros shares the `192.168.1.0/24` range with the home network, but is on a physically separate network at the remote's location. No routing conflict — Tailscale addresses it exclusively via `100.y.y.y` from remote.

| Service | URL / Address | Port | Notes |
|---|---|---|---|
| Pi-hole | `http://192.168.1.11/admin` | `80` | DNS filtering, v6 |
| Unbound | — | `5335` | Upstream for Pi-hole only |
| Caddy | — | `443` | Reverse proxy → Mnemosyne via Tailscale |
| Node Exporter | — | `9100` | Scraped by Prometheus on Mnemosyne via Tailscale |
| Pi-hole Exporter | — | `9666` | systemd service, scraped via Tailscale |
| Fritz Exporter | — | `9787` | FritzBox at remote's location |
| Fritz Exporter Lua | — | `9042` | FritzBox DECT + system metrics via Lua API |

### Proxied domains (via Zephyros Caddy → Mnemosyne)

Devices on the remote's network without Tailscale can access these `.home` services after importing the Zephyros CA certificate.

| Domain | Notes |
|---|---|
| `ghostwrite.home` | Resolves to Zephyros (`192.168.1.11`), proxied to Mnemosyne |
| `ghostproxy.home` | Resolves to Zephyros (`192.168.1.11`), proxied to Mnemosyne |

---

## Hephaestus — `192.168.1.13`

| Service | URL / Address | Port | Notes |
|---|---|---|---|
| vcontrold | `127.0.0.1:3002` | `3002` | Viessmann KW2 daemon, localhost only |
| Viessmann Control API | `https://viessmann.home` | `8081` | Proxied via Caddy on Mnemosyne, token injected |
| Node Exporter | — | `9100` | Scraped by Prometheus on Mnemosyne (60s interval) |

> vcontrold and the Flask API are internal to Hephaestus. Port 8081 is not exposed externally — access only via `viessmann.home` through Caddy.

---

## DNS Records (Pi-hole, Boreas)

| Domain | Resolves to | Notes |
|---|---|---|
| `mnemosyne.local` | `192.168.1.10` | |
| `boreas.local` | `192.168.1.11` | |
| `hephaestus.local` | `192.168.1.13` | |
| `vault.home` | `192.168.1.10` | |
| `git.home` | `192.168.1.10` | |
| `grafana.home` | `192.168.1.10` | |
| `prometheus.home` | `192.168.1.10` | |
| `calibre.home` | `192.168.1.10` | |
| `kosync.home` | `192.168.1.10` | |
| `syncthing.home` | `192.168.1.10` | |
| `immich.home` | `192.168.1.10` | |
| `homepage.home` | `192.168.1.10` | |
| `ghostwrite.home` | `192.168.1.10` | |
| `ghostproxy.home` | `192.168.1.10` | |
| `viessmann.home` | `192.168.1.10` | Caddy proxy → Hephaestus:8081 |
| `cloud.yourdomain.dedyn.io` | `192.168.1.10` | Also public via deSEC DynDNS |
| `blog.yourdomain.dedyn.io` | `192.168.1.10` | Public via deSEC DynDNS |
| `yourdomain.dedyn.io` | `192.168.1.10` | deSEC DynDNS |

### DNS Records (Pi-hole, Zephyros — remote network)

| Domain | Resolves to | Notes |
|---|---|---|
| `ghostwrite.home` | `192.168.1.11` | → Zephyros Caddy → Mnemosyne via Tailscale |
| `ghostproxy.home` | `192.168.1.11` | → Zephyros Caddy → Mnemosyne via Tailscale |

---

## Storage Paths

| Data | Path | Host |
|---|---|---|
| Vaultwarden | `/mnt/vault/vaultwarden/data/` | Mnemosyne |
| Caddy TLS | `/mnt/vault/caddy/` | Mnemosyne |
| Nextcloud files | `/mnt/codex/nextcloud/data/` | Mnemosyne |
| Nextcloud DB | `/mnt/codex/nextcloud/db/` | Mnemosyne |
| Gitea | `/mnt/codex/gitea/data/` | Mnemosyne |
| Immich uploads | `/mnt/codex/immich/upload/` | Mnemosyne |
| Immich DB | `/mnt/codex/immich/db/` | Mnemosyne |
| Ghost content | `/mnt/codex/ghost/content/` | Mnemosyne |
| Calibre library | `/mnt/codex/calibre-library/Library/` | Mnemosyne |
| Syncthing vault | `/mnt/codex/syncthing/obsidian/` | Mnemosyne |
| Wakapi data | `/mnt/codex/wakapi/` | Mnemosyne |
| Alertmanager data | `/mnt/codex/alertmanager/` | Mnemosyne |
| Netatmo token | `/mnt/codex/netatmo-exporter/` | Mnemosyne |
| Tado token | `/mnt/codex/tado-exporter/` | Mnemosyne |
| Docker data root | `/mnt/codex/docker/` | Mnemosyne |
| containerd root | `/mnt/codex/containerd/` | Mnemosyne |
| Backup target | `/mnt/backup/` | Mnemosyne (UUID `XXXX-XXXX`) |
| vcontrold config | `/etc/vcontrold/` | Hephaestus |
| Viessmann metrics | `/var/lib/node_exporter/textfile_collector/viessmann.prom` | Hephaestus |
| Viessmann change log | `/var/log/viessmann-control.log.json` | Hephaestus |
| Fan metrics | `/var/lib/node_exporter/textfile_collector/fan.prom` | Mnemosyne |

---

## Symlinks

| Symlink | Target |
|---|---|
| `~/stacks/` | `~/homelab-infra/mnemosyne/stacks/` |
| `/usr/local/bin/backup-services.sh` | `~/homelab-infra/mnemosyne/scripts/backup-services.sh` |
| `/usr/local/bin/verify-backup.sh` | `~/homelab-infra/mnemosyne/scripts/verify-backup.sh` |
| `/usr/local/bin/restore-services.sh` | `~/homelab-infra/mnemosyne/scripts/restore-services.sh` |
| `/usr/local/bin/tailscale-metrics.sh` | `~/homelab-infra/mnemosyne/scripts/tailscale-metrics.sh` |
| `/usr/local/bin/fan-metrics.sh` | `~/homelab-infra/mnemosyne/scripts/fan-metrics.sh` |
