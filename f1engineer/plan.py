"""Practice plans: the engineer sets targets, the engine measures every lap against them and coaches the gap.

The AI designs the plan (start_practice_plan tool) from the driver's data; tracking and feedback are deterministic,
so every call after a lap is instant and exact, and it closes the loop:
  miss      -> the number, the target, WHY it was missed (from that lap's corner analysis) and the fix
  closer    -> "closer: 112 -> 113"
  stuck     -> after two clear misses a halfway target ("let's get 114 first") instead of repeating the impossible
  done      -> raise the bar once for a stretch goal, then move on

Metrics (what `target` means)
  corner  corner_loss (s lost, max)  entry_loss (s, max)  exit_loss (s, max)  apex_speed (km/h, min)
          exit_speed (km/h, min)  brake_point (m before apex, max = brake later)  throttle_on (m after apex, max)
          coasting (m in the corner, max)  apex_gear (gear, exact)  no_lockup  no_wheelspin  no_off
  lap     lap_time (s, max)  delta_ref (s vs reference, max)  sector (sector 1-3 time in s, max; use `sector`)
          clean_lap  valid_lap  mistakes_max (count, max)  coasting_lap (m per lap, max)
          consistency (`hits` laps in a row within `target` s)
"""
from __future__ import annotations

import time

from f1engineer import radio

CORNER = {"corner_loss", "entry_loss", "exit_loss", "apex_speed", "exit_speed", "brake_point", "throttle_on",
          "coasting", "apex_gear", "no_lockup", "no_wheelspin", "no_off"}
LAP = {"lap_time", "delta_ref", "sector", "clean_lap", "valid_lap", "mistakes_max", "coasting_lap", "consistency"}
METRICS = tuple(sorted(CORNER | LAP))
HIGHER_IS_BETTER = {"apex_speed", "exit_speed"}
BOOL = {"no_lockup", "no_wheelspin", "no_off", "clean_lap", "valid_lap"}
NUMERIC = {"corner_loss", "entry_loss", "exit_loss", "apex_speed", "exit_speed", "brake_point", "throttle_on", "coasting",
           "lap_time", "delta_ref", "sector", "mistakes_max", "coasting_lap"}
STRETCH = {"apex_speed": 2, "exit_speed": 3, "brake_point": -5, "throttle_on": -5, "corner_loss": -0.05,
           "lap_time": -0.2, "delta_ref": -0.1, "sector": -0.1, "coasting": -10, "coasting_lap": -40}

FIX = [  # cause fragment -> how to fix it
    ("locked", "ease off the brake as you turn in"), ("ran wide", "a metre tighter on exit"),
    ("earlier", "brake at a later marker, the car will stop"), ("overshot", "brake a touch earlier and hit the apex"),
    ("apex speed", "release the brake earlier and carry more speed to the apex"), ("too much into the apex", "slower in, faster out"),
    ("full throttle", "get on the power earlier, unwind the steering first"), ("coasted", "no coasting: brake or throttle"),
    ("wheelspin", "smoother on the throttle, short-shift"), ("exit speed", "straighten the wheel earlier on exit"),
]


def _num(v: float, metric: str) -> str:
    return {"apex_speed": f"{v:.0f}", "exit_speed": f"{v:.0f}", "brake_point": f"{v:.0f} metres",
            "throttle_on": f"{v:.0f} metres", "coasting": f"{v:.0f} metres", "coasting_lap": f"{v:.0f} metres",
            "lap_time": radio.short_t(v), "sector": f"{v:.2f}", "apex_gear": f"gear {int(v)}",
            "mistakes_max": f"{int(v)}"}.get(metric, f"{v:+.2f}" if metric in ("corner_loss", "entry_loss", "exit_loss", "delta_ref") else f"{v:.2f}")


