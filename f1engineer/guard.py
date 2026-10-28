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
from pathlib import Path

import psutil

from f1engineer.storage import dir_size_gb, disk_free_gb


class _Tee:
    """Line-buffered stdout/stderr -> size-rotated log file (and the console when there is one).

    Owns its file directly: a logging.Handler would be closed by uvicorn's logging.config.dictConfig().
    """

    MAX_BYTES, BACKUPS = 2_000_000, 3

    def __init__(self, path: Path, stream, lock: threading.Lock):
        self.path, self.stream, self.lock = path, stream, lock
        self.buf = ""

    def _rotate(self) -> None:
        for i in range(self.BACKUPS - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def write(self, s):
        with self.lock:
            self.buf += s
            if "\n" in self.buf:
                *lines, self.buf = self.buf.split("\n")
                stamp = time.strftime("%H:%M:%S ")
                text = "".join(stamp + ln.rstrip() + "\n" for ln in lines if ln.strip())
                if text:
                    try:
                        with open(self.path, "a", encoding="utf-8") as f:
                            f.write(text)
                        if self.path.stat().st_size > self.MAX_BYTES:
                            self._rotate()
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
    path, lock = logs_dir / f"{name}.log", threading.Lock()
    sys.stdout = _Tee(path, sys.stdout if sys.stdout and sys.stdout.isatty() else None, lock)
    sys.stderr = _Tee(path, sys.stderr if sys.stderr and sys.stderr.isatty() else None, lock)


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

    def held_by_other(self) -> bool:
        try:
            pid = int(self.path.read_text().strip())
            return pid != os.getpid() and psutil.pid_exists(pid) and "python" in psutil.Process(pid).name().lower()
        except Exception:
            return False

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
                    if n % 4320 == 0:  # every 6 h: age limits (history, radio log, recordings)
                        storage.prune_history()
                    if dir_size_gb(cfg.data_dir) > cfg.max_data_gb:
                        # biggest first: raw recordings, then detailed traces of older sessions
                        print(f"[guard] data/ over {cfg.max_data_gb} GB: pruning recordings, then older sessions")
                        for keep_caps in (1, 0):
                            storage.prune_history(keep_captures=keep_caps)
                            if dir_size_gb(cfg.data_dir) <= cfg.max_data_gb:
                                break
                        if dir_size_gb(cfg.data_dir) > cfg.max_data_gb:
                            keep = cfg.keep_sessions
                            cfg.keep_sessions = 1
                            storage.retention()
                            cfg.keep_sessions = keep
                    if disk_free_gb(cfg.data_dir) < cfg.min_free_disk_gb:
                        print(f"[guard] free disk below {cfg.min_free_disk_gb} GB: writes paused")
            except Exception as e:
                print(f"[guard] watchdog error {e!r}")

    threading.Thread(target=loop, name="guard", daemon=True).start()
