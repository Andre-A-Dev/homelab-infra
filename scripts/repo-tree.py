#!/usr/bin/env python3
"""
repo-tree.py — Print a repository directory tree respecting .gitignore

Usage:
    python repo-tree.py                        # tree of current directory
    python repo-tree.py /path/to/repo          # explicit path
    python repo-tree.py --depth 3              # limit depth
    python repo-tree.py --out tree.txt         # write to file
    python repo-tree.py --depth 2 --out tree.txt /path/to/repo

Options:
    path            Root directory (default: current directory)
    --depth N       Maximum depth to display (default: unlimited)
    --out FILE      Write output to file instead of stdout
    --all           Include files ignored by .gitignore (still hides .git/)

Requirements: Python 3.6+, no external packages
"""

import argparse
import os
import re
import sys
from pathlib import Path

# ── Always-ignore list ────────────────────────────────────────────────────────
# Excluded regardless of .gitignore content.

ALWAYS_IGNORE: set[str] = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
}

# ── .gitignore parser ─────────────────────────────────────────────────────────

class GitignoreFilter:
    """
    Minimal .gitignore pattern matcher.

    Supports:
      - Literal filenames       (e.g. secrets.txt)
      - Wildcard patterns       (e.g. *.env, *.log)
      - Directory anchoring     (trailing slash: build/)
      - Root anchoring          (leading slash: /dist)
      - Negation                (leading !: !important.log)
      - Double-star glob        (**) treated as any path segment match

    Does NOT support: character classes ([abc]), complex ** in mid-path.
    Covers ~95% of real-world .gitignore usage.
    """

    def __init__(self, root: Path):
        self.root = root
        self.rules: list[tuple[bool, bool, bool, str]] = []
        # (negated, dir_only, anchored, pattern)
        self._load(root / ".gitignore")

    def _load(self, path: Path) -> None:
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            negated = line.startswith("!")
            if negated:
                line = line[1:]

            dir_only = line.endswith("/")
            if dir_only:
                line = line.rstrip("/")

            # Anchored if slash present (other than trailing)
            anchored = "/" in line
            if line.startswith("/"):
                line = line.lstrip("/")

            self.rules.append((negated, dir_only, anchored, line))

    def _match_pattern(self, pattern: str, name: str, rel: str) -> bool:
        """Match a single pattern against name (basename) and rel (relative path)."""
        # ** — match any path segment
        if "**" in pattern:
            regex = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
            return bool(re.fullmatch(regex, rel)) or bool(re.fullmatch(regex, name))

        # Wildcard in pattern — fnmatch-style against basename
        if "*" in pattern or "?" in pattern:
            return _fnmatch(pattern, name)

        # Plain name or path segment
        return name == pattern or rel == pattern or rel.endswith("/" + pattern)

    def is_ignored(self, path: Path, is_dir: bool = False) -> bool:
        """Return True if path should be ignored according to .gitignore rules."""
        try:
            rel = str(path.relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return False

        name = path.name
        ignored = False

        for negated, dir_only, anchored, pattern in self.rules:
            if dir_only and not is_dir:
                continue
            if anchored:
                match = _fnmatch(pattern, rel) or rel == pattern
            else:
                match = self._match_pattern(pattern, name, rel)

            if match:
                ignored = not negated

        return ignored


def _fnmatch(pattern: str, name: str) -> bool:
    """Translate a glob pattern to regex and match."""
    regex = re.escape(pattern)
    regex = regex.replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return bool(re.fullmatch(regex, name)) or bool(re.fullmatch(regex, name.split("/")[-1]))


def _always_ignored(name: str) -> bool:
    """Check name against the always-ignore set (supports * wildcards)."""
    for pattern in ALWAYS_IGNORE:
        if "*" in pattern:
            if _fnmatch(pattern, name):
                return True
        elif name == pattern:
            return True
    return False

# ── Tree builder ──────────────────────────────────────────────────────────────

def build_tree(
    root: Path,
    gitignore: GitignoreFilter,
    max_depth: int | None,
    respect_gitignore: bool,
) -> tuple[list[str], int, int]:
    """
    Walk the directory tree and build output lines.

    Returns:
        lines       — list of formatted tree lines
        dir_count   — total directories visited
        file_count  — total files visited
    """
    lines: list[str] = []
    dir_count = 0
    file_count = 0

    def walk(path: Path, prefix: str, depth: int) -> None:
        nonlocal dir_count, file_count

        if max_depth is not None and depth > max_depth:
            return

        try:
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return

        # Filter entries
        visible = []
        for entry in entries:
            if _always_ignored(entry.name):
                continue
            if respect_gitignore and gitignore.is_ignored(entry, is_dir=entry.is_dir()):
                continue
            visible.append(entry)

        for i, entry in enumerate(visible):
            is_last = i == len(visible) - 1
            connector = "└── " if is_last else "├── "
            extension = "    " if is_last else "│   "

            # Annotation
            annotation = ""
            if entry.is_symlink():
                target = os.readlink(entry)
                annotation = f" -> {target}"

            if entry.is_dir() and not entry.is_symlink():
                dir_count += 1
                lines.append(f"{prefix}{connector}{entry.name}/{annotation}")
                walk(entry, prefix + extension, depth + 1)
            elif entry.is_dir() and entry.is_symlink():
                dir_count += 1
                lines.append(f"{prefix}{connector}{entry.name}/ [symlink{annotation}]")
            else:
                file_count += 1
                lines.append(f"{prefix}{connector}{entry.name}{annotation}")

    walk(root, "", 1)
    return lines, dir_count, file_count

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print a repository directory tree respecting .gitignore",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Root directory (default: current directory)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=None,
        metavar="N",
        help="Maximum depth to display (default: unlimited)",
    )
    parser.add_argument(
        "--out",
        default=None,
        metavar="FILE",
        help="Write output to file instead of stdout",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include files ignored by .gitignore (still hides .git/ and __pycache__)",
    )
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 1

    gitignore = GitignoreFilter(root)
    respect_gitignore = not args.all

    tree_lines, dir_count, file_count = build_tree(
        root=root,
        gitignore=gitignore,
        max_depth=args.depth,
        respect_gitignore=respect_gitignore,
    )

    depth_label = f"  depth: {args.depth}" if args.depth is not None else ""
    gitignore_label = "  .gitignore: active" if respect_gitignore else "  .gitignore: ignored (--all)"

    output_lines = [
        f"{root.name}/",
        *tree_lines,
        "",
        f"{dir_count} directories, {file_count} files",
        f"{gitignore_label}{depth_label}",
    ]

    output = "\n".join(output_lines)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")
        print(f"Tree written to {out_path}  ({dir_count} dirs, {file_count} files)")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
