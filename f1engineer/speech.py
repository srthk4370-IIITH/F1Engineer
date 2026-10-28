"""Speech worker (separate process so ML never stalls telemetry ingest).

Core -> worker:  ("say", text, priority) | ("ptt", bool) | ("quit",)
Worker -> core:  ("transcript", text, stt_ms) | ("log", text)

Push-to-talk sources (OR-ed): keyboard hotkey (pynput), XInput controller button (ctypes, no deps),
and the game's own UDP button state forwarded by the core.
"""
from __future__ import annotations

import ctypes
import re
import multiprocessing as mp
import queue
import threading
import time

import numpy as np

STT_PROMPT = ("F1 race engineer radio. Silverstone, Monza, Spa, Monaco, Copse, Maggotts, Becketts, Stowe, Vale, Club, "
              "Luffield, the Loop. Box box, undercut, overcut, DRS, ERS, softs, mediums, hards, inters, brake bias, "
              "front wing, rear wing, differential, understeer, oversteer, lock-up, tyre wear, delta, sector, lap time.")


def _bandpass(sr: int, lo: float = 300, hi: float = 3400, taps: int = 101) -> np.ndarray:
    n = np.arange(taps) - (taps - 1) / 2
    h = (2 * hi / sr) * np.sinc(2 * hi / sr * n) - (2 * lo / sr) * np.sinc(2 * lo / sr * n)
    return (h * np.hamming(taps)).astype(np.float32)


def radio_fx(x: np.ndarray, sr: int) -> np.ndarray:
    """Cheap team-radio colour: band-limit ~300-3400 Hz, light saturation, squelch clicks."""
    y = np.convolve(x.astype(np.float32) / 32768.0, _bandpass(sr), mode="same")
    y = np.tanh(y * 2.5) * 0.8
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

    def synth(self, text: str) -> np.ndarray:
        pcm = b"".join(ch.audio_int16_bytes for ch in self.voice.synthesize(speakable(text)))
        x = np.frombuffer(pcm, dtype=np.int16)
        return radio_fx(x, self.sr) if self.fx else x.astype(np.float32) / 32768.0


def _register_cuda_dlls() -> None:
    """pip-installed CUDA libs (nvidia-cublas-cu12 / nvidia-cudnn-cu12) live in site-packages/nvidia/*/bin."""
    import os
    import site
    for sp in site.getsitepackages():
        root = os.path.join(sp, "nvidia")
        if os.path.isdir(root):
            for sub in os.listdir(root):
                b = os.path.join(root, sub, "bin")
                if os.path.isdir(b):
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
        auto = cfg.stt_model == "auto"
        if _gpu_ok(cfg):
            c = copy.copy(cfg)
            c.stt_model = "base.en" if auto else cfg.stt_model
            try:  # CUDA needs cuBLAS/cuDNN DLLs; the warm-up proves they load
                self.model = WhisperModel(str(ensure_whisper(c)), device="cuda", compute_type="float16")
                self.transcribe(np.zeros(16000, np.float32))
                self.device, self.name = "cuda", c.stt_model
            except Exception:
                self.model = None
        if self.model is None:
            c = copy.copy(cfg)
            c.stt_model = "tiny.en" if auto else cfg.stt_model
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
        c.stt_model = "tiny.en" if cfg.stt_model == "auto" else cfg.stt_model
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
        segs, _ = self.model.transcribe(audio16k, language="en", beam_size=1, initial_prompt=STT_PROMPT,
                                        vad_filter=False, condition_on_previous_text=False, without_timestamps=True)
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
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
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

    def set_ptt(src: str, down: bool):
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
            rec["chunks"] = []
            rec["on"] = True
            beep(1200)
            log(f"PTT down ({src})")
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
            text = stt.transcribe_safe(audio, cfg, log)
            ms = (time.perf_counter() - t0) * 1000
            log(f"PTT released: {len(audio) / 16000:.1f}s audio, level {level:.0f} dBFS, recognised in {ms:.0f} ms on {stt.device}: {text!r}")
            if text:
                outq.put(("transcript", text, round(ms)))
            elif level < -55:
                log("microphone is (nearly) silent: wrong input device or muted?")
        except Exception as e:
            log(f"speech recognition failed: {e!r}")

    def mic_cb(indata, frames, t, status):
        if rec["on"]:
            rec["chunks"].append(indata.copy())

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

    try:
        from pynput import keyboard
        key = cfg.ptt_key.lower()

        def match(k):
            # "shift"/"ctrl"/"alt" match the left and right key alike
            name = getattr(k, "name", None)
            return getattr(k, "char", None) and k.char.lower() == key or name in (key, key + "_l", key + "_r")

        kl = keyboard.Listener(on_press=lambda k: match(k) and set_ptt("key", True),
                               on_release=lambda k: match(k) and set_ptt("key", False))
        kl.daemon = True
        kl.start()
    except Exception as e:
        log(f"keyboard hotkey unavailable: {e!r}")

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

    import os
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
                print(f"[speech] heard ({m[2]} ms): {m[1]}")
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
