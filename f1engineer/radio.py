"""Radio messages: zero-latency templates (with configurable sass) + a scheduler that only
speaks when it is safe: on straights, under SC/VSC, in the garage/pits, or when paused.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

WORDS_PER_SEC = 2.7  # Piper at normal rate
CLOCK = time.time  # replaced by replay/tests to run on capture time

SASS = {
    "loss": [
        "Be a man, brake later.", "My nan carries more speed through there.", "Commit to it!",
        "That corner owes you money now.", "You're driving it like a rental.", "Less hope, more throttle.",
    ],
    "lockup": ["Flat-spotting tyres isn't a strategy.", "We heard that lock-up from the pit wall.", "Smoke show. Nice. Now stop."],
    "offtrack": ["The track's the grey bit, mate.", "Scenic route again?", "Grass doesn't count as a shortcut."],
    "good": ["Look at you, actually driving.", "Who are you and what did you do with my driver?", "Lovely. Do it again."],
    "slow_lap": ["Was that a reconnaissance lap?", "Warm-up's over, let's go racing.", "Somewhere a stopwatch is crying."],
}


def sass(kind: str, level: int) -> str:
    if level <= 0 or kind not in SASS:
        return ""
    return " " + random.choice(SASS[kind]) if random.random() < 0.25 * level + 0.15 else ""


@dataclass(order=True)
class Message:
    priority: int                  # lower = more important
    created: float = field(compare=False)
    text: str = field(compare=False)
    kind: str = field(default="straight", compare=False)   # urgent | straight | debrief | garage
    ttl: float = field(default=45.0, compare=False)
    key: str = field(default="", compare=False)

    @property
    def duration(self) -> float:
        return len(self.text.split()) / WORDS_PER_SEC + 0.4

    def expired(self, now: float) -> bool:
        return now - self.created > self.ttl


def fmt_t(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:06.3f}" if m else f"{sec:.3f}"


_NUM = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]


def fmt_d(d: float) -> str:
    """Time gap the way an engineer says it: 'six tenths', 'four hundredths', '1.3 seconds'."""
    d = abs(d)
    if d >= 1:
        return f"{d:.1f} seconds"
    if d >= 0.095:
        n = round(d * 10)
        return "a tenth" if n == 1 else f"{_NUM[n]} tenths"
    n = max(1, round(d * 100))
    return "a hundredth" if n == 1 else f"{_NUM[n]} hundredths"


# ---- templates -------------------------------------------------------------
def tt_lap_messages(rep, sass_level: int, is_best: bool) -> list[Message]:
    now = CLOCK()
    msgs = []
    if is_best and rep.valid:
        head = f"Lap {fmt_t(rep.lap_time)}. New best."
        if rep.ref_time and rep.delta < 0:
            head += f" {fmt_d(rep.delta)} up on {rep.ref_label}."
        head += sass("good", sass_level)
    else:
        head = f"Lap {fmt_t(rep.lap_time)}, {fmt_d(rep.delta)} {'down on' if rep.delta > 0 else 'up on'} {rep.ref_label}."
        if rep.delta > 2:
            head += sass("slow_lap", sass_level)
    if not rep.valid:
        head += " Invalid though, track limits." if rep.delta < 0 else " And it's invalid."
    msgs.append(Message(1, now, head, "straight", 60, "lap_head"))
    for i, c in enumerate(rep.losses[:2]):
        cause = c["causes"][0] if c["causes"] else "slower through there"
        t = f"{c['corner']}: {cause}. That cost {fmt_d(c['time_loss'])}."
        t += sass("lockup" if c["lockup"] else "offtrack" if c["off_track"] else "loss", sass_level)
        msgs.append(Message(2 + i, now, t, "straight", 90, f"loss{i}"))
    if rep.gains:
        g = rep.gains[0]
        msgs.append(Message(5, now, f"Good job in {g['corner']}, gained {fmt_d(g['time_loss'])}.", "straight", 90, "gain"))
    return msgs


def lap_debrief_text(rep, advice: list[dict]) -> str:
    """Longer garage/pause debrief (template version; the LLM can expand it)."""
    parts = [f"Lap {rep.lap_num}: {fmt_t(rep.lap_time)}, {rep.delta:+.3f} versus {rep.ref_label}. You were on {rep.input_device}."]
    for c in rep.losses[:3]:
        parts.append(f"{c['corner']} cost {fmt_d(c['time_loss'])}: " + ", ".join(c["causes"][:3]) + ".")
    for g in rep.gains[:2]:
        parts.append(f"You were quicker in {g['corner']} by {fmt_d(g['time_loss'])}, keep that.")
    for a in advice[:2]:
        parts.append(f"Suggestion: {a['change']}, because {a['why']}.")
    return " ".join(parts)


def race_messages(snap: dict, sass_level: int, last_calls: set[str]) -> list[Message]:
    now = CLOCK()
    msgs = []
    for call in snap.get("calls", []):
        if call in last_calls:
            continue
        urgent = "safety car" in call or "fuel" in call
        msgs.append(Message(0 if urgent else 3, now, call[0].upper() + call[1:] + ".", "urgent" if urgent else "straight", 40, call))
    return msgs


def race_lap_message(snap: dict, lap_time: float) -> Message:
    bits = [f"P{snap.get('position')}. Lap {fmt_t(lap_time)}."]
    a, b = snap.get("ahead", {}), snap.get("behind", {})
    if a.get("gap_s") is not None:
        bits.append(f"Gap ahead {a['gap_s']:.1f}.")
    if b.get("gap_s") is not None:
        bits.append(f"Behind {b['gap_s']:.1f}.")
    t = snap.get("tyres", {})
    if t.get("wear_pct"):
        bits.append(f"Tyres {max(t['wear_pct'])} percent worn.")
    return Message(2, CLOCK(), " ".join(bits), "straight", 30, "race_lap")


# ---- scheduler ---------------------------------------------------------------
class RadioScheduler:
    """Decides *when* a queued message may be spoken."""

    def __init__(self, say):
        self.say = say          # callable(text, priority)
        self.queue: list[Message] = []
        self.busy_until = 0.0
        self.spoken_keys: dict[str, float] = {}

    def push(self, msgs: list[Message]) -> None:
        for m in msgs:
            self.queue = [q for q in self.queue if not (m.key and q.key == m.key)]
            self.queue.append(m)
        self.queue.sort()

    def clear(self) -> None:
        self.queue.clear()

    def window_seconds(self, ctx: dict) -> float:
        """How many seconds of safe talking time we have right now."""
        if ctx.get("paused") or ctx.get("in_garage") or ctx.get("pit"):
            return 30.0
        if ctx.get("safety_car") in ("full", "virtual"):
            return 20.0
        if ctx.get("throttle", 0) < 0.9 or abs(ctx.get("steer", 0)) > 0.35 or ctx.get("brake", 0) > 0.05:
            return 0.0
        tb = ctx.get("time_to_brake")
        return 0.0 if tb is None else max(0.0, tb - 0.8)

    def tick(self, ctx: dict) -> None:
        now = CLOCK()
        self.queue = [m for m in self.queue if not m.expired(now)]
        if not self.queue or now < self.busy_until:
            return
        win = self.window_seconds(ctx)
        # Highest priority goes first; lower-priority messages may jump the queue only if the
        # top one has waited > 15 s for a long-enough straight.
        top = self.queue[0]
        candidates = self.queue if now - top.created > 15 else [top]
        parked = bool(ctx.get("paused") or ctx.get("in_garage"))
        for m in candidates:
            if m.kind == "garage":
                if not parked:
                    continue  # full debriefs only when the car is parked
            elif not (m.kind == "urgent" and win > 1.5 or m.duration <= win):
                continue
            self.queue.remove(m)
            self.busy_until = now + m.duration
            self.spoken_keys[m.key] = now
            self.say(m.text, m.priority)
            return
