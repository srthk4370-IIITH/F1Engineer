"""Instant answers for plain data questions (no AI, no latency, no cost): the AI is kept for real reasoning.

"Tyre temps?", "gap ahead?", "laps left?", "radio check" are lookups, answered straight from live telemetry the way an
engineer reads them off the screen. Anything asking for judgement ("why", "should I", "how do I", setup, strategy,
improvement) returns None and goes to the AI.
"""
from __future__ import annotations

import random
import re
import time

import numpy as np

from f1engineer import radio
from f1engineer.telemetry import packets_2020 as P

REASONING = re.compile(r"\b(why|should|how (do|can|to|would)|what if|better|improv|setup|strategy|explain|advice|"
                       r"recommend|think|worth|compare|style|learn|teach|wrong|mistake|faster|plan b|undercut|overcut)\b")


def _w(rx: str):
    return re.compile(rx)


COMMANDS = [  # radio commands: always instant, never the AI
    ("repeat", _w(r"^(say (that )?again|repeat( that)?|come again|what did you say)\b")),
    ("plan_status", _w(r"\b(plan status|where am i in the plan|how'?s the plan|what'?s next in the plan)\b")),
    ("plan_skip", _w(r"\b(skip (this |that )?step|next step)\b")),
    ("plan_stop", _w(r"\b(stop|cancel|end) the plan\b|\b(stop|cancel|end) plan\b")),
    ("stop", _w(r"^(stop|stop talking|shut up|enough|hold on|wait)\b")),
    ("quiet", _w(r"\b(quiet|less talking|talk less|silence|leave me alone|stop the chatter)\b")),
    ("radio_on", _w(r"\b(radio on|talk to me|you can talk|back on)\b")),
]
EXPLAIN = _w(r"^(explain( that| this| it)?|what do you mean|why( is that| that)?|how do i do that|meaning)\??$")

RIVAL = _w(r"(losing|lose|lost|gaining|gain|faster|quicker|slower|time)\b.{0,25}\b(to|than|against|vs|versus|on|compared to)\b"
           r"|compare me|me (vs|versus|against)|how do i compare")
LOSING = _w(r"losing time|where (am|was|did|do) i (slow|losing|lose|lost)|where'?s the time|where.{0,20}\bslow")
RACE_UPDATE = _w(r"race update|how'?s the race|race status|update me|sitrep|status update|what'?s happening|give me an update")

INTENTS = [  # (name, pattern) - first match wins
    ("radio_check", _w(r"radio check|how do you read|do you (read|copy|hear) me|can you hear")),
    ("brakes", _w(r"brake temp|brakes? (hot|temp)")),
    ("temps", _w(r"tyre temp|tire temp|temps?\b|temperature of (the )?tyres|tyres? (hot|cold)")),
    ("wear", _w(r"tyre wear|tire wear|\bwear\b|how (are|r) (the|my) tyres|tyres? (life|left|condition)")),
    ("compound", _w(r"what tyres|which tyres|compound|tyre age|how old")),
    ("gap_ahead", _w(r"gap (to the car )?(ahead|in front|forward)|who'?s ahead|car ahead|in front of me")),
    ("gap_behind", _w(r"gap (to the car )?behind|who'?s behind|car behind|behind me")),
    ("racing", _w(r"who am i racing|who'?s around|cars around")),
    ("position", _w(r"what position|which position|my position|where am i (placed|running)|\bp\?|\bplace\b")),
    ("laps", _w(r"laps? (left|to go|remaining)|what lap|which lap|how many laps")),
    ("fuel", _w(r"\bfuel\b")),
    ("ers", _w(r"\bers\b|battery|deploy|energy")),
    ("delta", _w(r"\bdelta\b|am i up|am i down")),
    ("last_lap", _w(r"last lap( time)?|lap time")),
    ("best_lap", _w(r"best lap|fastest lap|personal best|\bpb\b")),
    ("damage", _w(r"damage|car (ok|okay|fine|alright)|is the car")),
    ("weather", _w(r"weather|rain|track temp|air temp|conditions|is it (wet|dry)")),
    ("plan", _w(r"when (do|am) i (pit|box)|pit window|when.*box|what'?s the plan")),
]

