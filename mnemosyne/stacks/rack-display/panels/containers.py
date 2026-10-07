"""All containers at once, as a grid.

42 entries do not need scrolling, they need a shape: colour-coded dots read at
a glance, and an exception is found by colour rather than by position. The two
header lines carry the verdict, so in the normal case you read one sentence
and are done.

Ordering is by compose project then name, and deliberately stable. Sorting
exceptions to the front would move every position on every incident and
destroy any chance of learning where a container sits — while red pops out
regardless of where it is.
"""

from __future__ import annotations

import math

from .base import T, Canvas, breath, status_colour

ROWS = 7
MAX_COLS = 6
GRID_TOP = 112
ROW_H = 38
GRID_LEFT = 44
GRID_RIGHT = 1244


def draw(c: Canvas, state: dict, now: float) -> None:
    data = state.get("containers") or {}
    if not data:
        c.empty("No container data", (40, 180))
        return
    _verdict(c, data)
    _resources(c, data)
    c.rule(96)
    _grid(c, data, now)


def _verdict(c: Canvas, data: dict) -> None:
    """One readable sentence. In the normal case it is the whole answer."""
    running = c.text(f"{data.get('running', '?')} running", "heading",
                     T.text, (40, 22))
    notes = [f"{item['name']} {item['note']}"
             for item in data.get("items", []) if item.get("note")]
    if not notes:
        text, colour = "nothing unusual", T.muted
    elif len(notes) <= 3:
        text, colour = "  \u00b7  ".join(notes), T.warning
    else:
        text, colour = f"{len(notes)} containers need attention", T.warning
    c.text(f"\u00b7  {text}", "host", colour, (running.right + 22, 30))


def _resources(c: Canvas, data: dict) -> None:
    pressure = data.get("pressure_pct")
    parts = [f"{data.get('mem_text', '?')} used",
             f"{data.get('swap_text', '0')} swap"]
    if pressure is None:
        parts.append("pressure n/a")
        colour = T.faint
    elif pressure < 1:
        parts.append("no memory pressure")
        colour = T.faint
    else:
        # Only name a culprit when there is actually something to blame for.
        blame = data.get("top_name")
        parts.append(f"memory pressure {pressure:.0f}%")
        if blame:
            parts.append(f"{blame} {data.get('top_mem_text', '')}")
        colour = T.warning
    c.text("  \u00b7  ".join(parts), "small", colour, (42, 62))


def _grid(c: Canvas, data: dict, now: float) -> None:
    items = list(data.get("items", []))
    capacity = ROWS * MAX_COLS
    overflow = 0
    if len(items) > capacity:
        # Keep every exception, drop healthy ones off the end. No scrolling and
        # no hidden gesture — the count says what was left out.
        exceptions = [i for i in items if i["state"] != "up"]
        healthy = [i for i in items if i["state"] == "up"]
        keep = max(0, capacity - 1 - len(exceptions))
        overflow = len(healthy) - keep
        items = exceptions + healthy[:keep]
        items.sort(key=lambda i: (i["project"], i["name"]))

    cols = min(MAX_COLS, max(1, math.ceil(len(items) / ROWS)))
    cell_w = (GRID_RIGHT - GRID_LEFT) / cols
    label_w = int(cell_w) - 26

    for index, item in enumerate(items):
        # Column-major so compose projects stay together vertically instead of
        # being torn across the full width.
        col, row = divmod(index, ROWS)
        if col >= cols:
            break
        x = GRID_LEFT + col * cell_w
        y = GRID_TOP + row * ROW_H
        st = item["state"]
        colour = status_colour("critical" if st == "gone" else st)
        c.dot((x, y + 9), colour, radius=5, halo=st != "up",
              pulse=breath(now, 2.4) if st in ("gone", "critical") else None)
        # The "gone" suffix needs its own room, or it lands on the name.
        available = label_w - (44 if st == "gone" else 0)
        c.text(_fit(c, item["name"], available), "tiny",
               T.text if st == "up" else colour, (x + 14, y))
        if st == "gone":
            c.text("gone", "tiny", T.critical, (x + cell_w - 18, y), "right")

    if overflow:
        c.text(f"+{overflow} more healthy", "tiny", T.faint,
               (GRID_RIGHT, GRID_TOP + ROWS * ROW_H - 12), "right")


def _fit(c: Canvas, text: str, width: int) -> str:
    font = c.f["tiny"]
    if font.size(text)[0] <= width:
        return text
    while text and font.size(text + "\u2026")[0] > width:
        text = text[:-1]
    return text + "\u2026"
