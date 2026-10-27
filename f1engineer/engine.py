"""The engineer's core: UDP ingest -> tracker -> analysis -> radio/LLM -> storage + live state for the dashboard."""
from __future__ import annotations

import queue
import socket
import threading
import time
import traceback

import numpy as np

from f1engineer import radio
from f1engineer.analysis import coach, lapmath as LM, setup_advisor, strategy
from f1engineer.config import Config
from f1engineer.llm import knowledge
from f1engineer.llm.agent import Agent
from f1engineer.state.tracker import SessionTracker
from f1engineer.storage import Storage
from f1engineer.telemetry import packets_2020 as P

PARSERS = {2020: P.parse}


class NullSpeech:
    """Prints instead of speaking (no audio stack / tests)."""

    def say(self, text: str, priority: int = 5) -> None:
        print(f"[radio] {text}")

    def ptt(self, down: bool) -> None:
        pass

    def stop(self) -> None:
        pass


class Engine:
    def __init__(self, cfg: Config | None = None, speech=None, storage: Storage | None = None, provider=None):
        self.cfg = cfg or Config()
        self.speech = speech or NullSpeech()
        self.store = storage or Storage(self.cfg)
        self.tr = SessionTracker()
        self.gaps = strategy.GapTimer()
        self.tyres = strategy.TyreModel()
        self.sched = radio.RadioScheduler(self._say)
        self.agent = Agent(self._tools(), self._context, sass=self.cfg.sass, provider=provider)
        self.reports: list[coach.LapReport] = []
        self.ref: dict | None = None          # {"rs": resampled, "time": float, "label": str, "brake_starts": [...]}
        self.corners: list[LM.Corner] = []
        self.advice: list[dict] = []
        self.race: dict = {}
        self.last_calls: set[str] = set()
        self.input_device = "unknown"
        self.unsupported_formats: set[int] = set()
        self.stats = {"packets": 0, "parse_errors": 0, "last_lap_analysis_ms": 0.0}
        self._bg: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._last_buttons = 0
        self._ptt_mask = next((k for k, v in P.BUTTONS.items() if v == self.cfg.ptt_game_button), 0)
        self.listeners: list = []  # callables(event: dict) for the dashboard websocket
        tr = self.tr
        tr.on("session_start", self._on_session)
        tr.on("lap_complete", self._on_lap)
        tr.on("lap_aborted", lambda lap, why: self._emit({"type": "lap_aborted", "lap": lap.lap_num, "reason": why}))
        tr.on("sc_change", self._on_sc)
        tr.on("flashback", lambda t: self.sched.clear())

    # ---- ingest ------------------------------------------------------------------
    def feed(self, data: bytes, wall: float | None = None) -> None:
        self.stats["packets"] += 1
        if len(data) < 24:
            return
        fmt = int.from_bytes(data[0:2], "little")
        parse = PARSERS.get(fmt)
        if parse is None:
            if fmt not in self.unsupported_formats:
                self.unsupported_formats.add(fmt)
                print(f"[engine] packet format {fmt} not supported yet (set the game's UDP format to 2020 if available)")
            return
        r = parse(data)
        if r is None:
            self.stats["parse_errors"] += 1
            return
        pid, p = r
        try:
            self.tr.on_packet(pid, p, wall)
            if pid == 2 and self.tr.lapdata is not None:
                self.gaps.update(self.tr.lapdata, self.tr.session_time)
            elif pid == 6:
                self._buttons(self.tr.buttons)
        except Exception:
            traceback.print_exc()

    def bind_udp(self, retries: int = 10) -> socket.socket:
        """Bind the telemetry port (retrying while a previous instance releases it). Raises if still busy."""
        for i in range(retries):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            try:
                sock.bind((self.cfg.udp_host, self.cfg.udp_port))
                sock.settimeout(0.5)
                print(f"[engine] listening for F1 telemetry on {self.cfg.udp_host}:{self.cfg.udp_port}")
                return sock
            except OSError:
                sock.close()
                if i == retries - 1:
                    raise RuntimeError(f"UDP port {self.cfg.udp_port} is busy (another telemetry app running?)")
                time.sleep(1)

    def run_udp(self, sock: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                data = sock.recv(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            self.feed(data)
        sock.close()

    def start(self) -> None:
        sock = self.bind_udp()
        threading.Thread(target=self.run_udp, args=(sock,), name="udp", daemon=True).start()
        threading.Thread(target=self._bg_worker, name="bg", daemon=True).start()
        threading.Thread(target=self._tick_loop, name="tick", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # ---- events --------------------------------------------------------------------
    def _emit(self, ev: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                pass

    def _on_session(self, tr) -> None:
        self.reports.clear()
        self.advice = []
        self.corners = []
        self.ref = None
        self.gaps = strategy.GapTimer()
        self.tyres = strategy.TyreModel()
        self.sched.clear()
        self.agent.history.clear()
        self._bg_put(self.store.start_session, tr.session_uid, tr.track, tr.session_type, tr.packet_format)
        # stored personal best becomes the initial reference
        stored = self.store.reference(tr.track)
        if stored:
            self._set_reference(stored[0], stored[1], "your stored best")
        self._emit({"type": "session", "track": tr.track, "mode": tr.session_type})
        greeting = f"Radio check. {tr.track}, {tr.session_type}. "
        greeting += "Give me a clean lap and I'll tell you where the time is." if tr.is_time_trial else "Let's get to work."
        self._say(greeting, 3)

    def _set_reference(self, arrays: dict, lap_time: float, label: str) -> None:
        L = float(self.tr.session.get("trackLength") or 0) or float(arrays["dist"].max())
        rs = LM.resample(arrays, L)
        if rs is None:
            return
        if not self.corners:
            self.corners = LM.detect_corners(rs, coach.corner_names(self.tr.track))
        brake_starts = []
        for c in self.corners:
            s, a = int(c.start / LM.GRID_M), int(c.apex / LM.GRID_M)
            bp, _ = LM._phase_points(rs, s, a, a)
            brake_starts.append((bp if bp is not None else a) * LM.GRID_M)
        self.ref = {"rs": rs, "time": lap_time, "label": label, "brake_starts": brake_starts, "track_length": L}

    def _on_lap(self, lap) -> None:
        tr = self.tr
        if lap.car_idx != tr.player_idx:
            # ghosts / AI: candidate references in TT, stored for the dashboard
            if lap.complete and tr.is_time_trial and (self.ref is None or lap.lap_time < self.ref["time"]):
                self._set_reference(lap.arrays, lap.lap_time, tr.car_name(lap.car_idx).lower() + " ghost")
            if lap.complete and (tr.is_time_trial or lap.car_idx < 22):
                self._bg_put(self.store.save_lap, tr.session_uid, lap, tr.car_name(lap.car_idx), None, "")
            return
        t0 = time.perf_counter()
        self.input_device = coach.detect_input_device(lap.arrays) if lap.n() > 100 else self.input_device
        rep = None
        if lap.complete:
            if self.ref is None:
                self._set_reference(lap.arrays, lap.lap_time, "your best lap")
                if not tr.is_race:
                    self._say(f"Lap {radio.fmt_t(lap.lap_time)}. That's the banker, now we have a reference.", 3)
            else:
                rs = LM.resample(lap.arrays, self.ref["track_length"])
                if rs is not None:
                    rep = coach.build_report(lap, rs, self.ref["rs"], self.ref["label"], self.ref["time"], self.corners)
                    self.reports.append(rep)
                    del self.reports[:-20]
                    self.advice = setup_advisor.advise(self.reports[-5:], tr.setup, self.input_device, lap.assists)
                    is_best = lap.lap_time < self.ref["time"] and (not lap.invalid or "best" in self.ref["label"])
                    if tr.is_time_trial or not tr.is_race:
                        self.sched.push(radio.tt_lap_messages(rep, self.cfg.sass, is_best))
                        self.sched.push([radio.Message(6, radio.CLOCK(), radio.lap_debrief_text(rep, self.advice),
                                                       "garage", 900, "garage_debrief")])
            # new personal best (valid laps only, or any lap if we have nothing valid yet)
            best_stored = self.store.reference(tr.track)
            if not lap.invalid and (best_stored is None or lap.lap_time < best_stored[1]):
                self._bg_put(self.store.save_reference, tr.track, lap.arrays, lap.lap_time)
            if self.ref and lap.lap_time < self.ref["time"] and (not lap.invalid or self.ref["label"] == "your best lap"):
                self._set_reference(lap.arrays, lap.lap_time, "your best lap")
        if tr.is_race:
            st = tr.status[lap.car_idx] if tr.status else {}
            self.tyres.on_lap(lap.lap_num, st)
            self.race = strategy.race_snapshot(tr, self.gaps, self.tyres)
            self.sched.push([radio.race_lap_message(self.race, lap.lap_time)])
        self.stats["last_lap_analysis_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._bg_put(self.store.save_lap, tr.session_uid, lap, tr.car_name(lap.car_idx),
                     rep.to_dict() if rep else None, self.input_device)
        self._emit({"type": "lap", "lap": lap.lap_num, "time": lap.lap_time, "valid": not lap.invalid,
                    "report": rep.to_dict() if rep else None})

    def _on_sc(self, status: str) -> None:
        if status in ("full", "virtual"):
            msg = "Safety car, safety car." if status == "full" else "VSC deployed. Hold the delta."
            self.sched.push([radio.Message(0, radio.CLOCK(), msg, "urgent", 10, "sc")])
        else:
            self.sched.push([radio.Message(0, radio.CLOCK(), "Safety car ending, get ready.", "urgent", 10, "sc")])

    # ---- radio / PTT ---------------------------------------------------------------
    def _say(self, text: str, priority: int = 5) -> None:
        self.speech.say(text, priority)
        self._emit({"type": "radio", "role": "engineer", "text": text})
        self._bg_put(self.store.log_radio, self.tr.session_uid, "engineer", text)

    def _buttons(self, b: int) -> None:
        if not self._ptt_mask:
            return
        down, was = bool(b & self._ptt_mask), bool(self._last_buttons & self._ptt_mask)
        if down != was:
            self.speech.ptt(down)
        self._last_buttons = b

    def ask(self, question: str) -> str:
        """Driver question (from STT or dashboard chat). Speaks sentences as they stream in."""
        self._emit({"type": "radio", "role": "driver", "text": question})
        self._bg_put(self.store.log_radio, self.tr.session_uid, "driver", question)
        out = []
        for s in self.agent.ask(question):
            out.append(s)
            self._say(s, 1)
        return " ".join(out)

    def ctx(self) -> dict:
        """Driving context for the radio scheduler."""
        tr = self.tr
        c = {"paused": tr.paused}
        if tr.telemetry is None or tr.lapdata is None:
            c["paused"] = True
            return c
        T, L = tr.telemetry[tr.player_idx], tr.lapdata[tr.player_idx]
        c.update(throttle=float(T["throttle"]), brake=float(T["brake"]), steer=float(T["steer"]),
                 in_garage=int(L["driverStatus"]) == 0, pit=int(L["pitStatus"]) > 0,
                 safety_car=P.SAFETY_CAR.get(tr.session.get("safetyCarStatus", 0), "none"))
        if self.ref:
            d = float(L["lapDistance"]) % self.ref["track_length"]
            v = max(float(T["speed"]) / 3.6, 10.0)
            ahead = [b - d for b in self.ref["brake_starts"] if b > d]
            nxt = min(ahead) if ahead else self.ref["track_length"] - d + (self.ref["brake_starts"] or [0])[0]
            c["time_to_brake"] = nxt / v
        else:
            c["time_to_brake"] = 3.0 if float(T["throttle"]) > 0.95 and float(T["speed"]) > 250 else 0.0
        return c

    def _tick_loop(self) -> None:
        n = 0
        while not self._stop.is_set():
            time.sleep(0.1)
            n += 1
            try:
                if not self.tr.session:
                    continue
                # F1 2020 stops sending while paused / in menus: treat silence as "parked" (debrief time)
                quiet = time.time() - self.tr.last_packet_wall > 3
                self.sched.tick({"paused": True} if quiet else self.ctx())
                if quiet:
                    continue
                if self.tr.is_race and n % 20 == 0:
                    self.race = strategy.race_snapshot(self.tr, self.gaps, self.tyres)
                    msgs = radio.race_messages(self.race, self.cfg.sass, self.last_calls)
                    self.last_calls |= {m.key for m in msgs}
                    self.sched.push(msgs)
                    if n % 600 == 0:
                        self.last_calls.clear()  # allow repeating important calls every minute
            except Exception:
                traceback.print_exc()

    # ---- background work -----------------------------------------------------------
    def _bg_put(self, fn, *args) -> None:
        try:
            self._bg.put_nowait((fn, args))
        except queue.Full:
            print("[engine] background queue full, dropping a write")

    def _bg_worker(self) -> None:
        while not self._stop.is_set():
            try:
                fn, args = self._bg.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()

    def drain(self) -> None:
        """Run queued background work synchronously (tests/replay)."""
        while True:
            try:
                fn, args = self._bg.get_nowait()
            except queue.Empty:
                return
            fn(*args)

    # ---- LLM tools ------------------------------------------------------------------
    def _context(self) -> str:
        tr = self.tr
        best = min((r.lap_time for r in self.reports), default=None)
        return (f"track={tr.track}; mode={tr.session_type}; laps_analysed={len(self.reports)}; best_analysed={best}; "
                f"reference={self.ref['label'] + ' ' + str(round(self.ref['time'], 3)) if self.ref else 'none'}; "
                f"input={self.input_device}; assists={self.reports[-1].assists if self.reports else {}}")

    def _tools(self) -> dict:
        def overview():
            tr = self.tr
            return {"track": tr.track, "mode": tr.session_type, "input_device": self.input_device,
                    "laps": [{"lap": r.lap_num, "time": r.lap_time, "valid": r.valid, "delta": r.delta} for r in self.reports[-10:]],
                    "reference": None if not self.ref else {"label": self.ref["label"], "time": self.ref["time"]}}

        def last_lap(lap: int | None = None):
            reps = [r for r in self.reports if lap is None or r.lap_num == lap]
            if not reps:
                return {"error": "no analysed lap yet (needs a reference lap plus one more complete lap)"}
            d = reps[-1].to_dict()
            d["corners"] = [{k: c[k] for k in ("corner", "time_loss", "causes")} for c in d["corners"]]
            return d

        def compare(lap_a: int, lap_b: int):
            a = next((r for r in self.reports if r.lap_num == lap_a), None)
            b = next((r for r in self.reports if r.lap_num == lap_b), None)
            if not a or not b:
                return {"error": "lap not found"}
            return {"lap_a": lap_a, "lap_b": lap_b, "time_diff": round(b.lap_time - a.lap_time, 3),
                    "corners": [{"corner": ca["corner"], "b_minus_a": round(cb["time_loss"] - ca["time_loss"], 3)}
                                for ca, cb in zip(a.corners, b.corners)]}

        def corner_hist(corner: str):
            out = []
            for r in self.reports[-8:]:
                for c in r.corners:
                    if corner.lower() in c["corner"].lower():
                        out.append({"lap": r.lap_num, **{k: c[k] for k in ("time_loss", "min_speed", "brake_point_m", "causes")}})
            return {"corner": corner, "laps": out} if out else {"error": f"no corner matching {corner}"}

        return {
            "get_session_overview": overview,
            "get_last_lap_report": last_lap,
            "compare_laps": compare,
            "get_corner_history": corner_hist,
            "get_setup_and_advice": lambda: {"setup": self.tr.setup, "advice": self.advice or setup_advisor.advise(
                self.reports[-5:], self.tr.setup, self.input_device, self.reports[-1].assists if self.reports else {})},
            "get_race_status": lambda: self.race or strategy.race_snapshot(self.tr, self.gaps, self.tyres),
            "get_consistency": lambda: {"corners": coach.consistency(self.reports[-6:])},
            "search_knowledge": lambda query="": {"results": knowledge.search(query)},
        }

    # ---- dashboard state -----------------------------------------------------------
    def live_state(self) -> dict:
        tr = self.tr
        s = {"connected": time.time() - tr.last_packet_wall < 3, "track": tr.track if tr.session else None,
             "mode": tr.session_type if tr.session else None, "input_device": self.input_device, "stats": self.stats,
             "reference": None if not self.ref else {"label": self.ref["label"], "time": self.ref["time"]}}
        if tr.telemetry is not None and tr.lapdata is not None:
            T, L = tr.telemetry[tr.player_idx], tr.lapdata[tr.player_idx]
            s["car"] = {"speed": int(T["speed"]), "gear": int(T["gear"]), "throttle": round(float(T["throttle"]), 2),
                        "brake": round(float(T["brake"]), 2), "steer": round(float(T["steer"]), 2), "rpm": int(T["engineRPM"]),
                        "tyre_surface": [int(x) for x in T["tyresSurfaceTemperature"]],
                        "tyre_inner": [int(x) for x in T["tyresInnerTemperature"]],
                        "brake_temp": [int(x) for x in T["brakesTemperature"]]}
            s["lap"] = {"num": int(L["currentLapNum"]), "time": round(float(L["currentLapTime"]), 3),
                        "dist": round(float(L["lapDistance"])), "last": round(float(L["lastLapTime"]), 3),
                        "best": round(float(L["bestLapTime"]), 3), "invalid": bool(L["currentLapInvalid"]),
                        "position": int(L["carPosition"])}
            if self.ref:
                rs = self.ref["rs"]
                d = float(L["lapDistance"])
                if 0 <= d < self.ref["track_length"]:
                    s["lap"]["delta"] = round(float(L["currentLapTime"]) - float(np.interp(d, rs["dist"], rs["t"])), 3)
        st = tr.status[tr.player_idx] if tr.status and tr.status[tr.player_idx] else {}
        if st:
            s["status"] = {"tyre_wear": st.get("tyresWear"), "compound": P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "?"),
                           "tyre_age": st.get("tyresAgeLaps"), "fuel_laps": round(float(st.get("fuelRemainingLaps", 0)), 2),
                           "ers_pct": round(float(st.get("ersStoreEnergy", 0)) / 4e6 * 100), "tc": st.get("tractionControl"),
                           "abs": st.get("antiLockBrakes")}
        if tr.is_race:
            s["race"] = self.race
        if self.reports:
            s["last_report"] = self.reports[-1].to_dict()
        s["advice"] = self.advice
        return s
