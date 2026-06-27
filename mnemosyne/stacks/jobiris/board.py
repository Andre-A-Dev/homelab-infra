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

# AI rating: Anthropic API key (optional – rating features are disabled if unset)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Path to the rating system-prompt. Lives next to board.py so it can be edited
# without touching Python code and is tracked in Git like any other config file.
RATING_PROFILE_PATH = Path(os.environ.get(
    "JOBIRIS_RATING_PROFILE",
    Path(__file__).resolve().parent / "rating-profile.txt",
))

# BA API constants (same values as job-monitor.py)
BA_API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
BA_API_KEY  = "jobboerse-jobsuche"
BA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# In-memory rating registry: refnr -> {"status": "pending"|"done"|"error",
#                                       "score": int|None, "summary": str|None}
_ratings: dict[str, dict] = {}
_ratings_lock = threading.Lock()

STATUS_OPTIONS = [
    "Neu",
    "Interessant",
    "Beworben",
    "Feedback ausstehend",
    "Absage erhalten",
    "Nicht relevant",
]

# Statuses that hide jobs from the default view
HIDDEN_STATUSES = {"Absage erhalten", "Nicht relevant"}

# Maps URL-friendly sort keys to actual column names (whitelist to avoid
# building ORDER BY from unvalidated input).
REMINDER_INTERESSANT_DAYS = int(os.environ.get("REMINDER_INTERESSANT_DAYS", "5"))
REMINDER_FOLLOWUP_DAYS   = int(os.environ.get("REMINDER_FOLLOWUP_DAYS", "14"))
BOARD_TIMEZONE           = os.environ.get("JOBIRIS_TIMEZONE", "Europe/Berlin")

SORTABLE_COLUMNS = {
    "date":      "first_seen",
    "published": "published_at",
    "title":     "titel",
    "company":   "arbeitgeber",
    "location":  "ort",
    "distance":  "distance_home_km",
    "salary":    "salary_from",
    "tag":       "tag",
    "status":    "status",
}

app = Flask(__name__, template_folder="templates")

# In-memory run registry: run_id -> {"lines": [...], "done": bool, "rc": int|None}
_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

import json as _json


def get_last_run() -> dict | None:
    """Read the last_run.json status file written by job-monitor.py.
    Returns None if the file doesn't exist yet."""
    status_path = DB_PATH.parent / "last_run.json"
    try:
        return _json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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

    # Migrate existing databases
    existing = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    for col, definition in {
        "salary_from":       "INTEGER",
        "lat":               "REAL",
        "lon":               "REAL",
        "distance_home_km":  "INTEGER",
        "distance_home":     "TEXT",
        "published_at":      "TEXT",
        "ignore_match":      "TEXT",
        "notes":             "TEXT",
        "status_changed_at": "TEXT",
        "applied_at":        "TEXT",
        "ai_score":          "INTEGER",
        "ai_summary":        "TEXT",
    }.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {col} {definition}")
    conn.commit()

    return conn


