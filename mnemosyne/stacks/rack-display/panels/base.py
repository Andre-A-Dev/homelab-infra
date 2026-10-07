"""Drawing vocabulary shared by every panel.

The client draws each frame itself, so anything here can animate. Panels never
touch pygame directly — they go through Canvas, which means a change to bar
styling or dot behaviour lands on every panel at once.
"""

from __future__ import annotations

import logging
import math

import pygame

LOG = logging.getLogger("rack-display.panels")

WIDTH, HEIGHT = 1280, 400


class Theme:
    """Talos was a bronze automaton: warm metal on cool graphite. Bronze
    belongs to the wordmark and to progress; status is carried by dots and
    bars alone."""

    bg = (0x14, 0x16, 0x1A)
    surface = (0x1C, 0x1F, 0x24)
    hairline = (0x2A, 0x2E, 0x35)
    text = (0xE6, 0xE8, 0xEA)
    muted = (0x86, 0x8D, 0x96)
    faint = (0x4A, 0x50, 0x58)
    bronze = (0xC0, 0x8A, 0x3E)
    ok = (0x4E, 0x9A, 0x6A)
    warning = (0xE0, 0xA3, 0x2E)
    critical = (0xE0, 0x4F, 0x4F)


T = Theme


def scale(colour, factor: float):
    return tuple(max(0, min(255, int(c * factor))) for c in colour)


def status_colour(state: str):
    return {
        "up": T.ok, "ok": T.ok, "healthy": T.ok, "printing": T.ok,
        "warning": T.warning, "unhealthy": T.warning,
        "down": T.critical, "critical": T.critical,
        "off": T.faint, "idle": T.muted,
    }.get(state, T.muted)


def breath(now: float, period: float = 2.2, low: float = 0.55) -> float:
    """0..1 brightness factor. Slow enough to read as breathing, never as
    flashing — a panel that blinks at you is a panel you stop looking at."""
    return low + (1 - low) * (0.5 + 0.5 * math.sin(2 * math.pi * now / period))


def ease(current: float, target: float, dt: float, tau: float = 0.25) -> float:
    """Exponential approach. Values arrive in 15s steps but should not jump;
    panels keep their own eased copy and call this every frame."""
    if tau <= 0:
        return target
    return current + (target - current) * (1 - math.exp(-dt / tau))


# Set when any face fell back. The boot diagnostics report it, because a
# panel in the wrong typeface looks broken and the cause is invisible.
FONT_FALLBACK = False


def load_fonts() -> dict:
    def pick(families, size, bold=False):
        global FONT_FALLBACK
        for family in families:
            path = pygame.font.match_font(family, bold=bold)
            if path:
                return pygame.font.Font(path, size)
        LOG.warning("none of %s installed, using default face", families)
        FONT_FALLBACK = True
        return pygame.font.Font(None, size)

    sans = ["ibmplexsans", "inter", "dejavusans"]
    mono = ["ibmplexmono", "dejavusansmono"]
    return {
        "wordmark": pick(sans, 96, bold=True),
        "huge": pick(sans, 54, bold=True),
        "big_number": pick(sans, 44, bold=True),
        "heading": pick(sans, 32, bold=True),
        "host": pick(sans, 24),
        "metric": pick(mono, 22),
        "small": pick(sans, 18),
        "small_mono": pick(mono, 18),
        "tiny": pick(sans, 15),
    }


RING_SUPERSAMPLE = 4


def _render_ring(radius: int, thickness: int, colour,
                 value: float) -> pygame.Surface:
    scale_factor = RING_SUPERSAMPLE
    pad = 2
    extent = int(radius + thickness / 2 + pad)
    size = extent * 2 * scale_factor
    big = pygame.Surface((size, size), pygame.SRCALPHA)
    centre = size // 2
    big_radius = radius * scale_factor
    big_thickness = max(1, int(thickness * scale_factor))

    pygame.draw.circle(big, T.hairline, (centre, centre), big_radius,
                       big_thickness)

    if value > 0:
        start = -math.pi / 2                  # twelve o'clock
        sweep = 2 * math.pi * value
        steps = max(8, int(240 * value))
        outer = big_radius + big_thickness / 2
        inner = big_radius - big_thickness / 2
        outer_points, inner_points = [], []
        for i in range(steps + 1):
            angle = start + sweep * i / steps
            ca, sa = math.cos(angle), math.sin(angle)
            outer_points.append((centre + outer * ca, centre + outer * sa))
            inner_points.append((centre + inner * ca, centre + inner * sa))
        pygame.draw.polygon(big, colour, outer_points + inner_points[::-1])
        cap = max(1, int(big_thickness / 2))
        for angle in (start, start + sweep):
            pygame.draw.circle(
                big, colour,
                (int(centre + big_radius * math.cos(angle)),
                 int(centre + big_radius * math.sin(angle))), cap)

    return pygame.transform.smoothscale(big, (extent * 2, extent * 2))


