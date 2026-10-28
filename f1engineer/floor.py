"""Who has the radio: one ordered queue for everything the engineer says, and the driver always wins the floor.

Rules
  - Nothing starts while the driver is talking (button held, or answering after "Go ahead").
  - The driver can cut in at any time: playback stops mid-sentence and that sentence goes back to the front of the
    queue, so it's resumed afterwards ("As I was saying, ...") - nothing is thrown away. Acknowledgements are the
    only thing dropped (they'd be stale).
  - Order: urgent calls, then acknowledgements, then answers, then routine calls, then long analysis; within one
    message, its sentences stay in order. Items expire only after a generous per-kind time-to-live.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

PRIORITY = {"urgent": 0, "ack": 1, "answer": 2, "proactive": 3, "analysis": 4}
TTL = {"urgent": 30, "ack": 6, "answer": 180, "proactive": 90, "analysis": 600}
_SENT = re.compile(r"(?<=[.!?])\s+")


@dataclass(order=True)
class Item:
    prio: int
    seq: int
    text: str = field(compare=False)
    kind: str = field(compare=False, default="proactive")
    key: str = field(compare=False, default="")
    created: float = field(compare=False, default_factory=time.time)
    ttl: float = field(compare=False, default=90)
    resumed: bool = field(compare=False, default=False)


class Floor:
    def __init__(self):
        self.items: list[Item] = []
        self.cv = threading.Condition()
        self.seq = 0
        self.driver = False           # driver has the floor
        self.driver_until = 0.0       # small grace after the driver finishes
        self.current: Item | None = None
        self.interrupted = False

    def put(self, text: str, kind: str = "proactive", key: str = "", ttl: float | None = None, prio: int | None = None):
        parts = [p for p in _SENT.split(text.strip()) if p] if kind in ("analysis", "proactive", "answer") else [text]
        with self.cv:
            for p in parts:
                self.seq += 1
                self.items.append(Item(PRIORITY.get(kind, 3) if prio is None else prio, self.seq, p, kind, key,
                                       ttl=ttl or TTL.get(kind, 90)))
            self.items.sort()
            self.cv.notify_all()

    def set_driver(self, on: bool) -> Item | None:
        """Driver starts/stops talking. Returns the item that was cut off (caller stops the audio)."""
        with self.cv:
            self.driver = on
            if not on:
                self.driver_until = time.time() + 0.4
                self.cv.notify_all()
                return None
            cut = self.current
            if cut is not None:
                self.interrupted = True
                if cut.kind != "ack":   # resume it afterwards, from the start of the sentence
                    cut.resumed = True
                    cut.prio = min(cut.prio, PRIORITY["answer"]) if cut.kind == "answer" else cut.prio
                    self.items.append(cut)
                    self.items.sort()
            return cut

    def next(self) -> Item | None:
        """Block until something may be said now; returns it (or None on shutdown)."""
        with self.cv:
            while True:
                now = time.time()
                self.items = [i for i in self.items if now - i.created <= i.ttl]
                if not self.driver and now >= self.driver_until and self.items:
                    it = self.items.pop(0)
                    self.current, self.interrupted = it, False
                    return it
                self.cv.wait(timeout=0.2 if self.items or now < self.driver_until else 1.0)

    def done(self, it: Item) -> bool:
        """Playback of `it` ended; True if it finished normally (not cut off by the driver)."""
        with self.cv:
            ok = not (self.current is it and self.interrupted)
            self.current = None
            self.interrupted = False
            return ok

    def clear(self, keep_urgent: bool = True) -> None:
        with self.cv:
            self.items = [i for i in self.items if keep_urgent and i.kind == "urgent"]

    def pending(self, kind: str | None = None) -> int:
        with self.cv:
            return sum(1 for i in self.items if kind is None or i.kind == kind)