def describe(s: dict) -> str:
    c, t, m = s.get("corner", ""), s.get("target"), s["metric"]
    d = {
        "corner_loss": f"{c}, lose less than {radio.fmt_d(t or 0.05)}", "entry_loss": f"{c} entry, lose less than {radio.fmt_d(t or 0.05)}",
        "exit_loss": f"{c} exit, lose less than {radio.fmt_d(t or 0.05)}", "apex_speed": f"{c}, apex speed {t:.0f} or better" if t else c,
        "exit_speed": f"{c}, exit speed {t:.0f} or better" if t else c,
        "brake_point": f"{c}, brake no earlier than {t:.0f} metres before the apex" if t else c,
        "throttle_on": f"{c}, full throttle within {t:.0f} metres of the apex" if t else c,
        "coasting": f"{c}, less than {t:.0f} metres of coasting" if t is not None else c,
        "apex_gear": f"{c} in gear {int(t)}" if t else c,
        "no_lockup": f"{c} without a lock-up", "no_wheelspin": f"{c} without wheelspin", "no_off": f"{c} inside the white lines",
        "lap_time": f"a {radio.short_t(t)} or better" if t else "a quicker lap",
        "delta_ref": f"within {radio.fmt_d(t or 0.3)} of your reference" if (t or 0) >= 0 else f"{radio.fmt_d(t)} under your reference",
        "sector": f"sector {s.get('sector', 1)} in {t:.2f} or better" if t else f"a better sector {s.get('sector', 1)}",
        "clean_lap": "a clean lap: no lock-ups, no offs, valid", "valid_lap": "a valid lap, no track limits",
        "mistakes_max": f"a lap with at most {int(t or 0)} mistakes", "coasting_lap": f"under {t:.0f} metres of coasting in a lap" if t else "less coasting",
        "consistency": f"{s.get('hits', 3)} laps in a row within {radio.fmt_d(t or 0.3)} of each other",
    }
    return d.get(m, m)


def sanitise(steps: list[dict], reports: list) -> tuple[list[dict], list[str]]:
    """Targets must be real numbers for this driver (absolute, near what they actually do)."""
    fixes, out = [], []
    best_lap = min((r.lap_time for r in reports), default=None)
    for s in steps:
        s = dict(s)
        m, c, t = s.get("metric"), (s.get("corner") or "").lower(), s.get("target")
        vals = [x for r in reports for x in r.corners if c and c in x["corner"].lower()]
        field = {"apex_speed": "min_speed", "exit_speed": "exit_speed"}.get(m)
        if field and vals:
            best = max(v[field] for v in vals)
            if t is None or not (0.85 * best <= t <= 1.15 * best):
                s["target"] = round(best + 3)
                fixes.append(f"{s.get('corner')}: {m} target {t} -> {s['target']} km/h (best so far {best:.0f})")
        elif m in ("brake_point", "throttle_on"):
            cur = [v.get("brake_point_m" if m == "brake_point" else "throttle_on_m") for v in vals]
            cur = [x for x in cur if x is not None]
            best = min(cur) if cur else None
            new = t
            if t is None or not 0 <= t <= 300:
                new = max(0, round(best - 5)) if best is not None else 60
            elif best is not None and m == "brake_point" and t < 0.5 * best:
                new = max(0, round(best - t))          # "12" meant "12 m later than now", not 12 m before the apex
            elif best is not None and t >= best:
                new = max(0, round(best - (5 if m == "brake_point" else 10)))   # already achieved: make it a target
            if new != t:
                s["target"] = new
                fixes.append(f"{s.get('corner')}: {m} target {t} -> {new} m (now {best:.0f} m)" if best is not None
                             else f"{s.get('corner')}: {m} target -> {new} m")
        elif m in ("corner_loss", "entry_loss", "exit_loss") and (t is None or not -0.5 <= t <= 1.5):
            s["target"] = 0.1
            fixes.append(f"{s.get('corner')}: {m} target -> 0.1 s")
        elif m == "lap_time" and best_lap and (t is None or not 0.9 * best_lap <= t <= 1.1 * best_lap):
            s["target"] = round(best_lap - 0.2, 1)
            fixes.append(f"lap_time target -> {s['target']} s")
        elif m == "sector" and (t is None or not 5 <= t <= 120):
            k = int(s.get("sector") or 1) - 1
            secs = [r.sectors[k] / 1000 for r in reports if getattr(r, "sectors", None) and len(r.sectors) > k and r.sectors[k]]
            s["target"] = round(min(secs) - 0.1, 2) if secs else None
            fixes.append(f"sector {k + 1} target -> {s['target']}")
        elif m == "apex_gear" and (t is None or not 1 <= t <= 8):
            s["target"] = None
        out.append(s)
    return [s for s in out if s.get("metric") in METRICS and (s["metric"] not in CORNER or s.get("corner"))], fixes


