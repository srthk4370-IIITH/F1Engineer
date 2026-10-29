"""You vs another car, corner by corner - from live data, no AI needed.

Every car's speed, throttle, brake, gear and lap time are recorded (~20 Hz for the others), so "where am I losing time
to Verstappen?" can be answered on the current lap as soon as you've driven part of it (the car ahead has always already
driven that part), and on the last full lap of both. Corners come from the engine's detected corners, else the track's
corner table, else equal slices of the lap.
"""
from __future__ import annotations

import difflib
import re

import numpy as np

from f1engineer.analysis import coach, lapmath as LM

ALIASES = {   # what speech recognition makes of some names
    "verstappen": ["verstappen", "for stappen", "versatappen", "towards to happen", "worst happen", "verstapen", "max"],
    "hamilton": ["hamilton", "lewis", "hammilton"], "leclerc": ["leclerc", "leclair", "le clerc", "charles"],
    "bottas": ["bottas", "botas", "valtteri"], "norris": ["norris", "lando"], "sainz": ["sainz", "signs", "sines", "carlos"],
    "ricciardo": ["ricciardo", "daniel"], "perez": ["perez", "checo"], "vettel": ["vettel", "seb"],
    "russell": ["russell", "russel", "george"], "gasly": ["gasly", "gasley", "pierre"], "ocon": ["ocon", "o con"],
    "albon": ["albon", "alex"], "alonso": ["alonso", "fernando"], "piastri": ["piastri", "oscar"],
}


def _snap(lap) -> dict | None:
    """Arrays of a lap (finished or still being driven: snapshot every channel to the same length)."""
    if lap is None:
        return None
    if lap.arrays is not None:
        a = lap.arrays
    else:
        ch = dict(lap.channels)
        if not ch or "dist" not in ch:
            return None
        m = min(len(v) for v in ch.values())
        a = {k: np.asarray(v[:m], dtype=np.float64) for k, v in ch.items() if k in ("dist", "t", "speed", "throttle", "brake", "gear")}
    if len(a.get("dist", ())) < 20:
        return None
    return a


def _grid(a: dict, upto: float) -> dict | None:
    d = np.asarray(a["dist"], dtype=np.float64)
    ok = d >= -1
    d = np.maximum.accumulate(d[ok])
    d, idx = np.unique(d, return_index=True)
    if len(d) < 20:
        return None
    upto = min(upto, float(d[-1]))
    g = np.arange(max(0.0, float(d[0])), upto, LM.GRID_M)
    if len(g) < 20:
        return None
    return {"dist": g, **{k: np.interp(g, d, np.asarray(a[k], dtype=np.float64)[ok][idx])
                          for k in ("t", "speed", "throttle", "brake") if k in a}}


def _lap_of(car, n: int):
    if car.lap is not None and car.lap.lap_num == n:
        return car.lap
    return next((lp for lp in reversed(car.completed) if lp.lap_num == n and lp.complete), None)


def segments(eng, track_len: float) -> list[tuple[str, float, float]]:
    """(name, start_m, end_m) per corner."""
    if eng.corners:
        return [(c.name, c.start, c.end) for c in eng.corners]
    names = sorted(coach.corner_names(eng.tr.track).items())
    if len(names) >= 3:
        ap = [a for a, _ in names]
        cuts = [0.0] + [(ap[i] + ap[i + 1]) / 2 for i in range(len(ap) - 1)] + [track_len]
        return [(n, cuts[i], cuts[i + 1]) for i, (_, n) in enumerate(names)]
    k = 8
    return [(f"part {i + 1} of the lap", track_len * i / k, track_len * (i + 1) / k) for i in range(k)]


def _at(g: dict, key: str, m: float) -> float:
    return float(np.interp(m, g["dist"], g[key]))


