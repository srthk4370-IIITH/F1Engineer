"""Hands-free "Hey Bono": a wake word that works whatever the game does with the keyboard.

No extra model: an energy-based voice detector cuts speech bursts out of the mic stream, and only those short bursts
go through the same Whisper model used for push-to-talk. A burst that starts with the engineer's name (fuzzy-matched
against how Whisper heard *you* say it during enrolment) is a call:
  "Hey Bono, what are my tyre temps?"  -> the rest is the question
  "Bono"                               -> "Go ahead", then the next thing you say is the question
Enrol with `f1engineer wake` (say the name 4 times); data/wake.json stores the variants and your voice/noise levels.
"""
from __future__ import annotations

import difflib
import json
import re
import time
from pathlib import Path

import numpy as np

_WORD = re.compile(r"[a-z']+")


def load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def variants(name: str, enrolled: list[str] | None = None) -> set[str]:
    """Spellings the recogniser may produce for the name: built-ins for Bono, the name itself for custom names,
    plus whatever it actually heard during enrolment."""
    base = name.lower()
    out = {base, "bono", "bonno", "bonnow", "bonner", "bono's", "bonus", "bueno", "bonnie", "bonow"} if base == "bono" else \
        {base, base + "'s", base.replace("ie", "y"), base.replace("y", "ie")}
    for e in enrolled or []:
        w = _WORD.findall(e.lower())
        out |= {x for x in w if x not in ("hey", "hi", "hello", "ok", "okay", "a")}
    return out


PHANTOMS = {"you", "thank you", "thanks", "thank you very much", "thanks for watching", "bye", "okay", "ok", "copy",
            "yeah", "so", "the", "uh", "um", "hmm", "oh", "i", "a", "and", "music", "applause", "silence"}


def is_phantom(text: str) -> bool:
    """Words Whisper hallucinates from noise or silence (it loves 'Thank you.' and 'you')."""
    t = " ".join(_WORD.findall(text.lower()))
    return not t or t in PHANTOMS or (len(t.split()) == 1 and len(t) <= 3)


def split_wake(text: str, names: set[str]) -> tuple[bool, str]:
    """(is a call, remaining question). The name must be among the first three words."""
    words = _WORD.findall(text.lower())
    raw = text.split()
    for i, w in enumerate(words[:3]):
        if w in names or difflib.get_close_matches(w, names, n=1, cutoff=0.78):
            rest = " ".join(raw[i + 1:]).lstrip(",.!? ").strip()
            return True, rest
    return False, ""


def safe_threshold(wk: dict | None) -> float:
    """Trigger level that can never sit above the driver's own voice (old enrolments could: game sound on speakers)."""
    if not wk:
        return -38.0
    th, voice = wk.get("threshold_db", -38.0), wk.get("voice_db")
    return min(th, voice - 4) if voice is not None else th


class VoiceGate:
    """Cuts speech out of 0.1 s mic blocks with hysteresis: starts above `threshold`, keeps going until the level has
    stayed `drop` dB lower for `hang` s (so quieter syllables mid-sentence don't end it)."""

    def __init__(self, threshold_db: float, hang: float = 0.6, max_len: float = 9.0, drop: float = 8.0):
        self.th, self.hang, self.max_len, self.drop = threshold_db, hang, max_len, drop
        self.buf: list[np.ndarray] = []
        self.pre: list[np.ndarray] = []
        self.quiet = 0.0
        self.active = False

    def reset(self) -> None:
        self.active, self.buf, self.pre, self.quiet = False, [], [], 0.0

    def arm(self) -> None:
        """Start a segment now, without waiting for the level (used right after "Go ahead")."""
        self.active, self.buf, self.quiet = True, list(self.pre), 0.0

    def feed(self, block: np.ndarray) -> np.ndarray | None:
        level = 20 * np.log10(float(np.sqrt(np.mean(block ** 2))) + 1e-9)
        dur = len(block) / 16000
        if not self.active:
            self.pre = (self.pre + [block])[-3:]          # 0.3 s of lead-in so the first syllable survives
            if level > self.th:
                self.active, self.buf, self.quiet = True, list(self.pre), 0.0
            return None
        self.buf.append(block)
        self.quiet = self.quiet + dur if level <= self.th - self.drop else 0.0
        length = sum(len(b) for b in self.buf) / 16000
        if self.quiet >= self.hang or length >= self.max_len:
            self.active = False
            seg = np.concatenate(self.buf)
            self.buf = []
            return seg if length - self.quiet >= 0.3 else None   # ignore clicks
        return None


def enrol(cfg, name: str = "Bono", takes: int = 4) -> dict:
    """Interactive: measure the room, then record the name `takes` times."""
    import sounddevice as sd
    from f1engineer.speech import STT, pick_input
    dev = pick_input(cfg.mic)
    stt = STT(cfg)

    def rec(sec):
        a = sd.rec(int(sec * 16000), samplerate=16000, channels=1, dtype="float32", device=dev)
        sd.wait()
        return a[:, 0]

    def db(a):
        return 20 * np.log10(float(np.sqrt(np.mean(a ** 2))) + 1e-9)

    input("\nStay quiet for 2 seconds so I can hear the room (game sound on if you play on speakers). Press Enter...")
    noise = db(rec(2.0))
    heard, voice = [], []
    for i in range(takes):
        input(f"  Take {i + 1}/{takes}: press Enter, then say 'Hey {name}' ...")
        a = rec(2.5)
        frames = a[: len(a) // 1600 * 1600].reshape(-1, 1600)
        lv = [db(f) for f in frames]
        voice.append(float(np.percentile(lv, 90)))
        t = stt.transcribe(a)
        heard.append(t)
        print(f"    heard: {t!r}")
    v = float(np.median(voice))
    th = noise + 0.55 * (v - noise) if v > noise + 6 else min(noise + 3, v - 4)   # never above the voice
    data = {"name": name, "heard": heard, "voice_db": round(v, 1), "noise_db": round(noise, 1), "threshold_db": round(th, 1),
            "date": time.strftime("%Y-%m-%d")}
    out = cfg.data_dir / "wake.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1), encoding="utf-8")
    ok = sum(split_wake(h, variants(name, heard))[0] for h in heard)
    print(f"\n  Voice {v:.0f} dBFS, room {noise:.0f} dBFS -> trigger level {th:.0f} dBFS. Recognised {ok}/{takes} takes.")
    if v < noise + 6:
        print("  Your voice is barely louder than the room (the mic hears the game). For clean recognition use a headset "
              "mic (set it in setup or F1E_MIC=...), or headphones instead of speakers.")
    print(f"  Saved {out}. In the game, say 'Hey {name}' (or just '{name}') followed by your question.")
    return data
