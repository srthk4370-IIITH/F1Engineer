"""Tiny always-on watcher: starts the engineer when an F1 game starts, stops it after the game closes.

Cost: one process scan every 3 s (~10 ms CPU), ~25 MB RAM. Restarts a crashed engine with backoff,
gives up after 5 crashes in 10 minutes.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import webbrowser

import psutil

from f1engineer.config import ROOT, Config
from f1engineer.guard import InstanceLock, lower_priority, setup_logging

GAME_RE = re.compile(r"^f1_?(20[1-3]\d|2\d)(_dx12)?\.exe$", re.I)  # F1_2020.exe, F1_2020_dx12.exe, F1_22.exe, F1_24.exe ...
GRACE_S = 45


def game_running() -> str | None:
    for p in psutil.process_iter(["name"]):
        n = p.info.get("name") or ""
        if GAME_RE.match(n):
            return n
    return None


def spawn_engine() -> subprocess.Popen:
    exe = sys.executable
    if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"  # no console window
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    return subprocess.Popen([exe, "-m", "f1engineer", "run", "--no-browser"], cwd=str(ROOT), creationflags=flags)


def main() -> None:
    cfg = Config()
    setup_logging(cfg.logs_dir, "watcher")
    lower_priority()
    lock = InstanceLock(cfg.data_dir / "watcher.lock")
    if not lock.acquire():
        print("[watcher] already running")
        return
    print("[watcher] started; waiting for an F1 game")
    proc: subprocess.Popen | None = None
    last_seen = 0.0
    crashes: list[float] = []
    opened = False
    try:
        while True:
            game = game_running()
            now = time.time()
            if game:
                last_seen = now
                if proc is None or proc.poll() is not None:
                    if proc is not None and proc.returncode not in (0, None):
                        crashes = [t for t in crashes if now - t < 600] + [now]
                        print(f"[watcher] engine exited with {proc.returncode} ({len(crashes)} crashes in 10 min)")
                        if len(crashes) >= 5:
                            print("[watcher] too many crashes; not restarting until the game restarts")
                            while game_running():
                                time.sleep(5)
                            crashes.clear()
                            proc = None
                            continue
                        time.sleep(min(60, 5 * 2 ** (len(crashes) - 1)))
                    print(f"[watcher] {game} detected: starting engineer")
                    proc = spawn_engine()
                    if not opened and os.getenv("F1E_OPEN_DASHBOARD", "1") == "1":
                        time.sleep(4)
                        webbrowser.open(f"http://127.0.0.1:{cfg.web_port}")
                        opened = True
            elif proc is not None and proc.poll() is None and now - last_seen > GRACE_S:
                print("[watcher] game closed: stopping engineer")
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                proc = None
                opened = False
            time.sleep(3)
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
        lock.release()


STARTUP_NAME = "F1 Engineer Watcher.lnk"


def startup_dir() -> str:
    return os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")


def autostart(enable: bool) -> str:
    """Add/remove a Startup-folder shortcut (per-user, no admin, trivially removable)."""
    link = os.path.join(startup_dir(), STARTUP_NAME)
    if not enable:
        if os.path.exists(link):
            os.remove(link)
        return f"removed {link}"
    exe = sys.executable
    if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');$s.TargetPath='{exe}';"
          f"$s.Arguments='-m f1engineer watch';$s.WorkingDirectory='{ROOT}';$s.WindowStyle=7;$s.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    return f"created {link}"


def start_detached() -> None:
    """Start the watcher now, detached from this terminal."""
    exe = sys.executable
    if exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        exe = exe[:-10] + "pythonw.exe"
    flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    subprocess.Popen([exe, "-m", "f1engineer", "watch"], cwd=str(ROOT), creationflags=flags, close_fds=True)
