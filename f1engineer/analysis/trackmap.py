"""Track centreline (x, z -> lap distance) built from a completed player lap.

Used to place cars that send no LapData (Time Trial ghosts) onto the lap-distance axis.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

STEP_M = 5.0


class TrackMap:
    def __init__(self, dist: np.ndarray, x: np.ndarray, z: np.ndarray, track_length: float):
        self.dist, self.x, self.z = dist, x, z
        self.track_length = track_length

    @classmethod
    def from_lap(cls, arrays: dict[str, np.ndarray], track_length: float) -> "TrackMap | None":
        d = arrays.get("dist")
        if d is None or "x" not in arrays or len(d) < 100:
            return None
        order = np.argsort(d, kind="stable")
        d, x, z = d[order], arrays["x"][order], arrays["z"][order]
        keep = (d >= 0) & (d <= track_length)
        d, x, z = d[keep], x[keep], z[keep]
        d, idx = np.unique(d, return_index=True)
        x, z = x[idx], z[idx]
        grid = np.arange(0, track_length, STEP_M, dtype=np.float32)
        return cls(grid, np.interp(grid, d, x).astype(np.float32), np.interp(grid, d, z).astype(np.float32), track_length)

    def project(self, x: float, z: float, hint: int = 0) -> tuple[float, float, int]:
        """Return (lap_distance, lateral_error_m, index). Searches near `hint` (last index) first."""
        n = len(self.dist)
        lo = hint - 40
        idx = np.arange(lo, lo + 120) % n
        d2 = (self.x[idx] - x) ** 2 + (self.z[idx] - z) ** 2
        k = int(np.argmin(d2))
        if d2[k] > 400:  # > 20 m off: full search (teleport / restart)
            d2 = (self.x - x) ** 2 + (self.z - z) ** 2
            k_full = int(np.argmin(d2))
            return float(self.dist[k_full]), float(np.sqrt(d2[k_full])), k_full
        k_abs = int(idx[k])
        return float(self.dist[k_abs]), float(np.sqrt(d2[k])), k_abs

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"track_length": self.track_length, "x": self.x.round(2).tolist(),
                                    "z": self.z.round(2).tolist()}))

    @classmethod
    def load(cls, path: Path) -> "TrackMap | None":
        if not path.exists():
            return None
        j = json.loads(path.read_text())
        x = np.asarray(j["x"], np.float32)
        return cls(np.arange(len(x), dtype=np.float32) * STEP_M, x, np.asarray(j["z"], np.float32), j["track_length"])
