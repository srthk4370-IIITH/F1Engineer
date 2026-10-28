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
        self.started = False         # first sentence handed to the radio
        self.interleaved = False     # a proactive call went out while this one was being worked on
        self.after_other = False     # asked while another answer was still pending
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
        self.speaking = False        # an answer is being voiced: routine calls wait
        self.open: list[Question] = []   # asked, not yet started
        threading.Thread(target=self._speaker, name="ask-speaker", daemon=True).start()

    def submit(self, text: str, mode: str | None = None) -> Question:
        key = " ".join(text.lower().split())
        if quick.instant(text):   # commands and plain facts: straight out, never queued behind slow AI answers
            ans = quick.answer(self.eng, text)
            if ans:
                with self.lock:
                    self.seq += 1
                    q = Question(self.seq, text, mode or "radio")
                    self.eng.agent.history += [{"role": "user", "content": text}, {"role": "assistant", "content": ans}]
                q.source, q.answer = "instant", ans
                self.eng._say(ans, 1, "answer", f"ans{q.seq}")
                print(f"[ask] '{text}' -> instant: {ans[:100]}")
                q.done.set()
                return q
        with self.lock:
            old = self.recent.get(key)
            if old and time.time() - old.asked < 5:
                return old           # the same question twice (double press / echo): answer once
            self.seq += 1
            if mode is None and DEEP.search(text.lower()):
                mode = "analysis"      # "debrief me", "break it down": the deep, tool-heavy version on demand
            q = Question(self.seq, text, mode or self.eng.mode())
            q.after_other = bool(self.open)
            self.open.append(q)
            self.recent = {k: v for k, v in self.recent.items() if time.time() - v.asked < 30}
            self.recent[key] = q
        if not quick.instant(text):   # going to the AI: say we heard it, straight away
            q.acked = True
            self.eng.speech.say(quick.ack_for(text), 1, "ack", f"ack{q.seq}")
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
                            self.eng.speech.say(random.choice(["Checking.", "Stand by.", "One sec."]), 1, "ack", f"ack{q.seq}")
                            continue
                        if not out:
                            self.eng._say("Radio's patchy, ask me again.", 1, "answer", f"ans{q.seq}")
                        break
                    if s is _DONE:
                        break
                    if not out and _is_filler(s):
                        continue        # a bare "Copy." / "Okay." opener: the acknowledgement already did that
                    if not out:
                        with self.lock:
                            q.started = True
                            if q in self.open:
                                self.open.remove(q)
                        self.speaking = True
                        if q.interleaved or q.after_other or time.time() - q.asked > 12:
                            s = quick.lead_in(q.text) + s[:1].lower() + s[1:]   # "On the setup: ..."
                        print(f"[ask] '{q.text}' -> first sentence in {time.time() - q.asked:.2f}s "
                              f"({q.source if q.source == 'instant' else type(self.eng.agent.provider).__name__}, {q.mode})")
                    out.append(s)
                    self.eng._say(s, 1, "answer", f"ans{q.seq}")
            finally:
                q.answer = " ".join(out)
                with self.lock:   # history in asking order, whatever order the answers finished in
                    self.eng.agent.history += [{"role": "user", "content": q.text},
                                               {"role": "assistant", "content": q.answer}]
                    del self.eng.agent.history[:-24]
                print(f"[ask] answered in {time.time() - t0:.2f}s: {q.answer[:120]}")
                with self.lock:
                    if q in self.open:
                        self.open.remove(q)
                q.done.set()
                # keep the floor a moment after the last word so routine calls don't step on it
                threading.Timer(1.5, self._release).start()

    def _release(self) -> None:
        self.busy = not self.pending.empty()
        self.speaking = self.busy and any(q.started for q in self.open)

    def waiting_long(self) -> bool:
        """A question has been waiting 2.5 s+ for its first sentence: a proactive call may go out, framed as 'meanwhile'."""
        with self.lock:
            return any(not q.started and time.time() - q.asked > 2.5 for q in self.open)

    def waiting_short(self) -> bool:
        with self.lock:
            return any(not q.started and time.time() - q.asked <= 2.5 for q in self.open)

    def note_interleaved(self) -> None:
        with self.lock:
            for q in self.open:
                q.interleaved = True


_FILLER = {"copy", "okay", "ok", "understood", "right", "roger", "sure", "yes", "yeah", "got it"}


def _is_filler(s: str) -> bool:
    return " ".join(re.findall(r"[a-z']+", s.lower())) in _FILLER
