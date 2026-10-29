"""Speech worker (separate process so ML never stalls telemetry ingest).

Core -> worker:  ("say", text, priority) | ("ptt", bool) | ("quit",)
Worker -> core:  ("transcript", text, stt_ms) | ("log", text)

Push-to-talk sources (OR-ed): keyboard hotkey (pynput), XInput controller button (ctypes, no deps),
and the game's own UDP button state forwarded by the core.
"""
from __future__ import annotations

import ctypes
import os
import sys
import re
import multiprocessing as mp
import queue
import random
import threading
from collections import deque
import time

import numpy as np

STT_PROMPT = ("F1 race engineer radio, driver talking to the engineer. Hey Bono. What setup changes should I make? "
              "What's my driving style? Am I improving? Tyre temps, tyre wear, gap ahead, gap behind, laps left, coasting, "
              "steering, braking, throttle, corners, traction, understeer, oversteer. Silverstone, Monza, Spa, Monaco, Copse, Maggotts, Becketts, Stowe, Vale, Club, "
              "Luffield, the Loop. Verstappen, Hamilton, Bottas, Leclerc, Norris, Sainz, Perez, Ricciardo, Russell, Gasly, "
              "Ocon, Alonso, Vettel, Albon, Stroll, Piastri. Where am I losing time to Verstappen? Race update. Box box, undercut, overcut, DRS, ERS, softs, mediums, hards, inters, brake bias, "
              "front wing, rear wing, differential, understeer, oversteer, lock-up, tyre wear, delta, sector, lap time.")


def prep_audio(a: np.ndarray) -> np.ndarray:
    """Steady input level for the recogniser (quiet laptop mics). Measured: trimming silence or pre-emphasis made it
    WORSE (it clipped first words), so only the DC offset and the gain are touched: loud parts to ~-20 dBFS, max +20 dB."""
    a = np.asarray(a, dtype=np.float32)
    if len(a) < 1600:
        return a
    a = a - float(np.mean(a))
    frames = a[: len(a) // 320 * 320].reshape(-1, 320)
    level = float(np.percentile(np.sqrt(np.mean(frames ** 2, axis=1)), 90)) + 1e-9
    return np.clip(a * min(10.0, 0.1 / level), -1, 1).astype(np.float32)


def openai_transcriber():
    """Optional (F1E_STT=openai): OpenAI's transcription model - best in a noisy room, needs the API key, ~0.5-1 s."""
    import io
    import os
    import wave
    if not os.getenv("OPENAI_API_KEY"):
        return None
    from openai import OpenAI
    client = OpenAI(timeout=8.0, max_retries=0)
    model = os.getenv("F1E_STT_CLOUD_MODEL", "gpt-4o-mini-transcribe")

    def run(audio16k: np.ndarray) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(audio16k, -1, 1) * 32767).astype(np.int16).tobytes())
        buf.name = "radio.wav"
        buf.seek(0)
        r = client.audio.transcriptions.create(model=model, file=buf, language="en", prompt=STT_PROMPT)
        return (r.text or "").strip()
    return run


def _bandpass(sr: int, lo: float = 300, hi: float = 3400, taps: int = 101) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = (2 * hi / sr) * np.sinc(2 * hi / sr * n) - (2 * lo / sr) * np.sinc(2 * lo / sr * n)
    return (h * np.hamming(taps)).astype(np.float32)


FX = {"light": (180, 6500, 1.2), "full": (300, 3400, 2.5)}   # band (Hz) and saturation drive


def radio_fx(x: np.ndarray, sr: int, mode: str = "light") -> np.ndarray:
    """Team-radio colour. light (default): a hint of radio, still crisp; full: classic narrow, crunchy pit radio."""
    if mode == "off":
        return x.astype(np.float32) / 32768.0
    lo, hi, drive = FX.get(mode, FX["light"])
    y = np.convolve(x.astype(np.float32) / 32768.0, _bandpass(sr, lo, min(hi, sr / 2 - 500)), mode="same")
    y = np.tanh(y * drive) / np.tanh(drive) * 0.9
    click = (np.random.rand(int(sr * 0.03)).astype(np.float32) - 0.5) * 0.25
    return np.concatenate([click, y, click * 0.6])


