"""Rule-based setup + technique advice from measured symptoms (no LLM), against the car's CURRENT setup.

Every setup item is a concrete move on a real slider ("front anti-roll bar 7 -> 6"), clamped to the game's range;
a move that is impossible (already at the limit) is replaced by the next-best lever or skipped. Each item says what it
is based on, so the engineer (and the driver) can judge it.
"""
from __future__ import annotations

from collections import Counter

from f1engineer.analysis import setup_model as SM


def advise(reports: list, setup: dict, input_device: str, assists: dict, fmt: int | None = 2020,
           live: dict | None = None) -> list[dict]:
    if not reports or not setup:
        return []
    n = len(reports)
    lockups = Counter(c["corner"] for r in reports for c in r.corners if c["lockup"])
    spins = Counter(c["corner"] for r in reports for c in r.corners if c["wheelspin"])
    offs = Counter(c["corner"] for r in reports for c in r.corners if c["off_track"])
    coast = sum(c["time_loss"] > 0 and any("coasted" in x for x in c["causes"]) for r in reports for c in r.corners)
    late_throttle = sum(any("full throttle" in x for x in c["causes"]) for r in reports for c in r.corners)
    bal = reports[-1].balance or {}
    kb = (input_device or "").startswith("keyboard")
    out: list[dict] = []
    used: set[str] = set()

    def move(param: str, steps: int, why: str, conf: str = "medium", alt: tuple | None = None) -> bool:
        c = SM.change(setup, fmt, param, steps) if param not in used else None
        if c is None and alt:
            return move(*alt, why=why, conf=conf)
        if c is None:
            return False
        used.add(param)
        out.append({"change": c[0], "why": why, "confidence": conf, "param": param, "from": c[1], "to": c[2]})
        return True

    def tip(change: str, why: str, conf: str = "medium"):
        out.append({"change": change, "why": why, "confidence": conf})

    # braking: front lock-ups -> rearward bias, else less pressure
    if lockups:
        where = ", ".join(k for k, _ in lockups.most_common(3))
        n_lock = sum(lockups.values())
        move("brakeBias", -2, f"{n_lock} front lock-up(s) in {n} laps ({where}): less front bias lets the fronts stop saturating first",
             "high", alt=("brakePressure", -1))
        if kb or n_lock >= 2:
            move("brakePressure", -1, "a keyboard/trigger slam is 100% instantly; lower peak pressure keeps it under the lock-up limit",
                 "high" if kb else "medium")
        if assists.get("abs") == 0 and kb:
            tip("Turn ABS on while you learn the braking points", "keyboard brake is all-or-nothing; without ABS every stop is a lock-up lottery", "high")
    # traction
    if sum(spins.values()) >= max(2, n):
        where = ", ".join(k for k, _ in spins.most_common(3))
        move("onThrottle", -2, f"wheelspin on exit at {where}: a more open diff spins the inside rear less", "high")
        move("rearAntiRollBar", -1, "softer rear roll = more rear mechanical grip in the traction zones")
        if kb and assists.get("tc") == 0:
            tip("Traction control to Medium", "digital throttle = 100% instantly; TC does the feathering a keyboard can't")
    # balance by corner speed (relative to the rest of the lap)
    slow, med, fast = (bal.get(k, {}).get("tendency") for k in ("slow", "medium", "fast"))
    if slow == "more understeer":
        move("frontAntiRollBar", -1, "slow corners push compared with the rest of the lap: a softer front roll bar gives more front bite at low speed",
             alt=("frontSuspension", -1))
        move("onThrottle", -1, "a slightly more open diff helps the car rotate on the way out of slow corners")
    elif slow == "more oversteer":
        move("rearAntiRollBar", -1, "the rear steps out in slow corners: softer rear roll bar = more rear grip", alt=("rearSuspension", -1))
    if fast == "more understeer":
        move("frontWing", 1, "fast corners push: more front wing rotates the car at speed (costs a little top speed)")
    elif fast == "more oversteer":
        move("rearWing", 1, "the rear is nervous in fast corners: more rear wing buys stability (costs a little top speed)")
    if med == "more understeer" and "frontWing" not in used and fast != "more oversteer":
        move("frontWing", 1, "medium-speed corners push: a touch more front wing")
    # tyre temperatures (live): overheating or an unbalanced front/rear temperature
    temps = (live or {}).get("tyre_surface")
    if temps and len(temps) == 4 and max(temps) > 0:
        rear, front = max(temps[0], temps[1]), max(temps[2], temps[3])
        if rear > 108:
            move("rearLeftTyrePressure", -1, f"rears overheating ({rear} C): lower pressure, cooler and more grip", alt=("onThrottle", -1))
        if front > 108:
            move("frontLeftTyrePressure", -1, f"fronts overheating ({front} C): lower pressure", alt=("frontCamber", 1))
    # things a setup can't fix
    if sum(offs.values()) >= 2:
        tip("No setup change for track limits", f"you're running wide at {', '.join(k for k, _ in offs.most_common(3))}: that's line and "
            "entry speed, not the car. Brake a touch earlier, hit the apex, then commit to the throttle.", "high")
    if kb and coast >= 3:
        tip("Technique: keyboard throttle pulsing", "mid-corner coasting: short throttle taps hold the speed instead of lifting completely")
    if late_throttle >= 3:
        tip("Technique: earlier, progressive throttle", "back to full throttle late in several corners; exit speed onto the straights is worth most")
    return out
