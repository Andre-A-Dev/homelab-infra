#!/usr/bin/env python3
"""
Hephaestus OLED health status display with rotating pages.

Four-page layout on a 128x64 SSD1306 I2C OLED (yellow/blue split):
  Header (yellow, always): gateway indicator + hostname + time
  Page 0 - NET:  LAN IP, Tailscale IP, WLAN signal
  Page 1 - SVC:  vcontrold, viessmann-api, node-exporter status
  Page 2 - SYS:  CPU temp, uptime, disk, RAM
  Page 3 - HTG:  outdoor/boiler/hot water temps, burner status

Each page shows for PAGE_DURATION seconds, then rotates.
Page indicator dots at the bottom right show current position.
"""

import os
import socket
import subprocess
import time
from datetime import datetime

import board
import busio
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

# --- Configuration ---------------------------------------------------------

DISPLAY_WIDTH = 128
DISPLAY_HEIGHT = 64
I2C_ADDRESS = 0x3C
PAGE_DURATION = 10  # seconds per page
NUM_PAGES = 4

# Icon dimensions for service status symbols
ICON_SIZE = 7
ICON_MARGIN = 3

# vclient connection (vcontrold on localhost)
VCLIENT_HOST = "127.0.0.1"
VCLIENT_PORT = 3002
VCLIENT_COMMANDS = "getTempA,getTempKist,getTempWWist,getBrennerStatus"
VCLIENT_CACHE_TTL = 60  # avoid conflicts with exporter cron

# Services to monitor
SYSTEMD_SERVICES = ["vcontrold", "viessmann-api"]
DOCKER_SERVICES = ["node-exporter"]

# --- Display setup ----------------------------------------------------------

i2c = busio.I2C(board.SCL, board.SDA)
oled = adafruit_ssd1306.SSD1306_I2C(DISPLAY_WIDTH, DISPLAY_HEIGHT, i2c, addr=I2C_ADDRESS)

try:
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10
    )
except OSError:
    font = ImageFont.load_default()

image = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT))
draw = ImageDraw.Draw(image)

# --- Caches -----------------------------------------------------------------

_heating_cache: dict = {}
_heating_cache_ts: float = 0
_gateway_cache: bool = False
_gateway_cache_ts: float = 0
_GATEWAY_CACHE_TTL = 30


# --- Drawing helpers --------------------------------------------------------

def draw_ok_icon(x: int, y: int) -> None:
    """Filled circle — service healthy."""
    draw.ellipse([x, y, x + ICON_SIZE, y + ICON_SIZE], fill=255)


def draw_fail_icon(x: int, y: int) -> None:
    """X mark — service down."""
    draw.line([x, y, x + ICON_SIZE, y + ICON_SIZE], fill=255, width=2)
    draw.line([x, y + ICON_SIZE, x + ICON_SIZE, y], fill=255, width=2)


def draw_page_dots(active: int) -> None:
    """Four small dots at bottom-right showing current page."""
    dot_r = 2
    gap = 4
    total_w = NUM_PAGES * (dot_r * 2) + (NUM_PAGES - 1) * gap
    start_x = DISPLAY_WIDTH - total_w - 2  # 2px right margin
    cy = DISPLAY_HEIGHT - dot_r - 1

    for i in range(NUM_PAGES):
        cx = start_x + i * (dot_r * 2 + gap) + dot_r
        x0, y0 = cx - dot_r, cy - dot_r
        x1, y1 = cx + dot_r, cy + dot_r
        if i == active:
            draw.ellipse([x0, y0, x1, y1], fill=255)
        else:
            draw.ellipse([x0, y0, x1, y1], outline=255)


def draw_service_line(name: str, is_ok: bool, y: int) -> None:
    """Icon + label for a single service."""
    if is_ok:
        draw_ok_icon(0, y + 1)
    else:
        draw_fail_icon(0, y + 1)
    draw.text((ICON_SIZE + ICON_MARGIN, y), name, font=font, fill=255)


# --- Data collection --------------------------------------------------------

def is_gateway_reachable() -> bool:
    """Ping the default gateway (cached for 30s)."""
    global _gateway_cache, _gateway_cache_ts
    now = time.time()
    if now - _gateway_cache_ts < _GATEWAY_CACHE_TTL:
        return _gateway_cache
    try:
        route = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=2,
        )
        gw = route.stdout.split()[2] if route.returncode == 0 else None
        if not gw:
            _gateway_cache = False
        else:
            ping = subprocess.run(
                ["ping", "-c", "1", "-W", "1", gw],
                capture_output=True, timeout=3,
            )
            _gateway_cache = ping.returncode == 0
    except Exception:
        _gateway_cache = False
    _gateway_cache_ts = now
    return _gateway_cache


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "-"


def get_tailscale_ip() -> str:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
        )
        ip = result.stdout.strip()
        return ip if result.returncode == 0 and ip else "down"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "down"


def get_wlan_signal() -> str:
    """Read WLAN signal level from /proc/net/wireless."""
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()
            if len(lines) >= 3:
                parts = lines[2].split()
                level = parts[3].rstrip(".")
                return f"{level}dBm"
    except (FileNotFoundError, IndexError):
        pass
    return "-"


