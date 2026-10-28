"""Adaptation: who is driving, on what, with which aids -> how the engineer coaches.

Recomputed after every lap from data (no settings needed):
  game + OS (platform), input device (keyboard / controller-wheel), assists (TC, ABS), skill tier from recent laps.
The tier changes how picky the in-lap calls are, how much praise there is and whether sector deltas are worth airtime;
assists switch off advice that can't apply (no lock-up coaching with ABS on).
"""
from __future__ import annotations

import numpy as np

TIERS = {
    #             call losses above  praise chance  sector calls  explanation depth for the LLM
    "learning":   {"loss_call_s": 0.40, "praise": 0.9, "sectors": False, "style": "explain simply, one fix at a time, encourage"},
    "developing": {"loss_call_s": 0.25, "praise": 0.6, "sectors": True, "style": "clear technique + numbers, one or two priorities"},
    "quick":      {"loss_call_s": 0.12, "praise": 0.3, "sectors": True, "style": "terse, numbers first, hundredths matter"},
}


def skill_tier(reports: list) -> tuple[str, dict]:
    """From the last few analysed laps: consistency, mistakes per lap, typical gap to the reference."""
    reps = reports[-8:]
    if len(reps) < 2:
        return "developing", {"laps": len(reps)}
    times = np.array([r.lap_time for r in reps])
    rep_times = times[times <= times.min() * 1.07]
    std = float(np.std(rep_times)) if len(rep_times) > 1 else 0.0
    mistakes = sum(len(r.mistakes) for r in reps) / len(reps)
    gap = float(np.median([r.delta for r in reps]))
    if mistakes >= 1.0 or std > 1.5 or gap > 3.0:
        tier = "learning"
    elif std < 0.5 and mistakes < 0.3 and gap < 0.6:
        tier = "quick"
    else:
        tier = "developing"
    return tier, {"laps": len(reps), "consistency_std_s": round(std, 2), "mistakes_per_lap": round(mistakes, 2),
                  "median_gap_to_ref_s": round(gap, 2)}


def profile(engine) -> dict:
    """Everything the engineer adapts to, as one dict (also shown on the dashboard)."""
    from f1engineer.platform_info import capabilities
    tr = engine.tr
    st = tr.status[tr.player_idx] if tr.status and tr.status[tr.player_idx] else {}
    abs_on = bool(st.get("antiLockBrakes")) if st else False
    tc = {0: "off", 1: "medium", 2: "full"}.get(st.get("tractionControl"), "?") if st else "?"
    tier, evidence = skill_tier(engine.reports)
    caps = capabilities()
    return {
        "game": engine.game, "os": caps["os"], "input": engine.input_device, "abs": abs_on, "tc": tc,
        "tier": tier, "evidence": evidence, **TIERS[tier],
        "coach_lockups": not abs_on,             # ABS on: the car can't lock, don't coach it
        "coach_wheelspin": tc != "full",         # full TC: wheelspin is the aid's job
        "keyboard": engine.input_device == "keyboard",
        "ptt": {"keyboard": caps["keyboard_ptt"], "game_buttons": True, "xinput": caps["xinput_pad"],
                "note": caps["keyboard_ptt_note"]},
    }


KEYBOARD_TIPS = {   # keyboard versions of the next-lap instructions (inputs are on/off)
    "coasted": "tap the throttle in short pulses instead of coasting",
    "full throttle": "start pulsing the throttle earlier on exit, then hold it once the wheel is straight",
    "wheelspin": "short throttle taps until the car is straight, or more traction control",
    "locked": "lighter taps on the brake as you turn in, or turn ABS on",
    "apex speed": "fewer, shorter steering taps mid-corner to carry more speed",
}
