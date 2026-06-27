# Zephyros

Raspberry Pi 3B+ at a **remote location**, reachable from the home network only
via Tailscale (`100.y.y.y`). It provides local DNS and acts as a
Caddy reverse proxy so that `.home` services on Mnemosyne remain accessible
from that site.

## Role

| Service | How | Port |
|---|---|---|
| Pi-hole v6 | native install | 53 / 80 (admin) |
| Unbound | native install | 5335 |
| pihole6-exporter | systemd | 9666 |
| Caddy | Docker | 443 |
| node-exporter | Docker | 9100 |
| fritz-exporter | Docker | 9787 |

## What is tracked here

### `caddy/` — active reverse proxy

Unlike Boreas, Caddy is **always running** here. It is the primary way to
access `.home` services from this location — browsers resolve `.home` names via
Pi-hole, which returns Zephyros's LAN IP, and Caddy proxies the request over
Tailscale to Mnemosyne (`100.x.x.x`).

```
Browser (remote LAN)
  → DNS: Pi-hole resolves *.home → Zephyros LAN IP
  → Caddy :443 (TLS, internal CA)
    ├── pihole.home  → 172.19.0.1:80  (Docker bridge → Pi-hole admin, local)
    ├── immich.home  → 100.x.x.x  (Tailscale direct — bypasses DNS chain)
    └── *.home       → https://<same hostname>.home  (resolved by Pi-hole → Mnemosyne Caddy)
```

`pihole.home` routes to `172.19.0.1` (Docker bridge gateway) instead of
`localhost` because Caddy runs in Docker and Pi-hole runs natively on the host.

`immich.home` is proxied directly to the Tailscale IP rather than via the
hostname because the DNS resolution chain would loop back through Caddy.

All upstream transports use `versions 1.1` — TLS 1.3 negotiation is unreliable
on the armv7 build of Caddy's Go runtime on this hardware.

```bash
cd caddy && docker compose up -d
```

### `monitoring/` — node-exporter + fritz-exporter

```bash
cp monitoring/.env.example monitoring/.env
# fill in FRITZ_HOSTNAME, FRITZ_USERNAME, FRITZ_PASSWORD
cd monitoring && docker compose up -d
```

`fritz-exporter` polls the local FritzBox router via TR-064 and exposes metrics
on `:9787`. Both exporters are scraped by Prometheus on Mnemosyne over Tailscale.

node-exporter here does **not** mount a `textfile_collector` volume — there are
no custom textfile metrics on this node.

### `pihole/pihole.toml`

A snapshot of `/etc/pihole/pihole.toml` checked in for reference. Notable
non-default settings:

| Setting | Value | Reason |
|---|---|---|
| `dns.upstreams` | `1.1.1.1`, `1.0.0.1` | Cloudflare upstream (Unbound used for local resolution only) |
| `dns.interface` | `eth0` | Explicit bind to wired interface |
| `webserver.port` | `80o` only | HTTPS handled by Caddy; Pi-hole admin runs plain HTTP |
| `dns.specialDomains.mozillaCanary` | `true` | Blocks Firefox DoH bypass |
| `dns.specialDomains.iCloudPrivateRelay` | `true` | Blocks iCloud Private Relay bypass |
| `dns.specialDomains.designatedResolver` | `true` | Blocks DDR discovery (RFC 9462) |

To update: copy the live file from the host and commit it as a reference snapshot.

### `scripts/backup-zephyros.sh`

Backs up Pi-hole, Unbound, Caddy, and misc configs to Mnemosyne via
Tailscale rsync. Run from cron on Zephyros.

```
Destination: youruser@100.x.x.x:/mnt/codex/backups/zephyros/<date>/
Retention:   30 daily snapshots (older ones deleted on Mnemosyne)
SSH key:     /home/youruser/.ssh/backup_key
```

What gets backed up: `/etc/pihole/pihole.toml`, `/etc/pihole/custom.list`,
`/etc/unbound/unbound.conf.d/pi-hole.conf`, `/etc/msmtprc`, `/etc/apticron/apticron.conf`,
`/etc/systemd/system/pihole6-exporter.service`, and the live Caddy stack.

## Network

```
Remote LAN (e.g. 192.168.x.0/24)
        │
   Zephyros :84 (Tailscale)
        ├── Pi-hole v6 (DNS :53)
        │       └── Unbound (local resolution :5335)
        ├── Caddy :443  ──── Tailscale ────▶ Mnemosyne :443
        ├── pihole6-exporter :9666  ◀── Prometheus (Mnemosyne, via Tailscale)
        ├── node-exporter   :9100  ◀── Prometheus (Mnemosyne, via Tailscale)
        └── fritz-exporter  :9787  ◀── Prometheus (Mnemosyne, via Tailscale)
```

DNS for remote LAN clients is set to Zephyros's LAN IP in the local FritzBox
DHCP config.
