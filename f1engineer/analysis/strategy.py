"""Race strategy maths: gaps, tyre wear rate, fuel, ERS, pit and safety-car calls (deterministic)."""
from __future__ import annotations

import numpy as np

from f1engineer.telemetry import packets_2020 as P

# Approximate pit-lane time loss (s) per track; default 22 s.
PIT_LOSS = {"Silverstone": 20.5, "Monaco": 19.0, "Monza": 23.0, "Spa": 19.0, "Singapore": 29.0, "Melbourne": 21.0,
            "Sakhir": 22.0, "Catalunya": 21.5, "Montreal": 19.0, "Suzuka": 22.0, "Abu Dhabi": 20.5, "Austria": 20.0}
CHECKPOINT_M = 50.0


class GapTimer:
    """Gap between cars = time difference at the same total-distance checkpoint (robust, no speed guessing)."""

    def __init__(self):
        self.times: dict[int, dict[int, float]] = {}

    def update(self, lapdata, session_time: float) -> None:
        for i in range(22):
            td = float(lapdata[i]["totalDistance"])
            if td <= 0:
                continue
            cp = int(td // CHECKPOINT_M)
            d = self.times.setdefault(i, {})
            if cp not in d:
                d[cp] = session_time
                if len(d) > 400:
                    for k in sorted(d)[:100]:
                        del d[k]

    def gap(self, me: int, other: int) -> float | None:
        """+ = other is ahead of me by N seconds."""
        a, b = self.times.get(me), self.times.get(other)
        if not a or not b:
            return None
        common = set(a) & set(b)
        if not common:
            return None
        cp = max(common)
        return a[cp] - b[cp]


class TyreModel:
    """Per-lap wear tracking for the player -> wear rate and laps to the cliff."""

    CLIFF = 70.0  # % average wear where pace falls off sharply in F1 2020

    def __init__(self):
        self.history: list[tuple[int, float, str]] = []  # (lap, avg wear %, compound)

    def on_lap(self, lap_num: int, status: dict) -> None:
        if not status:
            return
        wear = float(np.mean(status.get("tyresWear", [0, 0, 0, 0])))
        comp = P.VISUAL_COMPOUND.get(status.get("visualTyreCompound", 0), "?")
        if self.history and comp != self.history[-1][2]:
            self.history.clear()  # new stint
        self.history.append((lap_num, wear, comp))

    def rate(self) -> float | None:
        if len(self.history) < 2:
            return None
        laps = np.array([h[0] for h in self.history], float)
        wear = np.array([h[1] for h in self.history], float)
        return float(np.polyfit(laps, wear, 1)[0]) if np.ptp(laps) > 0 else None

    def laps_left(self) -> float | None:
        r = self.rate()
        if not r or r <= 0 or not self.history:
            return None
        return max(0.0, (self.CLIFF - self.history[-1][1]) / r)


def race_snapshot(tr, gaps: GapTimer, tyres: TyreModel) -> dict:
    """Everything the radio/LLM needs for race strategy, as plain numbers."""
    me = tr.player_idx
    out: dict = {"session": tr.session_type, "track": tr.track}
    if tr.lapdata is None:
        return out
    L = tr.lapdata[me]
    pos = int(L["carPosition"])
    out.update({"position": pos, "lap": int(L["currentLapNum"]), "total_laps": tr.session.get("totalLaps"),
                "pit_status": int(L["pitStatus"]), "penalties_s": int(L["penalties"])})
    ahead = next((i for i in range(22) if int(tr.lapdata[i]["carPosition"]) == pos - 1), None)
    behind = next((i for i in range(22) if int(tr.lapdata[i]["carPosition"]) == pos + 1 and int(tr.lapdata[i]["resultStatus"]) == 2), None)
    if ahead is not None:
        g = gaps.gap(me, ahead)
        out["ahead"] = {"name": tr.car_name(ahead), "gap_s": None if g is None else round(g, 2)}
    if behind is not None:
        g = gaps.gap(behind, me)
        out["behind"] = {"name": tr.car_name(behind), "gap_s": None if g is None else round(g, 2)}
    st = tr.status[me] if tr.status else {}
    if st:
        out["tyres"] = {"compound": P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "?"), "age_laps": st.get("tyresAgeLaps"),
                        "wear_pct": st.get("tyresWear"), "wear_rate_per_lap": None if tyres.rate() is None else round(tyres.rate(), 2),
                        "laps_to_cliff": None if tyres.laps_left() is None else round(tyres.laps_left(), 1)}
        out["fuel"] = {"remaining_laps_delta": round(float(st.get("fuelRemainingLaps", 0)), 2), "mix": st.get("fuelMix")}
        out["ers"] = {"store_pct": round(float(st.get("ersStoreEnergy", 0)) / 4e6 * 100), "mode": st.get("ersDeployMode")}
        dmg = {k: st.get(k) for k in ("frontLeftWingDamage", "frontRightWingDamage", "rearWingDamage") if st.get(k)}
        if dmg:
            out["damage"] = dmg
    if tr.telemetry is not None:
        T = tr.telemetry[me]
        out["tyre_temps_surface"] = [int(x) for x in T["tyresSurfaceTemperature"]]
        out["brake_temps"] = [int(x) for x in T["brakesTemperature"]]
    out["safety_car"] = P.SAFETY_CAR.get(tr.session.get("safetyCarStatus", 0), "none")
    out["weather"] = P.WEATHER.get(tr.session.get("weather", 0), "?")
    out["forecast"] = tr.session.get("forecast", [])[:5]
    out["pit_loss_s"] = PIT_LOSS.get(tr.track, 22.0)
    out["calls"] = strategy_calls(out)
    return out


def strategy_calls(s: dict) -> list[str]:
    calls = []
    t = s.get("tyres") or {}
    left = t.get("laps_to_cliff")
    laps_remaining = (s.get("total_laps") or 0) - (s.get("lap") or 0)
    if left is not None and laps_remaining > left + 1:
        calls.append(f"tyres reach the cliff in ~{left:.0f} laps with {laps_remaining} to go: plan a stop")
    if s.get("safety_car") in ("full", "virtual") and t.get("wear_pct") and np.mean(t["wear_pct"]) > 25 and laps_remaining > 3:
        calls.append(f"{s['safety_car']} safety car: pit loss is much smaller now, box for fresh tyres")
    f = s.get("fuel", {}).get("remaining_laps_delta")
    if f is not None and f < -0.3:
        calls.append(f"fuel is {abs(f):.1f} laps short: lift and coast or lean mix")
    fc = [w for w in s.get("forecast", []) if "rain" in w["weather"] or w["weather"] == "storm"]
    if fc and "rain" not in s.get("weather", ""):
        calls.append(f"rain expected in ~{fc[0]['offset_min']} min")
    behind = s.get("behind", {}).get("gap_s")
    if behind is not None and behind < 1.0:
        calls.append(f"{s['behind']['name']} is within DRS, {behind:.1f}s behind: defend into the braking zones")
    ahead = s.get("ahead", {}).get("gap_s")
    if ahead is not None and ahead < 1.0:
        calls.append(f"you're in DRS range of {s['ahead']['name']}, {ahead:.1f}s: use ERS overtake on the straight")
    return calls
