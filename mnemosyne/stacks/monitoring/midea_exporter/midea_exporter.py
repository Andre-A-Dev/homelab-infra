#!/usr/bin/env python3
"""
Midea Portasplit / AC Prometheus exporter (cache + auto-discovery).

Strategy:
  * COLD START (no cache): run Discover.discover_single() once. This contacts
    the Midea cloud briefly to fetch the CORRECT device id and the WORKING
    token/key pair (devices expose two pairs; only one answers state queries).
    The result is written to a local cache file.
  * EVERY START AFTER: load id/token/key from the cache and authenticate
    purely locally. No cloud, no broadcast -- works behind a FritzBox block.

This removes the two failure modes we hit by hand:
  * Wrong device id in the packet header -> auth ok but queries time out.
  * Wrong token/key pair -> auth ok but device never answers.

One-time requirement: the device must reach the internet during the FIRST run
so discovery can fetch credentials. Afterwards you can block it again.

Note: this device does NOT report energy over LAN (total/real_time power = None).
Use a smart plug for kWh.
"""

import asyncio
import json
import logging
import os

from prometheus_client import Gauge, start_http_server

from msmart.device import AirConditioner as AC
from msmart.discover import Discover

# --- Configuration -----------------------------------------------------------
DEVICE_IP = os.environ["MIDEA_IP"]
DEVICE_PORT = int(os.environ.get("MIDEA_PORT", "6444"))
CACHE_FILE = os.environ.get("MIDEA_CACHE", "/data/device_creds.json")
REGION = os.environ.get("MIDEA_REGION", "DE")          # built-in cloud creds region
ACCOUNT_EMAIL = os.environ.get("MIDEA_ACCOUNT_EMAIL", "")  # optional own MSmartHome login
ACCOUNT_PW = os.environ.get("MIDEA_ACCOUNT_PASSWORD", "")
DEVICE_NAME = os.environ.get("MIDEA_DEVICE_NAME", "portasplit")
ACCOUNT_LABEL = os.environ.get("MIDEA_ACCOUNT", "home")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9116"))
OP_TIMEOUT = int(os.environ.get("OP_TIMEOUT", "15"))   # seconds per device call
REBUILD_BACKOFF = int(os.environ.get("REBUILD_BACKOFF", "4"))  # let device release its slot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("midea-exporter")

LABELS = ["account", "device"]

# --- Metrics -----------------------------------------------------------------
g_online = Gauge("midea_device_online", "Device reachable (1=yes, 0=no)", LABELS)
g_power = Gauge("midea_power_state", "Unit powered on (1=on, 0=off)", LABELS)
g_target = Gauge("midea_target_temperature_celsius", "Target temperature", LABELS)
g_indoor = Gauge("midea_indoor_temperature_celsius", "Indoor coil-side sensor (under-reports!)", LABELS)
g_outdoor = Gauge("midea_outdoor_temperature_celsius", "Outdoor unit sensor", LABELS)
g_fan = Gauge("midea_fan_speed", "Fan speed (raw value / percent)", LABELS)
# AUTO=1, COOL=2, DRY=3, HEAT=4, FAN_ONLY=5
g_mode = Gauge("midea_operational_mode", "Operational mode (1=auto 2=cool 3=dry 4=heat 5=fan)", LABELS)

_first_read_logged = False


def _set(metric: Gauge, value) -> None:
    if value is None:
        return
    try:
        metric.labels(account=ACCOUNT_LABEL, device=DEVICE_NAME).set(float(value))
    except (TypeError, ValueError):
        pass


# --- Credential cache --------------------------------------------------------
def _load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fh:
            creds = json.load(fh)
        if all(creds.get(k) for k in ("id", "token", "key")):
            return creds
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return None


def _save_cache(creds: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_FILE) or ".", exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(creds, fh)
    os.replace(tmp, CACHE_FILE)  # atomic
    try:
        os.chmod(CACHE_FILE, 0o600)
    except OSError:
        pass


