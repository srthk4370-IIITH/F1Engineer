"""First-run wizard (`f1engineer setup`) and health check (`f1engineer doctor`)."""
from __future__ import annotations

import getpass
import shutil
import socket
import time
from pathlib import Path

from f1engineer.config import ROOT, Config

ENV = ROOT / ".env"
VOICES = {"1": ("en_GB-alan-medium", "British male (default)"), "2": ("en_GB-jenny_dioco-medium", "British female"),
          "3": ("en_US-ryan-medium", "American male"), "4": ("en_US-amy-medium", "American female")}


def _ask(prompt: str, default: str = "") -> str:
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def _yes(prompt: str, default: bool = True) -> bool:
    v = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not v else v.startswith("y")


def _read_env() -> dict[str, str]:
    out = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _write_env(vals: dict[str, str]) -> None:
    lines = ["# F1 Engineer settings (this file is gitignored - never commit it)"]
    lines += [f"{k}={v}" for k, v in vals.items() if v != ""]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _check_openai(key: str) -> str | None:
    try:
        from openai import OpenAI
        OpenAI(api_key=key).models.list()
        return None
    except Exception as e:
        return str(e)[:200]


def main() -> None:
    print("""
=====================================================
  F1 ENGINEER - setup
  A local race engineer for Codemasters/EA F1 games.
=====================================================
""")
    cfg = Config()
    env = _read_env()
    free = shutil.disk_usage(ROOT).free / 1e9
    print(f"Disk: {free:.1f} GB free (models need ~0.4 GB; we always keep {cfg.min_free_disk_gb} GB free).")

    # 1. game telemetry
    print("\n[1/6] Game telemetry (UDP)")
    from f1engineer import gameconfig as G
    cfgs = G.find_configs()
    if not cfgs:
        print("  No F1 config found in Documents/My Games. In the game: Game Options > Settings > Telemetry Settings >\n"
              "  UDP Telemetry: On, UDP IP 127.0.0.1, Port 20777, Send Rate 60Hz, UDP Format 2020 (if offered).")
    for p in cfgs:
        game = p.parent.parent.name
        udp = G.read_udp(p)
        print(f"  {game}: {udp}")
        if udp and udp.get("enabled") != "true" and _yes(f"  Enable UDP telemetry for {game}? (a backup is made first)"):
            print("   ", G.enable_udp(p, cfg.udp_port))
        if udp and udp.get("format") not in (None, "2020"):
            print(f"  Note: {game} sends format {udp.get('format')}. This version fully supports format 2020; "
                  "if your game offers 'UDP Format: 2020' in its telemetry settings, pick it.")

    # 2. OpenAI
    print("""
[2/6] OpenAI API key (powers the conversational engineer; lap maths runs locally either way)
  How to get one:
    1. Go to https://platform.openai.com/api-keys and sign in (or create an account).
    2. Click "Create new secret key", give it a name, copy it (starts with "sk-").
    3. Add a few dollars of credit: https://platform.openai.com/settings/organization/billing
       (a typical session costs well under $0.05).
  Press Enter to skip: the engineer then uses its offline template brain (radio calls still work).""")
    cur = env.get("OPENAI_API_KEY", "")
    key = getpass.getpass(f"  Paste key{' (Enter keeps the current one)' if cur else ''}: ").strip() or cur
    if key:
        err = _check_openai(key)
        print("  Key works." if not err else f"  Could not verify the key ({err}). Saved anyway; check it later.")
        env["OPENAI_API_KEY"] = key
        env["OPENAI_MODEL"] = _ask("  Model", env.get("OPENAI_MODEL", "gpt-6-luna"))

    # 3. preferences
    print("\n[3/6] Your engineer")
    env["F1E_SASS"] = _ask("  Sass level 0 (strictly professional) .. 3 (roasts you)", env.get("F1E_SASS", "2"))
    for k, (v, desc) in VOICES.items():
        print(f"    {k}. {desc} ({v})")
    choice = _ask("  Voice", "1")
    env["F1E_TTS_VOICE"] = VOICES.get(choice, VOICES["1"])[0]
    env["F1E_PTT_KEY"] = _ask("  Push-to-talk keyboard key (hold while speaking)", env.get("F1E_PTT_KEY", "t"))
    env["F1E_PTT_BUTTON"] = _ask("  Push-to-talk controller button (A,B,X,Y,LB,RB,LS,RS,VIEW,DPAD_*) - pick one you\n"
                                 "  don't use for driving", env.get("F1E_PTT_BUTTON", "RS"))
    env["F1E_GPU"] = "auto" if _yes("  Use the GPU for speech recognition when it's free?", env.get("F1E_GPU", "auto") == "auto") else "off"
    _write_env(env)
    print(f"  Saved to {ENV.name} (gitignored).")

    # 4. models
    print("\n[4/6] Downloading local speech models (Piper voice + Whisper)")
    cfg = Config()
    from f1engineer.models import ensure_piper, ensure_whisper
    import copy
    ensure_piper(cfg)
    for m in ("tiny.en", "base.en"):
        c = copy.copy(cfg)
        c.stt_model = m
        ensure_whisper(c)
    print("  Models ready.")

    # 5. audio
    print("\n[5/6] Audio check")
    if _yes("  Play a test radio message and record 3 s from your mic?"):
        _audio_test(cfg)

    # 6. autostart
    print("\n[6/6] Start automatically with the game")
    from f1engineer import watcher
    if _yes("  Run a tiny background watcher at Windows logon that opens the engineer when F1 starts?"):
        print("  ", watcher.autostart(True))
        watcher.start_detached()
        print("   Watcher running now.")
    print(f"""
Done. Start an F1 session and the engineer comes on the radio.
  Manual start:   uv run f1engineer run
  Dashboard:      http://127.0.0.1:{cfg.web_port}
  Health check:   uv run f1engineer doctor
  Undo autostart: uv run f1engineer autostart off
  Restore game config: uv run f1engineer udp restore
""")


