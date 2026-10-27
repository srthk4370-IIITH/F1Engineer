"""Rule-based F1 2020 setup + technique advice from measured symptoms (no LLM).

Each rule returns {"change", "why", "confidence"}; the LLM only rephrases these.
F1 2020 ranges: wings 1-11, diff on/off 50-100 %, ARB/suspension/ride height 1-11,
brake pressure 50-100 %, brake bias 50-70 % (front), tyre pressure ~21-25 psi front / 19.5-23.5 rear.
"""
from __future__ import annotations

from collections import Counter


def advise(reports: list, setup: dict, input_device: str, assists: dict) -> list[dict]:
    if not reports:
        return []
    n = len(reports)
    lockups = Counter(c["corner"] for r in reports for c in r.corners if c["lockup"])
    spins = Counter(c["corner"] for r in reports for c in r.corners if c["wheelspin"])
    offs = Counter(c["corner"] for r in reports for c in r.corners if c["off_track"])
    coast = sum(c["time_loss"] > 0 and any("coasted" in x for x in c["causes"]) for r in reports for c in r.corners)
    late_throttle = sum(any("full throttle" in x for x in c["causes"]) for r in reports for c in r.corners)
    bal = reports[-1].balance
    out: list[dict] = []
    kb = input_device.startswith("keyboard")

    def add(change, why, conf="medium"):
        out.append({"change": change, "why": why, "confidence": conf})

    bias = setup.get("brakeBias")
    press = setup.get("brakePressure")
    if sum(lockups.values()) >= 1:
        where = ", ".join(k for k, _ in lockups.most_common(3))
        if bias and bias > 54:
            add(f"Brake bias {bias}% -> {bias - 2}% (move it rearwards)",
                f"front lock-ups at {where}; less front bias stops the fronts saturating first", "high")
        if press and press > 90:
            add(f"Brake pressure {press}% -> {press - 5}%",
                "lower peak pressure makes a 100% (keyboard/trigger slam) input less likely to lock", "high" if kb else "medium")
        if assists.get("abs") == 0 and kb:
            add("Turn ABS on while you learn the braking points",
                "keyboard brake is all-or-nothing; without ABS every stop is a lock-up lottery", "high")
    if sum(spins.values()) >= max(2, n):
        on = setup.get("onThrottle")
        where = ", ".join(k for k, _ in spins.most_common(3))
        if on and on > 55:
            add(f"On-throttle diff {on}% -> {max(50, on - 10)}%", f"wheelspin on exit at {where}; a more open diff lets the inside rear spin less", "high")
        add("Rear anti-roll bar -1 (softer)", "more rear mechanical grip on traction zones")
        if kb and assists.get("tc") == 0:
            add("Traction control to Medium", "digital throttle = 100% instantly; TC does the feathering a keyboard can't")
    slow = bal.get("slow", {}).get("tendency")
    fast = bal.get("fast", {}).get("tendency")
    if slow == "more understeer":
        add("Front anti-roll bar -1 and front suspension -1", "slow corners push relative to the rest of the lap; softer front = more front bite at low speed")
    elif slow == "more oversteer":
        add("Rear anti-roll bar -1, on-throttle diff -5%", "rear steps out in slow corners")
    if fast == "more understeer":
        add("Front wing +1", "high-speed corners push; more front downforce rotates the car at speed")
    elif fast == "more oversteer":
        add("Rear wing +1", "rear is nervous in fast corners; buy stability with rear downforce")
    if sum(offs.values()) >= 2:
        add("No setup change for track limits", f"you're running wide at {', '.join(k for k, _ in offs.most_common(3))}: "
            "that's line and entry speed, not the car. Brake a touch earlier, hit the apex, then commit to the throttle.", "high")
    if kb:
        if coast >= 3:
            add("Technique: keyboard throttle pulsing", "you coast mid-corner; on a keyboard tap the throttle in short pulses to hold speed instead of lifting completely")
        add("Controls: keyboard steering sensitivity", "in Controls > Calibration lower steering saturation/raise linearity so digital steering ramps smoother")
    if late_throttle >= 3:
        add("Technique: earlier, progressive throttle", "you get back to full throttle late in several corners; prioritise exit speed onto the straights")
    return out
