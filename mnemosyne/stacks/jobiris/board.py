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
    "Archiviert",        # set automatically by job-monitor.py (auto_archive)
]

# Statuses that hide jobs from the default view
HIDDEN_STATUSES = {"Absage erhalten", "Nicht relevant", "Archiviert"}

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


# --------------------------------------------------------------------------- #
# Column filters
# --------------------------------------------------------------------------- #

import re as _re
from urllib.parse import urlencode as _urlencode

_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")

# All query parameters that belong to the filter row (used for "reset" and
# for counting active filters).
FILTER_KEYS = (
    "f_q", "f_found_from", "f_found_to", "f_pub_from", "f_pub_to",
    "f_title", "f_company", "f_location", "f_dist",
    "f_salary", "f_ho", "f_tag", "f_status", "f_ai",
)


def _text_filter(column: str, raw: str, where: list, params: list) -> None:
    """Smart text filter:
      - comma separates alternatives (OR):  'devops, build'
      - leading '-' or '!' excludes a term: '-kubernetes'
    Matching is case-insensitive incl. umlauts (via PYLOWER)."""
    include, exclude = [], []
    for token in (t.strip() for t in raw.split(",")):
        if not token:
            continue
        if token[0] in "-!" and len(token) > 1:
            exclude.append(token[1:].strip().lower())
        else:
            include.append(token.lower())
    if include:
        where.append("(" + " OR ".join(f"PYLOWER({column}) LIKE ?" for _ in include) + ")")
        params.extend(f"%{t}%" for t in include)
    for t in exclude:
        where.append(f"(PYLOWER({column}) NOT LIKE ? OR {column} IS NULL)")
        params.append(f"%{t}%")


def _range_filter(column: str, raw: str, where: list, params: list) -> None:
    """Numeric range filter. Accepts '50' (max), '20-80', '>100', '<30'."""
    raw = raw.replace(" ", "")
    m = _re.fullmatch(r"(\d+)-(\d+)", raw)
    if m:
        where.append(f"{column} BETWEEN ? AND ?")
        params.extend([int(m.group(1)), int(m.group(2))])
    elif _re.fullmatch(r">\d+", raw):
        where.append(f"{column} > ?")
        params.append(int(raw[1:]))
    elif _re.fullmatch(r"<?\d+", raw):
        where.append(f"{column} <= ?")
        params.append(int(raw.lstrip("<")))


def build_filters(args) -> tuple[list[str], list, str, int]:
    """Translate request args into SQL WHERE parts.
    Returns (where_parts, params, effective_status_filter, active_filter_count)."""
    where: list[str] = []
    params: list = []

    # Status: f_status wins; legacy params (all / status_filter from the
    # metric bar) are mapped onto it for backwards compatibility.
    f_status = args.get("f_status", "")
    if not f_status:
        if args.get("all") == "1":
            f_status = "__all__"
        elif args.get("status_filter") in STATUS_OPTIONS:
            f_status = args.get("status_filter")
    wanted = [p for p in f_status.split(",") if p in STATUS_OPTIONS]
    if wanted:
        where.append(f"status IN ({', '.join('?' * len(wanted))})")
        params.extend(wanted)
    elif f_status != "__all__":
        where.append(f"status NOT IN ({', '.join('?' * len(HIDDEN_STATUSES))})")
        params.extend(sorted(HIDDEN_STATUSES))

    # Date ranges (inclusive, compared on the YYYY-MM-DD prefix)
    for key, column, op in (
        ("f_found_from", "substr(first_seen, 1, 10)", ">="),
        ("f_found_to",   "substr(first_seen, 1, 10)", "<="),
        ("f_pub_from",   "published_at",              ">="),
        ("f_pub_to",     "published_at",              "<="),
    ):
        value = args.get(key, "")
        if _DATE_RE.match(value):
            where.append(f"{column} {op} ?")
            params.append(value)

    # Global search box: title, company and location at once
    if args.get("f_q", "").strip():
        _text_filter("(COALESCE(titel,'') || ' ' || COALESCE(arbeitgeber,'') || ' ' || COALESCE(ort,''))",
                     args["f_q"], where, params)

    # Text columns
    for key, column in (("f_title", "titel"), ("f_company", "arbeitgeber"), ("f_location", "ort")):
        value = args.get(key, "").strip()
        if value:
            _text_filter(column, value, where, params)

    # Distance range
    if args.get("f_dist", "").strip():
        _range_filter("distance_home_km", args["f_dist"], where, params)

    # Presence filters
    presence = {
        "f_salary": "(salary IS NOT NULL AND salary != '')",
        "f_ho":     "(home_office IS NOT NULL AND home_office != '')",
    }
    for key, expr in presence.items():
        if args.get(key) == "with":
            where.append(expr)
        elif args.get(key) == "without":
            where.append(f"NOT {expr}")

    # Exact tag
    if args.get("f_tag"):
        where.append("tag = ?")
        params.append(args["f_tag"])

    # AI score
    f_ai = args.get("f_ai", "")
    if f_ai == "none":
        where.append("ai_score IS NULL")
    elif f_ai.isdigit():
        where.append("ai_score >= ?")
        params.append(int(f_ai))

    active = sum(1 for k in FILTER_KEYS if args.get(k))
    return where, params, f_status, active