def _corner(me: dict, th: dict, s: float, e: float) -> dict | None:
    if e > me["dist"][-1] or e > th["dist"][-1] or s < max(me["dist"][0], th["dist"][0]):
        return None
    dt = (_at(me, "t", e) - _at(me, "t", s)) - (_at(th, "t", e) - _at(th, "t", s))
    sl = lambda g: (g["dist"] >= s) & (g["dist"] <= e)  # noqa: E731
    m1, m2 = sl(me), sl(th)
    if m1.sum() < 3 or m2.sum() < 3:
        return None
    out = {"loss_s": round(dt, 3), "min_kmh": [round(float(me["speed"][m1].min())), round(float(th["speed"][m2].min()))],
           "top_kmh": [round(float(me["speed"][m1].max())), round(float(th["speed"][m2].max()))],
           "exit_kmh": [round(_at(me, "speed", e)), round(_at(th, "speed", e))]}

    def brake_at(g, m):
        """Start of the braking zone for this corner's apex (the last one within 300 m before it)."""
        d, sp, br = g["dist"][m], g["speed"][m], g["brake"][m]
        ap = int(np.argmin(sp))
        on = np.nonzero((br[:ap + 1] > 0.3) & (d[:ap + 1] >= d[ap] - 300))[0]
        if not len(on):
            return None
        i = on[-1]
        while i > 0 and br[i - 1] > 0.3:   # walk back to where this stop began
            i -= 1
        return float(d[i])

    def full_at(g, m):
        sp = g["speed"][m]
        ap = int(np.argmin(sp))
        f = np.nonzero(g["throttle"][m][ap:] > 0.95)[0]
        return float(g["dist"][m][ap + f[0]]) if len(f) else None
    if "brake" in me and "brake" in th:
        b = [brake_at(me, m1), brake_at(th, m2)]
        out["brake_m"] = b if None not in b and abs(b[0] - b[1]) < 60 else [None, None]   # same stop, or not compared
        out["full_throttle_m"] = [full_at(me, m1), full_at(th, m2)]
    return out


def short(name: str) -> str:
    return (name or "Car").split()[-1].title()


def why(c: dict, them: str) -> str:
    """The reasons for a corner delta that point the same way as the delta, in radio words.

    Losses: what the other driver does better ("he brakes 9 m later, carries 18 km/h more"); gains: what you do better.
    """
    them = short(them)
    lost = c["loss_s"] > 0
    bits = []
    b = c.get("brake_m") or [None, None]
    if b[0] is not None and b[1] is not None and abs(b[0] - b[1]) >= 8 and (b[1] > b[0]) == lost:
        bits.append(f"{them if lost else 'you'} brake{'s' if lost else ''} {abs(b[0] - b[1]):.0f} m later")
    dmin = c["min_kmh"][1] - c["min_kmh"][0]
    if abs(dmin) >= 4 and (dmin > 0) == lost:
        bits.append(f"{'he carries' if lost else 'you carry'} {abs(dmin)} km/h more through the apex")
    f = c.get("full_throttle_m") or [None, None]
    if f[0] is not None and f[1] is not None and abs(f[0] - f[1]) >= 10 and (f[1] < f[0]) == lost:
        bits.append(f"{'he' if lost else 'you'}'s on full throttle {abs(f[0] - f[1]):.0f} m sooner".replace("you's", "you're"))
    dex = c["exit_kmh"][1] - c["exit_kmh"][0]
    if abs(dex) >= 5 and (dex > 0) == lost and len(bits) < 2:
        bits.append(f"{'he' if lost else 'you'}'s {abs(dex)} km/h quicker onto the straight".replace("you's", "you're"))
    dtop = c["top_kmh"][1] - c["top_kmh"][0]
    if abs(dtop) >= 5 and (dtop > 0) == lost and not bits:
        bits.append(f"{'his' if lost else 'your'} top speed is {abs(dtop)} km/h higher (tow, DRS or wing)")
    return ", ".join(bits[:2])


