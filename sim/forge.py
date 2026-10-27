"""Forge spec-exact F1 2020 UDP packets for a synthetic race, built from real recorded laps.

The player car replays real laps (so lap analysis sees genuine driving); 19 AI cars follow the
best real lap time-scaled by a pace factor plus tyre degradation. Includes tyre wear, fuel, ERS,
a safety-car period, weather forecast, participants, setups and events.

  python sim/forge.py --laps 5 --sc-lap 3 --udp --speed 4     # stream to a running engine
  python sim/forge.py --out data/captures/forged_race.f1cap   # write a capture for tests
"""
from __future__ import annotations

import argparse
import pickle
import socket
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from f1engineer.state.tracker import SessionTracker  # noqa: E402
from f1engineer.telemetry import packets_2020 as P  # noqa: E402
from f1engineer.telemetry.capture import CaptureWriter, read_capture  # noqa: E402

AI_NAMES = ["HAMILTON", "BOTTAS", "VERSTAPPEN", "ALBON", "LECLERC", "VETTEL", "NORRIS", "SAINZ", "RICCIARDO",
            "OCON", "GASLY", "KVYAT", "PEREZ", "STROLL", "RAIKKONEN", "GIOVINAZZI", "MAGNUSSEN", "GROSJEAN", "RUSSELL"]
TRACK_ID, TRACK_LEN = 7, 5891


def load_real_laps(capture: str) -> tuple[list, float]:
    cache = Path("data/forge_laps.pkl")
    if cache.exists():
        return pickle.loads(cache.read_bytes())
    tr = SessionTracker()
    laps = []
    tr.on("lap_complete", lambda lap: laps.append(lap) if lap.car_idx == tr.player_idx and lap.complete else None)
    L = [0.0]
    for _, d in read_capture(capture):
        r = P.parse(d)
        if r:
            tr.on_packet(r[0], r[1])
            if tr.session.get("trackLength"):
                L[0] = float(tr.session["trackLength"])
    out = ([{k: v for k, v in lap.arrays.items()} | {"_time": lap.lap_time} for lap in laps], L[0])
    cache.write_bytes(pickle.dumps(out))
    return out