def get_systemd_status(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit], timeout=3
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def get_docker_status(container: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_cpu_temp() -> str:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return f"{int(f.read().strip()) / 1000:.1f}°C"
    except (FileNotFoundError, ValueError):
        return "-"


def get_uptime() -> str:
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
            d = int(secs // 86400)
            h = int((secs % 86400) // 3600)
            m = int((secs % 3600) // 60)
            return f"{d}d {h}h {m}m" if d > 0 else f"{h}h {m}m"
    except (FileNotFoundError, ValueError):
        return "-"


def get_disk_usage() -> str:
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
        pct = int((1 - free / total) * 100)
        total_gb = total / (1024 ** 3)
        return f"{pct}% /{total_gb:.0f}G"
    except OSError:
        return "-"


def get_ram_usage() -> str:
    try:
        with open("/proc/meminfo") as f:
            lines = f.readlines()
            total = int(lines[0].split()[1]) // 1024
            avail = int(lines[2].split()[1]) // 1024
            used = total - avail
            return f"{used}/{total}MB"
    except (FileNotFoundError, ValueError, IndexError):
        return "-"


def get_heating_data() -> dict:
    """Query vclient for heating values (cached for 60s)."""
    global _heating_cache, _heating_cache_ts
    now = time.time()
    if now - _heating_cache_ts < VCLIENT_CACHE_TTL and _heating_cache:
        return _heating_cache
    try:
        result = subprocess.run(
            ["vclient", "-h", f"{VCLIENT_HOST}:{VCLIENT_PORT}",
             "-c", VCLIENT_COMMANDS],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = {}
            for line in result.stdout.strip().split("\n"):
                if ":" in line:
                    key, val = line.split(":", 1)
                    data[key.strip()] = val.strip()
            _heating_cache = data
            _heating_cache_ts = now
            return data
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return _heating_cache or {}


def parse_temp(raw: str) -> str:
    """Extract numeric value from vclient output like '12.300000 Grad Celsius'."""
    try:
        return f"{float(raw.split()[0]):.1f}°C"
    except (ValueError, IndexError, AttributeError):
        return "-"


# --- Page renderers ---------------------------------------------------------

def render_header() -> None:
    """Yellow zone (rows 0-15): gateway dot + hostname + clock."""
    draw.rectangle((0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT), outline=0, fill=0)

    # Gateway indicator: filled = reachable, outline = down
    r = 3
    if is_gateway_reachable():
        draw.ellipse([0, 3, r * 2, 3 + r * 2], fill=255)
    else:
        draw.ellipse([0, 3, r * 2, 3 + r * 2], outline=255)

    # Hostname
    draw.text((r * 2 + 3, 3), socket.gethostname(), font=font, fill=255)

    # Clock, right-aligned
    now = datetime.now().strftime("%H:%M")
    bbox = draw.textbbox((0, 0), now, font=font)
    draw.text((DISPLAY_WIDTH - (bbox[2] - bbox[0]), 3), now, font=font, fill=255)


def page_net(idx: int) -> None:
    render_header()
    draw.text((0, 17), f"LAN  {get_lan_ip()}", font=font, fill=255)
    draw.text((0, 27), f"TS   {get_tailscale_ip()}", font=font, fill=255)
    draw.text((0, 37), f"WLAN {get_wlan_signal()}", font=font, fill=255)
    draw_page_dots(idx)
    oled.image(image)
    oled.show()


def page_svc(idx: int) -> None:
    render_header()
    y = 17
    for unit in SYSTEMD_SERVICES:
        draw_service_line(unit, get_systemd_status(unit), y)
        y += 10
    for container in DOCKER_SERVICES:
        draw_service_line(container, get_docker_status(container), y)
        y += 10
    draw_page_dots(idx)
    oled.image(image)
    oled.show()


def page_sys(idx: int) -> None:
    render_header()
    draw.text((0, 17), f"CPU  {get_cpu_temp()}", font=font, fill=255)
    draw.text((0, 27), f"Up   {get_uptime()}", font=font, fill=255)
    draw.text((0, 37), f"Disk {get_disk_usage()}", font=font, fill=255)
    draw.text((0, 47), f"RAM  {get_ram_usage()}", font=font, fill=255)
    draw_page_dots(idx)
    oled.image(image)
    oled.show()


def page_htg(idx: int) -> None:
    render_header()
    data = get_heating_data()

    outside = parse_temp(data.get("getTempA", ""))
    boiler = parse_temp(data.get("getTempKist", ""))
    hw = parse_temp(data.get("getTempWWist", ""))
    burner_raw = data.get("getBrennerStatus", "")
    burner = "ON" if "1" in burner_raw else ("OFF" if burner_raw else "-")

    draw.text((0, 17), f"Outside {outside:>8s}", font=font, fill=255)
    draw.text((0, 27), f"Boiler  {boiler:>8s}", font=font, fill=255)
    draw.text((0, 37), f"HW      {hw:>8s}", font=font, fill=255)
    draw.text((0, 47), f"Burner  {burner:>8s}", font=font, fill=255)
    draw_page_dots(idx)
    oled.image(image)
    oled.show()


PAGES = [page_net, page_svc, page_sys, page_htg]


# --- Main loop --------------------------------------------------------------

def main():
    page = 0
    while True:
        try:
            PAGES[page](page)
        except Exception as exc:  # noqa: BLE001
            print(f"Page {page} render failed: {exc}")
        time.sleep(PAGE_DURATION)
        page = (page + 1) % NUM_PAGES


if __name__ == "__main__":
    main()
