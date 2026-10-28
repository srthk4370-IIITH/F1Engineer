"""SQLite + compressed npz traces with retention (last N sessions + one PB reference lap per track).

All writes stay under data/ and are skipped when free disk drops below the guardrail.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(uid TEXT PRIMARY KEY, track TEXT, type TEXT, started REAL, fmt INTEGER);
CREATE TABLE IF NOT EXISTS laps(id INTEGER PRIMARY KEY AUTOINCREMENT, session_uid TEXT, car_idx INTEGER, car_name TEXT,
  lap_num INTEGER, lap_time REAL, valid INTEGER, input_device TEXT, assists TEXT, report TEXT, trace TEXT, created REAL);
CREATE TABLE IF NOT EXISTS radio(id INTEGER PRIMARY KEY AUTOINCREMENT, session_uid TEXT, t REAL, role TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS laps_session ON laps(session_uid);
CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY, mtime REAL);
"""


def disk_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def dir_size_gb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e9 if path.exists() else 0.0


class Storage:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = cfg.data_dir
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "sessions").mkdir(exist_ok=True)
        (self.root / "reference").mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "f1engineer.db", check_same_thread=False)
        self.db.executescript(SCHEMA)
        if "sectors" not in [r[1] for r in self.db.execute("PRAGMA table_info(laps)")]:
            self.db.execute("ALTER TABLE laps ADD COLUMN sectors TEXT")  # added after the first release
        self.session_clock: float | None = None  # importer: date sessions by the recording, not by now
        self.lock = threading.Lock()

    def can_write(self) -> bool:
        ok = disk_free_gb(self.root) > self.cfg.min_free_disk_gb
        if not ok:
            print("[storage] low disk space: skipping writes")
        return ok

    # --- sessions ---------------------------------------------------------
    def start_session(self, uid: int, track: str, stype: str, fmt: int) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?,?,?)",
                            (str(uid), track, stype, self.session_clock or time.time(), fmt))
            self.db.commit()
        self.retention()

    def retention(self) -> None:
        """Keep the newest `keep_sessions` sessions that have laps; drop the rest (db rows + traces)."""
        with self.lock:
            rows = self.db.execute(
                "SELECT s.uid FROM sessions s WHERE EXISTS(SELECT 1 FROM laps l WHERE l.session_uid=s.uid) "
                "ORDER BY s.started DESC").fetchall()
            empty = [] if self.session_clock else self.db.execute(  # an import is still filling its (back-dated) session
                "SELECT uid FROM sessions s WHERE NOT EXISTS(SELECT 1 FROM laps l WHERE l.session_uid=s.uid) "
                "AND started < ?", (time.time() - 3600,)).fetchall()
            old = [r[0] for r in rows[self.cfg.keep_sessions:]] + [r[0] for r in empty]
            for uid in old:
                self.db.execute("DELETE FROM laps WHERE session_uid=?", (uid,))
                self.db.execute("DELETE FROM radio WHERE session_uid=?", (uid,))
                self.db.execute("DELETE FROM sessions WHERE uid=?", (uid,))
                shutil.rmtree(self.root / "sessions" / uid, ignore_errors=True)
            self.db.commit()

    # --- laps -------------------------------------------------------------
    def save_lap(self, uid: int, lap, car_name: str, report: dict | None, input_device: str) -> int | None:
        if not self.can_write():
            return None
        with self.lock:  # same lap delivered twice (e.g. a replay of the same session): keep one
            if self.db.execute("SELECT 1 FROM laps WHERE session_uid=? AND car_idx=? AND lap_num=? AND abs(lap_time-?)<0.0005",
                               (str(uid), lap.car_idx, lap.lap_num, lap.lap_time)).fetchone():
                return None
        d = self.root / "sessions" / str(uid)
        d.mkdir(parents=True, exist_ok=True)
        trace = d / f"car{lap.car_idx}_lap{lap.lap_num}_{int(time.time() * 1000) % 10**8}.npz"
        np.savez_compressed(trace, **lap.arrays)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO laps(session_uid,car_idx,car_name,lap_num,lap_time,valid,input_device,assists,report,trace,created,"
                "sectors) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uid), lap.car_idx, car_name, lap.lap_num, lap.lap_time, int(not lap.invalid), input_device,
                 json.dumps(lap.assists), json.dumps(report, default=str) if report else None,
                 str(trace.relative_to(self.root)), self.session_clock or time.time(),
                 json.dumps([int(x) for x in lap.sector_ms]) if any(lap.sector_ms) else None))
            self.db.commit()
            return cur.lastrowid

    def log_radio(self, uid: int, role: str, text: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO radio(session_uid,t,role,text) VALUES(?,?,?,?)", (str(uid), time.time(), role, text))
            self.db.commit()

    def list_laps(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT l.id,l.session_uid,s.track,s.type,l.car_idx,l.car_name,l.lap_num,l.lap_time,l.valid,l.input_device,"
                "l.assists,l.created,l.sectors FROM laps l LEFT JOIN sessions s ON s.uid=l.session_uid "
                "ORDER BY s.started DESC, l.id DESC LIMIT 300").fetchall()
        keys = ("id", "session", "track", "type", "car_idx", "car", "lap", "lap_time", "valid", "input", "assists", "created",
                "sectors")
        out = [dict(zip(keys, r)) for r in rows]
        for d in out:
            d["sectors"] = json.loads(d["sectors"]) if d["sectors"] else None
        return out

    def was_imported(self, path: Path) -> bool:
        with self.lock:
            r = self.db.execute("SELECT mtime FROM imports WHERE path=?", (str(path.resolve()),)).fetchone()
        return bool(r) and abs(r[0] - path.stat().st_mtime) < 1

    def mark_imported(self, path: Path) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO imports VALUES(?,?)", (str(path.resolve()), path.stat().st_mtime))
            self.db.commit()

    def lap_detail(self, lap_id: int) -> dict | None:
        with self.lock:
            r = self.db.execute(
                "SELECT l.report, l.trace, l.lap_time, l.car_name, l.lap_num, s.track, l.session_uid, l.valid, l.input_device,"
                " l.assists FROM laps l LEFT JOIN sessions s ON s.uid=l.session_uid WHERE l.id=?", (lap_id,)).fetchone()
        if not r:
            return None
        return {"report": json.loads(r[0]) if r[0] else None, "trace": r[1], "lap_time": r[2], "car": r[3], "lap": r[4],
                "track": r[5], "session": r[6], "valid": bool(r[7]), "input": r[8],
                "assists": json.loads(r[9]) if r[9] else {}}

    def load_trace(self, rel: str) -> dict[str, np.ndarray]:
        with np.load(self.root / rel) as z:
            return {k: z[k] for k in z.files}

    def radio_log(self, limit: int = 100) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT t, role, text FROM radio ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"t": t, "role": ro, "text": tx} for t, ro, tx in rows][::-1]

    # --- personal-best reference per track ---------------------------------
    def reference(self, track: str) -> tuple[dict, float] | None:
        p = self.root / "reference" / f"{track}.npz"
        if not p.exists():
            return None
        with np.load(p) as z:
            arrays = {k: z[k] for k in z.files if k != "_lap_time"}
            return arrays, float(z["_lap_time"])

    def save_reference(self, track: str, arrays: dict, lap_time: float) -> None:
        if self.can_write():
            np.savez_compressed(self.root / "reference" / f"{track}.npz", _lap_time=np.float32(lap_time), **arrays)
