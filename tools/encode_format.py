"""Re-encode F1 2020 packets as a newer game's UDP format (2021..2026): the inverse of telemetry.formats.

Used by tests (a real 2020 session must give identical laps in every format) and to try the dashboard/engineer
as if you were playing F1 22/23/24/25:

  uv run python tools/encode_format.py data/captures/x.f1cap 2024 data/captures/x_as_2024.f1cap
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from f1engineer.telemetry import formats as F  # noqa: E402
from f1engineer.telemetry import packets_2020 as C  # noqa: E402


def _copy(dst, src, names=None):
    for n in (names or dst.dtype.names):
        if n in src.dtype.names:
            dst[n] = src[n]


class Encoder:
    def __init__(self, fmt: int):
        self.fmt = fmt
        self.lay = F._layout(fmt)
        self.buttons = None
        inv = F.SESSION_MAP.get(fmt, {})
        self.session_inv = {v: k for k, v in inv.items()}

    def _hdr(self, dt, h, pid):
        out = np.zeros((), dt["header"])
        _copy(out, h)
        out["packetFormat"] = self.fmt
        out["packetId"] = pid
        if "gameYear" in out.dtype.names:
            out["gameYear"] = min(self.fmt, 2025) % 100
        return out

    def _pack(self, pid, fill) -> bytes:
        dt = self.lay[pid][0]
        rec = np.zeros((), dt)
        fill(rec, dt)
        return rec.tobytes()

    def encode(self, data: bytes) -> list[bytes]:
        r = C.parse(data)
        if r is None:
            return []
        pid, p = r
        h = p["header"]
        out = []
        if pid == 0:
            def f0(rec, dt):
                rec["header"] = self._hdr(dt, h, 0)
                rec["carMotionData"] = p["carMotionData"]
                if self.fmt < 2023:
                    _copy(rec, p, [n for n in dt.names if n not in ("header", "carMotionData")])
            out.append(self._pack(0, f0))
            if self.fmt >= 2023:
                def f13(rec, dt):
                    rec["header"] = self._hdr(dt, h, 13)
                    _copy(rec["ex"], p)
                    rec["ex"]["wheelSlipRatio"] = p["wheelSlip"]
                out.append(self._pack(13, f13))
        elif pid == 1:
            def f1(rec, dt):
                rec["header"] = self._hdr(dt, h, 1)
                _copy(rec, p, F._SESSION_NAMES)
                rec["sessionType"] = self.session_inv.get(int(p["sessionType"]), int(p["sessionType"]))
                n = int(p["numWeatherForecastSamples"])
                for k in ("timeOffset", "weather", "trackTemperature", "airTemperature"):
                    rec["weatherForecastSamples"][k][:n] = p["weatherForecastSamples"][k][:n]
                rec["weatherForecastSamples"]["sessionType"][:n] = [
                    self.session_inv.get(int(v), int(v)) for v in p["weatherForecastSamples"]["sessionType"][:n]]
            out.append(self._pack(1, f1))
        elif pid == 2:
            def f2(rec, dt):
                rec["header"] = self._hdr(dt, h, 2)
                src, dst = p["lapData"], rec["lapData"]
                _copy(dst, src)
                dst["lastLapTimeInMS"] = np.round(src["lastLapTime"] * 1000)
                dst["currentLapTimeInMS"] = np.round(src["currentLapTime"] * 1000)
                if "sector1TimeMinutes" in dst.dtype.names:
                    for s in ("sector1", "sector2"):
                        ms = src[f"{s}TimeInMS"].astype(np.int64)
                        dst[f"{s}TimeInMS"], dst[f"{s}TimeMinutes"] = ms % 60000, ms // 60000
            out.append(self._pack(2, f2))
        elif pid == 3:
            def f3(rec, dt):
                rec["header"] = self._hdr(dt, h, 3)
                rec["eventStringCode"] = p["eventStringCode"]
                rec["details"][:7] = p["details"]
            out.append(self._pack(3, f3)[:{2021: 36, 2022: 40}.get(self.fmt, 45)])
        elif pid == 4:
            def f4(rec, dt):
                rec["header"] = self._hdr(dt, h, 4)
                rec["numActiveCars"] = p["numActiveCars"]
                _copy(rec["participants"], p["participants"])
                if self.fmt >= 2025:   # 32-char names
                    rec["participants"]["name"] = [bytes(n)[:31] for n in p["participants"]["name"]]
            out.append(self._pack(4, f4))
        elif pid == 5:
            def f5(rec, dt):
                rec["header"] = self._hdr(dt, h, 5)
                _copy(rec["carSetups"], p["carSetups"])
            out.append(self._pack(5, f5))
        elif pid == 6:
            b = int(p["buttonStatus"])
            if b != self.buttons:   # 2021+: buttons are BUTN events
                self.buttons = b

                def fb(rec, dt):
                    rec["header"] = self._hdr(dt, h, 3)
                    rec["eventStringCode"] = b"BUTN"
                    rec["details"][:4] = np.frombuffer(b.to_bytes(4, "little"), np.uint8)
                out.append(self._pack(3, fb)[:{2021: 36, 2022: 40}.get(self.fmt, 45)])

            def f6(rec, dt):
                rec["header"] = self._hdr(dt, h, 6)
                _copy(rec["carTelemetryData"], p["carTelemetryData"])
                _copy(rec, p, ["mfdPanelIndex", "mfdPanelIndexSecondaryPlayer", "suggestedGear"])
            out.append(self._pack(6, f6))
        elif pid == 7:
            def f10(rec, dt):
                rec["header"] = self._hdr(dt, h, 10)
                _copy(rec["carDamageData"], p["carStatusData"])
                rec["carDamageData"]["tyresWear"] = p["carStatusData"]["tyresWear"]
            out.append(self._pack(10, f10))

            def f7(rec, dt):
                rec["header"] = self._hdr(dt, h, 7)
                _copy(rec["carStatusData"], p["carStatusData"])
            out.append(self._pack(7, f7))
        return out


def main():
    from f1engineer.telemetry.capture import CaptureWriter, read_capture
    src, fmt, dst = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    enc = Encoder(fmt)
    w = CaptureWriter(Path(dst))
    n = 0
    for t, d in read_capture(Path(src)):
        for b in enc.encode(d):
            w.write(b, t)
            n += 1
    w.close()
    print(f"wrote {n} packets as format {fmt} to {dst}")


if __name__ == "__main__":
    main()
