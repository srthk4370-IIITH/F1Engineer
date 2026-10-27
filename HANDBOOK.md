# F1 Engineer — HANDBOOK (working memory / log)

Everything I (Claude) learn, decide, or change is logged here. Newest log entries go at the bottom of **Log**.
If context is lost, read this file first, then `PLAN.md`.

---

## 1. Environment facts (verified 2026-10-28)

| Item | Value |
|---|---|
| OS | Windows 11 Home 10.0.26200 |
| CPU | i5-13420H, 8C/12T |
| RAM | 15.6 GB |
| GPU | RTX 4050 Laptop, 6 GB VRAM. ~2.7 GB already used by another process. **Use GPU only if free; release immediately when another program needs it.** |
| Disk C: | 32 GB free at start. **Keep >= 5 GB free.** Never touch anything related to `Desktop\qidk`. |
| Python | via `uv` (`C:\Users\hp\.local\bin\uv.exe`); CPython 3.12.13 managed by uv. `python` on PATH is the MS Store stub, so don't use it. Use `uv run`. |
| Other tools | git, node present. No ollama, no cmake. |
| Game | F1 2020 at `C:\Games\F1 2020\F1_2020.exe` (+ `F1_2020_dx12.exe`). Desktop shortcuts in `Desktop\Games`. |
| Game config | `%USERPROFILE%\Documents\My Games\F1 2020\hardwaresettings\hardware_settings_config.xml` |
| Config backup | same folder, `hardware_settings_config.xml.f1engineer.bak` (original, UDP disabled, 20 Hz) |
| UDP setting now | `enabled="true" broadcast="false" ip="127.0.0.1" port="20777" sendRate="60" format="2020" yourTelemetry="restricted"` |

**Restore the game config:** copy the `.bak` over `hardware_settings_config.xml` while the game is closed.

## 2. User decisions (Q&A session, 2026-10-28)

| Topic | Decision |
|---|---|
| LLM brain | **OpenAI only** for language/chat. All maths stays deterministic local code. Key arrives tomorrow, so **mock provider tonight**. |
| Voice out | **Piper local TTS** |
| Talk trigger | **Push-to-talk** (controller/wheel button via UDP buttonStatus + keyboard hotkey) |
| Input device | Keyboard mostly, sometimes controller. **Auto-detect** using the telemetry signature plus Windows XInput/joystick polling, re-evaluated every lap. Show the result on the dashboard. |
| Radio timing | TT: debrief may start right after the lap ends even while driving, on straights, or in the garage. Race: much more info (strategy, tyres, fuel, ERS, gaps, weather), delivered on straights or under SC/VSC. |
| UI | App or local web dashboard to review telemetry & laps later. **Retain max last 2 sessions** of full data. (My choice: also keep a tiny per-track personal-best reference lap; see PLAN.) |
| Modes | Time Trial, Grand Prix/Career races, Practice/Quali. (Online not a priority.) |
| Game config edit | Allowed. **Must not break the game**: backup, minimal edit, XML validated, only while the game is closed. |
| GitHub | Local git only for now. |
| Personality | Calm, concise pro (real radio style, numbers-first, explains the WHY in debriefs). |
| Skill/assists | Detect from telemetry (CarStatus assists fields). |
| Versions | F1 2020 fully, plus a multi-version design (parsers keyed by packetFormat; 22–25 if time allows). |
| GPU | Use if free, yield as soon as another program needs it. |
| Real capture | User recording real laps on 2026-10-28 before leaving. |
| Autonomy | When the user says they're leaving: no questions, make the best call, log it here. |

## 3. Project rules (from CLAUDE.md, condensed)
- Personal/recreational project, shareable. Users bring their own API keys; any models must be **local**.
- Proper `.gitignore`; secrets in `.env` only.
- Onboarding (asks for keys + explains how to get them) is built **last**.
- Focus: **low latency** and a solid pipeline. Loop: try → find problems → research → fix → repeat.
- Forging data is allowed (simulate what the real run provides).
- Cooperate with the other GPU/RAM process.
- Never delete permanent stuff that isn't mine. Keep git history.

