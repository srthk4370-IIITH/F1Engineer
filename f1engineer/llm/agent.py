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

SYSTEM_PROMPT = """You are the user's F1 race engineer on team radio while they play an F1 video game.
Personality: calm, concise, numbers-first professional with dry British-pitwall sass (sass level {sass}/3; 0 = none).
At sass >= 2 you may throw in cheeky banter like "be a man, brake later" or "my nan carries more speed than that",
but never during safety-critical or strategy-critical calls, and never insult the driver beyond playful teasing.
You are also a teacher: when asked "why" or "how", explain the driving technique or setup physics simply.

Rules:
- Use the tools for every number. Never invent lap times, gaps, corners or setup values.
- Radio replies: max 2 short sentences unless the driver asks to explain or for a debrief.
- Speak naturally for text-to-speech: no markdown, no lists, no emojis; say "point three" style numbers briefly.
- If data is missing, say so in a few words.
- The context below is a live snapshot (tyres, position, delta, nearby cars, last lap losses). If it already answers
  the question, answer straight away without calling a tool; use tools for detail.
- Sound like a real race engineer: calm, direct ("Copy.", "Understood.", "Box, box."), number first, then the instruction.
Current context: {context}"""

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
]


def openai_tools() -> list[dict]:
    return [{"type": "function", "function": {"name": n, "description": d,
             "parameters": {"type": "object", "properties": p, "required": []}}} for n, d, p in TOOL_SCHEMAS]


_SENT = re.compile(r"(.+?[.!?])(\s+|$)", re.S)


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
        self.client = OpenAI()
        self.model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        effort = os.getenv("OPENAI_REASONING", "none")
        self._efforts = EFFORT_LADDER[EFFORT_LADDER.index(effort):] if effort in EFFORT_LADDER else [effort]
        # learned once per process: params this model rejects are dropped/adjusted, then reused
        self.kw: dict = {"temperature": 0.6, "max_completion_tokens": 600, "reasoning_effort": self._efforts[0]}

    def _create(self, messages, tools):
        for _ in range(6):
            try:
                return self.client.chat.completions.create(model=self.model, messages=messages, tools=tools,
                                                           stream=True, **self.kw)
            except Exception as e:
                msg = str(e)
                if "temperature" in msg and "temperature" in self.kw:
                    self.kw.pop("temperature")
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

    def stream(self, messages: list[dict], tools: list[dict]) -> Iterator[dict]:
        resp = self._create(messages, tools)
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
        (("temp", "pressure", "damage", "wear"), "get_car_status"),
        (("racing", "who", "ahead", "behind", "rival", "position", "p1", "place"), "get_rivals"),
        (("tyre", "tire", "gap", "fuel", "ers", "strategy", "box", "pit", "position", "rain", "weather"), "get_race_status"),
        (("setup", "wing", "diff", "brake bias", "understeer", "oversteer", "balance"), "get_setup_and_advice"),
        (("consistent", "consistency"), "get_consistency"),
        (("corner", "where", "losing", "lose", "lap", "faster", "slow", "wrong", "mistake", "improve", "how"), "get_last_lap_report"),
    ]

    def stream(self, messages: list[dict], tools: list[dict]) -> Iterator[dict]:
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
        for keys, tool in self.ROUTES:
            if any(k in q for k in keys):
                yield {"type": "tool_call", "id": "mock1", "name": tool, "args": "{}"}
                return
        yield {"type": "text", "text": "Copy. Ask me about your lap, corners, setup or strategy."}

    @staticmethod
    def _summarise(d: dict) -> str:
        if "error" in d:
            return f"No data yet: {d['error']}."
        if "losses" in d:
            if not d["losses"]:
                return f"Lap {d['lap_time']}, nothing major lost. Clean."
            c = d["losses"][0]
            return (f"Biggest loss is {c['corner']}, {c['time_loss']:.2f}. "
                    f"{(c['causes'] or ['slower there'])[0].capitalize()}. Be a man, brake later.")
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
                 provider=None):
        self.tools = tools
        self.context_fn = context_fn
        self.sass = sass
        self.provider = provider or make_provider()
        self.history: list[dict] = []

    def ask(self, question: str, max_rounds: int = 3) -> Iterator[str]:
        """Yield speakable sentences as soon as they are ready."""
        t0 = time.perf_counter()
        sys = {"role": "system", "content": SYSTEM_PROMPT.format(sass=self.sass, context=self.context_fn())}
        msgs = [sys, *self.history[-8:], {"role": "user", "content": question}]
        answer = ""
        for _ in range(max_rounds):
            calls = []

            def text_stream():
                nonlocal answer
                try:  # the request fails on the first event (bad model name, no credit, network down)
                    events = iter(self.provider.stream(msgs, openai_tools()))
                    first = next(events, None)
                except Exception as e:
                    print(f"[llm] {type(self.provider).__name__} failed: {str(e)[:300]}")
                    if isinstance(self.provider, MockProvider):
                        raise
                    events = iter(MockProvider().stream(msgs, openai_tools()))  # this answer comes from the offline brain
                    first = next(events, None)
                for ev in _chain(first, events):
                    if ev["type"] == "text":
                        answer += ev["text"]
                        yield ev["text"]
                    else:
                        calls.append(ev)

            for s in sentences(text_stream()):
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
        self.history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
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
