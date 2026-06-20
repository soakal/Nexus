"""
NEXUS System Tray

Usage:
  pythonw.exe tray.py                   — normal launch
  python.exe  tray.py --install-startup  — write Run registry key
  python.exe  tray.py --uninstall-startup — remove Run registry key
"""
import json
import logging
import logging.handlers
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import winreg

import pystray
from PIL import Image, ImageDraw

# ── constants ─────────────────────────────────────────────────────────────────

NEXUS_DIR      = os.path.dirname(os.path.abspath(__file__))
DASHBOARD_URL  = "http://localhost:3000"
HEALTH_URL     = "http://127.0.0.1:8000/api/health"
ASSETS_DIR     = os.path.join(NEXUS_DIR, "assets")
LOG_PATH       = os.path.join(NEXUS_DIR, "logs", "tray.log")
VBS_PATH       = os.path.join(NEXUS_DIR, "launch_tray.vbs")
SINGLETON_PORT = 57890
RUN_KEY        = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE      = "NEXUS_Tray"

PALETTE = {
    "running": {
        "bg": (8, 14, 28), "border": (0, 212, 255),
        "letter": (0, 212, 255), "dot": (0, 230, 120),
    },
    "starting": {
        "bg": (8, 14, 28), "border": (255, 160, 0),
        "letter": (255, 160, 0), "dot": (255, 160, 0),
    },
    "stopped": {
        "bg": (18, 20, 32), "border": (55, 60, 82),
        "letter": (55, 60, 82), "dot": (55, 60, 82),
    },
}

# ── logging ───────────────────────────────────────────────────────────────────

os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("nexus.tray")

# ── single-instance guard (socket bind) ──────────────────────────────────────

_singleton_sock = None  # kept alive for process lifetime


def acquire_single_instance():
    global _singleton_sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLETON_PORT))
    except OSError:
        sock.close()
        log.info("Another instance already running — exiting (pid %d)", os.getpid())
        return None
    sock.listen(1)
    _singleton_sock = sock
    return sock


def _kill_other_tray_instances():
    """Kill any orphaned pythonw.exe processes running tray.py."""
    my_pid = os.getpid()

    # Get parent PID so we never kill our own venv launcher stub
    parent_pid = 0
    try:
        pr = subprocess.run(
            ["powershell", "-NonInteractive", "-Command",
             f"(Get-WmiObject Win32_Process -Filter 'ProcessId={my_pid}').ParentProcessId"],
            capture_output=True, text=True, timeout=5,
        )
        parent_pid = int(pr.stdout.strip()) if pr.returncode == 0 else 0
    except Exception:
        pass

    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command",
         "Get-WmiObject Win32_Process | Where-Object { "
         "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') "
         "-and $_.CommandLine -like '*tray.py*' "
         "} | Select-Object ProcessId | ConvertTo-Json"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        try:
            data = json.loads(result.stdout.strip())
            if isinstance(data, dict):
                data = [data]
            for item in data:
                pid = int(item.get("ProcessId", 0))
                if pid and pid != my_pid and pid != parent_pid:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True)
                    log.info("Killed orphaned tray instance pid %d", pid)
        except Exception:
            log.exception("Error killing orphaned tray instances")

# ── icon rendering ────────────────────────────────────────────────────────────

def _hex_pts(cx, cy, r):
    return [
        (cx + r * math.cos(math.radians(a + 90)),
         cy + r * math.sin(math.radians(a + 90)))
        for a in range(0, 360, 60)
    ]


