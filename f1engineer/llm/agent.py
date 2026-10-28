"""Race-engineer chat agent: tool-calling over live session data, streamed sentence by sentence.

Providers:
  OpenAIProvider: real model (needs OPENAI_API_KEY), streaming + parallel tool calls.
  MockProvider:   deterministic offline stand-in with the same interface (used for tests/no key).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable, Iterator

SYSTEM_PROMPT = """You are {name}, the driver's race engineer and driving coach for an F1 video game. You've worked
with this driver across many sessions: you know their history (tools), remember what they tell you, and you want them
faster. You lead; you don't wait to be asked.

Ground rules:
- Every number comes from the tools or the live context below. Never invent lap times, gaps, corners, setup values
  or trends. Missing data: say what you need in a few words ("give me two clean laps").
- Have opinions. If the driver's idea is worse than the data says, tell them and give the number. If it's good, say so.
- Never end with an offer or a question back ("Do you want me to...", "If you want, I can...", "Would you like...",
  "Let me know..."). Engineers don't offer, they tell. Finish on the instruction.
- No preamble, no recap of the question, no "Great question". Plain spoken English for text-to-speech: no markdown,
  lists or emojis, numbers said naturally.

Two different jobs, current mode: {mode}
CONVERSATION (radio, chat, review): the driver asked something. Answer like a mate on the pit wall: short, warm,
  a bit cheeky (sass {sass}/3; 0 = none), straight to the point.
  - radio: they're driving. One or two short sentences, number first, then the instruction.
  - chat: they're parked or paused. Two to four sentences, natural back-and-forth, banter welcome.
  - review: on the dashboard away from the game. Up to six sentences, can teach.
  Simple facts are often already in the live context: answer from it without tools.
ANALYSIS (analysis, strategist): your own initiative or an explicit "debrief / break it down / analyse".
  Investigate first with several tools (lap report, corner history, consistency, driving style, progress, setup,
  race history, strategy simulation), then deliver it like an engineer in the garage: what happened, why, and the
  one or two things to change with exactly how, plus a setup change with a value when the data supports it.
  Human language, prioritised, no lists. analysis: up to eight sentences. strategist: either exactly SILENT or one
  call of at most two sentences.
- Adapt to the game, input device, assists and skill tier in the context (simple and encouraging for a learner,
  hundredths-level for a quick driver). search_knowledge has notes per game ("Which game: ...") and per track.
- Be solution-oriented: every call ends with a concrete action (what to change, where, by how much).
- Memory: goals and facts -> remember. How they like to drive or be coached -> set_preference (a new one on the same
  topic replaces the old; when it does, acknowledge the change in a few words). Preferences are soft: follow them, but
  if the data clearly shows they cost time in a specific place, say so once with the number, then respect their call.

Driver profile (things they asked you to remember): {profile}
Live context: {context}"""

MODES = {  # reasoning effort and answer budget per mode
    "radio": {"effort": "none", "max_tokens": 250},
    "chat": {"effort": "none", "max_tokens": 400},
    "garage": {"effort": "none", "max_tokens": 400},       # old name for chat
    "review": {"effort": "low", "max_tokens": 900},
    "analysis": {"effort": "low", "max_tokens": 1200},     # deep, agentic: delivered on slow laps / in the garage
    "strategist": {"effort": "low", "max_tokens": 500},    # the engineer's own unprompted calls
}

_OFFER = re.compile(r"^(if you (want|like)|do you want|would you like|want me to|let me know|shall i|i can also|"
                    r"happy to|should i (also|go)|just say)", re.I)


def is_offer(sentence: str) -> bool:
    """GPT-style closing offers/questions; engineers don't end on those."""
    return bool(_OFFER.search(sentence.strip()))

