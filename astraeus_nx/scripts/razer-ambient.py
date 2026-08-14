#!/usr/bin/env python3
"""
razer-ambient.py — Per-key screen ambient light for Razer peripherals.

Captures a region of the screen using spectacle (KDE/Wayland-native),
maps each keyboard column to a horizontal screen segment, and sets
individual key colors via openrazer's advanced matrix API.
"""

import subprocess
import time
import logging
import signal
import sys
from PIL import Image
import openrazer.client
from openrazer.client.devices.keyboard import RazerKeyboard

# --- Configuration ---
FPS = 155                 # Capture frequency (frames per second)
BRIGHTNESS = 1.0          # Global brightness multiplier (0.0 – 1.0)
SMOOTHING = 0.0           # Color smoothing (0.0 = instant, 1.0 = max lag)
CAPTURE_PATH = "/tmp/razer-ambient-cap.png"

# Screen region to sample (pixels)
REGION_X1 = 0             # Left edge
REGION_X2 = 2560          # Right edge (left monitor only)
REGION_Y_START = 0.75     # Top of region as fraction of screen height
REGION_Y_END = 1.0        # Bottom of region
# ---------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("razer-ambient")


def capture_screen() -> Image.Image:
    """Capture screen via spectacle and return cropped region as PIL Image."""
    result = subprocess.run(
        ["spectacle", "-b", "-n", "-o", CAPTURE_PATH],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"spectacle failed: {result.stderr.decode()}")

    img = Image.open(CAPTURE_PATH).convert("RGB")
    _, h = img.size
    return img.crop((
        REGION_X1,
        int(h * REGION_Y_START),
        REGION_X2,
        int(h * REGION_Y_END),
    ))


def region_avg(img: Image.Image, x1: int, x2: int) -> tuple[int, int, int]:
    """Return average RGB color of a vertical slice of the image."""
    w = max(1, x2 - x1)
    slice_ = img.crop((x1, 0, x2, img.height)).resize((max(1, w // 4), 4), Image.LANCZOS)
    pixels = [slice_.getpixel((x, y)) for x in range(slice_.width) for y in range(slice_.height)]
    return tuple(int(sum(c[i] for c in pixels) / len(pixels)) for i in range(3))


def smooth_matrix(
    current: list[list[tuple]],
    target: list[list[tuple]],
    factor: float,
) -> list[list[tuple[int, int, int]]]:
    """Lerp between two color matrices."""
    return [
        [
            tuple(int(current[r][c][i] + (target[r][c][i] - current[r][c][i]) * (1.0 - factor)) for i in range(3))
            for c in range(len(current[r]))
        ]
        for r in range(len(current))
    ]


def build_color_matrix(img: Image.Image, rows: int, cols: int) -> list[list[tuple[int, int, int]]]:
    """
    Map screen columns to keyboard matrix columns.
    All rows in a column get the same color (horizontal ambilight).
    """
    img_w = img.width
    matrix = []
    for _ in range(rows):
        row = []
        for col in range(cols):
            x1 = int(img_w * col / cols)
            x2 = int(img_w * (col + 1) / cols)
            r, g, b = region_avg(img, x1, x2)
            r = int(r * BRIGHTNESS)
            g = int(g * BRIGHTNESS)
            b = int(b * BRIGHTNESS)
            row.append((r, g, b))
        matrix.append(row)
    return matrix


def apply_matrix(keyboard, matrix: list[list[tuple[int, int, int]]]) -> None:
    """Write color matrix to keyboard via openrazer advanced fx."""
    for r, row in enumerate(matrix):
        for c, (red, green, blue) in enumerate(row):
            keyboard.fx.advanced.matrix[r, c] = (red, green, blue)
    keyboard.fx.advanced.draw()


def main() -> None:
    dm = openrazer.client.DeviceManager()
    devices = dm.devices

    if not devices:
        log.error("No openrazer devices found. Is openrazer-daemon running?")
        sys.exit(1)

    keyboard = next((d for d in devices if "Huntsman" in d.name), None)
    if keyboard is None:
        log.error("Huntsman keyboard not found.")
        sys.exit(1)

    rows = keyboard.fx.advanced.rows
    cols = keyboard.fx.advanced.cols
    log.info(f"Keyboard: {keyboard.name} ({rows}x{cols} matrix)")

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        log.info("Shutting down...")
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    interval = 1.0 / FPS
    current_matrix = [[(0, 0, 0)] * cols for _ in range(rows)]

    log.info(f"Starting per-key ambient loop — {FPS} FPS | smoothing={SMOOTHING}")

    while running:
        start = time.monotonic()

        try:
            img = capture_screen()
            target_matrix = build_color_matrix(img, rows, cols)
            current_matrix = smooth_matrix(current_matrix, target_matrix, SMOOTHING)
            apply_matrix(keyboard, current_matrix)

        except Exception as e:
            log.error(f"Loop error: {e}")

        elapsed = time.monotonic() - start
        time.sleep(max(0.0, interval - elapsed))

    # Turn off all keys on exit
    for r in range(rows):
        for c in range(cols):
            keyboard.fx.advanced.matrix[r, c] = (0, 0, 0)
    keyboard.fx.advanced.draw()
    log.info("Done — LEDs off.")


if __name__ == "__main__":
    main()