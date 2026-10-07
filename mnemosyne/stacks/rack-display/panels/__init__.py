"""Panel registry.

Adding a panel: write a module with draw(canvas, state, now), register it
here, list its id in display.yml under `pages`. Nothing else changes.
"""

from . import alerts, backup, containers, network, overview, pihole, prusa

PANELS = {
    "overview": overview.draw,
    "pihole": pihole.draw,
    "containers": containers.draw,
    "prusa": prusa.draw,
    "alerts": alerts.draw,
    "backup": backup.draw,
    "network": network.draw,
}


def get(name: str):
    return PANELS.get(name)


def names() -> list[str]:
    return sorted(PANELS)