_SPEAK_RULES = [
    (re.compile(r"\b(\d):(\d\d)\.(\d)(\d)\d?\b"), r"\1 \2 point \3 \4"),   # 1:29.098 -> "1 29 point 0 9"
    (re.compile(r"\bkm/h\b"), "k p h"),
    (re.compile(r"(\d+) ?m\b"), r"\1 metres"),
    (re.compile(r"(\d+)%"), r"\1 percent"),
    (re.compile(r"\s*->\s*"), " to "),
    (re.compile(r"\bP(\d+)\b"), r"P \1"),
    (re.compile(r"\bDRS\b"), "D R S"),
    (re.compile(r"\bERS\b"), "E R S"),
    (re.compile(r"\bSC\b"), "safety car"),
    (re.compile(r"\bVSC\b"), "virtual safety car"),
]


def speakable(text: str) -> str:
    """Radio text as it should be *said* (the dashboard keeps the written form)."""
    for pat, rep in _SPEAK_RULES:
        text = pat.sub(rep, text)
    return text


class TTS:
    def __init__(self, cfg, fx: bool = True):
        from piper import PiperVoice
        from f1engineer.models import ensure_piper
        onnx, js = ensure_piper(cfg)
        self.voice = PiperVoice.load(str(onnx), config_path=str(js))
        # ONNX Runtime's CPU arena grows with every new sentence length (+300 MB measured); without it RSS stays flat.
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.enable_cpu_mem_arena = False
        so.intra_op_num_threads = 2
        self.voice.session = ort.InferenceSession(str(onnx), sess_options=so, providers=["CPUExecutionProvider"])
        self.sr = self.voice.config.sample_rate
        self.fx = fx
        self.speed = cfg.tts_speed
        self.fx_mode = cfg.radio_fx

    def synth(self, text: str) -> np.ndarray:
        from piper.config import SynthesisConfig
        sc = SynthesisConfig(length_scale=1.0 / max(0.6, min(2.0, self.speed)), noise_scale=0.6, noise_w_scale=0.7)
        pcm = b"".join(ch.audio_int16_bytes for ch in self.voice.synthesize(speakable(text), syn_config=sc))
        x = np.frombuffer(pcm, dtype=np.int16)
        return radio_fx(x, self.sr, self.fx_mode) if self.fx else x.astype(np.float32) / 32768.0


def _register_cuda_dlls() -> None:
    """pip-installed CUDA libs (nvidia-cublas-cu12 / nvidia-cudnn-cu12) live in site-packages/nvidia/*/bin."""
    import site
    for sp in site.getsitepackages():
        root = os.path.join(sp, "nvidia")
        if os.path.isdir(root):
            for sub in os.listdir(root):
                b = os.path.join(root, sub, "bin")
                if os.path.isdir(b) and hasattr(os, "add_dll_directory"):   # Windows; Linux finds the .so via rpath
                    os.add_dll_directory(b)
                    os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


def _gpu_ok(cfg) -> bool:
    """Use the GPU only if CUDA works and enough VRAM is free (another app may own the GPU)."""
    if cfg.gpu == "off":
        return False
    try:
        _register_cuda_dlls()
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return False
        import pynvml
        pynvml.nvmlInit()
        free = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(0)).free / 1e9
        return free > 1.5
    except Exception:
        return False


