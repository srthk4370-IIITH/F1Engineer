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
