#!/usr/bin/env python3
"""Prometheus exporter for a Prusa MK4S via the local PrusaLink API.

Read-only. Scrapes `GET /api/v1/status` on every Prometheus scrape (pull model),
so each scrape reflects the printer's live state. No control endpoints are used.

The /api/v1/status endpoint returns data even while idle (unlike /api/v1/job),
including nozzle/bed temperatures and printer state, plus a `job` object while a
print is running.

Authentication depends on your firmware -- provide ONE of:
  * PRUSA_API_KEY  -> sent as the `X-Api-Key` header, or
  * PRUSA_PASSWORD -> HTTP Digest auth with username `maker` (PRUSA_USERNAME).
Find both on the printer: Settings -> Network -> PrusaLink. Test which one your
firmware accepts before deploying:
  curl -s -H "X-Api-Key: $KEY"        http://$PRUSA_HOST/api/v1/status | jq
  curl -s --digest -u maker:$PASSWORD http://$PRUSA_HOST/api/v1/status | jq

Config via environment variables:
  PRUSA_HOST      Printer IP or host (e.g. 192.168.178.xx, or pygmalion.home).
                  Append :PORT only if PrusaLink runs on a non-default port.
  PRUSA_API_KEY   PrusaLink API key (preferred if set).
  PRUSA_PASSWORD  PrusaLink password (used if no API key is set).
  PRUSA_USERNAME  Digest-auth username (default: maker).
  PRINTER_NAME    Value for the `printer` label (default: pygmalion).
  EXPORTER_PORT   Port to serve /metrics on (default: 9118).
  SCRAPE_TIMEOUT  HTTP timeout in seconds (default: 5).
"""

import os
import time

import requests
from requests.auth import HTTPDigestAuth
from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

PRUSA_HOST = os.environ["PRUSA_HOST"]
PRUSA_API_KEY = os.environ.get("PRUSA_API_KEY", "")
PRUSA_PASSWORD = os.environ.get("PRUSA_PASSWORD", "")
PRUSA_USERNAME = os.environ.get("PRUSA_USERNAME", "maker")
PRINTER_NAME = os.environ.get("PRINTER_NAME", "pygmalion")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9118"))
SCRAPE_TIMEOUT = float(os.environ.get("SCRAPE_TIMEOUT", "5"))

STATUS_URL = f"http://{PRUSA_HOST}/api/v1/status"

# States PrusaLink can report. Emitted as an enum: exactly one series is 1, the
# rest 0, so Grafana/alerts can match on the `state` label. Unknown live states
# are added on the fly, so new firmware states won't silently disappear.
KNOWN_STATES = [
    "IDLE", "READY", "PRINTING", "PAUSED",
    "FINISHED", "STOPPED", "ATTENTION", "ERROR", "BUSY",
]


def _request_kwargs():
    """Build headers/auth for the status request based on the credential given.

    Prefers the API key (stateless header); falls back to digest auth with the
    printer's password.
    """
    if PRUSA_API_KEY:
        return {"headers": {"X-Api-Key": PRUSA_API_KEY}, "auth": None}
    if PRUSA_PASSWORD:
        return {"headers": {}, "auth": HTTPDigestAuth(PRUSA_USERNAME, PRUSA_PASSWORD)}
    raise SystemExit("Set PRUSA_API_KEY or PRUSA_PASSWORD (see Settings -> Network -> PrusaLink).")


class PrusaCollector:
    """Fetches /api/v1/status once per Prometheus scrape and yields metrics."""

    def collect(self):
        labels = ["printer"]
        lv = [PRINTER_NAME]

        up = GaugeMetricFamily(
            "prusa_up",
            "1 if PrusaLink was reachable and returned valid JSON on this scrape, else 0",
            labels=labels,
        )

        try:
            kwargs = _request_kwargs()
            resp = requests.get(STATUS_URL, timeout=SCRAPE_TIMEOUT, **kwargs)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            # Reachability/auth failure -> report down and emit nothing else, so
            # the dashboard shows a gap instead of a stale-but-green value.
            up.add_metric(lv, 0.0)
            yield up
            return

        up.add_metric(lv, 1.0)
        yield up

        printer = data.get("printer") or {}
        job = data.get("job") or {}

        # --- Printer state enum ---------------------------------------------
        current_state = str(printer.get("state", "")).upper()
        state_metric = GaugeMetricFamily(
            "prusa_printer_state",
            "Printer state (1 for the active state, 0 otherwise)",
            labels=["printer", "state"],
        )
        states = set(KNOWN_STATES)
        if current_state:
            states.add(current_state)
        for st in sorted(states):
            state_metric.add_metric(
                [PRINTER_NAME, st], 1.0 if st == current_state else 0.0
            )
        yield state_metric

        # --- Simple numeric gauges ------------------------------------------
        def gauge(name, doc, key, src):
            value = src.get(key)
            if value is None:
                return None
            g = GaugeMetricFamily(name, doc, labels=labels)
            g.add_metric(lv, float(value))
            return g

        numeric = [
            # Temperatures
            ("prusa_temp_nozzle_celsius", "Nozzle temperature (C)", "temp_nozzle", printer),
            ("prusa_target_nozzle_celsius", "Nozzle target temperature (C)", "target_nozzle", printer),
            ("prusa_temp_bed_celsius", "Heatbed temperature (C)", "temp_bed", printer),
            ("prusa_target_bed_celsius", "Heatbed target temperature (C)", "target_bed", printer),
            # Motion / rates
            ("prusa_axis_z_mm", "Z axis height (mm)", "axis_z", printer),
            ("prusa_flow_percent", "Flow rate (%)", "flow", printer),
            ("prusa_speed_percent", "Speed factor (%)", "speed", printer),
            ("prusa_fan_hotend_rpm", "Hotend fan (RPM)", "fan_hotend", printer),
            ("prusa_fan_print_rpm", "Print (part) fan (RPM)", "fan_print", printer),
            # Job (present only while a job exists)
            ("prusa_job_progress_percent", "Print progress (%)", "progress", job),
            ("prusa_job_time_remaining_seconds", "Estimated time remaining (s)", "time_remaining", job),
            ("prusa_job_time_printing_seconds", "Elapsed print time (s)", "time_printing", job),
        ]
        for name, doc, key, src in numeric:
            metric = gauge(name, doc, key, src)
            if metric is not None:
                yield metric


def main():
    _request_kwargs()  # fail fast if no credential is configured
    REGISTRY.register(PrusaCollector())
    start_http_server(EXPORTER_PORT)
    print(
        f"prusa-exporter serving :{EXPORTER_PORT}/metrics "
        f"for {PRINTER_NAME} -> {STATUS_URL}",
        flush=True,
    )
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
