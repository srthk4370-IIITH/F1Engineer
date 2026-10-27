"""End-to-end latency benchmark (no game needed): ingest, lap analysis, and PTT-release -> first audio.

  uv run python tools/bench_latency.py [capture]
"""
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from f1engineer.config import Config  # noqa: E402
from f1engineer.engine import Engine  # noqa: E402
from f1engineer.storage import Storage  # noqa: E402
from f1engineer.telemetry.capture import read_capture  # noqa: E402


def main():
    cap = sys.argv[1] if len(sys.argv) > 1 else "data/captures/real_f1_2020_session1.f1cap"
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp())
    eng = Engine(cfg, storage=Storage(cfg))
    eng.speech.say = lambda *a: None
    pk = [d for _, d in read_capture(cap)]
    t = time.perf_counter()
    for d in pk:
        eng.feed(d)
    ingest_us = (time.perf_counter() - t) / len(pk) * 1e6
    eng.drain()
    print(f"ingest: {ingest_us:.1f} us/packet  ->  {ingest_us * 270 / 1e4:.2f}% of one core at the game's ~270 packets/s")
    print(f"lap analysis: {eng.stats['last_lap_analysis_ms']} ms per lap")

    from f1engineer.speech import STT, TTS
    tts, stt = TTS(cfg), STT(cfg)
    q = "Where am I losing time at the Loop?"
    a = TTS(cfg, fx=False).synth(q)
    a16 = np.interp(np.arange(0, len(a), tts.sr / 16000), np.arange(len(a)), a).astype(np.float32)
    rows = []
    for _ in range(3):
        t0 = time.perf_counter()
        text = stt.transcribe(a16)
        t1 = time.perf_counter()
        first = next(iter(eng.agent.ask(text)))
        t2 = time.perf_counter()
        tts.synth(first)
        t3 = time.perf_counter()
        rows.append((t1 - t0, t2 - t1, t3 - t2, t3 - t0))
    s, l, v, tot = np.median(np.array(rows), axis=0) * 1000
    print(f"STT ({stt.name} on {stt.device}): {s:.0f} ms | LLM first sentence ({type(eng.agent.provider).__name__}): {l:.0f} ms | "
          f"TTS first sentence: {v:.0f} ms | PTT release -> first audio: {tot:.0f} ms")
    print(f"  heard: {text!r} -> said: {first!r}")


if __name__ == "__main__":
    main()
