#!/usr/bin/env python3
"""
sync_public.py — Sync a sanitized snapshot from private Gitea repo to a local GitHub clone.

Copies all git-tracked files from the source repo (Gitea) to an existing local
GitHub clone, applying the same sanitization rules as export_public.py.
Only files that actually changed are written. No git operations are performed —
push to GitHub manually after reviewing the diff.

Usage:
    python sync_public.py <github-clone-dir>
    python sync_public.py <github-clone-dir> --source /path/to/gitea-repo
    python sync_public.py <github-clone-dir> --dry-run
    python sync_public.py <github-clone-dir> --verbose
    python sync_public.py <github-clone-dir> --report /path/to/report.html

Defaults:
    --source    repo root containing this script

Requirements: git in PATH, no extra pip packages needed
"""

import argparse
import datetime
import html as html_mod
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ── Replacement rules ──────────────────────────────────────────────────────────
# Identical to export_public.py. Order matters — most specific first.

REPLACEMENTS: list[tuple[str, str]] = [
    # Internal IPs
    (r"192\.168\.178\.87",   "192.168.1.10"),    # Mnemosyne
    (r"192\.168\.178\.90",   "192.168.1.13"),    # Hephaestus
    (r"192\.168\.178\.78",   "192.168.1.11"),    # Boreas / Zephyros
    (r"192\.168\.178\.100",  "192.168.1.12"),    # Zephyros local alt
    (r"192\.168\.178\.\d+",  "192.168.1.x"),     # Any other .178 IP
    (r"192\.168\.178\.0/24", "192.168.1.0/24"),
    # Tailscale IPs
    (r"100\.65\.240\.16",    "100.x.x.x"),       # Mnemosyne Tailscale
    (r"100\.117\.164\.84",   "100.y.y.y"),       # Zephyros Tailscale
    (r"100\.\d+\.\d+\.\d+", "100.x.x.x"),       # Any other Tailscale IP
    # Domain / identity
    (r"youruser\.dedyn\.io",  "yourdomain.dedyn.io"),
    (r"\bauralis\b",         "youruser"),
    # Backup SSD UUID
    (r"XXXX-XXXX",           "XXXX-XXXX"),
    # ntfy topic with embedded secret
    (r"Mnemosyne-Validate-[A-Za-z0-9]+", "Mnemosyne-Validate-<topic>"),
]

# Files to remove entirely from the public export
REMOVE_FILES: list[str] = [
    "mnemosyne/webhook/hooks.json",   # contains webhook secrets
]

# File patterns to exclude (matched against relative path)
REMOVE_PATTERNS: list[str] = [
    r"(^|[\\/])\.env$",              # .env files (not .env.example)
    r"\.key$",                        # private keys
    r"\.pem$",                        # certificates / keys
    r"(^|[\\/])\.idea[\\/]",         # IDE metadata
]

# Extensions treated as binary — copied as-is, no content replacement
BINARY_EXT: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".tar",
    ".gz", ".db", ".sqlite", ".bin", ".pdf", ".ttf", ".woff", ".woff2",
}

# Terminal color codes
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    CYAN   = "\033[36m"
    GRAY   = "\033[90m"


# ── Helpers ────────────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def find_repo_root(path: Path) -> Path | None:
    """Walk up from path until a .git directory is found."""
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def git_tracked_files(repo: Path) -> list[Path]:
    """Return all files currently tracked by git in the given repo."""
    result = run(["git", "ls-files"], cwd=repo)
    paths = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        full = repo / Path(line)
        if full.exists():
            paths.append(full)
    return paths


def git_head_info(repo: Path) -> tuple[str, str]:
    """Return (short hash, subject) of HEAD commit, or fallback strings."""
    try:
        h = run(["git", "log", "-1", "--format=%h"], cwd=repo).stdout.strip()
        s = run(["git", "log", "-1", "--format=%s"], cwd=repo).stdout.strip()
        return h, s
    except Exception:
        return "unknown", "unknown"


def should_exclude(rel: str) -> bool:
    """Return True if this file must not appear in the public copy."""
    rel_normalized = rel.replace("\\", "/")
    for pattern in REMOVE_PATTERNS:
        if re.search(pattern, rel_normalized):
            return True
    for remove_path in REMOVE_FILES:
        if rel_normalized == remove_path.replace("\\", "/"):
            return True
    return False


def sanitize(content: str) -> str:
    """Apply all replacement rules to text content."""
    for pattern, replacement in REPLACEMENTS:
        content = re.sub(pattern, replacement, content)
    return content


