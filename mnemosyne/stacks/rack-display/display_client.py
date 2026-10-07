#!/usr/bin/env python3
"""rack-display client.

Drives the 1280x400 rack panel from a state document produced by the
renderer. Everything except the DASHBOARD screen is drawn locally, so the
panel keeps telling the truth when the renderer is unreachable.

Screens:
    IDLE       wordmark plus one status dot per host, breathing, drifting
    DIM        a single low dot. Proves the host is alive without the backlight
    DASHBOARD  the rendered panel, or a local fallback if it cannot be fetched
    ALERT      full-bleed critical or stale takeover, drawn locally, ignores touch

Dry run on a desktop:
    RACK_DISPLAY_BACKEND=window \\
    RACK_DISPLAY_STATE=file://$PWD/mock_state.json \\
    python3 display_client.py

Keys in window mode: 1 idle, 2 dashboard, 3 alert, 4 dim, q quit.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pygame
import yaml

import panels
import panels.base
from mirror import Mirror
from panels.base import Canvas, load_fonts

LOG = logging.getLogger("rack-display")

CONFIG_PATH = Path(os.environ.get("RACK_DISPLAY_CONFIG", "display.yml"))

# Screen identifiers. Kept as plain strings so they can appear in the
# heartbeat metric without a translation table.
IDLE, DIM, DASHBOARD, ALERT = "idle", "dim", "dashboard", "alert"
BOOT = "boot"


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: Any, default: float = 0.0) -> float:
    """Accept 5, "5s", "15m", "10h" and return seconds. 0 means "never"."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"unparseable duration: {value!r}")
    return float(match.group(1)) * _UNITS.get(match.group(2) or "s", 1.0)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def scale(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in colour)  # type: ignore[return-value]


