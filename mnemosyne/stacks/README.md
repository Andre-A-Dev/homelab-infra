# mnemosyne/stacks

All Docker Compose stacks running on Mnemosyne. This directory is the target of
the `~/stacks/` symlink on the host, so compose files are always the live
checked-out working tree.

Stacks are deployed via Gitea webhook → selective `docker compose up -d` on
push. Never run `docker compose up/down/restart` manually unless explicitly
needed for a one-off operation.

---

## Stack index

| Stack | Purpose | Caddy hostname | Data path |
|---|---|---|---|
| [caddy](caddy/) | Reverse proxy, internal CA, TLS termination | — (is the proxy) | `/mnt/vault/caddy` |
| [vaultwarden](vaultwarden/) | Bitwarden-compatible password manager | `vault.home` | `/mnt/vault/vaultwarden` |
| [nextcloud](nextcloud/) | File sync + CalDAV/CardDAV | `cloud.yourdomain.dedyn.io` (public) | `/mnt/codex/nextcloud/{data,db}` |
| [gitea](gitea/) | Self-hosted Git | `git.home` | `/mnt/codex/gitea/data` |
| [gitea-runner](gitea-runner/) | Gitea Actions CI runner | — | — |
| [immich](immich/) | Photo library | `immich.home` | `UPLOAD_LOCATION`, `DB_DATA_LOCATION` (see `.env`) |
| [monitoring](monitoring/) | Prometheus + Grafana + Alertmanager + exporters | `grafana.home` `prometheus.home` `alertmanager.home` | `/mnt/codex/{prometheus,grafana,alertmanager}` |
| [calibre](calibre/) | Calibre-Web ebook library + KOSync read-progress sync | `calibre.home` `kosync.home` | `/mnt/codex/calibre-library` |
| [homepage](homepage/) | Homelab dashboard | `homepage.home` | — |
| [ghost](ghost/) | Blog | `blog.yourdomain.dedyn.io` (public) | — |
| [ghostproxy](ghostproxy/) | Ghost API proxy | `ghostproxy.home` | — |
| [ghostwrite](ghostwrite/) | Ghost writing interface | `ghostwrite.home` | — |
| [wakapi](wakapi/) | Coding time tracker | `wakapi.home` | — |
| [aether](aether/) | Weather console | `weather.home` | — |
| [jobiris](jobiris/) | Job board monitor | `jobiris.home` | — |
| [diun](diun/) | Container image update notifier | — | — |
| [solar](solar/) | FusionSolar exporter | — | — (inactive) |

---

## Non-obvious stack notes

**caddy** — must be started before any other stack because it creates the
external `caddy_proxy` network that all other stacks join. See
[caddy/README.md](caddy/README.md).

**nextcloud** — four containers: `nextcloud` (Apache), `nextcloud-db`
(MariaDB), `redis`, and `notify_push` (high-performance file change push).
`notify_push` requires the `notify_push` app to be installed inside Nextcloud.
Startup order is enforced via `condition: service_healthy` — do not remove the
healthchecks.

**gitea** — uses SQLite (not a separate DB container). SSH git is exposed on
port `2222` (not 22). HTTP port `3000` is bound to `127.0.0.1` only — the
webhook handler on the host reaches it via localhost, external access goes
through Caddy. Webhook allowed hosts include Boreas, Hephaestus, and Zephyros
by IP.

**immich** — requires `tensorchord/pgvecto-rs` (not plain Postgres) for vector
similarity search used by face recognition and CLIP. The DB service name must
be `database` — Immich constructs `DATABASE_URL` expecting that hostname.
ML model cache is a named volume (`immich-model-cache`, ~1 GB) so models
survive container recreates. Version is pinned via `IMMICH_VERSION` in `.env`
— Immich breaks on minor version mismatches between server and ML containers.

**solar** — currently inactive (FusionSolar integration not in use).

**diun** — no persistent state; just polls the Docker socket and sends
notifications when image digests change. Complements
`mnemosyne/scripts/container-update-metrics.sh` (which writes Prometheus
metrics) by sending push notifications.

---

## `caddy_proxy` network

The shared external network that connects Caddy to all proxied services.
Create it once before first startup:

```bash
docker network create caddy_proxy
```

Caddy must be running for any `.home` service to be reachable.
