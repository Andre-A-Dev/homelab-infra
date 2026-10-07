"""What fired in the last 24 hours.

Answers the one question nothing else answers well in passing: was there
anything overnight? Grafana can show it, but only if you go looking and know
what to look for — here an empty area is the answer.

Rows for currently silenced alerts are dimmed rather than dropped. A silence
means "do not wake me", not "did not happen", and a history that hides
silenced incidents would be lying by omission.
"""

from __future__ import annotations

import time

from .base import T, Canvas, breath, scale, status_colour

MAX_ROWS = 6
ROW_H = 40
GRID_TOP = 118
TRACK_LEFT = 360
TRACK_RIGHT = 1240
AXIS_Y = 372


def draw(c: Canvas, state: dict, now: float) -> None:
    data = state.get("alert_history") or {}
    rows = data.get("rows", [])
    _header(c, data, rows)
    _axis(c, data)
    if not rows:
        c.text("Nothing fired in the last 24 hours", "host", T.muted,
               ((TRACK_LEFT + TRACK_RIGHT) / 2, 210), "center")
        return
    _rows(c, data, rows, now)


def _header(c: Canvas, data: dict, rows: list) -> None:
    incidents = data.get("incidents", 0)
    if not incidents:
        headline, colour = "Quiet", T.text
        detail = "no alerts in 24h"
    else:
        headline, colour = f"{incidents} incident{'' if incidents == 1 else 's'}", T.text
        names = len(rows)
        detail = f"across {names} alert{'' if names == 1 else 's'}, last 24h"
    mark = c.text(headline, "heading", colour, (40, 22))
    c.text(f"\u00b7  {detail}", "host", T.muted, (mark.right + 22, 30))


def _x(data: dict, offset: float) -> float:
    span = max(1, data.get("window_s", 86400))
    return TRACK_LEFT + (TRACK_RIGHT - TRACK_LEFT) * min(offset / span, 1.0)


def _axis(c: Canvas, data: dict) -> None:
    """Hour marks every six hours, labelled with wall clock time — the
    question is "was there something at 3am", not "was there something 19
    hours ago"."""
    c.rule(96, left=40, right=TRACK_RIGHT)
    start = data.get("start_epoch")
    window = data.get("window_s", 86400)
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        x = TRACK_LEFT + (TRACK_RIGHT - TRACK_LEFT) * fraction
        import pygame
        pygame.draw.line(c.s, T.hairline, (x, GRID_TOP - 12), (x, AXIS_Y - 14))
        if start:
            label = time.strftime("%H:%M", time.localtime(start + window * fraction))
        else:
            label = ""
        align = "left" if fraction == 0 else "right" if fraction == 1 else "center"
        anchor = x + (2 if fraction == 0 else -2 if fraction == 1 else 0)
        c.text(label, "tiny", T.faint, (anchor, AXIS_Y - 8), align)


def _instances(row: dict) -> str:
    """Shorten hosts and URLs so two of them fit, and count the rest rather
    than truncating mid-string."""
    raw = row.get("instances") or []
    total = row.get("instance_count", len(raw))
    short = []
    for item in raw:
        name = item.split("://")[-1].rstrip("/")
        short.append(name if len(name) <= 18 else name[:17] + "\u2026")
    text = ", ".join(short[:2])
    if total > 2:
        text = f"{text} +{total - 2}"
    return text


def _rows(c: Canvas, data: dict, rows: list, now: float) -> None:
    import pygame
    shown = rows[:MAX_ROWS]
    for index, row in enumerate(shown):
        y = GRID_TOP + index * ROW_H
        colour = status_colour(row.get("severity", "warning"))
        dimmed = row.get("silenced")
        if dimmed:
            colour = scale(colour, 0.45)

        c.dot((52, y + 9), colour, radius=5,
              pulse=breath(now, 2.6) if row.get("spans") and not dimmed
              and row["spans"][-1][1] >= data.get("window_s", 0) - 600 else None)
        c.text(row["name"][:26], "small", T.text if not dimmed else T.muted,
               (70, y))
        meta = _instances(row)
        if row.get("silenced"):
            meta = f"{meta}  silenced".strip()
        if meta:
            c.text(meta, "tiny", T.faint, (70, y + 20))

        for start_offset, end_offset in row.get("spans", []):
            x0 = _x(data, start_offset)
            # A five minute incident is three pixels wide across a 24h track,
            # too narrow to read the colour off. Give every span enough width
            # to be legible; the exact duration is not the point here.
            x1 = max(_x(data, end_offset), x0 + 8)
            pygame.draw.rect(c.s, colour, (x0, y + 2, x1 - x0, 14),
                             border_radius=7)

        if row.get("incidents", 0) > 1:
            c.text(f"{row['incidents']}\u00d7", "tiny", T.faint,
                   (TRACK_RIGHT, y + 20), "right")

    if len(rows) > MAX_ROWS:
        c.text(f"+{len(rows) - MAX_ROWS} more", "tiny", T.faint,
               (TRACK_RIGHT, GRID_TOP + MAX_ROWS * ROW_H - 6), "right")
