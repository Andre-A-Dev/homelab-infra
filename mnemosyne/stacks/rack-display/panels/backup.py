"""The backup chain in full.

Four stages down the left, the per-service archive list on the right. The
headline belongs to coverage, because "nothing is watching this mount" is a
different and worse problem than "a run was late" — and no other view reports
it at all.

No bars: immich is 43 GB against kosync at 1.1 KB, and immich takes 367s
against most services at zero. Any linear bar would be a single full block and
fourteen invisible ones.
"""

from __future__ import annotations

from .base import T, Canvas, breath, status_colour

CHAIN_X = 44
CHAIN_TOP = 118
CHAIN_H = 64
LIST_LEFT = 600
LIST_TOP = 116
LIST_ROW = 34
LIST_ROWS = 8


def _age(seconds) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def _duration(seconds) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _count(value) -> str:
    return "?" if value is None else f"{value:.0f}"


def draw(c: Canvas, state: dict, now: float) -> None:
    data = state.get("backup") or {}
    if not data:
        c.empty("No backup data", (40, 180))
        return
    _headline(c, data, now)
    c.rule(96)
    c.divider(560, top=110, bottom=372)
    _chains(c, data, now)
    _services(c, data, now)


def _headline(c: Canvas, data: dict, now: float) -> None:
    mounts = data.get("uncovered_mounts") or 0
    services = data.get("uncovered_services") or 0
    if mounts or services:
        # This outranks everything else on the panel: a late run is visible in
        # a dozen places, an unwatched mount in none.
        parts = []
        if mounts:
            parts.append(f"{mounts:.0f} mount{'' if mounts == 1 else 's'}")
        if services:
            parts.append(f"{services:.0f} service{'' if services == 1 else 's'}")
        c.text(" and ".join(parts) + " not covered by any backup", "heading",
               T.critical, (40, 22))
        return

    disk_problem = _disk_problem(data)
    if disk_problem:
        c.text(disk_problem, "heading", T.critical, (40, 22))
        return

    mark = c.text("All covered", "heading", T.text, (40, 22))
    skipped = data.get("skipped_total") or 0
    detail = f"{len(data.get('services', []))} services"
    if skipped:
        detail += f"  \u00b7  {skipped:.0f} skipped"
    detail += f"  \u00b7  last run {_age(data.get('local_age'))} ago"
    c.text(f"\u00b7  {detail}", "host", T.muted, (mark.right + 22, 30))


def _disk_problem(data: dict) -> str | None:
    """Only speaks up when something is wrong. A healthy disk count is a
    number in passing, not a headline."""
    failing = data.get("disks_failing") or []
    if failing:
        return f"SMART failing on {', '.join(failing)}"
    seen, expected = data.get("disks_seen"), data.get("disks_expected")
    if expected and seen is not None and seen < expected:
        return f"Only {seen:.0f} of {expected} disks visible to smartctl"
    return None


def _disks_text(data: dict) -> str:
    seen, expected = data.get("disks_seen"), data.get("disks_expected")
    if seen is None:
        return ""
    if expected:
        return f"  \u00b7  {seen:.0f}/{expected} disks"
    return f"  \u00b7  {seen:.0f} disks"


def _chains(c: Canvas, data: dict, now: float) -> None:
    rows = [
        ("Local", data.get("local_state"), data.get("local_age"), [
            f"ran {_duration(data.get('local_duration_s'))}",
            f"{_count(data.get('disk_usage_pct'))}% used"
            f"  \u00b7  {_count(data.get('disk_free_gb'))}G free"
            + _disks_text(data),
        ]),
        ("Offsite", data.get("offsite_state"), data.get("offsite_age"), [
            f"ran {_duration(data.get('offsite_duration_s'))}",
            f"{_count(data.get('offsite_snapshots'))} snapshots"
            f"  \u00b7  {_count(data.get('offsite_files_new'))} new",
        ]),
        ("Verify", data.get("verify_state"), data.get("verify_age"), [
            f"{_count(data.get('verify_pass'))} pass"
            f"  \u00b7  {_count(data.get('verify_fail'))} fail"
            f"  \u00b7  {_count(data.get('verify_skip'))} skip",
            "quick run" if data.get("verify_quick") else "full run",
        ]),
        ("Maintenance", data.get("maintenance_state"),
         data.get("maintenance_age"), [
            f"ran {_duration(data.get('maintenance_duration_s'))}",
            f"check {_ok(data.get('maintenance_check_ok'))}"
            f"  \u00b7  prune {_ok(data.get('maintenance_prune_ok'))}",
        ]),
    ]
    for index, (label, state, age, details) in enumerate(rows):
        y = CHAIN_TOP + index * CHAIN_H
        colour = status_colour(state or "unknown")
        attention = state in ("warning", "critical")
        c.dot((CHAIN_X + 8, y + 12), colour, radius=6, halo=attention,
              pulse=breath(now) if attention else None)
        c.text(label, "host", T.text, (CHAIN_X + 26, y))
        c.text(_age(age), "metric", colour if attention else T.muted,
               (520, y + 2), "right")
        c.text(details[0], "tiny", T.faint, (CHAIN_X + 26, y + 26))
        c.text(details[1], "tiny", T.faint, (CHAIN_X + 26, y + 42))


def _ok(value) -> str:
    if value is None:
        return "?"
    return "ok" if value else "failed"


def _services(c: Canvas, data: dict, now: float) -> None:
    services = data.get("services") or []
    if not services:
        c.empty("No per-service data", (LIST_LEFT, 180))
        return
    column_w = 330
    for index, item in enumerate(services[:LIST_ROWS * 2]):
        col, row = divmod(index, LIST_ROWS)
        x = LIST_LEFT + col * column_w
        y = LIST_TOP + row * LIST_ROW
        state = item.get("state", "up")
        attention = state != "up"
        c.dot((x, y + 9), status_colour(state), radius=5, halo=attention,
              pulse=breath(now, 2.6) if state == "critical" else None)
        c.text(item["name"][:18], "small",
               T.text if not attention else status_colour(state), (x + 14, y))
        right_text = item.get("note") or item.get("size_text") or "\u2014"
        c.text(right_text, "small_mono",
               status_colour(state) if attention else T.muted,
               (x + column_w - 34, y), "right")

    if len(services) > LIST_ROWS * 2:
        c.text(f"+{len(services) - LIST_ROWS * 2} more", "tiny", T.faint,
               (LIST_LEFT + column_w * 2 - 34, LIST_TOP + LIST_ROWS * LIST_ROW),
               "right")
