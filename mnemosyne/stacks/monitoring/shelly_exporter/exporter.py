#!/usr/bin/env python3
"""
Shelly Plug Prometheus Exporter
Polls Shelly Plug S (Gen1 + Gen2/3), H&T Gen3, and BLU H&T via BLU Gateway KVS.
No cloud, no MQTT required.
"""

import os
import time
import json
import logging
import requests
from flask import Flask, Response
from prometheus_client import (
    Gauge, CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
registry = CollectorRegistry()

# --- Metrics -----------------------------------------------------------------

POWER    = Gauge("shelly_power_watts",        "Current active power in watts",    ["device", "host"], registry=registry)
VOLTAGE  = Gauge("shelly_voltage_volts",       "Current voltage in volts",          ["device", "host"], registry=registry)
CURRENT  = Gauge("shelly_current_amps",        "Current in amperes",                ["device", "host"], registry=registry)
ENERGY   = Gauge("shelly_energy_total_kwh",    "Total energy consumed in kWh",      ["device", "host"], registry=registry)
SWITCH   = Gauge("shelly_switch_state",        "Relay state (1=on, 0=off)",         ["device", "host"], registry=registry)
ONLINE   = Gauge("shelly_device_online",       "Device reachable (1=yes, 0=no)",    ["device", "host"], registry=registry)
DEV_TEMP = Gauge("shelly_device_temp_celsius", "Internal device temperature (°C)",  ["device", "host"], registry=registry)
ENV_TEMP = Gauge("shelly_temperature_celsius", "Ambient temperature in °C",          ["device", "host"], registry=registry)
HUMIDITY = Gauge("shelly_humidity_percent",    "Relative humidity in percent",       ["device", "host"], registry=registry)
BATTERY  = Gauge("shelly_battery_percent",     "Battery charge in percent",          ["device", "host"], registry=registry)

# --- Config ------------------------------------------------------------------

TIMEOUT       = int(os.getenv("SHELLY_TIMEOUT",   "5"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL",    "15"))
EXPORTER_PORT = int(os.getenv("EXPORTER_PORT",    "9117"))


def parse_devices() -> list[dict]:
    """
    Reads SHELLY_DEVICES env var (power plugs Gen1/Gen2).
    Format: "name:host:gen,..."
    Example: "PC-Astraeus:192.168.1.12:1,PC-Desktop:192.168.1.x:2"
    """
    raw = os.getenv("SHELLY_DEVICES", "")
    devices = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            log.warning("Skipping malformed SHELLY_DEVICES entry: %s", entry)
            continue
        devices.append({"name": parts[0], "host": parts[1], "gen": int(parts[2])})
    return devices


def parse_ht_devices() -> list[dict]:
    """
    Reads SHELLY_HT_DEVICES env var (H&T Gen3, direct WiFi poll).
    Format: "name;host,..."   (semicolon inside entry to avoid MAC colon clash)
    Example: "HT-Keller;192.168.1.x"
    """
    raw = os.getenv("SHELLY_HT_DEVICES", "")
    devices = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(";")
        if len(parts) != 2:
            log.warning("Skipping malformed SHELLY_HT_DEVICES entry: %s", entry)
            continue
        devices.append({"name": parts[0], "host": parts[1]})
    return devices


def parse_blu_devices() -> list[dict]:
    """
    Reads SHELLY_BLU_DEVICES env var (BLU H&T via gateway KVS).
    Format: "name;gateway_host;mac,..."
    Example: "BLU-Keller;192.168.1.x;c0:2c:ed:77:df:12"
    The KVS key used on the gateway equals the MAC address (set by BLU Gateway script).
    """
    raw = os.getenv("SHELLY_BLU_DEVICES", "")
    devices = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(";")
        if len(parts) != 3:
            log.warning("Skipping malformed SHELLY_BLU_DEVICES entry: %s", entry)
            continue
        devices.append({"name": parts[0], "host": parts[1], "mac": parts[2]})
    return devices

# --- Polling -----------------------------------------------------------------

def poll_gen1(device: dict) -> None:
    """Poll Gen1 devices via /status endpoint."""
    url = f"http://{device['host']}/status"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        labels = (device["name"], device["host"])
        ONLINE.labels(*labels).set(1)

        meters = data.get("meters", [{}])[0]
        POWER.labels(*labels).set(meters.get("power", 0))
        # Gen1 /status returns total in Watt-minutes → convert to kWh
        ENERGY.labels(*labels).set(meters.get("total", 0) / 60000)

        SWITCH.labels(*labels).set(1 if data.get("relays", [{}])[0].get("ison") else 0)

        if "tmp" in data:
            DEV_TEMP.labels(*labels).set(data["tmp"].get("tC", 0))

    except Exception as exc:
        log.warning("Gen1 poll failed for %s (%s): %s", device["name"], device["host"], exc)
        ONLINE.labels(device["name"], device["host"]).set(0)


def poll_gen2(device: dict) -> None:
    """Poll Gen2/3 plug devices via RPC endpoint."""
    url = f"http://{device['host']}/rpc/Switch.GetStatus?id=0"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        labels = (device["name"], device["host"])
        ONLINE.labels(*labels).set(1)

        POWER.labels(*labels).set(data.get("apower", 0))
        VOLTAGE.labels(*labels).set(data.get("voltage", 0))
        CURRENT.labels(*labels).set(data.get("current", 0))
        # aenergy.total is in Wh → convert to kWh
        ENERGY.labels(*labels).set(data.get("aenergy", {}).get("total", 0) / 1000)
        SWITCH.labels(*labels).set(1 if data.get("output") else 0)

        if "temperature" in data:
            DEV_TEMP.labels(*labels).set(data["temperature"].get("tC", 0))

    except Exception as exc:
        log.warning("Gen2 poll failed for %s (%s): %s", device["name"], device["host"], exc)
        ONLINE.labels(device["name"], device["host"]).set(0)


def poll_ht_gen3(device: dict) -> None:
    """Poll Shelly H&T Gen3 via /rpc/Shelly.GetStatus (one call → temp, humidity, battery)."""
    url = f"http://{device['host']}/rpc/Shelly.GetStatus"
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()

        labels = (device["name"], device["host"])
        ONLINE.labels(*labels).set(1)

        ENV_TEMP.labels(*labels).set(data.get("temperature:0", {}).get("tC", 0))
        HUMIDITY.labels(*labels).set(data.get("humidity:0", {}).get("rh", 0))
        bat = data.get("devicepower:0", {}).get("battery", {})
        BATTERY.labels(*labels).set(bat.get("percent", 0))

    except Exception as exc:
        log.warning("H&T Gen3 poll failed for %s (%s): %s", device["name"], device["host"], exc)
        ONLINE.labels(device["name"], device["host"]).set(0)


def poll_blu_ht(device: dict) -> None:
    """
    Poll Shelly BLU H&T via gateway KVS.
    Requires the BLU Gateway script running on the gateway device; it stores
    each BLU device's readings under its MAC address as KVS key.
    """
    url = f"http://{device['host']}/rpc/KVS.Get"
    try:
        r = requests.get(url, params={"key": device["mac"]}, timeout=TIMEOUT)
        r.raise_for_status()
        raw = r.json()
        # KVS value is a JSON-encoded string
        data = json.loads(raw.get("value", "{}"))

        labels = (device["name"], device["host"])
        ONLINE.labels(*labels).set(1)

        ENV_TEMP.labels(*labels).set(data.get("temperature", 0))
        HUMIDITY.labels(*labels).set(data.get("humidity", 0))
        if "battery" in data:
            BATTERY.labels(*labels).set(data["battery"])

    except Exception as exc:
        log.warning(
            "BLU H&T poll failed for %s (gateway %s, mac=%s): %s",
            device["name"], device["host"], device["mac"], exc,
        )
        ONLINE.labels(device["name"], device["host"]).set(0)


def poll_loop(
    devices:     list[dict],
    ht_devices:  list[dict],
    blu_devices: list[dict],
) -> None:
    while True:
        for device in devices:
            if device["gen"] == 1:
                poll_gen1(device)
            else:
                poll_gen2(device)
        for device in ht_devices:
            poll_ht_gen3(device)
        for device in blu_devices:
            poll_blu_ht(device)
        time.sleep(POLL_INTERVAL)

# --- Flask -------------------------------------------------------------------

@app.route("/metrics")
def metrics():
    return Response(generate_latest(registry), mimetype=CONTENT_TYPE_LATEST)

@app.route("/health")
def health():
    return "ok"

# --- Main --------------------------------------------------------------------

if __name__ == "__main__":
    import threading
    devices     = parse_devices()
    ht_devices  = parse_ht_devices()
    blu_devices = parse_blu_devices()

    all_devices = devices + ht_devices + blu_devices
    if not all_devices:
        log.error(
            "No devices configured. Set SHELLY_DEVICES, SHELLY_HT_DEVICES, or SHELLY_BLU_DEVICES."
        )
    else:
        log.info(
            "Starting polling for %d device(s): %s",
            len(all_devices),
            [d["name"] for d in all_devices],
        )
        t = threading.Thread(
            target=poll_loop, args=(devices, ht_devices, blu_devices), daemon=True
        )
        t.start()
    app.run(host="0.0.0.0", port=EXPORTER_PORT)