def _audio_test(cfg) -> None:
    try:
        import numpy as np
        import sounddevice as sd
        from f1engineer.speech import STT, TTS
        tts = TTS(cfg)
        a = tts.synth("Radio check. How do you read me?")
        sd.play(a, tts.sr)
        sd.wait()
        print("  Say something like 'Where am I losing time?' now...")
        rec = sd.rec(int(3 * 16000), samplerate=16000, channels=1, dtype="float32")
        sd.wait()
        t = time.perf_counter()
        txt = STT(cfg).transcribe(rec[:, 0])
        print(f"  Heard: {txt!r} ({(time.perf_counter() - t) * 1000:.0f} ms)")
    except Exception as e:
        print(f"  Audio test failed: {e!r}")


def doctor() -> None:
    cfg = Config()
    ok = lambda b: "OK  " if b else "FAIL"  # noqa: E731
    print("F1 Engineer doctor\n")
    free = shutil.disk_usage(ROOT).free / 1e9
    print(f"[{ok(free > cfg.min_free_disk_gb)}] disk free {free:.1f} GB (floor {cfg.min_free_disk_gb})")
    for mod in ("numpy", "fastapi", "uvicorn", "openai", "faster_whisper", "piper", "sounddevice", "pynput", "psutil"):
        try:
            __import__(mod)
            print(f"[OK  ] import {mod}")
        except Exception as e:
            print(f"[FAIL] import {mod}: {e!r}")
    from f1engineer.models import piper_paths
    print(f"[{ok(all(p.exists() for p in piper_paths(cfg)))}] piper voice {cfg.tts_voice}")
    for m in ("tiny.en", "base.en"):
        print(f"[{ok((cfg.models_dir / 'whisper' / m / 'model.bin').exists())}] whisper {m}")
    env = _read_env()
    print(f"[{ok(bool(env.get('OPENAI_API_KEY')))}] OPENAI_API_KEY in .env (optional; offline brain otherwise)")
    from f1engineer import gameconfig as G
    for p in G.find_configs():
        u = G.read_udp(p) or {}
        print(f"[{ok(u.get('enabled') == 'true')}] {p.parent.parent.name} UDP {u}")
    for port, proto in ((cfg.udp_port, socket.SOCK_DGRAM), (cfg.web_port, socket.SOCK_STREAM)):
        s = socket.socket(socket.AF_INET, proto)
        try:
            s.bind(("127.0.0.1", port))
            print(f"[OK  ] port {port} free")
        except OSError:
            print(f"[INFO] port {port} in use (fine if the engineer is running)")
        finally:
            s.close()
    from f1engineer.watcher import STARTUP_NAME, startup_dir
    print(f"[{ok(Path(startup_dir(), STARTUP_NAME).exists())}] autostart shortcut")
