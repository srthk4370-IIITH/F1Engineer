"""Turns a completed lap into a structured, fact-only coaching report (no LLM here).

The report is what templates speak mid-lap and what the LLM gets via tools.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from f1engineer.analysis import lapmath as LM

TRACK_NAMES = json.loads((Path(__file__).parent.parent / "data" / "tracks.json").read_text())


def corner_names(track: str) -> dict[float, str]:
    return {float(k): v for k, v in TRACK_NAMES.get(track, {}).items()}


def detect_input_device(arrays: dict[str, np.ndarray]) -> str:
    """Keyboard steering/pedals are digital in F1 2020 (steer only -1/0/+1, throttle 0/1)."""
    moving = arrays["speed"] > 20
    if moving.sum() < 100:
        return "unknown"
    st = np.abs(arrays["steer"][moving])
    th = arrays["throttle"][moving]
    st_digital = ((st < 0.01) | (st > 0.99)).mean()
    th_digital = ((th < 0.02) | (th > 0.98)).mean()
    if st_digital > 0.97 and th_digital > 0.97:
        return "keyboard"
    if st_digital > 0.97:
        return "keyboard+analog pedals"
    return "controller/wheel"


@dataclass
class LapReport:
    lap_num: int
    lap_time: float
    ref_label: str
    ref_time: float
    delta: float
    valid: bool
    input_device: str
    assists: dict
    compound: str
    corners: list[dict] = field(default_factory=list)       # all corners, track order
    losses: list[dict] = field(default_factory=list)        # top losses with causes
    gains: list[dict] = field(default_factory=list)         # where you were better than the reference
    balance: dict = field(default_factory=dict)
    mistakes: list[str] = field(default_factory=list)
    sectors: tuple = (0, 0, 0)

    def to_dict(self) -> dict:
        return asdict(self)


def _causes(m: LM.CornerMetrics) -> list[str]:
    """Human-readable, numeric reasons for a corner's time difference (largest effects first)."""
    c = []
    if m.off_track:
        c.append("ran wide off the track")
    if m.lockup:
        c.append("locked the fronts on the brakes")
    if m.brake_point is not None and m.ref_brake_point is not None:
        diff = m.brake_point - m.ref_brake_point
        if diff > 10:
            c.append(f"braked {diff:.0f} m earlier")
        elif diff < -10 and m.min_speed < m.ref_min_speed - 3:
            c.append(f"braked {-diff:.0f} m later and overshot the apex")
    dv = m.min_speed - m.ref_min_speed
    if dv < -4:
        c.append(f"apex speed {-dv:.0f} km/h lower")
    elif dv > 6 and m.exit_loss > 0.05:
        c.append(f"carried {dv:.0f} km/h too much into the apex and compromised the exit")
    if m.throttle_on is not None and m.ref_throttle_on is not None and m.throttle_on - m.ref_throttle_on > 15:
        c.append(f"full throttle {m.throttle_on - m.ref_throttle_on:.0f} m later")
    if m.wheelspin:
        c.append("wheelspin on exit")
    if m.coast_m > 25:
        c.append(f"coasted {m.coast_m:.0f} m (neither pedal)")
    if m.apex_gear and m.ref_apex_gear and m.apex_gear != m.ref_apex_gear:
        c.append(f"gear {m.apex_gear} at apex vs {m.ref_apex_gear}")
    dx = m.exit_speed - m.ref_exit_speed
    if dx < -5:
        c.append(f"exit speed {-dx:.0f} km/h down")
    return c


