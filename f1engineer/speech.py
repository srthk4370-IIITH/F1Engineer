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
              "Luffield, the Loop. Box box, undercut, overcut, DRS, ERS, softs, mediums, hards, inters, brake bias, "
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

    def transcribe_safe(self, audio16k: np.ndarray, cfg, log) -> str:
        """transcribe(), but a GPU failure (e.g. the game grabbed the VRAM) falls back to the CPU and retries."""
        try:
            return self.transcribe(audio16k)
        except Exception as e:
            if self.device != "cuda":
                raise
            log(f"GPU speech recognition failed ({e!r}): switching to CPU")
            self.to_cpu(cfg)
            return self.transcribe(audio16k)

    def transcribe(self, audio16k: np.ndarray) -> str:
        if self.cloud is not None:
            try:
                return self.cloud(prep_audio(audio16k))
            except Exception as e:
                print(f"[speech] cloud transcription failed ({e!r}): using the local model")
        segs, _ = self.model.transcribe(prep_audio(audio16k), language="en", beam_size=5 if self.device == "cuda" else 2,
                                        initial_prompt=STT_PROMPT, vad_filter=False, condition_on_previous_text=False,
                                        without_timestamps=True, temperature=0.0)
        return " ".join(s.text.strip() for s in segs).strip()


