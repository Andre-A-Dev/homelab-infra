# Aether — Weather Console

A dedicated weather frontend for **Home** — Netatmo, Tado and Shelly readings
plus a 5-day forecast — a calm, glanceable alternative to a Grafana dashboard.
Aether stores nothing: it queries the existing Prometheus live, where the
exporters already aggregate everything into one place.

`weather.home` → `aether:8050` (Docker, `caddy_proxy` network).

**Scope: Home only.** Fuchsbau sensors are deliberately excluded — they don't
belong in a frontend that may be shared, and the data stays private.

## Why this exists

The data is already unified in Prometheus. What was missing was a *display* that
isn't Grafana. Aether is a thin presentation layer — no second integration of
Netatmo/Tado/Shelly, no new state, no duplicated work. Adding Home Assistant
would have re-integrated all three devices a second time; that's integration we
don't need. We only needed a frontend.

## The idea: sensors are config, not code

Every tile is defined in `sensors.yaml` as a PromQL expression. Adding a sensor
is a YAML block — no Python, no rebuild. Netatmo is pre-filled with the real
metric names and runs out of the box. **Tado** rooms use the `eko`/`IamTheLoki`
exporter scheme (`tado_sensor_temperature_value`, `tado_sensor_humidity_percentage`,
label `zone`, plus `tado_activity_heating_power_percentage` for the heating chip)
— set your real zone names. **Shelly** (BLU H&T outdoor, H&T Gen3 indoor) is a
commented stub: fill in the real metric names and uncomment.

Indoor sensors (Netatmo + Tado) render together as **one horizontally scrollable
rooms strip**, in catalog order.

Discover an exporter's real metric names:

```bash
curl -s http://prometheus:9090/api/v1/label/__name__/values \
  | tr ',' '\n' | grep -i shelly
```

## Layout

```
~/homelab-infra/mnemosyne/stacks/aether/
├── app.py                 # Flask + Prometheus query layer (rarely changes)
├── sensors.yaml           # sensor catalog — edit here to add tiles
├── templates/
│   └── index.html         # the console UI (edit freely, restart to apply)
├── static/
│   └── leaflet/           # vendored Leaflet (leaflet.js + leaflet.css)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── caddyfile-snippet.txt
```

## Setup

```bash
cd ~/homelab-infra/mnemosyne/stacks/aether

# 1. Confirm the monitoring network name and fix it in docker-compose.yml if needed
docker network ls | grep monitoring

# 2. Build & start
docker compose up -d --build

# 3. Caddy: add the block from caddyfile-snippet.txt, then reload
docker exec caddy caddy reload --config /etc/caddy/Caddyfile

# 4. Pi-hole: add Local DNS record  weather.home -> 192.168.1.10
```

Open `https://weather.home`.

## Forecast (Open-Meteo)

The forecast uses **Open-Meteo**: no API key, no account, free for non-commercial
use, open-source and self-hostable, CC BY 4.0 (attribution shown in the footer).
For Europe it draws on DWD ICON — effectively official German weather data. The
backend caches responses for 20 min, so call volume is negligible.

Set your location in `sensors.yaml` under `settings.location`:

```yaml
location:
  name: "Home"
  latitude:  49.5897    # use TOWN-CENTRE coordinates, not your address
  longitude: 11.0120
```

Horizon is configurable: `forecast_days` (1–16 daily) and `forecast_hours`
(1–48 hourly). The forecast tile shows current conditions, an hourly strip, and
a daily strip.

**Privacy:** a forecast is identical for the whole town, so use town-centre
coordinates — this keeps your home location out of the repo. `export_public.py`
should treat these as sensitive. Until coordinates are set, the forecast tile
shows a "set coordinates" prompt instead of failing.

The `aether` container needs outbound HTTPS to `api.open-meteo.com`. If the
FritzBox restricts the container's internet (as it does for the Midea AC), allow
it — or self-host Open-Meteo for a fully local path.

## Radar map

An optional precipitation-radar map: **Leaflet** (vendored locally under
`static/leaflet/`, no CDN) + **OpenStreetMap** base tiles + a radar overlay.
Toggle with `settings.map.radar`, set the initial `settings.map.zoom`, and pick
the source with `settings.map.radar_source`:

