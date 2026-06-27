# Caddy

Caddy is the reverse proxy for all services on Mnemosyne and the secondary proxy on Zephyros. It handles TLS automatically -- Let's Encrypt for public domains, an internal CA for `.home` domains.

---

## Why Caddy

- **Automatic TLS** -- no manual certificate management, no Certbot cron jobs
- **Internal CA** -- issues trusted certificates for `.home` domains without self-signed warnings, after a one-time root certificate import per client
- **Minimal config** -- adding a service is a three-line block; no YAML anchors, no label-based magic
- **In-place reload** -- config changes take effect without restarting containers or dropping connections

---

## Two instances

| Instance | Host | Port | Role |
|---|---|---|---|
| Mnemosyne | Mnemosyne | `80`, `443` | Primary reverse proxy + internal CA |
| Zephyros | Zephyros | `443` | Forward proxy → Mnemosyne via Tailscale |

The instances are independent. Each has its own CA and issues its own certificates. Clients on the remote network import the Zephyros CA; clients on the home network import the Mnemosyne CA.

---

## Mnemosyne Caddy

### Network architecture

Caddy owns the `caddy_proxy` Docker network. Every service that should be reachable through Caddy joins this network. Caddy resolves container names directly -- no IP addresses needed in the Caddyfile.

```
Client → Caddy :443 → container-name:port (via caddy_proxy network)
```

Services that run outside Docker (Syncthing as systemd) are reached via `host.docker.internal`, which resolves to the host IP via the `extra_hosts` setting in the compose file.

### Caddyfile patterns

**Standard internal service:**

```caddy
service.home {
  tls internal
  reverse_proxy container-name:port
}
```

