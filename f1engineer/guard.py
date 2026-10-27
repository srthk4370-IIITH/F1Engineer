"""Guardrails so the engineer can never hurt the laptop or the game.

- runs below-normal CPU priority (game keeps priority)
- memory watchdog: exits cleanly if RSS (incl. children) exceeds the cap; the watcher restarts it
- disk watchdog: prunes old sessions when data/ grows past its cap; storage refuses writes under the free-space floor
- single instance lock
- rotating log files (no unbounded logs)
- only ever writes inside the project's data/, logs/, models/ folders
"""
from __future__ import annotations

import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psutil

from f1engineer.storage import dir_size_gb, disk_free_gb


class _Tee:
    """Line-buffered stdout/stderr -> rotating log file (and the console when there is one)."""

    def __init__(self, handler: RotatingFileHandler, stream):
        self.h, self.stream = handler, stream
        self.buf = ""
        self.lock = threading.Lock()

    def write(self, s):
        with self.lock:
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                if line.strip():
                    try:
                        self.h.stream.write(time.strftime("%H:%M:%S ") + line.rstrip() + "\n")
                        self.h.stream.flush()
                        if self.h.shouldRollover(None):
                            self.h.doRollover()
                    except Exception:
                        pass
        if self.stream:
            try:
                self.stream.write(s)
            except Exception:
                pass
        return len(s)

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("tee has no fileno")

    def flush(self):
        if self.stream:
            try:
                self.stream.flush()
            except Exception:
                pass


def setup_logging(logs_dir: Path, name: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(logs_dir / f"{name}.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.shouldRollover = lambda rec: h.stream.tell() > h.maxBytes  # type: ignore[assignment]
    sys.stdout = _Tee(h, sys.stdout if sys.stdout and sys.stdout.isatty() else None)
    sys.stderr = _Tee(h, sys.stderr if sys.stderr and sys.stderr.isatty() else None)


def lower_priority() -> None:
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)
    except Exception:
        pass


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path

    def acquire(self) -> bool:
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip())
                if psutil.pid_exists(pid) and pid != os.getpid():
                    p = psutil.Process(pid)
                    if "python" in p.name().lower():
                        return False
            except Exception:
                pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(str(os.getpid()))
        return True

    def release(self) -> None:
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except Exception:
            pass


def start_watchdogs(cfg, storage, on_fatal) -> None:
    def loop():
        me = psutil.Process()
        n = 0
        while True:
            time.sleep(5)
            n += 1
            try:
                rss = me.memory_info().rss + sum(c.memory_info().rss for c in me.children(recursive=True))
                if rss / 1e6 > cfg.max_rss_mb:
                    print(f"[guard] memory {rss / 1e6:.0f} MB > cap {cfg.max_rss_mb} MB: shutting down")
                    on_fatal("memory cap")
                    return
                if n % 12 == 0:  # every minute
                    if dir_size_gb(cfg.data_dir) > cfg.max_data_gb:
                        print("[guard] data/ over cap: pruning to the newest session")
                        keep = cfg.keep_sessions
                        cfg.keep_sessions = 1
                        storage.retention()
                        cfg.keep_sessions = keep
                    if disk_free_gb(cfg.data_dir) < cfg.min_free_disk_gb:
                        print(f"[guard] free disk below {cfg.min_free_disk_gb} GB: writes paused")
            except Exception as e:
                print(f"[guard] watchdog error {e!r}")

    threading.Thread(target=loop, name="guard", daemon=True).start()