def read_text(path: Path) -> str | None:
    """Read file as UTF-8. Returns None if binary or unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, PermissionError):
        return None


def collect_dest_files(dest: Path) -> set[Path]:
    """Return all files currently present in the destination directory."""
    return {f for f in dest.rglob("*") if f.is_file() and ".git" not in f.parts}


# ── Terminal output helpers ────────────────────────────────────────────────────

ACTION_FMT = {
    "NEW":     (C.GREEN,  "NEW    "),
    "WRITE":   (C.CYAN,   "WRITE  "),
    "SKIP":    (C.GRAY,   "SKIP   "),
    "BINARY":  (C.GRAY,   "BINARY "),
    "EXCLUDE": (C.YELLOW, "EXCLUDE"),
    "DELETE":  (C.RED,    "DELETE "),
}

def log(action: str, rel: str, note: str = "") -> None:
    color, label = ACTION_FMT.get(action, (C.RESET, action.ljust(7)))
    suffix = f"  {C.DIM}{note}{C.RESET}" if note else ""
    print(f"  {color}{label}{C.RESET}  {rel}{suffix}")


# ── Core sync ─────────────────────────────────────────────────────────────────

# Each entry: (action, rel_path, note)
FileRecord = tuple[str, str, str]


def sync(
    source: Path,
    dest: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> tuple[dict, list[FileRecord]]:
    stats = {
        "written":  0,
        "skipped":  0,
        "excluded": 0,
        "binary":   0,
        "deleted":  0,
    }
    records: list[FileRecord] = []

    source_files = git_tracked_files(source)
    expected_dest: set[Path] = set()

    for src in source_files:
        rel = str(src.relative_to(source)).replace("\\", "/")

        # --- Exclusion check -------------------------------------------------
        if should_exclude(rel):
            stats["excluded"] += 1
            records.append(("EXCLUDE", rel, "removed by sanitization rules"))
            if verbose or dry_run:
                log("EXCLUDE", rel)
            continue

        dst = dest / src.relative_to(source)
        expected_dest.add(dst)

        # --- Binary files ----------------------------------------------------
        if src.suffix.lower() in BINARY_EXT:
            is_new   = not dst.exists()
            differs  = is_new or src.read_bytes() != dst.read_bytes()
            if differs:
                stats["binary"]  += 1
                stats["written"] += 1
                action = "NEW" if is_new else "BINARY"
                records.append((action, rel, "binary"))
                if verbose or dry_run:
                    log(action, rel, "binary")
                if not dry_run:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            else:
                stats["skipped"] += 1
                records.append(("SKIP", rel, "unchanged"))
                if verbose:
                    log("SKIP", rel, "unchanged")
            continue

        # --- Text files ------------------------------------------------------
        raw = read_text(src)
        if raw is None:
            # Unreadable as UTF-8 — treat as binary
            stats["binary"] += 1
            is_new = not dst.exists()
            if is_new or src.read_bytes() != dst.read_bytes():
                stats["written"] += 1
                records.append(("BINARY", rel, "non-UTF-8"))
                if not dry_run:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
            else:
                stats["skipped"] += 1
                records.append(("SKIP", rel, "unchanged"))
            continue

        sanitized     = sanitize(raw)
        dest_current  = read_text(dst) if dst.exists() else None

        if sanitized == dest_current:
            stats["skipped"] += 1
            records.append(("SKIP", rel, "unchanged"))
            if verbose:
                log("SKIP", rel, "unchanged")
            continue

        is_new  = dest_current is None
        changed = sanitized != raw
        action  = "NEW" if is_new else "WRITE"
        note    = "sanitized" if changed else ""
        stats["written"] += 1
        records.append((action, rel, note))
        if verbose or dry_run or action == "NEW":
            log(action, rel, note)
        elif not verbose:
            log(action, rel, note)

        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(sanitized, encoding="utf-8")

    # --- Stale file cleanup --------------------------------------------------
    existing_dest = collect_dest_files(dest)
    stale = existing_dest - expected_dest

    for stale_file in sorted(stale):
        rel = str(stale_file.relative_to(dest)).replace("\\", "/")
        stats["deleted"] += 1
        records.append(("DELETE", rel, "no longer in source"))
        log("DELETE", rel, "no longer in source")
        if not dry_run:
            stale_file.unlink()
            try:
                stale_file.parent.rmdir()
            except OSError:
                pass

    return stats, records


# ── HTML report ───────────────────────────────────────────────────────────────

BADGE_COLORS = {
    "NEW":     ("#d1fae5", "#065f46"),
    "WRITE":   ("#dbeafe", "#1e3a8a"),
    "SKIP":    ("#f3f4f6", "#374151"),
    "BINARY":  ("#f3f4f6", "#374151"),
    "EXCLUDE": ("#fef9c3", "#713f12"),
    "DELETE":  ("#fee2e2", "#7f1d1d"),
}

REPORT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       font-size: 14px; line-height: 1.6; color: #111; background: #f9fafb;
       padding: 2rem; }
h1   { font-size: 20px; font-weight: 600; margin-bottom: 0.25rem; }
.meta { font-size: 12px; color: #6b7280; margin-bottom: 2rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
         gap: 12px; margin-bottom: 2rem; }
.card  { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
         padding: 1rem 1.25rem; }
.card-label { font-size: 11px; color: #9ca3af; text-transform: uppercase;
              letter-spacing: 0.05em; margin-bottom: 4px; }
.card-value { font-size: 26px; font-weight: 600; }
.card-value.green  { color: #059669; }
.card-value.blue   { color: #2563eb; }
.card-value.red    { color: #dc2626; }
.card-value.yellow { color: #d97706; }
.card-value.gray   { color: #6b7280; }
.section { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
           overflow: hidden; margin-bottom: 1.5rem; }
.section-header { padding: 0.75rem 1rem; background: #f9fafb;
                  border-bottom: 1px solid #e5e7eb; font-weight: 500;
                  font-size: 13px; color: #374151; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 0.5rem 1rem; text-align: left; border-bottom: 1px solid #f3f4f6; }
th { font-size: 11px; color: #9ca3af; text-transform: uppercase;
     letter-spacing: 0.05em; font-weight: 500; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f9fafb; }
td.path { font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; color: #374151; }
td.note { font-size: 12px; color: #9ca3af; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
         font-size: 11px; font-weight: 500; font-family: monospace; }
.dry-banner { background: #fef9c3; border: 1px solid #fde68a; border-radius: 8px;
              padding: 0.75rem 1rem; margin-bottom: 1.5rem; font-size: 13px;
              color: #92400e; }
.nothing { padding: 1.5rem 1rem; color: #9ca3af; font-size: 13px; text-align: center; }
"""


