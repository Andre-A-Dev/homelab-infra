# Pi-hole + Unbound

Network-wide DNS filtering and privacy via recursive resolution -- without sending every DNS query to Google or Cloudflare.

Pi-hole filters ads and tracking domains for all devices on the network. Unbound resolves DNS queries recursively by querying the authoritative nameservers directly, with no third-party intermediary.

**Boreas is DNS-critical.** If it goes down, the entire network loses DNS resolution. Keep the emergency rollback procedure in mind before making changes.

---

## How it works

```
Device makes DNS request
        │
        ▼
Pi-hole (port 53)
  ├── blocked domain? → return 0.0.0.0, done
  └── not blocked?
        │
        ▼
Unbound (127.0.0.1:5335)
  └── recursive resolution against root nameservers
        │
        ▼
Authoritative nameserver (no Google, no Cloudflare)
```

Without Unbound, Pi-hole forwards to an upstream resolver that sees every query. Unbound eliminates this: it walks the DNS tree from the root down to the authoritative nameserver, with no intermediary.

---

## Installation

### Pi-hole

```bash
curl -sSL https://install.pi-hole.net | bash
```

The installer walks through an interactive menu. Set the upstream DNS to Cloudflare (1.1.1.1) for now -- it will be replaced by Unbound in the next step. Enable the web interface and logging.

```bash
# Pi-hole v6 -- set admin password
pihole setpassword
```

### Unbound

```bash
sudo apt install unbound -y
```

Create `/etc/unbound/unbound.conf.d/pi-hole.conf`:

```yaml
server:
    verbosity: 0
    interface: 127.0.0.1
    port: 5335
    do-ip4: yes
    do-udp: yes
    do-tcp: yes
    do-ip6: no

    # Keep root hints current
    root-hints: "/var/lib/unbound/root.hints"

    # Security
    harden-glue: yes
    harden-dnssec-stripped: yes
    use-caps-for-id: no

    # Performance
    edns-buffer-size: 1232
    prefetch: yes
    num-threads: 1
    cache-min-ttl: 3600
    cache-max-ttl: 86400

    # Resolve router hostname locally
    local-zone: "fritz.box." static
    local-data: "fritz.box. IN A 192.168.1.1"
```

Download root hints and start Unbound:

```bash
wget -O /var/lib/unbound/root.hints https://www.internic.net/domain/named.cache
sudo chown unbound:unbound /var/lib/unbound/root.hints

sudo systemctl enable unbound
sudo systemctl start unbound

# Verify
dig pi-hole.net @127.0.0.1 -p 5335
# Expected: NOERROR with an IP address
```

### Point Pi-hole at Unbound

Pi-hole dashboard → **Settings → DNS**:
- Disable all existing upstream servers
- Set Custom DNS 1: `127.0.0.1#5335`
- Save

### Keep root hints current

Add a monthly cron job:

```bash
sudo crontab -e
```

```
0 3 1 * * wget -O /var/lib/unbound/root.hints https://www.internic.net/domain/named.cache && systemctl restart unbound
```

---

## Router configuration

### IPv4 DNS

Point the router's DHCP DNS server at the Pi-hole host IP. All devices on the network will use Pi-hole automatically without any per-device configuration.

### IPv6 DNS

Windows and Android prefer IPv6. If the router advertises an IPv6 DNS server via Router Advertisement, it wins over IPv4 -- even if IPv4 is correctly pointing at Pi-hole. Without this step, IPv6 traffic bypasses Pi-hole entirely.

Assign a static IPv6 address to the Pi-hole host and configure the router to announce it as the IPv6 DNS server.

### DNS rebind protection

If your router has DNS rebind protection enabled (FritzBox does by default), add an exception for your external domain. Without this, Pi-hole's local DNS override for your domain will be blocked.

Add your domain (e.g. `yourdomain.dedyn.io`) to the DNS rebind exception list in the router settings.

---

## Local DNS records

Pi-hole can resolve internal hostnames and override public DNS. Add records under **Local DNS → DNS Records**:

| Domain | IP | Purpose |
|---|---|---|
| `mnemosyne.local` | `192.168.1.10` | Primary server hostname |
| `vault.home` | `192.168.1.10` | Vaultwarden |
| `git.home` | `192.168.1.10` | Gitea |
| `grafana.home` | `192.168.1.10` | Grafana |
| `cloud.yourdomain.dedyn.io` | `192.168.1.10` | Nextcloud (hairpin NAT workaround) |

The hairpin NAT override is particularly important: without it, devices on the home network trying to reach `cloud.yourdomain.dedyn.io` would hit the router instead of Mnemosyne, because the public IP resolves back to the same network.

---

## Recommended blocklists

Pi-hole ships with a default list. Two additions worth adding via **Pi-hole dashboard → Adlists → Add**:

| List | URL |
|---|---|
| Steven Black (ads + malware) | `https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts` |
| oisd (big) | `https://big.oisd.nl` |

---

## Useful commands

```bash
# Pi-hole
pihole status
pihole -c                          # statistics
pihole -g                          # update blocklists
sudo systemctl restart pihole-FTL

# Unbound
sudo systemctl status unbound
dig google.com @127.0.0.1 -p 5335  # test recursive resolution
journalctl -u unbound -f

# Verify DNS is working from a client
nslookup google.com                 # should show Pi-hole IP as server
dig vault.home @192.168.1.11        # should resolve to 192.168.1.10
```

---

## Emergency rollback

If the Pi-hole host goes down and the network loses DNS resolution, restore DNS via the router directly.

**The router admin interface is always reachable by IP** (e.g. `192.168.1.1`) -- it does not depend on DNS.

### IPv4

Router admin → **Network settings → Local DNS server** → clear the field → Apply

### IPv6

Router admin → **Network → IPv6** → clear the local DNS field → Reset → Apply

### Windows (if DNS was set manually)

```cmd
netsh interface ipv4 set dns "Ethernet" dhcp
ipconfig /flushdns
ipconfig /release
ipconfig /renew
```

Verify:
```cmd
nslookup google.com
# Server should show the router's IP or hostname
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Entire network loses DNS | Pi-hole host down | Emergency rollback (see above) |
| Pi-hole dashboard not reachable | Service stopped | `sudo systemctl restart pihole-FTL` |
| Unbound not responding | Config error | `sudo systemctl status unbound`, `journalctl -u unbound` |
| Router hostname not resolving | Missing local-zone entry | Add `local-zone` and `local-data` to Unbound config |
| External domain points to router | DNS rebind protection active | Add domain exception in router settings |
| IPv6 traffic bypasses Pi-hole | IPv6 DNS not configured | Set static IPv6 on Pi-hole host, configure router IPv6 DNS |
| Local DNS records lost after restore | `custom.list` missing from backup | Re-add records manually in Pi-hole dashboard |