## 4. Key file locations
- `tools/record_udp.py`: stdlib raw-packet recorder → `.f1cap` (magic `F1CAP001`, records `<f64 t><u16 len><bytes>`).
- `data/captures/`: raw captures (gitignored). Real capture: `data/captures/real_f1_2020_session1.f1cap`.

## 5. Known facts / research notes
- F1 2020 UDP: header 24 bytes `<HBBBBQfIBB>` (packetFormat, gameMajor, gameMinor, packetVersion, packetId, sessionUID, sessionTime, frameIdentifier, playerCarIndex, secondaryPlayerCarIndex). 22 cars.
- 2020 packet ids 0–9: Motion, Session, LapData, Event, Participants, CarSetups, CarTelemetry, CarStatus, FinalClassification, LobbyInfo.
- Expected 2020 sizes (to verify against the real capture): Motion 1464, Session 251, LapData 1190, Event 35, Participants 1213, CarSetups 1102, CarTelemetry 1307, CarStatus 1344, FinalClassification 839, LobbyInfo 1169.
- `yourTelemetry="restricted"` only affects what *other online players* see of you. Offline AI telemetry is fully available.

## 6. Log
- **2026-10-28 ~02:40** Read prompt.txt and surveyed the machine. Found F1 2020 install and UDP config (disabled).
- **02:43** Asked the user 16 clarification questions; answers recorded in §2.
- **02:44** Backed up the game config and enabled UDP (60 Hz). Verified XML parses and the game was not running.
- **02:44** `git init`, `.gitignore` (secrets, data/, models/, venv, caches).
- **02:45** Wrote `tools/record_udp.py`. Started recording on 127.0.0.1:20777 (background).
- **02:46** User: OpenAI key tomorrow, so focus on the pipeline tonight with a mock LLM.
- Wrote PLAN.md + HANDBOOK.md.
- **02:49–02:57** Real capture recorded: `data/captures/real_f1_2020_session1.f1cap` (112 MB, 84,782 packets, format 2020, ~54 Hz per main type). Gaps (game paused/menus) ~02:51:30–02:52:30 and after 02:57:40. Game closed by 03:00; recorder stopped.
- **User's notes on the capture (ground truth for tests):**
  - Track: **Silverstone**, Time Trial.
  - Laps 1–2 on **keyboard**. Lap 1: a few mistakes. Lap 2: an obvious mistake at **the Loop** (T3/Village–Loop complex).
  - Then switched to **controller with different assists**. Went **off track, restarted the lap** (flashback/restart handling!).
  - Next controller lap: lots of mistakes, **lock-ups** and **missed the line**.
