#!/usr/bin/env python3
"""
audit_repo.py — Pre-publication secret scanner for homelab-infra

Scans the working tree and full git history for secrets, tokens,
and sensitive values before making a repository public.

Usage:
    python audit_repo.py                                    # console output only
    python audit_repo.py --no-history                       # skip git history scan
    python audit_repo.py --fix-hints                        # show remediation hints
    python audit_repo.py --template config/report.html.j2  # also generate HTML report
    python audit_repo.py --template config/report.html.j2 --report-out D:\\out.html
    python audit_repo.py C:\\path\\to\\repo           # explicit repo path

Requirements:
    pip install colorama jinja2
    both are optional — script runs without them (no color, no HTML report)
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ── Optional dependencies ──────────────────────────────────────────────────────

try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    class _NoColor:
        def __getattr__(self, _): return ""
    Fore = Style = _NoColor()
    HAS_COLOR = False

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    HAS_JINJA = True
except ImportError:
    HAS_JINJA = False

# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str       # "fail" | "warn"
    check: str
    detail: str
    file: str = ""
    line: int = 0
    remediation: str = ""

@dataclass
class Section:
    title: str
    findings: list[Finding] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""

# Active section during a run — findings are appended here
_current_section: Section | None = None
_all_sections: list[Section] = []


def begin_section(title: str) -> Section:
    global _current_section
    s = Section(title=title)
    _all_sections.append(s)
    _current_section = s
    return s


def fail(check: str, detail: str, file: str = "", line: int = 0, remediation: str = "") -> None:
    assert _current_section is not None
    _current_section.findings.append(Finding("fail", check, detail, file, line, remediation))


def warn(check: str, detail: str, file: str = "", line: int = 0, remediation: str = "") -> None:
    assert _current_section is not None
    _current_section.findings.append(Finding("warn", check, detail, file, line, remediation))


def skip_section(reason: str) -> None:
    assert _current_section is not None
    _current_section.skipped = True
    _current_section.skip_reason = reason

# ── Pattern definitions ────────────────────────────────────────────────────────

# (label, regex, severity, remediation)
TREE_PATTERNS: list[tuple[str, str, str, str]] = [
    (
        "Password / passphrase assignment",
        # Literal value after = or : that is not:
        # - a function/method call  (value contains a dot or open paren)
        # - an env var read         (os., environ, getenv)
        # - a known placeholder     (CHANGEME, YOUR_*, all-caps 5+ chars)
        # - a variable reference    (starts with $ or {)
        # - a comment line          (handled in scan loop)
        r"(?i)(password|passwd|passphrase)\s*[=:]\s*[\"']?"
        r"(?![\s\"']*(?:$|[\$\{/]|\w+[\.\(]|os\.|CHANGEME|YOUR_|ENTER_|your-|changeme|example|placeholder|TODO|none|null|NOPASSWD|ALL=|[A-Z][A-Z_]{4,}\b))"
        r"[^\s\"'\\#\(\.,/]{4,}",
        "fail",
        "Move value to .env file, reference as ${VAR_NAME} in config",
    ),
    (
        "API key / token assignment",
        r"(?i)(api_key|api_token|auth_token|access_token|bearer)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{16,}",
        "fail",
        "Move to .env file",
    ),
    (
        "PEM private key block",
        r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY",
        "fail",
        "Remove immediately — private keys must never be committed",
    ),
    (
        "Tailscale auth key",
        r"tskey-[A-Za-z0-9\-]+",
        "fail",
        "Revoke key in Tailscale admin console, remove from repo",
    ),
    (
        "Grafana service account token",
        r"glsa_[A-Za-z0-9]{32}",
        "fail",
        "Revoke token in Grafana, move to .env",
    ),
    (
        "Credentials embedded in URL (hardcoded password)",
        # user:password@ where password does not start with ${ (those are token refs, caught separately)
        r"(?i)https?://[A-Za-z0-9._\-]{2,}:(?!\$\{)[^\s@\"'{\n\\]{4,}@[A-Za-z0-9._\-/]+",
        "fail",
        "Use environment variables for credentials, not inline URLs",
    ),
    (
        "Username embedded in URL with token auth",
        # user:${TOKEN}@ — token is a variable but username leaks identity
        r"(?i)https?://[A-Za-z0-9._\-]{2,}:\$\{[^}]+\}@",
        "warn",
        "Remove hardcoded username — token-only auth is sufficient: https://${TOKEN}@host/...",
    ),
    (
        "ntfy topic with embedded secret (long lowercase topic = likely secret)",
        # Exclude ALLCAPS_WITH_UNDERSCORES placeholders like NTFY_PUMP_CRITICAL_TOPIC.
        # NOTE: this pattern must be matched WITHOUT re.IGNORECASE (use flag NOIGNORECASE below).
        # Prefix __CASE_SENSITIVE__ signals the scan loop to omit re.IGNORECASE for this pattern.
        r"__CASE_SENSITIVE__ntfy\.sh/(?![A-Z][A-Z_]{7,})[A-Za-z0-9_\-]{20,}",
        "warn",
        "Consider whether the topic name is sensitive — move to .env if so",
    ),
    (
        "Webhook secret value",
        r'"secret"\s*:\s*"[^"]{8,}"',
        "fail",
        "Move webhook secret to .env",
    ),
    (
        "deSEC / DynDNS token",
        r"(?i)(desec|dyndns|ddns).{0,30}(token|key|secret)\s*[=:]",
        "warn",
        "Verify no actual token value follows — move to .env",
    ),
    (
        "Internal RFC-1918 IP in non-documentation file",
        r"192\.168\.\d{1,3}\.\d{1,3}",
        "warn",
        "Replace with placeholder (e.g. 192.168.1.x) or move to .env",
    ),
    (
        "Tailscale IP in non-documentation file",
        r"100\.\d{1,3}\.\d{1,3}\.\d{1,3}",
        "warn",
        "Replace with placeholder (e.g. 100.x.x.x)",
    ),
]

SKIP_DIRS       = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea"}
SKIP_NAMES      = {".env"}
BINARY_EXT      = {".png",".jpg",".jpeg",".gif",".ico",".svg",".zip",".tar",
                   ".gz",".db",".sqlite",".bin",".pdf",".ttf",".woff",".woff2"}
# Generated output files — skip from content scan
SKIP_GENERATED  = {"audit_report.html", "audit_report.html.j2"}
DOC_EXT         = {".md", ".rst", ".txt"}

# ── Filesystem helpers ─────────────────────────────────────────────────────────

def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in BINARY_EXT:
            continue
        if path.name in SKIP_GENERATED:
            continue
        yield path


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, PermissionError):
        return None

# ── Check functions ────────────────────────────────────────────────────────────

def check_tracked_env_files(root: Path) -> None:
    result = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True
    )
    for f in result.stdout.splitlines():
        if Path(f).name == ".env":
            fail(
                "Tracked .env file", f,
                remediation=f"git rm --cached {f}  &&  echo '.env' >> .gitignore",
            )


def check_env_example_coverage(root: Path) -> None:
    for env_file in root.rglob(".env"):
        if any(part in SKIP_DIRS for part in env_file.parts):
            continue
        example = env_file.parent / ".env.example"
        if not example.exists():
            warn(
                ".env.example missing",
                str(env_file.relative_to(root)),
                remediation="Create .env.example with variable names and placeholder values",
            )


def check_gitignore(root: Path) -> None:
    required = [".env", "*.key", "*.pem"]
    gi = root / ".gitignore"
    if not gi.exists():
        fail(".gitignore missing", "No .gitignore found in repo root",
             remediation="Create .gitignore with at minimum: .env, *.key, *.pem")
        return
    content = gi.read_text(encoding="utf-8", errors="replace")
    for pattern in required:
        if pattern not in content:
            warn(".gitignore incomplete", f"Missing pattern: {pattern}",
                 remediation=f"Add '{pattern}' to .gitignore")


def check_private_key_files(root: Path) -> None:
    for pattern in ["*.key", "id_rsa", "id_ed25519", "id_ecdsa", "*.pem"]:
        for match in root.rglob(pattern):
            if any(part in SKIP_DIRS for part in match.parts):
                continue
            fail(
                "Private key file on disk",
                str(match.relative_to(root)),
                remediation="Remove file and add pattern to .gitignore",
            )


def check_working_tree_patterns(root: Path) -> None:
    for path in iter_files(root):
        # Skip .env files (caught by check_tracked_env_files)
        # Skip .env.example files — they intentionally contain variable names without values
        if path.name in SKIP_NAMES or path.name == ".env.example":
            continue
        content = read_text(path)
        if content is None:
            continue
        rel     = str(path.relative_to(root))
        is_doc  = path.suffix.lower() in DOC_EXT
        for label, pattern, severity, remediation in TREE_PATTERNS:
            # Suppress IP warnings in documentation — expected and reviewed
            if is_doc and "IP" in label:
                continue
            # Some patterns opt out of case-insensitive matching via a prefix flag
            case_sensitive = pattern.startswith("__CASE_SENSITIVE__")
            actual_pattern = pattern.removeprefix("__CASE_SENSITIVE__")
            scan_flags     = re.MULTILINE if case_sensitive else re.MULTILINE | re.IGNORECASE
            for match in re.finditer(actual_pattern, content, scan_flags):
                line_no = content[: match.start()].count("\n") + 1
                line    = content.splitlines()[line_no - 1]
                context = line.strip()[:200]
                # Skip pure comment lines — example values in config comments are not findings
                if line.lstrip().startswith("#"):
                    # For fail-severity, still flag non-comment-only secrets
                    # (a secret could appear after code on a commented-out line is rare but possible)
                    # For warnings, comment lines are almost always example values → skip entirely
                    if severity == "warn":
                        continue
                if severity == "fail":
                    fail(label, context, rel, line_no, remediation)
                else:
                    warn(label, context, rel, line_no, remediation)


def check_git_history(root: Path) -> None:
    history_patterns = [
        ("Password in history",           r'(?i)(password|passwd|secret)\s*[=:]\s*["\']?(?![\$\{<])[^\s"\']{4,}'),
        ("API token in history",          r'(?i)(api_key|api_token|access_token)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}'),
        ("PEM private key in history",    r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"),
        ("Tailscale auth key in history", r"tskey-[A-Za-z0-9\-]+"),
    ]
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--full-diff", "-p"],
            cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        history = result.stdout
    except subprocess.TimeoutExpired:
        warn("Git history scan timed out",
             "Scan manually: git log --all -p | grep -i password",
             remediation="Increase timeout or scan manually")
        return
    except FileNotFoundError:
        warn("git not found in PATH", "Install git and ensure it is on your PATH")
        return

    for label, pattern in history_patterns:
        actual_pattern = pattern.removeprefix("__CASE_SENSITIVE__")
        matches = list(re.finditer(actual_pattern, history))
        if matches:
            first   = matches[0]
            snippet = history[max(0, first.start() - 80): first.end() + 40]
            snippet = snippet.replace("\n", " ").strip()[:120]
            fail(
                label,
                f"{len(matches)} match(es) — first: {snippet}",
                remediation="Create a fresh repo without history (safest), or: pip install git-filter-repo",
            )


def check_personal_identifiers(root: Path) -> None:
    email_re = r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
    for path in iter_files(root):
        if path.suffix.lower() in DOC_EXT:
            continue
        content = read_text(path)
        if content is None:
            continue
        rel = str(path.relative_to(root))
        for match in re.finditer(email_re, content):
            line_no = content[: match.start()].count("\n") + 1
            warn("Email address in non-doc file", match.group(), rel, line_no,
                 remediation="Replace with placeholder or move to .env")

# ── Console output ─────────────────────────────────────────────────────────────

def print_section_header(title: str, index: int) -> None:
    print(f"\n{Fore.CYAN}{Style.BRIGHT}── {index:02d} · {title} {Style.RESET_ALL}")


def print_console_results(show_hints: bool) -> int:
    failures = [f for s in _all_sections for f in s.findings if f.severity == "fail"]
    warnings = [f for s in _all_sections for f in s.findings if f.severity == "warn"]

    for group, color, symbol in [
        (failures, Fore.RED,    "✗"),
        (warnings, Fore.YELLOW, "⚠"),
    ]:
        for r in group:
            loc = f"  {r.file}:{r.line}" if r.file else ""
            print(f"  {color}{symbol}{Style.RESET_ALL}  {r.check}")
            print(f"      {Style.DIM}{r.detail}{Style.RESET_ALL}")
            if loc:
                print(f"      {Style.DIM}{loc}{Style.RESET_ALL}")
            if show_hints and r.remediation:
                print(f"      {Fore.CYAN}→ {r.remediation}{Style.RESET_ALL}")

    print()
    if not failures and not warnings:
        print(f"  {Fore.GREEN}{Style.BRIGHT}All checks passed. Repo looks clean for publication.{Style.RESET_ALL}")
    elif not failures:
        print(f"  {Fore.YELLOW}{Style.BRIGHT}{len(warnings)} warning(s), no hard failures.{Style.RESET_ALL}")
        print("  Review warnings above before publishing.")
    else:
        print(f"  {Fore.RED}{Style.BRIGHT}{len(failures)} finding(s) require attention before publishing.{Style.RESET_ALL}")
        if warnings:
            print(f"  {Fore.YELLOW}{Style.BRIGHT}{len(warnings)} additional warning(s) to review.{Style.RESET_ALL}")
        print(f"\n  {Style.BRIGHT}Next steps:{Style.RESET_ALL}")
        print(f"  1. Fix all findings ({Fore.RED}✗{Style.RESET_ALL}) — these are blockers")
        print(f"  2. Review warnings ({Fore.YELLOW}⚠{Style.RESET_ALL}) — decide case by case")
        print(f"  3. If history contains secrets: create a fresh repo (safest)")
        print(f"     or: pip install git-filter-repo")
        print(f"  4. Re-run this script until clean")

    return len(failures)

# ── HTML report ────────────────────────────────────────────────────────────────

def generate_html_report(
    root: Path,
    output_path: Path,
    template_path: Path,
    history_scanned: bool,
) -> None:
    if not HAS_JINJA:
        print(f"{Fore.YELLOW}  jinja2 not installed — skipping HTML report.{Style.RESET_ALL}")
        print(f"  pip install jinja2")
        return

    if not template_path.exists():
        print(f"{Fore.RED}  Template not found: {template_path}{Style.RESET_ALL}")
        print(f"  Place audit_report.html.j2 next to audit_repo.py, or use --template to specify a path.")
        return

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template(template_path.name)

    total_fail = sum(1 for s in _all_sections for f in s.findings if f.severity == "fail")
    total_warn = sum(1 for s in _all_sections for f in s.findings if f.severity == "warn")

    html = template.render(
        repo_name      = root.name,
        repo_path      = str(root),
        timestamp      = datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        history_scanned= history_scanned,
        sections       = _all_sections,
        total_fail     = total_fail,
        total_warn     = total_warn,
    )

    output_path.write_text(html, encoding="utf-8")
    print(f"\n  {Fore.GREEN}HTML report written:{Style.RESET_ALL} {output_path}")

# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pre-publication secret scanner for homelab-infra"
    )
    parser.add_argument("--no-history",  action="store_true", help="Skip git history scan")
    parser.add_argument("--fix-hints",   action="store_true", help="Show remediation hints per finding")
    parser.add_argument("--report-out",  default=None,
                        help="HTML report output path (default: <script-dir>/audit_report.html)")
    parser.add_argument("--template",    default=None,
                        help="Path to Jinja2 template — if set, an HTML report is generated automatically")
    parser.add_argument("path", nargs="?", default=".",
                        help="Path to repo root (default: current directory)")
    args = parser.parse_args()

    root = Path(args.path).resolve()

    # If no explicit path was given and there's no .git here,
    # walk up to find the repo root automatically
    if args.path == "." and not (root / ".git").exists():
        for parent in root.parents:
            if (parent / ".git").exists():
                root = parent
                break
        else:
            print(f"{Fore.RED}Not a git repository (searched up from {root}){Style.RESET_ALL}")
            print(f"  Run from within a git repo, or pass the repo path explicitly:")
            print(f"  python audit_repo.py C:\\path\\to\\repo")
            return 1

    if not (root / ".git").exists():
        print(f"{Fore.RED}Not a git repository: {root}{Style.RESET_ALL}")
        return 1

    print(f"{Style.BRIGHT}homelab-infra — pre-publication audit{Style.RESET_ALL}")
    print(f"Repo : {root}")
    print(f"Color: {'yes' if HAS_COLOR else 'no  (pip install colorama to enable)'}")

    # ── Run checks ────────────────────────────────────────────────────────────

    begin_section("Tracked .env files")
    check_tracked_env_files(root)

    begin_section(".env.example coverage")
    check_env_example_coverage(root)

    begin_section(".gitignore")
    check_gitignore(root)

    begin_section("Private key files on disk")
    check_private_key_files(root)

    begin_section("Secrets in working tree")
    check_working_tree_patterns(root)

    begin_section("Personal identifiers")
    check_personal_identifiers(root)

    begin_section("Git history")
    if args.no_history:
        skip_section("Skipped via --no-history flag")
        print(f"\n{Fore.YELLOW}  Skipping git history scan (--no-history){Style.RESET_ALL}")
    else:
        print(f"\n{Fore.CYAN}{Style.BRIGHT}── 07 · Git history {Style.RESET_ALL}")
        print(f"{Fore.CYAN}  Scanning all commits — may take a moment...{Style.RESET_ALL}")
        check_git_history(root)

    # ── Console summary ───────────────────────────────────────────────────────

    print(f"\n{Fore.CYAN}{Style.BRIGHT}── Summary {Style.RESET_ALL}")
    failure_count = print_console_results(args.fix_hints)

    # ── HTML report ───────────────────────────────────────────────────────────

    if args.template:
        script_dir    = Path(__file__).parent
        # Template path: relative to CWD so "config/audit_report.html.j2" works
        template_path = Path.cwd() / args.template
        # Report output: explicit path, or default next to the script
        output_path   = Path(args.report_out) if args.report_out else script_dir / "audit_report.html"
        generate_html_report(root, output_path, template_path, not args.no_history)
        if output_path.exists():
            import webbrowser
            webbrowser.open(output_path.resolve().as_uri())

    return 0 if failure_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())