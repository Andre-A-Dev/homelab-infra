#!/usr/bin/env python3
"""rack-display collector.

Queries Prometheus and Alertmanager and serves one document:

    GET /state.json   everything the client needs to draw
    GET /healthz

No drawing, no Pillow, no fonts. The client owns every pixel, which is why
this file is a fifth of what the renderer was.

All PromQL lives here so the panels stay pure drawing code.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

LOG = logging.getLogger("rack-display-collector")

PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")
ALERTMANAGER = os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093")
HOSTS_FILE = Path(os.environ.get("HOSTS_FILE", "hosts.yml"))
REFRESH = float(os.environ.get("REFRESH_SECONDS", "15"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9119"))

# Verified against Prometheus 2026-09-10. Blocked statuses per the exporter's
# query_status label; the denominator drops in-flight states so the rate is
# not diluted by queries that have not resolved yet.
PIHOLE_BLOCKED = ('GRAVITY|GRAVITY_CNAME|REGEX|REGEX_CNAME|DENYLIST'
                  '|DENYLIST_CNAME|SPECIAL_DOMAIN'
                  '|EXTERNAL_BLOCKED_IP|EXTERNAL_BLOCKED_NULL'
                  '|EXTERNAL_BLOCKED_NXRA|EXTERNAL_BLOCKED_EDE15')
PIHOLE_PENDING = 'IN_PROGRESS|RETRIED|RETRIED_DNSSEC|DBBUSY|UNKNOWN'


# --------------------------------------------------------------------------
# prometheus access
# --------------------------------------------------------------------------

def _get(url: str, timeout: float = 6.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


def query(promql: str) -> list[dict]:
    """Instant query. Returns [] on failure — a blank panel section beats a
    dead collector."""
    url = f"{PROMETHEUS}/api/v1/query?query=" + urllib.parse.quote(promql)
    try:
        payload = _get(url)
    except Exception as exc:
        LOG.warning("query failed (%s): %s", promql[:70], exc)
        return []
    if payload.get("status") != "success":
        LOG.warning("query rejected: %s", payload.get("error"))
        return []
    return payload["data"]["result"]


def query_range(promql: str, minutes: int = 60, step: int = 300) -> list[dict]:
    """Range query for sparklines."""
    end = int(time.time())
    params = urllib.parse.urlencode({
        "query": promql, "start": end - minutes * 60, "end": end, "step": step,
    })
    try:
        payload = _get(f"{PROMETHEUS}/api/v1/query_range?{params}")
    except Exception as exc:
        LOG.warning("range query failed (%s): %s", promql[:70], exc)
        return []
    if payload.get("status") != "success":
        return []
    return payload["data"]["result"]


def by_instance(promql: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for series in query(promql):
        key = series["metric"].get("instance")
        if key is None:
            continue
        try:
            out[key] = float(series["value"][1])
        except (TypeError, ValueError):
            continue
    return out


def scalar(promql: str) -> float | None:
    result = query(promql)
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (TypeError, ValueError, KeyError):
        return None


def fetch_alerts() -> list[dict]:
    """Alertmanager rather than Prometheus ALERTS, so silences are respected."""
    url = (f"{ALERTMANAGER}/api/v2/alerts"
           "?active=true&silenced=false&inhibited=false")
    try:
        raw = _get(url)
    except Exception as exc:
        LOG.warning("alertmanager unreachable: %s", exc)
        return []
    alerts = [{
        "name": item.get("labels", {}).get("alertname", "unknown"),
        "instance": item.get("labels", {}).get("instance", ""),
        "severity": item.get("labels", {}).get("severity", "warning"),
        "summary": item.get("annotations", {}).get("summary", ""),
    } for item in raw]
    alerts.sort(key=lambda a: 0 if a["severity"] == "critical" else 1)
    return alerts


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

CPU_LINUX = ('100 - (avg by (instance) '
             '(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)')
CPU_WINDOWS = ('100 - (avg by (instance) '
               '(rate(windows_cpu_time_total{mode="idle"}[5m])) * 100)')
MEM_LINUX = ('100 * (1 - node_memory_MemAvailable_bytes '
             '/ node_memory_MemTotal_bytes)')
TEMP_LINUX = "max by (instance) (node_hwmon_temp_celsius)"


def fetch_hosts(config: dict) -> list[dict]:
    """expect is derived from the ephemeral label on the scrape target, so it
    cannot disagree with prometheus.yml."""
    up, ephemeral = {}, set()
    for series in query("up"):
        metric = series["metric"]
        instance, job = metric.get("instance"), metric.get("job")
        if instance is None:
            continue
        up[(job, instance)] = float(series["value"][1]) > 0
        if metric.get("ephemeral") == "true":
            ephemeral.add(instance)

    cpu = {**by_instance(CPU_LINUX), **by_instance(CPU_WINDOWS)}
    temp = by_instance(TEMP_LINUX)
    mem = by_instance(MEM_LINUX)

    hosts = []
    for entry in config.get("hosts", []):
        sources = entry.get("sources", [])
        live = next((s for s in sources
                     if up.get((s.get("job"), s.get("instance")))), None)
        row = {"name": entry["name"], "os": None,
               "cpu": None, "mem": None, "temp": None}
        if live is not None:
            key = live["instance"]
            row.update(state="up", os=live.get("os"), cpu=cpu.get(key),
                       mem=mem.get(key), temp=temp.get(key))
        else:
            declared = entry.get("expect")
            intermittent = (declared == "intermittent"
                            or (declared is None and any(
                                s.get("instance") in ephemeral for s in sources)))
            row["state"] = "off" if intermittent else "down"
        hosts.append(row)
    return hosts


# backup_step_status per service. 0 is success; the single service reporting
# 2 matches backup_skipped_total exactly, so 2 is "skipped". Anything else is
# treated as a failure rather than guessed at.
STEP_OK, STEP_SKIPPED = 0.0, 2.0

# Which firing alert colours which chain. First match wins, so the specific
# chains are tested before the generic backup prefix.
BACKUP_ROUTES = (
    ("offsite_state", ("offsite",)),
    ("verify_state", ("verify",)),
    ("maintenance_state", ("maintenance", "prune")),
    ("local_state", ("backup", "restic")),
)


def _age(timestamp, now: float):
    return now - timestamp if timestamp else None


def fetch_backup(alerts: list[dict]) -> dict:
    now = time.time()
    backup = {
        # The first four are kept flat because the overview panel reads them.
        "local_age": _age(scalar("backup_last_success_timestamp"), now),
        "offsite_age": _age(scalar("restic_offsite_last_success_timestamp"), now),
        "verify_age": _age(scalar("backup_verify_last_run_timestamp"), now),
        "maintenance_age": _age(
            scalar("restic_maintenance_last_run_timestamp"), now),

        "local_duration_s": scalar("backup_duration_seconds"),
        "local_exit_code": scalar("backup_last_exit_code"),
        "disk_free_gb": scalar("backup_disk_free_gb"),
        "disk_usage_pct": scalar("backup_disk_usage_percent"),
        "skipped_total": scalar("backup_skipped_total"),

        "offsite_duration_s": scalar("restic_offsite_duration_seconds"),
        "offsite_snapshots": scalar("restic_offsite_snapshot_count"),
        "offsite_bytes_added": scalar("restic_offsite_bytes_added"),
        "offsite_files_new": scalar("restic_offsite_files_new"),
        "offsite_files_changed": scalar("restic_offsite_files_changed"),

        "verify_pass": scalar("backup_verify_pass_count"),
        "verify_fail": scalar("backup_verify_fail_count"),
        "verify_warn": scalar("backup_verify_warn_count"),
        "verify_skip": scalar("backup_verify_skip_count"),
        "verify_quick": scalar("backup_verify_quick"),

        "maintenance_check_ok": scalar("restic_maintenance_check_ok"),
        "maintenance_prune_ok": scalar("restic_maintenance_prune_ok"),
        "maintenance_duration_s": scalar("restic_maintenance_duration_seconds"),

        # The blind spots: things no backup covers at all. Nothing else reports
        # these, and a non-zero value outranks every other figure on the panel.
        "uncovered_mounts": scalar("backup_uncovered_mounts"),
        "uncovered_services": scalar("backup_verify_uncovered_services"),
    }

    # Disk health lives with the backup chain rather than on a page of its
    # own: SMART attributes barely move over months, and the event that
    # actually happened here was a disk vanishing, not a value degrading.
    backup["disks_seen"] = scalar("smartctl_devices")
    backup["disks_expected"] = SMART_EXPECTED_DEVICES or None
    backup["disks_failing"] = sorted(
        r["metric"].get("device", "?")
        for r in query('smartctl_device_smart_status == 0'))

    sizes = {r["metric"].get("service"): float(r["value"][1])
             for r in query("backup_archive_size_bytes")}
    durations = {r["metric"].get("service"): float(r["value"][1])
                 for r in query("backup_step_duration_seconds")}
    statuses = {r["metric"].get("service"): float(r["value"][1])
                for r in query("backup_step_status")}

    services = []
    for name in sorted(set(sizes) | set(statuses)):
        if not name:
            continue
        status = statuses.get(name, STEP_OK)
        services.append({
            "name": name,
            "size_bytes": sizes.get(name),
            "size_text": _bytes_text(sizes[name]) if name in sizes else None,
            "duration_s": durations.get(name),
            "state": ("up" if status == STEP_OK
                      else "warning" if status == STEP_SKIPPED else "critical"),
            "note": (None if status == STEP_OK
                     else "skipped" if status == STEP_SKIPPED else "failed"),
        })
    services.sort(key=lambda item: -(item["size_bytes"] or 0))
    backup["services"] = services

    # Colour comes from the alert rules, never from thresholds re-invented
    # here — two sources of truth drift, and then the panel contradicts the
    # alert that fired.
    buckets: dict[str, list[dict]] = {key: [] for key, _ in BACKUP_ROUTES}
    for alert in alerts:
        name = alert["name"].lower()
        for key, needles in BACKUP_ROUTES:
            if any(needle in name for needle in needles):
                buckets[key].append(alert)
                break
    for key, matched in buckets.items():
        if not matched:
            backup[key] = "ok"
        elif any(a["severity"] == "critical" for a in matched):
            backup[key] = "critical"
        else:
            backup[key] = "warning"

    if backup["local_exit_code"]:
        backup["local_state"] = "critical"
    if backup["verify_fail"]:
        backup["verify_state"] = "critical"
    for key, age_key in (("local_state", "local_age"),
                         ("offsite_state", "offsite_age"),
                         ("verify_state", "verify_age"),
                         ("maintenance_state", "maintenance_age")):
        if backup[key] == "ok" and backup[age_key] is None:
            backup[key] = "unknown"

    # Kept for the overview panel's compact rows.
    backup["verify_text"] = ("n/a" if backup["verify_fail"] is None
                             else f"{backup['verify_fail']:.0f} fail")
    backup["free_text"] = ("n/a" if backup["disk_free_gb"] is None
                           else f"{backup['disk_free_gb']:.0f}G")
    return backup


def fetch_rack() -> dict:
    """Ambient air and total draw of the rack.

    Lives on the overview next to the per-host CPU temperatures, because it is
    the context that explains them: a warm cellar shows up in every host at
    once, and only this reading says why.
    """
    def shelly(metric: str, device: str) -> float | None:
        return scalar(f'shelly_{metric}{{device="{device}"}}')

    temp = shelly("temperature_celsius", RACK_TEMP_DEVICE)
    state = "unknown"
    if temp is not None:
        state = ("critical" if temp >= RACK_TEMP_CRIT
                 else "warning" if temp >= RACK_TEMP_WARN else "ok")
    if not shelly("device_online", RACK_TEMP_DEVICE):
        state = "unknown"

    return {
        "temp_c": temp,
        "temp_state": state,
        "humidity_pct": shelly("humidity_percent", RACK_TEMP_DEVICE),
        "power_w": shelly("power_watts", RACK_PLUG_DEVICE),
        "energy_kwh": shelly("energy_total_kwh", RACK_PLUG_DEVICE),
        "plug_online": bool(shelly("device_online", RACK_PLUG_DEVICE)),
    }


def fetch_pihole() -> list[dict]:
    blocked = by_instance(
        f'sum by (instance) (pihole_query_by_status'
        f'{{query_status=~"{PIHOLE_BLOCKED}"}})')
    total = by_instance(
        f'sum by (instance) (pihole_query_by_status'
        f'{{query_status!~"{PIHOLE_PENDING}"}})')
    clients = by_instance("pihole_client_count")
    gravity = by_instance("pihole_domains_being_blocked")
    errors = by_instance("pihole_dns_errors_1m")
    timeouts = by_instance("pihole_dns_timeouts_1m")
    # The histogram is already a 1m window, so no rate() around it.
    latency = by_instance(
        "histogram_quantile(0.95, sum by (instance, le) "
        "(pihole_dns_latency_seconds_1m_bucket))")

    trends: dict[str, list[float]] = {}
    for series in query_range("pihole_dns_queries_processed_1m"):
        key = series["metric"].get("instance")
        if key:
            trends[key] = [float(v[1]) for v in series["values"]]

    nodes = []
    for instance in sorted(set(total) | set(clients) | {"boreas", "zephyros"}):
        has_data = instance in total and total[instance] > 0
        node = {
            "name": instance.capitalize(),
            "instance": instance,
            "has_data": has_data,
            "block_pct": (100 * blocked.get(instance, 0) / total[instance]
                          if has_data else None),
            "queries": int(total.get(instance, 0)),
            "blocked": int(blocked.get(instance, 0)),
            "clients": int(clients.get(instance, 0)) if instance in clients else None,
            "gravity": int(gravity.get(instance, 0)) if instance in gravity else None,
            "errors": errors.get(instance),
            "timeouts": timeouts.get(instance),
            "latency_ms": (latency[instance] * 1000
                           if latency.get(instance) is not None else None),
            "trend": trends.get(instance, []),
        }
        nodes.append(node)
    return nodes[:2]


def _bytes_text(value: float) -> str:
    for unit, size in (("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if value >= size:
            return f"{value / size:.1f}{unit}" if value / size < 10 else \
                   f"{value / size:.0f}{unit}"
    return f"{value:.0f}B"


def _uptime_text(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


# container_health_state is undocumented here; the value distribution and a
# cross-check against `docker ps --filter health=unhealthy` gave:
#   -1  no HEALTHCHECK defined in the image
#    0  unhealthy
#    1  healthy
HEALTH_UNHEALTHY = 0.0
HEALTH_NONE = -1.0


def fetch_containers() -> dict:
    now = time.time()

    projects, current = {}, set()
    for series in query('container_last_seen{name!=""}'):
        metric = series["metric"]
        name = metric.get("name")
        if not name:
            continue
        current.add(name)
        projects[name] = metric.get(
            "container_label_com_docker_compose_project", "")

    # cAdvisor only reports running containers, so one that dies simply
    # disappears instead of turning red — and the running count quietly drops
    # by one. Comparing against an hour ago catches exactly that, with no list
    # to maintain. A container removed on purpose ages out by itself.
    previous = {s["metric"].get("name")
                for s in query('container_last_seen{name!=""} offset 1h')}
    gone = sorted(n for n in previous - current if n)

    starts = {s["metric"].get("name"): float(s["value"][1])
              for s in query('container_start_time_seconds{name!=""}')}
    ooms = {s["metric"].get("name"): float(s["value"][1])
            for s in query(
                'sum by (name) (increase(container_oom_events_total{name!=""}[1h]))')
            if float(s["value"][1]) > 0}
    health = {s["metric"].get("name"): float(s["value"][1])
              for s in query('container_health_state{name!=""}')}

    items = []
    for name in current:
        started = starts.get(name)
        age = now - started if started else None
        state = "up"
        note = None
        if ooms.get(name):
            state, note = "critical", f"oom killed {ooms[name]:.0f}x"
        elif health.get(name, HEALTH_NONE) == HEALTH_UNHEALTHY:
            state, note = "warning", "unhealthy"
        elif age is not None and age < 3600:
            state, note = "warning", f"restarted {_uptime_text(age)} ago"
        items.append({"name": name, "project": projects.get(name, ""),
                      "state": state, "note": note,
                      "uptime": _uptime_text(age) if age is not None else "?"})

    for name in gone:
        items.append({"name": name, "project": projects.get(name, "zz"),
                      "state": "gone", "note": "gone", "uptime": "-"})

    # Stable ordering: exceptions are found by colour, not by position, and a
    # list that reshuffles on every incident is one you can never learn.
    items.sort(key=lambda c: (c["project"], c["name"]))

    used = scalar('node_memory_MemTotal_bytes{instance="mnemosyne"} '
                  '- node_memory_MemAvailable_bytes{instance="mnemosyne"}')
    swap = scalar('node_memory_SwapTotal_bytes{instance="mnemosyne"} '
                  '- node_memory_SwapFree_bytes{instance="mnemosyne"}')
    # PSI: share of time something stalled waiting for memory. Unlike a
    # "largest consumer" ranking this actually changes, and it changes exactly
    # when the machine is in trouble.
    pressure = scalar('rate(container_pressure_memory_stalled_seconds_total'
                      '{id="/"}[5m]) * 100')
    biggest = query('topk(1, container_memory_working_set_bytes{name!=""})')

    return {
        "running": len(current),
        "items": items,
        "gone": gone,
        "mem_text": _bytes_text(used) if used else "n/a",
        "swap_text": _bytes_text(swap) if swap else "0",
        "pressure_pct": pressure,
        "top_name": biggest[0]["metric"].get("name") if biggest else None,
        "top_mem_text": (_bytes_text(float(biggest[0]["value"][1]))
                         if biggest else None),
    }


# prusa_printer_state is one series per state with a 0/1 value, verified
# 2026-09-13 while printing. Nine states exist; these are the ones that change
# how the panel should read.
PRUSA_ATTENTION = {"ATTENTION", "ERROR", "STOPPED"}
PRUSA_PAUSED = {"PAUSED"}
PRUSA_ACTIVE = {"PRINTING"}


def fetch_prusa() -> dict:
    if not scalar("prusa_up"):
        return {"up": False, "state": "offline", "severity": "off"}

    active = query("prusa_printer_state == 1")
    state = active[0]["metric"].get("state", "UNKNOWN") if active else "UNKNOWN"
    printer = (active[0]["metric"].get("printer") if active else None)

    if state in PRUSA_ATTENTION:
        severity = "critical"
    elif state in PRUSA_PAUSED:
        severity = "warning"
    elif state in PRUSA_ACTIVE:
        severity = "ok"
    else:
        severity = "idle"

    progress = scalar("prusa_job_progress_percent")
    return {
        "up": True,
        "state": state.lower(),
        "severity": severity,
        "printer": printer,
        "progress": (progress or 0) / 100,
        "nozzle": scalar("prusa_temp_nozzle_celsius"),
        "nozzle_target": scalar("prusa_target_nozzle_celsius"),
        "bed": scalar("prusa_temp_bed_celsius"),
        "bed_target": scalar("prusa_target_bed_celsius"),
        "remaining_s": scalar("prusa_job_time_remaining_seconds"),
        "printing_s": scalar("prusa_job_time_printing_seconds"),
        "speed_pct": scalar("prusa_speed_percent"),
        "flow_pct": scalar("prusa_flow_percent"),
        "z_mm": scalar("prusa_axis_z_mm"),
    }


ALERT_WINDOW_S = 86400
ALERT_STEP_S = 300


def fetch_alert_history() -> dict:
    """Reconstruct the last 24h from the ALERTS series.

    Alertmanager keeps no history, so this comes from Prometheus. ALERTS knows
    nothing about silences — which is right for a history: a silence means "do
    not wake me", not "did not happen". Rows for alerts that are currently
    silenced are marked so the panel can dim them rather than hide them.
    """
    end = int(time.time())
    start = end - ALERT_WINDOW_S

    silenced = set()
    try:
        for item in _get(f"{ALERTMANAGER}/api/v2/alerts?silenced=true&active=true"):
            if item.get("status", {}).get("state") == "suppressed":
                silenced.add(item.get("labels", {}).get("alertname"))
    except Exception as exc:
        LOG.warning("silence lookup failed: %s", exc)

    params = urllib.parse.urlencode({
        "query": 'ALERTS{alertstate="firing"}',
        "start": start, "end": end, "step": ALERT_STEP_S,
    })
    try:
        payload = _get(f"{PROMETHEUS}/api/v1/query_range?{params}", timeout=10)
        series = payload["data"]["result"] if payload.get("status") == "success" else []
    except Exception as exc:
        LOG.warning("alert history failed: %s", exc)
        series = []

    rows: dict[str, dict] = {}
    for item in series:
        name = item["metric"].get("alertname", "unknown")
        row = rows.setdefault(name, {
            "name": name,
            "severity": item["metric"].get("severity", "warning"),
            "silenced": name in silenced,
            "instances": set(),
            "samples": set(),
        })
        if item["metric"].get("severity") == "critical":
            row["severity"] = "critical"
        if item["metric"].get("instance"):
            row["instances"].add(item["metric"]["instance"])
        for point in item["values"]:
            row["samples"].add(int(float(point[0])))

    result = []
    for row in rows.values():
        # Samples are one step apart while firing; a bigger gap is a new
        # incident rather than one long one.
        stamps = sorted(row["samples"])
        spans, span_start, previous = [], stamps[0], stamps[0]
        for stamp in stamps[1:]:
            if stamp - previous > ALERT_STEP_S * 2:
                spans.append((span_start - start, previous - start + ALERT_STEP_S))
                span_start = stamp
            previous = stamp
        spans.append((span_start - start, previous - start + ALERT_STEP_S))
        result.append({
            "name": row["name"],
            "severity": row["severity"],
            "silenced": row["silenced"],
            "instances": sorted(row["instances"])[:2],
            "instance_count": len(row["instances"]),
            "spans": spans,
            "incidents": len(spans),
            "total_s": sum(b - a for a, b in spans),
        })

    result.sort(key=lambda r: (0 if r["severity"] == "critical" else 1,
                               -r["total_s"]))
    return {
        "window_s": ALERT_WINDOW_S,
        "start_epoch": start,
        "rows": result,
        "incidents": sum(r["incidents"] for r in result),
    }


# Only this location is reported. The second Fritzbox in the metrics belongs
# to another household and is deliberately out of scope.
NETWORK_LOCATION = os.environ.get("NETWORK_LOCATION", "home")

# How many disks smartctl should see. A fixed expectation rather than a
# rolling maximum: a window-based baseline quietly lowers itself if a disk
# stays missing, and the warning disappears without anything being fixed.
SMART_EXPECTED_DEVICES = int(os.environ.get("SMART_EXPECTED_DEVICES", "0"))

# Shelly device labels for the rack itself. Both sit on the same plug strip:
# one reports ambient air, the other the draw of everything in the rack.
RACK_TEMP_DEVICE = os.environ.get("RACK_TEMP_DEVICE", "Talos-Temp")
RACK_PLUG_DEVICE = os.environ.get("RACK_PLUG_DEVICE", "Talos-Plug")
# Chosen here, not derived from an alert rule — adjust once there is one.
RACK_TEMP_WARN, RACK_TEMP_CRIT = 35.0, 40.0


def fetch_network() -> dict:
    loc = NETWORK_LOCATION
    sel = f'{{location="{loc}"}}'

    def dsl(name: str, **labels) -> float | None:
        selector = ",".join([f'location="{loc}"'] +
                            [f'{k}="{v}"' for k, v in labels.items()])
        return scalar(f"fritz_dsl_{name}{{{selector}}}")

    rx_curr = dsl("datarate_kbps", direction="rx", type="curr")
    rx_max = dsl("datarate_kbps", direction="rx", type="max")
    tx_curr = dsl("datarate_kbps", direction="tx", type="curr")
    tx_max = dsl("datarate_kbps", direction="tx", type="max")

    # fritz_device_reachable is unusable: the exporter sets it to 0 when any
    # single capability fails, and the fibre-only GPON query always fails on a
    # DSL line. Freshness of an actual metric is the honest test instead.
    age = scalar(f'time() - timestamp(fritz_dsl_datarate_kbps'
                 f'{{location="{loc}",direction="rx",type="curr"}})')

    throughput = {}
    for direction in ("rx", "tx"):
        throughput[direction] = scalar(
            f'rate(fritz_lan_data_bytes_total'
            f'{{location="{loc}",direction="{direction}"}}[10m]) * 8 / 1e6')

    probes = []
    successes = {}
    for series in query("probe_success"):
        key = (series["metric"].get("job"), series["metric"].get("instance"))
        successes[key] = float(series["value"][1]) > 0
    durations = {}
    for series in query("probe_duration_seconds"):
        key = (series["metric"].get("job"), series["metric"].get("instance"))
        durations[key] = float(series["value"][1])
    ssl_days = {}
    for series in query("(probe_ssl_earliest_cert_expiry - time()) / 86400"):
        key = (series["metric"].get("job"), series["metric"].get("instance"))
        ssl_days[key] = float(series["value"][1])

    for (job, instance), ok in sorted(successes.items(),
                                      key=lambda kv: (kv[0][0] or "", kv[0][1] or "")):
        probes.append({
            "name": (instance or "").split("://")[-1].rstrip("/"),
            "external": "external" in (job or ""),
            "ok": ok,
            "duration_ms": (durations.get((job, instance), 0) * 1000
                            if (job, instance) in durations else None),
            "ssl_days": ssl_days.get((job, instance)),
        })

    return {
        "location": loc,
        "age_s": age,
        "rx_curr_mbps": rx_curr / 1000 if rx_curr else None,
        "rx_max_mbps": rx_max / 1000 if rx_max else None,
        "tx_curr_mbps": tx_curr / 1000 if tx_curr else None,
        "tx_max_mbps": tx_max / 1000 if tx_max else None,
        "snr_rx_db": dsl("noise_margin_dB", direction="rx"),
        "snr_tx_db": dsl("noise_margin_dB", direction="tx"),
        "att_rx_db": dsl("attenuation_dB", direction="rx"),
        "att_tx_db": dsl("attenuation_dB", direction="tx"),
        "crc_errors": dsl("crc_errors_count_total"),
        "fec_errors": dsl("fec_errors_count_total"),
        "devices": scalar(f"fritz_known_devices_count{sel}"),
        "throughput_rx_mbps": throughput["rx"],
        "throughput_tx_mbps": throughput["tx"],
        "probes": probes,
        "probes_failing": sum(1 for p in probes if not p["ok"]),
    }


# --------------------------------------------------------------------------
# builder
# --------------------------------------------------------------------------

class Builder:
    def __init__(self, config: dict, interval: float):
        self.config = config
        self.interval = interval
        self.state: dict = {}
        self.lock = threading.Lock()

    def build_once(self) -> None:
        alerts = fetch_alerts()
        hosts = fetch_hosts(self.config)
        overall = "ok"
        if any(a["severity"] == "critical" for a in alerts) or \
           any(h["state"] == "down" for h in hosts):
            overall = "critical"
        elif alerts:
            overall = "warning"

        state = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "clock": time.strftime("%H:%M"),
            "overall": overall,
            "hosts": hosts,
            "alerts": alerts,
            "backup": fetch_backup(alerts),
            "rack": fetch_rack(),
            "pihole": fetch_pihole(),
            "containers": fetch_containers(),
            "prusa": fetch_prusa(),
            "alert_history": fetch_alert_history(),
            "network": fetch_network(),
        }
        with self.lock:
            self.state = state

    def loop(self) -> None:
        while True:
            try:
                self.build_once()
            except Exception:
                LOG.exception("state build failed")
            time.sleep(self.interval)


def make_handler(builder: Builder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass  # the client polls constantly; access logs are pure noise

        def _send(self, body: bytes, content_type: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = urllib.parse.urlparse(self.path).path
            if path == "/healthz":
                self._send(b"ok", "text/plain")
            elif path == "/state.json":
                with builder.lock:
                    body = json.dumps(builder.state).encode()
                self._send(body, "application/json")
            else:
                self._send(b"not found", "text/plain", 404)

    return Handler


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    config = yaml.safe_load(HOSTS_FILE.read_text(encoding="utf-8"))
    builder = Builder(config, REFRESH)
    builder.build_once()   # serve real content on the very first request
    threading.Thread(target=builder.loop, daemon=True).start()
    LOG.info("serving state on :%d", LISTEN_PORT)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT),
                        make_handler(builder)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
