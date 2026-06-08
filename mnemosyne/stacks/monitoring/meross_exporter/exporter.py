#!/usr/bin/env python3
"""
Prometheus exporter for Meross smart plugs.

Supports multiple Meross accounts. Each account is configured via numbered
environment variables. Falls back to the single-account variables for
backwards compatibility.

Multi-account example:
  MEROSS_EMAIL_1=main@example.com    MEROSS_PASSWORD_1=...  MEROSS_ALIAS_1=home
  MEROSS_EMAIL_2=other@example.com   MEROSS_PASSWORD_2=...  MEROSS_ALIAS_2=balcony

Single-account fallback (still works):
  MEROSS_EMAIL=main@example.com      MEROSS_PASSWORD=...

Metrics exposed (all carry 'account' and 'device' labels):
  meross_power_watts        - Instantaneous power draw
  meross_voltage_volts      - Voltage
  meross_current_amps       - Current draw
  meross_switch_state       - Outlet on/off (also carries 'channel' label)
  meross_device_online      - Device reachable (1/0)
  meross_energy_today_kwh   - Energy consumed today
"""

import asyncio
import logging
import os
import sys
from dataclasses import dataclass

from prometheus_client import Gauge, start_http_server
from meross_iot.http_api import MerossHttpClient
from meross_iot.manager import MerossManager
from meross_iot.model.enums import OnlineStatus

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meross_exporter")
logging.getLogger("meross_iot").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HTTP_PORT         = int(os.environ.get("EXPORTER_PORT",      "9114"))
POLL_SECONDS      = int(os.environ.get("POLL_INTERVAL",      "15"))
RECONNECT_SECONDS = int(os.environ.get("RECONNECT_INTERVAL", "60"))
API_URL           = os.environ.get("MEROSS_API_URL", "https://iotx-eu.meross.com")

# Comma-separated device name filter — empty = all devices (per account)
_raw_names   = os.environ.get("MEROSS_DEVICE_NAMES", os.environ.get("MEROSS_DEVICE_NAME", ""))
DEVICE_NAMES = [n.strip() for n in _raw_names.split(",") if n.strip()]


@dataclass
class AccountConfig:
    email:    str
    password: str
    alias:    str   # short label used in Prometheus metrics


def parse_accounts() -> list:
    """
    Parse account credentials from environment variables.

    Tries numbered vars first (MEROSS_EMAIL_1, MEROSS_EMAIL_2, ...),
    then falls back to the single-account vars (MEROSS_EMAIL / MEROSS_PASSWORD).
    """
    accounts = []
    i = 1
    while True:
        email    = os.environ.get(f"MEROSS_EMAIL_{i}")
        password = os.environ.get(f"MEROSS_PASSWORD_{i}")
        if not email or not password:
            break
        alias = os.environ.get(f"MEROSS_ALIAS_{i}", email.split("@")[0])
        accounts.append(AccountConfig(email=email, password=password, alias=alias))
        i += 1

    # Single-account fallback
    if not accounts:
        email    = os.environ.get("MEROSS_EMAIL")
        password = os.environ.get("MEROSS_PASSWORD")
        if email and password:
            alias = os.environ.get("MEROSS_ALIAS", email.split("@")[0])
            accounts.append(AccountConfig(email=email, password=password, alias=alias))

    return accounts


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
BASE   = ["account", "device"]
SWITCH = Gauge("meross_switch_state",     "Outlet on/off (1=on, 0=off)",              [*BASE, "channel"])
POWER  = Gauge("meross_power_watts",      "Instantaneous power draw in watts",         BASE)
VOLT   = Gauge("meross_voltage_volts",    "Voltage in volts",                          BASE)
AMP    = Gauge("meross_current_amps",     "Current draw in amperes",                   BASE)
ONLINE = Gauge("meross_device_online",    "Device reachable via Meross (1=yes, 0=no)", BASE)
ENERGY = Gauge("meross_energy_today_kwh", "Energy consumed today in kWh",              BASE)


# ---------------------------------------------------------------------------
# Polling helpers
# ---------------------------------------------------------------------------

def _is_on(dev, channel: int) -> bool:
    """Version-safe wrapper for dev.is_on()."""
    if hasattr(dev, "is_on"):
        return bool(dev.is_on(channel=channel))
    try:
        return bool(dev.channels[channel].is_on)
    except Exception:
        return False


