# 3D-Printing

Monitoring and infrastructure integration for the Prusa MK4S (**Pygmalion**). Covers the PrusaLink exporter, authentication, the enclosure climate sensor, and the (planned) camera node. Material choice, slicing, and part-design notes live in the private vault, not here.

---

## Overview

| | |
|---|---|
| Printer | Original Prusa MK4S (Pygmalion) |
| IP | `192.168.1.x` |
| Firmware | Buddy / PrusaLink (local) — Prusa Connect (cloud) not used |
| Exporter | `prusa-exporter` (monitoring stack), port `9118` |
| Auth | HTTP Digest (`maker` + password) — see below |
| Dashboard | `01_12_Prusa_MK4S` |

The exporter is read-only: it scrapes `GET /api/v1/status` and never sends control commands. Print done/fail notifications are handled by the Prusa mobile app, so ntfy is intentionally **not** wired for the printer.

---

## PrusaLink authentication

The built-in PrusaLink web UI (MK4S) has **no Settings tab** — credentials are set on the printer: `Settings → Network → PrusaLink` (username `maker`, a 15-character password, an optional API key, and the printer IP).

Two auth methods exist; which one the `/api/v1/` endpoints accept depends on firmware. Verify before deploying:

```bash
# Option A: API key header
curl -s -H "X-Api-Key: $KEY" http://192.168.1.x/api/v1/status | jq

# Option B: HTTP Digest (maker + password)
curl -s --digest -u maker:$PASSWORD http://192.168.1.x/api/v1/status | jq
```

> On this MK4S only **Option B (Digest)** works — the API key header returns 401. The exporter is configured with `PRUSA_PASSWORD`, not `PRUSA_API_KEY`.

The password is printer-generated and cannot be freely chosen, only regenerated. Regenerating it on the printer requires updating the exporter env (see Credential rotation).

---

## Monitoring

The exporter runs in the `monitoring` stack and is scraped over the internal Docker network. Exposed metrics: `prusa_up`, `prusa_printer_state{state=...}` (enum), nozzle/bed temperatures (+ targets), job progress, time remaining, Z height, fan RPMs. On an unreachable printer it emits only `prusa_up 0`, so the dashboard shows a gap rather than stale values. See [[Monitoring]] for the scrape topology and dashboard list.

Secrets (`PRUSA_HOST`, `PRUSA_PASSWORD`) live in the monitoring stack `.env` and are never committed in plaintext; `.env.example` ships with empty placeholders.

---

## Room climate sensor (planned)

The MK4S is open-frame — no enclosure was bought (conscious scope cut; MMU3 and an enclosure only get revisited if a real ASA need shows up). There is therefore no chamber to measure.

What's planned instead is a Shelly H&T at the printer's basement location (Hephaestus' room — damp environment), scraped via the existing `shelly-exporter`, feeding Prometheus rules (`printer-environment`: humidity > 55 %/65 %, temp > 40 °C / < 15 °C, sensor liveness) into Alertmanager. This makes the location decision measurable, separate from the printer's own instrumentation.

**Status: not yet deployed.** Open items: place the Shelly H&T on-site, then wire up the `printer-environment` rules — neither exists in this repo yet.

---

## Runbook

### Verify exporter output

```bash
docker exec prusa-exporter python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9118/metrics').read().decode())" | grep -E '^prusa_'
```

Expect `prusa_up 1` and one `prusa_printer_state{...state="..."} 1`.

### Restart exporter

```bash
cd ~/stacks/monitoring
docker compose restart prusa-exporter
docker compose logs prusa-exporter -f --tail 30
```

### Credential rotation (after regenerating the PrusaLink password)

```bash
# 1. Printer: Settings → Network → PrusaLink → regenerate password
# 2. Update PRUSA_PASSWORD in the monitoring stack env (.env / .env.sops), then:
cd ~/stacks/monitoring
docker compose up -d prusa-exporter
# 3. Confirm
docker compose logs prusa-exporter --tail 20
```

Symptom of a stale password: `prusa_up 0` while the printer is reachable on the network.

### Verify scrape in Prometheus

```bash
curl -s http://localhost:9090/api/v1/targets \
  | python3 -c "
import sys, json
for t in json.load(sys.stdin)['data']['activeTargets']:
    if t['labels'].get('job') == 'prusa':
        print(t['labels'].get('instance'), t['health'], t.get('lastError',''))
"
```

---

## Camera node (planned)

A dedicated Pi (camera + `go2rtc` / `mjpg-streamer`) at the printer location, streamed over the LAN and reachable via `printcam.home` behind Caddy / Tailscale — independent of Prusa Connect (no cloud). PrusaLink on the MK4S board has no camera support, so a separate host is required. Will be added to the Hosts table and get its own stack folder when built.

---

## Related

- [[Monitoring]] — scrape topology, dashboards, alert rules
- [[Services]] — exporter port reference
- Material choice, filament storage, slicing, OpenSCAD part design: private vault (`22_Pygmalion`), not mirrored here.
