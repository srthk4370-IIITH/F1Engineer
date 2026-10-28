"""Radio messages: zero-latency templates (with configurable sass) + a scheduler that only
speaks when it is safe: on straights, under SC/VSC, in the garage/pits, or when paused.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

WORDS_PER_SEC = 3.4  # Piper at the default 1.3x speaking rate (engine sets it from the config)
CLOCK = time.time  # replaced by replay/tests to run on capture time

SASS = {   # rare, dry pit-wall humour (never on safety or strategy calls)
    "loss": ["Be a man, brake later.", "You're better than that.", "Commit to it."],
    "lockup": ["Easy on the tyres.", "We felt that one from here."],
    "offtrack": ["Track's the grey bit.", "Keep it on the black stuff."],
    "good": ["Lovely.", "That's more like it.", "Do that again."],
    "slow_lap": [],
}


def sass(kind: str, level: int) -> str:
    """At most occasionally: real engineers joke rarely, and it lands better that way."""
    if level <= 0 or not SASS.get(kind):
        return ""
    return " " + random.choice(SASS[kind]) if random.random() < 0.08 * level else ""


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
# Written the way a pit wall talks: short, number first, no filler. One idea per call.
def pick(*options: str) -> str:
    return random.choice(options)


def short_t(s: float) -> str:
    """Lap time as said on the radio: '1:29.1' (tenths are enough out loud)."""
    m, sec = divmod(s, 60)
    return f"{int(m)}:{sec:04.1f}" if m else f"{sec:.1f}"


WEATHER_SAY = {0: "dry", 1: "dry, bit of cloud", 2: "overcast but dry", 3: "light rain", 4: "heavy rain", 5: "storm"}


def radio_check(tr, compound: str, best: float | None) -> str:
    """Opening call built from the session: conditions, tyres, the target."""
    s = tr.session
    temps = f"track {int(s.get('trackTemperature', 0))}, air {int(s.get('airTemperature', 0))}"
    wx = WEATHER_SAY.get(s.get("weather", 0), "dry")
    rain = next((f["offset_min"] for f in s.get("forecast", []) if f.get("weather") in ("light rain", "heavy rain", "storm")
                 and f.get("offset_min", 0) > 0), None)
    tyre = f" {compound.capitalize()}s on." if compound else ""
    rain_say = f" Rain possible in about {rain} minutes." if rain and s.get("weather", 0) < 3 else ""
    if tr.is_time_trial:
        target = (pick(f" Best here is {short_t(best)}, let's beat it.", f" Target's your {short_t(best)}.")
                  if best else pick(" No time here yet, build up to it.", " First run here, take a couple of laps to find it."))
        return f"Radio check. {temps}, {wx}.{tyre}{target}"
    if tr.is_race:
        laps = s.get("totalLaps")
        grid = tr.lapdata[tr.player_idx]["gridPosition"] if tr.lapdata is not None else 0
        start = f" Starting P{int(grid)}." if grid else ""
        return f"Radio check. {laps} laps, {temps}, {wx}.{start}{tyre}{rain_say} Car's good."
    if tr.session_type.startswith(("Q", "Short Q", "OSQ", "Sprint")):
        return f"Radio check. {temps}, {wx}.{tyre}{rain_say} One clean lap, that's all we need."
    return f"Radio check. {temps}, {wx}.{tyre}{rain_say} Let's get a feel for the car."


def tt_lap_messages(rep, sass_level: int, is_best: bool) -> list[Message]:
    now = CLOCK()
    t = short_t(rep.lap_time)
    top = next((c for c in rep.losses if c["time_loss"] >= 0.15), None)
    where = f" Most of it in {top['corner']}." if top and rep.delta > 0.15 else ""
    if not rep.valid and rep.delta < 0:
        head = pick(f"{t}, but it's deleted. Track limits. Would've been {fmt_d(rep.delta)} quicker.",
                    f"Lap deleted, track limits. Shame, {t} was {fmt_d(rep.delta)} up.")
    elif is_best:
        head = pick(f"{t}, new best. {fmt_d(rep.delta).capitalize()} quicker.", f"Purple. {t}, {fmt_d(rep.delta)} up.",
                    f"That's a {t}, best of the day by {fmt_d(rep.delta)}.") + sass("good", sass_level)
    elif rep.delta > 2:
        head = pick(f"{t}. That one got away from you. Reset and go again.", f"{t}, {fmt_d(rep.delta)} off. Messy one, go again.")
    elif abs(rep.delta) < 0.05:
        head = f"{t}, right on your best."
    else:
        head = f"{t}, {fmt_d(rep.delta)} {'off' if rep.delta > 0 else 'up on'} {rep.ref_label}.{where}"
        if not rep.valid:
            head += " Deleted though."
    msgs = [Message(0, now, head, "straight", 60, "lap_head")]
    if top:
        cause = top["causes"][0] if top["causes"] else "slower there"
        msgs.append(Message(2, now, f"{top['corner']}, {fmt_d(top['time_loss'])}: {cause}."
                            + sass("lockup" if top["lockup"] else "offtrack" if top["off_track"] else "loss", sass_level),
                            "straight", 90, "loss0"))
    if rep.gains and rep.gains[0]["time_loss"] <= -0.15 and random.random() < 0.5:
        msgs.append(Message(5, now, pick(f"Good through {rep.gains[0]['corner']}.", f"{rep.gains[0]['corner']} was strong."),
                            "straight", 90, "gain"))
    return msgs


TIPS = [  # cause fragment -> what to do next lap
    ("locked", "ease off the brake as you turn in"),
    ("ran wide", "a metre tighter on exit, keep it on the track"),
    ("earlier", "brake later, the car will stop"),
    ("overshot", "brake a touch earlier and hit the apex"),
    ("apex speed", "carry more speed to the apex, less steering"),
    ("too much into the apex", "slower in, faster out"),
    ("full throttle", "get on the power earlier"),
    ("coasted", "no coasting, brake or throttle"),
    ("wheelspin", "smoother on the throttle"),
    ("exit speed", "straighten the wheel earlier on exit"),
]


def next_lap_messages(rep, advice: list[dict], said: set[str], adapt: dict | None = None) -> list[Message]:
    """What to work on next lap (biggest loss, turned into an instruction) + an occasional setup idea."""
    from f1engineer.adapt import KEYBOARD_TIPS
    now = CLOCK()
    msgs = []
    adapt = adapt or {}
    for c in rep.losses[:1]:
        if c["time_loss"] < 0.08:
            break
        tip = None
        if adapt.get("keyboard"):   # on/off inputs need different instructions
            tip = next((t for frag, t in KEYBOARD_TIPS.items() for cause in c["causes"] if frag in cause), None)
        tip = tip or next((t for frag, t in TIPS for cause in c["causes"] if frag in cause), None)
        if tip:
            msgs.append(Message(4, now, f"Next lap, {c['corner']}: {tip}.", "straight", 120, "focus"))
    for a in advice[:1]:
        if a["change"] not in said:
            said.add(a["change"])
            msgs.append(Message(6, now, f"When you're back in, try {a['change'][0].lower() + a['change'][1:]}.",
                                "straight", 240, "setup_tip"))
    return msgs


ISSUE_SAY = {"lockup": "a lock-up into {c}", "off": "running wide at {c}", "wheelspin": "wheelspin out of {c}",
             "coast": "coasting into {c}", "late_throttle": "late on the power out of {c}"}


def self_review_messages(lap_time: float, valid: bool, issues: list[dict], sass_level: int) -> list[Message]:
    """First lap at a track: no reference yet, so judge the lap on its own."""
    now = CLOCK()
    t = short_t(lap_time)
    head = pick(f"{t}, that's the benchmark.", f"{t}. Good start, that's our baseline.") + ("" if valid else " Deleted though.")
    if not issues:
        return [Message(0, now, head + " Clean, no mistakes. Now push.", "straight", 60, "lap_head")]
    said = [ISSUE_SAY.get(i.get("kind"), "{c}").format(c=i["corner"]) for i in issues[:2]]
    msgs = [Message(0, now, head, "straight", 60, "lap_head"),
            Message(2, now, "Saw " + " and ".join(said) + ".", "straight", 90, "loss0")]
    top = issues[0]
    msgs.append(Message(4, now, f"Next lap, {top['corner']}: {top['tip']}.", "straight", 120, "focus"))
    return msgs


def lap_debrief_text(rep, advice: list[dict]) -> str:
    """Garage debrief (template fallback; the LLM writes a better one when available)."""
    parts = [f"Lap {rep.lap_num}, {short_t(rep.lap_time)}, {fmt_d(rep.delta)} {'off' if rep.delta > 0 else 'up on'} {rep.ref_label}."]
    for c in rep.losses[:2]:
        parts.append(f"{c['corner']} cost {fmt_d(c['time_loss'])}, " + " and ".join(c["causes"][:2]) + ".")
    if rep.gains:
        parts.append(f"{rep.gains[0]['corner']} was good, keep that.")
    for a in advice[:1]:
        parts.append(f"For the setup, try {a['change'][0].lower() + a['change'][1:]}: {a['why']}.")
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
    """Lap-end race call: position, the gaps that matter, tyres only when they matter."""
    a, b = snap.get("ahead", {}), snap.get("behind", {})
    bits = [f"P{snap.get('position')}, {short_t(lap_time)}."]
    if a.get("gap_s") is not None and a["gap_s"] < 3:
        bits.append(f"{a.get('name', 'Car ahead').split()[-1].title()} {a['gap_s']:.1f} ahead.")
    if b.get("gap_s") is not None and b["gap_s"] < 2:
        bits.append(f"{b.get('name', 'Car behind').split()[-1].title()} {b['gap_s']:.1f} behind.")
    t = snap.get("tyres", {})
    if t.get("wear_pct") and max(t["wear_pct"]) >= 40:
        bits.append(f"Tyres at {max(t['wear_pct'])} percent.")
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
            if ctx.get("talking") and m.kind != "urgent":
                continue  # the driver is being answered: routine calls wait
            if m.kind == "garage":
                if not parked:
                    continue  # full debriefs only when the car is parked
            elif not (m.kind == "urgent" and win > 1.5 or m.duration <= win * (1.5 if now - m.created > 20 else 1.0)):
                continue  # after 20 s of waiting a message may run a little into the braking zone
            self.queue.remove(m)
            self.busy_until = now + m.duration
            self.spoken_keys[m.key] = now
            self.say(m.text, m.priority)
            return