async def poll_account(manager: MerossManager, alias: str) -> None:
    """Fetch and publish metrics for all devices on one account."""
    if DEVICE_NAMES:
        devices = []
        for name_filter in DEVICE_NAMES:
            devices.extend(manager.find_devices(device_name=name_filter))
    else:
        devices = manager.find_devices()

    if not devices:
        log.warning("[%s] No devices found", alias)
        return

    for dev in devices:
        name      = dev.name
        is_online = dev.online_status == OnlineStatus.ONLINE
        ONLINE.labels(account=alias, device=name).set(1 if is_online else 0)

        if not is_online:
            log.warning("[%s] Device '%s' is offline", alias, name)
            POWER.labels(account=alias, device=name).set(0)
            AMP.labels(account=alias,   device=name).set(0)
            for ch in range(len(dev.channels)):
                SWITCH.labels(account=alias, device=name, channel=str(ch)).set(0)
            continue

        try:
            await dev.async_update()

            # Switch state — one entry per channel
            for ch in range(len(dev.channels)):
                SWITCH.labels(account=alias, device=name, channel=str(ch)).set(
                    1 if _is_on(dev, ch) else 0
                )

            # Instantaneous electricity
            from meross_iot.controller.mixins.electricity import ElectricityMixin
            if isinstance(dev, ElectricityMixin):
                reading = (
                    await dev.async_get_instant_metrics()
                    if hasattr(dev, "async_get_instant_metrics")
                    else await dev.async_get_electricity()
                )
                POWER.labels(account=alias, device=name).set(reading.power)
                VOLT.labels(account=alias,  device=name).set(reading.voltage)
                AMP.labels(account=alias,   device=name).set(reading.current)
                log.info(
                    "[%s] %-20s  %.1f W  %.1f V  %.3f A  [%s]",
                    alias, name,
                    reading.power, reading.voltage, reading.current,
                    "ON" if _is_on(dev, 0) else "OFF",
                )
            else:
                log.warning("[%s] Device '%s' does not expose electricity metrics", alias, name)

            # Daily energy consumption (optional — not all models support this)
            try:
                from meross_iot.controller.mixins.consumption import ConsumptionXMixin
                if isinstance(dev, ConsumptionXMixin):
                    records = await dev.async_get_daily_power_consumption()
                    if records:
                        ENERGY.labels(account=alias, device=name).set(
                            records[-1].total_consumption_kwh
                        )
            except Exception as exc:
                log.debug("[%s] Daily consumption unavailable for '%s': %s", alias, name, exc)

        except Exception as exc:
            log.error("[%s] Failed to poll '%s': %s", alias, name, exc)
            ONLINE.labels(account=alias, device=name).set(0)


# ---------------------------------------------------------------------------
# Per-account connection loop
# ---------------------------------------------------------------------------

async def run_account(cfg: AccountConfig) -> None:
    """
    Outer retry loop for a single account.
    Reconnects automatically after any connection failure.
    """
    while True:
        manager     = None
        http_client = None
        try:
            log.info("[%s] Connecting to %s ...", cfg.alias, API_URL)
            http_client = await MerossHttpClient.async_from_user_password(
                email=cfg.email,
                password=cfg.password,
                api_base_url=API_URL,
            )
            manager = MerossManager(http_client=http_client)
            await manager.async_init()
            await manager.async_device_discovery()

            found = manager.find_devices()
            log.info("[%s] Discovery complete — %d device(s):", cfg.alias, len(found))
            for d in found:
                log.info("[%s]   * %-24s  type=%-12s  online=%s",
                         cfg.alias, d.name, d.type, d.online_status)

            while True:
                await poll_account(manager, cfg.alias)
                await asyncio.sleep(POLL_SECONDS)

        except asyncio.CancelledError:
            log.info("[%s] Shutting down", cfg.alias)
            break
        except Exception as exc:
            log.error("[%s] Connection error: %s — retrying in %d s",
                      cfg.alias, exc, RECONNECT_SECONDS)
        finally:
            if manager:
                manager.close()
            if http_client:
                try:
                    await http_client.async_logout()
                except Exception:
                    pass

        await asyncio.sleep(RECONNECT_SECONDS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_exporter() -> None:
    accounts = parse_accounts()
    if not accounts:
        log.error(
            "No accounts configured. Set MEROSS_EMAIL_1 / MEROSS_PASSWORD_1 "
            "(or MEROSS_EMAIL / MEROSS_PASSWORD for a single account)."
        )
        return

    log.info("Starting exporter for %d account(s): %s",
             len(accounts), ", ".join(a.alias for a in accounts))

    # Run all accounts concurrently — each has its own reconnect loop
    await asyncio.gather(*[run_account(cfg) for cfg in accounts])


def main() -> None:
    start_http_server(HTTP_PORT)
    log.info("Metrics endpoint: http://0.0.0.0:%d/metrics", HTTP_PORT)
    asyncio.run(run_exporter())


if __name__ == "__main__":
    main()