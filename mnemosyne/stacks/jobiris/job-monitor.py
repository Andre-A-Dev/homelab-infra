#!/usr/bin/env python3
"""
job-monitor.py (JobIris)

Polls the Bundesagentur fuer Arbeit "Jobsuche" API for configured search
profiles, deduplicates results against a local SQLite database, and sends
a digest notification via ntfy for any newly discovered postings.

Usage:
    job-monitor.py --schedule daily
    job-monitor.py --schedule weekly
    job-monitor.py --schedule daily --dry-run
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
API_KEY = "jobboerse-jobsuche"  # public client id shared by all official frontends

# The API's WAF blocks the default "python-requests/x.x" User-Agent when
# additional filter params (arbeitszeit, angebotsart, veroeffentlichtseit)
# are present, returning 403. A browser-like User-Agent avoids this.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

DEFAULT_CONFIG = Path(__file__).resolve().parent / "search-profiles.yaml"
DEFAULT_DB = Path("/mnt/vault/jobiris/seen_jobs.db")
DEFAULT_LOG_FILE = Path("/mnt/vault/jobiris/jobiris.log")

MAX_ENTRIES = 1000  # board archive cap; oldest entries pruned beyond this
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.0  # seconds between API calls, be polite to a public service
PAGE_SIZE = 50
LOG_MAX_BYTES = 1_000_000  # 1 MB
LOG_BACKUP_COUNT = 5

log = logging.getLogger("jobiris")


def setup_logging(log_file: Path) -> None:
    """Log to stdout (picked up by journalctl) and, if writable, to a
    rotating logfile next to the dedup database."""
    handlers = [logging.StreamHandler()]

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
            )
        )
    except OSError as exc:
        print(f"Could not set up log file {log_file}: {exc}", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_terms(config: dict, term_groups: list) -> list:
    """Flatten a list of term-group names (e.g. ['tier1', 'tier2']) into a
    single list of search strings."""
    terms = []
    for group in term_groups:
        terms.extend(config["search_terms"][group])
    return terms


def resolve_locations(config: dict, location_ref: str) -> list:
    """A location reference points to either a single location dict or a
    list of location dicts (e.g. relocation_clusters). Always return a list."""
    locations = config["locations"][location_ref]
    if isinstance(locations, dict):
        return [locations]
    return locations


# --------------------------------------------------------------------------- #
# SQLite dedup store
# --------------------------------------------------------------------------- #

def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict) -> None:
    """Add missing columns to an existing table. SQLite has no
    'ADD COLUMN IF NOT EXISTS', so check PRAGMA table_info first - this lets
    older databases (created before the board feature) pick up new columns
    without a manual migration step."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.commit()


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
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

    # Migration for databases created before the board feature
    _ensure_columns(conn, "seen_jobs", {
        "distance_km": "INTEGER",
        "distance": "TEXT",
        "salary": "TEXT",
        "salary_from": "INTEGER",
        "home_office": "TEXT",
        "tag": "TEXT",
        "status": "TEXT DEFAULT 'Neu'",
    })
    return conn


def is_known(conn: sqlite3.Connection, refnr: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_jobs WHERE refnr = ?", (refnr,)
    ).fetchone() is not None