TOOL_SCHEMAS = [
    ("get_session_overview", "Track, mode, lap count, best/last lap, input device, assists.", {}),
    ("get_last_lap_report", "Corner-by-corner analysis of a lap vs the reference: losses, gains, causes, mistakes.",
     {"lap": {"type": "integer", "description": "lap number; omit for the latest"}}),
    ("compare_laps", "Compare two of the driver's laps corner by corner.",
     {"lap_a": {"type": "integer"}, "lap_b": {"type": "integer"}}),
    ("get_corner_history", "How a named corner went across recent laps (time loss, speeds, mistakes).",
     {"corner": {"type": "string"}}),
    ("get_setup_and_advice", "Current car setup and rule-based setup/technique suggestions.", {}),
    ("get_race_status", "Position, gaps, tyres (wear, temps, laps to cliff), fuel, ERS, weather, safety car, strategy calls.", {}),
    ("get_consistency", "Which corners vary most lap to lap (fix these first).", {}),
    ("search_knowledge", "Driving technique / setup / track knowledge base.",
     {"query": {"type": "string"}}),
    ("get_car_status", "Live car: tyre surface/inner temps, pressures and wear per corner, compound and age, brake and "
     "engine temps, fuel margin, ERS, damage.", {}),
    ("get_rivals", "Who I'm racing: cars around my position with gaps, last/best laps, tyre compound and age, pit status.",
     {"around": {"type": "integer", "description": "positions either side (default 3)"}}),
    ("get_progress", "Long-term progress on a track across all sessions: best and representative pace per session, "
     "consistency, mistakes per lap, trend, per-corner improvement, first vs second half of this session.",
     {"track": {"type": "string", "description": "track name; omit for the current/latest track"}}),
    ("get_driving_style", "The driver's style from recent laps: braking (ramp, trail braking, brake points vs reference), "
     "apex speeds, throttle use, coasting, lock-ups/offs, input device, with plain-English labels.",
     {"track": {"type": "string"}}),
    ("evaluate_strategy", "Simulate race time for a pit plan vs the best 0/1/2-stop alternatives (live race data fills "
     "the defaults: laps, compound, tyre age, wear rate, lap time, pit loss).",
     {"stops": {"type": "array", "description": "planned pit stops", "items": {"type": "object", "properties": {
         "lap": {"type": "integer"}, "compound": {"type": "string", "enum": ["soft", "medium", "hard", "intermediate", "wet"]}}}},
      "total_laps": {"type": "integer"}, "current_lap": {"type": "integer"},
      "start_compound": {"type": "string"}, "tyre_age": {"type": "integer"}, "base_lap_s": {"type": "number"},
      "pit_loss_s": {"type": "number"}}),
    ("remember", "Save a goal or fact about the driver for future sessions (not preferences: use set_preference).",
     {"note": {"type": "string"}}),
    ("set_preference", "The driver states how they like to drive or be coached ('coasting is fine on keyboard', "
     "'don't nag me about kerbs', 'talk less'). One per topic: a new one replaces the old (history kept).",
     {"topic": {"type": "string", "enum": ["coasting", "braking", "lockups", "track_limits", "throttle", "steering",
                                           "trail_braking", "kerbs", "setup", "tyres", "fuel", "talking", "other"]},
      "preference": {"type": "string", "description": "the preference in the driver's own terms"}}),
    ("forget", "Remove a preference (by topic) or notes containing some text, when the driver asks.",
     {"what": {"type": "string"}}),
    ("get_race_history", "This race lap by lap: position, gaps ahead/behind, tyre compound, wear and temps, fuel, ERS, "
     "safety car, weather. Use it to see trends (tyre deg, gap closing, position changes).", {}),
    ("get_stored_lap", "A stored lap's corner report from the database (works away from the game).",
     {"lap": {"type": "integer", "description": "lap number; omit for the latest"}}),
]


def openai_tools(names=None) -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": d,
             "parameters": {"type": "object", "properties": p, "required": []}}}
            for n, d, p in TOOL_SCHEMAS if names is None or n in names]


_SENT = re.compile(r"(.+?[.!?])\s+", re.S)