def build_report(
    source: Path,
    dest: Path,
    stats: dict,
    records: list[FileRecord],
    dry_run: bool,
    src_hash: str,
    src_subject: str,
) -> str:
    now    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total  = sum(stats.values())

    def badge(action: str) -> str:
        bg, fg = BADGE_COLORS.get(action, ("#e5e7eb", "#374151"))
        return f'<span class="badge" style="background:{bg};color:{fg}">{action}</span>'

    def rows_for(actions: list[str], section_records: list[FileRecord]) -> str:
        filtered = [r for r in section_records if r[0] in actions]
        if not filtered:
            return '<tr><td colspan="3" class="nothing">—</td></tr>'
        out = []
        for action, rel, note in filtered:
            out.append(
                f'<tr><td>{badge(action)}</td>'
                f'<td class="path">{html_mod.escape(rel)}</td>'
                f'<td class="note">{html_mod.escape(note)}</td></tr>'
            )
        return "\n".join(out)

    def card(label: str, value: int, color: str) -> str:
        return (
            f'<div class="card">'
            f'<div class="card-label">{label}</div>'
            f'<div class="card-value {color}">{value}</div>'
            f'</div>'
        )

    dry_banner = (
        '<div class="dry-banner">⚠ Dry run — no files were written.</div>'
        if dry_run else ""
    )

    nothing_banner = ""
    if not dry_run and stats["written"] == 0 and stats["deleted"] == 0:
        nothing_banner = '<div class="dry-banner" style="background:#f0fdf4;border-color:#bbf7d0;color:#14532d">✓ Nothing to sync — destination already up to date.</div>'

    changed_rows  = rows_for(["NEW", "WRITE"], records)
    excluded_rows = rows_for(["EXCLUDE"], records)
    deleted_rows  = rows_for(["DELETE"], records)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>sync_public — {now}</title>
<style>{REPORT_CSS}</style>
</head>
<body>
<h1>homelab-infra — public sync report</h1>
<p class="meta">
  {now} &nbsp;·&nbsp;
  source: <code>{html_mod.escape(str(source))}</code> &nbsp;·&nbsp;
  dest: <code>{html_mod.escape(str(dest))}</code> &nbsp;·&nbsp;
  HEAD: <code>{html_mod.escape(src_hash)}</code> {html_mod.escape(src_subject)}
</p>

{dry_banner}
{nothing_banner}

<div class="cards">
  {card("written",  stats["written"],  "green")}
  {card("skipped",  stats["skipped"],  "gray")}
  {card("binary",   stats["binary"],   "gray")}
  {card("excluded", stats["excluded"], "yellow")}
  {card("deleted",  stats["deleted"],  "red")}
  {card("total",    total,             "blue")}
</div>

