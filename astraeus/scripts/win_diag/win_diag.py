#!/usr/bin/env python3
"""
Windows System Diagnostics & Fix Script
Collects system information, runs diagnostics, applies smart fixes,
and generates a self-contained HTML report.
Requires Administrator privileges for full results and fix execution.
"""

import argparse
import logging
import subprocess
import platform
import os
import sys
import json
import socket
import datetime
import re
import shutil
import threading
import itertools
import time
from pathlib import Path

# tomllib is in stdlib from Python 3.11+; fall back to tomli (pip install tomli) for 3.10
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        tomllib = None  # type: ignore  — config file support disabled


# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────

# Valid fix keys — used for skip/only validation
ALL_FIX_KEYS = {
    "sfc", "dism_check", "dism_restore", "dns_flush",
    "winsock_reset", "services", "clear_temp", "disk_cleanup", "windows_update",
}

# Profile definitions: maps profile name → set of fix keys that are ENABLED
PROFILES: dict[str, set[str]] = {
    "readonly": set(),   # Diagnostics only, no fixes at all
    "safe": {            # Default — everything except destructive network reset
        "sfc", "dism_check", "dns_flush", "services",
        "clear_temp", "disk_cleanup", "windows_update",
    },
    "full": ALL_FIX_KEYS.copy(),  # Everything including winsock_reset + dism_restore
}


class DiagConfig:
    """
    Holds the merged configuration from win_diag.toml (if present) and
    CLI argument overrides. CLI always wins over TOML.

    Attributes
    ----------
    profile         : active profile name
    enabled_fixes   : set of fix keys that may run
    skip_fixes      : fixes explicitly disabled (applied on top of profile)
    benign_sources  : event sources excluded from health scoring
    benign_event_ids: event IDs excluded from health scoring
    log_path        : resolved log file path (or None = auto)
    report_path     : resolved report file path (or None = disabled)
    extra_services  : additional service names to monitor
    ignore_services : service names to skip even if stopped
    config_path     : path of the loaded TOML file (or None)
    """

    def __init__(self) -> None:
        self.profile          = "safe"
        self.enabled_fixes    = PROFILES["safe"].copy()
        self.skip_fixes:  set[str] = set()
        self.benign_sources:  set[str] = {
            "BTHUSB",
            "Microsoft-Windows-DeviceAssociationService",
        }
        self.benign_event_ids: set[int] = {7009}
        self.log_path:    Path | None = None
        self.report_path: Path | None = None
        self.extra_services:  list[str] = []
        self.ignore_services: list[str] = []
        self.config_path: Path | None = None

    def fix_enabled(self, key: str) -> bool:
        """Return True if this fix key should run under the current config."""
        return key in self.enabled_fixes and key not in self.skip_fixes

    def summary(self) -> str:
        """One-line summary for terminal/log output."""
        skipped = self.skip_fixes or {"(none)"}
        return (
            f"profile={self.profile}  "
            f"fixes={len(self.enabled_fixes - self.skip_fixes)}/{len(ALL_FIX_KEYS)}  "
            f"skip={','.join(sorted(skipped))}"
        )


def load_config(
    toml_path: Path | None,
    cli_profile:  str | None,
    cli_log:      str | None,
    cli_report:   str | None,
    cli_skip:     list[str] | None,
    cli_only:     list[str] | None,
) -> DiagConfig:
    """
    Build a DiagConfig by merging TOML file → CLI overrides.
    Priority (highest to lowest): CLI flags > TOML values > built-in defaults.
    """
    cfg = DiagConfig()

    # ── 1. Load TOML ────────────────────────────
    toml: dict = {}
    if toml_path and toml_path.exists():
        if tomllib is None:
            print(
                f"  \033[33m!\033[0m  Config file found but tomllib is unavailable "
                f"(Python < 3.11 and tomli not installed). Ignoring {toml_path}."
            )
        else:
            try:
                with open(toml_path, "rb") as f:
                    toml = tomllib.load(f)
                cfg.config_path = toml_path
            except Exception as e:
                print(f"  \033[33m!\033[0m  Could not parse {toml_path}: {e}. Using defaults.")

    # ── 2. Apply TOML values ─────────────────────
    toml_profile = toml.get("profile", {}).get("default", "safe")
    if toml_profile in PROFILES:
        cfg.profile       = toml_profile
        cfg.enabled_fixes = PROFILES[toml_profile].copy()

    if "log" in toml and "path" in toml["log"]:
        cfg.log_path = Path(toml["log"]["path"])

    if "report" in toml and "path" in toml["report"]:
        cfg.report_path = Path(toml["report"]["path"])

    scoring = toml.get("scoring", {})
    if "benign_sources" in scoring:
        cfg.benign_sources = set(scoring["benign_sources"])
    if "benign_event_ids" in scoring:
        cfg.benign_event_ids = set(int(i) for i in scoring["benign_event_ids"])

    fixes_toml = toml.get("fixes", {})
    if "skip" in fixes_toml:
        cfg.skip_fixes = set(fixes_toml["skip"]) & ALL_FIX_KEYS

    services_toml = toml.get("services", {})
    cfg.extra_services  = services_toml.get("extra", [])
    cfg.ignore_services = services_toml.get("ignore", [])

    # ── 3. Apply CLI overrides (always win) ──────
    if cli_profile:
        if cli_profile in PROFILES:
            cfg.profile       = cli_profile
            cfg.enabled_fixes = PROFILES[cli_profile].copy()
        else:
            print(f"  \033[33m!\033[0m  Unknown profile '{cli_profile}'. Valid: {', '.join(PROFILES)}. Using '{cfg.profile}'.")

    if cli_log:
        cfg.log_path = Path(cli_log)
    if cli_report:
        cfg.report_path = Path(cli_report)

    # --only overrides the profile entirely
    if cli_only:
        valid = set(cli_only) & ALL_FIX_KEYS
        invalid = set(cli_only) - ALL_FIX_KEYS
        if invalid:
            print(f"  \033[33m!\033[0m  Unknown fix keys ignored: {', '.join(sorted(invalid))}")
        cfg.enabled_fixes = valid
        cfg.skip_fixes    = set()

    # --skip adds to existing skip set
    if cli_skip:
        cfg.skip_fixes |= set(cli_skip) & ALL_FIX_KEYS

    return cfg


# ─────────────────────────────────────────────
#  Logger
# ─────────────────────────────────────────────