class STT:
    """faster-whisper. model "auto" = base.en on a free GPU, tiny.en on CPU (~0.6 s per command)."""

    def __init__(self, cfg):
        import copy
        from faster_whisper import WhisperModel
        from f1engineer.models import ensure_whisper
        self.device = "cpu"
        self.model = None
        self.cloud = openai_transcriber() if cfg.stt == "openai" else None
        auto = cfg.stt_model == "auto"
        if _gpu_ok(cfg):
            c = copy.copy(cfg)
            c.stt_model = "small.en" if auto else cfg.stt_model
            try:  # CUDA needs cuBLAS/cuDNN DLLs; the warm-up proves they load
                self.model = WhisperModel(str(ensure_whisper(c)), device="cuda", compute_type="float16")
                self.transcribe(np.zeros(16000, np.float32))
                self.device, self.name = "cuda", c.stt_model
            except Exception:
                self.model = None
        if self.model is None:
            c = copy.copy(cfg)
            c.stt_model = "base.en" if auto else cfg.stt_model
            self.model = WhisperModel(str(ensure_whisper(c)), device="cpu", compute_type="int8", cpu_threads=4)
            self.name = c.stt_model
            self.transcribe(np.zeros(16000, np.float32))  # warm-up

    def to_cpu(self, cfg) -> None:
        """Release the GPU (another program needs VRAM) and continue on CPU."""
        import copy
        import gc
        from faster_whisper import WhisperModel
        from f1engineer.models import ensure_whisper
        c = copy.copy(cfg)
        c.stt_model = "base.en" if cfg.stt_model == "auto" else cfg.stt_model
        cpu = WhisperModel(str(ensure_whisper(c)), device="cpu", compute_type="int8", cpu_threads=4)
        self.model, old = cpu, self.model
        del old
        gc.collect()
        self.device, self.name = "cpu", c.stt_model

    def transcribe_safe(self, audio16k: np.ndarray, cfg, log, quick: bool = False) -> str:
        """transcribe(), but a GPU failure (e.g. the game grabbed the VRAM) falls back to the CPU and retries."""
        try:
            return self.transcribe(audio16k, quick)
        except Exception as e:
            if self.device != "cuda":
                raise
            log(f"GPU speech recognition failed ({e!r}): switching to CPU")
            self.to_cpu(cfg)
            return self.transcribe(audio16k, quick)

    def transcribe(self, audio16k: np.ndarray, quick: bool = False) -> str:
        """quick: a short clip that only has to contain the engineer's name - local greedy decoding, ~2x faster."""
        if self.cloud is not None and not quick:
            try:
                return self.cloud(prep_audio(audio16k))
            except Exception as e:
                print(f"[speech] cloud transcription failed ({e!r}): using the local model")
        beam = 1 if quick else 5 if self.device == "cuda" else 2
        segs, _ = self.model.transcribe(prep_audio(audio16k), language="en", beam_size=beam,
                                        initial_prompt=STT_PROMPT, vad_filter=False, condition_on_previous_text=False,
                                        without_timestamps=True, temperature=0.0)
        return " ".join(s.text.strip() for s in segs).strip()


def input_devices(kind: str = "input") -> list[str]:
    """Microphone (or speaker) names (MME host API: it resamples to our rate on any device), without duplicates."""
    import sounddevice as sd
    mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
    out = []
    for d in sd.query_devices():
        if d[f"max_{kind}_channels"] > 0 and (mme is None or d["hostapi"] == mme) and "Mapper" not in d["name"] \
                and d["name"] not in out:
            out.append(d["name"])
    return out


def pick_input(name: str):
    """Device index whose name contains `name` (case-insensitive), preferring MME; None = system default."""
    if not name:
        return None
    import sounddevice as sd
    mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
    hits = [(d["hostapi"] != mme, i) for i, d in enumerate(sd.query_devices())
            if d["max_input_channels"] > 0 and name.lower() in d["name"].lower()]
    return min(hits)[1] if hits else None


def pick_output(name: str):
    """Output device whose name contains `name` (e.g. 'Kraken'), preferring MME; None = system default."""
    if not name:
        return None
    import sounddevice as sd
    mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
    hits = [(d["hostapi"] != mme, i) for i, d in enumerate(sd.query_devices())
            if d["max_output_channels"] > 0 and name.lower() in d["name"].lower()]
    return min(hits)[1] if hits else None


