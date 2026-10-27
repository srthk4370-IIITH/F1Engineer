"""Distance-domain lap maths: resampling, delta-time, corner detection and per-corner metrics."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

GRID_M = 2.0
WHEELBASE_M = 3.6


def resample(arrays: dict[str, np.ndarray], track_length: float) -> dict[str, np.ndarray] | None:
    """Put a lap on a fixed 2 m distance grid. Returns None if the lap doesn't cover the track."""
    d = arrays["dist"].astype(np.float64)
    ok = d >= -1
    d = d[ok]
    if len(d) < 50 or d[-1] - d[0] < 0.9 * track_length:
        return None
    d = np.maximum.accumulate(d)
    d, idx = np.unique(d, return_index=True)
    grid = np.arange(0.0, track_length, GRID_M)
    out = {"dist": grid}
    for k, v in arrays.items():
        if k != "dist":
            out[k] = np.interp(grid, d, v[ok][idx])
    return out


def smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return x
    k = np.ones(n) / n
    return np.convolve(np.pad(x, (n // 2, n - 1 - n // 2), mode="edge"), k, mode="valid")


@dataclass
class Corner:
    num: int
    name: str
    apex: float          # metres
    start: float         # segment start (end of previous straight)
    end: float           # segment end (end of following straight)


def detect_corners(ref: dict[str, np.ndarray], names: dict[float, str] | None = None,
                   min_drop_kmh: float = 10.0) -> list[Corner]:
    """Apexes = speed minima with a meaningful drop from the preceding straight.

    names: optional {apex_distance_m: "Corner name"} table; matched within 120 m.
    """
    sp = smooth(ref["speed"], 10)  # 20 m window
    n = len(sp)
    w = int(60 / GRID_M)
    cands = []
    for i in range(w, n - w):
        if sp[i] == sp[max(0, i - w): i + w].min():
            back = sp[max(0, i - int(500 / GRID_M)): i].max(initial=sp[i])
            if back - sp[i] >= min_drop_kmh:
                cands.append(i)
    apex_idx: list[int] = []
    for i in cands:  # merge minima of one corner complex (speed barely recovers between them)
        if apex_idx and (i - apex_idx[-1] < int(60 / GRID_M)
                         or sp[apex_idx[-1]:i].max() - max(sp[i], sp[apex_idx[-1]]) < 4):
            if sp[i] < sp[apex_idx[-1]]:
                apex_idx[-1] = i
        else:
            apex_idx.append(i)
    d = ref["dist"]
    bounds = [0]
    for a, b in zip(apex_idx, apex_idx[1:]):
        bounds.append(a + int(np.argmax(sp[a:b])))
    bounds.append(n - 1)
    corners = []
    for k, a in enumerate(apex_idx):
        name = f"Turn {k + 1}"
        if names:
            near = min(names, key=lambda x: abs(x - d[a]))
            if abs(near - d[a]) < 120:
                name = names[near]
        corners.append(Corner(k + 1, name, float(d[a]), float(d[bounds[k]]), float(d[bounds[k + 1]])))
    return corners


@dataclass
class CornerMetrics:
    corner: Corner
    time_loss: float = 0.0        # + = slower than reference over the segment
    entry_loss: float = 0.0
    exit_loss: float = 0.0
    brake_point: float | None = None   # metres before apex where braking/lift started
    ref_brake_point: float | None = None
    min_speed: float = 0.0
    ref_min_speed: float = 0.0
    throttle_on: float | None = None   # metres after apex where full throttle resumed
    ref_throttle_on: float | None = None
    exit_speed: float = 0.0
    ref_exit_speed: float = 0.0
    apex_gear: int = 0
    ref_apex_gear: int = 0
    lockup: bool = False
    wheelspin: bool = False
    off_track: bool = False
    coast_m: float = 0.0
    understeer: float = 0.0       # mid-corner balance metric, rad (+ under / - over)
    flags: list[str] = field(default_factory=list)


def _first(mask: np.ndarray) -> int | None:
    idx = np.flatnonzero(mask)
    return int(idx[0]) if len(idx) else None


def _phase_points(r, s, a, e):
    """Brake (or lift) start index in [s, a] and full-throttle return index in [a, e]."""
    bp = _first(r["brake"][s:a + 1] > 0.1)
    if bp is None:
        bp = _first(r["throttle"][s:a + 1] < 0.5)
    th = _first(r["throttle"][a:e + 1] > 0.9)
    return (s + bp if bp is not None else None), (a + th if th is not None else None)


def corner_metrics(lap: dict[str, np.ndarray], ref: dict[str, np.ndarray], delta: np.ndarray,
                   corners: list[Corner]) -> list[CornerMetrics]:
    out = []
    for c in corners:
        s, a, e = (int(round(v / GRID_M)) for v in (c.start, c.apex, c.end))
        e = min(e, len(delta) - 1)
        m = CornerMetrics(c)
        m.time_loss = float(delta[e] - delta[s])
        m.entry_loss = float(delta[a] - delta[s])
        m.exit_loss = float(delta[e] - delta[a])
        win = slice(max(s, a - 30), min(e, a + 30) + 1)
        m.min_speed = float(lap["speed"][win].min())
        m.ref_min_speed = float(ref["speed"][win].min())
        bp, th = _phase_points(lap, s, a, e)
        rbp, rth = _phase_points(ref, s, a, e)
        m.brake_point = (a - bp) * GRID_M if bp is not None else None
        m.ref_brake_point = (a - rbp) * GRID_M if rbp is not None else None
        m.throttle_on = (th - a) * GRID_M if th is not None else None
        m.ref_throttle_on = (rth - a) * GRID_M if rth is not None else None
        x = min(e, a + 60)
        m.exit_speed, m.ref_exit_speed = float(lap["speed"][x]), float(ref["speed"][x])
        m.apex_gear, m.ref_apex_gear = int(round(lap["gear"][a])), int(round(ref["gear"][a]))
        seg = slice(s, e + 1)
        if "slip_fl" in lap:
            braking = lap["brake"][seg] > 0.3
            fslip = np.maximum(np.abs(lap["slip_fl"][seg]), np.abs(lap["slip_fr"][seg]))
            rslip = np.maximum(np.abs(lap["slip_rl"][seg]), np.abs(lap["slip_rr"][seg]))
            # calibrated on real F1 2020 data: ABS-on braking peaks ~0.09, real lock-ups saturate at 1.0
            m.lockup = bool(((fslip > 0.2) & braking).sum() >= 3)
            m.wheelspin = bool(((rslip > 0.12) & (lap["throttle"][seg] > 0.5)).sum() >= 8)
            m.off_track = bool((lap["surface"][seg] >= 2).sum() >= 6)
            mid = slice(max(s, a - 20), min(e, a + 20) + 1)
            v = np.maximum(lap["speed"][mid] / 3.6, 5)
            wa = lap["wheels_angle"][mid]
            yr = lap["yaw_rate"][mid]
            sgn = np.sign(wa)
            # game yaw rate sign is opposite to wheel angle sign (verified on real data)
            us = (np.abs(wa) + sgn * yr * WHEELBASE_M / v)
            m.understeer = float(np.median(us[np.abs(wa) > 0.01])) if (np.abs(wa) > 0.01).any() else 0.0
        coast = (lap["throttle"][seg] < 0.05) & (lap["brake"][seg] < 0.05) & (lap["speed"][seg] > 60)
        m.coast_m = float(coast.sum() * GRID_M)
        out.append(m)
    return out


def delta_trace(lap: dict[str, np.ndarray], ref: dict[str, np.ndarray]) -> np.ndarray:
    return lap["t"] - ref["t"]
