# F1 Engineer

A local, low-latency **teaching race engineer** for the Codemasters/EA F1 games (built and tested on **F1 2020**).
It reads the game's UDP telemetry, analyses every lap corner by corner against your best lap or the Time Trial ghost,
tells you on the radio where you lost time and *why* (braked 12 m early, apex speed 18 km/h down, lock-up, wheelspin,
coasting, late throttle...), suggests setup changes, runs race strategy (gaps, tyre wear, fuel, ERS, safety car, weather)
and lets you talk to it with push-to-talk.

- **Radio that never talks mid-corner**: messages wait for a straight long enough to say them, or the safety car, garage or pause.
- **Time Trial coaching**: lap debrief right after the line, corner-by-corner losses, positives, setup and technique advice.
- **Race engineer**: position, gaps ahead and behind, tyre wear and cliff prediction, fuel, ERS, SC/VSC pit calls, rain forecast, DRS threats.
- **Knows your controls**: detects keyboard vs controller from the telemetry, reads TC/ABS assists, and adapts the advice.
- **Dashboard** at `http://127.0.0.1:8020`: live delta, tyres, last-lap corner table, lap browser with speed, throttle,
  brake, gear and steering overlays, track map showing where you lose time, and chat.
- **Sass**: adjustable from professional (0) to roasting (3). "Be a man, brake later."

Everything runs locally except the optional OpenAI chat brain. Speech-to-text (Whisper) and text-to-speech (Piper) are local models.

## Quick start (Windows)

```powershell
git clone <this repo>
cd F1Engineer
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` installs [uv](https://docs.astral.sh/uv/) if needed and the dependencies, then runs the setup wizard. The wizard:
1. turns on the game's UDP telemetry (after backing up the config),
2. asks for an **OpenAI API key** (optional) and explains how to get one,
3. sets the sass level, voice and push-to-talk key or button,
4. downloads the local speech models (~0.4 GB),
5. tests your speakers and microphone,
6. optionally starts a tiny watcher at logon so the engineer opens **only when the F1 game is running**.

NVIDIA GPU? `uv sync --extra gpu` adds CUDA libraries (~2 GB) and speech recognition drops from ~0.6 s to ~0.06 s.
The engineer only uses the GPU while it has free memory, and moves to the CPU if another program needs it.

## Commands

| | |
|---|---|
| `uv run f1engineer run` | start now (telemetry, radio, dashboard) |
| `uv run f1engineer autostart on\|off` | start/stop launching with the game |
| `uv run f1engineer udp [show\|enable\|restore]` | game telemetry config (restore = put your original back) |
| `uv run f1engineer doctor` | health check |
| `uv run f1engineer record` | record raw telemetry to `data/captures` |
| `uv run python tools/replay.py <capture>` | replay a capture through the engineer offline |
| `uv run python sim/forge.py --udp --speed 2` | stream a synthetic 20-car race (needs a real capture to build from) |

## In the game
- F1 2020: Game Options > Settings > Telemetry Settings > UDP Telemetry **On**, IP 127.0.0.1, port 20777, 60 Hz, format 2020.
  (`f1engineer udp enable` does this for you.)
- Review laps any time (no game needed): `uv run f1engineer dashboard` -> F1-style telemetry (speed, throttle, brake, gear, RPM, steering, G, slip), lap A vs B comparison with delta trace, mini-sectors, corner table and track map. Import a recording with `uv run f1engineer import`.
- Push-to-talk: hold **Shift** (keyboard, either side) or the configured controller button (default: right-stick click), speak, release.
- Ask things like *"Where am I losing time?"*, *"What happened at the Loop?"*, *"What setup should I change?"*,
  *"How are my tyres?"*, *"Should I pit?"*, *"Explain trail braking."*

## How it works
```
F1 game --UDP 60Hz--> ingest (numpy, ~30 us/packet) --> session tracker (laps, flashbacks, restarts, TT ghosts)
   --> lap analysis (2 m distance grid, corner detection, delta, brake/apex/throttle/exit, lock-ups, wheelspin, off-track)
   --> insights --> radio scheduler (straights / SC / garage only) --> Piper TTS (separate process)
PTT --> Whisper STT --> LLM agent with tools over live session data (OpenAI, or offline templates) --> streamed speech
```
- All numbers come from deterministic local code. The LLM only phrases answers and must call tools for every number.
- Time Trial ghosts send no lap data, so their laps are rebuilt by projecting their positions onto a track map learned from your first lap.
- Guardrails: below-normal CPU priority, memory cap with auto-restart, keeps ≥5.5 GB disk free, data capped at 2 GB
  (full telemetry for the last 5 sessions, 90 days of lap history, newest 2 raw recordings (max 30 days), 30 days of radio log, a personal-best reference per track; data/ hard cap 1 GB), rotating logs, localhost-only network, single instance.

## Supported games
F1 2020 (packet format 2020) is fully supported and tested on real telemetry. F1 2021 can also send the 2020 format
(Telemetry Settings > UDP Format: 2020). Each game only sends its own format and the previous year's, so F1 22 and later
need native parsers. The ingest dispatches on `packetFormat` and everything downstream runs on a normalised model,
so adding a version only means adding a parser.

## Privacy and keys
`.env` holds your keys and is gitignored. Telemetry, recordings and models stay in `data/` and `models/`, both gitignored.
