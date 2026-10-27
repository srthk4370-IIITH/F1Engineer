# F1 Engineer — Overnight Implementation Plan

A local, low-latency **teaching race engineer** for F1 2020 (designed for 2020–2025). It reads the game's UDP telemetry, analyses every lap corner by corner, tells you where you lost or gained time and why, suggests setup changes, runs race strategy, and talks with you over push-to-talk.

Decisions and context live in `HANDBOOK.md` §2. This file is the build plan.

---

## 0. Answers to the questions in prompt.txt (feasibility refactor)

| Original idea | Verdict | How |
|---|---|---|
| Tied to F1 game; opens only when the game opens | ✅ | A tiny watcher (≈15 MB RAM, ~0% CPU) registered at Windows logon polls for `F1_20xx*.exe` / `F1_2x*.exe` every 3 s. It starts the engineer when the game starts and stops it shortly after the game closes. |
| Works with any F1 version? | ✅ (with per-version parsers) | Every Codemasters/EA F1 game since 2018 sends UDP telemetry. Each packet header carries `packetFormat` (2020, 2021, …, 2025), so we dispatch to a version parser and normalise into one internal model. Analysis code never sees version differences. Consoles work too, over LAN. |
| Detect the mode you're playing | ✅ | Session packet `sessionType` (P1–P3, Q1–Q3, short Q, OSQ, Race, Time Trial) plus track, weather, and formula. |
| Time Trial setup advice | ✅ advice, ❌ auto-apply | The CarSetups packet gives your current setup (read-only). A rules engine maps measured symptoms (entry/mid/exit under/oversteer, lockups, wheelspin, tyre temps) to setup changes. You apply them in the garage; the game can't receive setups. |
| Data on other drivers | ✅ | Offline: all 22 cars' speed, throttle, brake, gear, position, lap/sector times and tyres. Online: others' detailed telemetry may be restricted by *their* setting. In TT we compare against your PB and (if present in the packets) the rival ghost. The real capture will confirm this. |
| Conversation (Whisper) | ✅ | Push-to-talk → local faster-whisper or whisper.cpp → OpenAI with tool-calling over live session data → streaming Piper TTS. |
| Local or API logic? | Hybrid by layer | **Numbers are always local deterministic code** (fast, exact, free). **Language is OpenAI** (your choice); a mock is used tonight. LLMs are never trusted to do the maths. |
| OpenCV for input? | ❌ not needed | UDP gives exact data at 60 Hz for <3% of one CPU core. Screen capture + OCR would cost 10–30% CPU, compete for your GPU, and be less accurate. |
| Telemetry, tyre temps, lap times, records | ✅ | Static app code: SQLite for laps/sessions plus compressed per-lap traces. No LLM involved. |
| RAG over the session | ✅, done right | Live session facts are **structured retrieval via tool calls** (`get_lap_comparison`, `get_tyres`, `get_gaps`, …), which is exact and fast. A rolling ~1 KB session summary is always in the prompt. Vector-style RAG is used only for static knowledge (technique and setup guides, track notes), as a small local index. |
| Fast answers | ✅ | Streaming LLM → sentence chunker → streaming TTS, so speech starts on the first sentence. Prompt prefix kept stable (OpenAI prompt caching). Mid-lap radio calls use **templates (0 ms LLM)**. The LLM is used for debriefs and Q&A. |
| Distribution | ✅ | `git clone` → `setup.ps1` (installs uv if missing) → `uv run f1engineer setup` (onboarding: keys + how to get them, model downloads, mic test, enable UDP with backup, register auto-start). Models download to `models/` (gitignored). |

**Latency targets**

| Path | Target |
|---|---|
| Packet → state update | < 2 ms |
| Lap complete → debrief insight computed | < 150 ms |
| Lap complete → first spoken word (template/LLM) | < 0.5 s / < 1.5 s |
| PTT release → first spoken word (Q&A) | < 2.0 s (STT ≤ 0.6 s, LLM first sentence ≤ 1.0 s, TTS first chunk ≤ 0.2 s) |
| Engine CPU while driving | < 5% of a core on average, no game FPS impact (process priority below normal; audio threads normal) |

---

## 1. Architecture

