"""Race engineer rules: watches the race every tick and makes the calls a real engineer would, unprompted.

Deterministic and instant (the AI strategist adds judgement on top, see engine._strategist). Each call has a key and a
cooldown so it is said once, not nagged; urgent ones (box calls, safety car) jump the radio queue.
"""
from __future__ import annotations

import numpy as np

from f1engineer import brain, radio
from f1engineer.telemetry import packets_2020 as P

HOT_SURFACE, COLD_SURFACE, HOT_BRAKES = 108, 75, 1050
SLICKS = {"soft", "medium", "hard"}


def surname(name: str) -> str:
    return (name or "").split()[-1].title() if name else "him"


class RaceEngineer:
    def __init__(self, engine):
        self.eng = engine
        self.last: dict[str, float] = {}       # key -> time said
        self.hot_since: dict[str, float] = {}
        self.penalties = 0
        self.plan: dict | None = None          # latest strategy simulation
        self.plan_lap = -1
        self.sc_seen = "none"

    def _due(self, key: str, cooldown: float) -> bool:
        now = radio.CLOCK()
        if now - self.last.get(key, -1e9) < cooldown:
            return False
        self.last[key] = now
        return True

    def _call(self, text: str, key: str, cooldown: float, urgent: bool = False, ttl: float = 25) -> None:
        if self._due(key, cooldown):
            self.eng.sched.push([radio.Message(0 if urgent else 2, radio.CLOCK(), text, "urgent" if urgent else "straight",
                                               ttl, key)])

    # ---- every tick (10 Hz) ----------------------------------------------------------------------------
    def tick(self) -> None:
        tr = self.eng.tr
        if not tr.is_race or tr.lapdata is None or tr.telemetry is None:
            return
        me = tr.player_idx
        L, T = tr.lapdata[me], tr.telemetry[me]
        st = tr.status[me] if tr.status and tr.status[me] else {}
        lap = int(L["currentLapNum"])
        total = int(tr.session.get("totalLaps") or 0)
        left = total - lap
        in_pit = int(L["pitStatus"]) > 0
        sc = P.SAFETY_CAR.get(tr.session.get("safetyCarStatus", 0), "none")
        comp = P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "?") if st else "?"
        wear = list(st.get("tyresWear") or [0, 0, 0, 0]) if st else [0, 0, 0, 0]
        age = int(st.get("tyresAgeLaps") or 0) if st else 0

        # safety car: is a stop cheap enough to take now?
        if sc != self.sc_seen:
            self.sc_seen = sc
            if sc in ("full", "virtual") and not in_pit:
                worth = left > 3 and (np.mean(wear) > 25 or age > 8 or not self._two_compounds_done(comp))
                name = "Safety car" if sc == "full" else "VSC"
                if worth:
                    self._call(f"{name}. Box, box this lap, it's a cheap stop.", "sc_box", 30, urgent=True)
                else:
                    self._call(f"{name}. Stay out. Keep the tyres and brakes warm, watch the delta.", "sc_stay", 30, urgent=True)
            elif sc == "none":
                self._call("Safety car in this lap. Get ready, tyres need heat.", "sc_end", 30, urgent=True)

        if in_pit or lap < 1:
            return
        rate = self.eng.tyres.laps_left()
        # tyres finished
        if max(wear) >= 68 or (rate is not None and rate <= 1 and left > 1):
            self._call(f"Tyres are done, {max(wear):.0f} percent. Box this lap.", "tyres_done", 90, urgent=True)
        elif rate is not None and rate <= 3 and left > rate + 1:
            self._call(f"Tyres going off in about {rate:.0f} laps. Window's opening.", "tyres_soon", 180)

        # temperatures (sustained, not a single kerb strike)
        surf = [int(x) for x in T["tyresSurfaceTemperature"]]
        fronts, rears = max(surf[2], surf[3]), max(surf[0], surf[1])
        self._sustained("rears_hot", rears > HOT_SURFACE and float(T["throttle"]) > 0.5,
                        f"Rears are hot, {rears} degrees. Easy on the exits, short shift.", 5)
        self._sustained("fronts_hot", fronts > HOT_SURFACE,
                        f"Fronts overheating, {fronts}. Less sliding into the corners.", 5)
        if lap >= 2 and sc == "none" and comp in SLICKS:
            self._sustained("tyres_cold", max(surf) < COLD_SURFACE,
                            f"Tyres are cold, {max(surf)} degrees. Push a bit to get heat in them.", 8)
        brakes = int(max(T["brakesTemperature"]))
        self._sustained("brakes_hot", brakes > HOT_BRAKES, f"Brakes are hot, {brakes}. Lift and coast into the big stops.", 4)

        # weather vs tyres
        weather = tr.session.get("weather", 0)
        if weather >= 3 and comp in SLICKS:
            self._call("It's raining. Box for inters when it gets slippery, I'll tell you.", "rain_now", 120)
        elif weather <= 1 and comp in ("inter", "wet") and lap > 2:
            self._call("Track's drying. Slicks soon, push the inters while they last.", "drying", 180)

        # damage
        wing = max(int(st.get("frontLeftWingDamage") or 0), int(st.get("frontRightWingDamage") or 0)) if st else 0
        if wing >= 25:
            self._call(f"Front wing damage, {wing} percent. Box for a new nose if the balance is bad.", "wing", 240)

        # penalties
        pen = int(L["penalties"])
        if pen > self.penalties:
            self._call(f"{pen} second penalty. Watch the track limits.", "penalty", 20, urgent=True)
        self.penalties = pen

    def battles(self, snap: dict) -> None:
        """Called every 2 s with the race snapshot: DRS fights, fuel, rain forecast."""
        a, b = snap.get("ahead") or {}, snap.get("behind") or {}
        if a.get("gap_s") is not None and 0.1 <= a["gap_s"] < 1.0:
            self._call(f"{surname(a.get('name'))} {a['gap_s']:.1f} ahead, you're in DRS. Overtake button on the straight.",
                       "battle_ahead", 90)
        if b.get("gap_s") is not None and 0.1 <= b["gap_s"] < 1.0:
            self._call(f"{surname(b.get('name'))} {b['gap_s']:.1f} behind, in DRS. Cover the inside into the braking zones.",
                       "battle_behind", 90)
        f = (snap.get("fuel") or {}).get("remaining_laps_delta")
        if f is not None and f < -0.3:
            self._call(f"Fuel's {abs(f):.1f} laps short. Lift and coast, lean mix on the straights.", "fuel", 300)
        comp = ((snap.get("tyres") or {}).get("compound") or "").lower()
        rain = next((w for w in snap.get("forecast", []) if w["weather"] in ("light rain", "heavy rain", "storm")
                     and 0 < w["offset_min"] <= 15), None)
        if rain and comp in SLICKS and "rain" not in snap.get("weather", ""):
            self._call(f"Rain expected in about {rain['offset_min']} minutes. We'll react, keep pushing.", "rain_soon", 600)

    def _sustained(self, key: str, cond: bool, text: str, seconds: float) -> None:
        now = radio.CLOCK()
        if not cond:
            self.hot_since.pop(key, None)
            return
        start = self.hot_since.setdefault(key, now)
        if now - start >= seconds:
            self._call(text, key, 120)

    def _two_compounds_done(self, comp: str) -> bool:
        stints = {h[2] for h in self.eng.tyres.history} | {comp}
        return len(stints & SLICKS) >= 2

    # ---- every lap ------------------------------------------------------------------------------------
    def on_lap(self, lap_num: int, snap: dict) -> None:
        """Pit window from the strategy simulation (rerun each lap with live wear and pace)."""
        tr = self.eng.tr
        total = int(tr.session.get("totalLaps") or 0)
        if not tr.is_race or total < 5:
            return
        d = self.eng._strategy_defaults()
        if not d.get("total_laps"):
            return
        try:
            self.plan = brain.evaluate_strategy(None, **{k: d[k] for k in ("total_laps", "current_lap", "start_compound",
                                                                              "tyre_age", "base_lap", "pit_loss") if k in d},
                                                wear_rate_soft=d.get("wear_rate_soft"))
        except Exception:
            return
        best = (self.plan.get("best_alternatives") or [None])[0]
        if not best or not best["stops"]:
            return
        stop = best["stops"][0]
        cur = int(d.get("current_lap", lap_num))
        if stop["lap"] == cur:
            self._call(f"Box, box. {stop['compound'].capitalize()}s.", "box_now", 60, urgent=True, ttl=40)
        elif stop["lap"] == cur + 1:
            self._call(f"Box next lap for {stop['compound']}s. Push now, use the battery.", "box_next", 60, ttl=60)
        elif stop["lap"] - cur in (3, 4) and self._due("plan_info", 600):
            self.eng.sched.push([radio.Message(4, radio.CLOCK(), f"Plan is to stop around lap {stop['lap']} for "
                                               f"{stop['compound']}s.", "straight", 60, "plan_info")])
