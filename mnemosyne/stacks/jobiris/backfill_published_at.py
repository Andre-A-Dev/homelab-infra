#!/usr/bin/env python3
"""
backfill_published_at.py

One-time script: fetches datumErsteVeroeffentlichung for all seen_jobs
entries where published_at is NULL, using the v6 job detail endpoint.

Run once on Mnemosyne:
    python3 backfill_published_at.py

Safe to re-run - only updates rows where published_at is still NULL.
"""

import sqlite3
import time
from pathlib import Path

import requests

DB_PATH = Path("/mnt/vault/jobiris/seen_jobs.db")
DETAIL_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobdetail/{refnr}"
HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}
DELAY = 1.0


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT refnr, titel FROM seen_jobs WHERE published_at IS NULL"
    ).fetchall()

    print(f"Found {len(rows)} entries without published_at")

    updated = 0
    failed = 0

    for row in rows:
        refnr = row["refnr"]
        url = DETAIL_URL.format(refnr=refnr)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                data = resp.json()
                published = data.get("datumErsteVeroeffentlichung")
                if published:
                    conn.execute(
                        "UPDATE seen_jobs SET published_at = ? WHERE refnr = ?",
                        (published, refnr),
                    )
                    conn.commit()
                    print(f"  ✓ {refnr[:30]:<30} → {published}")
                    updated += 1
                else:
                    print(f"  – {refnr[:30]:<30} → no date in response")
                    failed += 1
            elif resp.status_code == 404:
                print(f"  ✗ {refnr[:30]:<30} → 404 (expired/removed)")
                failed += 1
            else:
                print(f"  ✗ {refnr[:30]:<30} → HTTP {resp.status_code}")
                failed += 1
        except requests.RequestException as exc:
            print(f"  ✗ {refnr[:30]:<30} → {exc}")
            failed += 1

        time.sleep(DELAY)

    conn.close()
    print(f"\nDone: {updated} updated, {failed} failed/skipped")


if __name__ == "__main__":
    main()