```
 F1 game ──UDP 60Hz──► [Ingest thread] ─► version parser ─► normalised events
                             │                                   │
                             └─► raw recorder (.f1cap)           ▼
                                                        [State store]  (session, cars, current lap trace by distance)
                                                                 │
                         ┌───────────────────────────────────────┼─────────────────────────┐
                         ▼                                       ▼                         ▼
               [Analysis engine]                         [Strategy engine]         [Persistence: SQLite + npz]
     corners, deltas, mistakes, input-device,      tyres, fuel, ERS, gaps, weather,     retention = last 2 sessions
     balance (under/oversteer), assists, setup     pit loss, undercut, SC windows        + per-track PB reference lap
                         └──────────────► Insights (priority, timing class, TTL, dedupe) ◄┘
                                                         │
                                                  [Radio scheduler]  speak only on straights / SC-VSC / garage / post-lap
                                                         │
          PTT (UDP buttons | hotkey | XInput) ─► [STT worker] ─► [LLM agent + tools] ─► sentence stream
                                                         │                                  │
                                                  template lines ──────────────► [TTS worker: Piper] ─► audio out
                                                         │
                                          [FastAPI + WebSocket dashboard on localhost]
```

**Process layout (protects the ingest path from the GIL and ML stalls):**
- **Core process:** UDP ingest thread (blocking `recvfrom`, 4 MB socket buffer), state, analysis, strategy, scheduler, FastAPI.
- **Speech process:** STT + TTS + audio I/O, talking to the core over `multiprocessing` queues. A Whisper or Piper stall can never drop telemetry.
- **Watcher process:** tiny, launches and stops the other two.

**Stack:** Python 3.12 via `uv`; `numpy` (structured dtypes for 22-car arrays); stdlib `struct`; `fastapi` + `uvicorn` + websockets; `sqlite3`; `sounddevice`; `faster-whisper` (CTranslate2) or `pywhispercpp`, chosen by benchmark; `piper-tts` (onnxruntime); `pynvml` for GPU cooperation; `psutil`; `openai`. Frontend is vanilla JS + **uPlot** (very fast trace plots), no build step.

**Package layout**
```
f1engineer/
  telemetry/  packets_2020.py, packets_2021+.py, dispatch.py, model.py (normalised), receiver.py, capture.py (record/replay)
  state/      session.py, lap_trace.py (resample by lap distance, 2 m grid), flashback handling
  analysis/   corners.py (auto-segmentation + track tables), compare.py (delta by distance), mistakes.py,
              balance.py (under/oversteer), input_detect.py, assists.py, setup_advisor.py, consistency.py
  strategy/   tyres.py (deg model), fuel_ers.py, gaps.py, pit.py, weather.py, safety_car.py
  insights/   model.py, generators.py, scheduler.py (straight detection, SC, garage, post-lap), templates.py
  llm/        provider.py (interface), openai_provider.py, mock_provider.py, agent.py (tool loop, streaming), tools.py, prompts.py, knowledge/ (md files + index)
  speech/     stt.py, tts.py, audio.py, ptt.py (UDP buttons, hotkey, XInput via ctypes), radio_fx.py, worker.py
  storage/    db.py, retention.py
  web/        app.py, static/ (index.html, live.js, laps.js, uPlot)
  gpu.py      NVML watcher: use GPU only when free, fall back to CPU on pressure
  watcher.py, main.py, config.py, cli.py
sim/          forge.py (synthetic F1 2020 packet generator), tracks/ (centerlines from real capture), scenarios/
tools/        record_udp.py, replay.py, bench_*.py
tests/
```

---

## 2. Overnight phases (each ends with tests passing and a git commit; HANDBOOK log updated)

### Phase 1: Telemetry foundation (highest priority)
1. `uv init` project, pinned deps, `pyproject.toml`, `ruff`, `pytest`.
2. F1 2020 parsers for all 10 packet types (precompiled `struct.Struct` and numpy dtypes). Assert packet sizes against the spec **and** the real capture.
3. Normalised model (`CarFrame`, `LapState`, `SessionInfo`, `SetupInfo`, `StatusInfo`, `Event`) shared by all versions.
4. Receiver thread plus `.f1cap` record/replay (replay at 1×, N×, or as fast as possible).
5. **Validate on the real capture:** every packet parses, values look sane (speeds, lap times match the game, lap count), and I write down what TT actually sends (rival ghost? other car slots?).
6. Edge cases: flashbacks (frameIdentifier/sessionTime goes backwards → truncate trace), restarts, sessionUID change, out-lap and in-lap, invalidated laps, pause, packet loss or reordering.

