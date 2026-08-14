#!/usr/bin/env python3
"""
Aether - a dedicated weather console backed by Prometheus.

The app is a thin presentation layer over the Prometheus HTTP API. It owns no
state and stores nothing: every reading is queried live from Prometheus, which
already aggregates Netatmo, Tado and Shelly via their exporters. Sensors and
their PromQL live in sensors.yaml, so this file never changes when you add one.
"""

import os
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import yaml
import requests
from flask import Flask, render_template, jsonify, request

# --- Configuration -----------------------------------------------------------

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
SENSORS_FILE = os.environ.get("AETHER_SENSORS", "sensors.yaml")
QUERY_TIMEOUT = float(os.environ.get("AETHER_QUERY_TIMEOUT", "8"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aether")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # template edits apply on restart, no rebuild


# --- Catalog -----------------------------------------------------------------

def load_catalog():
    """Read sensors.yaml fresh on every call so edits need no app restart."""
    with open(SENSORS_FILE, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def list_sensors(catalog):
    return catalog.get("sensors", [])


def sensor_sources(sensor):
    """Normalize iteration over a sensor's metrics.

    Multi-source sensors define `sources: {key: {metric: promql}}`; flat sensors
    define `metrics: {metric: promql}` (yielded under source key None).
    """
    if "sources" in sensor:
        for key, metrics in sensor["sources"].items():
            yield key, metrics
    else:
        yield None, sensor.get("metrics", {})


# --- Prometheus access -------------------------------------------------------

def prom_instant(query):
    """Return the latest scalar value for a PromQL expression, or None."""
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=QUERY_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            return None
        return float(result[0]["value"][1])
    except Exception as exc:  # noqa: BLE001 - surface as "no data", never crash a tile
        log.warning("instant query failed (%s): %s", query, exc)
        return None


def prom_range(query, hours, step):
    """Return [[unix_ts, value], ...] for the sparkline, or an empty list."""
    end = time.time()
    start = end - hours * 3600
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
            timeout=QUERY_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()["data"]["result"]
        if not result:
            return []
        return [[float(ts), float(val)] for ts, val in result[0]["values"]]
    except Exception as exc:  # noqa: BLE001
        log.warning("range query failed (%s): %s", query, exc)
        return []


def prometheus_reachable():
    try:
        requests.get(f"{PROMETHEUS_URL}/-/healthy", timeout=QUERY_TIMEOUT).raise_for_status()
        return True
    except Exception:  # noqa: BLE001
        return False


# --- Routes ------------------------------------------------------------------

def client_view(sensors):
    """Strip PromQL before handing the catalog to the browser; the client only
    needs metric names, the queries stay server-side."""
    view = []
    for s in sensors:
        base = {
            "id": s["id"],
            "label": s.get("label", s["id"]),
            "sublabel": s.get("sublabel", ""),
            "zone": s.get("zone", "indoor"),
            "hero": bool(s.get("hero", False)),
        }
        if "sources" in s:
            base["primary"] = s.get("primary") or next(iter(s["sources"]))
            base["sources"] = {k: list(m.keys()) for k, m in s["sources"].items()}
        else:
            base["metrics"] = list(s.get("metrics", {}).keys())
        view.append(base)
    return view


@app.route("/")
def index():
    catalog = load_catalog()
    return render_template(
        "index.html",
        sensors=client_view(list_sensors(catalog)),
        settings=catalog.get("settings", {}),
    )


@app.route("/api/current")
def api_current():
    """Latest value for every metric of every sensor, queried concurrently.

    Flat sensors return {metric: value}; multi-source sensors return
    {source: {metric: value}}.
    """
    catalog = load_catalog()
    sensors = list_sensors(catalog)

    jobs = []  # (sensor_id, source_key_or_None, metric_name, promql)
    out = {}
    for s in sensors:
        if "sources" in s:
            out[s["id"]] = {k: {} for k in s["sources"]}
        else:
            out[s["id"]] = {}
        for source_key, metrics in sensor_sources(s):
            for metric, query in metrics.items():
                jobs.append((s["id"], source_key, metric, query))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda j: (j[0], j[1], j[2], prom_instant(j[3])), jobs)
        for sensor_id, source_key, metric, value in results:
            if source_key is None:
                out[sensor_id][metric] = value
            else:
                out[sensor_id][source_key][metric] = value

    return jsonify({"reachable": prometheus_reachable(), "sensors": out})


@app.route("/api/history")
def api_history():
    """24h trace for one sensor+metric, for the sparkline.

    For multi-source sensors, ?source=<key> selects which source's series to
    trace (defaults to the primary).
    """
    catalog = load_catalog()
    settings = catalog.get("settings", {})
    sensor_id = request.args.get("sensor")
    metric = request.args.get("metric", "temperature")
    source = request.args.get("source")
    hours = int(request.args.get("hours", settings.get("history_hours", 24)))
    step = int(settings.get("step_seconds", 300))

    sensor = next((s for s in list_sensors(catalog) if s["id"] == sensor_id), None)
    if not sensor:
        return jsonify({"error": "unknown sensor"}), 404

    if "sources" in sensor:
        src = source or sensor.get("primary") or next(iter(sensor["sources"]))
        metrics = sensor["sources"].get(src, {})
    else:
        metrics = sensor.get("metrics", {})

    if metric not in metrics:
        return jsonify({"error": "unknown metric"}), 404

    return jsonify({"points": prom_range(metrics[metric], hours, step)})


# --- Forecast (Open-Meteo) ---------------------------------------------------
# No API key. Free for non-commercial use, CC BY 4.0 (attribution shown in UI).
# Cached so we hit Open-Meteo at most every FORECAST_TTL seconds.

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_TTL = int(os.environ.get("AETHER_FORECAST_TTL", "1200"))  # 20 min
_forecast_cache = {"ts": 0, "data": None}

# WMO weather codes -> (label, glyph). Swap glyphs for SVG later if desired.
WMO = {
    0: ("Clear", "☀"), 1: ("Mainly clear", "🌤"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁"), 45: ("Fog", "🌫"), 48: ("Rime fog", "🌫"),
    51: ("Light drizzle", "🌦"), 53: ("Drizzle", "🌦"), 55: ("Dense drizzle", "🌦"),
    56: ("Freezing drizzle", "🌧"), 57: ("Freezing drizzle", "🌧"),
    61: ("Light rain", "🌧"), 63: ("Rain", "🌧"), 65: ("Heavy rain", "🌧"),
    66: ("Freezing rain", "🌧"), 67: ("Freezing rain", "🌧"),
    71: ("Light snow", "🌨"), 73: ("Snow", "🌨"), 75: ("Heavy snow", "🌨"),
    77: ("Snow grains", "🌨"), 80: ("Showers", "🌦"), 81: ("Showers", "🌧"),
    82: ("Violent showers", "⛈"), 85: ("Snow showers", "🌨"), 86: ("Snow showers", "🌨"),
    95: ("Thunderstorm", "⛈"), 96: ("Thunderstorm + hail", "⛈"), 99: ("Thunderstorm + hail", "⛈"),
}


def describe_code(code):
    label, glyph = WMO.get(int(code), ("—", "·"))
    return {"code": int(code), "label": label, "glyph": glyph}


@app.route("/api/forecast")
def api_forecast():
    catalog = load_catalog()
    settings = catalog.get("settings", {})
    loc = settings.get("location", {})
    lat, lon = loc.get("latitude", 0), loc.get("longitude", 0)
    days = max(1, min(16, int(settings.get("forecast_days", 7))))
    fhours = max(1, min(48, int(settings.get("forecast_hours", 24))))

    if not lat and not lon:
        return jsonify({"configured": False})

    now = time.time()
    if (_forecast_cache["data"] and now - _forecast_cache["ts"] < FORECAST_TTL
            and _forecast_cache.get("days") == days and _forecast_cache.get("hours") == fhours):
        return jsonify(_forecast_cache["data"])

    try:
        resp = requests.get(OPEN_METEO_URL, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": max(days, 2),
        }, timeout=QUERY_TIMEOUT)
        resp.raise_for_status()
        raw = resp.json()

        cur = raw.get("current", {})
        daily = raw.get("daily", {})
        days_out = []
        for i, date in enumerate(daily.get("time", [])[:days]):
            days_out.append({
                "date": date,
                "code": describe_code(daily["weather_code"][i]),
                "max": daily["temperature_2m_max"][i],
                "min": daily["temperature_2m_min"][i],
                "pop": daily.get("precipitation_probability_max", [None]*99)[i],
            })

        # Hourly: slice the next `fhours` hours starting from the current hour.
        hourly = raw.get("hourly", {})
        htimes = hourly.get("time", [])
        htemp = hourly.get("temperature_2m", [])
        hcode = hourly.get("weather_code", [])
        hpop = hourly.get("precipitation_probability", [])
        cur_hour = (cur.get("time") or "")[:13]   # YYYY-MM-DDTHH
        start = next((i for i, t in enumerate(htimes) if t[:13] >= cur_hour), 0)
        hours_out = []
        for i in range(start, min(start + fhours, len(htimes))):
            hours_out.append({
                "time": htimes[i],
                "temp": htemp[i] if i < len(htemp) else None,
                "code": describe_code(hcode[i]) if i < len(hcode) else describe_code(-1),
                "pop": hpop[i] if i < len(hpop) else None,
            })

        data = {
            "configured": True,
            "place": loc.get("name", "Home"),
            "current": {
                "temperature": cur.get("temperature_2m"),
                "apparent": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"),
                "code": describe_code(cur.get("weather_code", -1)),
            },
            "hourly": hours_out,
            "daily": days_out,
        }
        _forecast_cache.update(ts=now, data=data, days=days, hours=fhours)
        return jsonify(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("forecast fetch failed: %s", exc)
        # Serve stale cache if we have it, else flag the error for the UI.
        if _forecast_cache["data"]:
            return jsonify(_forecast_cache["data"])
        return jsonify({"configured": True, "error": "forecast unavailable"})


# --- Radar (DWD WN + RainViewer) ---------------------------------------------
# Two sources, selectable via settings.map.radar_source:
#   dwd        - DWD GeoServer WMS, layer dwd:WN-Produkt (radar composite WITH
#                nowcast). Time-enabled, so we read the available timestamps from
#                GetCapabilities and the client animates them via the WMS TIME
#                parameter. The most accurate option for Germany.
#   rainviewer - global animated product (XYZ tiles + frame-metadata JSON).
# Tiles are always fetched client-side; only the small metadata is proxied here.

RAINVIEWER_URL = "https://api.rainviewer.com/public/weather-maps.json"
DWD_WMS_URL = "https://maps.dwd.de/geoserver/dwd/wms"
# Real DWD GeoServer layer names (no "dwd:" prefix on this endpoint). Override
# via settings.map.dwd_layer. "Niederschlagsradar" is the classic animated rain
# radar; "Radar_rv_product_1x1km_ger" adds a 2 h nowcast.
DWD_RADAR_LAYER = "Niederschlagsradar"
DWD_LIVE_LAYER = "Niederschlagsradar"
RADAR_TTL = int(os.environ.get("AETHER_RADAR_TTL", "300"))  # 5 min
_radar_cache = {"ts": 0, "data": None}
_dwd_cache = {"ts": 0, "data": None, "layer": None}


def _parse_iso(s):
    s = s.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _parse_period(p):
    # ISO-8601 duration, radar uses minutes (e.g. PT5M); handle H/M/S.
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", p.strip())
    if not m:
        return timedelta(minutes=5)
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return timedelta(hours=h, minutes=mi, seconds=s) or timedelta(minutes=5)


def _expand_extent(ext):
    """A WMS time extent is either a comma list or start/end/period; return ISO."""
    ext = ext.strip()
    if "/" in ext:
        parts = ext.split("/")
        if len(parts) >= 3:
            start, end, step = _parse_iso(parts[0]), _parse_iso(parts[1]), _parse_period(parts[2])
            out, t = [], start
            while t <= end and len(out) < 60:
                out.append(t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                t += step
            return out
        return [parts[0]]
    return [x.strip() for x in ext.split(",") if x.strip()][-30:]


def dwd_radar_times(layer):
    """Read the layer's available timestamps from DWD WMS GetCapabilities.

    Finds the exact <Layer> whose <Name> equals `layer`, then the time
    Dimension within that layer's block. Returns [] (and logs) if not found.
    """
    resp = requests.get(DWD_WMS_URL, params={
        "service": "WMS", "version": "1.3.0", "request": "GetCapabilities",
    }, timeout=QUERY_TIMEOUT)
    resp.raise_for_status()
    text = resp.text
    # locate the exact <Name>layer</Name>, then the time dimension after it,
    # bounded by the next <Name> so we don't read a neighbour's dimension.
    name_tag = f"<Name>{layer}</Name>"
    idx = text.find(name_tag)
    if idx < 0:
        log.warning("dwd radar layer %r not found in capabilities", layer)
        return []
    nxt = text.find("<Name>", idx + len(name_tag))
    block = text[idx: nxt if nxt != -1 else idx + 8000]
    m = re.search(r'<(?:Dimension|Extent) name="time"[^>]*>([^<]+)</(?:Dimension|Extent)>', block)
    if not m:
        log.warning("dwd radar layer %r has no time dimension", layer)
        return []
    return _expand_extent(m.group(1))


@app.route("/api/radar")
def api_radar():
    source = request.args.get("source", "rainviewer")

    if source == "dwd":
        catalog = load_catalog()
        layer = catalog.get("settings", {}).get("map", {}).get("dwd_layer") or DWD_RADAR_LAYER
        now = time.time()
        if (_dwd_cache["data"] and now - _dwd_cache["ts"] < RADAR_TTL
                and _dwd_cache.get("layer") == layer):
            return jsonify(_dwd_cache["data"])
        try:
            times = dwd_radar_times(layer)
        except Exception as exc:  # noqa: BLE001
            log.warning("dwd capabilities failed: %s", exc)
            times = []
        data = {"wms": DWD_WMS_URL, "layer": layer,
                "live_layer": layer, "times": times}
        _dwd_cache.update(ts=now, data=data, layer=layer)
        return jsonify(data)

    # rainviewer
    now = time.time()
    if _radar_cache["data"] and now - _radar_cache["ts"] < RADAR_TTL:
        return jsonify(_radar_cache["data"])
    try:
        r = requests.get(RAINVIEWER_URL, timeout=QUERY_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        _radar_cache.update(ts=now, data=data)
        return jsonify(data)
    except Exception as exc:  # noqa: BLE001
        log.warning("radar fetch failed: %s", exc)
        if _radar_cache["data"]:
            return jsonify(_radar_cache["data"])
        return jsonify({"error": "radar unavailable"}), 502


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "prometheus": prometheus_reachable()})


if __name__ == "__main__":
    # Dev server only. In the container, gunicorn serves app:app (see Dockerfile).
    port = int(os.environ.get("AETHER_PORT", "8050"))
    app.run(host="0.0.0.0", port=port, debug=True)