class PracticePlan:
    def __init__(self, goal: str, steps: list[dict]):
        self.goal = goal
        self.steps = [dict(metric=s["metric"], corner=s.get("corner", ""), target=s.get("target"), sector=s.get("sector"),
                           laps=int(s.get("laps") or 3), hits=int(s.get("hits") or (3 if s["metric"] == "consistency" else 1)),
                           why=s.get("why", ""), attempts=0, got=0, best=None, last=None, misses=0, eased=False,
                           stretched=False, done=False)
                      for s in steps if s.get("metric") in METRICS][:8]
        self.idx = 0
        self.started = time.time()
        self.recent_times: list[float] = []
        self.finished = not self.steps

    @property
    def step(self) -> dict | None:
        return self.steps[self.idx] if self.idx < len(self.steps) else None

    def intro(self) -> str:
        s = self.step
        return f"Plan: {self.goal}. {len(self.steps)} steps. Step one: {describe(s)}." if s else "No usable steps in that plan."

    def status(self) -> str:
        s = self.step
        if not s:
            return f"Plan done: {self.goal}."
        best = f", best so far {_num(s['best'], s['metric'])}" if s["best"] is not None and s["metric"] not in BOOL else ""
        return f"Step {self.idx + 1} of {len(self.steps)}: {describe(s)}. Attempt {s['attempts']} of {s['laps']}{best}."

    def skip(self) -> str:
        if not self.step:
            return "Nothing left to skip."
        self.step["done"] = True
        self.idx += 1
        return "Skipping. " + (f"Next: {describe(self.step)}." if self.step else self._finish())

    def to_dict(self) -> dict:
        return {"goal": self.goal, "step": self.idx + 1, "of": len(self.steps), "finished": self.finished,
                "steps": [{k: s[k] for k in ("metric", "corner", "target", "laps", "attempts", "best", "done")} | {"text": describe(s)}
                          for s in self.steps]}

    # ---- measurement -------------------------------------------------------------------------------------
    def _corner(self, s: dict, rep):
        return next((c for c in rep.corners if s["corner"] and s["corner"].lower() in c["corner"].lower()), None)

    def _measure(self, s: dict, rep):
        m, t = s["metric"], s["target"]
        if m == "clean_lap":
            ok = bool(rep.valid) and not rep.mistakes
            return float(ok), ok, None
        if m == "valid_lap":
            return float(bool(rep.valid)), bool(rep.valid), None
        if m == "mistakes_max":
            n = len(rep.mistakes)
            return n, n <= (t or 0), None
        if m == "lap_time":
            return rep.lap_time, t is not None and rep.lap_time <= t, None
        if m == "delta_ref":
            return rep.delta, rep.delta <= (t if t is not None else 0.3), None
        if m == "sector":
            k = int(s.get("sector") or 1) - 1
            secs = getattr(rep, "sectors", None)
            if not secs or len(secs) <= k or not secs[k]:
                return None, False, None
            v = secs[k] / 1000
            return v, t is not None and v <= t, None
        if m == "coasting_lap":
            v = sum(c.get("coast_m", 0) for c in rep.corners)
            return v, t is not None and v <= t, None
        if m == "consistency":
            self.recent_times = (self.recent_times + [rep.lap_time])[-max(2, s["hits"]):]
            if len(self.recent_times) < s["hits"]:
                return None, False, None
            spread = max(self.recent_times) - min(self.recent_times)
            return spread, spread <= (t or 0.3), None
        c = self._corner(s, rep)
        if c is None:
            return None, False, None
        get = {"corner_loss": "time_loss", "entry_loss": "entry_loss", "exit_loss": "exit_loss", "apex_speed": "min_speed",
               "exit_speed": "exit_speed", "brake_point": "brake_point_m", "throttle_on": "throttle_on_m",
               "coasting": "coast_m", "apex_gear": "apex_gear"}.get(m)
        if m == "no_lockup":
            return float(not c["lockup"]), not c["lockup"], c
        if m == "no_wheelspin":
            return float(not c["wheelspin"]), not c["wheelspin"], c
        if m == "no_off":
            return float(not c["off_track"]), not c["off_track"], c
        v = c.get(get)
        if v is None or t is None:
            return v, False, c
        if m == "apex_gear":
            return v, int(v) == int(t), c
        return v, (v >= t) if m in HIGHER_IS_BETTER else (v <= t), c

    def _why(self, c) -> str:
        """The reason for a miss, from that lap's corner analysis, and the fix."""
        if not c or not c.get("causes"):
            return ""
        cause = c["causes"][0]
        fix = next((f for frag, f in FIX if frag in cause), None)
        return f" You {cause}" + (f": {fix}." if fix else ".")

    # ---- the loop ----------------------------------------------------------------------------------------
    def on_report(self, rep) -> str | None:
        s = self.step
        if not s or self.finished:
            return None
        v, ok, c = self._measure(s, rep)
        if v is None and s["metric"] == "consistency":
            return None   # still collecting laps
        m = s["metric"]
        label = s["corner"] or {"lap_time": "Lap", "delta_ref": "Lap", "sector": f"Sector {s.get('sector') or 1}",
                                "clean_lap": "Clean lap", "valid_lap": "Valid lap", "mistakes_max": "Mistakes",
                                "coasting_lap": "Coasting", "consistency": "Consistency"}.get(m, "Lap")
        s["attempts"] += 1
        prev = s["last"]
        if v is not None and m in NUMERIC:
            s["best"] = v if s["best"] is None else (max if m in HIGHER_IS_BETTER else min)(s["best"], v)
            s["last"] = v
        shown = "clean" if m in BOOL and ok else ("not clean" if m in BOOL else _num(v, m) if v is not None else "no data")
        if ok:
            s["got"] += 1
            if s["got"] < s["hits"] and m != "consistency":
                return f"{label}: {shown}. That's {s['got']} of {s['hits']}, again."
            if m in STRETCH and not s["stretched"] and s["target"] is not None:   # raise the bar once
                s["stretched"], s["got"], s["attempts"] = True, 0, 0
                s["target"] = round(s["target"] + STRETCH[m], 2)
                return f"{label}: {shown}. Target done. Bar's up: {describe(s)}."
            s["done"] = True
            self.idx += 1
            return f"{label}: {shown}. Nailed it." + (f" Next: {describe(self.step)}." if self.step else " " + self._finish())
        s["misses"] += 1
        if s["attempts"] >= s["laps"]:
            self.idx += 1
            best = f"best {_num(s['best'], m)}" if s["best"] is not None and m in NUMERIC else "not there yet"
            return f"{label}: {best}. We'll come back to it." + (f" Next: {describe(self.step)}." if self.step else " " + self._finish())
        gap = ""
        if v is not None and s["target"] is not None and m in NUMERIC:
            gap = f", target {_num(s['target'], m)}"
        trend = ""
        if prev is not None and v is not None and m in NUMERIC and prev != v:
            better = (v > prev) if m in HIGHER_IS_BETTER else (v < prev)
            trend = f" Closer: {_num(prev, m)} to {_num(v, m)}." if better else f" Went backwards from {_num(prev, m)}."
        eased = ""
        if (s["misses"] >= 2 and not s["eased"] and s["best"] is not None and s["target"] is not None and m in NUMERIC
                and m not in ("mistakes_max",)):
            s["eased"] = True   # stuck: halfway target first
            s["target"] = round((s["best"] + s["target"]) / 2, 2)
            eased = f" Let's get {_num(s['target'], m)} first."
        return f"{label}: {shown}{gap}.{trend}{self._why(c)}{eased} Attempt {s['attempts'] + 1} of {s['laps']}."

    def _finish(self) -> str:
        self.finished = True
        done = sum(1 for s in self.steps if s["done"])
        return f"Plan complete: {done} of {len(self.steps)} targets hit."
