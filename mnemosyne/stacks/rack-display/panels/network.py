"""DSL line and service reachability.

The sync bars show how much of the negotiated maximum the line currently
carries — the figure you would otherwise never look at, and the one that
quietly explains a slow evening.

There are no mesh metrics here: the Lua exporter that would provide them only
runs for the other household, so the backhaul question this panel was meant to
answer has no data behind it yet. Saying nothing beats inventing a number.
"""

from __future__ import annotations

from .base import T, Canvas, breath, status_colour

LEFT = 44
PROBE_LEFT = 660
PROBE_TOP = 116
PROBE_ROW = 34
PROBE_ROWS = 4


def _mbps(value) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _count(value) -> str:
    if value is None:
        return "n/a"
    if value >= 1e6:
        return f"{value / 1e6:.0f}M"
    if value >= 1000:
        return f"{value / 1000:.0f}K"
    return f"{value:.0f}"


def _snr_colour(db):
    """Standard DSL line-quality bands, not thresholds invented for this
    panel: below 6 dB a line is prone to resyncs, 6-10 is marginal."""
    if db is None:
        return T.muted
    if db < 6:
        return T.critical
    if db < 10:
        return T.warning
    return T.ok


def draw(c: Canvas, state: dict, now: float) -> None:
    data = state.get("network") or {}
    if not data:
        c.empty("No network data", (40, 180))
        return
    _headline(c, data, now)
    c.rule(96)
    c.divider(620, top=110, bottom=372)
    _dsl(c, data, now)
    _probes(c, data, now)


def _headline(c: Canvas, data: dict, now: float) -> None:
    age = data.get("age_s")
    if age is not None and age > 300:
        c.text(f"Router data {age / 60:.0f} min old", "heading", T.critical,
               (40, 22))
        return

    failing = data.get("probes_failing", 0)
    if failing:
        mark = c.text(f"{failing} service{'' if failing == 1 else 's'} down",
                      "heading", T.critical, (40, 22))
    else:
        mark = c.text("All reachable", "heading", T.text, (40, 22))

    detail = (f"{_mbps(data.get('throughput_rx_mbps'))} down"
              f"  \u00b7  {_mbps(data.get('throughput_tx_mbps'))} up now"
              f"  \u00b7  {_count(data.get('devices'))} known devices")
    c.text(f"\u00b7  {detail}", "host", T.muted, (mark.right + 22, 30))


def _dsl(c: Canvas, data: dict, now: float) -> None:
    rows = [
        ("Downstream", data.get("rx_curr_mbps"), data.get("rx_max_mbps")),
        ("Upstream", data.get("tx_curr_mbps"), data.get("tx_max_mbps")),
    ]
    y = 116
    for label, current, maximum in rows:
        ratio = (current / maximum) if current and maximum else 0.0
        c.text(label, "small", T.muted, (LEFT, y))
        c.text(f"{_mbps(current)}", "heading", T.text, (330, y - 10), "right")
        c.text(f"/ {_mbps(maximum)} Mbit", "small_mono", T.faint, (346, y + 2))
        # Bronze, not a verdict colour: this is a fill level, and what counts
        # as "too low" depends on the line, not on a number chosen here.
        c.bar((LEFT, y + 38), 500, ratio, T.bronze, height=8)
        c.text(f"{ratio * 100:.0f}%", "small_mono", T.muted, (560, y + 32), "right")
        y += 84

    y = 292
    pairs = [
        ("Noise margin", f"{data.get('snr_rx_db', 0):.0f} / "
                         f"{data.get('snr_tx_db', 0):.0f} dB",
         _snr_colour(data.get("snr_rx_db"))),
        ("Attenuation", f"{data.get('att_rx_db', 0):.0f} / "
                        f"{data.get('att_tx_db', 0):.0f} dB", T.muted),
        ("CRC / FEC errors", f"{_count(data.get('crc_errors'))} / "
                             f"{_count(data.get('fec_errors'))}", T.muted),
    ]
    for label, value, colour in pairs:
        c.text(label, "small", T.muted, (LEFT, y))
        c.text(value, "small_mono", colour, (560, y), "right")
        y += 26


def _probes(c: Canvas, data: dict, now: float) -> None:
    probes = data.get("probes") or []
    if not probes:
        c.empty("No probes", (PROBE_LEFT, 180))
        return
    column_w = 300
    for index, probe in enumerate(probes[:PROBE_ROWS * 2]):
        col, row = divmod(index, PROBE_ROWS)
        x = PROBE_LEFT + col * column_w
        y = PROBE_TOP + row * PROBE_ROW
        ok = probe.get("ok")
        colour = status_colour("up" if ok else "down")
        c.dot((x, y + 9), colour, radius=5, halo=not ok,
              pulse=breath(now, 2.4) if not ok else None)
        name = probe.get("name", "?")
        c.text(name[:20], "small", T.text if ok else colour, (x + 14, y))
        duration = probe.get("duration_ms")
        c.text("down" if not ok else
               ("n/a" if duration is None else f"{duration:.0f} ms"),
               "small_mono", colour if not ok else T.muted,
               (x + column_w - 40, y), "right")

    # Certificate expiry only matters for the internet-facing probe; the
    # internal ones renew from Caddy's own CA.
    external = [p for p in probes if p.get("external") and p.get("ssl_days")]
    if external:
        probe = external[0]
        days = probe["ssl_days"]
        colour = T.critical if days < 7 else T.warning if days < 21 else T.faint
        c.text(f"{probe['name']} certificate expires in {days:.0f} days",
               "tiny", colour, (PROBE_LEFT, PROBE_TOP + PROBE_ROWS * PROBE_ROW + 14))