def _render_size(status, s):
    p   = PALETTE[status]
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    cx  = cy = s // 2
    R   = int(s * 0.45)

    d.polygon(_hex_pts(cx, cy, R), fill=p["bg"])

    bw = max(1, s // 20)
    for r in range(R, R - bw - 1, -1):
        d.polygon(_hex_pts(cx, cy, r), fill=None, outline=p["border"])

    lw  = max(1, s // 12)
    pad = int(s * 0.24)
    x0, y0 = cx - pad, cy - pad
    x1, y1 = cx - pad, cy + pad
    x2, y2 = cx + pad, cy - pad
    x3, y3 = cx + pad, cy + pad
    d.line([(x0, y0), (x1, y1)], fill=p["letter"], width=lw)
    d.line([(x0, y0), (x3, y3)], fill=p["letter"], width=lw)
    d.line([(x2, y2), (x3, y3)], fill=p["letter"], width=lw)

    if s >= 16:
        dr  = max(2, s // 10)
        dcx = cx + int(R * 0.68)
        dcy = cy + int(R * 0.68)
        d.ellipse([dcx - dr, dcy - dr, dcx + dr, dcy + dr], fill=p["dot"])

    return img


def _build_ico(status):
    os.makedirs(ASSETS_DIR, exist_ok=True)
    sizes  = [16, 20, 24, 32, 48, 64]
    frames = [_render_size(status, s) for s in sizes]
    path   = os.path.join(ASSETS_DIR, f"tray_{status}.ico")
    frames[0].save(
        path, format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    return Image.open(path)


_ICONS = {}


def _preload_icons():
    for s in ("running", "starting", "stopped"):
        try:
            _ICONS[s] = _build_ico(s)
        except Exception:
            log.exception("Failed to build icon for status=%s", s)
            _ICONS[s] = _render_size(s, 64)

# ── health check — BOTH backend (8000) AND frontend (3000) must be up ────────

def _backend_healthy():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=12) as r:
            data = json.loads(r.read())
            if data.get("status") not in ("ok", "vault_empty"):
                return False
    except Exception:
        return False
    try:
        with socket.create_connection(("127.0.0.1", 3000), timeout=1):
            return True
    except OSError:
        return False

# ── startup registry ──────────────────────────────────────────────────────────

def install_startup():
    cmd = f'wscript.exe //B "{VBS_PATH}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, cmd)
    log.info("Startup registry key installed: %s", cmd)
    print("Installed:", cmd)


def uninstall_startup():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_VALUE)
        log.info("Startup registry key removed.")
        print("Removed NEXUS_Tray from startup.")
    except FileNotFoundError:
        print("Not installed.")

# ── tray application ──────────────────────────────────────────────────────────

class NexusTray:
    def __init__(self):
        self._lock   = threading.Lock()
        self._status = "stopped"
        self._icon   = None

    def _set_status(self, status):
        with self._lock:
            if self._status == status:
                return
            self._status = status
        log.info("Status -> %s", status)
        if self._icon:
            self._icon.icon  = _ICONS.get(status, _render_size(status, 64))
            self._icon.title = f"NEXUS  ·  {status.capitalize()}"
            self._icon.update_menu()

    # ── subprocess helpers ────────────────────────────────────────────────────

    def _run_ps(self, script_name):
        """Run a PowerShell script and capture *its own* output for logging.

        Output is redirected to a temp FILE, never to an anonymous pipe.
        start.ps1 launches uvicorn/vite via Start-Process; those grandchildren
        inherit whatever stdio handles PowerShell has. If we captured via OS
        pipes (capture_output=True / stdout=PIPE), the pipe write-end leaks to
        the grandchildren, they hold it open for their whole lifetime, and the
        parent's read on that pipe never sees EOF -> subprocess.run() hangs
        forever (or until communicate()'s timeout). Redirecting to a real file
        has no reader to block, so run() returns the moment PowerShell exits,
        regardless of what the long-lived grandchildren inherited.
        """
        path = os.path.join(NEXUS_DIR, script_name)
        stdout_text = ""
        # delete=False so we can reopen/read after the child closes it (Windows
        # forbids a second open on a delete-on-close temp file).
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", prefix=f"{script_name}.",
            dir=os.path.join(NEXUS_DIR, "logs"), delete=False, encoding="utf-8",
        )
        try:
            with tmp:
                result = subprocess.run(
                    ["powershell", "-WindowStyle", "Hidden",
                     "-NonInteractive", "-File", path],
                    cwd=NEXUS_DIR,
                    stdin=subprocess.DEVNULL,
                    stdout=tmp,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                )
            try:
                with open(tmp.name, "r", encoding="utf-8", errors="replace") as fh:
                    stdout_text = fh.read()
            except OSError:
                pass

            if result.returncode != 0:
                log.warning("%s exit %d\noutput: %s",
                            script_name, result.returncode, stdout_text[-1600:])
            else:
                log.info("%s completed (exit 0)", script_name)
            return result
        except subprocess.TimeoutExpired:
            log.error("%s timed out after 180s\noutput: %s",
                      script_name, stdout_text[-1600:])
            raise
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # ── actions ───────────────────────────────────────────────────────────────

    def _open_dashboard(self, *_):
        webbrowser.open(DASHBOARD_URL)

    def _start(self, *_):
        with self._lock:
            if self._status != "stopped":
                return
        threading.Thread(target=self._do_start, daemon=True).start()

    def _stop(self, *_):
        with self._lock:
            if self._status != "running":
                return
        threading.Thread(target=self._do_stop, daemon=True).start()

    def _restart(self, *_):
        threading.Thread(target=self._do_restart, daemon=True).start()

    def _quit_only(self, *_):
        log.info("Quit — leaving NEXUS running")
        self._icon.stop()

    def _quit_and_stop(self, *_):
        log.info("Quit & Stop NEXUS")
        self._run_ps("stop.ps1")
        self._icon.stop()

    # ── background work ───────────────────────────────────────────────────────

    def _do_start(self, open_browser: bool = False):
        self._set_status("starting")
        result = self._run_ps("start.ps1")
        # Trust start.ps1's own health verification rather than re-checking
        # immediately (backend can be slow right after start.ps1 confirms it).
        if result.returncode == 0:
            self._set_status("running")
            if open_browser:
                webbrowser.open(DASHBOARD_URL)
        else:
            self._set_status("stopped")

    def _do_stop(self):
        self._set_status("starting")
        self._run_ps("stop.ps1")
        self._set_status("stopped")

    def _do_restart(self):
        self._do_stop()
        time.sleep(1)
        self._do_start()

    def _monitor(self):
        fail_count = 0
        while True:
            time.sleep(15)
            with self._lock:
                if self._status == "starting":
                    fail_count = 0
                    continue
            if _backend_healthy():
                fail_count = 0
                self._set_status("running")
            else:
                fail_count += 1
                if fail_count >= 2:
                    self._set_status("stopped")

    # ── setup callback: fires once pystray icon loop is live ─────────────────

    def _on_setup(self, icon):
        icon.visible = True
        if _backend_healthy():
            log.info("NEXUS already fully running — skipping auto-start")
        else:
            log.info("Auto-starting NEXUS on tray launch")
            # open_browser=True only on this cold-boot path so we get one tab at login
            threading.Thread(target=self._do_start, kwargs={"open_browser": True}, daemon=True).start()

    # ── menu ─────────────────────────────────────────────────────────────────

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem(
                "Open Dashboard", self._open_dashboard,
                default=True,
                enabled=lambda _: self._status == "running",
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start",   self._start,
                             enabled=lambda _: self._status == "stopped"),
            pystray.MenuItem("Restart", self._restart,
                             enabled=lambda _: self._status == "running"),
            pystray.MenuItem("Stop",    self._stop,
                             enabled=lambda _: self._status == "running"),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", pystray.Menu(
                pystray.MenuItem("Quit (leave NEXUS running)", self._quit_only),
                pystray.MenuItem("Quit && Stop NEXUS",         self._quit_and_stop),
            )),
        )

    # ── entry point ───────────────────────────────────────────────────────────

    def run(self):
        os.chdir(NEXUS_DIR)
        initial = "running" if _backend_healthy() else "stopped"
        self._status = initial
        log.info("Tray started (pid %d), initial status: %s", os.getpid(), initial)

        self._icon = pystray.Icon(
            "nexus",
            _ICONS.get(initial, _render_size(initial, 64)),
            f"NEXUS  ·  {initial.capitalize()}",
            menu=self._menu(),
        )

        threading.Thread(target=self._monitor, daemon=True).start()
        self._icon.run(setup=self._on_setup)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if "--install-startup" in sys.argv:
        install_startup()
        return
    if "--uninstall-startup" in sys.argv:
        uninstall_startup()
        return

    # Kill any orphaned old tray instances before claiming the singleton
    _kill_other_tray_instances()

    lock = acquire_single_instance()
    if lock is None:
        sys.exit(0)

    _preload_icons()

    try:
        NexusTray().run()
    except Exception:
        log.exception("Fatal error in tray")
        sys.exit(1)


if __name__ == "__main__":
    main()
