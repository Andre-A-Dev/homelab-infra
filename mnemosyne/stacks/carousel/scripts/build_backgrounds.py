#!/usr/bin/env python3
"""Compose full-page background images for a carousel template.

Takes the raw source art (background texture, logos) and bakes the
rounded content panel into three ready-to-use page backgrounds:
  - page_normal.png        (title + normal text slides)
  - page_trigger_title.png (trigger warning title slide)
  - page_trigger.png       (trigger text slides)

Compositing order (bottom to top): background art -> logos -> panel.
Logos sit BEHIND the semi-transparent panel, so they show through
muted rather than sitting on top of the text and hurting legibility.

Run once per template; commit the generated PNGs.

Usage: python3 build_backgrounds.py <template_dir>
"""

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

PAGE_W, PAGE_H = 1080, 1440


def cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale and center-crop an image to exactly w x h (CSS 'cover')."""
    scale = max(w / img.width, h / img.height)
    nw, nh = round(img.width * scale), round(img.height * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def rounded_panel(size, radius, color, alpha):
    """Return an RGBA layer containing one rounded rectangle."""
    layer = Image.new("RGBA", (PAGE_W, PAGE_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = size
    fill = tuple(int(color[i : i + 2], 16) for i in (1, 3, 5)) + (round(alpha * 255),)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=fill)
    return layer


def paste_logo(base, logo_path, box, opacity):
    logo = Image.open(logo_path).convert("RGBA")
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    scale = min(w / logo.width, h / logo.height)
    logo = logo.resize((round(logo.width * scale), round(logo.height * scale)), Image.LANCZOS)
    if opacity < 1:
        a = logo.getchannel("A").point(lambda v: round(v * opacity))
        logo.putalpha(a)
    base.alpha_composite(logo, (x0, y0))


def build(template_dir: Path):
    cfg = json.loads((template_dir / "template.json").read_text())
    assets = template_dir / "assets"
    panel = cfg["panel"]  # {x0, y0, x1, y1, radius}
    box = (panel["x0"], panel["y0"], panel["x1"], panel["y1"])

    variants = {
        "page_normal.png": (cfg["background"]["normal"], cfg["colors"]["panel_normal"]),
        "page_trigger_title.png": (cfg["background"]["trigger"], cfg["colors"]["panel_trigger_title"]),
        "page_trigger.png": (cfg["background"]["trigger"], cfg["colors"]["panel_trigger"]),
    }

    for out_name, (bg_name, panel_color) in variants.items():
        bg = Image.open(assets / bg_name).convert("RGBA")
        page = cover_crop(bg, PAGE_W, PAGE_H)
        # Logos first, so they sit behind the panel and show through muted
        # rather than on top of the text.
        for logo in cfg.get("logos", []):
            paste_logo(page, assets / logo["file"], tuple(logo["box"]), logo["opacity"])
        page.alpha_composite(rounded_panel(box, panel["radius"], panel_color, cfg["panel"]["alpha"]))
        page.convert("RGB").save(assets / out_name, optimize=True)
        print(f"built {out_name}")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