**Public service (Let's Encrypt):**

```caddy
cloud.yourdomain.dedyn.io {
  reverse_proxy container-name:port
}
```

No `tls` directive needed -- Caddy requests a Let's Encrypt certificate automatically for any domain it can verify via HTTP-01 or DNS-01.

**Forwarding a real IP (Vaultwarden):**

```caddy
vault.home {
  tls internal
  reverse_proxy vaultwarden:80 {
    header_up X-Real-IP {remote_host}
  }
}
```

Vaultwarden logs the client IP, not Caddy's internal IP.

**Token injection (Viessmann API):**

```caddy
viessmann.home {
  tls internal
  reverse_proxy http://192.168.1.13:8081 {
    header_up Authorization "Bearer {env.VIESSMANN_API_TOKEN}"
  }
}
```

The token lives in Caddy's `.env`, never in the browser or in Grafana panel HTML.

**Streaming / SSE services:**

```caddy
jobiris.home {
  tls internal
  reverse_proxy jobiris-board:8042 {
    flush_interval -1
    transport http {
      versions 1.1
    }
  }
}
```

`flush_interval -1` disables response buffering for server-sent events. `versions 1.1` forces HTTP/1.1 for services that do not support HTTP/2.

**Tailscale-only HTTP access (no TLS):**

```caddy
http://100.x.x.x:2283 {
  reverse_proxy immich-server:2283
}
```

For clients that access via Tailscale IP directly. Tailscale encrypts the tunnel; the inner connection is plain HTTP.

### Data paths

| What | Path |
|---|---|
| TLS certificates + CA | `/mnt/vault/caddy/data/` |
| Caddy config cache | `/mnt/vault/caddy/config/` |
| Caddyfile | `~/stacks/caddy/Caddyfile` |

---

## Adding a new internal service

1. **Add a block to the Caddyfile** (`~/stacks/caddy/Caddyfile`):

```caddy
service.home {
  tls internal
  reverse_proxy <container-name>:<port>
}
```

2. **Join the `caddy_proxy` network** in the service's `docker-compose.yml`:

```yaml
networks:
  caddy_proxy:
    external: true
```

And add the network to the service itself:

```yaml
services:
  myservice:
    ...
    networks:
      - caddy_proxy
```

3. **Add a DNS record** in Pi-hole: **Local DNS → DNS Records** → `service.home` → `192.168.1.10`

4. **Apply the Caddyfile change:**

```bash
cd ~/stacks/caddy && docker compose down && docker compose up -d
```

> **Do not use `caddy reload` or `docker compose restart`.** Editors save atomically (write new file + rename), which swaps the inode and breaks change detection. Only `down && up` reliably picks up the new file.

---

## Internal CA

Caddy acts as its own certificate authority for `.home` domains. On the first start it generates a root CA stored in `/mnt/vault/caddy/data/caddy/pki/authorities/local/`.

All `.home` certificates are signed by this CA. Browsers and OS certificate stores trust them after a one-time import of the root certificate.

### Extract the root certificate

```bash
docker cp caddy:/data/caddy/pki/authorities/local/root.crt ~/caddy-root.crt
```

### Import per platform

**Windows:**
Double-click `caddy-root.crt` → Install Certificate → Local Machine → Trusted Root Certification Authorities

**Android:**
Copy via Syncthing or ADB → Settings → Security → Install certificate → CA certificate

> Use Brave or Chrome for `.home` access -- Firefox on Android uses its own certificate store and ignores the system CA.

**CachyOS / Arch:**
```bash
sudo trust anchor --store caddy-root.crt
update-ca-trust
```

Electron apps (VSCodium, Bitwarden desktop) ignore the system CA. Add to the NSS database:

```bash
certutil -d sql:$HOME/.pki/nssdb -A -t "CT,," -n "Caddy Local CA" -i ~/caddy-root.crt
```

---

## Zephyros Caddy

Zephyros runs a second Caddy instance as a lightweight forward proxy. Devices on the remote network without Tailscale can access `.home` services by importing the Zephyros CA certificate once.

```
Remote device → Zephyros Caddy :443 → Mnemosyne Caddy (via Tailscale 100.x.x.x)
```

### Key differences from Mnemosyne

| | Mnemosyne | Zephyros |
|---|---|---|
| Ports | `80`, `443` | `443` only (Pi-hole owns `80`) |
| `auto_https` | default | `disable_redirects` |
| Network | `caddy_proxy` (shared with services) | standalone bridge |
| Data path | `/mnt/vault/caddy/` (SSD) | `./data/` (local to repo) |
| TLS backend | own internal CA | `tls_insecure_skip_verify` (Mnemosyne CA not trusted) |

### Why `tls_insecure_skip_verify`

Zephyros forwards requests to `https://service.home` on Mnemosyne. Mnemosyne's Caddy presents a certificate signed by the Mnemosyne CA. Zephyros does not import that CA, so verification would fail. `tls_insecure_skip_verify` skips it -- the connection is still encrypted, just not verified by the intermediate hop.

The end-to-end trust is provided by the Zephyros CA on the client side, not by this hop.

### Caddyfile pattern (Zephyros)

```caddy
ghostwrite.home {
  tls internal
  reverse_proxy https://ghostwrite.home {
    header_up Host ghostwrite.home
    transport http {
      tls_insecure_skip_verify
      versions 1.1
    }
  }
}
```

The `header_up Host` is required -- Mnemosyne's Caddy matches the virtual host by the `Host` header. Without it, the request arrives with the wrong host and is rejected.

---

## Useful commands

```bash
# Validate the Caddyfile without applying
docker exec caddy caddy validate --config /etc/caddy/Caddyfile

# Check which certificates are managed
docker exec caddy caddy list-modules

# View certificate info
docker exec caddy caddy environ

# Caddy logs
docker compose -f ~/stacks/caddy/docker-compose.yml logs -f --tail 50

# Check which containers are on the proxy network
docker network inspect caddy_proxy
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `.home` domain not trusted | Root CA not imported | Import `caddy-root.crt` on the client (see above) |
| Config change not picked up after restart | Atomic save swapped the inode | Use `docker compose down && up`, not `restart` |
| `upstream dial error` | Service container not on `caddy_proxy` network | Add `caddy_proxy` to the service's `networks:` |
| `no upstream` for a new service | Compose not restarted after adding network | `docker compose up -d` in the service stack |
| Firefox on Android doesn't trust `.home` | Firefox uses own cert store | Use Brave or Chrome |
| Zephyros proxy returns 502 | Mnemosyne unreachable via Tailscale | `tailscale ping 100.x.x.x` from Zephyros |
| Streaming response buffered (SSE broken) | Missing `flush_interval -1` | Add to the relevant reverse_proxy block |
