#!/usr/bin/env python3
"""
board.py (JobIris)

Small internal web UI for the seen_jobs SQLite table: sortable table of all
jobs JobIris has found, with an inline status dropdown (Neu / Interessant /
Beworben / Abgelehnt). "Abgelehnt" is hidden from the default view but stays
in the database, so the dedup logic in job-monitor.py is unaffected.

Manual runs (daily/weekly schedule) can be triggered from the UI. Output is
accumulated in memory and polled every 2 seconds by the browser - no SSE or
WebSocket required, works through any proxy.

Templates live in templates/ next to this file:
  templates/board.html  - main job table with metrics bar
  templates/run.html    - live run output page

Intended to run as a long-lived Docker service behind Caddy on a .home
domain - not exposed publicly.

Usage:
    python3 board.py
"""

import os
import shlex
import sqlite3
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, url_for

DEFAULT_DB = Path("/mnt/vault/jobiris/seen_jobs.db")
DB_PATH = Path(os.environ.get("JOBIRIS_DB", str(DEFAULT_DB)))
PORT = int(os.environ.get("JOBIRIS_BOARD_PORT", "8042"))
COMPOSE_PROJECT_DIR = os.environ.get("JOBIRIS_COMPOSE_DIR", "/app")

STATUS_OPTIONS = ["Neu", "Interessant", "Beworben", "Abgelehnt"]

# Maps URL-friendly sort keys to actual column names (whitelist to avoid
# building ORDER BY from unvalidated input).
SORTABLE_COLUMNS = {
    "date":     "first_seen",
    "title":    "titel",
    "company":  "arbeitgeber",
    "location": "ort",
    "distance": "distance_km",
    "salary":   "salary_from",
    "tag":      "tag",
    "status":   "status",
}

app = Flask(__name__, template_folder="templates")

