"""Session state: turns parsed packets into per-car lap traces and session events.

Handles lap completion, Time Trial restarts and flashbacks (time going backwards),
session changes, and keeps the latest snapshot of everything for the analysis/LLM tools.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from f1engineer.telemetry import packets_2020 as P

# Channels recorded for every car (cheap) and extra player-only channels (from Motion).
CAR_CHANNELS = ("dist", "t", "speed", "throttle", "brake", "steer", "gear", "rpm", "drs")
PLAYER_CHANNELS = ("x", "z", "yaw_rate", "wheels_angle", "slip_rl", "slip_rr", "slip_fl", "slip_fr",
                   "glat", "glon", "surface")
OFF_TRACK_SURFACES = {3, 4, 5, 6, 7, 8}  # rock, gravel, mud, sand, grass, water (tarmac/rumble/concrete are fine)


@dataclass
class Lap:
    car_idx: int
    lap_num: int
    channels: dict[str, list] = field(default_factory=dict)
    lap_time: float = 0.0
    sector_ms: tuple[int, int, int] = (0, 0, 0)
    invalid: bool = False
    complete: bool = False
    assists: dict = field(default_factory=dict)
    compound: str = ""
    started_wall: float = field(default_factory=time.time)
    arrays: dict[str, np.ndarray] | None = None

    def append(self, values: dict) -> None:
        ch = self.channels
        for k, v in values.items():
            ch.setdefault(k, []).append(v)

    def n(self) -> int:
        if self.arrays is not None:
            return self._n
        return len(self.channels.get("t", ()))

    def truncate_after(self, t: float) -> None:
        ts = self.channels.get("t", [])
        keep = next((i for i, x in enumerate(ts) if x > t), len(ts))
        for k in self.channels:
            del self.channels[k][keep:]

    def finalize(self) -> "Lap":
        self._n = self.n()
        self.arrays = {k: np.asarray(v, dtype=np.float32) for k, v in self.channels.items()}
        self.channels = {}  # free the Python lists; arrays hold the data
        return self


class CarTracker:
    """Tracks the in-progress lap of one car."""

    def __init__(self, idx: int, is_player: bool):
        self.idx = idx
        self.is_player = is_player
        self.lap: Lap | None = None
        self.last_lap_num = -1
        self.last_lap_time = 0.0
        self.last_cur_time = 0.0
        self.last_dist = 0.0
        self.completed: list[Lap] = []
        self.invalid_now = False


class SessionTracker:
    """Feed parsed packets via on_packet(); subscribe with .on(event, fn).

    Events: session_start(info), lap_complete(lap), lap_aborted(lap, reason), sector(car, sector),
    event(code, details), mode(session_type), flashback(t), sc_change(status)
    """

    MAX_CARS_TRACKED = 22
    OTHER_CAR_DECIMATE = 3  # store every 3rd sample for non-player cars (~20 Hz)

    def __init__(self):
        self._listeners: dict[str, list[Callable]] = {}
        self.reset(0)

    # --- pub/sub ---------------------------------------------------------
    def on(self, event: str, fn: Callable) -> None:
        self._listeners.setdefault(event, []).append(fn)

    def emit(self, event: str, *args) -> None:
        for fn in self._listeners.get(event, ()):
            try:
                fn(*args)
            except Exception as e:  # a listener must never kill ingest
                import traceback
                print(f"[tracker] listener {event} failed: {e!r}\n{traceback.format_exc(limit=-3)}")

    # --- state -----------------------------------------------------------
    def reset(self, uid: int) -> None:
        self.session_uid = uid
        self.packet_format = 0
        self.player_idx = 0
        self.session: dict = {}
        self.participants: list[dict] = []
        self.num_active = 0
        self.setup: dict = {}
        self.status: list[dict] = [{} for _ in range(22)]
        self.lapdata: np.ndarray | None = None
        self.telemetry: np.ndarray | None = None
        self.buttons = 0
        self.motion_player: dict = {}
        self.cars = [CarTracker(i, False) for i in range(22)]
        self.events: list[tuple[float, str, dict]] = []
        self.paused = False
        self._tel_count = 0
        self.last_packet_wall = 0.0
        self.session_time = 0.0
        self.trackmap = None  # analysis.trackmap.TrackMap, set once a clean player lap exists
        self.ghosts: dict = {}

    @property
    def track(self) -> str:
        return P.TRACKS.get(self.session.get("trackId", -1), f"track{self.session.get('trackId')}")

    @property
    def session_type(self) -> str:
        return P.SESSION_TYPES.get(self.session.get("sessionType", 0), "unknown")

    @property
    def is_time_trial(self) -> bool:
        return self.session_type == "Time Trial"

    @property
    def is_race(self) -> bool:
        return self.session_type.startswith("Race")

    def car_name(self, idx: int) -> str:
        if idx < len(self.participants):
            return self.participants[idx]["name"]
        return f"car{idx}"

    def player_lap(self) -> Lap | None:
        return self.cars[self.player_idx].lap

    def active_car_indices(self) -> list[int]:
        """Cars that are actually present (in TT: player + ghosts that move)."""
        if self.lapdata is None:
            return [self.player_idx]
        out = []
        for i in range(22):
            L = self.lapdata[i]
            if i == self.player_idx or (L["resultStatus"] >= 2 or (self.telemetry is not None and self.telemetry[i]["speed"] > 0)):
                out.append(i)
        return out

    # --- ingest ----------------------------------------------------------
    def on_packet(self, pid: int, p, wall: float | None = None) -> None:
        h = p["header"]
        uid = int(h["sessionUID"])
        self.last_packet_wall = wall or time.time()
        if uid != self.session_uid and uid != 0:
            self.reset(uid)
            self.packet_format = int(h["packetFormat"])
            self._new_session = True
        self.player_idx = int(h["playerCarIndex"])
        self.session_time = float(h["sessionTime"])
        if pid == 1:
            self._on_session(p)
        elif pid == 2:
            self._on_lap_data(p, float(h["sessionTime"]))
        elif pid == 6:
            self._on_telemetry(p)
        elif pid == 0:
            self._on_motion(p)
        elif pid == 7:
            self.status = LazyRecords(p["carStatusData"])
        elif pid == 5:
            self.setup = _rec_to_dict(p["carSetups"][self.player_idx])
        elif pid == 4:
            self.num_active = int(p["numActiveCars"])
            self.participants = [
                {"name": x["name"].split(b"\0")[0].decode("utf-8", "replace"), "ai": bool(x["aiControlled"]),
                 "team": int(x["teamId"]), "number": int(x["raceNumber"])}
                for x in p["participants"]
            ]
        elif pid == 3:
            code = p["eventStringCode"].decode("ascii", "replace")
            det = bytes(p["details"])
            self.events.append((float(h["sessionTime"]), code, {"raw": det}))
            self.emit("event", code, det)

    def _on_session(self, p) -> None:
        first = not self.session
        prev_sc = self.session.get("safetyCarStatus")
        s = {k: (int(p[k]) if k not in ("marshalZones", "weatherForecastSamples", "header") else None)
             for k in p.dtype.names if k not in ("marshalZones", "weatherForecastSamples", "header")}
        s["forecast"] = [
            {"offset_min": int(w["timeOffset"]), "weather": P.WEATHER.get(int(w["weather"]), "?"),
             "track_temp": int(w["trackTemperature"])}
            for w in p["weatherForecastSamples"][: int(p["numWeatherForecastSamples"])]
        ]
        self.session = s
        self.paused = bool(s["gamePaused"])
        if first or getattr(self, "_new_session", False):
            self._new_session = False
            self.emit("session_start", self)
        if prev_sc is not None and prev_sc != s["safetyCarStatus"]:
            self.emit("sc_change", P.SAFETY_CAR.get(s["safetyCarStatus"], "?"))

    def _on_telemetry(self, p) -> None:
        self.telemetry = p["carTelemetryData"]
        self.buttons = int(p["buttonStatus"])
        self._tel_count += 1
        if self.lapdata is None or self.paused:
            return
        decim = self._tel_count % self.OTHER_CAR_DECIMATE == 0
        for i in range(22):
            c = self.cars[i]
            if c.lap is None:
                continue
            is_player = i == self.player_idx
            if not is_player and not decim:
                continue
            T = self.telemetry[i]
            L = self.lapdata[i]
            vals = {
                "dist": float(L["lapDistance"]), "t": float(L["currentLapTime"]), "speed": float(T["speed"]),
                "throttle": float(T["throttle"]), "brake": float(T["brake"]), "steer": float(T["steer"]),
                "gear": float(T["gear"]), "rpm": float(T["engineRPM"]), "drs": float(T["drs"]),
            }
            if is_player:
                m = self.motion_player
                surf = T["surfaceType"]
                vals.update({
                    "x": m.get("x", 0.0), "z": m.get("z", 0.0), "yaw_rate": m.get("yaw_rate", 0.0),
                    "wheels_angle": m.get("wheels_angle", 0.0), "slip_rl": m.get("slip", (0, 0, 0, 0))[0],
                    "slip_rr": m.get("slip", (0, 0, 0, 0))[1], "slip_fl": m.get("slip", (0, 0, 0, 0))[2],
                    "slip_fr": m.get("slip", (0, 0, 0, 0))[3], "glat": m.get("glat", 0.0), "glon": m.get("glon", 0.0),
                    "surface": float(sum(1 for s in surf if int(s) in OFF_TRACK_SURFACES)),
                })
            c.lap.append(vals)

    def _on_motion(self, p) -> None:
        cm = p["carMotionData"][self.player_idx]
        self.motion_player = {
            "x": float(cm["worldPositionX"]), "z": float(cm["worldPositionZ"]),
            "yaw_rate": float(p["angularVelocityY"]), "wheels_angle": float(p["frontWheelsAngle"]),
            "slip": tuple(float(s) for s in p["wheelSlip"]), "glat": float(cm["gForceLateral"]),
            "glon": float(cm["gForceLongitudinal"]), "speed_local": float(p["localVelocityZ"]),
        }
        self.motion_all = p["carMotionData"]
        if self.trackmap is not None and self.telemetry is not None and self.lapdata is not None and not self.paused:
            self._feed_ghosts(p["carMotionData"])

    def _feed_ghosts(self, motion) -> None:
        from f1engineer.analysis.ghost import GhostTracker
        for i in range(1, min(len(self.participants), 22)):
            if i == self.player_idx or int(self.lapdata[i]["currentLapNum"]) != 0:
                continue
            tel = self.telemetry[i]
            if float(tel["speed"]) <= 0:
                continue
            g = self.ghosts.get(i)
            if g is None:
                g = self.ghosts[i] = GhostTracker(i, self.car_name(i), lambda lap: self.emit("lap_complete", lap))
            m = motion[i]
            g.feed(self.trackmap, self.session_time, float(m["worldPositionX"]), float(m["worldPositionZ"]), tel)

    def _on_lap_data(self, p, session_time: float) -> None:
        self.lapdata = p["lapData"]
        for i in range(22):
            L = self.lapdata[i]
            c = self.cars[i]
            c.is_player = i == self.player_idx
            lap_num = int(L["currentLapNum"])
            cur_t = float(L["currentLapTime"])
            dist = float(L["lapDistance"])
            if lap_num == 0 or (i != self.player_idx and int(L["resultStatus"]) < 2 and dist == 0 and cur_t == 0):
                continue
            if c.lap is None:
                c.lap = self._new_lap(i, lap_num)
            elif lap_num != c.last_lap_num:
                if lap_num > c.last_lap_num:
                    self._complete_lap(c, L)
                else:
                    self._abort(c, "lap number went backwards")
                c._s1 = c._s2 = 0
                c.lap = self._new_lap(i, lap_num)
            elif cur_t + 0.5 < c.last_cur_time:
                # Time went backwards within a lap: flashback (partial rewind) or a restart (back to ~0).
                if cur_t < 1.0 or dist < 50:
                    self._abort(c, "lap restarted")
                    c.lap = self._new_lap(i, lap_num)
                else:
                    c.lap.truncate_after(cur_t)
                    if c.is_player:
                        self.emit("flashback", cur_t)
            if int(L["sector1TimeInMS"]):
                c._s1 = int(L["sector1TimeInMS"])
            if int(L["sector2TimeInMS"]):
                c._s2 = int(L["sector2TimeInMS"])
            if int(L["currentLapInvalid"]):
                c.lap.invalid = True
            if c.is_player and int(L["sector"]) != getattr(c, "_sector", -1):
                prev = getattr(c, "_sector", -1)
                c._sector = int(L["sector"])
                if prev >= 0:
                    self.emit("sector", c, c._sector)
            c.last_lap_num = lap_num
            c.last_cur_time = cur_t
            c.last_dist = dist

    def _new_lap(self, idx: int, lap_num: int) -> Lap:
        lap = Lap(idx, lap_num)
        st = self.status[idx] if self.status and self.status[idx] else {}
        lap.assists = {"tc": st.get("tractionControl"), "abs": st.get("antiLockBrakes")}
        lap.compound = P.VISUAL_COMPOUND.get(st.get("visualTyreCompound", 0), "")
        return lap

    def _complete_lap(self, c: CarTracker, L) -> None:
        lap = c.lap
        lap.lap_time = float(L["lastLapTime"])
        s1, s2 = int(L["sector1TimeInMS"]), int(L["sector2TimeInMS"])
        lap.complete = lap.n() > 10 and lap.channels["dist"][0] < 100 and lap.lap_time > 0
        if lap.complete:
            # sector times of the finished lap are only available via the times we saw before the line
            s1_ = getattr(c, "_s1", s1)
            s2_ = getattr(c, "_s2", s2)
            lap.sector_ms = (s1_, s2_, max(0, int(lap.lap_time * 1000) - s1_ - s2_))
        lap.finalize()
        if lap.complete and c.is_player and self.trackmap is None:
            from f1engineer.analysis.trackmap import TrackMap
            self.trackmap = TrackMap.from_lap(lap.arrays, float(self.session.get("trackLength", 0)) or float(lap.arrays["dist"].max()))
            self.emit("trackmap", self.trackmap)
        c.completed.append(lap)
        del c.completed[:-(10 if c.is_player else 2)]  # bounded memory; full history goes to storage
        self.emit("lap_complete", lap)

    def _abort(self, c: CarTracker, reason: str) -> None:
        if c.lap is not None and c.is_player:
            self.emit("lap_aborted", c.lap, reason)



def _rec_to_dict(rec) -> dict:
    # void.tolist() leaves sub-array fields as ndarrays; make everything plain Python
    return {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in zip(rec.dtype.names, rec.tolist())}


class LazyRecords:
    """22-car record array that converts a car to a dict only when someone asks (hot path stays cheap)."""

    __slots__ = ("_arr", "_cache")

    def __init__(self, arr):
        self._arr = arr
        self._cache: dict[int, dict] = {}

    def __getitem__(self, i: int) -> dict:
        d = self._cache.get(i)
        if d is None:
            d = self._cache[i] = _rec_to_dict(self._arr[i])
        return d

    def __len__(self) -> int:
        return len(self._arr)

    def __bool__(self) -> bool:
        return True