async def _build_local(creds: dict) -> AC:
    """Authenticate purely locally from cached id/token/key. No cloud."""
    device = AC(ip=DEVICE_IP, port=DEVICE_PORT, device_id=int(creds["id"]))
    await asyncio.wait_for(device.authenticate(token=creds["token"], key=creds["key"]), OP_TIMEOUT)
    return device


async def _discover_and_cache() -> AC:
    """One-time cloud discovery: fetch correct id + working token/key pair."""
    log.info("no cached creds -- running one-time cloud discovery (needs internet)")
    kwargs = {"region": REGION}
    if ACCOUNT_EMAIL and ACCOUNT_PW:
        kwargs["account"] = ACCOUNT_EMAIL
        kwargs["password"] = ACCOUNT_PW

    device = await asyncio.wait_for(Discover.discover_single(DEVICE_IP, **kwargs), 60)
    if device is None or not getattr(device, "token", None):
        raise RuntimeError("discovery failed -- is the device online and reachable?")

    creds = {"id": int(device.id), "token": device.token, "key": device.key}
    _save_cache(creds)
    log.info("cached working creds for device id=%s -> %s", device.id, CACHE_FILE)
    return device


async def get_device(force_local: bool = False) -> AC:
    """Cache first; fall back to one-time discovery only on cold start."""
    creds = _load_cache()
    if creds:
        return await _build_local(creds)
    if force_local:
        raise RuntimeError("no cached creds available for local rebuild")
    return await _discover_and_cache()


# --- Poll loop ---------------------------------------------------------------
async def read_once(device: AC) -> None:
    global _first_read_logged
    await asyncio.wait_for(device.refresh(), OP_TIMEOUT)

    online = bool(getattr(device, "online", True))
    g_online.labels(account=ACCOUNT_LABEL, device=DEVICE_NAME).set(1 if online else 0)
    if not online:
        return

    _set(g_power, 1 if getattr(device, "power_state", False) else 0)
    _set(g_target, getattr(device, "target_temperature", None))
    _set(g_indoor, getattr(device, "indoor_temperature", None))
    _set(g_outdoor, getattr(device, "outdoor_temperature", None))

    fan = getattr(device, "fan_speed", None)
    _set(g_fan, getattr(fan, "value", fan))
    mode = getattr(device, "operational_mode", None)
    _set(g_mode, getattr(mode, "value", mode))

    if not _first_read_logged:
        log.info(
            "first read ok: power=%s target=%s indoor=%s outdoor=%s mode=%s",
            getattr(device, "power_state", None),
            getattr(device, "target_temperature", None),
            getattr(device, "indoor_temperature", None),
            getattr(device, "outdoor_temperature", None),
            getattr(device, "operational_mode", None),
        )
        _first_read_logged = True


async def poll_loop() -> None:
    device = await get_device()

    while True:
        try:
            await read_once(device)
        except Exception as exc:  # noqa: BLE001
            # One in-place retry on the SAME connection -- transient drops are
            # common and a full reconnect churns the device's single slot.
            log.warning("refresh failed (%s) -- retrying once", exc)
            try:
                await asyncio.sleep(2)
                await read_once(device)
            except Exception as exc2:  # noqa: BLE001
                log.warning("retry failed (%s) -- rebuilding (local)", exc2)
                g_online.labels(account=ACCOUNT_LABEL, device=DEVICE_NAME).set(0)
                await asyncio.sleep(REBUILD_BACKOFF)  # give device time to free its slot
                try:
                    device = await get_device(force_local=True)
                except Exception as exc3:  # noqa: BLE001
                    log.error("rebuild failed (%s)", exc3)

        await asyncio.sleep(POLL_INTERVAL)


def main() -> None:
    start_http_server(EXPORTER_PORT)
    log.info("midea-exporter listening on :%d", EXPORTER_PORT)
    asyncio.run(poll_loop())


if __name__ == "__main__":
    main()