class Speaker:
    """One output stream that stays open; every sound goes through it.

    sounddevice's play()/stop()/wait() open and close a PortAudio stream on every call and share global state between
    threads; the driver cutting in (stop from the mic thread while the player thread waits) could hang PortAudio and
    silence the engineer for the rest of the session. Here, stopping is just dropping the buffer the callback reads:
    no PortAudio call ever happens outside this object, and a wait can never block forever.
    """

    def __init__(self, sr: int, device=None, log=print):
        self.sr, self.device, self.log = sr, device, log
        self.lock = threading.Lock()
        self.buf: np.ndarray | None = None
        self.pos = 0
        self.tok = 0
        self.ev = threading.Event()
        self.stream = None
        self._open()

    def _open(self) -> None:
        import sounddevice as sd
        try:
            self.stream = sd.OutputStream(samplerate=self.sr, channels=1, dtype="float32", device=self.device,
                                          callback=self._cb)
            self.stream.start()
        except Exception as e:
            self.stream = None
            self.log(f"audio output failed to open: {e!r}")

    def _cb(self, outdata, frames, t, status):
        with self.lock:
            b = self.buf
            if b is None:
                outdata.fill(0)
                return
            chunk = b[self.pos:self.pos + frames]
            outdata[:len(chunk), 0] = chunk
            outdata[len(chunk):] = 0
            self.pos += frames
            if self.pos >= len(b):
                self.buf = None
                self.ev.set()

    def play(self, audio: np.ndarray, wait: bool = True, cancelled=None) -> int:
        """Play (replacing whatever is playing). `cancelled(tok)` is checked once the audio is queued, so a driver cut-in
        that lands at the same moment always wins. Returns the playback token."""
        if self.stream is None or not self.stream.active:
            self._reopen()
        ev = threading.Event()
        with self.lock:
            self.ev.set()             # whoever waited on the previous sound is released
            self.tok += 1
            tok, self.ev = self.tok, ev
            self.buf, self.pos = np.ascontiguousarray(audio, dtype=np.float32).ravel(), 0
        if cancelled is not None and cancelled(tok):
            self.stop(tok)
        if wait and not ev.wait(timeout=len(audio) / self.sr + 2.0):
            self.log("audio output stuck: reopening the device")
            self.stop(tok)
            self._reopen()
        return tok

    def stop(self, tok: int | None = None) -> None:
        """Stop the current sound (only if it is still `tok`, when given)."""
        with self.lock:
            if self.buf is not None and (tok is None or tok == self.tok):
                self.buf = None
                self.ev.set()

    def _reopen(self) -> None:
        old, self.stream = self.stream, None
        if old is not None:   # closing a broken stream can itself block: never on our thread
            threading.Thread(target=lambda: (old.abort(), old.close()), daemon=True).start()
        self._open()

    def name(self) -> str:
        import sounddevice as sd
        try:
            return sd.query_devices(self.device if self.device is not None else sd.default.device[1])["name"]
        except Exception:
            return "?"


_VK = {"shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "caps_lock": 0x14, "capslock": 0x14, "space": 0x20,
       "tab": 0x09, "backspace": 0x08, "enter": 0x0D, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
       "page_up": 0x21, "page_down": 0x22, "`": 0xC0, "backtick": 0xC0, "mouse4": 0x05, "mouse5": 0x06}


def _vk_code(name: str) -> int | None:
    """Windows virtual-key code for a key name ('shift', 't', 'f9', 'mouse4', ...)."""
    n = name.strip().lower()
    if n in _VK:
        return _VK[n]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    if n.startswith("f") and n[1:].isdigit() and 1 <= int(n[1:]) <= 24:
        return 0x6F + int(n[1:])
    return None