def compare(eng, other: int) -> dict:
    """You vs car `other`: the lap in progress (up to where you are) and the last lap you both finished."""
    tr = eng.tr
    me_c, th_c = tr.cars[tr.player_idx], tr.cars[other]
    L = float(tr.session.get("trackLength") or 0) or 5000.0
    segs = segments(eng, L)
    name = short(tr.car_name(other))
    out: dict = {"driver": name, "position": None, "gap_s": None}
    if tr.lapdata is not None:
        out["position"] = int(tr.lapdata[other]["carPosition"])
        g = eng.gaps.gap(tr.player_idx, other)
        out["gap_s"] = None if g is None else round(g, 2)
    cur = me_c.lap.lap_num if me_c.lap is not None else None
    for label, n in (("this_lap", cur), ("last_lap", None if cur is None else cur - 1)):
        if n is None or n < 1:
            continue
        a, b = _snap(_lap_of(me_c, n)), _snap(_lap_of(th_c, n))
        if a is None or b is None:
            continue
        upto = min(float(np.max(a["dist"])), float(np.max(b["dist"])))
        me, th = _grid(a, upto), _grid(b, upto)
        if me is None or th is None:
            continue
        corners = []
        for cname, s, e in segs:
            c = _corner(me, th, s, e)
            if c is not None:
                corners.append({"corner": cname, "start_m": round(s), "end_m": round(e), **c, "why": why(c, name)})
        if not corners:
            continue
        total = round(_at(me, "t", me["dist"][-1]) - _at(th, "t", me["dist"][-1]), 3)
        out[label] = {"lap": n, "upto_m": round(upto), "complete": upto > 0.95 * L, "total_loss_s": total,
                      "losses": sorted([c for c in corners if c["loss_s"] >= 0.05], key=lambda c: -c["loss_s"])[:4],
                      "gains": sorted([c for c in corners if c["loss_s"] <= -0.05], key=lambda c: c["loss_s"])[:3], "corners": corners}
    if "this_lap" not in out and "last_lap" not in out:
        out["error"] = "not enough of a lap from both cars yet (a few hundred metres is enough)"
    return out


def find_car(eng, text: str) -> int | None:
    """Which car the driver means: a name (fuzzy, incl. speech-recognition slips), 'leader', 'ahead', 'behind'."""
    tr = eng.tr
    if tr.lapdata is None:
        return None
    t = " " + re.sub(r"[^a-z ]", " ", text.lower()) + " "
    me = tr.player_idx
    pos = int(tr.lapdata[me]["carPosition"])
    by_pos = {int(tr.lapdata[i]["carPosition"]): i for i in range(22) if int(tr.lapdata[i]["carPosition"]) > 0}
    if re.search(r" (leader|leading|p ?1|first place) ", t) and pos != 1:
        return by_pos.get(1)
    if re.search(r" (behind|following|chasing) ", t):
        return by_pos.get(pos + 1)
    best, score = None, 0.0
    flat = t.replace(" ", "")
    for i in range(22):
        if i == me or i >= len(tr.participants):
            continue
        sur = tr.car_name(i).lower().split()[-1] if tr.car_name(i) else ""
        if not sur:
            continue
        for alias in ALIASES.get(sur, [sur]):
            if f" {alias} " in t:
                return i
        r = max(difflib.SequenceMatcher(None, w, sur).ratio() for w in t.split() or [""])
        r = max(r, difflib.SequenceMatcher(None, flat[-len(sur) - 6:], sur).ratio() * 0.9)
        if r > score:
            best, score = i, r
    if score >= 0.72:
        return best
    return by_pos.get(pos - 1) if pos > 1 else by_pos.get(pos + 1)   # no name: the car ahead (leader: behind)


def radio_text(r: dict) -> str:
    """Short spoken answer from compare()."""
    if "error" in r:
        return f"Not enough data on {r['driver']} yet. Give me half a lap."
    them = r["driver"]
    part = r.get("last_lap") if (r.get("last_lap") or {}).get("complete") else r.get("this_lap") or r.get("last_lap")
    tot = part["total_loss_s"]
    where = "last lap" if part is r.get("last_lap") else "this lap so far"
    if tot >= 0.05:
        head = f"{abs(tot):.1f} down to {them} {where}."
    elif tot <= -0.05:
        head = f"You're {abs(tot):.1f} up on {them} {where}."
    else:
        head = f"Dead level with {them} {where}."
    out = [head]
    losses = part["losses"][:2]
    for i, c in enumerate(losses):
        lead = "But you lose " if tot < 0.05 and i == 0 else ""
        out.append(f"{lead}{c['corner']}, {c['loss_s']:.2f}" + (f": {c['why']}." if c["why"] else "."))
    if part["gains"]:
        g = part["gains"][0]
        out.append(f"You're quicker at {g['corner']}" + (f", {g['why']}." if g["why"] else "."))
    return " ".join(out)
