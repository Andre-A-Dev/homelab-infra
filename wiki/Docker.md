# Docker

## Why Docker Compose?

Every service runs on the same host. Without containers, they share the same runtime environment - and that causes problems over time.

**Dependency conflicts.** Nextcloud requires PHP 8.2. Another service requires an older version. Both need to run on the same system.

**Global state.** A broken update to one service can destabilize others. There is no isolation boundary.

**No reproducible recovery.** If the hardware fails, every service has to be reinstalled manually - with no guarantee of the same result.

Docker Compose solves all three. Each service runs in its own container with exactly the dependencies it needs. The entire infrastructure is declared in `docker-compose.yml` files. A new Pi, a backup restore, and `docker compose up -d` - done.

```yaml
# Every service is explicitly declared - no hidden system state
services:
  vaultwarden:
    image: vaultwarden/server:latest
    restart: unless-stopped
    volumes:
      - /mnt/vault/vaultwarden/data:/data
```

Native `apt` installations leave config files, systemd units, and dependencies scattered across the system that are hard to remove cleanly. Docker containers are self-contained.

### When Docker is not the right choice

Not every service belongs in a container. Complexity without benefit is an anti-pattern.

| Service | Why not Docker? |
|---|---|
| `pihole6-exporter` (Boreas) | Pi-hole itself runs natively - a container adds no benefit here |
| Syncthing (Mnemosyne) | Runs as a systemd service; deep filesystem integration is simpler without a container boundary |
| Node Exporter (Hephaestus) | Technically in Docker, but borderline for a single exporter on a minimal host |

**Rule of thumb:** Docker is worth it when a service has its own dependencies, gets updated regularly, or needs to be reproducible. For simple, stable system tools, systemd is often cleaner.

### Summary

| Criterion | Native install | Docker Compose |
|---|---|---|
| Isolation | Shared runtime | Per container |
| Dependency conflicts | Possible | Eliminated |
| Recovery | Manual, error-prone | Declarative, reproducible |
| Version control | System state not versionable | YAML in Git |
| Debugging | Scattered logs and config | `docker logs`, `docker inspect` |
| Overhead | Minimal | Slightly higher |

---

## Stack layout

All stacks live under `~/stacks/<service>/` with their own `docker-compose.yml`. `~/stacks/` is a symlink to `~/homelab-infra/mnemosyne/stacks/` - one copy, no duplication.

```bash
cd ~/stacks/<stack>
docker compose up -d
```

---

## Command reference

### Container management

```bash
docker start <container>        # start container
docker stop <container>         # graceful shutdown (SIGTERM -> SIGKILL after timeout)
docker restart <container>      # restart container
docker kill <container>         # immediate shutdown (SIGKILL)
```

### Status and logs

```bash
docker ps                       # show running containers
docker ps -a                    # show all containers (incl. stopped)
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

docker logs <container>         # print logs
docker logs -f <container>      # follow logs in real time
docker logs --tail 100 <container>
docker logs --since 1h <container>
```

### Inspection and shell

```bash
docker inspect <container>      # full container config (JSON)
docker exec -it <container> sh  # open shell (sh)
docker exec -it <container> bash
docker top <container>          # running processes inside container
docker stats                    # live resource usage for all containers
docker stats --no-stream        # single snapshot
```

Always specify `--user` when running commands inside service containers. Running as root can corrupt file ownership in volumes:

```bash
docker compose exec --user www-data nextcloud php occ <command>
```

### Docker Compose - stack operations

```bash
docker compose up -d            # start stack (detached)
docker compose down             # stop stack and remove containers
docker compose down -v          # stop + delete named volumes data loss
docker compose restart          # restart all containers in the stack
docker compose ps               # show containers of the stack
docker compose logs -f          # follow logs in real time
docker compose logs -f <svc>    # follow logs of a single service
docker compose config           # show merged config (incl. .env substitution)
docker compose config --quiet   # check for errors only
```

### Images

```bash
docker images                   # list local images
docker pull <image>:<tag>       # download image
docker rmi <image>              # delete image
docker image prune              # delete untagged (dangling) images
docker image prune -a           # delete all unused images
```

### Volumes

```bash
docker volume ls                # list all volumes
docker volume inspect <volume>  # volume details (mountpoint etc.)
docker volume rm <volume>       # delete volume
docker volume prune             # delete all unused volumes
```

Named volumes live under `/var/lib/docker/volumes/<name>/_data/`. To back them up without stopping the service, use an Alpine container workaround - see [Backup Strategy](Backup-Strategy).

### Networks

```bash
docker network ls
docker network inspect <network>
docker network inspect caddy_proxy    # check which containers are on the proxy network
```

### Cleanup

```bash
docker system prune             # remove stopped containers, networks, dangling images
docker system prune -a          # remove everything unused (incl. images)
docker system df                # show Docker disk usage
```

`prune -a --volumes` deletes all volumes not actively mounted - including data volumes from stopped containers. Run `docker system df -v` first to review what would be removed.

---

## Updates

No auto-updates. Diun sends a notification when a new image is available. Updates are applied manually after reviewing the changelog.

```bash
cd ~/stacks/<stack>
docker compose pull
docker compose up -d
docker image prune -f
```

Update Caddy last - other stacks depend on the `caddy_proxy` network it creates.

---

## Troubleshooting

### Container won't start

```bash
docker compose logs <service>       # look for error message
docker compose config               # .env parsed correctly? syntax ok?
docker inspect <container>          # check volumes, networks, env vars
ls -la ~/stacks/<stack>/.env        # .env present and readable?
docker network inspect caddy_proxy  # network exists?
```

### DB not reachable at startup

Symptom: `SQLSTATE[HY000] [2002] Connection refused` in the app log, even though the DB container is running.

Cause: the app container started before the database was ready to accept connections.

```bash
# Quick fix: restart the app container after DB is fully ready
docker compose restart <app-service>

# Check if DB is reachable from inside the app container
docker compose exec <app-service> bash -c \
  "cat /dev/null > /dev/tcp/<db-service>/3306 && echo 'reachable' || echo 'unreachable'"
```

Permanent fix: add a `healthcheck` to the DB service and a `depends_on: condition: service_healthy` to the app service in `docker-compose.yml`.

### Layer cache corruption after unclean reboot

Symptom: containers exit with code 255, `RWLayer ... is unexpectedly nil` in logs.

```bash
docker compose down && docker compose up -d

# If that fails:
docker system prune -af
docker compose pull
docker compose up -d
```