CORNER = ["rear left", "rear right", "front left", "front right"]


ACKS = [  # instant, local: the driver knows he's been heard while the AI thinks
    (_w(r"setup|wing|diff|camber|toe|ride height|suspension|anti.?roll|brake bias|pressure"),
     ["Looking into setup changes.", "Let me check the setup.", "On it, looking at the setup."]),
    (_w(r"style|kind of driver|how do i drive"), ["Let me gather your driving style.", "Pulling up your driving style."]),
    (_w(r"improv|progress|getting (better|faster)"), ["Checking your progress.", "Let me look back over your laps."]),
    (_w(r"strategy|pit|box|stop|undercut|overcut|stint|one.?stop|two.?stop"),
     ["Running the strategy numbers.", "Checking the strategy.", "Let me run the numbers."]),
    (_w(r"lap|sector|corner|turn \d+|losing|slow|where|braking|apex|exit"),
     ["Looking at that lap.", "Checking the data.", "Let me look at the telemetry."]),
    (_w(r"remember|my goal"), ["Noted.", "Got it, saving that."]),
    (_w(r"why|explain|how (do|can|should)"), ["Good question, give me a second.", "Let me think about that."]),
]


def ack_for(question: str) -> str:
    q = question.lower()
    for rx, options in ACKS:
        if rx.search(q):
            return random.choice(options)
    return random.choice(["Copy, checking.", "We're checking.", "Stand by."])


def classify(question: str) -> str | None:
    q = question.lower().strip()
    cmd = next((name for name, rx in COMMANDS if rx.search(q)), None)
    if cmd:
        return cmd
    if RIVAL.search(q):
        return "rival"          # data answer in a race; elsewhere answer() hands it to the AI
    if RACE_UPDATE.search(q):
        return "race_update"
    if REASONING.search(q):
        return None
    if LOSING.search(q):
        return "losing"
    return next((name for name, rx in INTENTS if rx.search(q)), None)


def explain_request(eng, question: str) -> str | None:
    """'Explain that' -> a full question about the last radio call (goes to the AI)."""
    last = eng.last_message()
    if last and EXPLAIN.search(question.lower().strip(" .!")):
        return (f'Explain your last radio call properly: "{last}". What it means, why it matters, and exactly how to do '
                "it on track (where, when, how much). Use the data if it helps.")
    return None


def instant(question: str) -> bool:
    """Will this be answered without the AI? (no side effects)"""
    return classify(question) is not None


def answer(eng, question: str) -> str | None:
    intent = classify(question)
    if intent is None:
        return None
    if intent == "repeat":
        return eng.last_message() or "Nothing to repeat yet."
    if intent == "stop":
        eng.speech.hush()
        return "Okay."
    if intent == "quiet":
        eng.quiet_until = time.time() + 300
        return "Going quiet. Urgent calls only for five minutes. Say radio on to bring me back."
    if intent == "radio_on":
        eng.quiet_until = 0.0
        return "Radio's back on."
    if intent == "plan_status":
        return eng.plan.status() if eng.plan else "No plan running. Ask me for a practice plan."
    if intent == "plan_skip":
        return eng.plan.skip() if eng.plan else "No plan running."
    if intent == "plan_stop":
        eng.plan = None
        return "Plan stopped."
    if not eng.tr.session and eng.tr.lapdata is None and not eng.reports:
        return eng.no_data_line()   # a live-data question with no data yet: say so now, don't make the AI guess
    try:
        if intent in ("rival", "losing"):
            return rival_answer(eng, question)
        if intent == "race_update":
            return race_update(eng)
        return _answer(eng, intent)
    except Exception:
        return None   # missing data: let the AI handle it gracefully


