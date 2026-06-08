#!/usr/bin/env python3
"""
Viessmann heating curve control API
Accepts HTTP POST requests from Grafana to set heating parameters
via vcontrold on the local KW2 controller.
"""

import subprocess
import os
import time
import threading
import json as json_lib
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify
from flask_cors import CORS


app = Flask(__name__)
CORS(app)

# Static bearer token for basic auth — set via environment variable
API_TOKEN = os.environ.get("VIESSMANN_API_TOKEN", "changeme")

# Safe value ranges to prevent accidental misconfiguration
LIMITS = {
    "neigung": (0.2, 3.5),
    "niveau":  (-13.0, 13.0),
}

# Temperature setpoint limits
TEMP_LIMITS = {
    "ww_soll":     (40.0, 60.0),
    "raum_nor_m1": (15.0, 22.0),
    "raum_red_m1": (10.0, 20.0),
}

# Valid operating modes for KW2 — maps display name to vclient enum value
BETRIEBSART_MODES = {
    "ww":       "WW",       # Hot water only
    "red":      "RED",      # Reduced / setback
    "norm":     "NORM",     # Normal heating
    "hww":      "H+WW",     # Heating + hot water
    "abschalt": "ABSCHALT", # Shutdown
}

VCLIENT  = "/usr/local/bin/vclient"
VHOST    = "127.0.0.1:3002"
LOG_FILE = Path("/var/log/viessmann-control.log.json")

# Global lock — vcontrold accepts only one connection at a time
_vclient_lock = threading.Lock()


# ── Helpers ───────────────────────────────────────────────────────────────────

def check_auth(req):
    auth = req.headers.get("Authorization", "")
    return auth == f"Bearer {API_TOKEN}"


