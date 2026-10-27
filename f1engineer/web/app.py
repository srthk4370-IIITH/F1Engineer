"""Local dashboard: live state over WebSocket, lap browser with trace overlays, chat with the engineer.

Binds to 127.0.0.1 only.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from f1engineer.analysis import lapmath as LM

STATIC = Path(__file__).parent / "static"


def create_app(engine) -> FastAPI:
    app = FastAPI(title="F1 Engineer", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    clients: set[WebSocket] = set()
    loop_holder: dict = {}

    def on_event(ev: dict):
        loop = loop_holder.get("loop")
        if loop and clients:
            msg = json.dumps({"event": ev}, default=str)
            for ws in list(clients):
                asyncio.run_coroutine_threadsafe(_send(ws, msg), loop)

    async def _send(ws, msg):
        try:
            await ws.send_text(msg)
        except Exception:
            clients.discard(ws)

    engine.listeners.append(on_event)

    @app.on_event("startup")
    async def _startup():
        loop_holder["loop"] = asyncio.get_running_loop()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        return engine.live_state()

    @app.get("/api/laps")
    def laps():
        return engine.store.list_laps()

    @app.get("/api/radio")
    def radio_log():
        return engine.store.radio_log()

    @app.get("/api/lap/{lap_id}")
    def lap(lap_id: int):
        d = engine.store.lap_detail(lap_id)
        if not d:
            raise HTTPException(404)
        arrays = engine.store.load_trace(d["trace"])
        L = float(arrays["dist"].max())
        L = engine.ref["track_length"] if engine.ref else (5 * round(L / 5))
        rs = LM.resample(arrays, L)
        if rs is None:
            rs = {k: v for k, v in arrays.items()}
        step = max(1, int(round(5 / LM.GRID_M)))  # ~5 m resolution is plenty for plots
        keep = ("dist", "t", "speed", "throttle", "brake", "gear", "steer", "x", "z")
        d["trace"] = {k: np.round(rs[k][::step], 3).tolist() for k in keep if k in rs}
        return d

    @app.post("/api/ask")
    async def ask(body: dict):
        q = (body or {}).get("q", "").strip()
        if not q:
            raise HTTPException(400, "empty question")
        return {"answer": await run_in_threadpool(engine.ask, q)}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        clients.add(sock)
        try:
            while True:
                await sock.send_text(json.dumps({"state": engine.live_state()}, default=str))
                await asyncio.sleep(0.2)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            clients.discard(sock)

    return app
