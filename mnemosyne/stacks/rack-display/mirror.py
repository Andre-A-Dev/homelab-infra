"""Browser mirror of the physical panel.

Serves what is actually on the display — including IDLE, DIM and the alert
takeover, which the old collector-side mirror could never show because it only
knew the dashboard content.

Snapshots are pull-driven. Encoding a 1280x400 PNG costs ~50-70ms on a Pi 5,
so capturing on a timer would burn CPU around the clock for a page nobody has
open. Instead the HTTP thread asks, the render loop delivers on its next
frame, and an unattended mirror costs nothing.

pygame surfaces are not thread-safe, which is why capture() must only ever be
called from the render loop.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pygame

LOG = logging.getLogger("rack-display.mirror")

INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Talos rack panel</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#0E1013; color:#868D96;
         font:14px/1.5 "IBM Plex Sans",system-ui,sans-serif;
         display:flex; flex-direction:column; align-items:center; gap:16px;
         padding:32px 16px; }
  img { width:min(1280px,100%); aspect-ratio:1280/400; display:block;
        border:1px solid #2A2E35; border-radius:6px; background:#14161A;
        cursor:pointer; }
  nav { display:flex; gap:8px; flex-wrap:wrap; justify-content:center; }
  button { background:#1C1F24; color:#E6E8EA; border:1px solid #2A2E35;
           border-radius:5px; padding:7px 15px; font:inherit; cursor:pointer; }
  button:hover { border-color:#4A5058; }
  button[aria-current="true"] { border-color:#C08A3E; color:#C08A3E; }
  button.preview { border-style:dashed; color:#868D96; }
  #meta { font-variant-numeric:tabular-nums; }
  #meta.stale { color:#E04F4F; }
  small { color:#4A5058; }
</style></head><body>
<img id="panel" alt="live view of the rack panel" title="click to advance">
<nav id="tabs"></nav>
<div id="meta">connecting\u2026</div>
<small>this drives the real panel \u2014 clicking here changes what the rack shows</small>
<script>
const img = document.getElementById("panel");
const meta = document.getElementById("meta");
const tabs = document.getElementById("tabs");
let inflight = false, current = "";

async function send(body) {
  await fetch("input", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  // Give the render loop a frame to act on it before asking for a picture.
  setTimeout(refresh, 250);
}

async function loadTabs() {
  const pages = await (await fetch("pages")).json();
  const home = document.createElement("button");
  home.textContent = "idle";
  home.onclick = () => send({ action: "home" });
  // Dashed: this one is a design preview, not a state the system is in.
  const boot = document.createElement("button");
  boot.textContent = "boot";
  boot.className = "preview";
  boot.title = "preview the splash screen — ends on any real alert";
  boot.onclick = () => send({ action: "preview", screen: "boot" });
  const buttons = pages.map(p => {
    const b = document.createElement("button");
    b.textContent = p;
    b.dataset.page = p;
    b.onclick = () => send({ action: "page", page: p });
    return b;
  });
  tabs.replaceChildren(home, ...buttons, boot);
}

function markCurrent(page) {
  if (page === current) return;
  current = page;
  for (const b of tabs.querySelectorAll("button[data-page]")) {
    b.setAttribute("aria-current", String(b.dataset.page === page));
  }
}

async function refresh() {
  // One request at a time: frames come from the render loop, so queueing
  // more of them only makes the panel do redundant work.
  if (inflight || document.hidden) return;
  inflight = true;
  try {
    const res = await fetch(`panel.png?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    const blob = await res.blob();
    const old = img.src;
    img.src = URL.createObjectURL(blob);
    if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    const screen = res.headers.get("X-Screen") || "?";
    markCurrent(screen === "dashboard" ? (res.headers.get("X-Page") || "") : "");
    meta.textContent = `${screen} \u00b7 ${new Date().toLocaleTimeString()}`;
    meta.className = "";
  } catch {
    meta.textContent = "panel unreachable";
    meta.className = "stale";
  } finally {
    inflight = false;
  }
}

img.onclick = () => send({ action: "tap" });
document.addEventListener("keydown", e => {
  if (e.key === "r") refresh();
  if (e.key === " ") { e.preventDefault(); send({ action: "tap" }); }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
loadTabs();
refresh();
setInterval(refresh, 3000);
</script></body></html>
"""