@app.context_processor
def _inject_qs():
    """qs(**overrides) builds a query string from the current request args.
    Pass None/'' to drop a key. Keeps sort + filters combinable in links."""
    def qs(**overrides):
        args = request.args.to_dict()
        for key, value in overrides.items():
            if value is None or value == "":
                args.pop(key, None)
            else:
                args[key] = value
        return "?" + _urlencode(args)
    return {"qs": qs}


# --------------------------------------------------------------------------- #
# Views (tabs) and filter chips
# --------------------------------------------------------------------------- #

def _today_iso() -> str:
    import datetime as _dt, zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(BOARD_TIMEZONE)
    except Exception:
        tz = _dt.timezone.utc
    return _dt.datetime.now(tz).date().isoformat()


def builtin_views() -> list[dict]:
    today = _today_iso()
    return [
        {"id": "all",      "label": "Alle aktiven",         "params": {}},
        {"id": "today",    "label": "Neu heute",            "params": {"f_found_from": today, "f_found_to": today}},
        {"id": "commute",  "label": "Pendelbar mit Gehalt", "params": {"f_dist": "75", "f_salary": "with"}},
        {"id": "interest", "label": "Interessant",          "params": {"f_status": "Interessant"}},
        {"id": "applied",  "label": "Beworben",             "params": {"f_status": "Beworben,Feedback ausstehend"}},
        {"id": "top",      "label": "AI ≥ 7",               "params": {"f_ai": "7"}},
        {"id": "archived", "label": "Archiviert",           "params": {"f_status": "Archiviert"}},
    ]


