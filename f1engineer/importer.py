"""Import recorded .f1cap sessions into the lap database (what the dashboard shows).

Imported sessions are dated by the recording's file time, so the "keep the last N sessions" retention
treats them in true order. Each file is imported once (tracked in the `imports` table).
"""
from __future__ import annotations

from pathlib import Path


def import_capture(cfg, store, path: Path, log=print) -> int:
    from f1engineer.engine import Engine
    from f1engineer.llm.agent import MockProvider
    from f1engineer.telemetry.capture import read_capture
    before = len(store.list_laps())
    eng = Engine(cfg, storage=store, provider=MockProvider())
    eng._say = lambda *a, **k: None          # analysis only: no radio lines in the log
    eng.sched.say = lambda *a, **k: None
    store.session_clock = path.stat().st_mtime
    try:
        for _, d in read_capture(path):
            eng.feed(d)
        eng.drain()
    finally:
        store.session_clock = None
    store.mark_imported(path)
    store.retention()
    n = len(store.list_laps()) - before
    log(f"[import] {path.name}: {max(n, 0)} laps")
    return n


def auto_import(cfg, store, newest: int = 2, log=print, status: dict | None = None) -> None:
    """Import the newest recordings that were not imported yet (forged/simulated captures are skipped)."""
    from f1engineer import brain
    brain.backfill(store)   # laps stored before long-term summaries existed
    store.prune_history()
    caps = sorted((p for p in (cfg.data_dir / "captures").glob("*.f1cap") if not p.name.startswith("forged")),
                  key=lambda p: p.stat().st_mtime)[-newest:]
    for p in caps:
        if store.was_imported(p):
            continue
        if status is not None:
            status["importing"] = p.name
        try:
            import_capture(cfg, store, p, log)
        except Exception as e:
            log(f"[import] {p.name} failed: {e!r}")
    if status is not None:
        status["importing"] = None
