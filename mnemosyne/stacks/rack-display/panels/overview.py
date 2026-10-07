"""Hosts, alerts, backup — the panel that answers "is anything wrong".

Replaces both render_overview in the old renderer and _dashboard_fallback in
the client. Those two drew the same thing from different code and were already
drifting apart.
"""

from __future__ import annotations

from .base import T, Canvas, breath, status_colour


def _age_text(seconds) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def draw(c: Canvas, state: dict, now: float) -> None:
    c.divider(560)
    c.divider(1020)
    _hosts(c, state, now)
    _alerts(c, state, now)
    _backup(c, state)
    _rack(c, state, now)


def _hosts(c: Canvas, state: dict, now: float) -> None:
    y = 40
    for host in state.get("hosts", []):
        hs = host.get("state", "unknown")
        attention = hs in ("down", "critical", "warning")
        c.dot((52, y + 13), status_colour(hs), halo=attention,
              pulse=breath(now) if attention else None)
        name = c.text(host.get("name", "?"), "host", T.text, (76, y))
        if host.get("os") == "windows":
            c.text("win", "tiny", T.muted, (name.right + 10, y + 8))

        if hs == "up":
            cpu = host.get("cpu")
            if cpu is not None:
                c.bar((300, y + 11), 96, cpu / 100,
                      T.warning if cpu >= 85 else T.ok)
                c.text(f"{cpu:.0f}", "metric", T.muted, (446, y + 2), "right")
                c.text("%", "tiny", T.faint, (450, y + 8))
            temp = host.get("temp")
            if temp is not None:
                c.text(f"{temp:.0f}", "metric",
                       T.warning if temp >= 70 else T.muted, (506, y + 2), "right")
                c.text("\u00b0C", "tiny", T.faint, (510, y + 8))
        else:
            c.text(hs, "metric", status_colour(hs), (520, y + 2), "right")
        y += 44


def _alerts(c: Canvas, state: dict, now: float) -> None:
    alerts = state.get("alerts", [])
    if not alerts:
        c.empty("No active alerts", (600, 40))
        return
    y = 40
    for alert in alerts[:5]:
        c.dot((600, y + 13), status_colour(alert.get("severity", "warning")),
              halo=True, pulse=breath(now, 2.6))
        c.text(alert.get("name", "?"), "host", T.text, (624, y))
        detail = alert.get("instance") or alert.get("summary", "")
        c.text(detail[:44], "small", T.muted, (624, y + 27))
        y += 56
    if len(alerts) > 5:
        c.text(f"+{len(alerts) - 5} more", "small", T.muted, (624, y))


def _rack(c: Canvas, state: dict, now: float) -> None:
    """Ambient air and draw, separated from the backup rows by a hairline so
    the two groups do not read as one list."""
    rack = state.get("rack") or {}
    if not rack:
        return
    import pygame
    pygame.draw.line(c.s, T.hairline, (1060, 196), (1240, 196))

    temp = rack.get("temp_c")
    temp_state = rack.get("temp_state", "unknown")
    attention = temp_state in ("warning", "critical")
    c.dot((1060, 220), status_colour(temp_state), radius=5,
          pulse=breath(now) if attention else None)
    c.text("Rack", "small", T.muted, (1078, 210))
    c.text("n/a" if temp is None else f"{temp:.1f}\u00b0C", "small_mono",
           status_colour(temp_state) if attention else T.text, (1240, 210), "right")

    power = rack.get("power_w")
    online = rack.get("plug_online")
    c.dot((1060, 254), status_colour("ok" if online else "unknown"), radius=5)
    c.text("Power", "small", T.muted, (1078, 244))
    c.text("n/a" if power is None else f"{power:.0f} W", "small_mono",
           T.text, (1240, 244), "right")


def _backup(c: Canvas, state: dict) -> None:
    b = state.get("backup", {})
    rows = [
        ("Local", _age_text(b.get("local_age")), b.get("local_state")),
        ("Offsite", _age_text(b.get("offsite_age")), b.get("offsite_state")),
        ("Verify", b.get("verify_text", "n/a"), b.get("verify_state")),
        ("Free", b.get("free_text", "n/a"), "ok"),
    ]
    y = 42
    for label, value, st in rows:
        c.dot((1060, y + 10), status_colour(st or "unknown"), radius=5)
        c.text(label, "small", T.muted, (1078, y))
        c.text(value, "small_mono", T.text, (1240, y), "right")
        y += 34