def get_metrics(conn: sqlite3.Connection) -> dict:
    """Compute board metrics from the seen_jobs table."""
    import datetime as _dt
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(BOARD_TIMEZONE)
    except Exception:
        tz = _dt.timezone.utc

    today = _dt.datetime.now(tz).date().isoformat()
    reminder_int_cutoff = (
        _dt.datetime.now(tz) - _dt.timedelta(days=REMINDER_INTERESSANT_DAYS)
    ).isoformat()
    reminder_fu_cutoff = (
        _dt.datetime.now(tz) - _dt.timedelta(days=REMINDER_FOLLOWUP_DAYS)
    ).isoformat()

    rows = conn.execute(
        """
        SELECT
            COUNT(*)                                                          AS total,
            SUM(CASE WHEN substr(first_seen,1,10) = ?      THEN 1 ELSE 0 END) AS today,
            SUM(CASE WHEN status = 'Neu'                   THEN 1 ELSE 0 END) AS neu,
            SUM(CASE WHEN status = 'Interessant'           THEN 1 ELSE 0 END) AS interessant,
            SUM(CASE WHEN status = 'Beworben'
                      OR status = 'Feedback ausstehend'    THEN 1 ELSE 0 END) AS in_progress,
            SUM(CASE WHEN status = 'Absage erhalten'       THEN 1 ELSE 0 END) AS absage,
            SUM(CASE WHEN salary_from IS NOT NULL           THEN 1 ELSE 0 END) AS with_salary,
            SUM(CASE WHEN home_office IS NOT NULL           THEN 1 ELSE 0 END) AS with_homeoffice,
            SUM(CASE WHEN status = 'Interessant'
                      AND status_changed_at < ?             THEN 1 ELSE 0 END) AS reminder_interessant,
            SUM(CASE WHEN status = 'Beworben'
                      AND status_changed_at < ?             THEN 1 ELSE 0 END) AS reminder_followup
        FROM seen_jobs
        WHERE status NOT IN ('Absage erhalten', 'Nicht relevant')
        """,
        (today, reminder_int_cutoff, reminder_fu_cutoff),
    ).fetchone()

    return {
        "total":                rows["total"] or 0,
        "today":                rows["today"] or 0,
        "neu":                  rows["neu"] or 0,
        "interessant":          rows["interessant"] or 0,
        "in_progress":          rows["in_progress"] or 0,
        "absage":               rows["absage"] or 0,
        "with_salary":          rows["with_salary"] or 0,
        "with_homeoffice":      rows["with_homeoffice"] or 0,
        "reminder_interessant": rows["reminder_interessant"] or 0,
        "reminder_followup":    rows["reminder_followup"] or 0,
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
        hidden = ", ".join(f"'{s}'" for s in HIDDEN_STATUSES)
        where_clause = f"WHERE status NOT IN ({hidden})"
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
    last_run = get_last_run()
    conn.close()

    # Compute per-row reminder flags
    import datetime as _dt, zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(BOARD_TIMEZONE)
    except Exception:
        tz = _dt.timezone.utc
    now_ts = _dt.datetime.now(tz)

    def days_since(ts_str):
        if not ts_str:
            return None
        try:
            dt = _dt.datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            return (now_ts - dt).days
        except ValueError:
            return None

    def row_reminder(row):
        if row["status"] == "Interessant":
            d = days_since(row["status_changed_at"])
            if d is not None and d >= REMINDER_INTERESSANT_DAYS:
                return f"⏰ seit {d} Tagen interessant – bewerben?"
        if row["status"] == "Beworben":
            d = days_since(row["applied_at"] or row["status_changed_at"])
            if d is not None and d >= REMINDER_FOLLOWUP_DAYS:
                return f"⏰ beworben vor {d} Tagen – nachfassen?"
        return None

    rows_with_reminders = [(row, row_reminder(row)) for row in rows]

    def toggle_dir(key):
        if key == sort_key:
            return "asc" if direction == "desc" else "desc"
        return "desc"

    return render_template(
        "board.html",
        rows=rows,
        rows_with_reminders=rows_with_reminders,
        sort_key=sort_key,
        direction=direction,
        toggle_dir=toggle_dir,
        show_all=show_all,
        status_filter=status_filter,
        latest_date=latest_date,
        status_options=STATUS_OPTIONS,
        hidden_statuses=HIDDEN_STATUSES,
        metrics=metrics,
        last_run=last_run,
        reminder_interessant_days=REMINDER_INTERESSANT_DAYS,
        reminder_followup_days=REMINDER_FOLLOWUP_DAYS,
    )


@app.route("/status/<refnr>", methods=["POST"])
def update_status(refnr):
    new_status = request.form.get("status")
    if new_status not in STATUS_OPTIONS:
        return "Invalid status", 400

    import datetime as _dt, zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(BOARD_TIMEZONE)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).isoformat()

    conn = get_connection()
    # Set applied_at only when transitioning TO "Beworben" for the first time
    current = conn.execute(
        "SELECT status, applied_at FROM seen_jobs WHERE refnr = ?", (refnr,)
    ).fetchone()

    if new_status == "Beworben" and current and not current["applied_at"]:
        conn.execute(
            "UPDATE seen_jobs SET status = ?, status_changed_at = ?, applied_at = ? WHERE refnr = ?",
            (new_status, now, now, refnr),
        )
    else:
        conn.execute(
            "UPDATE seen_jobs SET status = ?, status_changed_at = ? WHERE refnr = ?",
            (new_status, now, refnr),
        )
    conn.commit()
    conn.close()

    return redirect(url_for(
        "board",
        sort=request.form.get("sort", "date"),
        dir=request.form.get("dir", "desc"),
        all=request.form.get("all", "0"),
    ))