def build_report(lap, lap_rs: dict, ref_rs: dict, ref_label: str, ref_time: float,
                 corners: list[LM.Corner]) -> LapReport:
    delta = LM.delta_trace(lap_rs, ref_rs)
    metrics = LM.corner_metrics(lap_rs, ref_rs, delta, corners)
    rep = LapReport(
        lap_num=lap.lap_num, lap_time=round(lap.lap_time, 3), ref_label=ref_label, ref_time=round(ref_time, 3),
        delta=round(lap.lap_time - ref_time, 3), valid=not lap.invalid,
        input_device=detect_input_device(lap.arrays), assists=lap.assists, compound=lap.compound,
        sectors=lap.sector_ms,
    )
    for m in metrics:
        d = {
            "corner": m.corner.name, "num": m.corner.num, "apex_m": round(m.corner.apex),
            "time_loss": round(m.time_loss, 3), "entry_loss": round(m.entry_loss, 3), "exit_loss": round(m.exit_loss, 3),
            "min_speed": round(m.min_speed), "ref_min_speed": round(m.ref_min_speed),
            "brake_point_m": None if m.brake_point is None else round(m.brake_point),
            "ref_brake_point_m": None if m.ref_brake_point is None else round(m.ref_brake_point),
            "throttle_on_m": None if m.throttle_on is None else round(m.throttle_on),
            "ref_throttle_on_m": None if m.ref_throttle_on is None else round(m.ref_throttle_on),
            "exit_speed": round(m.exit_speed), "ref_exit_speed": round(m.ref_exit_speed),
            "lockup": m.lockup, "wheelspin": m.wheelspin, "off_track": m.off_track,
            "understeer": round(m.understeer, 4), "causes": _causes(m),
        }
        rep.corners.append(d)
        if m.lockup:
            rep.mistakes.append(f"lock-up at {m.corner.name}")
        if m.off_track:
            rep.mistakes.append(f"off track at {m.corner.name}")
    by_loss = sorted(rep.corners, key=lambda d: -d["time_loss"])
    rep.losses = [d for d in by_loss if d["time_loss"] > 0.05][:4]
    rep.gains = [d for d in sorted(rep.corners, key=lambda d: d["time_loss"]) if d["time_loss"] < -0.05][:3]
    rep.balance = balance_summary(metrics, lap_rs)
    return rep


def self_review(lap_rs: dict, corners: list[LM.Corner]) -> list[dict]:
    """Mistakes visible without any reference lap, worst first: lock-ups, off-track, wheelspin, coasting, late power."""
    metrics = LM.corner_metrics(lap_rs, lap_rs, np.zeros(len(lap_rs["dist"])), corners)
    out = []
    for m in metrics:
        name = m.corner.name
        if m.lockup:
            out.append({"corner": name, "severity": 3, "kind": "lockup", "issue": f"locked up into {name}",
                        "tip": "smoother brake release as you turn in"})
        if m.off_track:
            out.append({"corner": name, "severity": 3, "kind": "off", "issue": f"ran wide at {name}",
                        "tip": "a metre tighter on exit, keep it inside the lines"})
        if m.wheelspin:
            out.append({"corner": name, "severity": 2, "kind": "wheelspin", "issue": f"wheelspin out of {name}",
                        "tip": "be gentler on the throttle on exit"})
        if m.coast_m >= 30:
            out.append({"corner": name, "severity": 2, "kind": "coast", "issue": f"coasted {m.coast_m:.0f} metres into {name}",
                        "tip": "no coasting, it's brake or throttle"})
        if m.throttle_on is not None and m.throttle_on > 120:
            out.append({"corner": name, "severity": 1, "kind": "late_throttle", "issue": f"full throttle {m.throttle_on:.0f} metres after the apex of {name}",
                        "tip": "get back to full throttle sooner"})
    out.sort(key=lambda x: -x["severity"])
    return out


def balance_summary(metrics: list[LM.CornerMetrics], lap_rs: dict) -> dict:
    """Relative mid-corner balance by corner speed class.

    The kinematic understeer metric (front minus rear slip angle, rad) has a car-specific baseline,
    so we report how each speed class deviates from this lap's overall median: that is what a setup
    change (wings vs mechanical grip) can actually shift.
    """
    allv = [m.understeer for m in metrics if m.understeer != 0]
    if len(allv) < 3:
        return {}
    base = float(np.median(allv))
    out = {"baseline": round(base, 4)}
    for cls, lo, hi in (("slow", 0, 140), ("medium", 140, 220), ("fast", 220, 400)):
        vals = [m.understeer for m in metrics if lo <= m.min_speed < hi and m.understeer != 0]
        if vals:
            dv = float(np.median(vals)) - base
            out[cls] = {"relative": round(dv, 4), "corners": len(vals),
                        "tendency": "more understeer" if dv > 0.004 else "more oversteer" if dv < -0.004 else "balanced"}
    return out


def consistency(reports: list[LapReport]) -> list[dict]:
    """Per-corner spread of time loss across laps: high spread = inconsistent = fix first."""
    if len(reports) < 2:
        return []
    by: dict[str, list[float]] = {}
    for r in reports:
        for c in r.corners:
            by.setdefault(c["corner"], []).append(c["time_loss"])
    out = [{"corner": k, "spread": round(float(np.std(v)), 3), "avg_loss": round(float(np.mean(v)), 3)}
           for k, v in by.items()]
    return sorted(out, key=lambda d: -d["spread"])
