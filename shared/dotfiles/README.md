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
- `mnemosyne` / `boreas` / `hephaestus` / `daidalos` — LAN hosts by IP
- `git.home` — Gitea SSH on port 2222 via Mnemosyne
- `mnemosyne-ts` / `boreas-ts` / `hephaestus-ts` / `daidalos-ts` — Tailscale FQDN entries
- `zephyros` — Tailscale IP (remote node, no LAN access)
