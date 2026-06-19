# JobIris

Polls the Bundesagentur fuer Arbeit Jobsuche API against configured search
profiles and sends new matches as ntfy notifications. Includes a web board
to view, sort and triage all jobs ever found.

Runs as two Docker services from a shared image:

- **jobiris-board**: long-running web UI (`board.py` + `templates/`),
  started via `docker compose up -d`
- **jobiris-monitor**: one-shot `job-monitor.py`, triggered by systemd timers
  via `docker compose run --rm`

---

## Setup on Mnemosyne

```bash
# 1. Stack lives under homelab-infra/mnemosyne/stacks/jobiris/
cd ~/homelab-infra/mnemosyne/stacks/jobiris

# 2. Create .env (never commit)
cp .env.example .env
nano .env

# 3. Build the shared image and start the board
docker compose up -d --build

# 4. First test run (no DB writes / no notification)
docker compose run --rm jobiris-monitor python3 /app/job-monitor.py --schedule daily --dry-run

# 5. Install systemd timers
sudo cp jobiris-monitor-*.service jobiris-monitor-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jobiris-monitor-daily.timer
sudo systemctl enable --now jobiris-monitor-weekly.timer
```

`requirements.txt` changes require `docker compose up -d --build`.
All other files (`job-monitor.py`, `board.py`, `templates/`, `search-profiles.yaml`)
take effect immediately - they are mounted as a volume at `/app`.
Restart the board after changing `board.py` or templates:

```bash
docker compose restart jobiris-board
```

---

## Manual run / debugging

```bash
# Manual monitor run
docker compose run --rm jobiris-monitor python3 /app/job-monitor.py --schedule daily

# Board logs
docker compose logs -f jobiris-board

# Monitor logs via systemd
journalctl -u jobiris-monitor-daily.service -n 50 --no-pager

# Logfile (shared volume)
tail -f /mnt/vault/jobiris/jobiris.log

# Inspect DB
sqlite3 /mnt/vault/jobiris/seen_jobs.db \
  "SELECT first_seen, titel, arbeitgeber, ort, salary, status FROM seen_jobs ORDER BY first_seen DESC LIMIT 10;"

# Timer status
systemctl list-timers | grep jobiris
```

---

## File structure

```
stacks/jobiris/
├── job-monitor.py               # Monitor: API polling, dedup, ntfy
├── board.py                     # Board: Flask routes, DB queries, run trigger
├── templates/
│   ├── board.html               # Board UI: metrics bar, table, dark mode
│   └── run.html                 # Live run output page
├── search-profiles.yaml         # Search profiles (terms, radii, tiers)
├── Dockerfile                   # Shared image for both services
├── docker-compose.yml
├── requirements.txt
├── .env                         # Secrets (gitignored)
├── .env.example
├── jobiris-monitor-daily.service/.timer
└── jobiris-monitor-weekly.service/.timer
```

---

## Data

- Search profiles: `search-profiles.yaml` (in repo, no secrets)
- Dedup DB / board archive: `/mnt/vault/jobiris/seen_jobs.db` (SQLite)
- Log file: `/mnt/vault/jobiris/jobiris.log` (rotating, 1 MB x 5)
- Archive cap: no time-based expiry - oldest entries pruned beyond `MAX_ENTRIES` (default 1000)

---

## Customization

| What | Where | Rebuild? |
|---|---|---|
| Search terms, radii, profiles | `search-profiles.yaml` | No |
| Board design / layout | `templates/board.html`, `templates/run.html` | No (restart only) |
| ntfy topic/token | `.env` | No |
| Schedule | `jobiris-monitor-*.timer` (`OnCalendar`) | No |
| Archive cap | `MAX_ENTRIES` in `job-monitor.py` | No |
| Python dependencies | `requirements.txt` | Yes |

---

## Board

Web UI at `jobiris.home` with:
- **Metrics bar**: Gesamt / Heute / Interessant / Beworben / Mit Gehalt /
  Home-Office – all clickable (filter table or sort by column)
- **Sortable columns**: date, title, company, location, distance, salary, tag, status
- **Dark mode**: toggle top-right, persisted in `localStorage`,
  respects `prefers-color-scheme` on first visit
- **Status dropdown**: Neu / Interessant / Beworben / Abgelehnt per row
  ("Abgelehnt" hidden by default, kept in DB so it won't re-notify)
- **Run trigger**: ▶ Run daily / ▶ Run weekly + dry-run checkbox,
  live output via JSON polling (works through any proxy)

Caddy site:

```caddy
jobiris.home {
  tls internal
  reverse_proxy jobiris-board:8042 {
    flush_interval -1
    transport http {
      versions 1.1
    }
  }
}
```

Pi-hole DNS entry: `jobiris.home` → Mnemosyne's IP.

---

## API notes

The board container mounts the Docker socket to trigger monitor runs from
the UI. This grants root-equivalent access - acceptable for an internal
`.home` service, but worth noting.

Key quirks of the Jobsuche API v6 (reverse-engineered, undocumented):

- **`wo` requires umlauts**: `Nuernberg` → `UNGUELTIG` → 0 results.
  Use `Nürnberg`, `München` etc., or use `koordinaten` instead.
- **`arbeitszeit=ho` is ineffective for IT roles**: returns 0 results even
  when jobs have `homeofficemoeglich: true`. Omitted from `search-profiles.yaml`;
  `home_office` is read from the response field instead.
- **Salary**: `gehaltsspanneVon`/`gehaltsspanneBis` (float, EUR/Jahr) where
  available; `verguetungsangabe` string enum as fallback (`KEINE_ANGABEN` = none).
- Results under `ergebnisliste` (not `stellenangebote` as in v4).
- City nested under `stellenlokationen[0].adresse.ort` (region suffix stripped).
- Browser-like `User-Agent` required - default `python-requests` UA triggers
  403 when filter params are present.
- `size` max 250; beyond-last-page returns HTTP 200 with empty list.
- No hard rate limit observed; `X-API-BLOCKED: 0` header present.

Full API reference: `BA_Jobsuche_API_v6.md`

---

## Backup

Add `/mnt/vault/jobiris/` to `backup-services.sh` to preserve job history
and status assignments.

---

*Created with Claude · claude.ai*
