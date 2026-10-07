"""Printer status.

One job at a time, so one number dominates. Temperatures sit next to their
targets because the delta is the signal, not the absolute value.

There is no layer counter and no filename in the exporter, so neither is shown
— inventing them would be worse than leaving the space empty.
"""

from __future__ import annotations

from .base import T, Canvas, breath, status_colour


def _duration(seconds) -> str:
    if seconds is None:
        return "n/a"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def draw(c: Canvas, state: dict, now: float) -> None:
    printer = state.get("prusa") or {}

    if not printer.get("up"):
        c.dot((52, 190), status_colour("off"), radius=7)
        c.text("Printer offline", "heading", T.muted, (76, 172))
        c.text("exporter reachable, printer not responding", "small",
               T.faint, (76, 214))
        return

    severity = printer.get("severity", "idle")
    c.dot((52, 54), status_colour(severity), radius=7,
          halo=severity in ("warning", "critical"),
          pulse=breath(now, 3.0) if severity != "idle" else None)
    c.text(printer.get("state", "?"), "heading",
           T.text if severity != "idle" else T.muted, (76, 36))
    # The exporter has no job filename, but it does name the printer.
    if printer.get("printer"):
        c.text(printer["printer"], "small", T.faint, (78, 80))

    c.divider(560)

    progress = printer.get("progress") or 0
    c.ring((300, 232), 84, progress, T.bronze, thickness=12)
    c.text(f"{progress * 100:.0f}", "huge", T.text, (300, 200), "center")
    c.text("%", "small", T.muted, (300, 256), "center")

    y = 48
    for label, actual, target in (
        ("Nozzle", printer.get("nozzle"), printer.get("nozzle_target")),
        ("Bed", printer.get("bed"), printer.get("bed_target")),
    ):
        c.text(label, "small", T.muted, (600, y))
        if actual is None:
            c.text("n/a", "heading", T.muted, (760, y - 10), "right")
        elif not target:
            # A target of 0 means no target is set, not a target of zero
            # degrees. Showing "/ 0C" and a full bar reads as a fault when the
            # printer is simply idle or cooling.
            c.text(f"{actual:.0f}", "heading", T.muted, (760, y - 10), "right")
            c.text("\u00b0C  no target", "small_mono", T.faint, (778, y + 2))
        else:
            off_target = abs(actual - target) > 5
            colour = T.warning if off_target else T.text
            c.text(f"{actual:.0f}", "heading", colour, (760, y - 10), "right")
            c.text(f"/ {target:.0f}\u00b0C", "small_mono", T.faint, (778, y + 2))
            c.bar((600, y + 44), 300, min(actual / target, 1.0),
                  T.warning if off_target else T.ok, height=6)
        y += 96

    stats = [
        ("Remaining", _duration(printer.get("remaining_s"))),
        ("Elapsed", _duration(printer.get("printing_s"))),
        ("Height", f"{printer['z_mm']:.1f} mm" if printer.get("z_mm") is not None else "n/a"),
        ("Speed", f"{printer['speed_pct']:.0f} %" if printer.get("speed_pct") is not None else "n/a"),
        ("Flow", f"{printer['flow_pct']:.0f} %" if printer.get("flow_pct") is not None else "n/a"),
    ]
    y = 48
    for label, value in stats:
        c.text(label, "small", T.muted, (1000, y))
        c.text(value, "small_mono", T.text, (1240, y), "right")
        y += 38
