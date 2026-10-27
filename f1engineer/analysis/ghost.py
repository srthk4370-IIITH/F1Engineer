"""Rebuild Time Trial ghost laps (ghost cars send telemetry + motion but no LapData)."""
from __future__ import annotations

from typing import Callable

from f1engineer.analysis.trackmap import TrackMap
from f1engineer.state.tracker import Lap


class GhostTracker:
    def __init__(self, idx: int, name: str, on_lap: Callable[[Lap], None]):
        self.idx, self.name, self.on_lap = idx, name, on_lap
        self.lap: Lap | None = None
        self.t0 = 0.0
        self.last_d = None
        self.lap_num = 0
        self.hint = 0

    def feed(self, tmap: TrackMap, session_time: float, x: float, z: float, tel) -> None:
        if float(tel["speed"]) < 1:
            return
        d, err, self.hint = tmap.project(x, z, self.hint)
        if err > 30:
            return
        L = tmap.track_length
        if self.last_d is not None and self.last_d > L - 200 and d < 200:
            # crossed the line: close the lap (interpolated crossing time is good to ~1 frame)
            if self.lap is not None and self.lap.n() > 50 and self.lap.channels["dist"][0] < 150:
                self.lap.lap_time = session_time - self.t0
                self.lap.complete = True
                self.on_lap(self.lap.finalize())
            self.lap_num += 1
            self.lap = Lap(self.idx, self.lap_num)
            self.t0 = session_time
        elif self.last_d is not None and abs(d - self.last_d) > 300:
            self.lap = None  # ghost reset/teleported
        self.last_d = d
        if self.lap is not None:
            self.lap.append({"dist": d, "t": session_time - self.t0, "speed": float(tel["speed"]),
                             "throttle": float(tel["throttle"]), "brake": float(tel["brake"]),
                             "steer": float(tel["steer"]), "gear": float(tel["gear"]),
                             "rpm": float(tel["engineRPM"]), "drs": float(tel["drs"]),
                             "x": x, "z": z})