def input_devices() -> list[str]:
    """Microphone names (MME host API: it resamples to our 16 kHz on any device), without duplicates."""
    import sounddevice as sd
    mme = next((i for i, h in enumerate(sd.query_hostapis()) if h["name"] == "MME"), None)
    out = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0 and (mme is None or d["hostapi"] == mme) and "Mapper" not in d["name"] \
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
    t = time.perf_counter()
    tts = TTS(cfg)
    log(f"TTS ready ({time.perf_counter() - t:.1f}s)")
    t = time.perf_counter()
    stt = STT(cfg)
    log(f"STT {stt.name} ready on {stt.device} ({time.perf_counter() - t:.1f}s)")
    outq.put(("ready", stt.device))

    say_q: queue.PriorityQueue = queue.PriorityQueue()
    seq = [0]
    playing = threading.Event()
    ptt_sources = {"key": False, "pad": False, "game": False}
    rec = {"on": False, "chunks": []}
    preroll: deque = deque(maxlen=5)   # last 0.5 s before the button: people start talking as they press
    lock = threading.Lock()

    def beep(freq: float):
        """Short radio chirp so the driver knows the button registered (rising = open, falling = closed)."""
        try:
            t = np.arange(int(tts.sr * 0.07)) / tts.sr
            sd.play((0.18 * min(1.0, cfg.volume) * np.sin(2 * np.pi * freq * t) * np.hanning(len(t))).astype(np.float32), tts.sr)
        except Exception:
            pass

    def player():
        while True:
            prio, _, text = say_q.get()
            if text is None:
                return
            if rec["on"]:
                continue  # driver is talking: drop engineer chatter
            try:
                audio = tts.synth(text) * min(1.0, max(0.0, cfg.volume))
                playing.set()
                sd.play(audio, tts.sr)
                sd.wait()
            except Exception as e:
                log(f"playback failed: {e!r}")
            finally:
                playing.clear()

    def set_ptt(src: str, down: bool, via: str = ""):
        with lock:
            was = any(ptt_sources.values())
            ptt_sources[src] = down
            now = any(ptt_sources.values())
        if now and not was:
            if playing.is_set():
                sd.stop()  # barge-in
            while not say_q.empty():
                try:
                    say_q.get_nowait()
                except queue.Empty:
                    break
            rec["chunks"] = list(preroll)
            rec["on"] = True
            beep(1200)
            log(f"PTT down ({src}{', ' + via if via else ''})")
        elif was and not now:
            threading.Thread(target=finish, daemon=True).start()

    def finish():
        time.sleep(0.25)  # tail of the last word
        rec["on"] = False
        beep(800)
        try:
            if not rec["chunks"]:
                log("PTT released: no microphone audio captured (check the Windows default input device)")
                return
            audio = np.concatenate(rec["chunks"])[:, 0]
            level = 20 * np.log10(float(np.sqrt(np.mean(audio ** 2))) + 1e-9)
            if len(audio) < 16000 * 0.3:
                log(f"PTT released after {len(audio) / 16000:.2f}s: too short, hold the key while you speak")
                return
            t0 = time.perf_counter()
            with stt_lock:
                text = stt.transcribe_safe(audio, cfg, log)
            ms = (time.perf_counter() - t0) * 1000
            log(f"PTT released: {len(audio) / 16000:.1f}s audio, level {level:.0f} dBFS, recognised in {ms:.0f} ms on {stt.device}: {text!r}")
            if text and not W.is_phantom(text) and level > -60:
                outq.put(("transcript", text, round(ms)))
            elif len(audio) > 16000 * 0.8:     # held the button properly but said nothing usable
                outq.put(("transcript", "", round(ms)))
                if level < -55:
                    log("microphone is (nearly) silent: wrong input device or muted?")
        except Exception as e:
            log(f"speech recognition failed: {e!r}")

    wake_q: queue.Queue = queue.Queue(maxsize=100)

    def mic_cb(indata, frames, t, status):
        if rec["on"]:
            rec["chunks"].append(indata.copy())
        else:
            preroll.append(indata.copy())
            if wake_on and not playing.is_set():   # never listen to our own voice
                try:
                    wake_q.put_nowait(indata[:, 0].copy())
                except queue.Full:
                    pass

    # ---- hands-free "Hey Bono" -------------------------------------------------------------------------
    from f1engineer import wake as W
    wk = W.load(cfg.data_dir / "wake.json")
    wake_on = cfg.wake == "on" or (cfg.wake == "auto" and wk is not None)
    stt_lock = threading.Lock()

    def wake_loop():
        names = W.variants(cfg.wake_name, (wk or {}).get("heard"))
        gate = W.VoiceGate(W.safe_threshold(wk), hang=1.2)   # a breath mid-sentence isn't the end
        arm = False
        await_until = 0.0
        log(f"wake word on: say 'Hey {cfg.wake_name}' (trigger level {gate.th:.0f} dBFS)")
        while True:
            block = wake_q.get()
            if arm:                      # first audio after "Go ahead" finished playing: the question starts now
                gate.arm()
                arm = False
            seg = gate.feed(block)
            if seg is None or rec["on"]:
                continue
            try:
                with stt_lock:
                    text = stt.transcribe_safe(seg, cfg, log)
            except Exception as e:
                log(f"wake transcription failed: {e!r}")
                continue
            if not text or W.is_phantom(text):
                continue
            if time.time() < await_until:          # the question after "Bono? - Go ahead."
                await_until = 0.0
                log(f"wake question: {text!r}")
                outq.put(("transcript", text, 0))
                continue
            call, rest = W.split_wake(text, names)
            if not call:
                continue
            log(f"wake word heard: {text!r}")
            if len(rest.split()) >= 3:
                beep(1200)
                outq.put(("transcript", rest, 0))
            else:
                beep(1200)
                seq[0] += 1
                say_q.put((0, seq[0], random.choice(["Go ahead.", "Yep, go.", "Listening."])))
                await_until = time.time() + 12.0
                arm = True
    if wake_on:
        threading.Thread(target=wake_loop, name="wake", daemon=True).start()

    threading.Thread(target=player, daemon=True).start()
    mic = None
    try:
        dev = pick_input(cfg.mic)
        if cfg.mic and dev is None:
            log(f"microphone '{cfg.mic}' not found: using the Windows default")
        mic = sd.InputStream(samplerate=16000, channels=1, dtype="float32", callback=mic_cb, blocksize=1600, device=dev)
        mic.start()
        name = sd.query_devices(dev if dev is not None else sd.default.device[0])["name"]
        log(f"microphone: {name}; PTT key '{cfg.ptt_key}', pad button {cfg.ptt_game_button}")
    except Exception as e:
        log(f"no microphone: {e!r}")

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
            seq[0] += 1
            say_q.put((msg[2], seq[0], msg[1]))
        elif msg[0] == "ptt":
            set_ptt("game", msg[1])
        elif msg[0] == "quit":
            say_q.put((-1, 0, None))
            if mic:
                mic.stop()
            return


class SpeechClient:
    """Core-side handle to the speech process."""

    def __init__(self, cfg, on_transcript):
        ctx = mp.get_context("spawn")
        self.inq, self.outq = ctx.Queue(), ctx.Queue()
        self.proc = ctx.Process(target=worker, args=(cfg, self.inq, self.outq), name="f1e-speech", daemon=True)
        self.proc.start()
        self.on_transcript = on_transcript
        self.device = None
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            try:
                m = self.outq.get()
            except (EOFError, OSError):
                return
            if m[0] == "transcript":
                print(f"[speech] heard ({m[2]} ms): {m[1]!r}")
                threading.Thread(target=self.on_transcript, args=(m[1],), daemon=True).start()
            elif m[0] == "ready":
                self.device = m[1]
            else:
                print(f"[speech] {m[1]}")

    def say(self, text: str, priority: int = 5) -> None:
        self.inq.put(("say", text, priority))

    def ptt(self, down: bool) -> None:
        self.inq.put(("ptt", down))

    def stop(self) -> None:
        try:
            self.inq.put(("quit",))
            self.proc.join(3)
        finally:
            if self.proc.is_alive():
                self.proc.terminate()
