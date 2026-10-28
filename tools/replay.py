"""Replay a .f1cap through the engine.

  uv run python tools/replay.py data/captures/x.f1cap            # as fast as possible, radio printed
  uv run python tools/replay.py x.f1cap --udp --speed 1          # re-send over UDP to a running engine (real time)
"""
import argparse
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from f1engineer import radio  # noqa: E402
from f1engineer.telemetry.capture import read_capture  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--udp", action="store_true", help="send packets over UDP instead of in-process")
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--speed", type=float, default=0, help="0 = as fast as possible (in-process only)")
    ap.add_argument("--ask", action="append", default=[], help="question(s) to ask the engineer at the end")
    a = ap.parse_args()
    if a.udp:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        t_first = w_first = None
        for t, d in read_capture(a.capture):
            if t_first is None:
                t_first, w_first = t, time.time()
            if a.speed > 0:
                wait = (t - t_first) / a.speed - (time.time() - w_first)
                if wait > 0:
                    time.sleep(wait)
            sock.sendto(d, ("127.0.0.1", a.port))
        return
    import tempfile
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.storage import Storage
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="f1e_replay_"))
    eng = Engine(cfg, storage=Storage(cfg))
    clock = [0.0]
    radio.CLOCK = lambda: clock[0]
    eng.speech.say = lambda text, prio=5: print(f"  [{clock[0] - t0:7.1f}s] RADIO: {text}")
    t0 = None
    n = 0
    t_start = time.perf_counter()
    for t, d in read_capture(a.capture):
        t0 = t0 or t
        clock[0] = t
        eng.feed(d, wall=t)
        n += 1
        if n % 24 == 0:  # ~10 Hz scheduler ticks in capture time
            eng.sched.tick(eng.ctx())
            eng.coach.tick()
    eng.drain()
    el = time.perf_counter() - t_start
    print(f"\n{n} packets in {el:.1f}s ({n / el:.0f} pkt/s, {(t - t0) / el:.0f}x real time); "
          f"last lap analysis {eng.stats['last_lap_analysis_ms']} ms; parse errors {eng.stats['parse_errors']}")
    for r in eng.reports:
        print(f"lap {r.lap_num}: {r.lap_time} {r.input_device} delta {r.delta:+.3f} vs {r.ref_label}; "
              f"top loss {[(c['corner'], c['time_loss']) for c in r.losses[:3]]}")
    print("advice:", [x["change"] for x in eng.advice])
    for q in a.ask:
        print(f"\nQ: {q}")
        print("A:", eng.ask(q))


if __name__ == "__main__":
    main()