class DiagLogger:
    """
    Structured plain-text logger for the diagnostics run.

    Writes every command, its output, and collected tool logs
    (CBS.log from SFC, dism.log from DISM) into a single file.

    Usage:
        logger = DiagLogger(Path("log/win_diag_20250608.log"))
        logger.section("Diagnostics")
        logger.cmd("sfc /scannow", output, rc=0)
        logger.tool_log("SFC", Path("C:/Windows/Logs/CBS/CBS.log"))
        logger.info("Overall status: ok")
    """

    SEP_MAJOR = "=" * 72
    SEP_MINOR = "-" * 72

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use Python logging as the backend so the file is flushed on every write
        self._log = logging.getLogger(f"diag.{id(self)}")
        self._log.setLevel(logging.DEBUG)
        self._log.propagate = False
        fh = logging.FileHandler(str(path), encoding="utf-8", mode="w")
        fh.setFormatter(logging.Formatter("%(message)s"))
        self._log.addHandler(fh)
        self._write_header()

    def _ts(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _write_header(self) -> None:
        self._log.info(self.SEP_MAJOR)
        self._log.info(f"  Windows Diagnostics Log")
        self._log.info(f"  Started : {self._ts()}")
        self._log.info(f"  Host    : {socket.gethostname()}")
        self._log.info(f"  User    : {os.environ.get('USERNAME', 'unknown')}")
        self._log.info(f"  Python  : {platform.python_version()}")
        self._log.info(self.SEP_MAJOR)
        self._log.info("")

    def section(self, title: str) -> None:
        """Write a major section header."""
        self._log.info("")
        self._log.info(self.SEP_MAJOR)
        self._log.info(f"  {title.upper()}")
        self._log.info(f"  {self._ts()}")
        self._log.info(self.SEP_MAJOR)

    def step(self, label: str) -> None:
        """Write a minor step header."""
        self._log.info("")
        self._log.info(self.SEP_MINOR)
        self._log.info(f"  {label}")
        self._log.info(self.SEP_MINOR)

    def cmd(self, command: str, output: str, rc: int) -> None:
        """Log a command invocation with its full output and return code."""
        self._log.info(f"  $ {command}")
        self._log.info(f"  Exit code: {rc}")
        if output.strip():
            self._log.info("  Output:")
            for line in output.splitlines():
                self._log.info(f"    {line}")
        else:
            self._log.info("  Output: (empty)")

    def info(self, message: str) -> None:
        """Write a plain informational line."""
        self._log.info(f"  {message}")

    def result(self, name: str, success: bool, detail: str) -> None:
        """Write a fix/check result summary line."""
        icon = "PASS" if success else "FAIL"
        self._log.info(f"  [{icon}] {name}: {detail}")

    def tool_log(self, tool_name: str, log_path: Path) -> None:
        """
        Read a Windows tool log file (CBS.log, dism.log) and append its
        last N lines into the diagnostics log.

        Windows tool logs are UTF-16 LE with BOM. Detecting this via BOM bytes
        is reliable; trying to decode as UTF-8 with errors="replace" silently
        produces mojibake instead of raising an exception, so encoding must be
        determined before calling read_text().
        """
        max_lines = 200  # Only include the tail — full CBS.log can be 100MB+
        self._log.info("")
        self._log.info(self.SEP_MINOR)
        self._log.info(f"  TOOL LOG: {tool_name}  ({log_path})")
        self._log.info(self.SEP_MINOR)

        try:
            raw = log_path.read_bytes()
            if not raw:
                self._log.info("  (file is empty)")
                return

            # Detect encoding from BOM — Windows tool logs are UTF-16 LE
            if raw[:2] == b"\xff\xfe":
                text = raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
            elif raw[:2] == b"\xfe\xff":
                text = raw.decode("utf-16-be", errors="replace").lstrip("\ufeff")
            elif raw[:3] == b"\xef\xbb\xbf":
                text = raw[3:].decode("utf-8", errors="replace")
            else:
                # No BOM — try UTF-8, fall back to latin-1
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin-1", errors="replace")

            lines = [l for l in text.splitlines() if l.strip()]
            if not lines:
                self._log.info("  (file is empty or could not be decoded)")
                return

            tail = lines[-max_lines:]
            if len(lines) > max_lines:
                self._log.info(f"  [showing last {max_lines} of {len(lines)} lines]")
            for line in tail:
                self._log.info(f"    {line}")

        except FileNotFoundError:
            self._log.info("  (file not found — tool may not have run yet)")
        except PermissionError:
            self._log.info("  (permission denied — run as Administrator to read this log)")
        except Exception as e:
            self._log.info(f"  (could not read log: {e})")

    def finalize(self, overall: str, fixes_ok: int, fixes_fail: int,
                 report_path: Path | None) -> None:
        """Write the closing summary block."""
        self._log.info("")
        self._log.info(self.SEP_MAJOR)
        self._log.info("  SUMMARY")
        self._log.info(self.SEP_MAJOR)
        self._log.info(f"  Finished  : {self._ts()}")
        self._log.info(f"  Status    : {overall.upper()}")
        self._log.info(f"  Fixes     : {fixes_ok} passed / {fixes_fail} failed")
        if report_path:
            self._log.info(f"  Report    : {report_path}")
        self._log.info(self.SEP_MAJOR)


# Sentinel used when no logger is active (no-op logger)
class _NullLogger:
    def section(self, *a, **kw): pass
    def step(self, *a, **kw): pass
    def cmd(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def result(self, *a, **kw): pass
    def tool_log(self, *a, **kw): pass
    def finalize(self, *a, **kw): pass


# Module-level logger instance — replaced in main() with a real DiagLogger
_logger: DiagLogger | _NullLogger = _NullLogger()

# Module-level config instance — replaced in main() after CLI + TOML merge
_cfg: DiagConfig = DiagConfig()


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

class Spinner:
    """
    Thread-based terminal spinner.
    Usage:
        with Spinner("Running sfc /scannow"):
            long_running_call()
    On success prints: "  + label  (1.2s)"
    On failure prints: "  x label"
    """

    FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    # Fallback for terminals that can't render braille
    FRAMES_ASCII = ["-", "\\", "|", "/"]

    def __init__(self, label: str, indent: int = 2):
        self.label   = label
        self.indent  = " " * indent
        self._stop   = threading.Event()
        self._thread = None
        self._start  = 0.0
        self._ok     = True
        # Detect whether the terminal supports Unicode
        try:
            "⠋".encode(sys.stdout.encoding or "utf-8")
            self._frames = self.FRAMES
        except (UnicodeEncodeError, LookupError):
            self._frames = self.FRAMES_ASCII

    def _spin(self) -> None:
        for frame in itertools.cycle(self._frames):
            if self._stop.is_set():
                break
            print(f"\r{self.indent}{frame}  {self.label}...", end="", flush=True)
            time.sleep(0.08)

    def __enter__(self) -> "Spinner":
        self._start  = time.monotonic()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._ok = exc_type is None
        self._stop.set()
        self._thread.join()
        elapsed = time.monotonic() - self._start
        if self._ok:
            print(f"\r{self.indent}\033[32m+\033[0m  {self.label:<50}  \033[2m{elapsed:.1f}s\033[0m")
        else:
            print(f"\r{self.indent}\033[31mx\033[0m  {self.label:<50}")
        return False  # Do not suppress exceptions

    def fail(self, reason: str = "") -> None:
        """Mark spinner as failed without raising an exception."""
        self._ok = False
        self._stop.set()
        if self._thread:
            self._thread.join()
        elapsed = time.monotonic() - self._start
        suffix  = f"  \033[2m{reason}\033[0m" if reason else ""
        print(f"\r{self.indent}\033[31mx\033[0m  {self.label:<50}{suffix}")

    def done(self, note: str = "") -> None:
        """Mark spinner as done with an optional inline note."""
        self._stop.set()
        if self._thread:
            self._thread.join()
        elapsed = time.monotonic() - self._start
        suffix  = f"  \033[2m{note}\033[0m" if note else f"  \033[2m{elapsed:.1f}s\033[0m"
        print(f"\r{self.indent}\033[32m+\033[0m  {self.label:<50}{suffix}")


def run(cmd: list[str], timeout: int = 60) -> tuple[str, int]:
    """Run a command, log it, and return (stdout, returncode)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = result.stdout.strip()
        rc  = result.returncode
        _logger.cmd(" ".join(cmd), out + (("\nSTDERR: " + result.stderr.strip()) if result.stderr.strip() else ""), rc)
        return out, rc
    except subprocess.TimeoutExpired:
        _logger.cmd(" ".join(cmd), "[Timeout]", -1)
        return "[Timeout]", -1
    except FileNotFoundError:
        _logger.cmd(" ".join(cmd), "[Command not found]", -1)
        return "[Command not found]", -1
    except Exception as e:
        _logger.cmd(" ".join(cmd), f"[Error: {e}]", -1)
        return f"[Error: {e}]", -1


def run_ps(script: str, timeout: int = 60) -> str:
    """Run a PowerShell snippet and return stdout."""
    out, _ = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )
    return out


def is_admin() -> bool:
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def parse_wmi_date(raw: str) -> str:
    """Parse WMI /Date(ms)/ or ISO timestamp into a readable string."""
    if not raw:
        return "unknown"
    match = re.search(r"\d{13}", raw)
    if match:
        return datetime.datetime.fromtimestamp(int(match.group()) / 1000).strftime("%Y-%m-%d %H:%M")
    return raw[:16].replace("T", " ")


# ─────────────────────────────────────────────
#  Diagnostic modules
# ─────────────────────────────────────────────

def collect_system_info() -> dict:
    _logger.step("System information")
    hostname = socket.gethostname()
    user = os.environ.get("USERNAME", "unknown")
    now = datetime.datetime.now()

    ver_raw = run_ps(
        "(Get-CimInstance Win32_OperatingSystem | "
        "Select-Object Caption, Version, BuildNumber, LastBootUpTime | ConvertTo-Json)"
    )
    try:
        ver = json.loads(ver_raw)
        os_name    = ver.get("Caption", platform.version())
        os_version = ver.get("Version", "")
        build      = ver.get("BuildNumber", "")
        last_boot  = parse_wmi_date(ver.get("LastBootUpTime", ""))
    except Exception:
        os_name = platform.version()
        os_version = build = ""
        last_boot = "unknown"

    uptime_s = run_ps(
        "(New-TimeSpan -Start (gcim Win32_OperatingSystem).LastBootUpTime).TotalSeconds"
    )
    try:
        secs = int(float(uptime_s.strip()))
        h, rem = divmod(secs, 3600)
        m = rem // 60
        uptime = f"{h}h {m}min"
    except Exception:
        uptime = "unknown"

    cpu_raw = run_ps(
        # Sum across all physical processor sockets — -First 1 only returns one CCD
        # which gives wrong counts on multi-CCD CPUs like the Ryzen 9 9950X3D
        "$procs = Get-CimInstance Win32_Processor; "
        "[PSCustomObject]@{"
        "Name=($procs | Select-Object -First 1 -ExpandProperty Name);"
        "NumberOfCores=($procs | Measure-Object -Property NumberOfCores -Sum).Sum;"
        "NumberOfLogicalProcessors=($procs | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum"
        "} | ConvertTo-Json"
    )
    try:
        cpu = json.loads(cpu_raw)
        cpu_name    = cpu.get("Name", "unknown").strip()
        cpu_cores   = cpu.get("NumberOfCores", "?")
        cpu_threads = cpu.get("NumberOfLogicalProcessors", "?")
    except Exception:
        cpu_name = "unknown"
        cpu_cores = cpu_threads = "?"

    ram_raw = run_ps(
        "$os=Get-CimInstance Win32_OperatingSystem; "
        "[PSCustomObject]@{"
        "TotalGB=[math]::Round($os.TotalVisibleMemorySize/1MB,1);"
        "FreeGB=[math]::Round($os.FreePhysicalMemory/1MB,1)"
        "} | ConvertTo-Json"
    )
    try:
        ram = json.loads(ram_raw)
        ram_total = ram.get("TotalGB", 0)
        ram_free  = ram.get("FreeGB", 0)
        ram_used  = round(ram_total - ram_free, 1)
        ram_pct   = round((ram_used / ram_total) * 100) if ram_total else 0
    except Exception:
        ram_total = ram_free = ram_used = ram_pct = 0

    return {
        "hostname":       hostname,
        "user":           user,
        "timestamp":      now.strftime("%Y-%m-%d %H:%M:%S"),
        "os_name":        os_name,
        "os_version":     os_version,
        "build":          build,
        "last_boot":      last_boot,
        "uptime":         uptime,
        "cpu_name":       cpu_name,
        "cpu_cores":      cpu_cores,
        "cpu_threads":    cpu_threads,
        "ram_total":      ram_total,
        "ram_free":       ram_free,
        "ram_used":       ram_used,
        "ram_pct":        ram_pct,
        "is_admin":       is_admin(),
        "python_version": platform.python_version(),
    }


def collect_disk_info() -> list[dict]:
    _logger.step("Drives")
    raw = run_ps(
        "Get-PSDrive -PSProvider FileSystem | Where-Object {$_.Used -ne $null} | "
        "Select-Object Name, "
        "@{N='UsedGB';E={[math]::Round($_.Used/1GB,1)}}, "
        "@{N='FreeGB';E={[math]::Round($_.Free/1GB,1)}} | ConvertTo-Json"
    )
    disks = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for d in data:
            used   = d.get("UsedGB", 0) or 0
            free   = d.get("FreeGB", 0) or 0
            total  = round(used + free, 1)
            pct    = round((used / total) * 100) if total else 0
            status = "critical" if pct >= 90 else ("warning" if pct >= 75 else "ok")
            disks.append({
                "name":   d.get("Name", "?") + ":",
                "used":   used,
                "free":   free,
                "total":  total,
                "pct":    pct,
                "status": status,
            })
    except Exception:
        pass
    return disks


def collect_network_info() -> dict:
    _logger.step("Network")
    adapters_raw = run_ps(
        "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object Name, InterfaceDescription, LinkSpeed | ConvertTo-Json"
    )
    adapters = []
    try:
        data = json.loads(adapters_raw)
        if isinstance(data, dict):
            data = [data]
        for a in data:
            adapters.append({
                "name":  a.get("Name", "?"),
                "desc":  a.get("InterfaceDescription", ""),
                "speed": a.get("LinkSpeed", "?"),
            })
    except Exception:
        pass

    ip_raw = run_ps(
        "Get-NetIPConfiguration | Select-Object InterfaceAlias, "
        "@{N='IPv4';E={$_.IPv4Address.IPAddress}}, "
        "@{N='Gateway';E={$_.IPv4DefaultGateway.NextHop}}, "
        "@{N='DNS';E={($_.DNSServer.ServerAddresses -join ', ')}} | ConvertTo-Json"
    )
    ip_configs = []
    try:
        data = json.loads(ip_raw)
        if isinstance(data, dict):
            data = [data]
        for c in data:
            ipv4 = c.get("IPv4")
            if not ipv4:
                continue
            if isinstance(ipv4, list):
                ipv4 = ipv4[0]
            ip_configs.append({
                "iface":   c.get("InterfaceAlias", "?"),
                "ipv4":    ipv4 or "-",
                "gateway": c.get("Gateway") or "-",
                "dns":     c.get("DNS") or "-",
            })
    except Exception:
        pass

    gateway = next((c["gateway"] for c in ip_configs if c["gateway"] != "-"), None)
    ping_targets = [
        ("Gateway", None),
        ("8.8.8.8",    "Google DNS"),
        ("1.1.1.1",    "Cloudflare DNS"),
        ("google.com", "DNS resolution"),
    ]
    pings = []
    for target, label in ping_targets:
        if target == "Gateway":
            if not gateway:
                continue
            target = gateway
            label  = f"Gateway ({gateway})"
        out, rc = run(["ping", "-n", "2", "-w", "1000", target])
        latency = "-"
        if rc == 0:
            m = re.search(r"Average\s*=\s*(\d+)ms|Durchschn\s*=\s*(\d+)ms", out)
            if m:
                latency = (m.group(1) or m.group(2)) + " ms"
        pings.append({"target": label or target, "ok": rc == 0, "latency": latency})

    try:
        socket.gethostbyname("google.com")
        dns_ok = True
    except Exception:
        dns_ok = False

    return {"adapters": adapters, "ip_configs": ip_configs, "pings": pings, "dns_ok": dns_ok}


def collect_windows_services() -> list[dict]:
    _logger.step("Services")
    critical = [
        "wuauserv",          # Windows Update
        "WinDefend",         # Windows Defender
        "mpssvc",            # Windows Firewall
        "BITS",              # Background Intelligent Transfer
        "eventlog",          # Event Log
        "Schedule",          # Task Scheduler
        "Spooler",           # Print Spooler
        "LanmanWorkstation", # Workstation
        "CryptSvc",          # Cryptographic Services
    ]
    # Merge extra services from config, deduplicate
    all_services = list(dict.fromkeys(critical + _cfg.extra_services))
    names_ps = ", ".join(f'"{s}"' for s in all_services)
    raw = run_ps(
        f"Get-Service -Name @({names_ps}) -ErrorAction SilentlyContinue | "
        "Select-Object Name, DisplayName, Status | ConvertTo-Json"
    )
    services = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for s in data:
            status  = str(s.get("Status", "")).lower()
            running = "running" in status or "4" in status
            name    = s.get("Name", "?")
            services.append({
                "name":    name,
                "display": s.get("DisplayName", name),
                "running": running,
                "ignored": name in _cfg.ignore_services,
            })
    except Exception:
        pass
    return services


def collect_event_errors() -> list[dict]:
    _logger.step("Event log (last 24h)")
    raw = run_ps(
        "Get-WinEvent -FilterHashtable @{LogName='System';Level=2;"
        "StartTime=(Get-Date).AddHours(-24)} -MaxEvents 10 "
        "-ErrorAction SilentlyContinue | "
        "Select-Object TimeCreated, Id, ProviderName, Message | ConvertTo-Json",
        timeout=30,
    )
    events = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for e in data:
            tc  = parse_wmi_date(e.get("TimeCreated", ""))
            msg = (e.get("Message") or "")[:200].replace("\n", " ").strip()
            events.append({
                "time":    tc,
                "id":      e.get("Id", "?"),
                "source":  e.get("ProviderName", "?"),
                "message": msg,
            })
    except Exception:
        pass
    return events


def collect_startup_items() -> list[dict]:
    _logger.step("Startup items")
    raw = run_ps(
        "Get-CimInstance Win32_StartupCommand | "
        "Select-Object Name, Command, Location, User | ConvertTo-Json"
    )
    items = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for s in data:
            items.append({
                "name":     (s.get("Name") or "?")[:60],
                "command":  (s.get("Command") or "?")[:80],
                "location": s.get("Location") or "?",
                "user":     s.get("User") or "All",
            })
    except Exception:
        pass
    return items[:20]


def collect_updates() -> dict:
    _logger.step("Windows Update")
    raw = run_ps(
        "Get-HotFix | Sort-Object InstalledOn -Descending | "
        "Select-Object -First 5 HotFixID, Description, InstalledOn | ConvertTo-Json"
    )
    updates = []
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        for u in data:
            updates.append({
                "id":   u.get("HotFixID", "?"),
                "desc": u.get("Description", "?"),
                "date": parse_wmi_date(u.get("InstalledOn") or ""),
            })
    except Exception:
        pass

    wu_status  = run_ps("(Get-Service wuauserv).Status").strip()
    wu_running = "running" in wu_status.lower() or "4" in wu_status
    return {"recent": updates, "service_running": wu_running}


# ─────────────────────────────────────────────
#  Fix modules
# ─────────────────────────────────────────────

def fix_sfc() -> dict:
    """Run System File Checker — scans and repairs protected system files."""
    # sfc writes UTF-16 LE output; reading as UTF-8 produces garbled text with spaces
    # between every character. Capture raw bytes and decode manually.
    try:
        proc = subprocess.run(
            ["sfc", "/scannow"],
            capture_output=True,
            timeout=600,
        )
        # Try UTF-16 LE first (Windows native), fall back to UTF-8
        try:
            out = proc.stdout.decode("utf-16-le", errors="replace").strip()
        except Exception:
            out = proc.stdout.decode("utf-8", errors="replace").strip()
        rc = proc.returncode
        _logger.cmd("sfc /scannow", out, rc)
    except subprocess.TimeoutExpired:
        out, rc = "[Timeout]", -1
        _logger.cmd("sfc /scannow", out, rc)
    except Exception as e:
        out, rc = f"[Error: {e}]", -1
        _logger.cmd("sfc /scannow", out, rc)

    out_lower = out.lower()
    if "did not find any integrity violations" in out_lower:
        result = "No integrity violations found — system files are clean."
    elif "successfully repaired" in out_lower:
        result = "Corrupt files found and successfully repaired."
    elif "unable to fix" in out_lower:
        result = "Corrupt files found but could not be repaired. Run DISM /RestoreHealth next."
    else:
        result = out[:300] if out else "No output — check CBS.log for details."

    # Append CBS.log tail to the diagnostics log
    cbs_log = Path(os.environ.get("WinDir", "C:\\Windows")) / "Logs" / "CBS" / "CBS.log"
    _logger.tool_log("SFC (CBS.log)", cbs_log)

    return {
        "name":    "SFC /scannow",
        "command": "sfc /scannow",
        "success": rc == 0,
        "result":  result,
        "log":     str(cbs_log),
    }


def fix_dism_check() -> dict:
    """DISM health check — fast, read-only, no internet required."""
    out, rc = run(
        ["DISM", "/Online", "/Cleanup-Image", "/CheckHealth"],
        timeout=120,
    )
    out_lower = out.lower()
    if "no component store corruption detected" in out_lower:
        result = "Component store is clean."
    elif "repairable" in out_lower:
        result = "Corruption detected — run DISM /RestoreHealth to repair."
    else:
        result = out[:300] if out else f"Exit code {rc}"

    dism_log = Path(os.environ.get("WinDir", "C:\\Windows")) / "Logs" / "DISM" / "dism.log"
    _logger.tool_log("DISM (dism.log)", dism_log)

    return {
        "name":    "DISM /CheckHealth",
        "command": "DISM /Online /Cleanup-Image /CheckHealth",
        "success": rc == 0,
        "result":  result,
        "log":     str(dism_log),
    }


def fix_dism_restore() -> dict:
    """DISM /RestoreHealth — repairs component store via Windows Update (requires internet)."""
    out, rc = run(
        ["DISM", "/Online", "/Cleanup-Image", "/RestoreHealth"],
        timeout=1800,
    )
    if rc == 0:
        result = "Component store successfully repaired. Re-run sfc /scannow now."
    else:
        result = f"DISM failed (exit {rc}). Check dism.log — may need install media."

    dism_log = Path(os.environ.get("WinDir", "C:\\Windows")) / "Logs" / "DISM" / "dism.log"
    _logger.tool_log("DISM /RestoreHealth (dism.log)", dism_log)

    return {
        "name":    "DISM /RestoreHealth",
        "command": "DISM /Online /Cleanup-Image /RestoreHealth",
        "success": rc == 0,
        "result":  result,
        "log":     str(dism_log),
    }


def fix_winsock_reset() -> dict:
    """Reset Winsock and TCP/IP stack — fixes mysterious connectivity issues."""
    _logger.step("Fix: Winsock & TCP/IP Reset")
    _, rc1 = run(["netsh", "winsock", "reset"], timeout=30)
    _, rc2 = run(["netsh", "int", "ip", "reset"], timeout=30)
    success = rc1 == 0 and rc2 == 0
    result = (
        "Winsock and TCP/IP stack reset successfully. Reboot required."
        if success
        else f"One or more resets failed (winsock rc={rc1}, ip rc={rc2})."
    )
    _logger.result("Winsock & TCP/IP Reset", success, result)
    return {
        "name":    "Winsock & TCP/IP Reset",
        "command": "netsh winsock reset && netsh int ip reset",
        "success": success,
        "result":  result,
        "log":     None,
    }


def fix_dns_flush() -> dict:
    """Flush DNS resolver cache."""
    _logger.step("Fix: DNS Cache Flush")
    out, rc = run(["ipconfig", "/flushdns"], timeout=15)
    result = "DNS resolver cache flushed." if rc == 0 else f"Failed (exit {rc}): {out[:100]}"
    _logger.result("DNS Cache Flush", rc == 0, result)
    return {
        "name":    "DNS Cache Flush",
        "command": "ipconfig /flushdns",
        "success": rc == 0,
        "result":  result,
        "log":     None,
    }


def fix_restart_service(service_name: str, display_name: str) -> dict:
    """Attempt to start a stopped critical service."""
    _logger.step(f"Fix: Start Service {display_name}")
    out, rc = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"Start-Service -Name '{service_name}' -ErrorAction Stop"],
        timeout=30,
    )
    success = rc == 0
    result = (
        f"Service '{display_name}' started successfully."
        if success
        else f"Could not start '{display_name}': {out[:200]}"
    )
    _logger.result(f"Start Service: {display_name}", success, result)
    return {
        "name":    f"Start Service: {display_name}",
        "command": f"Start-Service -Name '{service_name}'",
        "success": success,
        "result":  result,
        "log":     None,
    }


def fix_clear_temp() -> dict:
    """Delete contents of user and system TEMP folders."""
    _logger.step("Fix: Clear TEMP Folders")
    temp_dirs = [
        Path(os.environ.get("TEMP", "C:\\Windows\\Temp")),
        Path("C:\\Windows\\Temp"),
    ]
    removed = errors = 0
    for temp_dir in temp_dirs:
        if not temp_dir.exists():
            continue
        _logger.info(f"Clearing: {temp_dir}")
        for item in temp_dir.iterdir():
            try:
                if item.is_file():
                    item.unlink()
                    removed += 1
                elif item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                    removed += 1
            except Exception:
                errors += 1
    result = f"Removed {removed} items from TEMP folders ({errors} locked/skipped)."
    _logger.result("Clear TEMP Folders", True, result)
    return {
        "name":    "Clear TEMP Folders",
        "command": "del /q/f/s %TEMP%\\* & del /q/f/s C:\\Windows\\Temp\\*",
        "success": True,
        "result":  result,
        "log":     None,
    }


def fix_disk_cleanup(drive: str = "C") -> dict:
    """Schedule disk cleanup on the given drive via cleanmgr."""
    _logger.step(f"Fix: Disk Cleanup ({drive}:)")
    run_ps(
        "New-ItemProperty -Path "
        "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Explorer\\"
        "VolumeCaches\\Temporary Files' "
        "-Name StateFlags0100 -Value 255 -PropertyType DWORD -Force | Out-Null"
    )
    _, rc = run(["cleanmgr", f"/d{drive}", "/sagerun:100"], timeout=120)
    success = rc == 0
    result = (
        f"Disk cleanup launched for {drive}: — runs in background."
        if success
        else f"Disk cleanup failed or unavailable (exit {rc})."
    )
    _logger.result(f"Disk Cleanup ({drive}:)", success, result)
    return {
        "name":    f"Disk Cleanup ({drive}:)",
        "command": f"cleanmgr /d{drive} /sagerun:100",
        "success": success,
        "result":  result,
        "log":     None,
    }


def fix_restart_windows_update() -> dict:
    """Restart the Windows Update service."""
    _logger.step("Fix: Restart Windows Update Service")
    out, rc = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "Stop-Service wuauserv -Force -ErrorAction SilentlyContinue; "
         "Start-Sleep 2; Start-Service wuauserv; "
         "(Get-Service wuauserv).Status"],
        timeout=30,
    )
    success = "running" in out.lower() or rc == 0
    result = (
        "Windows Update service restarted successfully."
        if success
        else f"Could not restart Windows Update service: {out[:150]}"
    )
    _logger.result("Restart Windows Update Service", success, result)
    return {
        "name":    "Restart Windows Update Service",
        "command": "Stop-Service wuauserv -Force; Start-Service wuauserv",
        "success": success,
        "result":  result,
        "log":     None,
    }




# ─────────────────────────────────────────────
#  Overall status
# ─────────────────────────────────────────────

def compute_overall_status(data: dict) -> str:
    """
    Compute a weighted issue score and return 'ok', 'warning', or 'critical'.
    Benign sources and event IDs come from the active DiagConfig so they can
    be customised via win_diag.toml without touching the script.
    """
    issues = 0

    for d in data.get("disks", []):
        issues += 2 if d["status"] == "critical" else (1 if d["status"] == "warning" else 0)

    if data["system"]["ram_pct"] >= 90:
        issues += 2
    elif data["system"]["ram_pct"] >= 75:
        issues += 1

    for p in data["network"]["pings"]:
        if not p["ok"]:
            issues += 1

    # Ignore services that are in the ignore list
    for s in data.get("services", []):
        if not s["running"] and s["name"] not in _cfg.ignore_services:
            issues += 1

    # Count only events not covered by the benign filter
    actionable_events = [
        e for e in data.get("events", [])
        if e.get("source") not in _cfg.benign_sources
        and e.get("id") not in _cfg.benign_event_ids
    ]
    issues += min(len(actionable_events), 3)

    if issues >= 5:
        return "critical"
    elif issues >= 2:
        return "warning"
    return "ok"


# ─────────────────────────────────────────────
#  HTML report
# ─────────────────────────────────────────────

def generate_html(data: dict) -> str:
    sys_info = data["system"]
    disks    = data["disks"]
    net      = data["network"]
    services = data["services"]
    events   = data["events"]
    startup  = data["startup"]
    updates  = data["updates"]
    fixes    = data.get("fixes", [])
    overall  = data["overall"]

    overall_color = {"ok": "#22c55e", "warning": "#f59e0b", "critical": "#ef4444"}[overall]
    overall_label = {"ok": "System healthy", "warning": "Warnings detected", "critical": "Critical issues found"}[overall]
    overall_icon  = {"ok": "&#x2714;", "warning": "&#x26A0;", "critical": "&#x2718;"}[overall]

    def disk_bar(pct, status):
        color = {"ok": "#22c55e", "warning": "#f59e0b", "critical": "#ef4444"}[status]
        return (f'<div class="bar-bg">'
                f'<div class="bar-fill" style="width:{pct}%;background:{color}"></div>'
                f'</div>')

    def ram_bar(pct):
        color = "#22c55e" if pct < 75 else ("#f59e0b" if pct < 90 else "#ef4444")
        return (f'<div class="bar-bg large">'
                f'<div class="bar-fill" style="width:{pct}%;background:{color}"></div>'
                f'</div>')

    def status_badge(ok, true_label="Running", false_label="Stopped"):
        cls = "badge-ok" if ok else "badge-crit"
        label = true_label if ok else false_label
        return f'<span class="badge {cls}">{label}</span>'

    # ── Disks table
    disks_html = ""
    for d in disks:
        disks_html += (
            f"<tr>"
            f"<td class='td-mono'>{d['name']}</td>"
            f"<td>{d['total']} GB</td>"
            f"<td>{d['used']} GB</td>"
            f"<td>{d['free']} GB</td>"
            f"<td style='min-width:160px'>"
            f"{disk_bar(d['pct'], d['status'])} "
            f"<span class='bar-label'>{d['pct']}%</span>"
            f"</td>"
            f"</tr>"
        )

    # ── Network adapters
    net_adapters_html = "".join(
        f"<tr><td>{a['name']}</td><td>{a['desc']}</td><td>{a['speed']}</td></tr>"
        for a in net["adapters"]
    ) or "<tr><td colspan='3'>No active adapters found</td></tr>"

    # ── IP configuration
    net_ip_html = "".join(
        f"<tr>"
        f"<td>{c['iface']}</td>"
        f"<td class='td-mono'>{c['ipv4']}</td>"
        f"<td class='td-mono'>{c['gateway']}</td>"
        f"<td class='td-mono'>{c['dns']}</td>"
        f"</tr>"
        for c in net["ip_configs"]
    ) or "<tr><td colspan='4'>No data</td></tr>"

    # ── Ping tests
    pings_html = "".join(
        f"<tr>"
        f"<td>{p['target']}</td>"
        f"<td>{status_badge(p['ok'], 'OK', 'Failed')}</td>"
        f"<td>{p['latency']}</td>"
        f"</tr>"
        for p in net["pings"]
    ) or "<tr><td colspan='3'>No ping results</td></tr>"

    # ── Services
    services_html = "".join(
        f"<tr>"
        f"<td>{s['display']}</td>"
        f"<td class='td-mono'>{s['name']}</td>"
        f"<td>{status_badge(s['running'])}</td>"
        f"</tr>"
        for s in services
    ) or "<tr><td colspan='3'>No service data</td></tr>"

    # ── Event log
    if events:
        events_html = "".join(
            f"<tr>"
            f"<td style='white-space:nowrap'>{e['time']}</td>"
            f"<td class='td-mono'>{e['id']}</td>"
            f"<td>{e['source']}</td>"
            f"<td style='font-size:0.78rem;color:#94a3b8'>{e['message']}</td>"
            f"</tr>"
            for e in events
        )
    else:
        events_html = (
            '<tr><td colspan="4" style="color:#22c55e;text-align:center">'
            'No errors in the last 24 hours &#x2714;</td></tr>'
        )

    # ── Startup items
    startup_html = "".join(
        f"<tr>"
        f"<td>{s['name']}</td>"
        f"<td class='td-mono' style='font-size:0.75rem'>{s['command']}</td>"
        f"<td style='font-size:0.78rem;color:#94a3b8'>{s['location']}</td>"
        f"<td>{s['user']}</td>"
        f"</tr>"
        for s in startup
    ) or "<tr><td colspan='4'>No startup entries found</td></tr>"

    # ── Windows Updates
    updates_html = "".join(
        f"<tr>"
        f"<td class='td-mono'>{u['id']}</td>"
        f"<td>{u['desc']}</td>"
        f"<td>{u['date']}</td>"
        f"</tr>"
        for u in updates["recent"]
    ) or "<tr><td colspan='3'>No update data available</td></tr>"

    # ── Fixes
    fixes_ok   = sum(1 for f in fixes if f["success"])
    fixes_fail = len(fixes) - fixes_ok
    needs_reboot = any(
        "reset" in f.get("command", "").lower() and f["success"]
        for f in fixes
    )

    if fixes:
        fixes_html = ""
        for f in fixes:
            icon   = "&#x2714;" if f["success"] else "&#x2718;"
            b_cls  = "badge-ok" if f["success"] else "badge-crit"
            b_label = "Passed" if f["success"] else "Failed"
            row_cls = "fix-ok" if f["success"] else "fix-fail"
            log_html = (
                f'<span class="fix-log">Log: <code>{f["log"]}</code></span>'
                if f.get("log") else ""
            )
            fixes_html += (
                f'<div class="fix-row {row_cls}">'
                f'  <div class="fix-header">'
                f'    <span class="fix-icon">{icon}</span>'
                f'    <span class="fix-name">{f["name"]}</span>'
                f'    <span class="badge {b_cls}" style="margin-left:auto">{b_label}</span>'
                f'  </div>'
                f'  <div class="fix-detail">'
                f'    <code class="fix-cmd">{f["command"]}</code>'
                f'    <p class="fix-result">{f["result"]}</p>'
                f'    {log_html}'
                f'  </div>'
                f'</div>'
            )
    else:
        fixes_html = '<p style="color:var(--text3);padding:0.5rem 0">No fixes were run.</p>'

    admin_badge = status_badge(sys_info["is_admin"], "Admin", "No Admin")
    wu_badge    = status_badge(updates["service_running"])

    fix_summary_html = ""
    if fixes:
        reboot_note = " &nbsp;|&nbsp; <span style='color:#f59e0b'>&#x26A0; Reboot recommended</span>" if needs_reboot else ""
        fix_summary_html = (
            f'<div class="fix-summary">'
            f'  <strong>Fixes applied:</strong>'
            f'  <span style="color:#22c55e">&#x2714; {fixes_ok} passed</span>'
            f'  <span class="sep">|</span>'
            f'  <span style="color:#ef4444">&#x2718; {fixes_fail} failed</span>'
            f'  {reboot_note}'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Windows Diagnostics &mdash; {sys_info['hostname']}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

  :root {{
    --bg:       #0a0e17;
    --surface:  #111827;
    --surface2: #1a2233;
    --border:   #1e2d45;
    --accent:   #3b82f6;
    --accent2:  #60a5fa;
    --text:     #e2e8f0;
    --text2:    #94a3b8;
    --text3:    #475569;
    --ok:       #22c55e;
    --warn:     #f59e0b;
    --crit:     #ef4444;
    --r:        10px;
  }}

  * {{ box-sizing:border-box; margin:0; padding:0; }}

  body {{
    font-family:'IBM Plex Sans', sans-serif;
    background:var(--bg); color:var(--text);
    font-size:14px; line-height:1.6;
  }}

  body::before {{
    content:''; position:fixed; inset:0;
    background-image:
      linear-gradient(rgba(59,130,246,.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(59,130,246,.03) 1px, transparent 1px);
    background-size:40px 40px; pointer-events:none; z-index:0;
  }}

  .page {{ position:relative; z-index:1; max-width:1100px; margin:0 auto; padding:2rem 1.5rem 4rem; }}

  .header {{
    display:flex; flex-wrap:wrap; align-items:flex-start;
    justify-content:space-between; gap:1rem;
    padding:2rem; background:var(--surface);
    border:1px solid var(--border); border-radius:var(--r); margin-bottom:1.5rem;
  }}
  .header-left h1 {{ font-size:1.5rem; font-weight:600; letter-spacing:-0.02em; }}
  .header-left h1 span {{ color:var(--accent2); }}
  .header-left .sub {{ font-size:0.82rem; color:var(--text2); margin-top:4px; font-family:'IBM Plex Mono',monospace; }}

  .status-pill {{
    display:flex; align-items:center; gap:10px;
    padding:0.75rem 1.25rem; border-radius:50px; border:1px solid;
    font-weight:500; font-size:0.9rem;
  }}
  .status-dot {{ width:10px; height:10px; border-radius:50%; animation:pulse 2s infinite; }}
  @keyframes pulse {{ 0%,100%{{opacity:1;transform:scale(1)}} 50%{{opacity:.6;transform:scale(1.3)}} }}

  .fix-summary {{
    display:flex; align-items:center; gap:1rem; flex-wrap:wrap;
    padding:0.9rem 1.5rem; background:var(--surface);
    border:1px solid var(--border); border-radius:var(--r); margin-bottom:1.25rem;
    font-size:0.85rem;
  }}
  .fix-summary strong {{ color:var(--text); }}
  .fix-summary .sep {{ color:var(--border); }}

  .meta-grid {{
    display:grid; grid-template-columns:repeat(auto-fill, minmax(200px,1fr));
    gap:0.75rem; margin-bottom:1.5rem;
  }}
  .meta-card {{
    background:var(--surface); border:1px solid var(--border);
    border-radius:var(--r); padding:1rem 1.25rem;
  }}
  .meta-card .lbl {{ font-size:0.7rem; text-transform:uppercase; letter-spacing:.08em; color:var(--text3); margin-bottom:4px; }}
  .meta-card .val {{ font-size:0.95rem; font-weight:500; font-family:'IBM Plex Mono',monospace; word-break:break-all; }}

  .section {{
    background:var(--surface); border:1px solid var(--border);
    border-radius:var(--r); margin-bottom:1.25rem; overflow:hidden;
  }}
  .sec-hdr {{
    display:flex; align-items:center; gap:10px; padding:1rem 1.5rem;
    background:var(--surface2); border-bottom:1px solid var(--border);
    cursor:pointer; user-select:none;
  }}
  .sec-hdr:hover {{ background:#1f2f48; }}
  .sec-icon {{
    width:28px; height:28px; border-radius:6px;
    background:rgba(59,130,246,.15); display:flex; align-items:center;
    justify-content:center; font-size:0.85rem; color:var(--accent2); flex-shrink:0;
  }}
  .sec-title {{ font-weight:500; font-size:0.9rem; flex:1; }}
  .sec-chevron {{ color:var(--text3); font-size:0.8rem; transition:transform .2s; }}
  .sec-body {{ padding:1.25rem 1.5rem; }}
  .section.collapsed .sec-body {{ display:none; }}
  .section.collapsed .sec-chevron {{ transform:rotate(-90deg); }}

  table {{ width:100%; border-collapse:collapse; }}
  th {{
    text-align:left; font-size:0.72rem; text-transform:uppercase;
    letter-spacing:.07em; color:var(--text3); padding:0 0.75rem 0.6rem; font-weight:500;
  }}
  td {{
    padding:0.55rem 0.75rem; border-top:1px solid var(--border);
    color:var(--text2); font-size:0.85rem; vertical-align:middle;
  }}
  tr:first-child td {{ border-top:none; }}
  .td-mono {{ font-family:'IBM Plex Mono',monospace; font-size:0.8rem; }}

  .badge {{ display:inline-block; padding:2px 9px; border-radius:4px; font-size:0.72rem; font-weight:500; letter-spacing:.04em; }}
  .badge-ok   {{ background:rgba(34,197,94,.12);  color:#22c55e; border:1px solid rgba(34,197,94,.25); }}
  .badge-warn {{ background:rgba(245,158,11,.12); color:#f59e0b; border:1px solid rgba(245,158,11,.25); }}
  .badge-crit {{ background:rgba(239,68,68,.12);  color:#ef4444; border:1px solid rgba(239,68,68,.25); }}

  .bar-bg {{ display:inline-block; width:110px; height:6px; background:var(--border); border-radius:3px; vertical-align:middle; margin-right:6px; }}
  .bar-bg.large {{ display:block; width:100%; height:10px; margin:0; }}
  .bar-fill {{ height:100%; border-radius:3px; transition:width .5s; }}
  .bar-label {{ font-size:0.78rem; color:var(--text2); }}

  .ram-row {{ display:flex; align-items:center; gap:1.5rem; flex-wrap:wrap; }}
  .ram-stat {{ text-align:center; min-width:80px; }}
  .ram-stat .v {{ font-size:1.5rem; font-weight:600; font-family:'IBM Plex Mono',monospace; }}
  .ram-stat .l {{ font-size:0.7rem; color:var(--text3); text-transform:uppercase; }}
  .ram-bar-wrap {{ flex:1; min-width:200px; }}
  .ram-bar-wrap .row {{ display:flex; justify-content:space-between; margin-bottom:6px; font-size:0.75rem; color:var(--text3); }}
  .ram-bar-wrap .row span:last-child {{ font-weight:500; color:var(--text2); }}

  .two-col {{ display:grid; grid-template-columns:1fr 1fr; gap:1.25rem; }}
  @media (max-width:700px) {{ .two-col {{ grid-template-columns:1fr; }} }}

  .sub-h {{ font-size:0.72rem; text-transform:uppercase; letter-spacing:.07em; color:var(--text3); margin-bottom:8px; margin-top:1.25rem; }}
  .sub-h:first-child {{ margin-top:0; }}

  .fix-row {{ border:1px solid var(--border); border-radius:8px; margin-bottom:10px; overflow:hidden; }}
  .fix-ok   {{ border-color:rgba(34,197,94,.2); }}
  .fix-fail {{ border-color:rgba(239,68,68,.2); }}
  .fix-header {{ display:flex; align-items:center; gap:10px; padding:0.65rem 1rem; background:var(--surface2); }}
  .fix-ok   .fix-header {{ background:rgba(34,197,94,.05); }}
  .fix-fail .fix-header {{ background:rgba(239,68,68,.05); }}
  .fix-icon {{ font-size:0.9rem; width:20px; text-align:center; }}
  .fix-ok   .fix-icon {{ color:#22c55e; }}
  .fix-fail .fix-icon {{ color:#ef4444; }}
  .fix-name {{ font-weight:500; font-size:0.88rem; }}
  .fix-detail {{ padding:0.75rem 1rem; border-top:1px solid var(--border); }}
  .fix-cmd {{ display:block; font-family:'IBM Plex Mono',monospace; font-size:0.78rem; background:rgba(0,0,0,.3); color:var(--accent2); padding:4px 10px; border-radius:4px; margin-bottom:6px; }}
  .fix-result {{ font-size:0.82rem; color:var(--text2); margin-bottom:4px; }}
  .fix-log {{ font-size:0.75rem; color:var(--text3); }}
  .fix-log code {{ font-size:0.73rem; color:var(--text3); }}

  .footer {{ text-align:center; margin-top:2rem; font-size:0.75rem; color:var(--text3); font-family:'IBM Plex Mono',monospace; }}

  @media print {{
    body::before {{ display:none; }}
    .section.collapsed .sec-body {{ display:block !important; }}
    .sec-hdr {{ cursor:default; }}
  }}
</style>
</head>
<body>
<div class="page">

  <div class="header">
    <div class="header-left">
      <h1>Windows Diagnostics &mdash; <span>{sys_info['hostname']}</span></h1>
      <div class="sub">Generated: {sys_info['timestamp']} &nbsp;|&nbsp; User: {sys_info['user']} &nbsp;|&nbsp; {admin_badge}</div>
    </div>
    <div class="status-pill" style="color:{overall_color};border-color:{overall_color}30;background:{overall_color}10">
      <span class="status-dot" style="background:{overall_color}"></span>
      {overall_icon} &nbsp;{overall_label}
    </div>
  </div>

  {fix_summary_html}

  <div class="meta-grid">
    <div class="meta-card"><div class="lbl">Operating System</div><div class="val" style="font-size:0.82rem">{sys_info['os_name']}</div></div>
    <div class="meta-card"><div class="lbl">Build / Version</div><div class="val">{sys_info['build']} <span style="color:var(--text3)">({sys_info['os_version']})</span></div></div>
    <div class="meta-card"><div class="lbl">Last Boot</div><div class="val">{sys_info['last_boot']}</div></div>
    <div class="meta-card"><div class="lbl">Uptime</div><div class="val">{sys_info['uptime']}</div></div>
    <div class="meta-card"><div class="lbl">CPU</div><div class="val" style="font-size:0.78rem">{sys_info['cpu_name']}</div></div>
    <div class="meta-card"><div class="lbl">Cores / Threads</div><div class="val">{sys_info['cpu_cores']} / {sys_info['cpu_threads']}</div></div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x1F4BE;</div>
      <span class="sec-title">Memory (RAM)</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      <div class="ram-row">
        <div class="ram-stat"><div class="v">{sys_info['ram_total']} GB</div><div class="l">Total</div></div>
        <div class="ram-stat"><div class="v">{sys_info['ram_used']} GB</div><div class="l">Used</div></div>
        <div class="ram-stat"><div class="v">{sys_info['ram_free']} GB</div><div class="l">Free</div></div>
        <div class="ram-bar-wrap">
          <div class="row"><span>Usage</span><span>{sys_info['ram_pct']}%</span></div>
          {ram_bar(sys_info['ram_pct'])}
        </div>
      </div>
    </div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x1F5B4;</div>
      <span class="sec-title">Drives</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      <table>
        <tr><th>Drive</th><th>Total</th><th>Used</th><th>Free</th><th>Usage</th></tr>
        {disks_html}
      </table>
    </div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x1F310;</div>
      <span class="sec-title">Network</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      <div class="two-col">
        <div>
          <div class="sub-h">Active Adapters</div>
          <table>
            <tr><th>Name</th><th>Description</th><th>Speed</th></tr>
            {net_adapters_html}
          </table>
        </div>
        <div>
          <div class="sub-h">Ping Tests</div>
          <table>
            <tr><th>Target</th><th>Status</th><th>Latency</th></tr>
            {pings_html}
          </table>
        </div>
      </div>
      <div class="sub-h">IP Configuration</div>
      <table>
        <tr><th>Interface</th><th>IPv4</th><th>Gateway</th><th>DNS</th></tr>
        {net_ip_html}
      </table>
    </div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x2699;</div>
      <span class="sec-title">Critical System Services</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      <table>
        <tr><th>Display Name</th><th>Service Name</th><th>Status</th></tr>
        {services_html}
      </table>
    </div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x26A0;</div>
      <span class="sec-title">System Errors (last 24h)</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      <table>
        <tr><th>Time</th><th>ID</th><th>Source</th><th>Message</th></tr>
        {events_html}
      </table>
    </div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x1F504;</div>
      <span class="sec-title">Windows Update</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      <div style="margin-bottom:1rem">Update service (wuauserv): &nbsp;{wu_badge}</div>
      <div class="sub-h">Recently Installed Updates</div>
      <table>
        <tr><th>KB</th><th>Type</th><th>Installed</th></tr>
        {updates_html}
      </table>
    </div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x1F680;</div>
      <span class="sec-title">Startup Items</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      <table>
        <tr><th>Name</th><th>Command</th><th>Location</th><th>User</th></tr>
        {startup_html}
      </table>
    </div>
  </div>

  <div class="section">
    <div class="sec-hdr" onclick="toggle(this)">
      <div class="sec-icon">&#x1F527;</div>
      <span class="sec-title">Applied Fixes</span>
      <span class="sec-chevron">&#x25BE;</span>
    </div>
    <div class="sec-body">
      {fixes_html}
    </div>
  </div>

  <div class="footer">
    Generated by win_diag.py &nbsp;|&nbsp; {sys_info['timestamp']} &nbsp;|&nbsp;
    Python {sys_info['python_version']} &nbsp;|&nbsp; {sys_info['hostname']}
  </div>

</div>
<script>
function toggle(hdr) {{
  hdr.closest('.section').classList.toggle('collapsed');
}}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="win_diag.py",
        description="Windows system diagnostics, smart fixes, and optional HTML report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Profiles:
  readonly  Diagnostics only — no fixes run
  safe      Default — all fixes except winsock_reset and dism_restore
  full      All fixes including winsock_reset and dism_restore

Fix keys (for --skip / --only):
  {', '.join(sorted(ALL_FIX_KEYS))}

Examples:
  python win_diag.py
  python win_diag.py --profile full --report C:\\Users\\Andre\\Desktop\\report.html
  python win_diag.py --profile readonly --log C:\\Logs\\diag.log
  python win_diag.py --skip sfc dism_check
  python win_diag.py --only dns_flush services
  python win_diag.py --config C:\\tools\\myserver.toml
""",
    )
    parser.add_argument(
        "--report", "-r",
        metavar="PATH",
        default=None,
        help="Path for the HTML report. Overrides [report] path in TOML. If neither is set, no report is generated.",
    )
    parser.add_argument(
        "--log", "-l",
        metavar="PATH",
        default=None,
        help="Path for the log file. Overrides [log] path in TOML. Default: <script_dir>/log/win_diag_<timestamp>.log",
    )
    parser.add_argument(
        "--profile", "-p",
        metavar="PROFILE",
        default=None,
        choices=list(PROFILES),
        help=f"Fix profile: {', '.join(PROFILES)}. Overrides [profile] default in TOML.",
    )
    parser.add_argument(
        "--skip",
        metavar="FIX",
        nargs="+",
        default=None,
        help="Fix keys to skip, space-separated. Applied on top of --profile.",
    )
    parser.add_argument(
        "--only",
        metavar="FIX",
        nargs="+",
        default=None,
        help="Run only these fix keys, ignoring the profile entirely.",
    )
    parser.add_argument(
        "--config", "-c",
        metavar="PATH",
        default=None,
        help=(
            "Path to a TOML config file. "
            "Default: win_diag.toml next to the script (loaded if present)."
        ),
    )
    return parser.parse_args()


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

def run_fix_with_spinner(label: str, fix_fn, *args) -> dict:
    """Run a single fix function wrapped in a spinner, return the result dict."""
    sp = Spinner(label)
    sp.__enter__()
    result = fix_fn(*args)
    sp._stop.set()
    sp._thread.join()
    elapsed = time.monotonic() - sp._start
    if result["success"]:
        print(f"\r  \033[32m+\033[0m  {label:<50}  \033[2m{elapsed:.1f}s\033[0m")
    else:
        print(f"\r  \033[31mx\033[0m  {label:<50}  \033[2m{result['result'][:60]}\033[0m")
    return result


def main():
    global _logger, _cfg

    args = parse_args()

    if platform.system() != "Windows":
        print("This script is designed for Windows only.")
        sys.exit(1)

    # ── Load config ───────────────────────────────
    # Explicit --config > win_diag.toml next to script > no config
    if args.config:
        toml_path = Path(args.config)
    else:
        toml_path = Path(__file__).parent / "win_diag.toml"

    _cfg = load_config(
        toml_path    = toml_path if toml_path.exists() else None,
        cli_profile  = args.profile,
        cli_log      = args.log,
        cli_report   = args.report,
        cli_skip     = args.skip,
        cli_only     = args.only,
    )

    # ── Resolve paths ─────────────────────────────
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _cfg.log_path or (
        Path(__file__).parent / "log" / f"win_diag_{timestamp}.log"
    )
    report_path = _cfg.report_path

    _logger = DiagLogger(log_path)
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Header ────────────────────────────────────
    print("\n  Windows System Diagnostics")
    print("  " + "─" * 50)

    if not is_admin():
        _logger.info("WARNING: Not running as Administrator")
        print("  \033[33m!\033[0m  Not running as Administrator — some data and fixes may be limited.")
    else:
        _logger.info("Running as Administrator")
        print("  \033[32m+\033[0m  Running as Administrator")

    if _cfg.config_path:
        _logger.info(f"Config: {_cfg.config_path}")
        print(f"  \033[2mConfig : {_cfg.config_path}\033[0m")
    else:
        print(f"  \033[2mConfig : (none — using defaults)\033[0m")

    _logger.info(f"Config summary: {_cfg.summary()}")
    print(f"  \033[2mProfile: {_cfg.profile}  |  {_cfg.summary()}\033[0m")
    print(f"  \033[2mLog    : {log_path}\033[0m")
    if report_path:
        print(f"  \033[2mReport : {report_path}\033[0m")
    else:
        print(f"  \033[2mReport : (disabled — pass --report PATH or set [report] path in TOML)\033[0m")
    print()

    # ── Diagnostics ───────────────────────────────
    _logger.section("Diagnostics")
    print("  \033[2mDiagnostics\033[0m")

    with Spinner("System information"):
        sys_info = collect_system_info()

    with Spinner("Drives"):
        disks = collect_disk_info()

    with Spinner("Network"):
        net = collect_network_info()

    with Spinner("Services"):
        services = collect_windows_services()

    with Spinner("Event log (last 24h)"):
        events = collect_event_errors()

    with Spinner("Startup items"):
        startup = collect_startup_items()

    with Spinner("Windows Update"):
        updates = collect_updates()

    data = {
        "system":   sys_info,
        "disks":    disks,
        "network":  net,
        "services": services,
        "events":   events,
        "startup":  startup,
        "updates":  updates,
        "overall":  "",
        "fixes":    [],
        "profile":  _cfg.profile,
    }
    data["overall"] = compute_overall_status(data)
    _logger.info(f"Overall status: {data['overall'].upper()}")

    # ── Smart Fixes ───────────────────────────────
    _logger.section(f"Smart Fixes  [profile: {_cfg.profile}]")
    print()
    print(f"  \033[2mSmart Fixes  [{_cfg.profile}]\033[0m")

    fixes = []

    if _cfg.fix_enabled("sfc"):
        fixes.append(run_fix_with_spinner("SFC /scannow", fix_sfc))
    else:
        _logger.info("SKIP sfc (disabled by profile/config)")

    if _cfg.fix_enabled("dism_check"):
        fixes.append(run_fix_with_spinner("DISM /CheckHealth", fix_dism_check))
    else:
        _logger.info("SKIP dism_check (disabled by profile/config)")

    # dism_restore only if sfc ran and found unfixable corruption
    sfc_result = next((f for f in fixes if f["name"] == "SFC /scannow"), None)
    if (sfc_result and "unable to fix" in sfc_result["result"].lower()
            and _cfg.fix_enabled("dism_restore")):
        fixes.append(run_fix_with_spinner("DISM /RestoreHealth", fix_dism_restore))

    if _cfg.fix_enabled("dns_flush"):
        fixes.append(run_fix_with_spinner("DNS cache flush", fix_dns_flush))

    ping_failures = [p for p in data["network"]["pings"] if not p["ok"]]
    if ping_failures and _cfg.fix_enabled("winsock_reset"):
        fixes.append(run_fix_with_spinner("Winsock & TCP/IP reset", fix_winsock_reset))

    if _cfg.fix_enabled("services"):
        stopped = [
            s for s in data["services"]
            if not s["running"] and not s.get("ignored", False)
        ]
        for svc in stopped:
            fixes.append(run_fix_with_spinner(
                f"Start service: {svc['display']}", fix_restart_service,
                svc["name"], svc["display"],
            ))

    full_drives = [d for d in data["disks"] if d["status"] in ("warning", "critical")]
    if full_drives:
        if _cfg.fix_enabled("clear_temp"):
            fixes.append(run_fix_with_spinner("Clear TEMP folders", fix_clear_temp))
        if _cfg.fix_enabled("disk_cleanup"):
            letter = full_drives[0]["name"].rstrip(":")
            fixes.append(run_fix_with_spinner(f"Disk cleanup ({letter}:)", fix_disk_cleanup, letter))

    if not data["updates"]["service_running"] and _cfg.fix_enabled("windows_update"):
        fixes.append(run_fix_with_spinner("Restart Windows Update service", fix_restart_windows_update))

    data["fixes"] = fixes

    # ── Report (optional) ─────────────────────────
    print()
    if report_path:
        _logger.section("Report")
        print("  \033[2mReport\033[0m")
        with Spinner("Generating HTML report"):
            html = generate_html(data)
            report_path.write_text(html, encoding="utf-8")

        import webbrowser
        with Spinner("Opening report in browser"):
            webbrowser.open(report_path.as_uri())
            time.sleep(0.5)

    # ── Summary ───────────────────────────────────
    ok   = sum(1 for f in fixes if f["success"])
    fail = len(fixes) - ok
    overall_color = {"ok": "\033[32m", "warning": "\033[33m", "critical": "\033[31m"}[data["overall"]]
    overall_label = {"ok": "System healthy", "warning": "Warnings detected", "critical": "Critical issues found"}[data["overall"]]

    _logger.finalize(data["overall"], ok, fail, report_path)

    print("  " + "─" * 50)
    print(f"  {overall_color}{overall_label}\033[0m")
    print(f"  Fixes : \033[32m{ok} passed\033[0m  /  \033[31m{fail} failed\033[0m")
    print(f"  Log   : {log_path}")
    if report_path:
        print(f"  Report: {report_path}")

    if any("reset" in f.get("command", "").lower() and f["success"] for f in fixes):
        print("  \033[33m!\033[0m  Network stack was reset — reboot recommended.")
    print()


if __name__ == "__main__":
    main()