def lerp(a: tuple[int, int, int], b: tuple[int, int, int],
         t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            LOG.warning("no config at %s, falling back to built-in defaults", path)
            return cls({})
        with path.open(encoding="utf-8") as handle:
            return cls(yaml.safe_load(handle) or {})


# --------------------------------------------------------------------------
# state source
# --------------------------------------------------------------------------

@dataclass
class State:
    """The last successfully fetched state document, plus its age."""

    data: dict = field(default_factory=dict)
    fetched_at: float = 0.0
    ever_fetched: bool = False

    @property
    def hosts(self) -> list[dict]:
        return self.data.get("hosts", [])

    @property
    def alerts(self) -> list[dict]:
        return self.data.get("alerts", [])

    @property
    def overall(self) -> str:
        return self.data.get("overall", "unknown")

    def age(self, now: float) -> float:
        return now - self.fetched_at if self.ever_fetched else float("inf")

    def is_stale(self, now: float, threshold: float) -> bool:
        return self.age(now) > threshold

    def worst_alert(self) -> dict | None:
        """Highest-severity firing alert, critical before warning."""
        ranked = sorted(
            self.alerts,
            key=lambda a: 0 if a.get("severity") == "critical" else 1,
        )
        return ranked[0] if ranked else None

    def has_critical(self) -> bool:
        return any(a.get("severity") == "critical" for a in self.alerts)


class StateSource:
    """Polls the renderer. Never raises into the render loop."""

    def __init__(self, state_url: str, interval: float):
        self.state_url = state_url
        self.interval = interval
        self.state = State()
        self.last_error: str | None = None
        self._next_poll = 0.0

    def _read(self, url: str, timeout: float = 3.0) -> bytes:
        if url.startswith("file://"):
            return Path(url[7:]).read_bytes()
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.read()

    def poll(self, now: float) -> None:
        if now < self._next_poll:
            return
        self._next_poll = now + self.interval
        try:
            payload = json.loads(self._read(self.state_url))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            # Staleness is derived from fetched_at, so a failure needs no flag.
            # The text is kept only for the boot diagnostics.
            LOG.warning("state fetch failed: %s", exc)
            reason = getattr(exc, "reason", exc)
            self.last_error = str(getattr(reason, "strerror", None) or reason)
            return
        self.state.data = payload
        self.state.fetched_at = now
        self.state.ever_fetched = True
        self.last_error = None


# --------------------------------------------------------------------------
# theme
# --------------------------------------------------------------------------

class Theme:
    DEFAULTS = {
        "bg": "#14161A", "surface": "#1C1F24", "hairline": "#2A2E35",
        "text": "#E6E8EA", "muted": "#868D96", "faint": "#4A5058",
        "bronze": "#C08A3E",
        # Desaturated on purpose: the signature must never be mistaken for a
        # status colour sitting next to the host dots.
        "teal": "#3FA7A0",
        "ok": "#4E9A6A", "warning": "#E0A32E", "critical": "#E04F4F",
    }
    SIZES = {
        "wordmark": 96, "host": 24, "metric": 22,
        "alert_title": 30, "alert_body": 20, "small": 18, "tiny": 15,
    }

    def __init__(self, config: Config):
        palette = {**self.DEFAULTS, **(config.get("theme") or {})}
        for name, value in palette.items():
            setattr(self, name, hex_to_rgb(value))
        sizes = {**self.SIZES, **(config.get("typography.sizes") or {})}
        sans = config.get("typography.sans", "IBM Plex Sans")
        mono = config.get("typography.mono", "IBM Plex Mono")
        self.fonts = {
            "wordmark": self._font(sans, sizes["wordmark"], bold=True),
            "host": self._font(sans, sizes["host"]),
            "metric": self._font(mono, sizes["metric"]),
            "alert_title": self._font(sans, sizes["alert_title"], bold=True),
            "alert_body": self._font(sans, sizes["alert_body"]),
            "small": self._font(sans, sizes["small"]),
            "small_mono": self._font(mono, sizes["small"]),
            "tiny": self._font(sans, sizes["tiny"]),
        }

    @staticmethod
    def _font(family: str, size: int, bold: bool = False) -> pygame.font.Font:
        """Resolve a family, falling back to whatever the host actually has."""
        path = pygame.font.match_font(family.replace(" ", "").lower(), bold=bold)
        if path:
            return pygame.font.Font(path, size)
        LOG.warning("font %r not installed, using default face", family)
        return pygame.font.Font(None, size)

    def status_colour(self, state: str) -> tuple[int, int, int]:
        return {
            "up": self.ok,
            "ok": self.ok,
            "warning": self.warning,
            "down": self.critical,
            "critical": self.critical,
        }.get(state, self.muted)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

class Renderer:
    def __init__(self, surface: pygame.Surface, theme: Theme, config: Config):
        self.surface = surface
        self.theme = theme
        self.width, self.height = surface.get_size()
        self.drift_px = float(config.get("burn_in.drift_px", 20))
        self.drift_cycle = parse_duration(config.get("burn_in.cycle"), 600.0)
        self._boot_note = ""
        self.dim_brightness = float(config.get("blank.brightness", 0.06))
        self.dim_drifts = bool(config.get("blank.drift", True))

    def retarget(self, surface: pygame.Surface) -> None:
        """Point drawing at another surface — used to render the incoming page
        off-screen while the outgoing one is still visible."""
        self.surface = surface
        self.canvas.s = surface

    def touch_ring(self, pos, age: float, duration: float) -> None:
        """Immediate acknowledgement. The panel has no haptics and taps inside
        the debounce window are dropped, so without this you tap, see nothing,
        tap again, and the second one is swallowed too."""
        progress = min(age / duration, 1.0)
        radius = int(10 + 50 * (1 - (1 - progress) ** 3))
        alpha = int(150 * (1 - progress))
        if alpha <= 0:
            return
        size = radius * 2 + 6
        layer = pygame.Surface((size, size), pygame.SRCALPHA)
        pygame.draw.circle(layer, (*self.theme.bronze, alpha),
                           (size // 2, size // 2), radius, 3)
        self.surface.blit(layer, (pos[0] - size // 2, pos[1] - size // 2))

    def pager(self, index: int, count: int, age: float, duration: float) -> None:
        """Position feedback, deliberately transient.

        The seven dots on IDLE already mean host status and on BOOT mean a
        chase light. A third permanent meaning here would be one too many —
        showing these only right after a page change keeps them unambiguous,
        and avoids fighting the container grid for the bottom rows.
        """
        if count < 2:
            return
        fade = 1.0 if age < duration - 0.4 else max(0.0, (duration - age) / 0.4)
        spacing, radius = 20, 4
        width = count * spacing + 24
        y = self.height - 23
        scrim = pygame.Surface((width, 22), pygame.SRCALPHA)
        pygame.draw.rect(scrim, (*self.theme.bg, int(210 * fade)),
                         (0, 0, width, 22), border_radius=11)
        self.surface.blit(scrim, (self.width / 2 - width / 2, y - 11))
        start = self.width / 2 - (count - 1) * spacing / 2
        for i in range(count):
            colour = self.theme.bronze if i == index else self.theme.faint
            self._dot((start + i * spacing, y), scale(colour, fade), radius=radius)

    def drift(self, now: float) -> tuple[float, float]:
        """Slow Lissajous offset. Vertical travel is smaller — only 400px tall."""
        if self.drift_cycle <= 0:
            return 0.0, 0.0
        phase = 2 * math.pi * (now % self.drift_cycle) / self.drift_cycle
        return (
            math.sin(phase) * self.drift_px,
            math.cos(phase * 1.7) * self.drift_px * 0.4,
        )

    @staticmethod
    def breath(now: float, period: float = 4.0, low: float = 0.55) -> float:
        """low..1.0, eased. Slow enough to read as breathing, not blinking."""
        return low + (1 - low) * (0.5 + 0.5 * math.sin(2 * math.pi * now / period))

    def _text(self, text: str, font: str, colour, pos, align: str = "left"):
        surf = self.theme.fonts[font].render(text, True, colour)
        rect = surf.get_rect()
        setattr(rect, {"left": "topleft", "right": "topright",
                       "center": "midtop"}[align], pos)
        self.surface.blit(surf, rect)
        return rect

    def _dot(self, centre, colour, radius: int = 6, halo: bool = False,
             pulse: float | None = None):
        """pulse is a 0..1 brightness factor; only non-ok dots ever get one, so
        a calm panel stays completely still."""
        x, y = int(centre[0]), int(centre[1])
        if pulse is not None:
            colour = scale(colour, pulse)
        if halo:
            glow = pygame.Surface((radius * 6, radius * 6), pygame.SRCALPHA)
            pygame.draw.circle(glow, (*colour, 60), (radius * 3, radius * 3), radius * 3)
            self.surface.blit(glow, (x - radius * 3, y - radius * 3))
        pygame.draw.circle(self.surface, colour, (x, y), radius)

    # -- screens ----------------------------------------------------------

    def idle(self, state: State, now: float) -> None:
        t = self.theme
        self.surface.fill(t.bg)
        dx, dy = self.drift(now)
        cx, cy = self.width / 2 + dx, self.height / 2 + dy

        # The wordmark is the only loud element; it never encodes status.
        mark = t.fonts["wordmark"].render("TALOS", True, scale(t.bronze, self.breath(now)))
        mark_rect = mark.get_rect(center=(cx, cy - 26))
        self.surface.blit(mark, mark_rect)
        self._shimmer(mark, mark_rect, now)

        hosts = state.hosts
        if hosts:
            spacing = 34
            start = cx - (len(hosts) - 1) * spacing / 2
            for index, host in enumerate(hosts):
                host_state = host.get("state", "unknown")
                colour = t.status_colour(host_state)
                attention = host_state in ("down", "critical", "warning")
                self._dot(
                    (start + index * spacing, cy + 62),
                    colour,
                    radius=6,
                    halo=attention,
                    # Slow enough to read as breathing rather than flashing.
                    pulse=self.breath(now, period=2.2) if attention else None,
                )

        # Signature, small and quiet, below the dots and well away from them.
        # Its own slow rhythm rather than the wordmark's, so the two drift in
        # and out of phase instead of pulsing in lockstep.
        self._text("youruser", "small",
                   scale(t.teal, self.breath(now, period=5.5, low=0.38)),
                   (cx, cy + 92), "center")

    # A sweep of light across bronze, every SHIMMER_PERIOD seconds. Talos was
    # a bronze automaton, so this is the one ornament with a reason to exist.
    # Outside the window it costs nothing at all.
    SHIMMER_PERIOD = 26.0
    SHIMMER_DURATION = 1.4

    def _shimmer(self, mark: pygame.Surface, rect: pygame.Rect,
                 now: float) -> None:
        phase = now % self.SHIMMER_PERIOD
        if phase >= self.SHIMMER_DURATION:
            return
        progress = phase / self.SHIMMER_DURATION
        width, height = mark.get_size()
        half = max(10.0, width * 0.22)
        centre = -half + (width + 2 * half) * progress

        # Straight vertical columns, one per x. Slanted lines overlapped each
        # other on the alpha surface and the later line simply overwrote the
        # earlier one's alpha, which turned the gradient into a flat block.
        band = pygame.Surface((width, height), pygame.SRCALPHA)
        for x in range(max(0, int(centre - half)),
                       min(width, int(centre + half) + 1)):
            falloff = 1.0 - abs(x - centre) / half
            alpha = int(150 * falloff * falloff)
            if alpha > 0:
                pygame.draw.line(band, (255, 255, 255, alpha),
                                 (x, 0), (x, height))

        # font.render carries the glyph shape in the alpha channel only — the
        # RGB plane is the text colour everywhere, including between letters.
        # BLEND_RGBA_ADD ignores alpha, so adding this surface painted a solid
        # rectangle. Build a lit copy and let normal alpha blending mask it.
        highlight = mark.copy()
        highlight.fill((255, 228, 176), special_flags=pygame.BLEND_RGB_MAX)
        highlight.blit(band, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        self.surface.blit(highlight, rect)

    def dim(self, now: float) -> None:
        """Practically dark, but a dead host still looks different from a quiet one."""
        t = self.theme
        self.surface.fill(scale(t.bg, 0.4))
        dx, dy = self.drift(now) if self.dim_drifts else (0.0, 0.0)
        colour = scale(t.bronze, self.dim_brightness * self.breath(now, period=6.0))
        self._dot((self.width / 2 + dx, self.height / 2 + dy), colour, radius=5)

    def boot(self, now: float, elapsed: float, grace: float,
             checks: list[tuple[str, str, bool]]) -> None:
        """Shown until the first state document arrives.

        A cold boot starts this client before the collector, and treating that
        as a data outage produced a red wall on every reboot. This is a grace
        period, not a suppression: once `grace` runs out the normal stale
        handling takes over, and after the first successful poll this screen
        never returns.

        The accent walks from bronze toward amber as the grace expires, so a
        boot that is stuck looks wrong before it turns red — rather than
        staying friendly for ninety seconds and then alarming without warning.
        """
        t = self.theme
        self.surface.fill(t.bg)
        progress = min(elapsed / grace, 1.0) if grace > 0 else 0.0
        accent = lerp(t.bronze, t.warning, progress)
        dx, dy = self.drift(now)
        cx = self.width / 2 + dx * 0.3

        mark = t.fonts["wordmark"].render("TALOS", True, accent)
        self.surface.blit(mark, mark.get_rect(center=(cx, 168 + dy * 0.3)))

        # Same seven dots as IDLE, but chasing instead of reporting — the
        # transition into service is then just the light stopping.
        spacing, count = 34, 7
        lead = int(now * 4) % count
        start = cx - (count - 1) * spacing / 2
        for index in range(count):
            distance = (index - lead) % count
            self._dot((start + index * spacing, 236 + dy * 0.3),
                      scale(accent, max(0.18, 1.0 - distance * 0.22)), radius=6)

        reason = f"  \u00b7  {self._boot_note}" if self._boot_note else ""
        self._text(f"waiting for collector  \u00b7  {elapsed:.0f}s{reason}",
                   "small", t.muted, (cx, 274 + dy * 0.3), "center")

        # Diagnostics only once waiting stops being normal. Useful exactly
        # when something is wrong, invisible the rest of the time.
        if progress < 0.5:
            return
        fade = min((progress - 0.5) / 0.15, 1.0)
        y = 300
        for label, value, ok in checks:
            colour = t.ok if ok else accent
            self._dot((470, y + 8), scale(colour, fade), radius=4)
            self._text(label, "tiny", scale(t.muted, fade), (490, y))
            self._text(value, "small_mono", scale(colour, fade), (810, y - 2),
                       "right")
            y += 24

    def alert_band(self, state: State, now: float, stale: bool) -> None:
        """Red band drawn over whatever screen is showing.

        Once acknowledged the takeover steps aside, but the alert must stay
        visible on every screen — so this is an overlay rather than a mode.
        With several alerts firing it rotates, because a band that only ever
        names the first one hides the rest.
        """
        t = self.theme
        dx, dy = self.drift(now)
        band_h = 46
        top = self.height - band_h + dy * 0.3

        if stale:
            headline, detail = "NO DATA", f"last update {int(state.age(now))}s ago"
            colour = scale(t.critical, 0.6)
        else:
            alerts = state.alerts or [{}]
            index = int(now / 4.0) % len(alerts)   # rotate, ~4s per alert
            alert = alerts[index]
            headline = alert.get("name", "Critical")
            detail = alert.get("instance", "")
            if len(alerts) > 1:
                detail = f"{detail}   {index + 1}/{len(alerts)}"
            colour = t.critical if alert.get("severity") == "critical" else t.warning

        band = pygame.Surface((self.width, band_h))
        band.fill(colour)
        self.surface.blit(band, (0, top))

        white = (255, 255, 255)
        self._text(headline, "alert_body", white, (28 + dx, top + 12))
        if detail:
            self._text(detail, "small", scale(white, 0.85),
                       (self.width - 28 + dx, top + 14), "right")

    def alert(self, state: State, now: float, stale: bool) -> None:
        """Full bleed. Readable from the cellar door, and touch cannot dismiss it."""
        t = self.theme
        self.surface.fill(t.critical if not stale else scale(t.critical, 0.55))
        dx, dy = self.drift(now)

        if stale:
            headline = "No data"
            detail = "Renderer unreachable"
            age = state.age(now)
            meta = "never fetched" if age == float("inf") else f"last update {int(age)}s ago"
        else:
            alert = state.worst_alert() or {}
            headline = alert.get("name", "Critical")
            detail = alert.get("instance", "")
            meta = alert.get("summary", "")

        white = (255, 255, 255)
        self._text(headline, "wordmark", white, (self.width / 2 + dx, 108 + dy), "center")
        if detail:
            self._text(detail, "alert_title", scale(white, 0.92),
                       (self.width / 2 + dx, 226 + dy), "center")
        if meta:
            # Never below 0.85 here: light-on-red loses contrast fast and this
            # line has to stay legible from across the cellar.
            self._text(meta, "alert_body", scale(white, 0.85),
                       (self.width / 2 + dx, 274 + dy), "center")

    def dashboard(self, state: State, now: float, page: str) -> None:
        """Delegates to panels/. There is no fallback path any more: the client
        draws, so "renderer unreachable" cannot happen — only "no data", which
        the stale handling already covers."""
        self.surface.fill(self.theme.bg)
        draw = panels.get(page)
        if draw is None:
            self._text(f"unknown panel: {page}", "host", self.theme.critical,
                       (self.width / 2, 180), "center")
            return
        draw(self.canvas, state.data, now)


# --------------------------------------------------------------------------
# heartbeat
# --------------------------------------------------------------------------

class Heartbeat:
    """A crashed client looks exactly like a switched-off display. This is
    what makes the difference visible to Prometheus."""

    def __init__(self, path: str | None, interval: float):
        self.path = Path(path) if path else None
        self.interval = interval
        self._next = 0.0

    def tick(self, now: float, screen: str, stale: bool) -> None:
        if self.path is None or now < self._next:
            return
        self._next = now + self.interval
        body = (
            "# HELP rack_display_up Display client liveness.\n"
            "# TYPE rack_display_up gauge\n"
            "rack_display_up 1\n"
            "# HELP rack_display_state_stale Renderer data considered stale.\n"
            "# TYPE rack_display_state_stale gauge\n"
            f"rack_display_state_stale {int(stale)}\n"
            "# HELP rack_display_heartbeat_timestamp_seconds Last client tick.\n"
            "# TYPE rack_display_heartbeat_timestamp_seconds gauge\n"
            f"rack_display_heartbeat_timestamp_seconds {now:.0f}\n"
            f'rack_display_screen{{screen="{screen}"}} 1\n'
        )
        try:
            tmp = self.path.with_suffix(".prom.tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(self.path)  # atomic; the collector never sees a partial file
        except OSError as exc:
            LOG.warning("heartbeat write failed: %s", exc)


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------

class App:
    def __init__(self, config: Config):
        self.config = config
        self.running = True

        backend = os.environ.get("RACK_DISPLAY_BACKEND",
                                 config.get("display.backend", "kmsdrm"))
        if backend == "kmsdrm":
            os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")

        pygame.init()
        pygame.font.init()
        size = (int(config.get("display.width", 1280)),
                int(config.get("display.height", 400)))
        flags = pygame.FULLSCREEN if backend == "kmsdrm" else 0
        self.screen = pygame.display.set_mode(size, flags)
        pygame.display.set_caption("rack-display")
        pygame.mouse.set_visible(backend != "kmsdrm")

        self.theme = Theme(config)
        self.renderer = Renderer(self.screen, self.theme, config)
        self.renderer.canvas = Canvas(self.screen, load_fonts())
        self.clock = pygame.time.Clock()
        self.fps = int(config.get("display.fps", 20))

        self.source = StateSource(
            os.environ.get("RACK_DISPLAY_STATE",
                           config.get("source.state_url", "")),
            parse_duration(config.get("source.poll_interval"), 5.0),
        )
        self.stale_after = parse_duration(config.get("source.stale_after"), 60.0)
        # "Never had data" is not the same failure as "lost data". Only the
        # former gets a grace period, and only before the first poll succeeds.
        self.boot_grace = parse_duration(config.get("boot.grace"), 90.0)
        self.started_at = time.monotonic()
        # Design preview of BOOT, driven from the mirror. Never claims to be
        # the real thing: it is time-boxed, reported separately in X-Screen,
        # and any genuine alert or stale condition ends it immediately.
        self.preview_seconds = parse_duration(config.get("boot.preview"), 30.0)
        self.preview_started: float | None = None
        self.dashboard_timeout = parse_duration(config.get("timeouts.dashboard"), 60.0)
        self.idle_timeout = parse_duration(config.get("timeouts.idle"), 900.0)
        self.blank_mode = config.get("blank.mode", "dim")
        self.never_blank_when = set(config.get("blank.never_when") or [])

        self.mirror: Mirror | None = None
        if config.get("mirror.enabled", False):
            self.mirror = Mirror(
                port=int(config.get("mirror.port", 9120)),
                bind=config.get("mirror.bind", "0.0.0.0"),
                min_interval=parse_duration(config.get("mirror.min_interval"), 1.0),
                pages=list(config.get("pages") or ["overview"]),
            )
            self.mirror.start()

        self.heartbeat = Heartbeat(
            config.get("heartbeat.path"),
            parse_duration(config.get("heartbeat.interval"), 30.0),
        )

        now = time.monotonic()
        self.screen_state = IDLE
        self.entered_at = now
        self.forced: str | None = None
        self.pages: list[str] = list(config.get("pages") or ["overview"])
        self.page_index = 0
        # A full-bleed takeover is an event, not a permanent state: it grabs
        # attention once, then collapses to a band so the panel stays usable
        # and the static text stops sitting in one place.
        self.alert_takeover = parse_duration(config.get("alert.takeover"), 600.0)
        self.alert_acked = False
        # Acknowledging stores the names that were seen, not just the first
        # one. The collector sorts only by severity, so with two equally
        # critical alerts the leading entry can swap between polls — which
        # looked like a brand new alert and re-raised an acknowledged
        # takeover several times a second.
        self.alert_acked_names: set[str] = set()
        self.alert_firing_last: frozenset[str] = frozenset()
        # The digitizer exposes an event device AND a mouse emulation, so a
        # single tap produces two events. Without this, one tap skips a page.
        self.touch_debounce = parse_duration(config.get("input.debounce"), 0.35)
        self.last_touch = 0.0
        self.touch_start: tuple[tuple[int, int], float] | None = None
        self.swipe_px = int(config.get("input.swipe_px", 80))

        self.anim = bool(config.get("animation.enabled", True))
        self.slide_ms = parse_duration(config.get("animation.slide"), 0.22)
        self.fade_ms = parse_duration(config.get("animation.fade"), 0.18)
        self.ring_ms = parse_duration(config.get("animation.ring"), 0.25)
        self.pager_ms = parse_duration(config.get("animation.pager"), 1.5)
        self.scratch = self.screen.copy()
        self.prev_frame: pygame.Surface | None = None
        self.trans_kind: str | None = None
        self.trans_start = 0.0
        self.trans_dir = 1
        self.ring: tuple[tuple[int, int], float] | None = None
        self.pager_until = 0.0

        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        signal.signal(signal.SIGINT, lambda *_: self.stop())

    def stop(self) -> None:
        self.running = False

    # -- state machine ----------------------------------------------------

    def transition(self, target: str, now: float) -> None:
        if target != self.screen_state:
            LOG.info("%s -> %s", self.screen_state, target)
            self.screen_state = target
            self.entered_at = now

    def blank_permitted(self, state: State, stale: bool) -> bool:
        """The idle timeout must never hide a condition that needs attention."""
        if self.idle_timeout <= 0:
            return False
        if stale and "stale" in self.never_blank_when:
            return False
        if state.overall in self.never_blank_when:
            return False
        return True

    def resolve(self, now: float) -> str:
        state = self.source.state
        stale = state.is_stale(now, self.stale_after)

        # Cold boot: this client starts before the collector container is up.
        if (not state.ever_fetched
                and now - self.started_at < self.boot_grace):
            self.preview_started = None
            return BOOT

        if self.preview_started is not None:
            if (stale or state.has_critical()
                    or now - self.preview_started >= self.preview_seconds):
                self.preview_started = None   # reality wins, always
            else:
                return BOOT

        # ALERT outranks everything and cannot be dismissed by touch.
        if stale or state.has_critical():
            firing = frozenset({"stale"} if stale else {
                a.get("name") for a in state.alerts
                if a.get("severity") == "critical"})

            # Only a CHANGE in the set can start a takeover. Testing "is any
            # name still unacknowledged" instead deadlocked: the flag was
            # cleared on every frame, entered_at never aged, the timeout never
            # fired, and a touch was undone a frame later — the takeover could
            # not be dismissed at all.
            if firing != self.alert_firing_last:
                if firing - self.alert_acked_names:
                    self.alert_acked = False
                    self.entered_at = now
                self.alert_firing_last = firing

            if (not self.alert_acked and self.alert_takeover > 0
                    and now - self.entered_at >= self.alert_takeover):
                self.alert_acked = True
            if self.alert_acked:
                self.alert_acked_names |= firing
            else:
                return ALERT
            # Acknowledged: hand control back to the normal screens. IDLE is
            # the resting state, and alert_band rides on top of whatever shows.
            if self.screen_state == ALERT:
                return IDLE
        else:
            self.alert_acked = False
            self.alert_acked_names.clear()
            self.alert_firing_last = frozenset()

        if self.forced:
            return self.forced

        if self.screen_state in (ALERT, BOOT):
            return IDLE  # the condition cleared, or the boot window closed

        if self.screen_state == DASHBOARD:
            if now - self.entered_at >= self.dashboard_timeout:
                return IDLE
            return DASHBOARD

        if self.screen_state == IDLE:
            if (now - self.entered_at >= self.idle_timeout
                    and self.blank_permitted(state, stale)):
                return DIM
            return IDLE

        return self.screen_state  # DIM persists until touched

    def handle_events(self, now: float) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.stop()
            elif event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
                # The digitizer reports as both an event device and a mouse,
                # so one press arrives twice.
                if now - self.last_touch < self.touch_debounce:
                    continue
                self.last_touch = now
                self.touch_start = (self._event_pos(event), now)
                if self.anim:
                    self.ring = (self.touch_start[0], now)
            elif event.type in (pygame.MOUSEBUTTONUP, pygame.FINGERUP):
                if self.touch_start is None:
                    continue
                (start_x, _), _ = self.touch_start
                self.touch_start = None
                dx = self._event_pos(event)[0] - start_x
                if abs(dx) >= self.swipe_px:
                    # Swipe left means "forward", so the incoming page follows
                    # the finger in from the right.
                    self.swipe(-1 if dx > 0 else 1, now)
                else:
                    self.tap(now)
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_q:
                    self.stop()
                mapping = {pygame.K_1: IDLE, pygame.K_2: DASHBOARD,
                           pygame.K_3: ALERT, pygame.K_4: DIM}
                if event.key in mapping:
                    self.forced = mapping[event.key]
                    self.transition(self.forced, now)

    def _event_pos(self, event) -> tuple[int, int]:
        """FINGER events carry normalised coordinates, mouse events pixels."""
        if hasattr(event, "pos"):
            return event.pos
        return (int(getattr(event, "x", 0.5) * self.screen.get_width()),
                int(getattr(event, "y", 0.5) * self.screen.get_height()))

    def boot_checks(self) -> list[tuple[str, str, bool]]:
        """Only things the client can actually verify. No invented steps."""
        source = self.source
        connected = source.last_error is None
        return [
            (source.state_url.replace("http://", ""),
             "connected" if connected else "unreachable", connected),
            ("first state document",
             "received" if source.state.ever_fetched else "waiting",
             source.state.ever_fetched),
            (f"panel {self.screen.get_width()}x{self.screen.get_height()}",
             "ready", True),
            ("IBM Plex",
             "fallback face" if panels.base.FONT_FALLBACK else "loaded",
             not panels.base.FONT_FALLBACK),
        ]

    def begin_transition(self, kind: str, now: float, direction: int = 1) -> None:
        """Capture the outgoing frame. Alerts are never animated: a takeover
        that glides in arrives late and reads as decoration."""
        if not self.anim or self.screen_state == ALERT:
            return
        self.prev_frame = self.screen.copy()
        self.trans_kind = kind
        self.trans_start = now
        self.trans_dir = direction

    def trans_progress(self, now: float) -> float | None:
        if self.trans_kind is None:
            return None
        duration = self.slide_ms if self.trans_kind == "slide" else self.fade_ms
        elapsed = now - self.trans_start
        if elapsed >= duration:
            self.trans_kind = None
            self.prev_frame = None
            return None
        return elapsed / duration

    def compose_transition(self, progress: float) -> None:
        eased = 1 - (1 - progress) ** 3
        if self.trans_kind == "slide":
            offset = int(self.screen.get_width() * (1 - eased)) * self.trans_dir
            if self.prev_frame is not None:
                self.screen.blit(self.prev_frame,
                                 (offset - self.screen.get_width() * self.trans_dir, 0))
            self.screen.blit(self.scratch, (offset, 0))
        else:
            if self.prev_frame is not None:
                self.screen.blit(self.prev_frame, (0, 0))
            self.scratch.set_alpha(int(255 * eased))
            self.screen.blit(self.scratch, (0, 0))
            self.scratch.set_alpha(255)

    def go_to_page(self, index: int, now: float) -> None:
        direction = 1 if index > self.page_index or self.screen_state != DASHBOARD else -1
        if index == self.page_index and self.screen_state == DASHBOARD:
            return
        was_dashboard = self.screen_state == DASHBOARD
        self.page_index = index
        self.entered_at = now          # every page gets a full timeout
        self.pager_until = now + self.pager_ms
        if was_dashboard:
            self.begin_transition("slide", now, direction)
        else:
            self.begin_transition("fade", now)
        self.transition(DASHBOARD, now)
        LOG.info("page -> %s", self.pages[index])

    def tap(self, now: float) -> None:
        """One tap, wherever it came from — panel or browser."""
        if self.screen_state == BOOT:
            # A preview is dismissable; a real boot has nothing to page to.
            if self.preview_started is not None:
                self.preview_started = None
                self.transition(IDLE, now)
            return
        if self.screen_state == ALERT:
            # Acknowledge, never dismiss: the band stays until it clears.
            if not self.alert_acked:
                self.alert_acked = True
                LOG.info("alert acknowledged, collapsing to band")
                # resolve() folds the current names in on the next pass
            return
        self.forced = None
        if self.screen_state != DASHBOARD:
            self.go_to_page(0, now)
        else:
            # Wraps to the first page rather than dropping to IDLE; the
            # dashboard timeout is what returns the panel to rest.
            self.go_to_page((self.page_index + 1) % len(self.pages), now)

    def swipe(self, direction: int, now: float) -> None:
        if self.screen_state != DASHBOARD:
            self.tap(now)
            return
        self.go_to_page((self.page_index + direction) % len(self.pages), now)

    def drain_mirror(self, now: float) -> None:
        """Browser commands. Applied here, in the render loop, because the
        HTTP thread must not touch the state machine."""
        if self.mirror is None:
            return
        while (command := self.mirror.pop()) is not None:
            action = command.get("action")
            if action == "tap":
                self.tap(now)          # no debounce: a click arrives once
            elif action == "home":
                self.forced = None
                self.transition(IDLE, now)
            elif action == "preview":
                if command.get("screen") == "boot":
                    self.preview_started = now
                    self.transition(BOOT, now)
                    LOG.info("boot preview started (browser)")
            elif action == "page":
                page = command.get("page")
                if page in self.pages:
                    self.forced = None
                    if self.screen_state == ALERT and not self.alert_acked:
                        self.alert_acked = True
                    self.go_to_page(self.pages.index(page), now)

    def run(self) -> int:
        while self.running:
            now = time.monotonic()
            self.handle_events(now)
            self.drain_mirror(now)
            self.source.poll(now)

            state = self.source.state
            stale = state.is_stale(now, self.stale_after)
            self.transition(self.resolve(now), now)

            progress = self.trans_progress(now)
            # The incoming frame is rendered off-screen while the outgoing one
            # is still on the panel.
            self.renderer.retarget(self.scratch if progress is not None
                                   else self.screen)

            if self.screen_state == BOOT:
                preview = self.preview_started is not None
                if preview:
                    # Walk the whole grace window inside the preview so the
                    # bronze-to-amber shift is actually visible.
                    fraction = (now - self.preview_started) / self.preview_seconds
                    elapsed = fraction * self.boot_grace
                    self.renderer._boot_note = "preview"
                else:
                    elapsed = now - self.started_at
                    self.renderer._boot_note = self.source.last_error or ""
                self.renderer.boot(now, elapsed, self.boot_grace,
                                   self.boot_checks())
            elif self.screen_state == IDLE:
                self.renderer.idle(state, now)
            elif self.screen_state == DIM:
                self.renderer.dim(now)
            elif self.screen_state == ALERT:
                self.renderer.alert(state, now, stale)
            else:
                page = self.pages[min(self.page_index, len(self.pages) - 1)]
                self.renderer.dashboard(state, now, page)

            if progress is not None:
                self.compose_transition(progress)
            self.renderer.retarget(self.screen)

            # IDLE only. On a data panel the band cost too much room and
            # overlapped the content; the takeover already made sure the alert
            # was seen, and IDLE is where the panel rests.
            if self.screen_state == IDLE and (stale or state.alerts):
                self.renderer.alert_band(state, now, stale)

            # Overlays sit on top of the composition so they never slide.
            if now < self.pager_until and self.screen_state == DASHBOARD:
                self.renderer.pager(self.page_index, len(self.pages),
                                    now - (self.pager_until - self.pager_ms),
                                    self.pager_ms)
            if self.ring is not None:
                pos, started = self.ring
                if now - started >= self.ring_ms:
                    self.ring = None
                else:
                    self.renderer.touch_ring(pos, now - started, self.ring_ms)

            # Snapshot before the flip, and only when a browser is waiting.
            if (self.mirror is not None and self.trans_kind is None
                    and self.mirror.wants_frame(now)):
                label = self.screen_state
                if self.screen_state == BOOT and self.preview_started is not None:
                    label = "boot (preview)"   # the header must not lie
                self.mirror.capture(
                    self.screen, now, label,
                    self.pages[min(self.page_index, len(self.pages) - 1)])

            self.heartbeat.tick(now, self.screen_state, stale)
            pygame.display.flip()
            # Only DASHBOARD animates meaningfully. Dropping the resting
            # states to a few frames a second cuts the standing CPU cost to
            # roughly a third without anything looking different.
            busy = (self.trans_kind is not None or self.ring is not None
                    or now < self.pager_until)
            self.clock.tick(self.fps if busy else
                            {DIM: 4, IDLE: 8, ALERT: 12, BOOT: 20}.get(
                                self.screen_state, self.fps))

        if self.mirror is not None:
            self.mirror.stop()
        pygame.quit()
        return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("RACK_DISPLAY_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    return App(Config.load(CONFIG_PATH)).run()


if __name__ == "__main__":
    sys.exit(main())
