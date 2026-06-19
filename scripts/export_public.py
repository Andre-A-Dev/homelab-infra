#!/usr/bin/env python3
"""
export_public.py — Export a sanitized public copy of homelab-infra

Creates a clean snapshot of the repo with:
  - No git history
  - Internal IPs replaced with generic placeholders
  - Personal domain / username replaced
  - Sensitive files removed (hooks.json, .env files, private keys)
  - A fresh git repo ready to push to GitHub

Usage:
    python export_public.py                          # export next to current repo
    python export_public.py --out C:\\path\\to\\out  # explicit output path
    python export_public.py --dry-run                # show what would happen, no changes
    python export_public.py --push https://github.com/you/homelab-infra.git

Requirements: git in PATH, no extra pip packages needed
"""

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path


def _remove_readonly(func, path, _):
    """Error handler for shutil.rmtree — clears readonly bit on Windows before retrying."""
    os.chmod(path, stat.S_IWRITE)
    func(path)

# ── Replacement rules ──────────────────────────────────────────────────────────
# Applied to all text file contents. Order matters — most specific first.

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

# File patterns to remove (matched against relative path)
REMOVE_PATTERNS: list[str] = [
    r"(^|[\\/])\.env$",              # .env files (not .env.example)
    r"\.key$",                        # private keys
    r"\.pem$",                        # certificates/keys
    r"(^|[\\/])\.idea[\\/]",         # IDE metadata
]

# Extensions to treat as binary — skip content replacement
BINARY_EXT: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".zip", ".tar",
    ".gz", ".db", ".sqlite", ".bin", ".pdf", ".ttf", ".woff", ".woff2",
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def git_archive_files(repo: Path) -> list[Path]:
    """Return list of files tracked by git (respects .gitignore)."""
    result = run(["git", "ls-files"], cwd=repo)
    paths = []
    for p in result.stdout.splitlines():
        p = p.strip()
        if not p:
            continue
        # Path() normalises forward-slash paths on all platforms
        full = repo / Path(p)
        if full.exists():
            paths.append(full)
        # else: listed by git but not on disk (submodule, deleted) — skip
    return paths


def should_remove(rel: str) -> bool:
    """Check if a file should be excluded from the public export."""
    rel_normalized = rel.replace("\\", "/")
    for pattern in REMOVE_PATTERNS:
        if re.search(pattern, rel_normalized):
            return True
    for remove_path in REMOVE_FILES:
        if rel_normalized == remove_path.replace("\\", "/"):
            return True
    return False


def sanitize_content(content: str) -> str:
    """Apply all replacement rules to file content."""
    for pattern, replacement in REPLACEMENTS:
        content = re.sub(pattern, replacement, content)
    return content


def read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, PermissionError):
        return None

# ── Core export ────────────────────────────────────────────────────────────────

def export_public(
    source: Path,
    dest: Path,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    stats = {"copied": 0, "sanitized": 0, "removed": 0, "binary": 0}

    files = git_archive_files(source)
    print(f"  Source : {source}")
    print(f"  Output : {dest}")
    print(f"  Files  : {len(files)} tracked by git")
    if dry_run:
        print(f"  Mode   : DRY RUN — no files will be written\n")

    for src_path in files:
        rel = str(src_path.relative_to(source))

        # Check removal first
        if should_remove(rel):
            stats["removed"] += 1
            if verbose or dry_run:
                print(f"  REMOVE  {rel}")
            continue

        dest_path = dest / src_path.relative_to(source)

        # Binary files — copy as-is, no content replacement
        if src_path.suffix.lower() in BINARY_EXT:
            stats["binary"] += 1
            if verbose:
                print(f"  BINARY  {rel}")
            if not dry_run:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
            continue

        # Text files — read, sanitize, write
        content = read_text_safe(src_path)
        if content is None:
            # Unreadable as UTF-8 — copy as binary
            stats["binary"] += 1
            if not dry_run:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dest_path)
            continue

        sanitized = sanitize_content(content)
        changed = sanitized != content

        if changed:
            stats["sanitized"] += 1
            if verbose or dry_run:
                print(f"  SANITIZE {rel}")
        else:
            stats["copied"] += 1
            if verbose:
                print(f"  COPY    {rel}")

        if not dry_run:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(sanitized, encoding="utf-8")

    return stats


def init_git_repo(dest: Path, dry_run: bool = False) -> None:
    if dry_run:
        print("\n  [dry-run] would: git init && git add . && git commit")
        return
    run(["git", "init"], cwd=dest)
    run(["git", "add", "."], cwd=dest)
    run(["git", "commit", "-m", "initial commit — sanitized public export"], cwd=dest)
    print(f"  Git repo initialized with initial commit")