def rival_answer(eng, question: str) -> str | None:
    """'Where am I losing time to Verstappen?' - corner by corner from live data (races; no name = the car ahead)."""
    from f1engineer.analysis import rival
    tr = eng.tr
    if not tr.is_race or tr.lapdata is None:
        return None            # practice/TT: the lap report (AI) answers this
    other = rival.find_car(eng, question)
    if other is None:
        return None
    return rival.radio_text(rival.compare(eng, other))


def race_update(eng) -> str | None:
    """Position, the cars around you with trends, tyres, fuel, laps left - in one breath."""
    tr = eng.tr
    if tr.lapdata is None:
        return None
    snap = eng.race_now()
    L = tr.lapdata[tr.player_idx]
    total = int(tr.session.get("totalLaps") or 0)
    lap = int(L["currentLapNum"])
    out = [f"P{snap.get('position')}" + (f", lap {lap} of {total}." if tr.is_race and total else ".")]
    a, b = snap.get("ahead") or {}, snap.get("behind") or {}
    if a.get("gap_s") is not None:
        out.append(f"{radio_name(a)} {a['gap_s']:.1f} ahead.{_trend(eng, 'ahead', a.get('name'), a['gap_s'])}")
    if b.get("gap_s") is not None:
        out.append(f"{radio_name(b)} {b['gap_s']:.1f} behind.{_trend(eng, 'behind', b.get('name'), b['gap_s'])}")
    ty = snap.get("tyres") or {}
    if ty.get("compound"):
        w = max(ty.get("wear_pct") or [0])
        out.append(f"{str(ty['compound']).capitalize()}s, {ty.get('age_laps')} laps, {w:.0f} percent worn.")
    f = (snap.get("fuel") or {}).get("remaining_laps_delta")
    if f is not None and f < 0.2:
        out.append(f"Fuel {f:+.1f}, lift and coast a bit.")
    for c in (snap.get("calls") or [])[:1]:
        out.append(c if c.endswith(".") else c + ".")
    return " ".join(out)


def offline_answer(eng, question: str) -> str | None:
    """The AI is unreachable: the best data answer we can give instead of a shrug."""
    q = question.lower()
    if eng.tr.lapdata is None:
        return None
    if eng.tr.is_race:
        if RIVAL.search(q) or LOSING.search(q) or "losing" in q or "faster" in q:
            r = rival_answer(eng, question)
            if r:
                return r
        return race_update(eng)
    return None


TOPICS = [(_w(r"setup|wing|diff|camber|toe|suspension|anti.?roll|brake bias|pressure"), "the setup"),
          (_w(r"style|kind of driver"), "your driving style"), (_w(r"improv|progress"), "your progress"),
          (_w(r"strategy|pit|box|stop|stint|undercut"), "strategy"), (_w(r"tyre|tire"), "the tyres"),
          (_w(r"lap|sector|corner|turn \d+|losing|slow|braking|apex"), "that lap")]


def lead_in(question: str) -> str:
    """Context for an answer that comes late or after something else: 'On the setup: ...'."""
    t = next((name for rx, name in TOPICS if rx.search(question.lower())), None)
    if t:
        return random.choice([f"On {t}: ", f"Back to your question on {t}: ", f"About {t}: "])
    return random.choice(["On what you asked: ", "Back to your question: "])


def _say(*options: str) -> str:
    return random.choice(options)


def _trend(eng, side: str, name: str, gap: float) -> str:
    """'closing' / 'pulling away' from the gap one lap ago to the same car."""
    hist = [h for h in eng.gap_hist if h.get(side + "_name") == name and h.get(side) is not None]
    if not hist:
        return ""
    d = gap - hist[-1][side]
    if abs(d) < 0.15:
        return _say(" Holding steady.", " Stable.", "")
    if side == "ahead":
        return _say(f" You've taken {abs(d):.1f} out of him in a lap.", " You're closing.") if d < 0 else \
            _say(f" He's pulling away, {d:.1f} that lap.", " He's stretching it a bit.")
    return _say(f" He's closing, {abs(d):.1f} a lap.", " He's coming.") if d < 0 else \
        _say(f" You're pulling away, {d:.1f} that lap.", " Gap's growing, good.")