class Mirror:
    def __init__(self, port: int, bind: str = "0.0.0.0",
                 min_interval: float = 1.0, wait: float = 2.5,
                 pages: list[str] | None = None):
        self.port = port
        self.bind = bind
        self.pages = list(pages or [])
        # Commands are queued, never applied here: the render loop owns the
        # state machine, and the HTTP thread must not touch it.
        self.commands: queue.SimpleQueue = queue.SimpleQueue()
        self.min_interval = min_interval   # floor between two encodes
        self.wait = wait                   # how long a request waits for a frame
        self._lock = threading.Lock()
        self._png: bytes | None = None
        self._screen = "?"
        self._page = "?"
        self._captured_at = 0.0
        self._wanted = threading.Event()
        self._fresh = threading.Condition()
        self._server: ThreadingHTTPServer | None = None

    # -- render-loop side (single threaded, owns the surface) -------------

    def wants_frame(self, now: float) -> bool:
        return (self._wanted.is_set()
                and now - self._captured_at >= self.min_interval)

    def pop(self):
        """Next queued browser command, or None. Render-loop side."""
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None

    def capture(self, surface: pygame.Surface, now: float, screen: str,
                page: str = "?") -> None:
        """Call from the render loop only."""
        self._wanted.clear()
        try:
            buffer = io.BytesIO()
            pygame.image.save(surface, buffer, "panel.png")
            png = buffer.getvalue()
        except Exception as exc:
            LOG.warning("snapshot failed: %s", exc)
            return
        with self._lock:
            self._png, self._screen, self._page = png, screen, page
            self._captured_at = now
        with self._fresh:
            self._fresh.notify_all()

    # -- http side --------------------------------------------------------

    def _request_frame(self) -> tuple[bytes | None, str, str]:
        self._wanted.set()
        with self._fresh:
            self._fresh.wait(timeout=self.wait)
        with self._lock:
            # On timeout the last frame is served rather than an error: a
            # slightly old picture beats a broken image in the browser.
            return self._png, self._screen, self._page

    def start(self) -> None:
        mirror = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def _send(self, body: bytes, content_type: str, status: int = 200,
                      extra: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                path = urllib.parse.urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._send(INDEX_HTML.encode(), "text/html; charset=utf-8")
                elif path == "/healthz":
                    self._send(b"ok", "text/plain")
                elif path == "/pages":
                    self._send(json.dumps(mirror.pages).encode(),
                               "application/json")
                elif path == "/panel.png":
                    png, screen, page = mirror._request_frame()
                    if png is None:
                        self._send(b"no frame yet", "text/plain", 503)
                    else:
                        self._send(png, "image/png",
                                   extra={"X-Screen": screen, "X-Page": page})
                else:
                    self._send(b"not found", "text/plain", 404)

            def do_HEAD(self):  # noqa: N802
                # Caddy and health checks probe with HEAD; without this they
                # get a 501 and treat the upstream as down.
                self._send(b"", "text/plain")

            def do_POST(self):  # noqa: N802
                if urllib.parse.urlparse(self.path).path != "/input":
                    self._send(b"not found", "text/plain", 404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    command = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._send(b"bad request", "text/plain", 400)
                    return
                if command.get("action") not in ("tap", "page", "home", "preview"):
                    self._send(b"unknown action", "text/plain", 400)
                    return
                mirror.commands.put(command)
                self._send(b"ok", "text/plain")

        try:
            self._server = ThreadingHTTPServer((self.bind, self.port), Handler)
        except OSError as exc:
            LOG.error("mirror could not bind %s:%d: %s", self.bind, self.port, exc)
            return
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        LOG.info("mirror on http://%s:%d", self.bind, self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