def mark_seen(conn: sqlite3.Connection, job: dict, profile_name: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_jobs
            (refnr, titel, arbeitgeber, ort, url, distance_km, distance,
             salary, salary_from, home_office, profile, tag, status, first_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Neu', ?)
        """,
        (
            job["refnr"],
            job["titel"],
            job["arbeitgeber"],
            job["ort"],
            job["url"],
            job.get("distance_km"),
            job.get("distance"),
            job.get("salary"),
            job.get("salary_from"),
            job.get("home_office"),
            profile_name,
            job.get("_tag", profile_name),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def prune_excess_entries(conn: sqlite3.Connection, max_entries: int = MAX_ENTRIES) -> int:
    """Keeps the board archive bounded: deletes the oldest entries (by
    first_seen) once the table exceeds max_entries. No time-based cutoff -
    entries persist indefinitely until the count limit is hit."""
    count = conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
    if count <= max_entries:
        return 0

    excess = count - max_entries
    conn.execute(
        """
        DELETE FROM seen_jobs WHERE refnr IN (
            SELECT refnr FROM seen_jobs ORDER BY first_seen ASC LIMIT ?
        )
        """,
        (excess,),
    )
    conn.commit()
    return excess


# --------------------------------------------------------------------------- #
# Bundesagentur Jobsuche API
# --------------------------------------------------------------------------- #

def search_jobs(term: str, location: dict, defaults: dict, veroeffentlichtseit):
    """Returns a list of raw job dicts, or None if the request failed."""
    params = {
        "was": term,
        "wo": location["wo"],
        "umkreis": location["umkreis"],
        "page": 1,
        "size": PAGE_SIZE,
        "pav": "false",
    }
    params.update(defaults)
    if veroeffentlichtseit is not None:
        params["veroeffentlichtseit"] = veroeffentlichtseit

    headers = {"X-API-Key": API_KEY, "User-Agent": USER_AGENT}

    try:
        resp = requests.get(API_BASE, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("API request failed (term=%r, wo=%r): %s", term, location["wo"], exc)
        return None

    return resp.json().get("ergebnisliste", [])


def _location_name(location: dict) -> str:
    """v6 'stellenlokationen' entries nest the city under 'adresse'."""
    adresse = location.get("adresse", {})
    for source in (adresse, location):
        for key in ("ort", "ortsname", "region"):
            value = source.get(key)
            if value:
                return value
    return "(unknown)"


def _format_salary(raw: dict) -> str | None:
    """Extract salary from the v6 item. Prefer the structured
    gehaltsspanneVon/gehaltsspanneBis fields over the string enum
    verguetungsangabe, which is usually 'KEINE_ANGABEN'."""
    von = raw.get("gehaltsspanneVon")
    bis = raw.get("gehaltsspanneBis")
    if von is not None or bis is not None:
        if von and bis:
            return f"{int(von):,}–{int(bis):,} EUR/Jahr".replace(",", ".")
        if von:
            return f"ab {int(von):,} EUR/Jahr".replace(",", ".")
        if bis:
            return f"bis {int(bis):,} EUR/Jahr".replace(",", ".")

    # Fall back to the string enum
    verguetung = raw.get("verguetungsangabe")
    if not verguetung:
        return None
    if isinstance(verguetung, str):
        text = verguetung.strip()
        if not text or text.upper() in ("KEINE_ANGABEN", "KEINE_ANGABE"):
            return None
        return text.replace("_", " ").capitalize()
    return None


def _format_home_office(raw: dict) -> str | None:
    if not raw.get("homeofficemoeglich"):
        return None
    typ = raw.get("homeofficetyp")
    pct = raw.get("homeofficeprozent")
    if typ == "ANGABE_IN_PROZENT" and pct is not None:
        return f"Home office: {pct}%"
    if typ == "NACH_VEREINBARUNG":
        return "Home office: nach Vereinbarung"
    return "Home office möglich"


def normalize_job(raw: dict) -> dict:
    refnr = raw["referenznummer"]

    locations = raw.get("stellenlokationen") or []
    if locations:
        ort_full = _location_name(locations[0])
        # API often returns "Nürnberg, Mittelfranken" - keep only the city part
        ort = ort_full.split(",")[0].strip()
        if len(locations) > 1:
            ort = f"{ort} (+{len(locations) - 1} more)"
    else:
        ort = "(unknown)"

    url = raw.get("externeURL") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

    distance = raw.get("entfernung")
    salary = _format_salary(raw)

    # Numeric salary floor for DB sorting (None if not available)
    salary_from = raw.get("gehaltsspanneVon")
    if salary_from is not None:
        salary_from = int(salary_from)

    return {
        "refnr": refnr,
        "titel": raw.get("stellenangebotsTitel", "(no title)"),
        "arbeitgeber": raw.get("firma", "(unknown)"),
        "ort": ort,
        "url": url,
        "distance_km": distance,
        "distance": f"{distance} km" if distance is not None else None,
        "salary": salary,
        "salary_from": salary_from,
        "home_office": _format_home_office(raw),
    }


# --------------------------------------------------------------------------- #
# Notification
# --------------------------------------------------------------------------- #

def send_ntfy_notifications(new_jobs_by_profile: dict) -> int:
    """Sends one ntfy notification per new job, with a Click header so
    tapping the notification opens the job posting directly. Returns the
    number of successfully sent notifications."""
    total = sum(len(jobs) for jobs in new_jobs_by_profile.values())
    if total == 0:
        log.info("No new jobs found, skipping notification.")
        return 0

    ntfy_url = os.environ.get("NTFY_URL", "https://ntfy.sh")
    ntfy_topic = os.environ.get("NTFY_TOPIC", "mnemosyne-jobiris")
    ntfy_token = os.environ.get("NTFY_TOKEN")

    base_headers = {}
    if ntfy_token:
        base_headers["Authorization"] = f"Bearer {ntfy_token}"

    sent = 0
    for jobs in new_jobs_by_profile.values():
        for job in jobs:
            extras = [
                e for e in (job.get("distance"), job.get("home_office"), job.get("salary"))
                if e
            ]
            body_lines = [job["arbeitgeber"], job["ort"]]
            if extras:
                body_lines.append(" | ".join(extras))
            body = "\n".join(body_lines)

            headers = {
                **base_headers,
                # ntfy expects header values as Latin-1/UTF-8 bytes; passing
                # raw str can raise on umlauts depending on the requests
                # version, so encode explicitly.
                "Title": f"[{job['_tag']}] {job['titel']}".encode("utf-8"),
                "Click": job["url"].encode("utf-8"),
                "Tags": "briefcase",
            }

            try:
                resp = requests.post(
                    f"{ntfy_url}/{ntfy_topic}",
                    data=body.encode("utf-8"),
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                resp.raise_for_status()
                sent += 1
            except requests.RequestException as exc:
                log.error("Failed to send ntfy notification for %s: %s", job["refnr"], exc)

            time.sleep(0.5)  # avoid bursting the ntfy server

    log.info("Sent %d/%d ntfy notification(s).", sent, total)
    return sent


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(config: dict, conn: sqlite3.Connection, schedule: str, dry_run: bool) -> None:
    defaults = config.get("defaults", {})
    profiles_to_run = [p for p in config["profiles"] if p["schedule"] == schedule]

    if not profiles_to_run:
        log.warning("No profiles configured for schedule=%s - nothing to do.", schedule)
        return

    log.info("Starting JobIris run (schedule=%s, %d profile(s))", schedule, len(profiles_to_run))

    new_jobs_by_profile = {}
    total_checked = 0
    total_errors = 0

    for profile in profiles_to_run:
        terms = resolve_terms(config, profile["terms"])
        locations = resolve_locations(config, profile["location"])
        veroeffentlichtseit = profile.get("veroeffentlichtseit")
        tag = profile.get("tag", profile["name"])

        profile_checked = 0
        profile_new = 0
        profile_errors = 0

        for term in terms:
            for location in locations:
                raw_jobs = search_jobs(term, location, defaults, veroeffentlichtseit)
                if raw_jobs is None:
                    profile_errors += 1
                else:
                    profile_checked += len(raw_jobs)
                    for raw in raw_jobs:
                        job = normalize_job(raw)
                        if is_known(conn, job["refnr"]):
                            continue
                        job["_tag"] = tag
                        new_jobs_by_profile.setdefault(profile["name"], []).append(job)
                        if not dry_run:
                            mark_seen(conn, job, profile["name"])
                        else:
                            extras = [
                                e for e in (job.get("distance"), job.get("home_office"), job.get("salary"))
                                if e
                            ]
                            extra_str = f" | {' | '.join(extras)}" if extras else ""
                            log.info(
                                "  new: %s | %s | %s%s",
                                job["titel"], job["arbeitgeber"], job["ort"], extra_str,
                            )
                        profile_new += 1
                time.sleep(REQUEST_DELAY)

        total_checked += profile_checked
        total_errors += profile_errors

        log.info(
            "Profile '%s' [%s]: %d posting(s) checked across %d search(es), %d new, %d failed request(s)",
            profile["name"], tag, profile_checked, len(terms) * len(locations), profile_new, profile_errors,
        )

    total_new = sum(len(jobs) for jobs in new_jobs_by_profile.values())
    log.info(
        "Run summary: %d profile(s), %d posting(s) checked, %d new match(es), %d failed request(s)",
        len(profiles_to_run), total_checked, total_new, total_errors,
    )

    if dry_run:
        log.info("Dry run - skipping notification and database writes.")
        return

    send_ntfy_notifications(new_jobs_by_profile)

    pruned = prune_excess_entries(conn)
    if pruned:
        log.info("Pruned %d oldest entr%s (keeping %d most recent).", pruned, "y" if pruned == 1 else "ies", MAX_ENTRIES)

    log.info("JobIris run finished.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bundesagentur Jobsuche monitor")
    parser.add_argument(
        "--schedule", choices=["daily", "weekly"], required=True,
        help="Which profile group to run",
    )
    parser.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("JOBIRIS_CONFIG", str(DEFAULT_CONFIG))),
        help="Path to search-profiles.yaml",
    )
    parser.add_argument(
        "--db", type=Path,
        default=Path(os.environ.get("JOBIRIS_DB", str(DEFAULT_DB))),
        help="Path to SQLite dedup database",
    )
    parser.add_argument(
        "--log-file", type=Path,
        default=Path(os.environ.get("JOBIRIS_LOG_FILE", str(DEFAULT_LOG_FILE))),
        help="Path to rotating log file (in addition to stdout)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without writing to the database or sending notifications",
    )
    args = parser.parse_args()

    setup_logging(args.log_file)

    config = load_config(args.config)
    conn = init_db(args.db)

    try:
        run(config, conn, args.schedule, args.dry_run)
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
