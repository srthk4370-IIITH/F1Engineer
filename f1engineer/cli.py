"""f1engineer command line.

  run        start the engineer now (telemetry + radio + dashboard)
  watch      background watcher: auto-start with the game (what autostart launches)
  autostart  on|off   start the watcher at Windows logon (Startup-folder shortcut)
  udp        show / enable / restore the game's UDP telemetry config
  dashboard  review stored laps any time (no game needed)
  import     analyse a recorded .f1cap into the lap database
  replay     replay a .f1cap capture over UDP into a running engine
  record     record raw telemetry to data/captures
  setup      first-time onboarding
  doctor     check everything
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import webbrowser


def cmd_run(a) -> None:
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.guard import InstanceLock, lower_priority, setup_logging, start_watchdogs
    from f1engineer.storage import Storage

    cfg = Config()
    if a.no_speech:
        cfg.speech = False
    setup_logging(cfg.logs_dir, "engine")
    lower_priority()
    lock = InstanceLock(cfg.data_dir / "engine.lock")
    if not lock.acquire():
        print("[run] the engineer is already running")
        return
    store = Storage(cfg)
    speech = None
    eng_holder = {}
    if cfg.speech:
        try:
            from f1engineer.speech import SpeechClient
            speech = SpeechClient(cfg, lambda text: eng_holder["e"].ask(text))
        except Exception as e:
            print(f"[run] speech disabled: {e!r}")
    eng = Engine(cfg, speech=speech, storage=store)
    eng_holder["e"] = eng
    try:
        eng.start()
    except RuntimeError as e:
        print(f"[run] {e}")
        if speech:
            speech.stop()
        lock.release()
        os._exit(2)

    stop = threading.Event()

    def fatal(reason):
        print(f"[run] stopping: {reason}")
        stop.set()

    start_watchdogs(cfg, store, fatal)
    import uvicorn
    from f1engineer.web.app import create_app
    server = uvicorn.Server(uvicorn.Config(create_app(eng), host="127.0.0.1", port=cfg.web_port, log_level="warning"))

    def serve():  # an offline `f1engineer dashboard` may hold the port; it hands over within ~2 s once it sees us
        while not stop.is_set() and not _port_free(cfg.web_port):
            time.sleep(1)
        server.run()
    threading.Thread(target=serve, daemon=True).start()
    print(f"[run] dashboard: http://127.0.0.1:{cfg.web_port}")
    if not a.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{cfg.web_port}")).start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        eng.stop()
        if speech:
            speech.stop()
        lock.release()
        print("[run] bye")
        os._exit(0)


def _port_free(port: int) -> bool:
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def cmd_dashboard(a) -> None:
    """Review stored laps without the game. Hands the port to the live engineer when the game starts."""
    from f1engineer.config import Config
    from f1engineer.guard import InstanceLock, lower_priority
    cfg = Config()
    url = f"http://127.0.0.1:{cfg.web_port}/#review"
    engine_lock = InstanceLock(cfg.data_dir / "engine.lock")
    if engine_lock.held_by_other() or not _port_free(cfg.web_port):
        print(f"[dashboard] already running (live engineer or another dashboard): {url}")
        webbrowser.open(url)
        return
    lower_priority()
    import uvicorn
    from f1engineer.storage import Storage
    from f1engineer.web.app import ReviewOnly, create_app
    server = uvicorn.Server(uvicorn.Config(create_app(ReviewOnly(Storage(cfg))), host="127.0.0.1", port=cfg.web_port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    print(f"[dashboard] {url}  (Ctrl+C to stop; closes itself when the game starts the live engineer)")
    if not a.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        while not engine_lock.held_by_other():
            time.sleep(2)
        print("[dashboard] live engineer started: handing over the dashboard")
    except KeyboardInterrupt:
        pass
    server.should_exit = True
    time.sleep(1)
    os._exit(0)


def cmd_import(a) -> None:
    """Analyse a recorded .f1cap session into the lap database (so the dashboard can show it)."""
    from pathlib import Path
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.llm.agent import MockProvider
    from f1engineer.storage import Storage
    from f1engineer.telemetry.capture import read_capture
    cfg = Config()
    cfg.speech = False
    store = Storage(cfg)
    before = len(store.list_laps())
    eng = Engine(cfg, storage=store, provider=MockProvider())
    for cap in a.captures or [str(p) for p in sorted((cfg.data_dir / "captures").glob("*.f1cap"))[:1]]:
        print(f"[import] {cap}")
        for _, d in read_capture(Path(cap)):
            eng.feed(d)
        eng.drain()
    print(f"[import] {len(store.list_laps()) - before} laps added. Open: uv run f1engineer dashboard")


def cmd_watch(a) -> None:
    from f1engineer import watcher
    if a.detach:
        watcher.start_detached()
        print("watcher started in the background")
    else:
        watcher.main()


def cmd_autostart(a) -> None:
    from f1engineer import watcher
    print(watcher.autostart(a.state == "on"))
    if a.state == "on":
        watcher.start_detached()
        print("watcher started now too")


def cmd_udp(a) -> None:
    from f1engineer import gameconfig as G
    from f1engineer.config import Config
    cfgs = G.find_configs()
    if not cfgs:
        print("No F1 hardware config found under Documents/My Games. Enable UDP in-game: Settings > Telemetry.")
        return
    for p in cfgs:
        if a.action == "enable":
            print(p.parent.parent.name, "->", G.enable_udp(p, Config().udp_port))
        elif a.action == "restore":
            print(p.parent.parent.name, "->", G.restore(p))
        else:
            print(p.parent.parent.name, G.read_udp(p))


def cmd_replay(a) -> None:
    import subprocess
    subprocess.run([sys.executable, "tools/replay.py", a.capture, "--udp", "--speed", str(a.speed)])


def cmd_record(a) -> None:
    import subprocess
    subprocess.run([sys.executable, "tools/record_udp.py", "--host", "127.0.0.1"])


def cmd_setup(a) -> None:
    from f1engineer.onboarding import main as onboard
    onboard()


def cmd_doctor(a) -> None:
    from f1engineer.onboarding import doctor
    doctor()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="f1engineer", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-browser", action="store_true")
    r.add_argument("--no-speech", action="store_true")
    r.set_defaults(fn=cmd_run)
    w = sub.add_parser("watch")
    w.add_argument("--detach", action="store_true")
    w.set_defaults(fn=cmd_watch)
    s = sub.add_parser("autostart")
    s.add_argument("state", choices=["on", "off"])
    s.set_defaults(fn=cmd_autostart)
    u = sub.add_parser("udp")
    u.add_argument("action", nargs="?", default="show", choices=["show", "enable", "restore"])
    u.set_defaults(fn=cmd_udp)
    p = sub.add_parser("replay")
    p.add_argument("capture")
    p.add_argument("--speed", type=float, default=1.0)
    p.set_defaults(fn=cmd_replay)
    d = sub.add_parser("dashboard")
    d.add_argument("--no-browser", action="store_true")
    d.set_defaults(fn=cmd_dashboard)
    i = sub.add_parser("import")
    i.add_argument("captures", nargs="*", help=".f1cap files (default: the first one in data/captures)")
    i.set_defaults(fn=cmd_import)
    sub.add_parser("record").set_defaults(fn=cmd_record)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
