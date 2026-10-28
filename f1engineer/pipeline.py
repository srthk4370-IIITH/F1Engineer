"""Driver question pipeline: answers are generated in parallel, spoken strictly in the order asked.

  - submit() returns at once; a worker pool runs the model, a single speaker thread delivers answers in order
  - identical questions within a few seconds are merged; questions that waited too long are dropped politely
  - instant acknowledgement if the first sentence is slow; hard timeout so the radio never goes silent
  - while an answer is being spoken, routine radio waits (urgent calls still get through)
  - every question is logged with its latency and the provider that answered it
"""
from __future__ import annotations

import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from f1engineer import quick

import re

DEEP = re.compile(r"debrief|break (it|that|this) down|in detail|analy[sz]e|full (analysis|picture)|deep dive|"
                  r"go through|talk me through|explain properly")
MAX_SENTENCES = {"radio": 3, "chat": 5, "garage": 5, "review": 8, "analysis": 12}
ACK_AFTER_S = 1.2
FIRST_SENTENCE_TIMEOUT_S = 25
STALE_S = 60
_DONE = object()


class Question:
    def __init__(self, seq: int, text: str, mode: str):
        self.seq, self.text, self.mode = seq, text, mode
        self.asked = time.time()
        self.q: queue.Queue = queue.Queue()
        self.answer = ""
        self.source = "ai"
        self.acked = False
        self.done = threading.Event()


class AskPipeline:
    def __init__(self, engine, workers: int = 3):
        self.eng = engine
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ask")
        self.pending: queue.Queue = queue.Queue()
        self.seq = 0
        self.lock = threading.Lock()
        self.recent: dict[str, Question] = {}
        self.busy = False
        threading.Thread(target=self._speaker, name="ask-speaker", daemon=True).start()

    def submit(self, text: str, mode: str | None = None) -> Question:
        key = " ".join(text.lower().split())
        with self.lock:
            old = self.recent.get(key)
            if old and time.time() - old.asked < 5:
                return old           # the same question twice (double press / echo): answer once
            self.seq += 1
            if mode is None and DEEP.search(text.lower()):
                mode = "analysis"      # "debrief me", "break it down": the deep, tool-heavy version on demand
            q = Question(self.seq, text, mode or self.eng.mode())
            self.recent = {k: v for k, v in self.recent.items() if time.time() - v.asked < 30}
            self.recent[key] = q
        if quick.answer(self.eng, text) is None:   # going to the AI: say we heard it, straight away
            q.acked = True
            self.eng.speech.say(quick.ack_for(text), 1)
        history = list(self.eng.agent.history)   # each answer sees the conversation as it was when asked
        self.pool.submit(self._generate, q, history)
        self.pending.put(q)
        return q

    def _generate(self, q: Question, history: list) -> None:
        try:
            fast = quick.answer(self.eng, q.text)   # plain data question: read it off the screen, no AI
            if fast:
                q.source = "instant"
                q.q.put(fast)
                return
            cap = MAX_SENTENCES.get(q.mode, 6)
            for n, s in enumerate(self.eng.agent.ask(q.text, mode=q.mode, remember=False, history=history)):
                if n >= cap:
                    break   # at speed nobody listens to a speech: conversation stays short whatever the model writes
                q.q.put(s)
        except Exception as e:
            print(f"[ask] failed: {e!r}")
            q.q.put("Sorry, lost you there. Say again?")
        finally:
            q.q.put(_DONE)

    def _speaker(self) -> None:
        while True:
            q = self.pending.get()
            if time.time() - q.asked > STALE_S:
                q.done.set()
                continue
            self.busy = True
            out, acked = [], q.acked
            t0 = time.time()
            try:
                while True:
                    wait = ACK_AFTER_S if not out and not acked else FIRST_SENTENCE_TIMEOUT_S
                    try:
                        s = q.q.get(timeout=wait)
                    except queue.Empty:
                        if not out and not acked:
                            acked = True
                            self.eng.speech.say(random.choice(["Checking.", "Stand by.", "One sec."]), 1)
                            continue
                        if not out:
                            self.eng._say("Radio's patchy, ask me again.", 1)
                        break
                    if s is _DONE:
                        break
                    if not out:
                        print(f"[ask] '{q.text}' -> first sentence in {time.time() - q.asked:.2f}s "
                              f"({q.source if q.source == 'instant' else type(self.eng.agent.provider).__name__}, {q.mode})")
                    out.append(s)
                    self.eng._say(s, 1)
            finally:
                q.answer = " ".join(out)
                with self.lock:   # history in asking order, whatever order the answers finished in
                    self.eng.agent.history += [{"role": "user", "content": q.text},
                                               {"role": "assistant", "content": q.answer}]
                    del self.eng.agent.history[:-24]
                print(f"[ask] answered in {time.time() - t0:.2f}s: {q.answer[:120]}")
                q.done.set()
                # keep the floor a moment after the last word so routine calls don't step on it
                threading.Timer(1.5, lambda: setattr(self, "busy", not self.pending.empty())).start()
