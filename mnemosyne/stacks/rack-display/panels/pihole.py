"""DNS health per Pi-hole instance.

Deliberately not built around the block rate. With a warm cache that number
sits around 1% and never moves, so a big ring would be decoration. Latency and
errors are what actually tell you something is wrong; the block rate is a
supporting figure.

An instance that answers the scrape but exports no query data gets said so
plainly — that is exactly the failure this panel exists to surface.
"""

from __future__ import annotations

from .base import T, Canvas, breath, status_colour


def draw(c: Canvas, state: dict, now: float) -> None:
    nodes = state.get("pihole", [])
    if not nodes:
        c.empty("No Pi-hole data", (40, 180))
        return
    for index, node in enumerate(nodes[:2]):
        _node(c, node, now, x0=40 + index * 620)
        if index:
            c.divider(600)


def _node(c: Canvas, node: dict, now: float, x0: int) -> None:
    has_data = node.get("has_data")
    state = "up" if has_data else "warning"
    c.dot((x0 + 8, 54), status_colour(state), radius=7, halo=not has_data,
          pulse=None if has_data else breath(now))
    c.text(node.get("name", "?"), "heading", T.text, (x0 + 30, 38))

    if not has_data:
        c.text("exporter reachable, no query data", "host", T.warning,
               (x0 + 30, 120))
        c.text("scrape succeeds, so no alert fires", "small", T.muted,
               (x0 + 30, 152))
        return

    latency = node.get("latency_ms")
    errors = node.get("errors") or 0
    timeouts = node.get("timeouts") or 0

    # Latency leads: it is the number that changes when something is wrong.
    if latency is None:
        c.text("n/a", "big_number", T.muted, (x0 + 30, 96))
    else:
        # A warm cache answers in tens of microseconds, so a fixed ms format
        # would read as a permanent zero. Pick the unit from the value.
        value, unit = ((latency * 1000, "\u00b5s") if latency < 1
                       else (latency, "ms"))
        colour = (T.critical if latency > 200
                  else T.warning if latency > 80 else T.text)
        shown = f"{value:.0f}"
        c.text(shown, "big_number", colour, (x0 + 30, 96))
        c.text(unit, "small", T.muted, (x0 + 30 + _w(c, shown) + 6, 124))
    c.label("dns latency p95", (x0 + 32, 148))

    trouble = errors + timeouts
    c.text(f"{trouble:.0f}", "big_number",
           T.critical if trouble else T.muted, (x0 + 210, 96))
    c.label("errors + timeouts, 1m", (x0 + 212, 148))

    rows = [
        ("Blocked", f"{node['block_pct']:.1f} %" if node.get("block_pct") is not None else "n/a"),
        ("Queries", _grouped(node.get("queries"))),
        ("Clients", str(node.get("clients") if node.get("clients") is not None else "n/a")),
        ("Gravity", _grouped(node.get("gravity"))),
    ]
    y = 186
    for label, value in rows:
        c.text(label, "small", T.muted, (x0 + 30, y))
        c.text(value, "small_mono", T.text, (x0 + 500, y), "right")
        y += 30

    trend = node.get("trend") or []
    if len(trend) >= 2:
        c.label("queries per minute, last hour", (x0 + 32, 302))
        c.sparkline((x0 + 30, 322), (470, 42), trend, T.ok)


def _w(c: Canvas, text: str) -> int:
    return c.f["big_number"].size(text)[0]


def _grouped(value) -> str:
    if value is None:
        return "n/a"
    return f"{int(value):,}".replace(",", "\u2009")