class Forge:
    def __init__(self, real_laps: list[dict], track_len: float, laps: int = 5, sc_lap: int = 0, hz: int = 60):
        self.real = real_laps
        self.L = track_len
        self.total_laps = laps
        self.sc_lap = sc_lap
        self.hz = hz
        self.base = min(real_laps, key=lambda a: a["_time"])
        b = self.base
        order = np.argsort(b["dist"])
        self.bd, self.bt = b["dist"][order], b["t"][order]
        self.bd, idx = np.unique(self.bd, return_index=True)
        self.bt = self.bt[idx]
        self.bx, self.bz = b["x"][order][idx], b["z"][order][idx]
        self.bsp, self.bthr, self.bbr = b["speed"][order][idx], b["throttle"][order][idx], b["brake"][order][idx]
        self.bgear, self.bsteer = b["gear"][order][idx], b["steer"][order][idx]
        rng = np.random.default_rng(7)
        self.pace = np.sort(rng.uniform(0.985, 1.035, 19))  # AI pace factor vs the best real lap
        self.uid = int(rng.integers(1, 2**62))
        self.frame = 0
        self.T = 0.0

    # --- car kinematics --------------------------------------------------------------------------
    def _ai_states(self, T: float, sc: bool):
        """Vectorised over the 19 AI cars: (lap_num[], lap_dist[], lap_time[], last_lap[])."""
        start = 0.25 * (np.arange(19) + 1)
        t = np.maximum(0.0, T - start)
        base = self.base["_time"] * self.pace
        lap = np.ones(19, int)
        last = np.zeros(19)
        for _ in range(self.total_laps + 2):
            dur = base * (1 + 0.004 * (lap - 1)) * np.where(sc & (lap >= max(self.sc_lap, 1)), 1.4, 1.0)
            done = t >= dur
            if not done.any():
                break
            t = np.where(done, t - dur, t)
            last = np.where(done, dur, last)
            lap = lap + done
        d = np.interp(t / dur * self.base["_time"], self.bt, self.bd)
        return lap, d, t, last

    def _player_state(self, T: float) -> tuple[int, float, dict, float]:
        t = T
        lap, last = 1, 0.0
        while True:
            a = self.real[(lap - 1) % len(self.real)]
            dur = a["_time"]
            if t < dur:
                tt = a["t"]
                ch = {k: float(np.interp(t, tt, v)) for k, v in a.items() if k not in ("_time", "t")}
                j = min(int(np.searchsorted(tt, t)), len(tt) - 1)  # digital inputs: nearest sample, not blended
                for k in ("steer", "throttle", "brake", "gear"):
                    ch[k] = float(a[k][j])
                ch["t"] = t
                return lap, ch["dist"], ch, last
            t -= dur
            last = dur
            lap += 1

    # --- packets --------------------------------------------------------------------------------
    def _header(self, rec, pid: int) -> None:
        h = rec["header"]
        h["packetFormat"], h["gameMajorVersion"], h["gameMinorVersion"] = 2020, 1, 18
        h["packetVersion"], h["packetId"], h["sessionUID"] = 1, pid, self.uid
        h["sessionTime"], h["frameIdentifier"], h["playerCarIndex"] = self.T, self.frame, 0
        h["secondaryPlayerCarIndex"] = 255

    def frame_packets(self) -> list[bytes]:
        T = self.T
        sc = bool(self.sc_lap) and self._sc_active
        p_lap, p_d, ch, p_last = self._player_state(T)
        al, ad, at, alast = self._ai_states(T, sc)
        laps = [(p_lap, p_d, ch["t"], p_last)] + list(zip(al.tolist(), ad.tolist(), at.tolist(), alast.tolist()))
        total = np.array([(lp - 1) * self.L + d for lp, d, _, _ in laps])
        order = np.argsort(-total)
        pos = np.empty(20, int)
        pos[order] = np.arange(1, 21)
        self._sc_active = bool(self.sc_lap) and self.sc_lap <= laps[int(order[0])][0] < self.sc_lap + 1
        out = []

        # Motion
        m = np.zeros((), P.MOTION)
        self._header(m, 0)
        cmd = m["carMotionData"]
        cmd["worldPositionX"][0], cmd["worldPositionZ"][0] = ch["x"], ch["z"]
        cmd["gForceLateral"][0], cmd["gForceLongitudinal"][0] = ch.get("glat", 0), ch.get("glon", 0)
        cmd["worldPositionX"][1:20] = np.interp(ad, self.bd, self.bx) + (np.arange(1, 20) % 3 - 1) * 1.5
        cmd["worldPositionZ"][1:20] = np.interp(ad, self.bd, self.bz)
        m["wheelSlip"] = [ch.get("slip_rl", 0), ch.get("slip_rr", 0), ch.get("slip_fl", 0), ch.get("slip_fr", 0)]
        m["angularVelocityY"], m["frontWheelsAngle"] = ch.get("yaw_rate", 0), ch.get("wheels_angle", 0)
        out.append(m.tobytes())

        # Lap data
        ld = np.zeros((), P.LAP_DATA)
        self._header(ld, 2)
        for i, (lp, d, lt, last) in enumerate(laps):
            r = ld["lapData"][i]
            r["currentLapNum"], r["lapDistance"], r["totalDistance"] = lp, d, (lp - 1) * self.L + d
            r["currentLapTime"], r["lastLapTime"], r["carPosition"] = lt, last, pos[i]
            r["sector"] = 0 if d < self.L / 3 else 1 if d < 2 * self.L / 3 else 2
            r["driverStatus"], r["resultStatus"], r["gridPosition"] = 1, 2, i + 1
        out.append(ld.tobytes())

        # Telemetry
        tel = np.zeros((), P.CAR_TELEMETRY_PACKET)
        self._header(tel, 6)
        td = tel["carTelemetryData"]
        td["speed"][0], td["throttle"][0], td["brake"][0] = ch["speed"], ch["throttle"], ch["brake"]
        td["steer"][0], td["gear"][0], td["engineRPM"][0] = ch["steer"], round(ch["gear"]), ch["rpm"]
        td["surfaceType"][0] = [7, 7, 0, 0] if ch.get("surface", 0) >= 2 else [0, 0, 0, 0]
        td["speed"][1:20] = np.interp(ad, self.bd, self.bsp) * (0.6 if sc else 1.0)
        td["throttle"][1:20], td["brake"][1:20] = np.interp(ad, self.bd, self.bthr), np.interp(ad, self.bd, self.bbr)
        td["gear"][1:20], td["steer"][1:20] = np.round(np.interp(ad, self.bd, self.bgear)), np.interp(ad, self.bd, self.bsteer)
        td["engineRPM"][1:20] = 10500
        wl = np.array([lp - 1 for lp, _, _, _ in laps])[:, None]
        td["tyresSurfaceTemperature"][:20] = np.array([95, 96, 99, 101]) + wl
        td["tyresInnerTemperature"][:20] = [100, 100, 102, 103]
        td["brakesTemperature"][:20] = [550, 550, 600, 600]
        td["tyresPressure"][:20] = [21.5, 21.5, 23.0, 23.0]
        out.append(tel.tobytes())

        # Status
        stt = np.zeros((), P.CAR_STATUS_PACKET)
        self._header(stt, 7)
        for i, (lp, d, lt, _) in enumerate(laps):
            s = stt["carStatusData"][i]
            progress = (lp - 1) + d / self.L
            s["tractionControl"], s["antiLockBrakes"], s["fuelMix"], s["frontBrakeBias"] = 1, 0, 1, 56
            s["fuelInTank"], s["fuelCapacity"] = 30 - 1.6 * progress, 110
            s["fuelRemainingLaps"] = 0.4 - 0.05 * progress if i == 0 else 0.5
            s["maxRPM"], s["idleRPM"], s["maxGears"], s["drsAllowed"] = 13000, 4000, 8, 0
            w = min(99, 9.0 * progress) if i == 0 else min(99, 6 * progress)
            s["tyresWear"] = [int(w), int(w), int(w * 1.1), int(w * 1.1)]
            s["actualTyreCompound"], s["visualTyreCompound"], s["tyresAgeLaps"] = 16, 16, int(progress)
            s["ersStoreEnergy"], s["ersDeployMode"] = 2.5e6, 1
        out.append(stt.tobytes())

        # Lower-rate packets
        if self.frame % 30 == 0:
            se = np.zeros((), P.SESSION)
            self._header(se, 1)
            se["weather"], se["trackTemperature"], se["airTemperature"] = 1, 32, 22
            se["totalLaps"], se["trackLength"], se["sessionType"], se["trackId"] = self.total_laps, int(self.L), 10, TRACK_ID
            se["sessionTimeLeft"], se["sessionDuration"], se["pitSpeedLimit"] = 3600, 7200, 80
            se["safetyCarStatus"] = 1 if self._sc_active else 0
            se["numWeatherForecastSamples"] = 3
            for k, (off, w) in enumerate([(0, 1), (10, 2), (20, 3)]):
                f = se["weatherForecastSamples"][k]
                f["sessionType"], f["timeOffset"], f["weather"], f["trackTemperature"], f["airTemperature"] = 10, off, w, 30, 21
            out.append(se.tobytes())
            cs = np.zeros((), P.CAR_SETUPS)
            self._header(cs, 5)
            c = cs["carSetups"][0]
            c["frontWing"], c["rearWing"], c["onThrottle"], c["offThrottle"] = 6, 5, 70, 60
            c["frontAntiRollBar"], c["rearAntiRollBar"], c["brakePressure"], c["brakeBias"] = 6, 5, 100, 58
            c["frontSuspension"], c["rearSuspension"], c["fuelLoad"] = 5, 4, 30
            out.append(cs.tobytes())
        if self.frame % 300 == 0:
            pa = np.zeros((), P.PARTICIPANTS)
            self._header(pa, 4)
            pa["numActiveCars"] = 20
            for i in range(20):
                q = pa["participants"][i]
                q["aiControlled"], q["teamId"], q["raceNumber"] = (0 if i == 0 else 1), i // 2, i + 2
                q["name"] = (b"Player" if i == 0 else AI_NAMES[i - 1].encode())
            out.append(pa.tobytes())
        if self.frame == 0:
            ev = np.zeros((), P.EVENT)
            self._header(ev, 3)
            ev["eventStringCode"] = b"SSTA"
            out.append(ev.tobytes())
        self.frame += 1
        self.T += 1.0 / self.hz
        return out

    def run(self, sink, until_lap: int | None = None) -> None:
        self._sc_active = False
        end = until_lap or self.total_laps
        while True:
            pk = self.frame_packets()
            for b in pk:
                sink(self.T, b)
            if self._player_state(self.T)[0] > end:
                return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", default="data/captures/real_f1_2020_session1.f1cap")
    ap.add_argument("--laps", type=int, default=5)
    ap.add_argument("--sc-lap", type=int, default=3)
    ap.add_argument("--udp", action="store_true")
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    real, L = load_real_laps(a.capture)
    f = Forge(real, L or TRACK_LEN, a.laps, a.sc_lap)
    if a.out:
        w = CaptureWriter(a.out)
        f.run(lambda t, b: w.write(t, b))
        w.close()
        print(f"wrote {a.out}")
    if a.udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        w0 = time.time()

        def send(t, b):
            wait = t / a.speed - (time.time() - w0)
            if wait > 0:
                time.sleep(wait)
            sock.sendto(b, ("127.0.0.1", a.port))
        f.run(send)


if __name__ == "__main__":
    main()