def sentences(chunks: Iterator[str]) -> Iterator[str]:
    """Re-chunk a token stream into speakable sentences (first audio as early as possible)."""
    buf = ""
    for ch in chunks:
        buf += ch
        while True:
            m = _SENT.match(buf)
            if not m or (len(m.group(1)) < 12 and len(buf) < 60):
                break
            yield m.group(1).strip()
            buf = buf[m.end():]
    if buf.strip():
        yield buf.strip()


DEFAULT_MODEL = "gpt-5.4-mini"
# Radio needs speed over deep thought: ask for the lowest reasoning effort the model accepts.
EFFORT_LADDER = ["none", "minimal", "low"]


class OpenAIProvider:
    def __init__(self, model: str | None = None):
        from openai import OpenAI
        self.client = OpenAI(timeout=25.0, max_retries=1)   # never hang the radio
        self.model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        effort = os.getenv("OPENAI_REASONING", "none")
        self._efforts = EFFORT_LADDER[EFFORT_LADDER.index(effort):] if effort in EFFORT_LADDER else [effort]
        self._efforts += [e for e in ("low", "medium") if e not in self._efforts]
        # learned once per process: params this model rejects are dropped/adjusted, then reused
        self.kw: dict = {"temperature": 0.6, "max_completion_tokens": 600, "reasoning_effort": self._efforts[0]}

    def _create(self, messages, tools, effort=None, max_tokens=None):
        for _ in range(6):
            kw = dict(self.kw)
            if max_tokens:
                kw["max_completion_tokens"] = max_tokens
            if effort and "reasoning_effort" in kw and effort in self._efforts:
                kw["reasoning_effort"] = effort   # deeper thinking when there's time (garage / review)
            try:
                return self.client.chat.completions.create(model=self.model, messages=messages, tools=tools,
                                                           stream=True, **kw)
            except Exception as e:
                msg = str(e)
                if "temperature" in msg and "temperature" in self.kw:
                    self.kw.pop("temperature")
                elif "reasoning" in msg and effort and kw.get("reasoning_effort") == effort != self.kw.get("reasoning_effort"):
                    effort = None   # the requested deeper effort was rejected: use the learned default
                elif "reasoning" in msg and "reasoning_effort" in self.kw:
                    cur = self.kw["reasoning_effort"]
                    nxt = self._efforts[self._efforts.index(cur) + 1:] if cur in self._efforts else []
                    if f"'{cur}'" in msg and nxt:  # this effort value rejected -> next-lowest
                        self.kw["reasoning_effort"] = nxt[0]
                    else:  # parameter itself unsupported (non-reasoning model, e.g. gpt-4.1)
                        self.kw.pop("reasoning_effort")
                else:
                    raise
        raise RuntimeError(f"{self.model}: could not find accepted parameters ({self.kw})")

    def stream(self, messages: list[dict], tools: list[dict], effort=None, max_tokens=None) -> Iterator[dict]:
        resp = self._create(messages, tools, effort, max_tokens)
        calls: dict[int, dict] = {}
        for ev in resp:
            if not ev.choices:
                continue
            d = ev.choices[0].delta
            if d.content:
                yield {"type": "text", "text": d.content}
            for tc in d.tool_calls or []:
                c = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                c["id"] = tc.id or c["id"]
                if tc.function and tc.function.name:
                    c["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    c["args"] += tc.function.arguments
        for c in calls.values():
            yield {"type": "tool_call", **c}


class MockProvider:
    """Offline stand-in: picks tools by keyword, then summarises tool output with templates."""

    ROUTES = [
        (("remember",), "remember"),
        (("improv", "progress", "getting better", "getting faster"), "get_progress"),
        (("style", "kind of driver", "type of driver"), "get_driving_style"),
        (("strategy", "one stop", "one-stop", "two stop", "pit on lap", "undercut"), "evaluate_strategy"),
        (("temp", "pressure", "damage", "wear"), "get_car_status"),
        (("racing", "who", "ahead", "behind", "rival", "position", "p1", "place"), "get_rivals"),
        (("tyre", "tire", "gap", "fuel", "ers", "strategy", "box", "pit", "position", "rain", "weather"), "get_race_status"),
        (("setup", "wing", "diff", "brake bias", "understeer", "oversteer", "balance"), "get_setup_and_advice"),
        (("consistent", "consistency"), "get_consistency"),
        (("corner", "where", "losing", "lose", "lap", "faster", "slow", "wrong", "mistake", "improve", "how"), "get_last_lap_report"),
    ]

    def stream(self, messages: list[dict], tools: list[dict], **_) -> Iterator[dict]:
        last = messages[-1]
        if last["role"] == "tool":
            data = json.loads(last["content"])
            yield from ({"type": "text", "text": w + " "} for w in self._summarise(data).split(" "))
            return
        q = last["content"].lower()
        from f1engineer.analysis.coach import TRACK_NAMES
        corners = {n.lower().replace("the ", "") for t in TRACK_NAMES.values() if isinstance(t, dict) for n in t.values()}
        hit = next((c for c in sorted(corners, key=len, reverse=True) if c.split("/")[0] in q), None)
        if hit:
            yield {"type": "tool_call", "id": "mock1", "name": "get_corner_history", "args": json.dumps({"corner": hit.split("/")[0]})}
            return
        available = {t["function"]["name"] for t in tools}
        for keys, tool in self.ROUTES:
            if any(k in q for k in keys) and tool in available:
                args = json.dumps({"note": last["content"]}) if tool == "remember" else "{}"
                yield {"type": "tool_call", "id": "mock1", "name": tool, "args": args}
                return
        yield {"type": "text", "text": "Copy. Ask me about your lap, corners, setup or strategy."}

    @staticmethod
    def _summarise(d: dict) -> str:
        if "error" in d:
            return f"No data yet: {d['error']}."
        if "saved" in d:
            return "Copy, noted. I'll keep that in mind."
        if "sessions" in d:
            s = d["sessions"]
            t = d.get("trend")
            if t:
                word = "improving" if t["representative_change_s"] < 0 else "not improving yet"
                return (f"You're {word}: representative pace {t['representative_change_s']:+.2f} seconds and best "
                        f"{t['best_change_s']:+.2f} since your first session here.")
            return f"One session so far, best {s[-1]['best']}. Give me another session to measure a trend."
        if "style_summary" in d:
            return "Your style: " + "; ".join(d["style_summary"][:3]) + "." if d["style_summary"] else "Not enough laps to read your style yet."
        if "best_alternatives" in d:
            b = d["best_alternatives"][0] if d["best_alternatives"] else None
            if "your_plan_vs_best_s" in d:
                return f"Your plan is {d['your_plan_vs_best_s']:.1f} seconds off the best option I can find."
            if b:
                stops = ", ".join(f"lap {s['lap']} {s['compound']}" for s in b["stops"]) or "no stop"
                return f"Best option: pit {stops}."
            return "Not enough data for a strategy."
        if "losses" in d:
            if not d["losses"]:
                return f"Lap {d['lap_time']}, nothing major lost. Clean."
            c = d["losses"][0]
            return (f"Biggest loss is {c['corner']}, {c['time_loss']:.2f}. "
                    f"{(c['causes'] or ['slower there'])[0].capitalize()}.")
        if "laps" in d and "corner" in d:
            last = d["laps"][-1]
            cause = (last["causes"] or ["nothing obvious"])[0]
            return f"{d['corner'].title()}, last lap you lost {last['time_loss']:.2f}: {cause}."
        if "advice" in d:
            a = d["advice"][:1]
            return f"Try {a[0]['change']}. {a[0]['why'].capitalize()}." if a else "Setup looks fine, it's the driver."
        if "calls" in d:
            if d["calls"]:
                return d["calls"][0].capitalize() + "."
            return f"P{d.get('position', '?')}, nothing urgent."
        if "tyres" in d and isinstance(d["tyres"], dict) and "surface_temp_c" in d["tyres"]:
            t = d["tyres"]
            temps = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in t["surface_temp_c"].items())
            return f"{t['compound']} tyres, {t['age_laps']} laps old. Surface temps {temps}."
        if "cars" in d and "my_position" in d:
            near = [c for c in d["cars"] if not c["me"]]
            ahead = [c for c in near if c["position"] < d["my_position"]]
            behind = [c for c in near if c["position"] > d["my_position"]]
            out = f"You're P{d['my_position']}."
            if ahead:
                c = ahead[-1]
                out += f" Ahead is {c['name']}" + (f", {abs(c['gap_to_me_s']):.1f} up the road" if c["gap_to_me_s"] is not None else "") + "."
            if behind:
                c = behind[0]
                out += f" Behind is {c['name']}" + (f", {c['gap_to_me_s']:.1f} back" if c["gap_to_me_s"] is not None else "") + "."
            return out
        if "corners" in d and isinstance(d["corners"], list) and d["corners"] and "spread" in d["corners"][0]:
            return f"Least consistent is {d['corners'][0]['corner']}. Nail that first."
        return "Copy."