### Phase 2: Simulator / data forge (needed for everything else)
- Build the track centreline from the real capture's Motion world positions. Build a speed profile from the real lap.
- `sim/forge.py` emits spec-exact F1 2020 packets at 60 Hz (or faster than real time). It controls the player plus 19 AI cars, Session (incl. SC/VSC, weather), LapData, Events (fastest lap, penalties, pit), CarStatus (tyre wear, fuel, ERS, assists), CarSetups, and buttons.
- Scenario knobs: brake 15 m early at T4, lock-up at T1, wide exit with wheelspin at T10, keyboard vs controller input signature, understeer-heavy setup, tyre deg + pit stop + SC race, flashback mid-lap.
- Golden-scenario tests check that the analysis finds exactly the injected mistakes.

### Phase 3: Lap analysis engine (the core of the "teaching engineer")
- Resample each lap on a lap-distance grid (2 m). Delta-time trace vs reference (PB lap, rival/fastest car, previous lap).
- Corner segmentation: speed minima + steering/yaw from the reference lap, merged with a static corner table per 2020 track (names/numbers) where available.
- Per corner: brake point, peak brake, trail-brake release, min speed (apex), throttle pickup point, exit speed at +100 m, gear at apex, **time lost/gained (corner-attributed)**.
- Mistakes: lock-ups (wheel slip from Motion), wheelspin, off-track/surface type, coasting, early or late braking, over-slowing, late throttle, missed upshift, lap invalid reason.
- Balance: understeer/oversteer by phase (entry/mid/exit) and speed class (slow/medium/fast), using yaw rate vs steering × speed and front/rear slip.
- Input detection: keyboard signature (binary throttle/brake, quantised steering ramps) vs analog. Fused with an XInput poll on Windows. Hysteresis; re-evaluated every lap.
- Assists from CarStatus (TC, ABS, racing line, gearbox, pit/ERS assist). Coaching adapts (e.g. ABS on, so don't talk about lock-ups; suggest the next assist to remove when consistency is good).
- Consistency score per corner across laps → "what you did right" and "what to fix first" (fix the biggest *repeatable* loss first).

### Phase 4: Insights + radio scheduler
- Insight = {topic, facts, priority, timing_class: immediate/straight/debrief/garage, ttl, dedupe_key}.
- Straight detection: the next braking point is ≥ N metres away (from the reference lap), throttle ≈ 100%, steering ≈ 0, and the estimated speech duration fits in the time remaining. Never talk in braking zones or mid-corner.
- TT: the lap-end debrief starts immediately (top 2 losses + 1 positive), split into chunks that fit the straights. The full debrief plays in the garage.
- Race: gaps ahead/behind, tyre state and deg, fuel delta, ERS, weather changes, pit window, undercut threat, SC/VSC "box now?" calls, penalties. Delivered on straights or under SC/VSC.
- Cooldowns, priority pre-emption (safety-critical first), max talk density.

### Phase 5: Speech pipeline (local, low latency)
- Benchmark on this machine: faster-whisper `base.en`/`small.en` (int8 CPU, float16 GPU) vs whisper.cpp. Pick the fastest that is accurate on F1 vocabulary (initial prompt with track names, "box", "softs", "undercut", "delta"…). Keep the model warm.
- GPU cooperation: NVML checks free VRAM. Use the GPU only if ≥ ~1.5 GB free and no foreign process is growing. On pressure, unload and fall back to CPU within ~1 s.
- Piper TTS: pick a clear voice (e.g. en_GB), stream sentence by sentence, optional radio filter (band-pass + click). Pre-synthesise common phrases ("Copy", "Box box", corner numbers) for zero-latency templates.
- PTT: UDP `buttonStatus` (game buttons, works for pad and wheel), global keyboard hotkey, and XInput. Record while held, transcribe on release. Barge-in: pressing PTT stops current speech.
- Benchmarks: PTT → text, text → first audio, full round trip (mock LLM tonight).

### Phase 6: LLM agent (mock tonight, OpenAI tomorrow)
- Provider interface (`stream_chat(messages, tools)`). `MockProvider` is deterministic, emits scripted tool calls and streamed text. `OpenAIProvider` is ready to plug in with a model name in config, streaming, and parallel tool calls.
- Tools: `get_session_overview`, `get_lap_summary(lap)`, `compare_laps(a, b)`, `get_corner_analysis(corner)`, `get_tyres`, `get_fuel_ers`, `get_gaps`, `get_weather`, `get_strategy_options`, `get_setup_and_advice`, `search_knowledge(query)`.
- System prompt: calm, concise pro engineer. Radio answers ≤ 2 sentences unless asked "explain". Always grounded in tool results, never invents numbers.
- Knowledge pack (markdown): braking/trail-braking, racing line, keyboard and controller technique, F1 2020 setup parameter effects, tyre and ERS basics, per-track notes. Small local index for retrieval.
- Tomorrow with the key: measure real latency, tune prompts, choose the model.

### Phase 7: Persistence + dashboard
- SQLite: sessions, laps, sectors, corner metrics, insights, chat. Traces as compressed npz per lap.
- Retention: keep the **last 2 sessions** in full, plus one PB reference lap per track × car (a few hundred KB, needed for "where am I losing" across sessions). Older data is auto-pruned.
- Dashboard (localhost): **Live** (delta, current vs reference traces, tyres/temps, fuel/ERS, radio log), **Laps** (pick two laps, overlay speed/throttle/brake/gear/steer, corner table with losses), **Session report** (debrief, setup suggestions, consistency), **Chat** (text Q&A, same agent). Opens in the browser; optional desktop window later.

### Phase 8: Game linkage + multi-version
- Watcher (auto-start at logon via a Startup shortcut or Task Scheduler) → launches and stops the engine with the game.
- UDP config checker/fixer for F1 2020+ (backup first, validate XML, only while the game is closed).
- Parsers for F1 22/23/24/25 if time allows (public specs), with tests from forged packets.

### Phase 9: End-to-end soak + hardening
- 30–60 min forged race at 60 Hz plus real-capture replay loops: CPU/RAM/latency profile, no leaks, no dropped packets, correct debriefs.
- Fix → re-test loop. Document results in the HANDBOOK.

### Phase 10 (last): Onboarding + distribution
- `setup.ps1` → installs uv if missing → `uv sync`.
- `uv run f1engineer setup` wizard: detect games and enable UDP (with backup), ask for `OPENAI_API_KEY` with step-by-step instructions (platform.openai.com → API keys; billing note), download the Whisper + Piper models, mic/speaker test, PTT binding, auto-start option.
- README with quickstart, troubleshooting, and how to restore the game config.

---

## 3. Risks and mitigations
| Risk | Mitigation |
|---|---|
| Wrong byte offsets in a parser | Size asserts, real capture validation, sanity ranges on values |
| Flashbacks/restarts corrupt traces | Detect time going backwards and truncate; lap restart resets the trace |
| Talking during braking | Straight detection from the reference lap, speech-duration estimate, hard ban in braking zones |
| ML stalls dropping packets | Separate speech process; ingest thread only parses and enqueues |
| GPU fight with the other process | NVML watcher, CPU fallback, unload on pressure |
| Windows firewall prompt | Default bind 127.0.0.1; LAN/console mode is opt-in |
| Piper/whisper Windows packaging issues | Benchmark both STT options; Piper pip wheel, else official binary; fallback Windows SAPI voice |
| Global hotkey not firing in fullscreen | UDP game-button PTT as primary; XInput as second |
| Disk space | Models ≤ ~600 MB total; check free space before downloads; keep ≥ 5 GB free |
| No real OpenAI tonight | Mock provider with identical interface; prompts/tools unit-tested |

## 4. Autonomy rules for overnight
- No questions to the user. Choose, log the decision and why in the HANDBOOK, and move on.
- Never touch `Desktop\qidk` or anything it uses. Never delete non-project files except temp caches if disk drops below 5 GB.
- Don't run the game. Edit the game config only via the guarded checker (backup exists).
- Commit after each phase (and at meaningful sub-steps). Local git only.
