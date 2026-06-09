# Update Notifications

No auto-updates. The philosophy is: get notified, review the changelog, update deliberately. A silent breaking update to Vaultwarden or Caddy at 2 AM is not acceptable.

Two tools handle this:

- **Diun** — monitors Docker images, sends a push notification when a new image is available
- **apticron** — monitors apt packages, sends an email when system updates are available

---

## Architecture

```
Mnemosyne:
  new Docker image   ->  Diun  ->  ntfy (push)  ->  Android
  apt packages       ->  apticron  ->  email

Boreas / Zephyros:
  apt packages       ->  apticron  ->  email
  (no Docker -> no Diun)
```

---

## Diun — Docker image update notifications

Diun watches all running containers and checks their registries for newer images. When a new image is found, it sends a notification via ntfy.

The watch schedule is set to weekly (Monday 08:00) — daily would generate too much noise.

`WATCHBYDEFAULT=true` covers all containers automatically without needing to label each one individually.

### docker-compose.yml

```yaml
services:
  diun:
    image: crazymax/diun:latest
    container_name: diun
    volumes:
      - diun-data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TZ=Europe/Berlin
      - DIUN_WATCH_WORKERS=20
      - DIUN_WATCH_SCHEDULE=0 8 * * 1        # Mondays at 08:00
      - DIUN_PROVIDERS_DOCKER=true
      - DIUN_PROVIDERS_DOCKER_WATCHBYDEFAULT=true
      - DIUN_NOTIF_NTFY_ENDPOINT=https://ntfy.sh
      - DIUN_NOTIF_NTFY_TOPIC=${NTFY_DIUN_TOPIC}   # set in .env
    restart: unless-stopped

volumes:
  diun-data:
```

### Useful commands

```bash
# Check current image state
docker exec diun diun image list

# Send a test notification immediately
docker exec diun diun notif test

# Trigger an immediate check of all images
docker exec diun diun image update

# Logs
docker logs diun -f
```

---

## ntfy — push notifications

ntfy is a minimal pub/sub push service. Sending a notification is a single `curl` call. The Android app receives it like a normal system push notification.

The public `ntfy.sh` instance works without any setup. The topic name acts as a password — treat it like one. Use a random string, not something guessable like `mnemosyne-updates`.

```bash
# Send a test notification
curl -d "Test from Mnemosyne" https://ntfy.sh/your-topic-name

# With title and priority
curl \
  -H "Title: Mnemosyne Update" \
  -H "Priority: default" \
  -d "New Docker images available" \
  https://ntfy.sh/your-topic-name
```

**Android:** install the ntfy app (Play Store or F-Droid) and subscribe to your topic. Disable battery optimization for the app to ensure reliable delivery.

**Self-hosting:** ntfy can run as a Docker container on Mnemosyne for a fully private setup. Add it to the Diun stack and point `DIUN_NOTIF_NTFY_ENDPOINT` to the internal URL. Requires Tailscale or a reverse proxy for external delivery.

---

## apticron — system package update notifications

apticron runs daily, checks for available apt upgrades, and sends an email with the full package list and changelogs when updates are found. No output means no updates — not an error.

### Installation

```bash
sudo apt install apticron msmtp msmtp-mta -y
```

### apticron configuration

```bash
sudo nano /etc/apticron/apticron.conf
```

```ini
EMAIL="your@email.com"
NOTIFY_NO_UPDATES="0"    # only notify when updates are available
```

### msmtp — SMTP relay

apticron uses the system MTA to send mail. `msmtp` is the lightest option, forwarding via an external SMTP account.

Create `~/.msmtprc`:

```ini
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
logfile        /var/log/msmtp.log

account        smtp
host           smtp.example.com
port           587
from           your@email.com
user           your@email.com
password       your-app-password

account default : smtp
```

```bash
chmod 600 ~/.msmtprc                        # required — msmtp refuses to run otherwise

sudo touch /var/log/msmtp.log
sudo chown $(whoami):$(whoami) /var/log/msmtp.log

# apticron runs as root — it needs its own copy
sudo cp ~/.msmtprc /etc/msmtprc
sudo chmod 600 /etc/msmtprc

# Test
echo "Test from Mnemosyne" | msmtp your@email.com
sudo apticron    # manual run — sends mail only if updates are available
```

If using Gmail: a regular password is rejected. Enable 2FA and generate an App Password at `myaccount.google.com/apppasswords`. Use that 16-character code in `password`.

---

## Applying updates

When apticron sends a mail or Diun sends a push notification, apply updates manually:

**System packages:**

```bash
sudo apt update
apt list --upgradable 2>/dev/null   # review what changes
sudo apt dist-upgrade -y            # dist-upgrade resolves new dependencies; upgrade skips them
sudo reboot                         # required after kernel updates
```

**Docker images:**

```bash
cd ~/stacks/<stack>
docker compose pull
docker compose up -d
docker image prune -f
```

See the [Runbook](Runbook) for the full update procedure per stack.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Diun sends no notification | Wrong topic name | Check `DIUN_NOTIF_NTFY_TOPIC` in `.env` |
| Diun finds no updates despite new image | `latest` tag cached | `docker exec diun diun image list` shows current state |
| apticron sends no mail | No updates available | `apt list --upgradable` — empty list is correct |
| apticron sends no mail | msmtp not configured | Send a test mail manually: `echo test \| msmtp your@email.com` |
| msmtp: `authentication failed` | Wrong password or not an app password | Generate an app password, not the account password |
| msmtp: `contains secrets and therefore must have...` | File permissions too open | `chmod 600 ~/.msmtprc` |
| ntfy app receives nothing | Battery optimization active | Disable battery optimization for the ntfy app |