<div class="section">
  <div class="section-header">Changed files ({stats["written"]})</div>
  <table>
    <thead><tr><th>action</th><th>path</th><th>note</th></tr></thead>
    <tbody>{changed_rows}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-header">Excluded files ({stats["excluded"]})</div>
  <table>
    <thead><tr><th>action</th><th>path</th><th>reason</th></tr></thead>
    <tbody>{excluded_rows}</tbody>
  </table>
</div>

<div class="section">
  <div class="section-header">Deleted from destination ({stats["deleted"]})</div>
  <table>
    <thead><tr><th>action</th><th>path</th><th>reason</th></tr></thead>
    <tbody>{deleted_rows}</tbody>
  </table>
</div>

</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync sanitized files from private Gitea repo to local GitHub clone."
    )
    parser.add_argument("dest",          help="Path to the local GitHub clone (must already exist)")
    parser.add_argument("--source",      default=None, help="Path to source repo (default: repo root of this script)")
    parser.add_argument("--dry-run",     action="store_true", help="Show what would change without writing anything")
    parser.add_argument("--verbose",     action="store_true", help="Print every file processed, including skipped ones")
    parser.add_argument("--report",      default=None, metavar="FILE", help="Write an HTML report to FILE")
    args = parser.parse_args()

    # --- Resolve source repo -------------------------------------------------
    if args.source:
        source = Path(args.source).resolve()
    else:
        source = find_repo_root(Path(__file__).resolve().parent)
        if source is None:
            print(f"{C.RED}ERROR{C.RESET}  Could not find a git repository from this script's location.")
            print(f"       Use --source /path/to/gitea-repo to specify it explicitly.")
            return 1

    if not (source / ".git").exists():
        print(f"{C.RED}ERROR{C.RESET}  Not a git repository: {source}")
        return 1

    # --- Resolve destination -------------------------------------------------
    dest = Path(args.dest).resolve()

    if not dest.exists():
        print(f"{C.RED}ERROR{C.RESET}  Destination does not exist: {dest}")
        print(f"       Clone your GitHub repo there first:")
        print(f"       git clone https://github.com/Andre-A-Dev/homelab-infra.git \"{dest}\"")
        return 1

    if not (dest / ".git").exists():
        print(f"{C.RED}ERROR{C.RESET}  Destination is not a git repository: {dest}")
        return 1

    # --- Header --------------------------------------------------------------
    src_hash, src_subject = git_head_info(source)
    mode_label = f"{C.YELLOW}DRY RUN{C.RESET}" if args.dry_run else "live"

    print()
    print(f"  {C.BOLD}homelab-infra — public sync{C.RESET}  [{mode_label}]")
    print(f"  {C.DIM}source{C.RESET}  {source}  {C.DIM}({src_hash} {src_subject}){C.RESET}")
    print(f"  {C.DIM}dest  {C.RESET}  {dest}")
    print()

    # --- Run -----------------------------------------------------------------
    stats, records = sync(source, dest, dry_run=args.dry_run, verbose=args.verbose)

    # --- Summary -------------------------------------------------------------
    def stat_line(label: str, value: int, color: str) -> str:
        return f"  {color}{value:>5}{C.RESET}  {C.DIM}{label}{C.RESET}"

    print()
    print(stat_line("written   (new or changed)", stats["written"],  C.GREEN))
    print(stat_line("skipped   (identical)",       stats["skipped"],  C.GRAY))
    print(stat_line("binary    (copied as-is)",    stats["binary"],   C.GRAY))
    print(stat_line("excluded  (sanitized out)",   stats["excluded"], C.YELLOW))
    print(stat_line("deleted   (stale in dest)",   stats["deleted"],  C.RED))

    if args.dry_run:
        print(f"\n  {C.YELLOW}Dry run complete — no files written.{C.RESET}")
    elif stats["written"] == 0 and stats["deleted"] == 0:
        print(f"\n  {C.GREEN}Nothing to sync — destination already up to date.{C.RESET}")
    else:
        print(f"\n  {C.GREEN}Sync complete.{C.RESET} Review and push:")
        print(f"  {C.DIM}  cd \"{dest}\"{C.RESET}")
        print(f"  {C.DIM}  git diff --stat{C.RESET}")
        print(f"  {C.DIM}  git add -A{C.RESET}")
        print(f'  {C.DIM}  git commit -m "sync: sanitized export from homelab-infra"{C.RESET}')
        print(f"  {C.DIM}  git push{C.RESET}")

    # --- HTML report ---------------------------------------------------------
    if args.report:
        report_path = Path(args.report).resolve()
        report_html = build_report(
            source, dest, stats, records, args.dry_run, src_hash, src_subject
        )
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_html, encoding="utf-8")
        print(f"\n  {C.CYAN}Report written:{C.RESET} {report_path}")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())