@app.route("/notes/<refnr>", methods=["POST"])
def update_notes(refnr):
    """Save free-text notes for a job. Called via fetch() from the board."""
    notes = request.json.get("notes", "").strip() if request.is_json else ""
    conn = get_connection()
    conn.execute("UPDATE seen_jobs SET notes = ? WHERE refnr = ?", (notes or None, refnr))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/bulk-status", methods=["POST"])
def bulk_status():
    """Set status for multiple jobs at once. Expects JSON: {refnrs: [...], status: '...'}"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "no data"}), 400
    new_status = data.get("status")
    refnrs = data.get("refnrs", [])
    if new_status not in STATUS_OPTIONS or not refnrs:
        return jsonify({"error": "invalid"}), 400

    import datetime as _dt, zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(BOARD_TIMEZONE)
    except Exception:
        tz = _dt.timezone.utc
    now = _dt.datetime.now(tz).isoformat()

    conn = get_connection()
    for refnr in refnrs:
        if new_status == "Beworben":
            existing = conn.execute(
                "SELECT applied_at FROM seen_jobs WHERE refnr = ?", (refnr,)
            ).fetchone()
            if existing and not existing["applied_at"]:
                conn.execute(
                    "UPDATE seen_jobs SET status=?, status_changed_at=?, applied_at=? WHERE refnr=?",
                    (new_status, now, now, refnr),
                )
                continue
        conn.execute(
            "UPDATE seen_jobs SET status=?, status_changed_at=? WHERE refnr=?",
            (new_status, now, refnr),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "updated": len(refnrs)})


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


@app.route("/charts")
def charts():
    return render_template("charts.html")


@app.route("/api/charts")
def api_charts():
    """JSON endpoint for all chart data. Queried by charts.html on load."""
    conn = get_connection()

    # 1. New jobs per day (last 60 days)
    daily = conn.execute(
        """
        SELECT substr(first_seen, 1, 10) AS day, COUNT(*) AS count
        FROM seen_jobs
        WHERE first_seen >= date('now', '-60 days')
        GROUP BY day
        ORDER BY day ASC
        """
    ).fetchall()

    # 2. Cumulative total over time (same window)
    cumulative = []
    running = 0
    for row in daily:
        running += row["count"]
        cumulative.append({"day": row["day"], "total": running})

    # 3. Status distribution (all entries)
    status_dist = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM seen_jobs
        GROUP BY status
        ORDER BY count DESC
        """
    ).fetchall()

    # 4. Distance distribution (home distance, bucketed)
    buckets = [
        ("0–25 km",    0,   25),
        ("25–50 km",  25,   50),
        ("50–100 km", 50,  100),
        ("100–150 km",100, 150),
        (">150 km",   150, 9999),
        ("Unbekannt", None, None),
    ]
    distance_rows = conn.execute(
        "SELECT distance_home_km FROM seen_jobs"
    ).fetchall()
    dist_counts = {label: 0 for label, _, _ in buckets}
    for r in distance_rows:
        d = r["distance_home_km"]
        matched = False
        for label, lo, hi in buckets:
            if lo is None:
                continue
            if lo <= (d or -1) < hi:
                dist_counts[label] += 1
                matched = True
                break
        if not matched:
            dist_counts["Unbekannt"] += 1

    conn.close()

    return jsonify({
        "daily":       [{"day": r["day"], "count": r["count"]} for r in daily],
        "cumulative":  cumulative,
        "status_dist": [{"status": r["status"], "count": r["count"]} for r in status_dist],
        "distance":    [{"label": k, "count": v} for k, v in dist_counts.items()],
    })