class Canvas:
    """Thin wrapper over the framebuffer surface."""

    def __init__(self, surface: pygame.Surface, fonts: dict):
        self.s = surface
        self.f = fonts
        self.width, self.height = surface.get_size()
        self._ring_cache: dict = {}

    # -- primitives -----------------------------------------------------

    def text(self, value, font, colour, xy, align="left"):
        surf = self.f[font].render(str(value), True, colour)
        rect = surf.get_rect()
        setattr(rect, {"left": "topleft", "right": "topright",
                       "center": "midtop"}[align], xy)
        self.s.blit(surf, rect)
        return rect

    def label(self, value, xy):
        """Small, muted, sentence case. Never an all-caps eyebrow."""
        return self.text(value, "tiny", T.faint, xy)

    def dot(self, xy, colour, radius: int = 6, halo: bool = False,
            pulse: float | None = None):
        if pulse is not None:
            colour = scale(colour, pulse)
        x, y = int(xy[0]), int(xy[1])
        if halo:
            glow = pygame.Surface((radius * 6, radius * 6), pygame.SRCALPHA)
            pygame.draw.circle(glow, (*colour, 70), (radius * 3, radius * 3),
                               radius * 3)
            self.s.blit(glow, (x - radius * 3, y - radius * 3))
        pygame.draw.circle(self.s, colour, (x, y), radius)

    def bar(self, xy, width: int, value: float, colour, height: int = 6,
            track=None):
        """Value 0..1. A shape is read across the room; the number beside it
        is for when you actually want to know."""
        x, y = int(xy[0]), int(xy[1])
        pygame.draw.rect(self.s, track or T.hairline, (x, y, width, height),
                         border_radius=height // 2)
        filled = max(2, int(width * max(0.0, min(1.0, value))))
        pygame.draw.rect(self.s, colour, (x, y, filled, height),
                         border_radius=height // 2)

    def ring(self, xy, radius: int, value: float, colour, thickness: int = 9):
        """Progress ring with antialiased edges.

        pygame has no antialiased thick-arc primitive: draw.lines mitres its
        joints and leaves notches on a curve, draw.arc drops pixels at width>1,
        and a plain polygon has hard stair-stepped edges against the dark
        background. So the whole ring — track included — is rendered at four
        times the size and scaled down.

        The result is cached on the rounded value, because the underlying
        number only changes when the collector refreshes while this redraws
        every frame.
        """
        value = max(0.0, min(1.0, value))
        key = (radius, thickness, tuple(colour), round(value, 3))
        surface = self._ring_cache.get(key)
        if surface is None:
            surface = _render_ring(radius, thickness, colour, value)
            if len(self._ring_cache) > 48:
                self._ring_cache.clear()
            self._ring_cache[key] = surface
        self.s.blit(surface, (int(xy[0]) - surface.get_width() // 2,
                              int(xy[1]) - surface.get_height() // 2))

    def sparkline(self, xy, size, values, colour, fill: bool = True):
        x, y = xy
        w, h = size
        if len(values) < 2:
            return
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1.0
        points = [(x + w * i / (len(values) - 1),
                   y + h - h * (v - lo) / span) for i, v in enumerate(values)]
        if fill:
            poly = pygame.Surface((w, h), pygame.SRCALPHA)
            pygame.draw.polygon(
                poly, (*colour, 40),
                [(px - x, py - y) for px, py in points] + [(w, h), (0, h)])
            self.s.blit(poly, (x, y))
        pygame.draw.lines(self.s, colour, False, points, 2)

    def divider(self, x: int, top: int = 32, bottom: int = 368):
        pygame.draw.line(self.s, T.hairline, (x, top), (x, bottom))

    def rule(self, y: int, left: int = 40, right: int = 1240):
        pygame.draw.line(self.s, T.hairline, (left, y), (right, y))

    def empty(self, message: str, xy):
        """An empty region is a signal, not wasted space — say so plainly."""
        return self.text(message, "host", T.muted, xy)