- **`dwd`** (default, recommended for Germany) — DWD radar via the DWD GeoServer
  WMS (`maps.dwd.de/geoserver/dwd/wms`). The layer is time-enabled: Aether reads
  the available timestamps from GetCapabilities and **animates** them via the WMS
  TIME parameter. Pick the layer with `settings.map.dwd_layer` (real names on this
  server, no `dwd:` prefix):
    - `Niederschlagsradar` — classic animated rain radar (default)
    - `Radar_rv_product_1x1km_ger` — RV, precipitation incl. ~2 h nowcast
    - `Radar_wn-product_1x1km_ger` — WN composite with prediction
    - `RADOLAN-RW` / `RADOLAN-RY` — gauge-adjusted hourly / quality-checked

  ~1 km resolution, far more accurate for a German location than any global
  product. Official DWD open data — "Radar © DWD" attribution shown. If the
  chosen layer has no time dimension, Aether logs it and falls back to a single
  live layer with a manual Refresh (so a wrong layer name fails loudly, not
  silently).
- **`rainviewer`** — global product, lower resolution, but **animated** past
  frames + nowcast. Useful when travelling or as a fallback. Frame metadata is
  proxied + cached by the backend (`/api/radar`); tiles go browser→provider.

Switching source is a one-line YAML change + `restart` — no code edits.

**Local-first caveat (both sources).** This is the only feature that leaves
local-first: it is lazy-loaded (nothing loads until you click "Show radar"), but
while open the browser streams tiles to `tile.openstreetmap.org` and the chosen
radar provider (`maps.dwd.de` or `*.rainviewer.com`), leaking IP/location like
any web map. The container needs outbound HTTPS to those hosts when the panel is
used. Full-sovereignty path: self-host map tiles (TileServer GL) and process DWD
RADOLAN/RV binaries locally for a zero-external-call radar.

**Verifying DWD.** If the layer stays blank, check egress and reprojection. List
the real time-enabled radar layers your server offers:

```bash
docker exec aether python3 -c "import requests,re; t=requests.get('https://maps.dwd.de/geoserver/dwd/wms',params={'service':'WMS','request':'GetCapabilities'},timeout=30).text; print([n for n in set(re.findall(r'<Name>([^<]+)</Name>',t)) if re.search(r'radar|nieders|radolan',n,re.I)])"
```

Set one of those as `dwd_layer`. If the frames cycle but the image never changes,
DWD ignored the TIME parameter for that view — fall back to
`radar_source: rainviewer`. An empty map on a dry day is normal (no echo = a
transparent overlay).

Re-vendor Leaflet if needed: `npm pack leaflet@1.9.4` then copy
`package/dist/leaflet.js` and `leaflet.css` into `static/leaflet/`.

## Endpoints

| Route | Purpose |
|---|---|
| `/`               | the console |
| `/api/current`    | latest value per sensor/metric (JSON) |
| `/api/history`    | 24h trace for one sensor+metric (sparkline) |
| `/api/forecast`   | current + hourly + daily from Open-Meteo (cached 20 min) |
| `/api/radar`      | radar metadata: DWD WN timestamps (source=dwd) or RainViewer frames (source=rainviewer), cached 5 min |
| `/healthz`        | liveness + Prometheus reachability |

## Adjusting

- **Add/change sensors** → `sensors.yaml`, then `docker compose restart aether`
- **UI / design** → `templates/index.html`, then `docker compose restart aether`
- **History window** → `settings.history_hours` in `sensors.yaml`

## Two things worth knowing

**Prometheus retention.** Prometheus defaults to ~15 days of storage. Weather is
worth comparing over months. The temperature/humidity series are low-cardinality
and cheap — raise `--storage.tsdb.retention.time` (e.g. `2y`) on the Prometheus
container rather than reaching for a separate time-series DB.

**Cloud tether.** Shelly reads locally, but Netatmo and Tado both depend on their
vendor cloud APIs via their exporters. If a vendor API is down, those tiles go
empty (Aether shows them as "—", and the banner flags an unreachable Prometheus).
The Shelly BLU H&T outdoor sensor is the only outdoor reading that survives a
vendor outage — which is exactly why it was the strategically sound addition.

## Accessibility

The UI is semantic HTML with per-sensor `aria-live` summaries, visible keyboard
focus, and `prefers-reduced-motion` honoured — usable with VoiceOver, unlike
Grafana. Relevant if the console is ever shared.

---

*Erstellt mit Claude · claude.ai*