def _ensure_views_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_views (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            query      TEXT NOT NULL,
            created_at TEXT
        )
        """
    )
    conn.commit()


def current_filter_params(args) -> dict:
    """Active filter params as a plain dict (legacy params mapped onto f_status)."""
    cur = {k: args.get(k) for k in FILTER_KEYS if args.get(k)}
    if "f_status" not in cur:
        if args.get("all") == "1":
            cur["f_status"] = "__all__"
        elif args.get("status_filter") in STATUS_OPTIONS:
            cur["f_status"] = args.get("status_filter")
    return cur


def _fmt_date(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}." if iso and len(iso) == 10 else iso


def build_chips(args) -> list[dict]:
    """One chip per active filter: field key, label, value text, removal keys."""
    cur = current_filter_params(args)
    chips = []

    def add(field, label, value, keys):
        chips.append({"field": field, "label": label, "value": value, "remove": list(keys)})

    for field, label, k_from, k_to in (
        ("found", "Gefunden", "f_found_from", "f_found_to"),
        ("pub", "Veröffentlicht", "f_pub_from", "f_pub_to"),
    ):
        a, b = cur.get(k_from), cur.get(k_to)
        if a or b:
            if a and b and a == b:
                value = _fmt_date(a)
            elif a and b:
                value = f"{_fmt_date(a)}–{_fmt_date(b)}"
            elif a:
                value = f"ab {_fmt_date(a)}"
            else:
                value = f"bis {_fmt_date(b)}"
            add(field, label, value, (k_from, k_to))

    for key, field, label in (("f_title", "title", "Titel"), ("f_company", "company", "Arbeitgeber"),
                              ("f_location", "location", "Ort")):
        if cur.get(key):
            add(field, label, cur[key], (key,))

    if cur.get("f_dist"):
        v = cur["f_dist"]
        value = f"{v} km" if not v.isdigit() else f"bis {v} km"
        add("dist", "Entfernung", value, ("f_dist",))
    for key, field, label in (("f_salary", "salary", "Gehalt"), ("f_ho", "ho", "Home-Office")):
        if cur.get(key):
            add(field, label, "mit Angabe" if cur[key] == "with" else "ohne", (key,))
    if cur.get("f_tag"):
        add("tag", "Tag", cur["f_tag"], ("f_tag",))
    if cur.get("f_status"):
        v = cur["f_status"]
        value = "alle" if v == "__all__" else v.replace(",", ", ")
        add("status", "Status", value, ("f_status", "all", "status_filter"))
    if cur.get("f_ai"):
        v = cur["f_ai"]
        add("ai", "AI", "unbewertet" if v == "none" else f"≥ {v}", ("f_ai",))
    return chips

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
        WHERE status NOT IN ('Absage erhalten', 'Nicht relevant', 'Archiviert')
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

    where_parts, params, f_status, active_filters = build_filters(request.args)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    conn = get_connection()
    # Case-insensitive matching that also handles umlauts (SQLite LOWER is ASCII-only)
    conn.create_function("PYLOWER", 1, lambda s: s.lower() if isinstance(s, str) else s)
    rows = conn.execute(
        f"SELECT * FROM seen_jobs {where_clause} "
        f"ORDER BY {column} {direction_sql} NULLS LAST",
        params,
    ).fetchall()
    latest_date = conn.execute(
        "SELECT MAX(substr(first_seen, 1, 10)) FROM seen_jobs"
    ).fetchone()[0]

    # Facet values for filter dropdowns / autocomplete and date picker bounds
    def _distinct(col: str, limit: int = 200) -> list[str]:
        return [r[0] for r in conn.execute(
            f"SELECT {col}, COUNT(*) c FROM seen_jobs WHERE {col} IS NOT NULL AND {col} != '' "
            f"GROUP BY {col} ORDER BY c DESC LIMIT ?", (limit,)
        )]
    facets = {
        "tags":      _distinct("tag"),
        "companies": _distinct("arbeitgeber"),
        "locations": _distinct("ort"),
    }
    bounds = conn.execute(
        "SELECT MIN(substr(first_seen,1,10)), MAX(substr(first_seen,1,10)), "
        "MIN(published_at), MAX(published_at) FROM seen_jobs"
    ).fetchone()
    date_bounds = {
        "found_min": bounds[0], "found_max": bounds[1],
        "pub_min":   bounds[2], "pub_max":   bounds[3],
    }

    # ---- Views (tabs): built-in + user-saved, each with a live count ----
    from urllib.parse import parse_qsl as _parse_qsl
    _ensure_views_table(conn)
    current = current_filter_params(request.args)

    def _count(view_params: dict) -> int:
        w, p, _, _ = build_filters(view_params)
        clause = ("WHERE " + " AND ".join(w)) if w else ""
        return conn.execute(f"SELECT COUNT(*) FROM seen_jobs {clause}", p).fetchone()[0]

    views = []
    for v in builtin_views():
        views.append({**v, "custom": False})
    for vid, name, query in conn.execute("SELECT id, name, query FROM saved_views ORDER BY id"):
        views.append({"id": f"v{vid}", "db_id": vid, "label": name,
                      "params": dict(_parse_qsl(query)), "custom": True})
    for v in views:
        v["count"] = _count(v["params"])
        v["active"] = v["params"] == current
        v["href"] = "?" + _urlencode({"sort": sort_key, "dir": direction, **v["params"]})
    chips = build_chips(request.args)
    for c in chips:
        keep = {k: v for k, v in request.args.items() if k not in c["remove"]}
        c["href"] = "?" + _urlencode(keep)

    # Everything the filter popover needs, handed to JS as JSON
    filter_state = {
        "params": current,
        "today": _today_iso(),
        "bounds": date_bounds,
        "tags": facets["tags"],
        "companies": facets["companies"],
        "locations": facets["locations"],
        "statuses": STATUS_OPTIONS,
        "hidden": sorted(HIDDEN_STATUSES),
    }
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
        f=request.args,
        f_status=f_status,
        active_filters=active_filters,
        facets=facets,
        date_bounds=date_bounds,
        views=views,
        chips=chips,
        filter_state=filter_state,
        sort_label=dict(
            date="Gefunden", published="Veröffentlicht", title="Titel", company="Arbeitgeber",
            location="Ort", distance="Entfernung", salary="Gehalt", tag="Tag", status="Status",
        ).get(sort_key, "Gefunden"),
    )


@app.route("/views", methods=["POST"])
def save_view():
    """Save the current filter set as a named view (tab)."""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:40]
    query = data.get("query") or ""
    from urllib.parse import parse_qsl as _parse_qsl, urlencode as _enc
    # Keep only known filter keys
    clean = {k: v for k, v in _parse_qsl(query.lstrip("?")) if k in FILTER_KEYS and v}
    if not name or not clean:
        return jsonify({"error": "name and at least one filter required"}), 400
    conn = get_connection()
    _ensure_views_table(conn)
    conn.execute("INSERT INTO saved_views (name, query, created_at) VALUES (?, ?, ?)",
                 (name, _enc(clean), _today_iso()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/views/<int:view_id>/delete", methods=["POST"])
def delete_view(view_id):
    conn = get_connection()
    _ensure_views_table(conn)
    conn.execute("DELETE FROM saved_views WHERE id = ?", (view_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


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

    # Return to the exact view (sort + filters) the change was made from
    next_url = request.form.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
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


def _build_job_text(row: sqlite3.Row, detail: dict | None) -> str:
    """Assemble a compact text representation of the job for the prompt.
    Uses whatever detail the BA API returns; falls back to the board fields."""
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

    # Append free-text fields from the detail endpoint when available
    if detail:
        for key in ("stellenbeschreibung", "aufgaben", "qualifikationen",
                    "wir_bieten", "beschreibung"):
            value = detail.get(key, "")
            if value:
                parts.append(f"\n--- Job description ---\n{value[:3000]}")
                break  # one description field is enough

    return "\n".join(parts)


def _call_claude(system_prompt: str, user_text: str) -> tuple[int | None, str | None]:
    """POST to the Anthropic Messages API. Returns (score, summary) on success,
    (None, error_message) on failure."""
    import json as _json
    import urllib.request as _urlreq
    import urllib.error as _urlerr

    payload = _json.dumps({
        "model": "claude-sonnet-4-6",
        "max_tokens": 256,
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
        # Strip any accidental markdown fences before parsing
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = _json.loads(text)
        score   = int(parsed["score"])
        summary = str(parsed["summary"])
        return score, summary
    except _urlerr.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        return None, f"Anthropic API error {exc.code}: {body}"
    except Exception as exc:
        return None, f"Rating failed: {exc}"


def _run_rating(refnr: str) -> None:
    """Background thread: fetch job detail, call Claude, persist result to DB."""

    def _set(status, score=None, summary=None):
        with _ratings_lock:
            _ratings[refnr] = {"status": status, "score": score, "summary": summary}

    _set("pending")

    # Pull board row for base fields
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM seen_jobs WHERE refnr = ?", (refnr,)
    ).fetchone()
    conn.close()

    if row is None:
        _set("error", summary="Job not found in database.")
        return

    # Optionally enrich with full description from BA API
    detail = _fetch_job_detail(refnr)

    job_text     = _build_job_text(row, detail)
    system_prompt = _load_rating_profile()
    score, summary = _call_claude(system_prompt, job_text)

    if score is None:
        _set("error", summary=summary)
        return

    # Persist to DB
    conn = get_connection()
    conn.execute(
        "UPDATE seen_jobs SET ai_score = ?, ai_summary = ? WHERE refnr = ?",
        (score, summary, refnr),
    )
    conn.commit()
    conn.close()

    _set("done", score=score, summary=summary)


@app.route("/rate/<refnr>", methods=["POST"])
def rate_job(refnr):
    """Trigger an AI rating for a single job. Starts a background thread and
    returns immediately so the board stays responsive."""
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 503

    with _ratings_lock:
        entry = _ratings.get(refnr)
        if entry and entry["status"] == "pending":
            # Already running – don't start a second thread
            return jsonify({"status": "pending"}), 202

    thread = threading.Thread(target=_run_rating, args=(refnr,), daemon=True)
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