def _answer(eng, intent: str) -> str | None:
    tr = eng.tr
    me = tr.player_idx
    T = tr.telemetry[me] if tr.telemetry is not None else None
    L = tr.lapdata[me] if tr.lapdata is not None else None
    st = tr.status[me] if tr.status and tr.status[me] else {}
    snap = eng.race_now() if tr.lapdata is not None else (eng.race or {})
    if intent == "radio_check":
        return _say("Loud and clear.", "Loud and clear, mate.", "Reading you fine.", "Five by five.")
    if intent == "temps" and T is not None:
        s = [int(x) for x in T["tyresSurfaceTemperature"]]
        base = _say(f"Fronts {s[2]}, {s[3]}. Rears {s[0]}, {s[1]}.",
                    f"Front left {s[2]}, front right {s[3]}, rears {s[0]} and {s[1]}.")
        hot = int(np.argmax(s))
        if max(s) > 108:
            judge = f" {CORNER[hot].capitalize()} is running hot, " + (
                "easy on the exits." if hot < 2 else "less sliding into the corners.")
        elif max(s) < 75:
            judge = _say(" They're cold. Push, weave, get some heat in.", " On the cold side, lean on them a bit.")
        else:
            judge = _say(" All in the window.", " Right where we want them.", " Good, they're working.")
        return base + judge
    if intent == "brakes" and T is not None:
        b = [int(x) for x in T["brakesTemperature"]]
        if max(b) > 1050:
            return f"Brakes are hot, {max(b)}. Lift and coast into the big stops."
        if max(b) < 300:
            return f"Brakes are cold, {max(b)}. Lean on them to bring them up."
        return _say(f"Brakes fine, {max(b)} max.", f"{max(b)} on the hottest one. No problem.")
    if intent == "wear" and st:
        w = st.get("tyresWear") or [0, 0, 0, 0]
        worst = max(w)
        left = eng.tyres.laps_left()
        nums = f"Fronts {w[2]:.0f}, {w[3]:.0f}, rears {w[0]:.0f}, {w[1]:.0f} percent."
        if worst < 30:
            judge = _say(" Plenty left.", " Loads of life in them.")
        elif worst < 60:
            judge = " Starting to go" + (f", about {left:.0f} laps before they drop off." if left is not None else ".")
        else:
            judge = " They're nearly done. We need to talk about stopping."
        return nums + judge
    if intent == "compound" and st:
        comp = P.VISUAL_COMPOUND.get(st.get("visualTyreCompound"), "unknown")
        age = int(st.get("tyresAgeLaps") or 0)
        return f"{comp.capitalize()}s, {age} lap{'s' if age != 1 else ''} old." + (" Fresh, use them." if age <= 2 else "")
    if intent in ("gap_ahead", "gap_behind", "racing"):
        a, b = snap.get("ahead") or {}, snap.get("behind") or {}
        parts = []
        if intent in ("gap_ahead", "racing"):
            if a.get("gap_s") is not None:
                g = a["gap_s"]
                extra = " You're in DRS." if g < 1 else ""
                parts.append(f"{radio_name(a)}, {g:.1f} ahead.{extra}{_trend(eng, 'ahead', a.get('name'), g)}")
            elif snap.get("position") == 1:
                parts.append(_say("Nobody ahead, you're leading.", "Clear road, you're P1."))
        if intent in ("gap_behind", "racing"):
            if b.get("gap_s") is not None:
                g = b["gap_s"]
                extra = " He's in DRS, cover the inside." if g < 1 else ""
                parts.append(f"{radio_name(b)}, {g:.1f} behind.{extra}{_trend(eng, 'behind', b.get('name'), g)}")
        return " ".join(parts) or None
    if intent == "position" and L is not None:
        pos = int(L["carPosition"])
        a = snap.get("ahead") or {}
        extra = (f" {radio_name(a)}'s {a['gap_s']:.1f} up the road, go get him."
                 if a.get("gap_s") is not None and a["gap_s"] < 1.5 else "")
        return f"P{pos}{' of ' + str(tr.num_active) if tr.num_active else ''}.{extra}"
    if intent == "laps" and L is not None:
        lap, total = int(L["currentLapNum"]), int(tr.session.get("totalLaps") or 0)
        if tr.is_race and total:
            left = max(0, total - lap)
            extra = " Last lap, bring it home." if left == 0 else " Bring it home." if left <= 3 else ""
            return f"Lap {lap} of {total}, {left} to go.{extra}"
        return f"That's lap {lap}."
    if intent == "fuel" and st:
        f = float(st.get("fuelRemainingLaps", 0))
        if f >= 0.2:
            return _say(f"Fuel's fine, plus {f:.1f}.", f"Plus {f:.1f} on fuel, no saving needed.")
        if f >= 0:
            return f"Fuel's tight but OK, plus {f:.1f}. A little lift and coast wouldn't hurt."
        return f"We're {abs(f):.1f} laps short. Lift and coast into the big stops, lean mix on the straights."
    if intent == "ers" and st:
        pct = round(float(st.get("ersStoreEnergy", 0)) / 4e6 * 100)
        if pct > 60:
            return _say(f"Battery {pct} percent, use it.", f"{pct} percent, plenty to attack with.")
        if pct < 20:
            return f"Battery's low, {pct} percent. Harvest a bit, save it for the straights that matter."
        return f"Battery {pct} percent."
    if intent == "delta":
        d = eng.coach.delta_now()
        if d is None:
            return None
        return f"{radio.fmt_d(d).capitalize()} {'up' if d < 0 else 'down'}." + (" Keep it clean." if d < -0.1 else "")
    if intent == "last_lap" and L is not None and float(L["lastLapTime"]) > 0:
        last, best = float(L["lastLapTime"]), float(L["bestLapTime"])
        if best > 0 and abs(last - best) < 0.005:
            return f"{radio.short_t(last)}, that's your best."
        tail = f", {radio.fmt_d(last - best)} off your best" if best > 0 else ""
        return f"Last lap {radio.short_t(last)}{tail}."
    if intent == "best_lap":
        best = float(L["bestLapTime"]) if L is not None else 0
        stored = eng.store.reference(tr.track)
        out = f"Best today {radio.short_t(best)}." if best > 0 else ""
        if stored and (best <= 0 or stored[1] < best - 0.01):
            out += f" All-time here {radio.short_t(stored[1])}."
        return out.strip() or None
    if intent == "damage" and st:
        dmg = {k: int(st.get(k) or 0) for k in ("frontLeftWingDamage", "frontRightWingDamage", "rearWingDamage")}
        if not any(dmg.values()):
            return _say("Car's fine, no damage.", "All good, car's healthy.", "Nothing showing, car's fine.")
        parts = [f"{n} {v} percent" for n, v in zip(("front left wing", "front right wing", "rear wing"), dmg.values()) if v]
        return ("Damage: " + ", ".join(parts) + "."
                + (" Balance will be off, we can box for a new one." if max(dmg.values()) >= 25 else " Minor, keep going."))
    if intent == "weather" and tr.session:
        s = tr.session
        rain = next((f["offset_min"] for f in s.get("forecast", []) if f.get("weather") in ("light rain", "heavy rain", "storm")
                     and f.get("offset_min", 0) > 0), None)
        return (f"Track {s.get('trackTemperature')}, air {s.get('airTemperature')}, "
                f"{radio.WEATHER_SAY.get(s.get('weather', 0), 'dry')}."
                + (f" Rain possible in about {rain} minutes, we'll call it." if rain else _say(" No rain coming.", " Stays dry.")))
    if intent == "plan":
        best = ((eng.race_eng.plan or {}).get("best_alternatives") or [None])[0]
        if best and best["stops"]:
            st0 = best["stops"][0]
            return f"Plan is lap {st0['lap']} for {st0['compound']}s. I'll call it."
        return None
    return None


def radio_name(car: dict) -> str:
    return (car.get("name") or "Car").split()[-1].title()
