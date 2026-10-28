"""Proactive in-lap coaching: the engineer speaks up without being asked, like a real pit wall.

Runs on the engine's 10 Hz tick. All template based (zero latency, no LLM):
  - corner just finished: lock-up, off track, big time loss (with the cause), or a strong gain
  - lap invalidated, lap clearly gone (delta blown), sector deltas, "you're up, bring it home"
  - out lap / in lap / pit exit context
"""
from __future__ import annotations

import random

import numpy as np

from f1engineer import radio
from f1engineer.analysis import coach
from f1engineer.analysis import lapmath as LM

# F1 2020 LapData.driverStatus
GARAGE, FLYING, IN_LAP, OUT_LAP, ON_TRACK = 0, 1, 2, 3, 4
CHANNELS = ("t", "speed", "throttle", "brake", "gear", "steer", "slip_fl", "slip_fr", "slip_rl", "slip_rr",
            "surface", "wheels_angle", "yaw_rate")


def pick(*options: str) -> str:
    return random.choice(options)


class LiveCoach:
    LOSS_CALL_S = 0.25      # corner loss worth interrupting a lap for
    GAIN_CALL_S = 0.20
    LAP_GONE_S = 2.0

    def __init__(self, engine):
        self.eng = engine
        self.lap_num = -1
        self.next_corner = 0
        self.invalid_called = False
        self.gone_called = False
        self.bring_home_called = False
        self.status = -1
        self._race_mistake_said = -1e9
        self.statuses: dict[int, set[int]] = {}   # lap number -> driver statuses seen (out lap detection)

    # ---- helpers -----------------------------------------------------------------
    SUMMARY_KEYS = {"lap_head", "loss0", "loss1", "gain", "focus"}

    def _push(self, text: str, priority: int = 1, ttl: float = 12.0, key: str = "live", kind: str = "straight",
              evidence: float = 0.0) -> None:
        if key in ("live", "sector") and any(m.key in self.SUMMARY_KEYS for m in self.eng.sched.queue):
            return  # the last lap's debrief goes first; a stale corner call would only bury it
        self.eng.sched.push([radio.Message(priority, radio.CLOCK(), text, kind, ttl, key, evidence=evidence)])

    @property
    def sass(self) -> int:
        return self.eng.cfg.sass

    def was_out_lap(self, lap_num: int) -> bool:
        return OUT_LAP in self.statuses.get(lap_num, set()) and FLYING not in self.statuses.get(lap_num, set())

    def delta_now(self) -> float | None:
        ref, tr = self.eng.ref, self.eng.tr
        if not ref or tr.lapdata is None:
            return None
        L = tr.lapdata[tr.player_idx]
        d = float(L["lapDistance"])
        if not 0 <= d < ref["track_length"]:
            return None
        t, rt = float(L["currentLapTime"]), float(np.interp(d, ref["rs"]["dist"], ref["rs"]["t"]))
        if t < 0.5 * rt or t > 2 * rt + 10:   # around the line the timer resets before the distance does
            return None
        return t - rt

    # ---- per tick ----------------------------------------------------------------
    def tick(self) -> None:
        tr = self.eng.tr
        if tr.lapdata is None:
            return
        L = tr.lapdata[tr.player_idx]
        lap_num, status = int(L["currentLapNum"]), int(L["driverStatus"])
        self.statuses.setdefault(lap_num, set()).add(status)
        if lap_num != self.lap_num:
            self.lap_num, self.next_corner = lap_num, 0
            self.invalid_called = self.gone_called = self.bring_home_called = False
        if status != self.status:
            self._on_status(self.status, status)
            self.status = status
        if status not in (FLYING, ON_TRACK) and not tr.is_race:
            return
        if int(L["currentLapInvalid"]) and not self.invalid_called and status == FLYING:
            self.invalid_called = True
            if not tr.is_race:
                self._push(pick("Lap's invalid, track limits.", "That's a deleted lap. Track limits.",
                                "Lap invalid. Use it, learn the corners, go again.") + radio.sass("offtrack", self.sass),
                           priority=1, ttl=15, key="invalid")
        self._corner_check(float(L["lapDistance"]))
        self._delta_check(float(L["lapDistance"]), status)

    def _on_status(self, old: int, new: int) -> None:
        tr = self.eng.tr
        if new == OUT_LAP and old in (GARAGE, -1, IN_LAP):
            if tr.is_race:
                st = tr.status[tr.player_idx] if tr.status else {}
                comp = radio_compound(st)
                self._push(f"Out lap{' on the ' + comp if comp else ''}. Cold tyres, careful on the brakes for a few corners.",
                           priority=2, ttl=30, key="outlap")
            else:
                self._push(pick("Out lap. Get heat in the tyres and brakes, build up, then push from the last corner.",
                                "Out lap. Weave, brake hard a few times, get the temps up. Lap starts at the line.",
                                "Warm-up lap. Bring the tyres in, no heroics until the line."), priority=2, ttl=30, key="outlap")
        elif new == IN_LAP and old in (FLYING, ON_TRACK):
            self._push(pick("In lap. Cool it down, bring it home.", "That's it, in lap. Good work.", "Box this lap, in lap."),
                       priority=3, ttl=30, key="inlap")

    def _delta_check(self, dist: float, status: int) -> None:
        ref = self.eng.ref
        if status != FLYING or not ref or self.eng.tr.is_race:   # races are about position, not lap delta
            return
        d = self.delta_now()
        if d is None:
            return
        L = ref["track_length"]
        if not self.gone_called and d > self.LAP_GONE_S and dist > 0.15 * L:
            self.gone_called = True
            worst = self._worst_corner()
            tip = f" Use the rest to work on {worst}." if worst else " Use the rest of it as practice."
            self._push(pick(f"That lap's gone, {radio.fmt_d(d)} down.", f"{radio.fmt_d(d).capitalize()} down, this one's done.",
                            f"Lap's gone, {radio.fmt_d(d)} off.") + tip + radio.sass("slow_lap", self.sass),
                       priority=2, ttl=25, key="gone")
        elif not self.bring_home_called and not self.gone_called and d < -0.15 and dist > 0.72 * L \
                and not self.invalid_called:
            self.bring_home_called = True
            self._push(pick(f"You're {radio.fmt_d(d)} up. Bring it home.", f"Up {radio.fmt_d(d)}. Clean through the last sector.",
                            f"Purple pace, {radio.fmt_d(d)} up. Don't throw it away now."), priority=1, ttl=12, key="bringhome")

    def on_sector(self, sector: int) -> None:
        """Sector boundary (sector = index of the sector just started)."""
        if sector not in (1, 2) or self.status != FLYING or self.gone_called or self.eng.tr.is_race \
                or not self.eng.adapt.get("sectors", True):
            return
        d = self.delta_now()
        if d is None or abs(d) < 0.1:
            return
        name = "one" if sector == 1 else "two"
        self._push(f"Sector {name}, {radio.fmt_d(d)} {'up' if d < 0 else 'down'}.", priority=3, ttl=8, key="sector")

    def _worst_corner(self) -> str | None:
        reps = self.eng.reports[-3:]
        if not reps:
            return None
        tot: dict[str, float] = {}
        for r in reps:
            for c in r.corners:
                tot[c["corner"]] = tot.get(c["corner"], 0) + c["time_loss"]
        return max(tot, key=tot.get) if tot else None

    # ---- corner just finished --------------------------------------------------------
    def _corner_check(self, dist: float) -> None:
        eng = self.eng
        cs = eng.corners
        if not eng.ref or not cs or self.next_corner >= len(cs):
            return
        c = cs[self.next_corner]
        if dist < c.end + 10:   # a few grid points past the exit so the whole segment is real data
            return
        # skip any corners we already passed (e.g. after a flashback)
        while self.next_corner < len(cs) and cs[self.next_corner].end + 10 <= dist:
            self.next_corner += 1
        lap = eng.tr.player_lap()
        if lap is None or not lap.channels.get("dist") or lap.channels["dist"][0] > 50:
            return  # lap joined mid-way (restart / flashback to an earlier lap): no trustworthy delta
        m = self._metrics(lap, c, dist)
        if m is None or abs(m.time_loss) > 3:
            return
        name, sl = c.name, self.sass
        causes = coach._causes(m)
        ad = eng.adapt
        if m.lockup and ad.get("coach_lockups", True):
            txt = pick(f"Lock-up into {name}.", f"Locked the fronts at {name}.", f"Lock-up, {name}.")
            txt += " " + pick("Ease the brake pressure as you turn.", "Brake a touch earlier, then release smoothly.",
                              "Less pedal at the end of the braking zone.") + radio.sass("lockup", sl)
        elif m.off_track:
            txt = pick(f"Off at {name}.", f"Wide at {name}, all four over.", f"Track limits at {name}.") + radio.sass("offtrack", sl)
        elif eng.tr.is_race:
            return  # in a race only mistakes are worth the airtime
        elif m.time_loss > ad.get("loss_call_s", self.LOSS_CALL_S):
            cause = next((x for x in causes if "track" not in x), None)
            txt = f"{name}, {radio.fmt_d(m.time_loss)} lost" + (f": {cause}." if cause else ".") + radio.sass("loss", sl)
        elif m.time_loss < -self.GAIN_CALL_S and random.random() < ad.get("praise", 0.6):
            txt = pick(f"Good job at {name}, {radio.fmt_d(m.time_loss)} up there.",
                       f"{name} was mega. {radio.fmt_d(m.time_loss).capitalize()} gained.", f"Nice through {name}. Keep doing that.")
        else:
            return
        if eng.tr.is_race:   # in a race, mistakes are worth a word now and then, not a running commentary
            now = radio.CLOCK()
            if now - self._race_mistake_said < 120:
                return
            self._race_mistake_said = now
        self._push(txt, priority=1, ttl=12, key="live", evidence=max(0.0, m.time_loss))

    def _metrics(self, lap, c: LM.Corner, dist: float):
        """Corner metrics for the in-progress lap (grid-resampled up to the current distance, padded with the reference)."""
        ref = self.eng.ref["rs"]
        # the telemetry thread keeps appending while we read: snapshot every channel to the same length
        live = lap.channels
        m = min(len(v) for v in live.values())
        ch = {k: v[:m] for k, v in list(live.items())}
        d = np.asarray(ch["dist"], dtype=np.float64)
        ok = d >= -1
        if ok.sum() < 20:
            return None
        d = np.maximum.accumulate(d[ok])
        d, idx = np.unique(d, return_index=True)
        n = len(ref["dist"])
        upto = min(n, int(dist / LM.GRID_M) - 1)
        grid = ref["dist"][:upto]
        cur = {}
        for k in CHANNELS:
            if k not in ch:
                continue
            v = np.asarray(ch[k], dtype=np.float64)[ok][idx]
            arr = np.array(ref[k], dtype=np.float64, copy=True) if k in ref else np.zeros(n)
            arr[:upto] = np.interp(grid, d, v)
            cur[k] = arr
        if "t" not in cur:
            return None
        cur["dist"] = ref["dist"]
        delta = LM.delta_trace(cur, ref)
        return LM.corner_metrics(cur, ref, delta, [c])[0]


def radio_compound(st: dict) -> str:
    from f1engineer.telemetry import packets_2020 as P
    return P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "") if st else ""
