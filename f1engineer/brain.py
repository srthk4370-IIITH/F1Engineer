"""The engineer's long-term knowledge of the driver: progress, driving style, strategy maths, and memory.

Everything here is computed from stored data (numbers the LLM can trust); the LLM does the judging and talking.
  - lap_summary table (storage): one compact row per player lap, never pruned -> progress across sessions
  - style_metrics(): reference-free driving style numbers for one lap
  - progress() / driving_style(): aggregates with rule-based labels, so the model doesn't have to guess
  - evaluate_strategy(): race-time simulation of pit plans
  - DriverProfile: what the driver told the engineer to remember (goals, preferences)
"""
from __future__ import annotations

import itertools
import json
import time
from pathlib import Path

import numpy as np

from f1engineer.analysis import lapmath as LM


# ---- per-lap style numbers (no reference needed) ------------------------------------------
def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index runs where mask is true."""
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def style_metrics(rs: dict, issues: list[dict] | None = None) -> dict:
    """rs = lap resampled on the 2 m grid. Returns per-lap style numbers."""
    thr, brk, steer, spd = rs["throttle"], rs["brake"], rs["steer"], rs["speed"]
    n, km = len(thr), len(thr) * LM.GRID_M / 1000
    zones = [(s, e) for s, e in _runs(brk > 0.1) if e - s >= 5]
    ramps, peaks, trail = [], [], []
    for s, e in zones:
        seg = brk[s:e]
        peaks.append(float(seg.max()))
        hit = np.flatnonzero(seg >= 0.8 * seg.max())
        ramps.append(float(hit[0] * LM.GRID_M) if len(hit) else float((e - s) * LM.GRID_M))
        trail.append(float((np.abs(steer[s:e]) > 0.15).mean()))
    pulses = int((np.abs(np.diff((thr > 0.5).astype(np.int8))) > 0).sum())
    out = {
        "full_throttle_pct": round(float((thr > 0.98).mean() * 100), 1),
        "partial_throttle_pct": round(float(((thr > 0.05) & (thr <= 0.98)).mean() * 100), 1),
        "braking_pct": round(float((brk > 0.05).mean() * 100), 1),
        "coasting_m": round(float(((thr < 0.05) & (brk < 0.05) & (spd > 60)).sum() * LM.GRID_M)),
        "braking_zones": len(zones),
        "brake_peak_avg": round(float(np.mean(peaks)), 2) if peaks else None,
        "brake_ramp_m_avg": round(float(np.mean(ramps)), 1) if ramps else None,   # metres to reach 80% of peak
        "trail_braking_pct": round(float(np.mean(trail) * 100), 1) if trail else None,  # braking while turning
        "throttle_switches_per_km": round(pulses / km, 1) if km else None,
        "steering_activity_per_km": round(float(np.abs(np.diff(steer)).sum() / km), 1) if km else None,
    }
    if issues is not None:
        out["lockups"] = sum(1 for i in issues if "locked" in i["issue"])
        out["offs"] = sum(1 for i in issues if "wide" in i["issue"])
        out["wheelspins"] = sum(1 for i in issues if "wheelspin" in i["issue"])
    return out


# ---- aggregates for the LLM ------------------------------------------------------------------
def _rows(store, track: str | None, limit: int = 400) -> list[dict]:
    q = "SELECT ts,track,session_type,session_uid,lap_num,lap_time,valid,input,assists,style,corners,mistakes,ref_label " \
        "FROM lap_summary"
    args: tuple = ()
    if track:
        q += " WHERE lower(track)=lower(?)"
        args = (track,)
    q += " ORDER BY ts DESC LIMIT ?"
    with store.lock:
        rows = store.db.execute(q, args + (limit,)).fetchall()
    keys = ("ts", "track", "session_type", "session", "lap", "lap_time", "valid", "input", "assists", "style", "corners",
            "mistakes", "ref_label")
    out = []
    for r in rows[::-1]:
        d = dict(zip(keys, r))
        for k in ("assists", "style", "corners", "mistakes"):
            d[k] = json.loads(d[k]) if d[k] else None
        out.append(d)
    return out


def _pace(times: list[float]) -> dict:
    if not times:
        return {}
    best = min(times)
    rep = [t for t in times if t <= best * 1.03]   # representative laps (drop out laps / disasters)
    return {"best": round(best, 3), "representative_median": round(float(np.median(rep)), 3),
            "consistency_std_s": round(float(np.std(rep)), 3) if len(rep) > 1 else None, "laps": len(times)}


def progress(store, track: str | None = None) -> dict:
    rows = _rows(store, track)
    if not rows:
        return {"error": "no laps recorded yet"}
    track = track or rows[-1]["track"]
    rows = [r for r in rows if r["track"] == track]
    sessions = []
    for uid, grp in itertools.groupby(rows, key=lambda r: r["session"]):
        grp = list(grp)
        times = [r["lap_time"] for r in grp]
        valid = [r["lap_time"] for r in grp if r["valid"]]
        mistakes = sum(len(r["mistakes"] or []) for r in grp)
        sessions.append({"date": time.strftime("%Y-%m-%d %H:%M", time.localtime(grp[0]["ts"])),
                         "mode": grp[0]["session_type"], "input": grp[-1]["input"], **_pace(times),
                         "best_valid": round(min(valid), 3) if valid else None,
                         "valid_laps": len(valid), "mistakes_per_lap": round(mistakes / len(grp), 2)})
    out = {"track": track, "sessions": sessions[-8:]}
    if len(sessions) >= 2:
        a, b = sessions[0], sessions[-1]
        out["trend"] = {"best_change_s": round(b["best"] - a["best"], 3),
                        "representative_change_s": round(b["representative_median"] - a["representative_median"], 3),
                        "mistakes_per_lap_change": round(b["mistakes_per_lap"] - a["mistakes_per_lap"], 2)}
    # within the latest session: first half vs second half
    last = [r for r in rows if r["session"] == rows[-1]["session"]]
    if len(last) >= 4:
        h = len(last) // 2
        p1, p2 = _pace([r["lap_time"] for r in last[:h]]), _pace([r["lap_time"] for r in last[h:]])
        out["this_session"] = {"first_half": p1, "second_half": p2}
    # corner trends: average loss vs the reference, oldest third vs newest third of analysed laps
    an = [r for r in rows if r["corners"]]
    if len(an) >= 3:
        k = max(1, len(an) // 3)

        def avg(rs):
            acc: dict[str, list] = {}
            for r in rs:
                for name, loss in r["corners"].items():
                    acc.setdefault(name, []).append(loss)
            return {n: float(np.mean(v)) for n, v in acc.items()}
        old, new = avg(an[:k]), avg(an[-k:])
        out["corner_trend_s"] = {n: {"before": round(old[n], 3), "now": round(new[n], 3)} for n in new if n in old}
    out["note"] = "times in seconds; negative change = faster; representative = median of laps within 3% of best"
    return out


def driving_style(store, track: str | None = None, last_n: int = 15) -> dict:
    rows = [r for r in _rows(store, track) if r["style"]][-last_n:]
    if not rows:
        return {"error": "no laps recorded yet"}
    keys = [k for k in rows[-1]["style"] if isinstance(rows[-1]["style"][k], (int, float))]
    avg = {k: round(float(np.mean([r["style"][k] for r in rows if r["style"].get(k) is not None])), 2) for k in keys}
    labels = []
    if avg.get("coasting_m", 0) > 150:
        labels.append(f"coasts a lot ({avg['coasting_m']:.0f} m/lap with neither pedal): commit to brake or throttle")
    if avg.get("brake_ramp_m_avg") is not None:
        labels.append("stamps the brake hard (fast brake ramp)" if avg["brake_ramp_m_avg"] < 8 else
                      "squeezes the brake progressively")
    if avg.get("trail_braking_pct") is not None:
        labels.append("trail-brakes into corners" if avg["trail_braking_pct"] > 35 else
                      "brakes in a straight line, little trail braking")
    if avg.get("throttle_switches_per_km", 0) > 8:
        labels.append("pulses the throttle on exits (typical keyboard), traction-limited")
    if avg.get("lockups", 0) >= 0.5:
        labels.append(f"locks up often ({avg['lockups']:.1f} per lap)")
    if avg.get("offs", 0) >= 0.5:
        labels.append(f"runs wide often ({avg['offs']:.1f} per lap)")
    # relative to the reference: brake points / apex speeds / throttle-on from the corner reports
    rel = {"brakes_earlier_than_ref_m": [], "apex_speed_above_ref_kmh": [], "full_throttle_later_than_ref_m": []}
    for r in rows:
        for c in (r["style"].get("corner_detail") or []):
            for k in rel:
                if c.get(k) is not None:
                    rel[k].append(c[k])
    relavg = {k: round(float(np.mean(v)), 1) for k, v in rel.items() if v}
    if relavg.get("brakes_earlier_than_ref_m", 0) > 5:
        labels.append(f"brakes EARLIER than the reference (avg {relavg['brakes_earlier_than_ref_m']:.0f} m): room to brake later")
    elif relavg.get("brakes_earlier_than_ref_m", 0) < -5:
        labels.append("brakes later than the reference")
    if relavg.get("apex_speed_above_ref_kmh", 0) < -4:
        labels.append(f"under-speed at the apex (avg {-relavg['apex_speed_above_ref_kmh']:.0f} km/h): V-line, slow mid-corner")
    elif relavg.get("apex_speed_above_ref_kmh", 0) > 4:
        labels.append("carries more apex speed than the reference (U-line); watch the exits")
    if relavg.get("full_throttle_later_than_ref_m", 0) > 10:
        labels.append(f"late back on the power (avg {relavg['full_throttle_later_than_ref_m']:.0f} m after the reference)")
    inputs = sorted({r["input"] for r in rows if r["input"]})
    return {"laps_considered": len(rows), "input_devices": inputs, "averages": avg, "vs_reference": relavg,
            "style_summary": labels,
            "note": "brake_ramp_m_avg = metres to reach 80% of peak brake; trail_braking_pct = share of braking while steering"}


# ---- strategy simulator ----------------------------------------------------------------------
COMPOUND_PACE = {"soft": 0.0, "medium": 0.35, "hard": 0.7, "intermediate": 4.0, "wet": 7.0}   # s/lap vs soft (dry)
COMPOUND_WEAR = {"soft": 1.0, "medium": 0.7, "hard": 0.5, "intermediate": 0.8, "wet": 0.6}     # relative wear rate
CLIFF = 70.0


def evaluate_strategy(plan: list[dict] | None, total_laps: int, current_lap: int = 1, start_compound: str = "soft",
                      tyre_age: int = 0, wear_rate_soft: float | None = None, base_lap: float = 90.0,
                      pit_loss: float = 21.0) -> dict:
    """plan = [{"lap": 12, "compound": "medium"}, ...] pit stops from current_lap on. Returns race-time estimates
    for this plan and the best 0/1/2-stop alternatives (dry rule: two different dry compounds)."""
    wr = wear_rate_soft or 4.5   # % per lap on softs if we have no measurement yet

    def stint_time(comp: str, laps: int, age0: int) -> tuple[float, float]:
        t, wear = 0.0, (age0 * wr * COMPOUND_WEAR[comp])
        for _ in range(laps):
            wear += wr * COMPOUND_WEAR[comp]
            deg = 0.025 * wear / COMPOUND_WEAR[comp] ** 0.5 / 10          # gentle linear degradation
            cliff = max(0.0, wear - CLIFF) * 0.12                        # sharp fall-off past the cliff
            t += base_lap + COMPOUND_PACE.get(comp, 0.5) + deg + cliff
        return t, wear

    def simulate(stops: list[dict]) -> dict:
        laps_left = total_laps - current_lap + 1
        t, comp, age, lap, warn, used = 0.0, start_compound, tyre_age, current_lap, [], {start_compound}
        for s in sorted(stops, key=lambda x: x["lap"]):
            n = max(0, s["lap"] - lap + 1)
            st, w = stint_time(comp, n, age)
            t += st + pit_loss
            if w > CLIFF:
                warn.append(f"{comp} stint to lap {s['lap']} goes past the wear cliff ({w:.0f}%)")
            lap, comp, age = s["lap"] + 1, s["compound"], 0
            used.add(comp)
        st, w = stint_time(comp, total_laps - lap + 1, age)
        t += st
        if w > CLIFF:
            warn.append(f"final {comp} stint ends at {w:.0f}% wear (past the cliff)")
        dry = {c for c in used if c in ("soft", "medium", "hard")}
        if len(dry) < 2 and used <= {"soft", "medium", "hard"} and total_laps >= 5:
            warn.append("only one dry compound used: illegal in a dry race (must use two)")
        return {"stops": stops, "race_time_s": round(t, 1), "laps_simulated": laps_left, "warnings": warn}

    out = {"assumptions": {"base_lap_s": base_lap, "pit_loss_s": pit_loss, "soft_wear_pct_per_lap": round(wr, 2),
                           "compound_pace_s_per_lap_vs_soft": COMPOUND_PACE, "cliff_wear_pct": CLIFF}}
    if plan:
        out["your_plan"] = simulate(plan)
    cands = []
    rng = range(current_lap + 1, total_laps)
    comps = ["soft", "medium", "hard"]
    for c1 in comps:
        for l1 in rng:
            cands.append(simulate([{"lap": l1, "compound": c1}]))
    if total_laps - current_lap > 12:
        step = max(1, (total_laps - current_lap) // 12)
        for l1, l2 in itertools.combinations(range(current_lap + 1, total_laps, step), 2):
            for c1, c2 in itertools.product(comps, comps):
                cands.append(simulate([{"lap": l1, "compound": c1}, {"lap": l2, "compound": c2}]))
    legal = [c for c in cands if not any("illegal" in w for w in c["warnings"])]
    best = sorted(legal, key=lambda c: c["race_time_s"])[:3]
    out["best_alternatives"] = best
    if plan and best:
        out["your_plan_vs_best_s"] = round(out["your_plan"]["race_time_s"] - best[0]["race_time_s"], 1)
    return out


# ---- memory --------------------------------------------------------------------------------------
class DriverProfile:
    """Facts the driver asked the engineer to remember (goals, preferences). data/driver_profile.json"""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {"notes": []}

    def remember(self, note: str) -> dict:
        note = note.strip()[:300]
        if note and note not in [n["note"] for n in self.data["notes"]]:
            self.data["notes"].append({"date": time.strftime("%Y-%m-%d"), "note": note})
            self.data["notes"] = self.data["notes"][-40:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        return {"saved": note, "total_notes": len(self.data["notes"])}

    def summary(self) -> str:
        return "; ".join(f"{n['date']}: {n['note']}" for n in self.data["notes"][-12:]) or "nothing yet"


def brain_tools(store, profile: DriverProfile, defaults=lambda: {}) -> dict:
    """Tools that work from stored data: shared by the live engine and the offline dashboard."""
    def strat(stops=None, total_laps=None, current_lap=None, start_compound=None, tyre_age=None, base_lap_s=None,
              pit_loss_s=None):
        d = defaults()
        kw = {"total_laps": total_laps or d.get("total_laps"), "current_lap": current_lap or d.get("current_lap", 1),
              "start_compound": (start_compound or d.get("start_compound") or "soft").lower(),
              "tyre_age": tyre_age if tyre_age is not None else d.get("tyre_age", 0),
              "wear_rate_soft": d.get("wear_rate_soft"), "base_lap": base_lap_s or d.get("base_lap", 90.0),
              "pit_loss": pit_loss_s or d.get("pit_loss", 21.0)}
        if not kw["total_laps"]:
            return {"error": "need total_laps (race length) - ask the driver"}
        plan = [{"lap": int(s["lap"]), "compound": str(s["compound"]).lower()} for s in (stops or [])]
        return evaluate_strategy(plan or None, **kw)

    return {
        "get_progress": lambda track=None: progress(store, track),
        "get_driving_style": lambda track=None: driving_style(store, track),
        "evaluate_strategy": strat,
        "remember": lambda note: profile.remember(note),
        "get_stored_lap": lambda lap=None: lap_detail_from_store(store, lap or "latest"),
    }


def lap_detail_from_store(store, which: str | int = "latest") -> dict:
    """A stored lap's corner report (offline: works from the database)."""
    laps = [l for l in store.list_laps() if l["car"] == "Player"]
    if not laps:
        return {"error": "no laps stored"}
    lap = laps[0] if which in ("latest", None) else next((l for l in laps if l["lap"] == int(which)), None)
    if not lap:
        return {"error": f"lap {which} not found"}
    d = store.lap_detail(lap["id"])
    rep = d.get("report")
    out = {"lap": lap["lap"], "lap_time": lap["lap_time"], "valid": lap["valid"], "track": lap["track"],
           "input": lap["input"], "sectors_ms": lap.get("sectors")}
    if rep:
        out.update({"vs": rep["ref_label"], "delta": rep["delta"], "losses": rep["losses"], "gains": rep["gains"],
                    "mistakes": rep["mistakes"],
                    "corners": [{k: c[k] for k in ("corner", "time_loss", "causes")} for c in rep["corners"]]})
    return out


