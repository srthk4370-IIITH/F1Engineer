"""What each setup value means, its range per game, and safe changes (never outside the slider).

F1 2020/2021 ranges are exact. F1 22+ changed several sliders (wings 0-50, finer suspension and ride height steps);
those ranges are taken from the game where known and otherwise inferred from the values the game sends - a change
is never suggested past what the driver's own slider has shown.
"""
from __future__ import annotations

# param -> (label, step, what a HIGHER value does)
PARAMS = {
    "frontWing": ("front wing", 1, "more front downforce: sharper turn-in and more front grip in fast corners, less top speed"),
    "rearWing": ("rear wing", 1, "more rear downforce: more stable rear in fast corners and on traction, less top speed"),
    "onThrottle": ("on-throttle diff", 5, "more locked: better traction in a straight line but more understeer/inside-wheel push on exit; lower = more rotation, more inside-wheel spin"),
    "offThrottle": ("off-throttle diff", 5, "more locked: more stable on entry and lift-off, less rotation into the corner"),
    "frontCamber": ("front camber", 0.1, "less negative: less cornering grip, cooler and more even front tyres (more negative = more grip, more heat/wear)"),
    "rearCamber": ("rear camber", 0.1, "less negative: less rear cornering grip, cooler rear tyres"),
    "frontToe": ("front toe-out", 0.01, "sharper turn-in response, more tyre heat and drag"),
    "rearToe": ("rear toe-in", 0.03, "more rear stability (especially on entry and exit), more drag and heat"),
    "frontSuspension": ("front suspension", 1, "stiffer front: sharper response, less mechanical grip, more understeer over bumps and kerbs"),
    "rearSuspension": ("rear suspension", 1, "stiffer rear: more responsive rear, less traction"),
    "frontAntiRollBar": ("front anti-roll bar", 1, "stiffer front roll: quicker direction changes, more understeer mid-corner"),
    "rearAntiRollBar": ("rear anti-roll bar", 1, "stiffer rear roll: more rotation/oversteer, less traction"),
    "frontSuspensionHeight": ("front ride height", 1, "higher front: less downforce, safer over kerbs and bumps"),
    "rearSuspensionHeight": ("rear ride height", 1, "higher rear: more rake (more rotation, less stability), safer over bumps"),
    "brakePressure": ("brake pressure", 5, "more braking force per pedal %: shorter stops but easier to lock (keyboard: every press is 100%)"),
    "brakeBias": ("brake bias", 1, "more front bias: fronts lock first, more stable; less = more rotation under braking, rears can lock"),
    "frontLeftTyrePressure": ("front tyre pressure", 0.4, "higher: less rolling resistance and more heat, less grip"),
    "rearLeftTyrePressure": ("rear tyre pressure", 0.4, "higher: less rolling resistance and more heat, less traction"),
}

RANGES = {   # (min, max); F1 2020 values from the game
    2020: {"frontWing": (1, 11), "rearWing": (1, 11), "onThrottle": (50, 100), "offThrottle": (50, 100),
           "frontCamber": (-3.5, -2.5), "rearCamber": (-2.0, -1.0), "frontToe": (0.05, 0.15), "rearToe": (0.20, 0.50),
           "frontSuspension": (1, 11), "rearSuspension": (1, 11), "frontAntiRollBar": (1, 11), "rearAntiRollBar": (1, 11),
           "frontSuspensionHeight": (1, 11), "rearSuspensionHeight": (1, 11), "brakePressure": (50, 100),
           "brakeBias": (50, 70), "frontLeftTyrePressure": (21.0, 25.0), "rearLeftTyrePressure": (19.5, 23.5)},
}
RANGES[2021] = RANGES[2020]
RANGES[2022] = {**RANGES[2020], "frontWing": (0, 50), "rearWing": (0, 50), "frontSuspension": (1, 41),
                "rearSuspension": (1, 41), "frontAntiRollBar": (1, 21), "rearAntiRollBar": (1, 21),
                "frontSuspensionHeight": (10, 40), "rearSuspensionHeight": (40, 80), "onThrottle": (10, 100),
                "offThrottle": (10, 100)}
for _f in (2023, 2024, 2025, 2026):
    RANGES[_f] = RANGES[2022]


def ranges(fmt: int | None) -> dict:
    return RANGES.get(fmt or 2020, RANGES[2020])


def annotate(setup: dict, fmt: int | None) -> list[dict]:
    """The setup as the engineer should read it: value, slider range, and what moving it does."""
    rg, out = ranges(fmt), []
    for p, (label, _, higher) in PARAMS.items():
        if p not in setup or setup[p] is None:
            continue
        v = setup[p]
        lo, hi = rg.get(p, (None, None))
        out.append({"param": p, "label": label, "value": round(v, 2) if isinstance(v, float) else v, "range": [lo, hi],
                    "at_min": lo is not None and v <= lo + 1e-6, "at_max": hi is not None and v >= hi - 1e-6,
                    "higher_means": higher})
    return out


def change(setup: dict, fmt: int | None, param: str, steps: int) -> tuple[str, float, float] | None:
    """('front anti-roll bar 7 -> 6', old, new) clamped to the slider, or None when it can't move that way."""
    if param not in setup or setup[param] is None:
        return None
    label, step, _ = PARAMS[param]
    lo, hi = ranges(fmt).get(param, (-1e9, 1e9))
    old = setup[param]
    new = min(hi, max(lo, old + steps * step))
    if abs(new - old) < 1e-6:
        return None
    fmt_v = (lambda x: f"{x:.2f}".rstrip("0").rstrip(".")) if isinstance(step, float) else (lambda x: f"{int(round(x))}")
    unit = "%" if param in ("onThrottle", "offThrottle", "brakePressure", "brakeBias") else ""
    return f"{label} {fmt_v(old)}{unit} -> {fmt_v(new)}{unit}", old, new