class XInput:
    """Minimal XInput poller (Windows). Returns pressed button bitmask, 0 if no pad."""

    class _State(ctypes.Structure):
        _fields_ = [("dwPacketNumber", ctypes.c_uint), ("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                    ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                    ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]

    BUTTONS = {"DPAD_UP": 0x1, "DPAD_DOWN": 0x2, "DPAD_LEFT": 0x4, "DPAD_RIGHT": 0x8, "MENU": 0x10, "VIEW": 0x20,
               "LS": 0x40, "RS": 0x80, "LB": 0x100, "RB": 0x200, "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000}

    def __init__(self):
        self.dll = None
        for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
            try:
                self.dll = getattr(ctypes.windll, name)
                break
            except (OSError, AttributeError):
                continue

    def buttons(self) -> int:
        if self.dll is None:
            return 0
        st = self._State()
        if self.dll.XInputGetState(0, ctypes.byref(st)) != 0:
            return 0
        return st.wButtons

    def connected(self) -> bool:
        if self.dll is None:
            return False
        st = self._State()
        return self.dll.XInputGetState(0, ctypes.byref(st)) == 0


def worker(cfg, inq: mp.Queue, outq: mp.Queue) -> None:
    import sounddevice as sd
    try:
        import psutil
        psutil.Process().nice(psutil.NORMAL_PRIORITY_CLASS if os.name == "nt" else 0)   # latency-critical, bursty
    except Exception:
        pass
    log = lambda s: outq.put(("log", s))  # noqa: E731
    try:   # a native crash (audio driver, speech libraries) leaves a stack trace instead of silence
        import faulthandler
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        faulthandler.enable(open(cfg.logs_dir / "speech_crash.log", "a"), all_threads=True)
    except Exception:
        pass
    t = time.perf_counter()
    tts = TTS(cfg)
    _synth, synth_lock = tts.synth, threading.Lock()

    def synth_locked(text):   # the queue and the direct lines ("Go ahead") never synthesise at the same time
        with synth_lock:
            return _synth(text)
    tts.synth = synth_locked
    log(f"TTS ready ({time.perf_counter() - t:.1f}s)")
    t = time.perf_counter()
    stt = STT(cfg)
    log(f"STT {stt.name} ready on {stt.device} ({time.perf_counter() - t:.1f}s)")
    outq.put(("ready", stt.device))

    from f1engineer.floor import Floor
    floor = Floor()
    spoken: deque = deque(maxlen=12)       # (time, text) of what we said: ignored if the mic hears it back
    ptt_sources = {"key": False, "pad": False, "game": False}
    rec = {"on": False, "chunks": []}
    preroll: deque = deque(maxlen=5)   # last 0.5 s before the button: people start talking as they press
    lock = threading.Lock()
    out = Speaker(tts.sr, pick_output(cfg.speaker), log)
    if cfg.speaker and out.device is None:
        log(f"speaker '{cfg.speaker}' not found: using the Windows default")
    log(f"speaker: {out.name()}")
    queue_tok = {"tok": None}           # playback token of the queued item being spoken (None = direct line)

    def beep(freq: float):
        """Short radio chirp so the driver knows the button registered (rising = open, falling = closed)."""
        try:
            t = np.arange(int(tts.sr * 0.07)) / tts.sr
            out.play(0.18 * min(1.0, cfg.volume) * np.sin(2 * np.pi * freq * t) * np.hanning(len(t)))
        except Exception:
            pass

    def speak_now(text: str):
        """Short direct line ("Go ahead"), outside the queue: only used while the driver holds the floor."""
        try:
            audio = tts.synth(text) * min(1.0, max(0.0, cfg.volume))
            spoken.append((time.time(), text))
            log(f"said: {text!r}")
            out.play(audio)
        except Exception as e:
            log(f"playback failed: {e!r}")

    def player():
        while True:
            it = floor.next()
            if it is None:
                return
            text = it.text
            if it.resumed:   # cut off earlier by the driver: pick the thread back up
                text = "As I was saying, " + text[:1].lower() + text[1:]
                it.resumed = False
                log(f"resuming after the driver: {text[:60]!r}")
            try:
                audio = tts.synth(text) * min(1.0, max(0.0, cfg.volume))
                if floor.current is it and not floor.interrupted:   # else the driver cut in: it's back in the queue
                    spoken.append((time.time(), text))
                    out.play(audio, cancelled=mark)
            except Exception as e:
                log(f"playback failed: {e!r}")
            finally:
                queue_tok["tok"] = None
                if floor.done(it):
                    outq.put(("spoken", it.key, it.kind))

    def mark(tok: int) -> bool:
        """Queued speech just started: remember its token (a cut-in stops exactly this sound); stop if already cut."""
        queue_tok["tok"] = tok
        return floor.interrupted

    def driver_floor(on: bool):
        """The driver takes (or gives back) the floor: stop mid-sentence, resume it later."""
        cut = floor.set_driver(on)
        if cut is not None and queue_tok["tok"] is not None:
            out.stop(queue_tok["tok"])
        outq.put(("floor", "driver" if on else "free"))

    def set_ptt(src: str, down: bool, via: str = ""):
        with lock:
            was = any(ptt_sources.values())
            ptt_sources[src] = down
            now = any(ptt_sources.values())
        if now and not was:
            driver_floor(True)         # barge-in: he stops, and resumes that sentence afterwards
            rec["chunks"] = list(preroll)
            rec["on"] = True
            beep(1200)
            log(f"PTT down ({src}{', ' + via if via else ''})")
        elif was and not now:
            threading.Thread(target=finish, daemon=True).start()

    def finish():
        time.sleep(0.3)   # tail of the last word
        rec["on"] = False
        beep(800)
        try:
            transcribe_ptt()
        finally:
            driver_floor(False)

    def transcribe_ptt():
        try:
            if not rec["chunks"]:
                log("PTT released: no microphone audio captured (check the Windows default input device)")
                return
            audio = np.concatenate(rec["chunks"])[:, 0]
            level = 20 * np.log10(float(np.sqrt(np.mean(audio ** 2))) + 1e-9)
            if len(audio) < 16000 * 0.3:
                log(f"PTT released after {len(audio) / 16000:.2f}s: too short, hold the key while you speak")
                outq.put(("transcript", "", 0))
                return
            t0 = time.perf_counter()
            with stt_lock:
                text = stt.transcribe_safe(audio, cfg, log)
            ms = (time.perf_counter() - t0) * 1000
            log(f"PTT released: {len(audio) / 16000:.1f}s audio, level {level:.0f} dBFS, recognised in {ms:.0f} ms on {stt.device}: {text!r}")
            outq.put(("transcript", text or "", round(ms)))   # pressed = heard: no second-guessing
            if not text and level < -55:
                log("microphone is (nearly) silent: wrong input device or muted?")
        except Exception as e:
            log(f"speech recognition failed: {e!r}")

    wake_q: queue.Queue = queue.Queue(maxsize=100)

    mic_seen = {"t": time.time()}

    def mic_cb(indata, frames, t, status):
        mic_seen["t"] = time.time()
        if rec["on"]:
            rec["chunks"].append(indata.copy())
        else:
            preroll.append(indata.copy())
            if wake_on:
                try:
                    wake_q.put_nowait(indata[:, 0].copy())
                except queue.Full:
                    pass

    # ---- hands-free "Hey Bono" -------------------------------------------------------------------------
    from f1engineer import wake as W
    wk = W.load(cfg.data_dir / "wake.json")
    wake_on = cfg.wake == "on" or (cfg.wake == "auto" and wk is not None)
    stt_lock = threading.Lock()

    def echo(text: str) -> bool:
        """Is this just our own voice coming back through the mic?"""
        import difflib
        t = text.lower().strip(" .!?")
        return any(time.time() - ts < 25 and difflib.SequenceMatcher(None, t, said.lower()).ratio() > 0.6
                   for ts, said in list(spoken))

    GO_AHEAD = ["Go ahead.", "Yep, go.", "Listening."]
    NOTHING = ["Didn't hear anything. Say Bono when you're ready.", "Nothing heard. Call me again when you're ready."]
    cached: dict = {}

    def precache():   # the replies to "Hey Bono" are ready before you ever say it: no synthesis on that path
        for line in GO_AHEAD + NOTHING:
            try:
                cached[line] = tts.synth(line)
            except Exception:
                pass

    def speak_cached(line: str):
        if line not in cached:
            return speak_now(line)
        spoken.append((time.time(), line))
        log(f"said: {line!r}")
        out.play(cached[line] * min(1.0, max(0.0, cfg.volume)))

    def wake_loop():
        names = W.variants(cfg.wake_name, (wk or {}).get("heard"))
        # waiting for the name: 0.6 s of quiet ends it (fast); your question after "Go ahead": 1.4 s (never cut off)
        gate = W.VoiceGate(W.safe_threshold(wk), hang=0.6, max_len=15.0)
        window_until = 0.0        # after "Go ahead": you have 2.5 s to start talking
        log(f"wake word on: say 'Hey {cfg.wake_name}' (trigger level {gate.th:.0f} dBFS)")

        def open_window():
            nonlocal window_until
            gate.hang = 1.4
            window_until = time.time() + 2.5

        def close_window():
            nonlocal window_until
            gate.hang, window_until = 0.6, 0.0

        while True:
            try:
                block = wake_q.get(timeout=0.1)
            except queue.Empty:
                block = None
            if window_until and not gate.active and time.time() > window_until:
                close_window()
                speak_cached(random.choice(NOTHING))
                driver_floor(False)
                continue
            if block is None:
                continue
            seg = gate.feed(block)
            if seg is None or rec["on"]:
                continue
            quick = not window_until and len(seg) < 16000 * 3     # a short clip: is it the name?
            t0 = time.perf_counter()
            try:
                with stt_lock:
                    text = stt.transcribe_safe(seg, cfg, log, quick=quick)
            except Exception as e:
                log(f"wake transcription failed: {e!r}")
                continue
            if not text or W.is_phantom(text) or echo(text):
                continue
            if window_until:                       # the question after "Go ahead"
                close_window()
                log(f"wake question: {text!r}")
                outq.put(("transcript", text, 0))
                driver_floor(False)
                continue
            call, rest = W.split_wake(text, names)
            if not call:
                continue
            log(f"wake word heard: {text!r} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
            driver_floor(True)                     # barge-in if he was talking; he'll resume afterwards
            if len(rest.split()) >= 3:             # "Bono, what's my gap": the question came with the name
                if quick:                          # the name check was greedy: hear the question properly
                    with stt_lock:
                        full = stt.transcribe_safe(seg, cfg, log)
                    rest = W.split_wake(full, names)[1] or rest
                beep(1200)
                outq.put(("transcript", rest, 0))
                driver_floor(False)
                continue
            # still talking after the name ("Bono ... what's my gap")? then no "Go ahead": just keep listening
            pending = []
            while not wake_q.empty():
                try:
                    pending.append(wake_q.get_nowait())
                except queue.Empty:
                    break
            gate.reset()
            open_window()
            if pending and any(20 * np.log10(float(np.sqrt(np.mean(b ** 2))) + 1e-9) > gate.th for b in pending[-4:]):
                for b in pending:
                    gate.feed(b)
                beep(1200)
                continue
            beep(1200)
            speak_cached(random.choice(GO_AHEAD))
            while not wake_q.empty():              # drop what the mic heard of our own "Go ahead"
                try:
                    wake_q.get_nowait()
                except queue.Empty:
                    break
            gate.reset()
            open_window()

    def wake_supervised():
        while True:   # an error in one utterance must never leave the engineer deaf for the rest of the session
            try:
                wake_loop()
            except Exception as e:
                log(f"wake listener error, restarting: {e!r}")
                driver_floor(False)
                time.sleep(0.5)

    if wake_on:
        threading.Thread(target=precache, daemon=True).start()
        threading.Thread(target=wake_supervised, name="wake", daemon=True).start()

    threading.Thread(target=player, daemon=True).start()
    mic = None
    dev = pick_input(cfg.mic)
    if cfg.mic and dev is None:
        log(f"microphone '{cfg.mic}' not found: using the Windows default")

    def open_mic():
        nonlocal mic
        try:
            mic = sd.InputStream(samplerate=16000, channels=1, dtype="float32", callback=mic_cb, blocksize=1600, device=dev)
            mic.start()
            mic_seen["t"] = time.time()
            return True
        except Exception as e:
            log(f"no microphone: {e!r}")
            return False

    if open_mic():
        name = sd.query_devices(dev if dev is not None else sd.default.device[0])["name"]
        log(f"microphone: {name}; PTT key '{cfg.ptt_key}', pad button {cfg.ptt_game_button}")

    def mic_watch():
        """A microphone stream can stop delivering audio (device switched, driver hiccup): then reopen it."""
        nonlocal mic
        while True:
            time.sleep(1)
            if time.time() - mic_seen["t"] < 3:
                continue
            log("microphone stopped sending audio: reopening it")
            old, mic = mic, None
            if old is not None:   # closing a dead stream can block: never on this thread
                threading.Thread(target=lambda: (old.abort(), old.close()), daemon=True).start()
            if not open_mic():
                time.sleep(4)
    threading.Thread(target=mic_watch, daemon=True).start()

    # keyboard PTT: several independent detectors feed ONE key state (latest event wins, so nothing sticks)
    key_state = {"down": False, "since": 0.0}

    def key_event(src: str, down: bool):
        with lock:
            if down == key_state["down"]:
                return
            key_state["down"], key_state["since"] = down, time.time()
        set_ptt("key", down, via=src)

    vk = _vk_code(cfg.ptt_key) if sys.platform == "win32" else None
    if vk:
        from f1engineer import winkeys
        winkeys.raw_input_listener(vk, lambda d: key_event("raw input", d), log)
        winkeys.poll_listener(vk, lambda d: key_event("key state", d))

    def stuck_guard():   # a missed release must never leave the mic open
        while True:
            time.sleep(1)
            if key_state["down"] and time.time() - key_state["since"] > 20:
                log("PTT held over 20 s: releasing")
                key_event("guard", False)
    threading.Thread(target=stuck_guard, daemon=True).start()
    try:
        from pynput import keyboard
        key = cfg.ptt_key.lower()

        def match(k):
            # "shift"/"ctrl"/"alt" match the left and right key alike
            name = getattr(k, "name", None)
            return getattr(k, "char", None) and k.char.lower() == key or name in (key, key + "_l", key + "_r")

        kl = keyboard.Listener(on_press=lambda k: match(k) and key_event("hook", True),
                               on_release=lambda k: match(k) and key_event("hook", False))
        kl.daemon = True
        kl.start()
    except ImportError:
        pass
    except Exception as e:
        log(f"keyboard hotkey unavailable: {e!r} (use a controller button instead)")

    xi = XInput()
    pad_mask = XInput.BUTTONS.get(cfg.ptt_game_button, 0)

    def pad_poll():
        last = False
        while True:
            down = bool(xi.buttons() & pad_mask) if pad_mask else False
            if down != last:
                set_ptt("pad", down)
                last = down
            time.sleep(0.02)

    threading.Thread(target=pad_poll, daemon=True).start()

    def gpu_watch():
        """Cooperate with other GPU users: if free VRAM gets low, give ours back and use the CPU."""
        try:
            import pynvml
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            return
        while stt.device == "cuda":
            time.sleep(3)
            try:
                free_mb = pynvml.nvmlDeviceGetMemoryInfo(h).free / 1e6
            except Exception:
                continue
            if free_mb < 600 and not rec["on"]:
                stt.to_cpu(cfg)
                log(f"GPU memory low ({free_mb:.0f} MB free): speech recognition moved to CPU ({stt.name})")

    threading.Thread(target=gpu_watch, daemon=True).start()

    parent = os.getppid()
    while True:
        try:
            msg = inq.get(timeout=2)
        except queue.Empty:
            try:
                import psutil
                if not psutil.pid_exists(parent):
                    os._exit(0)  # engine died: never linger as an orphan
            except ImportError:
                pass
            continue
        if msg[0] == "say":
            _, text, kind, key, ttl = msg
            floor.put(text, kind, key, ttl)
        elif msg[0] == "hush":             # "stop": this message ends now; the queue waits
            if floor.current is not None and queue_tok["tok"] is not None:
                floor.interrupted = True
                out.stop(queue_tok["tok"])
        elif msg[0] == "ptt":
            set_ptt("game", msg[1])
        elif msg[0] == "quit":
            if mic:
                mic.stop()
            return


class SpeechClient:
    """Core-side handle to the speech process."""

    def __init__(self, cfg, on_transcript):
        self.cfg = cfg
        self.on_transcript = on_transcript
        self.on_spoken = None             # engine callback(key, kind)
        self.device = None
        self.driver_talking = False
        self._quit = False
        self._spawn()
        threading.Thread(target=self._watch, daemon=True).start()

    def _spawn(self):
        ctx = mp.get_context("spawn")
        self.inq, self.outq = ctx.Queue(), ctx.Queue()
        self.proc = ctx.Process(target=worker, args=(self.cfg, self.inq, self.outq), name="f1e-speech", daemon=True)
        self.proc.start()
        threading.Thread(target=self._pump, args=(self.outq,), daemon=True).start()

    def _watch(self):
        """The voice process must never die silently: if it does, say so in the log and bring it back."""
        restarts = 0
        while not self._quit:
            time.sleep(1)
            if self._quit or self.proc.is_alive():
                continue
            restarts += 1
            print(f"[speech] voice process stopped (exit code {self.proc.exitcode}): restarting ({restarts})")
            self.driver_talking = False
            if restarts > 5:
                print("[speech] voice process keeps stopping: giving up (see logs/speech_crash.log)")
                return
            self._spawn()

    def _pump(self, outq):
        while True:
            try:
                m = outq.get()
            except (EOFError, OSError, ValueError):
                return
            if m is None:
                return
            if m[0] == "transcript":
                print(f"[speech] heard ({m[2]} ms): {m[1]!r}")
                threading.Thread(target=self.on_transcript, args=(m[1],), daemon=True).start()
            elif m[0] == "ready":
                self.device = m[1]
            elif m[0] == "floor":
                self.driver_talking = m[1] == "driver"
            elif m[0] == "spoken":
                if self.on_spoken:
                    self.on_spoken(m[1], m[2])
            else:
                print(f"[speech] {m[1]}")

    def say(self, text: str, priority: int = 5, kind: str | None = None, key: str = "", ttl: float | None = None) -> None:
        kind = kind or ("urgent" if priority <= 0 else "answer" if priority == 1 else "proactive")
        self.inq.put(("say", text, kind, key, ttl))

    def hush(self) -> None:
        self.inq.put(("hush",))

    def ptt(self, down: bool) -> None:
        self.inq.put(("ptt", down))

    def stop(self) -> None:
        self._quit = True
        try:
            self.inq.put(("quit",))
            self.proc.join(3)
        finally:
            if self.proc.is_alive():
                self.proc.terminate()
