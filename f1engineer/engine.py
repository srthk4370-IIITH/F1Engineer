"""The engineer's core: UDP ingest -> tracker -> analysis -> radio/LLM -> storage + live state for the dashboard."""
from __future__ import annotations

import queue
import random
from collections import deque
import socket
import threading
import time
import traceback

import numpy as np

from f1engineer import adapt, brain, radio
from f1engineer.coaching import LiveCoach
from f1engineer.race import RaceEngineer
from f1engineer.insight import Patterns
from f1engineer.pipeline import AskPipeline
from f1engineer.analysis import coach, lapmath as LM, setup_advisor, strategy
from f1engineer.config import Config
from f1engineer.llm import knowledge
from f1engineer.llm.agent import Agent, MockProvider
from f1engineer.state.tracker import SessionTracker
from f1engineer.storage import Storage
from f1engineer.telemetry import formats, packets_2020 as P



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
        self.profile = brain.DriverProfile(self.cfg.data_dir / "driver_profile.json")
        self.tr = SessionTracker()
        self.gaps = strategy.GapTimer()
        self.tyres = strategy.TyreModel()
        self.sched = radio.RadioScheduler(self._say)
        self.agent = Agent(self._tools(), self._context, sass=self.cfg.sass, provider=provider,
                           profile_fn=self.profile.summary)
        self.reports: list[coach.LapReport] = []
        self.ref: dict | None = None          # {"rs": resampled, "time": float, "label": str, "brake_starts": [...]}
        self.corners: list[LM.Corner] = []
        self.advice: list[dict] = []
        self.race: dict = {}
        self.last_calls: set[str] = set()
        self.input_device = "unknown"
        self.unsupported_formats: set[int] = set()
        self.parsers = formats.Parsers()
        self.game = None          # e.g. "F1 24", from the packets
        self.stats = {"packets": 0, "parse_errors": 0, "last_lap_analysis_ms": 0.0}
        self._bg: queue.Queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._last_buttons = 0
        self._ptt_mask = next((k for k, v in P.BUTTONS.items() if v == self.cfg.ptt_game_button), 0)
        self.listeners: list = []  # callables(event: dict) for the dashboard websocket
        self.coach = LiveCoach(self)
        self.race_eng = RaceEngineer(self)
        self.patterns = Patterns(self)
        self._tips_said: set[str] = set()
        self._debrief_pending: int | None = None
        self.gap_hist: deque = deque(maxlen=10)
        self._recent_radio: deque[str] = deque(maxlen=20)
        self._last_strategist = 0.0
        self.pipeline = AskPipeline(self)
        self._greet_at = 0.0
        radio.WORDS_PER_SEC = 2.7 * self.cfg.tts_speed   # message length vs the straight
        self.adapt: dict = {}     # adapt.profile(): game, OS, input, assists, skill tier
        self._unanalysed = None  # first lap of a TT session, waiting for a ghost reference
        self._first_review: dict | None = None
        tr = self.tr
        tr.on("session_start", self._on_session)
        tr.on("lap_complete", self._on_lap)
        tr.on("lap_aborted", lambda lap, why: self._emit({"type": "lap_aborted", "lap": lap.lap_num, "reason": why}))
        tr.on("sc_change", self._on_sc)
        tr.on("flashback", lambda t: self.sched.clear())
        tr.on("sector", lambda car, sector: self.coach.on_sector(sector))

    # ---- ingest ------------------------------------------------------------------
    def feed(self, data: bytes, wall: float | None = None) -> None:
        self.stats["packets"] += 1
        if len(data) < 24:
            return
        fmt = int.from_bytes(data[0:2], "little")
        parse = self.parsers.get(fmt)
        if parse is None:
            if fmt not in self.unsupported_formats:
                self.unsupported_formats.add(fmt)
                print(f"[engine] packet format {fmt} not supported (supported: {', '.join(map(str, formats.SUPPORTED))}; "
                      "pick one of those as the game's UDP Format)")
            return
        r = parse(data)
        if r is None:
            self.stats["parse_errors"] += 1
            return
        pid, p = r
        if p is None:
            return  # absorbed into a later packet (e.g. MotionEx / CarDamage on newer games)
        if self.game is None or self.stats["packets"] % 500 == 0:
            self.game = self.parsers.game(fmt)
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
        p = self.agent.provider
        print(f"[llm] {'OpenAI ' + p.model if hasattr(p, 'model') else 'offline brain (no OpenAI key or F1E_LLM=mock)'}; "
              f"voice speed {self.cfg.tts_speed}x; PTT '{self.cfg.ptt_key}' / {self.cfg.ptt_game_button}")
        sock = self.bind_udp()
        threading.Thread(target=self.run_udp, args=(sock,), name="udp", daemon=True).start()
        threading.Thread(target=self._bg_worker, name="bg", daemon=True).start()
        self._bg_put(brain.backfill, self.store)
        self._bg_put(self.store.prune_history)
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
        self.coach = LiveCoach(self)
        self.race_eng = RaceEngineer(self)
        self.patterns = Patterns(self)
        self._unanalysed = None   # (the conversation itself carries on across sessions, like a real engineer)
        self._first_review = None
        self._bg_put(self.store.start_session, tr.session_uid, tr.track, tr.session_type, tr.packet_format)
        # stored personal best becomes the initial reference
        stored = self.store.reference(tr.track)
        if stored:
            self._set_reference(stored[0], stored[1], "your stored best")
        self._emit({"type": "session", "track": tr.track, "mode": tr.session_type})
        self._greet_at = radio.CLOCK() + 4   # wait for weather/temps/tyres to arrive, then a real radio check

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
                first, self._unanalysed = self._unanalysed, None
                # the driver's first lap had no reference: compare it with the ghost, if that's still news
                if first is not None and radio.CLOCK() - self._unanalysed_at < 30:
                    self._analyse(first)
            if lap.complete and (tr.is_time_trial or lap.car_idx < 22):
                self._bg_put(self.store.save_lap, tr.session_uid, lap, tr.car_name(lap.car_idx), None, "")
            return
        t0 = time.perf_counter()
        self.input_device = coach.detect_input_device(lap.arrays) if lap.n() > 100 else self.input_device
        rep = None
        if lap.complete:
            if self.ref is None:
                self._set_reference(lap.arrays, lap.lap_time, "your best lap")
                if self.ref and not tr.is_race:
                    self._unanalysed = lap if tr.is_time_trial else None
                    self._unanalysed_at = radio.CLOCK()
                    if self.coach.was_out_lap(lap.lap_num):
                        self._say(f"Out lap done, {radio.fmt_t(lap.lap_time)}. Tyres are in, now we push.", 3)
                    else:
                        issues = coach.self_review(self.ref["rs"], self.corners)
                        if not isinstance(self.agent.provider, MockProvider):   # independent AI read of lap 1
                            self._debrief_pending = lap.lap_num
                            self._last_strategist = 0
                            self._strategist(
                                f"first lap at this track ({radio.short_t(lap.lap_time)}), no reference yet: analyse it on "
                                "its own (sectors, inputs, mistakes, style via get_last_lap_report and get_driving_style) "
                                "and give the single most useful call for the next lap, plus one setup change with a "
                                "value if the data supports it", replace_key="focus")
                        self._first_review = {
                            "lap": lap.lap_num, "lap_time": round(lap.lap_time, 3), "valid": not lap.invalid,
                            "sectors_ms": list(lap.sector_ms), "input_device": self.input_device,
                            "note": "first lap at this track this session: no reference yet, so it is judged on its own "
                                    "(mistakes, inputs, style). Reason from these and from knowledge.",
                            "issues": issues, "style": brain.style_metrics(self.ref["rs"], issues)}
                        self.sched.push(radio.self_review_messages(lap.lap_time, not lap.invalid, issues, self.cfg.sass))
            else:
                rep = self._analyse(lap)
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
            a_, b_ = self.race.get("ahead") or {}, self.race.get("behind") or {}
            self.gap_hist.append({"lap": lap.lap_num, "ahead": a_.get("gap_s"), "ahead_name": a_.get("name"),
                                  "behind": b_.get("gap_s"), "behind_name": b_.get("name")})
            self.sched.push([radio.race_lap_message(self.race, lap.lap_time)])
            self._bg_put(self.race_eng.on_lap, lap.lap_num, self.race)           # pit window (strategy sim)
            if lap.lap_num % 2 == 0:
                self._strategist(f"lap {lap.lap_num} of {tr.session.get('totalLaps')} completed, P{self.race.get('position')}")
            self._bg_put(self.store.save_race_lap, tr.session_uid, lap.lap_num, lap.lap_time, self.race)
        if lap.complete:
            try:
                self._summarise(lap, rep)
            except Exception:
                traceback.print_exc()
        self.stats["last_lap_analysis_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self._bg_put(self.store.save_lap, tr.session_uid, lap, tr.car_name(lap.car_idx),
                     rep.to_dict() if rep else None, self.input_device)
        self._emit({"type": "lap", "lap": lap.lap_num, "time": lap.lap_time, "valid": not lap.invalid,
                    "report": rep.to_dict() if rep else None})

    def _analyse(self, lap):
        """Corner-by-corner report against the current reference + radio debrief. Returns the report (or None)."""
        tr = self.tr
        rs = LM.resample(lap.arrays, self.ref["track_length"])
        if rs is None:
            return None
        rep = coach.build_report(lap, rs, self.ref["rs"], self.ref["label"], self.ref["time"], self.corners)
        self.reports.append(rep)
        del self.reports[:-20]
        self.advice = setup_advisor.advise(self.reports[-5:], tr.setup, self.input_device, lap.assists)
        self.adapt = adapt.profile(self)
        is_best = lap.lap_time < self.ref["time"] and (not lap.invalid or "best" in self.ref["label"])
        if not self.coach.was_out_lap(lap.lap_num):   # patterns across laps: recurring corners, pace vs tyres/fuel
            st = tr.status[tr.player_idx] if tr.status and tr.status[tr.player_idx] else {}
            wear = float(np.mean(st.get("tyresWear") or [0])) if st else None
            self.sched.push(self.patterns.on_report(rep, wear))
        if self.coach.was_out_lap(lap.lap_num):
            self._say(f"Out lap done, {radio.fmt_t(lap.lap_time)}. Tyres are in, now we push.", 3)
        elif tr.is_time_trial or not tr.is_race:
            self.sched.push(radio.tt_lap_messages(rep, self.cfg.sass, is_best))
            self.sched.push(radio.next_lap_messages(rep, self.advice, self._tips_said, self.adapt))
            self.sched.push([radio.Message(6, radio.CLOCK(), radio.lap_debrief_text(rep, self.advice),
                                           "garage", 900, "garage_debrief")])
            if not isinstance(self.agent.provider, MockProvider):
                self._debrief_pending = rep.lap_num        # written by the AI only if/when the car is parked
            if not isinstance(self.agent.provider, MockProvider) and (abs(rep.delta) > 0.3 or len(self.reports) % 3 == 1):
                self._strategist(f"lap {rep.lap_num} analysed ({radio.short_t(rep.lap_time)}, {rep.delta:+.2f} vs "
                                 f"{rep.ref_label}): give the single most useful coaching call for the next lap "
                                 "(look at the lap report, corner history and style)", replace_key="focus")
        return rep

    def _ai_debrief(self, lap_num: int, fallback: radio.Message | None = None) -> None:
        """The model writes the garage debrief (progress, priorities, a drill); the template is the fallback."""
        text = ""
        try:
            a = Agent(self._tools(), self._context, sass=self.cfg.sass, provider=self.agent.provider,
                      profile_fn=self.profile.summary)
            text = " ".join(a.ask(
                f"(Your own analysis, delivered now in a quiet moment: slow lap, pit lane or garage.) Analyse lap "
                f"{lap_num} and the session so far: dig through the lap report, corner history, consistency, driving "
                "style and progress first. Then tell the driver what happened, why, the one or two things to change and "
                "exactly how, and a setup change with a value if the data supports it.", mode="analysis", remember=False))
        except Exception as e:
            print(f"[engine] AI debrief failed: {e!r}")
        if text:
            self.sched.push([radio.Message(6, radio.CLOCK(), text, "garage", 900, "garage_debrief")])
        elif fallback is not None:
            self.sched.push([fallback])

    def _summarise(self, lap, rep) -> None:
        """Long-term memory row for this lap: style numbers + per-corner losses (for progress/style questions)."""
        tr = self.tr
        L = self.ref["track_length"] if self.ref else float(lap.arrays["dist"].max())
        rs = LM.resample(lap.arrays, L)
        if rs is None:
            return
        style = brain.style_metrics(rs, coach.self_review(rs, self.corners) if self.corners else None)
        corners = None
        if rep:
            corners, detail = {}, []
            for c in rep.corners:
                corners[c["corner"]] = round(corners.get(c["corner"], 0) + c["time_loss"], 3)
                detail.append({"corner": c["corner"],
                               "brakes_earlier_than_ref_m": None if c["brake_point_m"] is None or c["ref_brake_point_m"] is None
                               else c["brake_point_m"] - c["ref_brake_point_m"],
                               "apex_speed_above_ref_kmh": c["min_speed"] - c["ref_min_speed"],
                               "full_throttle_later_than_ref_m": None if c["throttle_on_m"] is None or c["ref_throttle_on_m"] is None
                               else c["throttle_on_m"] - c["ref_throttle_on_m"]})
            style["corner_detail"] = detail
        self._bg_put(self.store.save_summary, tr.session_uid, tr.track, tr.session_type, lap, self.input_device, style,
                     corners, rep.mistakes if rep else [], rep.ref_label if rep else None)

    def _on_sc(self, status: str) -> None:
        if self.tr.is_race:
            self._last_strategist = 0
            self._strategist(f"safety car status changed to {status}")
        # the call itself (box or stay out) comes from the race engineer, which knows tyres and laps left

    # ---- radio / PTT ---------------------------------------------------------------
    def _say(self, text: str, priority: int = 5) -> None:
        self._recent_radio.append(text)
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

    def mode(self) -> str:
        """radio when the car is moving, garage when parked/paused (the engineer can talk longer and think deeper)."""
        tr = self.tr
        if tr.telemetry is None or time.time() - tr.last_packet_wall > 3 or tr.paused:
            return "chat"
        c = self.ctx()
        return "chat" if c.get("in_garage") or float(tr.telemetry[tr.player_idx]["speed"]) < 5 else "radio"

    def ask(self, question: str, mode: str | None = None, wait: float = 90) -> str:
        """Driver question (STT or dashboard chat): queued, answered in order, spoken as sentences arrive."""
        question = question.strip()
        if not question:   # the button worked but no words came through
            self._say(random.choice(["Didn't catch that, say again.", "Say again?", "Missed that, come again."]), 1)
            return ""
        self._emit({"type": "radio", "role": "driver", "text": question})
        self._bg_put(self.store.log_radio, self.tr.session_uid, "driver", question)
        q = self.pipeline.submit(question, mode)
        q.done.wait(wait)
        return q.answer

    # ---- the engineer's own initiative (agentic: it investigates with tools before deciding to speak) -------
    def _strategist(self, trigger: str, replace_key: str = "ai_call") -> None:
        if isinstance(self.agent.provider, MockProvider) or self.pipeline.busy:
            return
        now = time.time()
        if now - self._last_strategist < 40:
            return
        self._last_strategist = now

        def run():
            try:
                a = Agent(self._tools(), self._context, sass=self.cfg.sass, provider=self.agent.provider,
                          profile_fn=self.profile.summary)
                said = " | ".join(list(self._recent_radio)[-8:]) or "nothing yet"
                text = " ".join(a.ask(
                    f"(Pit wall, not a driver question.) Trigger: {trigger}. Decide whether the driver needs to hear "
                    "something right now that the radio hasn't already covered: strategy (pit now? stay out?), tyres "
                    "(wear, overheating, cold), fuel, threats or chances from cars around, weather, or the one driving "
                    "fix that matters most. Check the data with tools first. Already said on the radio recently: "
                    f"{said}. If nothing important, answer exactly SILENT.", mode="strategist", remember=False)).strip()
                print(f"[strategist] {trigger}: {text[:160]}")
                if text and "SILENT" not in text.upper()[:20]:
                    self.sched.push([radio.Message(3, radio.CLOCK(), text, "straight", 45, replace_key)])
            except Exception as e:
                print(f"[strategist] failed: {e!r}")
        threading.Thread(target=run, name="strategist", daemon=True).start()

    def ctx(self) -> dict:
        """Driving context for the radio scheduler."""
        tr = self.tr
        c = {"paused": tr.paused}
        if tr.telemetry is None or tr.lapdata is None:
            c["paused"] = True
            return c
        T, L = tr.telemetry[tr.player_idx], tr.lapdata[tr.player_idx]
        from f1engineer.coaching import IN_LAP, OUT_LAP
        c["cooldown"] = bool(self.coach.gone_called or self.coach.status in (OUT_LAP, IN_LAP)
                             or P.SAFETY_CAR.get(tr.session.get("safetyCarStatus", 0), "none") != "none")
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
                # F1 2020 stops sending while paused / in menus: treat silence as "parked" (debrief time)
                self.step(n, quiet=time.time() - self.tr.last_packet_wall > 3)
            except Exception:
                traceback.print_exc()

    def step(self, n: int, quiet: bool = False) -> None:
        """One 10 Hz tick of everything proactive (live loop and replays/tests call the same code)."""
        if not self.tr.session:
            return
        c = {"paused": True} if quiet else self.ctx()
        c["talking"] = self.pipeline.busy
        parked = bool(c.get("paused") or c.get("in_garage") or c.get("cooldown"))
        if parked and self._debrief_pending and not self.pipeline.busy:
            lap_num, self._debrief_pending = self._debrief_pending, None
            held = next((m for m in self.sched.queue if m.key == "garage_debrief"), None)
            if held:
                self.sched.queue.remove(held)      # hold the template while the AI writes the real debrief
            threading.Thread(target=self._ai_debrief, args=(lap_num, held), daemon=True).start()
        self.sched.tick(c)
        if quiet:
            return
        if self._greet_at and radio.CLOCK() > self._greet_at:
            self._greet_at = 0
            tr = self.tr
            st = tr.status[tr.player_idx] if tr.status and tr.status[tr.player_idx] else {}
            stored = self.store.reference(tr.track)
            self._say(radio.radio_check(tr, P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "") if st else "",
                                        stored[1] if stored else None), 3)
        self.coach.tick()
        if self.tr.is_race:
            self.race_eng.tick()
            if n % 20 == 0:
                self.race = strategy.race_snapshot(self.tr, self.gaps, self.tyres)
                self.race_eng.battles(self.race)

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
        """Compact live snapshot sent with every question: simple questions need no tool round trip."""
        tr = self.tr
        best = min((r.lap_time for r in self.reports), default=None)
        a = self.adapt or {}
        s = (f"game={self.game}; os={a.get('os')}; skill_tier={a.get('tier')} ({a.get('style')}); "
             f"driver_state={'driving' if self.mode() == 'radio' else 'parked/paused'}; "
             f"track={tr.track}; mode={tr.session_type}; laps_analysed={len(self.reports)}; best_analysed={best}; "
             f"reference={self.ref['label'] + ' ' + str(round(self.ref['time'], 3)) if self.ref else 'none'}; "
             f"input={self.input_device}; assists={self.reports[-1].assists if self.reports else {}}")
        try:
            car = self._car_status()
            if "tyres" in car:
                t = car["tyres"]
                s += (f"; tyres {t['compound']} {t['age_laps']} laps old, surface temps C {t['surface_temp_c']}, "
                      f"inner {t['inner_temp_c']}, wear % {t['wear_pct']}; fuel margin laps {car.get('fuel_margin_laps')}; "
                      f"ERS {car.get('ers_pct')}%")
            if tr.lapdata is not None:
                L = tr.lapdata[tr.player_idx]
                s += (f"; lap {int(L['currentLapNum'])}, P{int(L['carPosition'])}, last {round(float(L['lastLapTime']), 3)}, "
                      f"best {round(float(L['bestLapTime']), 3)}, current lap invalid={bool(L['currentLapInvalid'])}")
                d = self.coach.delta_now()
                if d is not None:
                    s += f", live delta {d:+.2f}s"
            if tr.is_race:
                s += f"; race lap {int(tr.lapdata[tr.player_idx]['currentLapNum'])} of {tr.session.get('totalLaps')}"
                plan = (self.race_eng.plan or {}).get("best_alternatives") or []
                if plan:
                    s += f"; current best strategy {plan[0]['stops']}"
                near = self._rivals(2).get("cars", [])
                s += "; nearby: " + ", ".join(f"P{c['position']} {c['name']} gap {c.get('gap_to_me_s')}s {c.get('tyre')}"
                                              for c in near if not c.get("me"))
            if self.reports:
                r = self.reports[-1]
                s += "; pit wall noticed: " + self.patterns.summary()
                s += f"; last analysed lap {r.lap_num}: top losses " + ", ".join(
                    f"{c['corner']} +{c['time_loss']:.2f} ({', '.join(c['causes'][:2])})" for c in r.losses[:3])
        except Exception as e:
            s += f"; (live snapshot error {e!r})"
        return s

    def _car_status(self) -> dict:
        tr = self.tr
        me = tr.player_idx
        out: dict = {}
        names = ["rear_left", "rear_right", "front_left", "front_right"]  # F1 2020 wheel order
        st = tr.status[me] if tr.status and tr.status[me] else {}
        if tr.telemetry is not None:
            T = tr.telemetry[me]
            out["tyres"] = {
                "compound": P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "?") if st else "?",
                "age_laps": st.get("tyresAgeLaps") if st else None,
                "surface_temp_c": dict(zip(names, (int(x) for x in T["tyresSurfaceTemperature"]))),
                "inner_temp_c": dict(zip(names, (int(x) for x in T["tyresInnerTemperature"]))),
                "pressure_psi": dict(zip(names, (round(float(x), 1) for x in T["tyresPressure"]))),
                "wear_pct": dict(zip(names, st.get("tyresWear", []))) if st else None,
                "typical_window_c": "surface ~85-105, inner ~90-105 (dry slicks)",
            }
            out["brake_temp_c"] = dict(zip(names, (int(x) for x in T["brakesTemperature"])))
            out["engine_temp_c"] = int(T["engineTemperature"])
        if st:
            out["fuel_margin_laps"] = round(float(st.get("fuelRemainingLaps", 0)), 2)
            out["ers_pct"] = round(float(st.get("ersStoreEnergy", 0)) / 4e6 * 100)
            dmg = {k: st.get(k) for k in ("frontLeftWingDamage", "frontRightWingDamage", "rearWingDamage",
                                          "engineDamage", "gearBoxDamage") if st.get(k)}
            out["damage_pct"] = dmg or "none"
        return out or {"error": "no car data yet"}

    def _strategy_defaults(self) -> dict:
        """Live inputs for the strategy simulator (the driver can override any of them)."""
        tr, d = self.tr, {}
        if tr.lapdata is not None:
            L = tr.lapdata[tr.player_idx]
            d["current_lap"] = int(L["currentLapNum"]) or 1
            if float(L["lastLapTime"]) > 0:
                d["base_lap"] = round(float(L["lastLapTime"]), 2)
        if tr.session.get("totalLaps"):
            d["total_laps"] = int(tr.session["totalLaps"])
        st = tr.status[tr.player_idx] if tr.status and tr.status[tr.player_idx] else {}
        if st:
            d["start_compound"] = P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "soft").lower()
            d["tyre_age"] = int(st.get("tyresAgeLaps") or 0)
        rate = self.tyres.rate()
        if rate:
            factor = brain.COMPOUND_WEAR.get(d.get("start_compound", "soft"), 1.0)
            d["wear_rate_soft"] = rate / factor
        d["pit_loss"] = strategy.PIT_LOSS.get(tr.track, 21.0)
        return d

    def _rivals(self, around: int = 3) -> dict:
        tr = self.tr
        if tr.lapdata is None:
            return {"error": "no timing data yet"}
        me = tr.player_idx
        my_pos = int(tr.lapdata[me]["carPosition"])
        cars = []
        for i in range(22):
            L = tr.lapdata[i]
            pos = int(L["carPosition"])
            if pos == 0 or int(L["resultStatus"]) < 2 or abs(pos - my_pos) > around:
                continue
            st = tr.status[i] if tr.status and tr.status[i] else {}
            g = 0.0 if i == me else (self.gaps.gap(me, i) if pos < my_pos else self.gaps.gap(i, me))
            cars.append({"position": pos, "name": tr.car_name(i), "me": i == me,
                         "gap_to_me_s": None if g is None else round(g if pos > my_pos else -g, 2),
                         "last_lap": round(float(L["lastLapTime"]), 3), "best_lap": round(float(L["bestLapTime"]), 3),
                         "tyre": P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "?") if st else "?",
                         "tyre_age_laps": st.get("tyresAgeLaps") if st else None,
                         "in_pit": int(L["pitStatus"]) > 0, "penalties_s": int(L["penalties"])})
        cars.sort(key=lambda c: c["position"])
        return {"my_position": my_pos, "total_cars": tr.num_active, "cars": cars,
                "note": "gap_to_me_s: negative = that car is ahead of me by that many seconds"}

    def _tools(self) -> dict:
        def overview():
            tr = self.tr
            return {"track": tr.track, "mode": tr.session_type, "input_device": self.input_device,
                    "laps": [{"lap": r.lap_num, "time": r.lap_time, "valid": r.valid, "delta": r.delta} for r in self.reports[-10:]],
                    "reference": None if not self.ref else {"label": self.ref["label"], "time": self.ref["time"]}}

        def last_lap(lap: int | None = None):
            reps = [r for r in self.reports if lap is None or r.lap_num == lap]
            if not reps:
                if self._first_review:   # first lap at this track: judged on its own, still useful
                    return self._first_review
                return {"error": "no complete lap yet this session"}
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
            "get_setup_and_advice": lambda: {
                "game": self.game, "track": self.tr.track, "current_setup": self.tr.setup,
                "rule_based_advice": self.advice or setup_advisor.advise(
                    self.reports[-5:], self.tr.setup, self.input_device, self.reports[-1].assists if self.reports else {}),
                "balance_last_lap": self.reports[-1].balance if self.reports else None,
                "first_lap_review": None if self.reports else self._first_review,
                "how_to_reason": "combine the balance (understeer/oversteer by corner speed), the corners where time is "
                                 "lost and their causes, the driver's input device and style, and the track's character "
                                 "(search_knowledge for the track and 'Which game') into 1-3 concrete changes with values "
                                 "and the reason for each"},
            "get_race_status": lambda: self.race or strategy.race_snapshot(self.tr, self.gaps, self.tyres),
            "get_consistency": lambda: {"corners": coach.consistency(self.reports[-6:])},
            "search_knowledge": lambda query="": {"results": knowledge.search(query)},
            "get_car_status": self._car_status,
            "get_race_history": lambda: {"laps": self.store.race_log(self.tr.session_uid)[-30:]} if self.tr.is_race
            else {"error": "not a race session"},
            "get_rivals": lambda around=3: self._rivals(int(around)),
            **brain.brain_tools(self.store, self.profile, self._strategy_defaults),
        }

    # ---- dashboard state -----------------------------------------------------------
    def live_state(self) -> dict:
        tr = self.tr
        s = {"connected": time.time() - tr.last_packet_wall < 3, "track": tr.track if tr.session else None, "game": self.game,
             "adapt": self.adapt,
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
