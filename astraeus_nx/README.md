# astraeus_nx

Same physical machine as `astraeus/`, second SSD booting CachyOS (Arch-based
Linux). Currently a Linux playground and the planned migration target if the
primary environment moves away from Windows.

## What is tracked here

| Path | Purpose |
|---|---|
| [`dotfiles/ssh_config`](dotfiles/ssh_config) | SSH client config covering all homelab hosts |

## SSH config

Symlink into place on the CachyOS install:

```bash
mkdir -p ~/.ssh
ln -sf ~/homelab-infra/astraeus_nx/dotfiles/ssh_config ~/.ssh/config
chmod 600 ~/.ssh/config
```

Covers:
- `mnemosyne` / `boreas` — LAN hosts by IP
- `git.home` — Gitea SSH on port 2222 via Mnemosyne
- `mnemosyne-ts` / `boreas-ts` — Tailscale FQDN entries
- `zephyros` — Tailscale IP (remote node, no LAN access)

## Caddy CA certificate

CachyOS uses the system trust store. Import the Caddy internal CA root once to
make `.home` domains trusted in browsers:

```bash
# Export the cert from the running Caddy container on Mnemosyne
ssh mnemosyne "docker exec caddy cat /data/caddy/pki/authorities/local/root.crt" \
  > ~/caddy-local-ca.crt

# Add to system trust store
sudo trust anchor --store ~/caddy-local-ca.crt
sudo update-ca-trust
```

Electron apps (VSCodium, Bitwarden desktop) ignore the system CA and need a
separate NSS database entry:

```bash
certutil -d sql:$HOME/.pki/nssdb \
  -A -t "CT,," -n "Caddy Local CA" -i ~/caddy-local-ca.crt
```

Run this for each user profile that runs Electron apps.
