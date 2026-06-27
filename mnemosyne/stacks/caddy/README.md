# caddy

Reverse proxy and TLS termination for all Mnemosyne services. Uses Caddy's
built-in ACME client for Let's Encrypt (public domains) and an internal CA
(`tls internal`) for `.home` LAN-only domains.

**Must be started before any other stack** — it creates the external
`caddy_proxy` network that all other stacks join.

## TLS

| Domain pattern | Certificate source | Who needs to trust it |
|---|---|---|
| `*.yourdomain.dedyn.io` | Let's Encrypt (auto-renewed) | Everyone, already trusted |
| `*.home` | Caddy internal CA | LAN clients — import the CA cert manually |

The internal CA root certificate lives in `/mnt/vault/caddy/data/pki/`. To
trust `.home` domains on a LAN client, export it and add it to the browser/OS
trust store:

```bash
# On Mnemosyne
docker exec caddy cat /data/caddy/pki/authorities/local/root.crt > caddy-local-ca.crt
# Then import caddy-local-ca.crt on the client
```

## Reloading the Caddyfile

Caddy normally detects Caddyfile changes via inode watch. **Editors that do
atomic saves (VS Code, vim with `backupcopy=no`) swap the inode on write**,
breaking change detection. `caddy reload` will not pick up the new file.

Always use:

```bash
docker compose down && docker compose up -d
```

Never use `docker compose restart` (does not re-read `env_file`) or
`docker exec caddy caddy reload` after an atomic save.

## Route reference

| Hostname | Upstream | Notes |
|---|---|---|
| `vault.home` | `vaultwarden:80` | Forwards `X-Real-IP` |
| `cloud.yourdomain.dedyn.io` | `nextcloud:80` | HSTS; CalDAV/CardDAV redirects at `/.well-known`; `/push/*` → `notify_push:7867` |
| `git.home` | `gitea:3000` | |
| `syncthing.home` | `https://192.168.1.10:8384` | Proxied to host IP (Syncthing runs natively, not in Docker) |
| `calibre.home` | `calibre-web:8083` | |
| `kosync.home` | `kosync:3000` | |
| `grafana.home` | `grafana:3000` | |
| `prometheus.home` | `prometheus:9090` | |
| `alertmanager.home` | `alertmanager:9093` | |
| `viessmann.home` | `http://192.168.1.13:8081` | Bearer token injected via `{env.VIESSMANN_API_TOKEN}` (from `.env`) |
| `blog.yourdomain.dedyn.io` | `ghost:2368` | Public; Let's Encrypt |
| `ghostproxy.home` | `ghostproxy:5000` | |
| `ghostwrite.home` | `ghostwrite:5000` | |
| `homepage.home` | `homepage:3000` | |
| `immich.home` | `immich-server:2283` | |
| `http://100.x.x.x:2283` | `immich-server:2283` | Tailscale HTTP access — no TLS (Tailscale encrypts the tunnel) |
| `jobiris.home` | `jobiris-board:8042` | `flush_interval -1` for SSE streaming; `versions 1.1` |
| `wakapi.home` | `wakapi:3000` | |
| `weather.home` | `aether:8050` | |

## Non-obvious entries

**`viessmann.home`** — Caddy injects the Hephaestus API bearer token from
`{env.VIESSMANN_API_TOKEN}` so the token never appears in browser requests.
The env var is set in `.env` (see `.env.example`).

**`syncthing.home`** — proxied to the host IP (`192.168.1.10`) rather than a
container name because Syncthing runs natively on the host. The TLS cert on
Syncthing's side is self-signed, so `tls_insecure_skip_verify` is set.

**`immich.home` Tailscale entry** — plain HTTP on the Tailscale IP. Zephyros's
Caddy proxies `immich.home` directly to this address; Tailscale provides
transport encryption so a second TLS layer is unnecessary.

**`jobiris.home`** — `flush_interval -1` disables Caddy's response buffering,
required for the server-sent events (SSE) stream the board UI uses. Without it
the browser never receives updates.

## Data

TLS certificates and ACME state are persisted at `/mnt/vault/caddy/{data,config}`
so they survive container recreates. Losing this volume means re-issuing all
certificates — Let's Encrypt rate limits apply.