def push_to_remote(dest: Path, remote_url: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"\n  [dry-run] would: git remote add origin {remote_url} && git push")
        return
    run(["git", "remote", "add", "origin", remote_url], cwd=dest)
    result = run(["git", "push", "-u", "origin", "main"], cwd=dest, check=False)
    if result.returncode != 0:
        # Try 'master' if 'main' doesn't exist yet
        run(["git", "push", "-u", "origin", "master"], cwd=dest)
    print(f"  Pushed to {remote_url}")

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Export sanitized public copy of homelab-infra")
    parser.add_argument("--out",     default=None, help="Output directory (default: <repo>-public next to source)")
    parser.add_argument("--push",    default=None, help="Remote URL to push to after export (e.g. https://github.com/you/repo.git)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output directory (preserves .git if present)")
    parser.add_argument("--no-commit", action="store_true", help="Skip git init and commit — just export files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without writing files")
    parser.add_argument("--verbose", action="store_true", help="Print every file processed")
    parser.add_argument("path",      nargs="?", default=".", help="Path to source repo (default: current directory)")
    args = parser.parse_args()

    # Find repo root
    source = Path(args.path).resolve()
    if not (source / ".git").exists():
        for parent in source.parents:
            if (parent / ".git").exists():
                source = parent
                break
        else:
            print(f"Not a git repository: {source}")
            return 1

    # Determine output path
    if args.out:
        dest = Path(args.out).resolve()
    else:
        dest = source.parent / f"{source.name}-public"

    # Handle existing output directory
    if dest.exists() and not args.dry_run:
        if not args.overwrite:
            print(f"Output directory already exists: {dest}")
            print(f"Use --overwrite to replace it, or --out to choose a different path.")
            return 1
        # Remove everything except .git — preserve the existing repo
        git_dir = dest / ".git"
        git_dir_backup = None
        if git_dir.exists():
            import tempfile
            git_dir_backup = Path(tempfile.mkdtemp()) / ".git"
            shutil.copytree(git_dir, git_dir_backup)
            print(f"  Preserving existing .git directory")
        shutil.rmtree(dest, onerror=_remove_readonly)
        dest.mkdir(parents=True)
        if git_dir_backup and git_dir_backup.exists():
            shutil.copytree(git_dir_backup, dest / ".git")
            shutil.rmtree(git_dir_backup.parent, onerror=_remove_readonly)
            print(f"  Restored .git directory")

    print(f"\nhomelab-infra — public export\n")

    # Export
    stats = export_public(source, dest, dry_run=args.dry_run, verbose=args.verbose)

    # Summary
    print(f"\n  copied    {stats['copied']}")
    print(f"  sanitized {stats['sanitized']}  (replacements applied)")
    print(f"  binary    {stats['binary']}   (copied as-is)")
    print(f"  removed   {stats['removed']}   (excluded from public export)")

    if args.dry_run:
        print(f"\n  Dry run complete — no files written.")
        return 0

    # Init git and commit — skipped if --no-commit
    if not args.no_commit:
        if not (dest / ".git").exists():
            init_git_repo(dest, dry_run=args.dry_run)
        else:
            if not args.dry_run:
                run(["git", "add", "."], cwd=dest)
                run(["git", "commit", "-m", "update — sanitized public export"], cwd=dest)
                print(f"  Committed update to existing git repo")
    else:
        print(f"  Skipping git init/commit (--no-commit)")

    # Run audit on the export to catch anything missed
    audit_script = dest / "scripts" / "audit_repo.py"
    template     = dest / "scripts" / "config" / "audit_report.html.j2"
    has_git      = (dest / ".git").exists()
    if audit_script.exists() and not args.dry_run and has_git:
        print(f"\n  Running audit on exported copy...")
        result = subprocess.run(
            [sys.executable, str(audit_script),
             "--no-history",   # history is clean — fresh repo
             "--template", str(template),
             "--report-out", str(dest / "scripts" / "audit_report.html"),
             str(dest)],
            capture_output=False,
        )
        if result.returncode != 0:
            print(f"\n  Audit found issues in export — review before publishing.")
            return 1
    elif not has_git:
        print(f"\n  Skipping audit — no .git in export (use without --no-commit to enable).")
    elif not audit_script.exists():
        print(f"\n  audit_repo.py not found in export — skipping audit.")

    # Push if requested
    if args.push:
        push_to_remote(dest, args.push, dry_run=args.dry_run)

    print(f"\n  Export complete: {dest}")
    if not args.push:
        print(f"\n  Next steps:")
        print(f"  1. Review the export: {dest}")
        print(f"  2. Create an empty repo on GitHub")
        print(f"  3. Push:")
        print(f"     cd \"{dest}\"")
        print(f"     git remote add origin https://github.com/you/homelab-infra.git")
        print(f"     git push -u origin main")

    return 0


if __name__ == "__main__":
    sys.exit(main())