# --------------------------------------------------------------------------- #
# AI rating
# --------------------------------------------------------------------------- #

def _fetch_job_detail(refnr: str) -> dict | None:
    """Fetch full job details from the BA API by reference number.
    Returns a dict with the relevant text fields, or None on failure."""
    import requests as _req
    try:
        r = _req.get(
            f"{BA_API_BASE}/{refnr}",
            headers={"X-API-Key": BA_API_KEY, "User-Agent": BA_USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _load_rating_profile() -> str:
    """Read the rating system-prompt from disk. Falls back to a sensible
    built-in default if the file does not exist yet."""
    try:
        return RATING_PROFILE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return (
            "You are evaluating job postings for a Build & Release Engineer "
            "with ~10 years of embedded/automotive software experience "
            "(Schaeffler, Jenkins CI/CD). Target role: Platform Engineering / DevOps. "
            "Rate on a scale 1–10. "
            "Return ONLY valid JSON with two keys: "
            "\"score\" (integer 1-10) and \"summary\" (2-3 sentences covering fit, "
            "gaps, and any dealbreakers). No markdown, no preamble, no trailing text."
        )


def _build_job_text(row: sqlite3.Row, detail: dict | None,
                    manual_text: str | None = None) -> str:
    """Assemble a compact text representation of the job for the prompt.

    Priority order for the job description:
      1. manual_text  – pasted by the user in the board UI (most reliable)
      2. BA API detail endpoint – rarely works (403 for most refnrs)
      3. Metadata-only fallback – title, company, location, salary, etc.
    """
    parts = [
        f"Title: {row['titel']}",
        f"Company: {row['arbeitgeber']}",
        f"Location: {row['ort']}",
    ]
    if row["distance_home"]:
        parts.append(f"Distance from home: {row['distance_home']}")
    if row["salary"]:
        parts.append(f"Salary: {row['salary']}")
    if row["home_office"]:
        parts.append(f"Home office: {row['home_office']}")
    if row["tag"]:
        parts.append(f"Search profile tag: {row['tag']}")

    # 1. User-pasted description takes priority – skip the API entirely
    if manual_text and manual_text.strip():
        parts.append(f"\n--- Job description ---\n{manual_text.strip()[:4000]}")
        return "\n".join(parts)

    # 2. Try the BA API detail endpoint (usually 403 – fails silently)
    has_description = False
    if detail:
        for key in ("stellenbeschreibung", "aufgaben", "qualifikationen",
                    "wir_bieten", "beschreibung"):
            value = detail.get(key, "")
            if value:
                parts.append(f"\n--- Job description ---\n{value[:3000]}")
                has_description = True
                break

    # 3. No description available – tell the model so it can flag the rating
    if not has_description:
        parts.append(
            "\n--- Hinweis ---\n"
            "Das Rating basiert ausschließlich auf Titel und Metadaten."
        )

    return "\n".join(parts)


def _call_claude(system_prompt: str, user_text: str) -> tuple[int | None, str | None]:
    """POST to the Anthropic Messages API. Returns (score, summary) on success,
    (None, error_message) on failure."""
    import urllib.request as _urlreq
    import urllib.error as _urlerr

    payload = _json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_text}],
    }).encode()

    req = _urlreq.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with _urlreq.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read())
        text = data["content"][0]["text"].strip()
        # Strip accidental markdown fences
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        # Primary parse
        try:
            parsed  = _json.loads(text)
            score   = int(parsed["score"])
            summary = str(parsed["summary"])
            return score, summary
        except (_json.JSONDecodeError, KeyError, ValueError):
            # Fallback: regex extraction in case Claude emitted unescaped
            # newlines or quotes inside the JSON string values
            import re as _re
            score_m   = _re.search(r'"score"\s*:\s*(\d+)', text)
            summary_m = _re.search(
                r'"summary"\s*:\s*"(.*?)(?<!\\)"(?:\s*[,}])', text, _re.DOTALL
            )
            if score_m and summary_m:
                score   = int(score_m.group(1))
                summary = summary_m.group(1).replace('\\"', '"').replace("\\n", " ").strip()
                return score, summary
            return None, f"Could not parse response: {text[:200]}"
    except _urlerr.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        return None, f"Anthropic API error {exc.code}: {body}"
    except Exception as exc:
        return None, f"Rating failed: {exc}"


