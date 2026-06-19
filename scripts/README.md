# scripts

Repo-level tools for maintaining and publishing this repository.
Not intended for deployment on individual hosts.

---

## Overview

| Script | Language | Purpose |
|---|---|---|
| `repo-tree.py` | Python | Print a directory tree respecting `.gitignore` |
| `export_public.py` | Python | Export a sanitized public copy of the repo |
| `audit_repo.py` | Python | Scan repo for secrets before publishing |

---

## repo-tree.py

Prints a directory tree of the repo, respecting `.gitignore`. Useful for
documenting the current structure or reviewing what is tracked by git.

```bash
python repo-tree.py                             # current directory
python repo-tree.py ~/homelab-infra             # explicit path
python repo-tree.py --depth 3                   # limit depth
python repo-tree.py --out tree.txt              # write to file
python repo-tree.py --all                       # ignore .gitignore
```

---

## export_public.py

Exports a sanitized copy of the repo suitable for publishing on GitHub.
Replaces internal IPs, personal domains, and usernames with generic
placeholders. Removes sensitive files (`.env`, private keys, `hooks.json`).

Automatically runs `audit_repo.py` on the export before finishing.

```bash
python export_public.py                         # export next to current repo
python export_public.py --out /path/to/output  # explicit output path
python export_public.py --dry-run              # show what would happen
python export_public.py --push https://github.com/you/homelab-infra.git
python export_public.py --overwrite            # update existing export
```

---

## audit_repo.py

Scans the working tree and full git history for secrets, tokens, and sensitive
values. Used as a pre-publication check; also called automatically by
`export_public.py`.

```bash
python audit_repo.py                           # scan with console output
python audit_repo.py --no-history              # skip git history scan
python audit_repo.py --fix-hints               # include remediation hints
python audit_repo.py --template config/audit_report.html.j2  # HTML report
```

**Optional dependencies:** `pip install colorama jinja2` (color output + HTML report)