def vclient_set(command, value):
    """Run vclient set command followed by a read-back to confirm.

    Uses a non-blocking lock to reject concurrent calls immediately —
    vcontrold only accepts one connection at a time. Retries up to 3 times
    to handle contention with the exporter cron (~30 s per run).
    """
    if not _vclient_lock.acquire(blocking=False):
        return False, "Another command is already running — please wait"
    try:
        last_error = "vclient error"
        for attempt in range(3):
            result = subprocess.run(
                [VCLIENT, "-h", VHOST, "-c", f"{command} {value}"],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            last_error = result.stderr.strip() or result.stdout.strip() or "vclient error"
            if attempt < 2:
                time.sleep(12)  # Wait out the exporter cycle before retrying
        return False, last_error
    finally:
        _vclient_lock.release()


def vclient_get(command):
    """Read current value via vclient.

    No lock — reads are fire-and-forget. The lock only guards set commands
    against each other. The exporter cron runs outside Flask entirely, so
    the lock never prevented read/write overlap at the vcontrold level anyway.
    """
    result = subprocess.run(
        [VCLIENT, "-h", VHOST, "-c", command],
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return None
    # Output format: "getNeigungM1:\n1.300000\n"
    lines = result.stdout.strip().splitlines()
    return lines[-1].split()[0] if lines else None


def vclient_get_multi(commands):
    """Read multiple values in a single vclient connection.

    Uses -c "cmd1,cmd2,..." with -j for JSON output — one connection, fast.
    Output format: {"getNeigungM1": 1.3, "getNiveauM1": 3.0, ...}

    NOTE: -j converts enum types (e.g. Betriebsart) to raw numeric bytes.
    Any command in the ENUM_COMMANDS set is fetched separately as plain text
    to preserve the string value (e.g. "H+WW").
    """
    import json as _json

    ENUM_COMMANDS = {"getBetriebArtM1", "getBetriebArtM2"}

    numeric_cmds = [c for c in commands if c not in ENUM_COMMANDS]
    enum_cmds    = [c for c in commands if c in ENUM_COMMANDS]
    result_dict  = {}

    # Batch numeric values via -j
    if numeric_cmds:
        res = subprocess.run(
            [VCLIENT, "-h", VHOST, "-j", "-c", ",".join(numeric_cmds)],
            capture_output=True, text=True, timeout=60
        )
        if res.returncode == 0:
            try:
                data = _json.loads(res.stdout)
                # Flat dict: {"getNeigungM1": 1.3, ...}
                result_dict.update({k: str(v) for k, v in data.items()})
            except Exception:
                pass

    # Enum values via plain text call (preserves "H+WW" etc.)
    if enum_cmds:
        res = subprocess.run(
            [VCLIENT, "-h", VHOST, "-c", ",".join(enum_cmds)],
            capture_output=True, text=True, timeout=60
        )
        if res.returncode == 0:
            lines = res.stdout.strip().splitlines()
            current_cmd = None
            for line in lines:
                line = line.strip()
                if line.endswith(":"):
                    current_cmd = line[:-1]
                elif current_cmd and line:
                    result_dict[current_cmd] = line.split()[0]
                    current_cmd = None

    return {cmd: result_dict.get(cmd) for cmd in commands}


def write_log(action, value, current):
    """Append a control action to the persistent JSON log (max 200 entries)."""
    entry = {
        "ts":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action":  action,
        "set":     str(value),
        "current": str(current)
    }
    entries = []
    if LOG_FILE.exists():
        try:
            entries = json_lib.loads(LOG_FILE.read_text())
        except Exception:
            entries = []
    entries.append(entry)
    LOG_FILE.write_text(json_lib.dumps(entries[-200:]))


# ── Routes ────────────────────────────────────────────────────────────────────


@app.route("/status", methods=["GET"])
def get_status():
    """Return all current heating parameter values in a single call."""
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    vals = vclient_get_multi([
        "getNeigungM1", "getNiveauM1", "getBetriebArtM1",
        "getTempWWsoll", "getTempRaumNorSollM1", "getTempRaumRedSollM1",
    ])
    return jsonify({
        "neigung":     vals.get("getNeigungM1"),
        "niveau":      vals.get("getNiveauM1"),
        "betriebsart": vals.get("getBetriebArtM1"),
        "ww_soll":     vals.get("getTempWWsoll"),
        "raum_nor":    vals.get("getTempRaumNorSollM1"),
        "raum_red":    vals.get("getTempRaumRedSollM1"),
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/log", methods=["GET"])
def get_log():
    """Return control action log, newest first."""
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    if not LOG_FILE.exists():
        return jsonify([])
    try:
        entries = json_lib.loads(LOG_FILE.read_text())
        return jsonify(list(reversed(entries)))
    except Exception:
        return jsonify([])


@app.route("/set/neigung", methods=["POST"])
def set_neigung():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "value" not in data:
        return jsonify({"error": "Missing value"}), 400
    try:
        value = float(data["value"])
    except ValueError:
        return jsonify({"error": "Invalid value"}), 400
    lo, hi = LIMITS["neigung"]
    if not lo <= value <= hi:
        return jsonify({"error": f"Value out of range ({lo}–{hi})"}), 400
    # Round to 1 decimal — KW2 only accepts 0.1 steps
    value = round(value, 1)
    success, msg = vclient_set("setNeigungM1", value)
    if not success:
        return jsonify({"error": msg}), 500
    current = vclient_get("getNeigungM1")
    write_log("Neigung M1", value, current)
    return jsonify({"set": value, "current": current, "message": "Neigung updated"})


@app.route("/set/niveau", methods=["POST"])
def set_niveau():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "value" not in data:
        return jsonify({"error": "Missing value"}), 400
    try:
        value = float(data["value"])
    except ValueError:
        return jsonify({"error": "Invalid value"}), 400
    lo, hi = LIMITS["niveau"]
    if not lo <= value <= hi:
        return jsonify({"error": f"Value out of range ({lo}–{hi})"}), 400
    value = round(value, 1)
    success, msg = vclient_set("setNiveauM1", value)
    if not success:
        return jsonify({"error": msg}), 500
    current = vclient_get("getNiveauM1")
    write_log("Niveau M1", value, current)
    return jsonify({"set": value, "current": current, "message": "Niveau updated"})


@app.route("/set/betriebsart", methods=["POST"])
def set_betriebsart():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "mode" not in data:
        return jsonify({"error": "Missing mode"}), 400
    mode_key = data["mode"].lower()
    if mode_key not in BETRIEBSART_MODES:
        valid = list(BETRIEBSART_MODES.keys())
        return jsonify({"error": f"Invalid mode. Valid: {valid}"}), 400
    vclient_value = BETRIEBSART_MODES[mode_key]
    success, msg = vclient_set("setBetriebArtM1", vclient_value)
    if not success:
        return jsonify({"error": msg}), 500
    current = vclient_get("getBetriebArtM1")
    write_log("Betriebsart M1", vclient_value, current)
    return jsonify({"set": vclient_value, "current": current, "message": "Betriebsart updated"})


@app.route("/set/ww_soll", methods=["POST"])
def set_ww_soll():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "value" not in data:
        return jsonify({"error": "Missing value"}), 400
    try:
        value = round(float(data["value"]), 1)
    except ValueError:
        return jsonify({"error": "Invalid value"}), 400
    lo, hi = TEMP_LIMITS["ww_soll"]
    if not lo <= value <= hi:
        return jsonify({"error": f"Value out of range ({lo}–{hi})"}), 400
    success, msg = vclient_set("setTempWWsoll", value)
    if not success:
        return jsonify({"error": msg}), 500
    current = vclient_get("getTempWWsoll")
    write_log("WW Soll", value, current)
    return jsonify({"set": value, "current": current, "message": "WW Soll updated"})


@app.route("/set/raum_nor_m1", methods=["POST"])
def set_raum_nor_m1():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "value" not in data:
        return jsonify({"error": "Missing value"}), 400
    try:
        value = round(float(data["value"]), 1)
    except ValueError:
        return jsonify({"error": "Invalid value"}), 400
    lo, hi = TEMP_LIMITS["raum_nor_m1"]
    if not lo <= value <= hi:
        return jsonify({"error": f"Value out of range ({lo}–{hi})"}), 400
    success, msg = vclient_set("setTempRaumNorSollM1", value)
    if not success:
        return jsonify({"error": msg}), 500
    current = vclient_get("getTempRaumNorSollM1")
    write_log("Raumsoll Normal M1", value, current)
    return jsonify({"set": value, "current": current, "message": "Raumsoll Normal updated"})


@app.route("/set/raum_red_m1", methods=["POST"])
def set_raum_red_m1():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json()
    if not data or "value" not in data:
        return jsonify({"error": "Missing value"}), 400
    try:
        value = round(float(data["value"]), 1)
    except ValueError:
        return jsonify({"error": "Invalid value"}), 400
    lo, hi = TEMP_LIMITS["raum_red_m1"]
    if not lo <= value <= hi:
        return jsonify({"error": f"Value out of range ({lo}–{hi})"}), 400
    success, msg = vclient_set("setTempRaumRedSollM1", value)
    if not success:
        return jsonify({"error": msg}), 500
    current = vclient_get("getTempRaumRedSollM1")
    write_log("Raumsoll Reduziert M1", value, current)
    return jsonify({"set": value, "current": current, "message": "Raumsoll Reduziert updated"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)