class Agent:
    def __init__(self, tools: dict[str, Callable[..., dict]], context_fn: Callable[[], str], sass: int = 2,
                 provider=None, profile_fn: Callable[[], str] = lambda: "nothing yet"):
        self.tools = tools
        self.context_fn = context_fn
        self.profile_fn = profile_fn
        self.sass = sass
        self.name = os.getenv("F1E_ENGINEER_NAME") or os.getenv("F1E_WAKE_NAME") or "Bono"
        self.provider = provider or make_provider()
        self.history: list[dict] = []

    def ask(self, question: str, max_rounds: int = 4, mode: str = "radio", remember: bool = True,
            history: list | None = None) -> Iterator[str]:
        """Yield speakable sentences as soon as they are ready. mode: radio | garage | review."""
        t0 = time.perf_counter()
        m = MODES.get(mode, MODES["radio"])
        sys = {"role": "system", "content": SYSTEM_PROMPT.format(name=self.name, sass=self.sass, mode=mode, profile=self.profile_fn(),
                                                                 context=self.context_fn())}
        msgs = [sys, *(self.history if history is None else history)[-12:], {"role": "user", "content": question}]
        tools = openai_tools(set(self.tools))
        answer = ""
        for _ in range(max_rounds):
            calls = []

            def text_stream():
                nonlocal answer
                try:  # the request fails on the first event (bad model name, no credit, network down)
                    events = iter(self.provider.stream(msgs, tools, effort=m["effort"], max_tokens=m["max_tokens"]))
                    first = next(events, None)
                except Exception as e:
                    print(f"[llm] {type(self.provider).__name__} failed: {str(e)[:300]}")
                    if isinstance(self.provider, MockProvider):
                        raise
                    events = iter(MockProvider().stream(msgs, tools))  # this answer comes from the offline brain
                    first = next(events, None)
                for ev in _chain(first, events):
                    if ev["type"] == "text":
                        answer += ev["text"]
                        yield ev["text"]
                    else:
                        calls.append(ev)

            for s in sentences(text_stream()):
                if is_offer(s):
                    continue   # drop "Do you want me to..." style endings
                yield s
            if not calls:
                break
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for c in calls]})
            for c in calls:
                fn = self.tools.get(c["name"])
                try:
                    res = fn(**json.loads(c["args"] or "{}")) if fn else {"error": f"unknown tool {c['name']}"}
                except Exception as e:
                    res = {"error": repr(e)}
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(res, default=str)[:6000]})
        if remember:
            self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
            del self.history[:-24]
        self.last_latency = time.perf_counter() - t0


def _chain(first, rest):
    if first is not None:
        yield first
    yield from rest


def make_provider():
    if os.getenv("OPENAI_API_KEY") and os.getenv("F1E_LLM", "openai") != "mock":
        try:
            return OpenAIProvider()
        except Exception as e:
            print(f"[llm] OpenAI unavailable ({e!r}); using mock")
    return MockProvider()
