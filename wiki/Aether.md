# Aether

A dedicated weather console for **Home** — Netatmo, Tado, and Shelly readings
plus a 5-day forecast, at `weather.home`. A calm, glanceable alternative to a
Grafana dashboard. Full sensor-catalog and setup detail lives in
`mnemosyne/stacks/aether/README.md`; this page covers the parts worth knowing
at a glance and the operational side.

---

## Overview

| | |
|---|---|
| URL | `https://weather.home` |
| Container | `aether` → `aether:8050` (Caddy) |
| Networks | `caddy_proxy` (Caddy → aether), `monitoring` (aether → Prometheus) |
| Storage | none — queries Prometheus live, stores nothing |
| Scope | Home only — Fuchsbau sensors are deliberately excluded |
| Dashboard config | `mnemosyne/stacks/aether/sensors.yaml` |

Aether is a presentation layer, not a new integration: Netatmo, Tado, and
Shelly are already scraped once each by their own exporters (see
[[Monitoring]]). Aether just queries the same Prometheus. See [[Architecture]]
for the full rationale (why not Home Assistant, why sensors are config).

---

## Sensor catalog

Every tile is a PromQL expression in `sensors.yaml` — no Python change, no
rebuild needed to add a sensor or room. Indoor sensors render as one
horizontally scrollable "rooms" strip in catalog order.

```bash
cd ~/stacks/aether
nano sensors.yaml
docker compose restart aether
```

Discover an exporter's real metric names before wiring up a new sensor:

```bash
curl -s http://prometheus:9090/api/v1/label/__name__/values | tr ',' '\n' | grep -i shelly
```

---

## Forecast and radar (external calls)

| Feature | Source | Notes |
|---|---|---|
| Forecast | Open-Meteo | No key, cached 20 min, DWD ICON for Europe |
| Radar (optional) | DWD GeoServer WMS (default) or RainViewer | Lazy-loaded — nothing fetched until "Show radar" is clicked |

The radar map is the one feature that isn't local-first: while open, the
browser streams tiles from OpenStreetMap and the chosen radar provider,
which can leak IP/location like any web map. Toggle it off via
`settings.map.radar: false` in `sensors.yaml` if that tradeoff isn't wanted.

**Privacy note — coordinates.** `settings.location` in `sensors.yaml` holds
real (town-centre) latitude/longitude for the forecast. `export_public.py`
does **not** currently redact this file — the coordinates would pass through
into the public mirror as-is. Treat `sensors.yaml` as sensitive until that
export rule exists; don't add a real address (town-centre precision only)
regardless.

---

## Endpoints

| Route | Purpose |
|---|---|
| `/` | the console |
| `/api/current` | latest value per sensor/metric (JSON) |
| `/api/history` | 24h trace for one sensor+metric (sparkline) |
| `/api/forecast` | current + hourly + daily from Open-Meteo (cached 20 min) |
| `/api/radar` | radar metadata (DWD timestamps or RainViewer frames, cached 5 min) |
| `/healthz` | liveness + Prometheus reachability |

---

## Runbook

### Apply a sensor or UI change

```bash
cd ~/stacks/aether
docker compose restart aether   # sensors.yaml and templates/ are bind-mounted
```

### Check health

```bash
curl -s https://weather.home/healthz
docker compose -f ~/stacks/aether/docker-compose.yml logs -f --tail 30
```

### Radar shows blank / stale

List the real time-enabled DWD radar layers if `dwd_layer` stops rendering:

```bash
docker exec aether python3 -c "import requests,re; t=requests.get('https://maps.dwd.de/geoserver/dwd/wms',params={'service':'WMS','request':'GetCapabilities'},timeout=30).text; print([n for n in set(re.findall(r'<Name>([^<]+)</Name>',t)) if re.search(r'radar|nieders|radolan',n,re.I)])"
```

If frames cycle but the image never changes, DWD ignored the WMS `TIME`
parameter for that layer — switch `radar_source` to `rainviewer` in
`sensors.yaml`. An empty map on a dry day is normal (no echo = a transparent
overlay), not a fault.

### Forecast tile empty

Confirm `settings.location` has real coordinates in `sensors.yaml` — until
set, the tile intentionally shows a "set coordinates" prompt instead of
failing. Then confirm outbound HTTPS to `api.open-meteo.com` isn't blocked at
the router (same class of restriction as the Midea AC exporter).

### First-time setup

```bash
cd ~/stacks/aether
docker network ls | grep monitoring   # confirm the network name matches docker-compose.yml
docker compose up -d --build
docker exec caddy caddy reload --config /etc/caddy/Caddyfile   # Caddyfile block already present
```

Then add the `weather.home` → `192.168.1.10` record in Pi-hole (**Local DNS →
DNS Records**) if not already present.

---

## Related

- [[Architecture]] — design rationale (why not Home Assistant, local-first tradeoffs)
- [[Monitoring]] — scrape topology for Netatmo/Tado/Shelly, which Aether reads from
- [[Caddy]] — reverse-proxy patterns; the `weather.home` block already exists in the Caddyfile
- [[Services]] — port and DNS reference
- `mnemosyne/stacks/aether/README.md` — full sensor-catalog format, accessibility notes, vendoring Leaflet