# In-memory run registry: run_id -> {"lines": [...], "done": bool, "rc": int|None}
_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_jobs (
            refnr       TEXT PRIMARY KEY,
            titel       TEXT,
            arbeitgeber TEXT,
            ort         TEXT,
            url         TEXT,
            distance_km INTEGER,
            distance    TEXT,
            salary      TEXT,
            salary_from INTEGER,
            home_office TEXT,
            profile     TEXT,
            tag         TEXT,
            status      TEXT DEFAULT 'Neu',
            first_seen  TEXT
        )
        """
    )
    conn.commit()

    # Migrate existing databases that predate the salary_from column
    existing = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    for col, definition in {"salary_from": "INTEGER"}.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {col} {definition}")
    conn.commit()

    return conn


def get_metrics(conn: sqlite3.Connection) -> dict:
    """Compute board metrics from the seen_jobs table for the metrics bar."""
    today = conn.execute(
        "SELECT substr(MAX(first_seen), 1, 10) FROM seen_jobs"
    ).fetchone()[0]

    rows = conn.execute(
        """
        SELECT
            COUNT(*)                                             AS total,
            SUM(CASE WHEN substr(first_seen,1,10) = ? THEN 1 ELSE 0 END) AS today,
            SUM(CASE WHEN status = 'Neu'          THEN 1 ELSE 0 END) AS neu,
            SUM(CASE WHEN status = 'Interessant'  THEN 1 ELSE 0 END) AS interessant,
            SUM(CASE WHEN status = 'Beworben'     THEN 1 ELSE 0 END) AS beworben,
            SUM(CASE WHEN status = 'Abgelehnt'    THEN 1 ELSE 0 END) AS abgelehnt,
            SUM(CASE WHEN salary_from IS NOT NULL  THEN 1 ELSE 0 END) AS with_salary,
            SUM(CASE WHEN home_office IS NOT NULL  THEN 1 ELSE 0 END) AS with_homeoffice
        FROM seen_jobs
        """,
        (today,),
    ).fetchone()

    return {
        "total":          rows["total"] or 0,
        "today":          rows["today"] or 0,
        "neu":            rows["neu"] or 0,
        "interessant":    rows["interessant"] or 0,
        "beworben":       rows["beworben"] or 0,
        "abgelehnt":      rows["abgelehnt"] or 0,
        "with_salary":    rows["with_salary"] or 0,
        "with_homeoffice":rows["with_homeoffice"] or 0,
    }


# --------------------------------------------------------------------------- #
# Manual run (polling-based)
# --------------------------------------------------------------------------- #

def _execute_run(run_id: str, schedule: str, dry_run: bool) -> None:
    """Runs job-monitor.py in a background thread, appending output lines to
    the run's entry in _runs. Sets 'done' to True when the process exits."""
    cmd = [
        "docker", "compose",
        "--project-directory", COMPOSE_PROJECT_DIR,
        "--project-name", "jobiris",
        "run", "--rm", "jobiris-monitor",
        "python3", "/app/job-monitor.py",
        "--schedule", schedule,
    ]
    if dry_run:
        cmd.append("--dry-run")

    def append(line: str):
        with _runs_lock:
            _runs[run_id]["lines"].append(line)

    append(f"$ {shlex.join(cmd)}\n")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        for line in proc.stdout:
            append(line)
        proc.wait()
        append(f"\n[Process exited with code {proc.returncode}]\n")
        rc = proc.returncode
    except Exception as exc:
        append(f"\n[Error starting process: {exc}]\n")
        rc = -1

    with _runs_lock:
        _runs[run_id]["done"] = True
        _runs[run_id]["rc"] = rc


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.route("/")
def board():
    sort_key  = request.args.get("sort", "date")
    direction = request.args.get("dir", "desc")
    show_all  = request.args.get("all") == "1"
    status_filter = request.args.get("status_filter")  # metric-bar click filter

    column       = SORTABLE_COLUMNS.get(sort_key, "first_seen")
    direction_sql = "ASC" if direction == "asc" else "DESC"

    if show_all:
        where_clause = ""
        params = []
    elif status_filter and status_filter in STATUS_OPTIONS:
        where_clause = "WHERE status = ?"
        params = [status_filter]
    else:
        where_clause = "WHERE status != 'Abgelehnt'"
        params = []

    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM seen_jobs {where_clause} "
        f"ORDER BY {column} {direction_sql} NULLS LAST",
        params,
    ).fetchall()
    latest_date = conn.execute(
        "SELECT MAX(substr(first_seen, 1, 10)) FROM seen_jobs"
    ).fetchone()[0]
    metrics = get_metrics(conn)
    conn.close()

    def toggle_dir(key):
        if key == sort_key:
            return "asc" if direction == "desc" else "desc"
        return "desc"

    return render_template(
        "board.html",
        rows=rows,
        sort_key=sort_key,
        direction=direction,
        toggle_dir=toggle_dir,
        show_all=show_all,
        status_filter=status_filter,
        latest_date=latest_date,
        status_options=STATUS_OPTIONS,
        metrics=metrics,
    )


@app.route("/status/<refnr>", methods=["POST"])
def update_status(refnr):
    new_status = request.form.get("status")
    if new_status not in STATUS_OPTIONS:
        return "Invalid status", 400

    conn = get_connection()
    conn.execute("UPDATE seen_jobs SET status = ? WHERE refnr = ?", (new_status, refnr))
    conn.commit()
    conn.close()

    return redirect(url_for(
        "board",
        sort=request.form.get("sort", "date"),
        dir=request.form.get("dir", "desc"),
        all=request.form.get("all", "0"),
    ))


@app.route("/run", methods=["POST"])
def start_run():
    schedule = request.form.get("schedule", "daily")
    if schedule not in ("daily", "weekly"):
        return "Invalid schedule", 400
    dry_run = request.form.get("dry_run") == "1"

    run_id = str(uuid.uuid4())
    with _runs_lock:
        _runs[run_id] = {"lines": [], "done": False, "rc": None}

    thread = threading.Thread(
        target=_execute_run, args=(run_id, schedule, dry_run), daemon=True
    )
    thread.start()

    return redirect(url_for("run_page", run_id=run_id))


@app.route("/run/<run_id>")
def run_page(run_id):
    with _runs_lock:
        if run_id not in _runs:
            return "Run not found", 404
    return render_template("run.html", run_id=run_id)


@app.route("/run/<run_id>/status")
def run_status(run_id):
    """JSON polling endpoint. Returns all log lines accumulated so far plus
    a 'done' flag. The browser polls this every 2 seconds."""
    with _runs_lock:
        if run_id not in _runs:
            return jsonify({"error": "not found"}), 404
        run = _runs[run_id]
        return jsonify({
            "lines": run["lines"],
            "done":  run["done"],
            "rc":    run["rc"],
        })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
