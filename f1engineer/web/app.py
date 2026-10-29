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

from f1engineer.analysis import coach
from f1engineer.analysis import lapmath as LM

STATIC = Path(__file__).parent / "static"
TRACE_KEYS = ("dist", "t", "speed", "throttle", "brake", "gear", "rpm", "steer", "drs", "x", "z", "glat", "glon")


class ReviewOnly:
    """Stand-in engine for `f1engineer dashboard`: stored laps only, no telemetry, no radio - but the engineer is
    there to talk to, with the whole history (progress, style, stored laps, strategy maths, memory)."""

    def __init__(self, store, sass: int = 2):
        from f1engineer import brain
        from f1engineer.llm import knowledge
        from f1engineer.llm.agent import Agent
        self.store = store
        self.ref = None
        self.listeners: list = []
        self.status: dict = {"importing": None}
        self.profile = brain.DriverProfile(store.root / "driver_profile.json")
        tools = {**brain.brain_tools(store, self.profile),
                 "search_knowledge": lambda query="": {"results": knowledge.search(query)}}
        self.agent = Agent(tools, self._context, sass=sass, profile_fn=self.profile.summary)

    def live_state(self) -> dict:
        return {"connected": False, "offline": True, "importing": self.status.get("importing")}

    def _context(self) -> str:
        laps = self.store.list_laps()[:12]
        recent = ", ".join(f"{l['track']} L{l['lap']} {l['lap_time']:.3f}{'' if l['valid'] else ' invalid'}"
                           for l in laps if l["car"] == "Player")
        return f"driver_state=away from the game, reviewing on the dashboard; recent stored laps: {recent or 'none'}"

    def ask(self, q: str) -> str:
        return " ".join(self.agent.ask(q, mode="review"))


def create_app(engine) -> FastAPI:
    app = FastAPI(title="F1 Engineer", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def no_cache(request, call_next):  # dashboard updates must show up without a hard refresh
        resp = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp
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

    @app.get("/api/track")
    def track():
        return engine.track_outline() if hasattr(engine, "track_outline") else {"x": [], "z": []}

    @app.get("/api/laps")
    def laps():
        return engine.store.list_laps()

    @app.get("/api/memory")
    def memory():
        return engine.profile.data

    @app.delete("/api/memory")
    def forget(what: str):
        return engine.profile.forget(what)

    @app.get("/api/race/{session}")
    def race(session: str):
        return engine.store.race_log(session)

    @app.get("/api/radio")
    def radio_log():
        return engine.store.radio_log()

    @app.get("/api/lap/{lap_id}")
    async def lap(lap_id: int):
        d = engine.store.lap_detail(lap_id)
        if not d:
            raise HTTPException(404)
        return await run_in_threadpool(_lap_payload, d)

    def _lap_payload(d: dict) -> dict:
        arrays = engine.store.load_trace(d["trace"])
        L = float(arrays["dist"].max())
        L = engine.ref["track_length"] if engine.ref else (5 * round(L / 5))
        rs = LM.resample(arrays, L)
        if rs is None:
            rs = {k: v for k, v in arrays.items()}
            d["corners"] = []
        else:
            d["corners"] = [{"num": c.num, "name": c.name, "apex": round(c.apex), "start": round(c.start), "end": round(c.end)}
                            for c in LM.detect_corners(rs, coach.corner_names(d.get("track") or ""))]
        if "slip_fl" in rs:  # worst front / rear wheel slip: lock-ups (negative) and wheelspin (positive)
            rs["slip_f"] = np.where(np.abs(rs["slip_fl"]) > np.abs(rs["slip_fr"]), rs["slip_fl"], rs["slip_fr"])
            rs["slip_r"] = np.where(np.abs(rs["slip_rl"]) > np.abs(rs["slip_rr"]), rs["slip_rl"], rs["slip_rr"])
        step = max(1, int(round(4 / LM.GRID_M)))  # ~4 m resolution is plenty for plots
        d["trace"] = {k: np.round(rs[k][::step], 3).tolist() for k in (*TRACE_KEYS, "slip_f", "slip_r") if k in rs}
        d["track_length"] = L
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