- **Personality update (user):** calm, concise pro **but a bit sassy**, with banter like "be a man", "my nan brakes later than that". Implement as a configurable `sass` level (0–3, default 2) in the prompts and template lines. Never sassy in safety-critical or strategy-critical calls.
- **03:00** User left. Autonomous mode from here.
- PTT default key: `T` (no answer from user, so I'm deciding). PB reference lap per track kept beyond the 2-session retention (my decision, tiny).

---

## 7. Verified facts from the real capture (2026-10-28)
- All 84,782 packets parse at exactly the spec sizes (2020). Game sends ~54 Hz per main packet at sendRate 60.
- **Time Trial participants:** idx0 "Player", idx1 "Default Ghost", idx2 "Personal Best". Ghosts send **CarTelemetry + Motion but NO LapData** (lapNum 0).
  We rebuild ghost laps by projecting world x/z onto a track map (5 m grid) learned from the first clean player lap. Accuracy ~0.07 s.
- **TT ghosts reset when the player crosses the line**, so a ghost "lap" is cut short. We extrapolate the last ≤200 m from speed.
- Default Ghost parks after its first lap (game behaviour).
- **Keyboard signature:** steer only {-1, 0, +1} (3 unique values), throttle/brake 0/1. Controller: hundreds of values. Detection = >97% digital.
- **Keyboard keys appear in buttonStatus** (throttle→RT 0x1000, steering→DPAD L/R, etc.), so game-button PTT must use a non-driving button (default RS = right-stick click).
- Assists in CarStatus: keyboard laps TC=2 ABS=1; controller laps TC 0/1, ABS 0/1 (matches user).
- **Yaw rate sign is opposite to frontWheelsAngle** (corr −0.9). Understeer metric = |δ| − |r|·L/v, baseline ~0.05 rad for this car, so balance is reported *relative* by corner-speed class.
- **Wheel slip:** ABS-on braking peaks ~0.09; real lock-ups saturate at 1.0 → lock-up = front slip > 0.2 for ≥3 samples while braking. Wheelspin = rear > 0.12 for ≥8 samples on throttle.
- Surfaces seen: 0 tarmac, 1 rumble, 4 gravel, 7 grass. Off-track = ≥2 wheels on {3..8} for ≥6 grid points.
- Silverstone (trackId 7, length 5891 m) corners by apex distance: Abbey/Farm 480, Village 900, **the Loop 1040 (slowest, 78 km/h)**, Brooklands 1980, Luffield 2060 / exit 2175, Copse 3160, Maggotts/Becketts 3880, Chapel 4080, Stowe 5050, Vale 5530. Table in `f1engineer/data/tracks.json`.
- User's laps: L1 90.427 (kb, invalid), L2 89.098 (kb, invalid, PB), L3 94.598 (controller, lock-up at Luffield, big losses Village/Vale/Luffield exit). The lap-1 out-lap restart and the lap-3 restarts are detected as "lap restarted".
- The user's machine also has an F1 22 config (format 2022, UDP disabled) but **F1 22 is not installed**. Left untouched.

## 8. Performance (measured)
| Metric | Value |
|---|---|
| Ingest | ~30 µs/packet → **0.8% of one core** at ~270 pkt/s (was 14% before lazy CarStatus conversion) |
| Lap analysis | 3–10 ms per lap |
| Full-session replay | 160× real time |
| Piper TTS | 130–400 ms per sentence (CPU) |
| Whisper GPU (base.en fp16) | ~60 ms per command; small.en ~175 ms |
| Whisper CPU (tiny.en int8) | ~570–940 ms; base.en ~1.2 s |
| PTT release → first audio (CPU STT, mock LLM) | ~1.16 s (GPU STT: ~0.3 s + LLM) |
| Speech worker startup | ~5 s |

## 9. Gotchas
- `uv add/remove/sync` **without `--extra gpu` deletes the CUDA libs** (2 GB). Always run `uv sync --extra gpu` on this machine.
- `uv run` blocks while another uv process holds the venv lock; use `.venv/Scripts/python.exe` directly in that case.
- faster-whisper CUDA needs cuBLAS 12 + cuDNN 9 DLLs: pip `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` + `os.add_dll_directory` (done in `speech._register_cuda_dlls`).
- numpy `void.tolist()` leaves sub-array fields as ndarrays; convert them (done in `_rec_to_dict`).
- `_Tee` (log redirect) needs `isatty()`, or uvicorn crashes silently in its thread.
- Default OpenAI model `gpt-4.1-mini` (overridable via `OPENAI_MODEL`). **Verify the model name and latency when the key arrives.**

## 10. Status / what's built
See git log. Modules: telemetry (2020 parser, capture IO), state/tracker (laps, flashbacks, restarts, ghosts), analysis (lapmath, coach, setup_advisor, strategy, trackmap, ghost), radio (templates + sass + scheduler), llm (agent, Mock/OpenAI providers, knowledge), speech (Piper, Whisper GPU/CPU, PTT key/XInput/UDP, VRAM watchdog), storage (SQLite + npz, retention), web (FastAPI + WS + uPlot dashboard), guard (priority, memory/disk watchdogs, lock, rotating logs), watcher (game detection, autostart), gameconfig (safe UDP enable/restore), onboarding (setup wizard, doctor), sim/forge (synthetic 20-car race), tools (record, replay, bench_latency), tests (9).
