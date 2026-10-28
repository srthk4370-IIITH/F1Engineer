"""Situational reading across laps: what a real engineer notices without being asked.

  - recurring corner losses: one bad lap in a corner is noise; three laps running is a pattern worth a call,
    said once (with the usual cause), then a word of praise when it's fixed
  - pace trend, with context: slower laps while the tyres wear is the car, not the driver; slower laps on healthy
    tyres is the driver; in races the burning fuel should make the car quicker (~0.05 s/lap), so flat lap times
    on a lighter car mean the tyres are going
"""
from __future__ import annotations

import random
from collections import Counter, deque

import numpy as np

from f1engineer import radio

FUEL_GAIN_S_PER_LAP = 0.05     # lighter car each lap (game cars burn ~1.5 kg/lap at ~0.03 s/kg)


class Patterns:
    def __init__(self, engine):
        self.eng = engine
        self.corners: dict[str, deque] = {}        # corner -> recent (loss, main cause)
        self.flagged: dict[str, int] = {}          # corner -> lap number when we called it
        self.laps: deque = deque(maxlen=8)         # (lap, time, avg wear %, valid)
        self.said_trend_at = -99

    def on_report(self, rep, wear_pct: float | None) -> list[radio.Message]:
        now = radio.CLOCK()
        out: list[radio.Message] = []
        thr = max(0.08, self.eng.adapt.get("loss_call_s", 0.25) / 2)
        for c in rep.corners:
            h = self.corners.setdefault(c["corner"], deque(maxlen=4))
            h.append((c["time_loss"], (c["causes"] or [""])[0]))
        for name, h in self.corners.items():
            losses = [x[0] for x in h]
            if name in self.flagged:
                # fixed? two laps clearly better than the threshold after we raised it
                if len(losses) >= 2 and all(v < thr / 2 for v in losses[-2:]):
                    del self.flagged[name]
                    out.append(radio.Message(5, now, random.choice([
                        f"{name}'s sorted now, nice work.", f"That's better through {name}. Keep it there.",
                        f"{name} fixed, good. That's time in the bank."]), "straight", 90, "pattern_fixed"))
                continue
            recent = losses[-3:]
            if len(recent) == 3 and all(v > thr for v in recent):
                cause = Counter(x[1] for x in list(h)[-3:] if x[1]).most_common(1)
                why = f" Same thing each time: {cause[0][0]}." if cause and cause[0][1] >= 2 else ""
                self.flagged[name] = rep.lap_num
                out.append(radio.Message(2, now, random.choice([
                    f"{name} again, that's three laps running, about {radio.fmt_d(np.mean(recent))} each time.{why}",
                    f"We keep losing {radio.fmt_d(np.mean(recent))} in {name}, three laps now.{why}",
                    f"Pattern in {name}: {radio.fmt_d(np.mean(recent))} a lap for three laps.{why}"]),
                    "straight", 120, f"pattern_{name}", evidence=float(np.mean(recent))))
                break   # one pattern call per lap is plenty
        self.laps.append((rep.lap_num, rep.lap_time, wear_pct, rep.valid))
        t = self._trend()
        if t:
            out.append(t)
        return out

    def _trend(self) -> radio.Message | None:
        laps = [x for x in self.laps if x[1] > 0]
        if len(laps) < 4 or laps[-1][0] - self.said_trend_at < 4:
            return None
        times = np.array([x[1] for x in laps[-4:]])
        if times.max() > times.min() * 1.04:      # a messy lap in there: not a trend
            return None
        slope = float(np.polyfit(np.arange(len(times)), times, 1)[0])   # s per lap
        race = self.eng.tr.is_race
        expected = -FUEL_GAIN_S_PER_LAP if race else 0.0
        drift = slope - expected
        wears = [x[2] for x in laps[-4:] if x[2] is not None]
        wear_rate = (wears[-1] - wears[0]) / (len(wears) - 1) if len(wears) >= 2 else 0
        now = radio.CLOCK()
        msg = None
        if drift > 0.12:
            if wear_rate > 2 or (wears and wears[-1] > 45):
                msg = random.choice([
                    f"Pace is dropping about {radio.fmt_d(drift)} a lap. That's the tyres, not you. Look after them.",
                    f"Losing {radio.fmt_d(drift)} a lap to tyre wear. Smooth on the exits, it's normal at this stage."])
            else:
                msg = random.choice([
                    f"You've lost {radio.fmt_d(drift)} a lap over the last few, and the tyres are fine. Reset, refocus.",
                    f"Pace is drifting, {radio.fmt_d(drift)} a lap with good tyres. Back to your markers."])
        elif drift < -0.12 and not race:
            msg = random.choice([f"Getting quicker every lap, {radio.fmt_d(drift)} a lap. Keep building.",
                                 f"Nice trend, {radio.fmt_d(drift)} quicker each lap."])
        elif race and abs(slope) < 0.04 and wear_rate > 2:
            msg = "Lap times are flat while the fuel burns off, so the tyres are starting to go. Manage them."
        if msg:
            self.said_trend_at = laps[-1][0]
            return radio.Message(4, now, msg, "straight", 90, "trend")
        return None

    def summary(self) -> str:
        """For the AI's context: what the pit wall has noticed."""
        flagged = ", ".join(self.flagged) or "none"
        rec = []
        for name, h in self.corners.items():
            losses = [x[0] for x in h]
            if len(losses) >= 2 and np.mean(losses) > 0.1:
                rec.append(f"{name} avg +{np.mean(losses):.2f}s over {len(losses)} laps")
        return f"recurring corner losses: {'; '.join(rec) or 'none'}; already raised: {flagged}"
