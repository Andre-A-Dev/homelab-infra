# Boreas

Raspberry Pi 3B at **192.168.1.11**, acting as the home network's DNS server
and monitoring satellite. The name is tracked in Prometheus as `boreas`.

## Role

| Service | How | Port |
|---|---|---|
| Pi-hole v6 | native install | 80 / 443 (admin) |
| Unbound | native install | 5335 (upstream for Pi-hole) |
| pihole6-exporter | systemd | 9666 |
| node-exporter | Docker | 9100 |
| Caddy | Docker (standby) | 443 |

Pi-hole and Unbound are installed natively — nothing in this directory manages
them. This repo only tracks the exporters, Caddy config, and msmtp config.

## What is tracked here

### `monitoring/` — node-exporter

Standard node-exporter container with `textfile_collector` support, scraped by
Prometheus on Mnemosyne at `192.168.1.11:9100`.

```bash
cd monitoring && docker compose up -d
```

### `pihole6-exporter/` — Pi-hole v6 Prometheus exporter

A systemd-managed exporter that talks to Pi-hole v6's REST API and exposes
metrics on port `9666`. Pi-hole v6 replaced the old PHP `/admin/api.php`
endpoint with an authenticated REST API, so the older pihole-exporter images
don't work.

Create `/etc/pihole6-exporter.env` from the example:

```bash
cp pihole6-exporter/pihole6-exporter.env.example /etc/pihole6-exporter.env
# fill in PIHOLE_HOST, PIHOLE_PASSWORD, PIHOLE_PORT
```

The systemd unit (installed separately) reads this env file. Prometheus scrapes
`:9666` with a 30 s interval and 25 s timeout (set in Mnemosyne's
`prometheus.yml`).

Alerts defined in `mnemosyne/stacks/monitoring/prometheus/alerts.yml`:
- `PiholeDown` — exporter unreachable
- `PiholeHighBlockRate` — block rate > 50 % (possible DNS hijack or misconfigured list)

### `caddy/` — standby reverse proxy

**Not currently running.** This Caddyfile is a contingency config for the
scenario where Mnemosyne moves to a different network. When active, Boreas
acts as a Tailscale subnet router and Caddy proxies `.home` domains through to
Mnemosyne via its Tailscale IP (`100.x.x.x`).

Prerequisites before activating (documented in the Caddyfile header):
1. Disable Pi-hole's built-in port 443
2. Set Pi-hole DNS records: all `.home` → `192.168.1.11`
3. Enable Tailscale subnet router on Boreas (`192.168.1.x/24`)
4. Change the remote FritzBox subnet to `192.168.179.0/24` to avoid collision

### `msmtprc` — email relay

msmtp config for outgoing mail from Pi-hole (alerts, weekly digests). Uses
Gmail SMTP on port 587. The actual credentials live in `/etc/msmtprc` on the
host (not committed).

```bash
# Deploy:
sudo cp msmtprc /etc/msmtprc
sudo chmod 600 /etc/msmtprc
# Fill in the password field
```

## Network

```
LAN 192.168.1.x/24
        │
   Boreas :78
        ├── Pi-hole v6 (DNS :53)
        │       └── Unbound (upstream :5335)
        ├── pihole6-exporter :9666  ←── Prometheus (Mnemosyne)
        └── node-exporter   :9100  ←── Prometheus (Mnemosyne)
```

DNS for all LAN clients is set to `192.168.1.11` in the FritzBox DHCP config.