def backfill(store) -> int:
    """Create long-term summary rows for stored laps that predate them (runs once; cheap)."""
    from f1engineer.analysis import coach
    with store.lock:
        have = {(r[0], r[1]) for r in store.db.execute("SELECT session_uid, lap_num FROM lap_summary")}
        rows = store.db.execute(
            "SELECT l.id, l.session_uid, s.track, s.type, s.started, l.lap_num, l.lap_time, l.valid, l.input_device, "
            "l.assists, l.report, l.trace FROM laps l LEFT JOIN sessions s ON s.uid=l.session_uid "
            "WHERE l.car_name='Player' ORDER BY l.id").fetchall()
    n = 0
    for (lid, uid, track, stype, started, lap_num, lap_time, valid, inp, assists, report, trace) in rows:
        if (uid, lap_num) in have or not track:
            continue
        try:
            arrays = store.load_trace(trace)
            rs = LM.resample(arrays, 5 * round(float(arrays["dist"].max()) / 5))
            if rs is None:
                continue
            corners_det = LM.detect_corners(rs, coach.corner_names(track))
            style = style_metrics(rs, coach.self_review(rs, corners_det))
            rep = json.loads(report) if report else None
            corners = None
            if rep:
                corners = {}
                for c in rep["corners"]:
                    corners[c["corner"]] = round(corners.get(c["corner"], 0) + c["time_loss"], 3)
                style["corner_detail"] = [{
                    "corner": c["corner"],
                    "brakes_earlier_than_ref_m": None if c["brake_point_m"] is None or c["ref_brake_point_m"] is None
                    else c["brake_point_m"] - c["ref_brake_point_m"],
                    "apex_speed_above_ref_kmh": c["min_speed"] - c["ref_min_speed"],
                    "full_throttle_later_than_ref_m": None if c["throttle_on_m"] is None or c["ref_throttle_on_m"] is None
                    else c["throttle_on_m"] - c["ref_throttle_on_m"]} for c in rep["corners"]]

            class _L:  # the minimal lap shape save_summary needs
                pass
            lap = _L()
            lap.lap_num, lap.lap_time, lap.invalid = lap_num, lap_time, not valid
            lap.assists = json.loads(assists) if assists else {}
            store.session_clock = (started or time.time()) + lap_num
            store.save_summary(uid, track, stype, lap, inp, style, corners, rep["mistakes"] if rep else [],
                               rep["ref_label"] if rep else None)
            n += 1
        except Exception as e:
            print(f"[brain] backfill lap {lid} failed: {e!r}")
        finally:
            store.session_clock = None
    return n