def _run_rating(refnr: str, manual_text: str | None = None) -> None:
    """Background thread: fetch job detail, call Claude, persist result to DB.

    If manual_text is provided it is used as the job description directly,
    skipping the BA API detail fetch (which returns 403 for virtually all jobs).
    """

    def _set(status, score=None, summary=None):
        with _ratings_lock:
            _ratings[refnr] = {"status": status, "score": score, "summary": summary}

    _set("pending")

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM seen_jobs WHERE refnr = ?", (refnr,)
        ).fetchone()
        conn.close()

        if row is None:
            _set("error", summary="Job not found in database.")
            return

        import logging as _log
        _log.getLogger("jobiris").info(
            "Rating %s: manual_text=%d chars",
            refnr, len(manual_text) if manual_text else 0,
        )

        # Skip the BA API when the user already provided the description
        detail    = None if manual_text else _fetch_job_detail(refnr)
        job_text  = _build_job_text(row, detail, manual_text=manual_text)
        score, summary = _call_claude(_load_rating_profile(), job_text)

        if score is None:
            _set("error", summary=summary)
            return

        conn = get_connection()
        conn.execute(
            "UPDATE seen_jobs SET ai_score = ?, ai_summary = ? WHERE refnr = ?",
            (score, summary, refnr),
        )
        conn.commit()
        conn.close()

        _set("done", score=score, summary=summary)

    except Exception as exc:
        import traceback as _tb
        import logging as _log
        _log.getLogger("jobiris").error(
            "Rating thread crashed: %s\n%s", exc, _tb.format_exc()
        )
        _set("error", summary=f"Internal error: {exc}")


@app.route("/rate/<refnr>", methods=["POST"])
def rate_job(refnr):
    """Trigger an AI rating for a single job. Starts a background thread and
    returns immediately so the board stays responsive.

    Accepts an optional JSON body: {"description": "<pasted job text>"}
    Use force=True so Flask parses the body regardless of the Content-Type
    header (Caddy / HTTP2 may alter it in transit).
    """
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 503

    manual_text = None
    try:
        body = request.get_json(force=True, silent=True) or {}
        manual_text = body.get("description", "").strip() or None
    except Exception:
        pass
    if manual_text is None and request.form.get("description"):
        manual_text = request.form.get("description").strip() or None

    with _ratings_lock:
        entry = _ratings.get(refnr)
        if entry and entry["status"] == "pending":
            return jsonify({"status": "pending"}), 202

    thread = threading.Thread(
        target=_run_rating, args=(refnr,), kwargs={"manual_text": manual_text},
        daemon=True,
    )
    thread.start()
    return jsonify({"status": "pending"}), 202


@app.route("/rate/<refnr>/status")
def rate_status(refnr):
    """Polling endpoint for the rating result. The browser polls every 2s
    after clicking the Rate button until status != 'pending'."""
    with _ratings_lock:
        entry = _ratings.get(refnr)

    if entry is None:
        # Check DB for a previously persisted rating
        conn = get_connection()
        row = conn.execute(
            "SELECT ai_score, ai_summary FROM seen_jobs WHERE refnr = ?", (refnr,)
        ).fetchone()
        conn.close()
        if row and row["ai_score"] is not None:
            return jsonify({"status": "done", "score": row["ai_score"],
                            "summary": row["ai_summary"]})
        return jsonify({"status": "idle"})

    return jsonify({
        "status":  entry["status"],
        "score":   entry.get("score"),
        "summary": entry.get("summary"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
