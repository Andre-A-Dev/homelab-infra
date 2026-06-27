# astraeus

x86_64 workstation (Windows 11), primary development machine. One of two SSDs
in the same physical machine — the other boots CachyOS and is tracked under
`astraeus_nx/`.

## Role in the homelab

- Primary workstation for development, Gitea access via Tailscale, and
  homelab management
- `windows_exporter` runs as a service on `:9182`, scraped by Prometheus on
  Mnemosyne (`windows` job, 15s interval)
- No Docker stacks — x86_64-only tooling lives here, not on the Pi nodes

## What is tracked here

| Path | Purpose |
|---|---|
| [`scripts/win_diag/`](scripts/win_diag/) | Windows system diagnostics and auto-fix tool |

Everything else (dev tools, IDE config, user data) is local to the machine and
not tracked in this repo.

## windows_exporter

Install from the [GitHub releases page](https://github.com/prometheus-community/windows_exporter/releases)
as an MSI. The installer registers a Windows service that starts automatically.

Default port: `:9182`. Verify:

```powershell
Invoke-RestMethod http://localhost:9182/metrics | Select-Object -First 5
```

No config is tracked here — the default collector set is sufficient. If
collectors are customized, add a `windows_exporter.yaml` to this directory.

## Caddy CA certificate

Import once to trust all `.home` domains in browsers. See the
[RUNBOOK — Import Caddy Root Certificate](../RUNBOOK.md#import-caddy-root-certificate-on-a-new-device)
for the Windows procedure.
