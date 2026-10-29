"""Core tests. Real-capture tests run only when the capture exists locally (it is gitignored)."""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from f1engineer import radio
from f1engineer.analysis import coach, lapmath as LM
from f1engineer.telemetry import packets_2020 as P

REAL = Path("data/captures/real_f1_2020_session1.f1cap")


def test_packet_sizes_match_spec():
    for pid, (_, dt) in P.PACKETS.items():
        assert dt.itemsize == P.EXPECTED_SIZES[pid]


def test_parse_roundtrip():
    rec = np.zeros((), P.LAP_DATA)
    rec["header"]["packetFormat"], rec["header"]["packetId"] = 2020, 2
    rec["lapData"][0]["lapDistance"] = 1234.5
    pid, p = P.parse(rec.tobytes())
    assert pid == 2 and p["lapData"][0]["lapDistance"] == pytest.approx(1234.5)
    assert P.parse(b"\x00" * 10) is None


def test_speech_number_formatting():
    assert radio.fmt_d(0.57) == "six tenths"
    assert radio.fmt_d(0.04) == "four hundredths"
    assert radio.fmt_d(1.33) == "1.3 seconds"
    assert radio.fmt_t(89.098) == "1:29.098"


def test_scheduler_waits_for_straight():
    said = []
    s = radio.RadioScheduler(lambda t, p, m=None: said.append(t))
    s.push([radio.Message(1, radio.CLOCK(), "Lap one twenty nine, two tenths down on your best.", "straight", 60, "k")])
    s.tick({"throttle": 1.0, "steer": 0.0, "brake": 0.0, "time_to_brake": 1.0})  # too short
    assert not said
    s.tick({"throttle": 0.3, "steer": 0.5, "brake": 0.8, "time_to_brake": 9.0})  # cornering
    assert not said
    s.tick({"throttle": 1.0, "steer": 0.05, "brake": 0.0, "time_to_brake": 9.0})  # long straight
    assert said


def test_input_detection():
    n = 2000
    kb = {"speed": np.full(n, 150.0), "steer": np.random.choice([-1.0, 0.0, 1.0], n), "throttle": np.random.choice([0.0, 1.0], n)}
    pad = {"speed": np.full(n, 150.0), "steer": np.random.uniform(-1, 1, n), "throttle": np.random.uniform(0, 1, n)}
    assert coach.detect_input_device(kb) == "keyboard"
    assert coach.detect_input_device(pad) == "controller/wheel"


def _synthetic_lap(brake_early_at=None):
    d = np.arange(0, 3000, 2.0)
    speed = np.full_like(d, 300.0)
    for apex in (800, 1800, 2600):
        speed -= 200 * np.exp(-((d - apex) / 80) ** 2)
    brake = np.zeros_like(d)
    throttle = np.ones_like(d)
    for apex in (800, 1800, 2600):
        start = apex - 120 - (40 if apex == brake_early_at else 0)
        brake[(d > start) & (d < apex - 20)] = 1.0
        throttle[(d > start) & (d < apex + 40)] = 0.0
    t = np.concatenate([[0], np.cumsum(np.diff(d) / (speed[1:] / 3.6))])
    if brake_early_at:
        t[d > brake_early_at - 160] += np.minimum(0.3, (d[d > brake_early_at - 160] - (brake_early_at - 160)) / 400)
    return {"dist": d, "t": t, "speed": speed, "brake": brake, "throttle": throttle, "gear": np.full_like(d, 5.0),
            "steer": np.zeros_like(d)}


def test_corner_analysis_finds_injected_mistake():
    ref = _synthetic_lap()
    lap = _synthetic_lap(brake_early_at=1800)
    corners = LM.detect_corners(ref)
    assert [round(c.apex, -2) for c in corners] == [800, 1800, 2600]
    m = LM.corner_metrics(lap, ref, LM.delta_trace(lap, ref), corners)
    worst = max(m, key=lambda x: x.time_loss)
    assert worst.corner.apex == pytest.approx(1800, abs=10)
    assert worst.brake_point - worst.ref_brake_point == pytest.approx(40, abs=6)


def test_gameconfig_enable_is_safe(tmp_path, monkeypatch):
    from f1engineer import gameconfig
    monkeypatch.setattr(gameconfig, "game_running", lambda: None)
    f = tmp_path / "hardware_settings_config.xml"
    f.write_text('<?xml version="1.0"?>\n<cfg><motion><udp enabled="false" broadcast="false" ip="127.0.0.1" '
                 'port="20777" sendRate="20" format="2020" /></motion></cfg>', encoding="utf-8")
    assert "enabled" in gameconfig.enable_udp(f)
    assert gameconfig.read_udp(f)["enabled"] == "true" and gameconfig.read_udp(f)["sendRate"] == "60"
    assert (tmp_path / "hardware_settings_config.xml.f1engineer.bak").exists()
    assert gameconfig.enable_udp(f) == "already enabled"
    monkeypatch.setattr(gameconfig, "game_running", lambda: "F1_2020.exe")
    assert "close it" in gameconfig.restore(f)


def test_storage_retention_keeps_last_two_sessions():
    from f1engineer.config import Config
    from f1engineer.state.tracker import Lap
    from f1engineer.storage import Storage
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp())
    cfg.keep_sessions = 2
    st = Storage(cfg)
    for uid in (1, 2, 3):
        st.start_session(uid, "Silverstone", "Time Trial", 2020)
        lap = Lap(0, 1)
        lap.append({"dist": 1.0, "t": 0.1})
        lap.lap_time = 90.0
        st.save_lap(uid, lap.finalize(), "Player", None, "keyboard")
        st.db.execute("UPDATE sessions SET started=? WHERE uid=?", (uid, str(uid)))
    st.retention()
    assert sorted({row["session"] for row in st.list_laps()}) == ["2", "3"]


@pytest.mark.skipif(not REAL.exists(), reason="real capture not present")
def test_real_capture_end_to_end():
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.storage import Storage
    from f1engineer.telemetry.capture import read_capture
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp())
    e = Engine(cfg, storage=Storage(cfg))
    e.speech.say = lambda *a: None
    from f1engineer import radio
    clock = [0.0]
    old, radio.CLOCK = radio.CLOCK, lambda: clock[0]   # run on capture time, as in a live session
    try:
        for t, d in read_capture(REAL):
            clock[0] = t
            e.feed(d)
    finally:
        radio.CLOCK = old
    e.drain()
    assert e.tr.track == "Silverstone" or e.reports
    assert [round(r.lap_time, 3) for r in e.reports] == [89.098, 94.598]
    assert [r.input_device for r in e.reports] == ["keyboard", "controller/wheel"]
    assert any("Luffield" in m for m in e.reports[1].mistakes)
    assert e.stats["parse_errors"] == 0
    assert "Village" in [c["corner"] for c in e.reports[1].losses]
    ans = e.ask("Where am I losing time?")
    assert "Village" in ans


def test_garage_debrief_waits_until_parked():
    said = []
    s = radio.RadioScheduler(lambda t, p, m=None: said.append(t))
    s.push([radio.Message(6, radio.CLOCK(), "Long debrief " * 40, "garage", 900, "g")])
    s.tick({"throttle": 1.0, "steer": 0.0, "brake": 0.0, "time_to_brake": 30.0})
    assert not said
    s.tick({"paused": True})
    assert said


@pytest.mark.skipif(not REAL.exists(), reason="real capture not present")
def test_live_coaching_on_real_capture():
    """Proactive calls on the real session: first-lap self review, in-lap corner calls, no absurd deltas."""
    import re
    from f1engineer import radio
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.storage import Storage
    from f1engineer.telemetry.capture import read_capture
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp())
    e = Engine(cfg, storage=Storage(cfg))
    said = []
    e.speech.say = lambda text, *a, **k: said.append(text)
    clock = [0.0]
    old, radio.CLOCK = radio.CLOCK, lambda: clock[0]
    try:
        for n, (t, d) in enumerate(read_capture(REAL)):
            clock[0] = t
            e.feed(d, wall=t)
            if n % 24 == 0:
                e.sched.tick(e.ctx())
                e.coach.tick()
    finally:
        radio.CLOCK = old
    text = " | ".join(said)
    assert "baseline" in text or "benchmark" in text        # lap 1 gets a debrief, not just "banker"
    assert "Saw " in text and "Next lap, " in text            # what went wrong + what to do
    assert "1:34.6" in text                                    # lap summaries are not crowded out
    assert "Give me a clean lap" not in text and "reconnaissance" not in text
    assert not re.search(r"\b\d{2,}\.\d seconds", text)     # no nonsense deltas around the line


def test_brain_strategy_and_profile(tmp_path):
    from f1engineer import brain
    r = brain.evaluate_strategy([{"lap": 4, "compound": "hard"}], total_laps=10, base_lap=89.5, pit_loss=20.5)
    assert r["your_plan"]["race_time_s"] > 0 and r["best_alternatives"]
    assert all(not any("illegal" in w for w in b["warnings"]) for b in r["best_alternatives"])
    one = brain.evaluate_strategy([{"lap": 4, "compound": "soft"}], total_laps=10)
    assert any("two" in w for w in one["your_plan"]["warnings"])        # single dry compound flagged
    p = brain.DriverProfile(tmp_path / "profile.json")
    p.remember("Goal: 1:27 flat at Silverstone")
    assert "1:27" in brain.DriverProfile(tmp_path / "profile.json").summary()


@pytest.mark.skipif(not REAL.exists(), reason="real capture not present")
def test_brain_progress_and_style_from_real_session():
    from f1engineer import brain
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.storage import Storage
    from f1engineer.telemetry.capture import read_capture
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp())
    st = Storage(cfg)
    e = Engine(cfg, storage=st)
    e.speech.say = lambda *a: None
    for _, d in read_capture(REAL):
        e.feed(d)
    e.drain()
    prog = brain.progress(st, "Silverstone")
    assert prog["sessions"] and prog["sessions"][-1]["best"] < 91
    style = brain.driving_style(st)
    assert style["style_summary"] and "keyboard" in style["input_devices"]
    ans = " ".join(e.agent.ask("Am I improving?", mode="garage"))   # offline brain routes to get_progress
    assert "session" in ans.lower() or "improv" in ans.lower()


def test_prune_history_limits(tmp_path):
    import os
    import time as _t
    from f1engineer.config import Config
    from f1engineer.storage import Storage
    cfg = Config()
    cfg.data_dir = tmp_path
    st = Storage(cfg)
    now = _t.time()
    for i, age_days in enumerate((1, 50, 120)):           # 120-day-old summary must go (90-day history)
        st.db.execute("INSERT INTO lap_summary(ts,track,session_uid,lap_num,lap_time) VALUES(?,?,?,?,?)",
                      (now - age_days * 86400, "Silverstone", "s", i, 90.0))
    st.db.execute("INSERT INTO radio(session_uid,t,role,text) VALUES('s',?, 'engineer', 'old')", (now - 40 * 86400,))
    st.db.commit()
    caps = tmp_path / "captures"
    caps.mkdir()
    for i, age_days in enumerate((0.1, 2, 3, 45)):         # keep newest 2; 45-day-old one is over the age limit anyway
        p = caps / f"c{i}.f1cap"
        p.write_bytes(b"x")
        os.utime(p, (now - age_days * 86400, now - age_days * 86400))
    out = st.prune_history()
    assert out["lap_summaries"] == 1 and out["radio_lines"] == 1
    assert sorted(p.name for p in caps.glob("*.f1cap")) == ["c0.f1cap", "c1.f1cap"]


def test_format_layouts_match_official_sizes():
    from f1engineer.telemetry import formats as F
    for fmt, sizes in F.OFFICIAL.items():
        lay = F._layout(fmt)
        for pid, size in sizes.items():
            assert lay[pid][0].itemsize == size, (fmt, pid)


@pytest.mark.skipif(not REAL.exists(), reason="real capture not present")
def test_real_session_reencoded_as_f1_24_gives_same_laps():
    """Inverse-encode the real F1 2020 session as F1 24 packets: the engineer must see exactly the same laps."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from encode_format import Encoder
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.storage import Storage
    from f1engineer.telemetry.capture import read_capture

    def run(fmt):
        cfg = Config()
        cfg.data_dir = Path(tempfile.mkdtemp())
        e = Engine(cfg, storage=Storage(cfg))
        e.speech.say = lambda *a: None
        enc = Encoder(fmt) if fmt != 2020 else None
        from f1engineer import radio
        clock = [0.0]
        old, radio.CLOCK = radio.CLOCK, lambda: clock[0]   # capture time: results must not depend on machine speed
        try:
            for t, d in read_capture(REAL):
                clock[0] = t
                for b in (enc.encode(d) if enc else [d]):
                    e.feed(b)
        finally:
            radio.CLOCK = old
        e.drain()
        return e
    a, b = run(2020), run(2024)
    key = lambda e: [(round(r.lap_time, 3), r.input_device, r.mistakes, [c["corner"] for c in r.losses]) for r in e.reports]  # noqa: E731
    assert key(a) == key(b) and b.game == "F1 24" and b.stats["parse_errors"] == 0


def test_pipeline_answers_in_order_and_dedupes():
    """Three quick questions: generated in parallel, spoken strictly in the order asked; a double press is merged."""
    import threading
    import time as _t
    from f1engineer.config import Config
    from f1engineer.engine import Engine
    from f1engineer.storage import Storage
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp())
    e = Engine(cfg, storage=Storage(cfg))
    said = []
    e.speech.say = lambda text, *a, **k: said.append(text)

    class SlowFirst:   # the first question is the slowest to answer
        def stream(self, messages, tools, **_):
            q = messages[-1]["content"]
            _t.sleep(0.6 if q.startswith("one") else 0.05)
            yield {"type": "text", "text": f"Answer to {q.split()[0]}. "}
    e.agent.provider = SlowFirst()
    qs = ["one question here", "two question here", "two question here", "three question here"]
    th = [threading.Thread(target=e.ask, args=(q,)) for q in qs]
    for x in th:
        x.start()
        _t.sleep(0.02)
    for x in th:
        x.join(10)
    answers = [s for s in said if "nswer to" in s]      # later ones may open with context ("On what you asked: ...")
    assert [a.split("nswer to ")[1] for a in answers] == ["one.", "two.", "three."]


def test_quick_answers_route_data_questions_without_ai():
    from f1engineer import quick
    assert quick.REASONING.search("why am i slow in stowe") and quick.REASONING.search("should i pit now")
    route = lambda q: next((n for n, rx in quick.INTENTS if rx.search(q.lower())), None)  # noqa: E731
    assert route("What are my tyre temps?") == "temps" and route("Brake temps?") == "brakes"
    assert route("Gap ahead?") == "gap_ahead" and route("How many laps left?") == "laps"
    assert route("Radio check") == "radio_check" and route("Who am I racing?") == "racing"


def test_patterns_one_off_quiet_recurring_called_fixed_praised_and_tyre_trend():
    from types import SimpleNamespace as NS
    from f1engineer.insight import Patterns
    eng = NS(adapt={"loss_call_s": 0.25}, tr=NS(is_race=False))
    p = Patterns(eng)

    def rep(n, t1_loss, lap_time=90.0):
        return NS(lap_num=n, lap_time=lap_time, valid=True,
                  corners=[{"corner": "Turn 1", "time_loss": t1_loss, "causes": ["braked 15 m earlier"]},
                           {"corner": "Turn 2", "time_loss": 0.0, "causes": []}])
    said = lambda msgs: " ".join(m.text for m in msgs)  # noqa: E731
    assert "Turn 1" not in said(p.on_report(rep(1, 0.4), 10))       # one bad lap: noise
    assert "Turn 1" not in said(p.on_report(rep(2, 0.02), 12))
    p.on_report(rep(3, 0.3), 14)
    p.on_report(rep(4, 0.3), 16)
    out = said(p.on_report(rep(5, 0.35), 18))                          # three laps running: call it, with the cause
    assert "Turn 1" in out and "braked 15 m earlier" in out
    assert "Turn 1" not in said(p.on_report(rep(6, 0.4), 20))          # said once, not nagged
    p.on_report(rep(7, 0.0), 22)
    fixed = said(p.on_report(rep(8, 0.0), 24)).lower()                # two clean laps after the call: praise
    assert any(w in fixed for w in ("sorted", "better", "fixed"))

    q = Patterns(NS(adapt={}, tr=NS(is_race=False)))
    msgs = []
    for n, (t, w) in enumerate([(90.0, 30), (90.2, 35), (90.4, 40), (90.6, 46)], 1):   # slower as tyres wear
        msgs += q.on_report(NS(lap_num=n, lap_time=t, valid=True, corners=[]), w)
    assert "tyre" in said(msgs).lower()                                # blamed on the tyres, not the driver


def test_wake_word_split_and_voice_gate():
    import numpy as np
    from f1engineer import wake as W
    names = W.variants("Bono", ["Hey, Bonnow."])
    assert W.split_wake("Hey Bono. What are my tyre temps?", names) == (True, "What are my tyre temps?")
    assert W.split_wake("Bonnow, should I pit?", names)[0]                      # learned from enrolment
    assert W.split_wake("Bono.", names) == (True, "")
    assert not W.split_wake("What a lovely day out there, bono", names)[0]      # name must lead the sentence
    gate = W.VoiceGate(-38.0)
    quiet, loud = np.full(1600, 1e-4, np.float32), (np.sin(np.arange(1600) / 5) * 0.1).astype(np.float32)
    segs = [s for b in [quiet] * 3 + [loud] * 8 + [quiet] * 8 for s in [gate.feed(b)] if s is not None]
    assert len(segs) == 1 and 0.8 < len(segs[0]) / 16000 < 1.8


def test_every_module_imports():
    """A syntax error anywhere (even in code tests don't exercise, like the setup wizard) must fail the suite."""
    import importlib
    import pkgutil
    import f1engineer
    for m in pkgutil.walk_packages(f1engineer.__path__, "f1engineer."):
        if m.name.endswith("__main__"):
            continue
        importlib.import_module(m.name)


def test_floor_driver_cuts_in_and_nothing_is_lost():
    import threading
    import time as _t
    from f1engineer.floor import Floor
    f = Floor()
    f.put("Box this lap.", "urgent")
    f.put("Turn four cost you a tenth. Brake later there.", "proactive", "p1")
    first = f.next()
    assert first.text == "Box this lap."
    f.done(first)
    cur = f.next()                                   # "Turn four cost you a tenth." starts playing
    f.put("Checking.", "ack")
    cut = f.set_driver(True)                         # driver presses Shift mid-sentence
    assert cut is cur and not f.done(cur)            # reported as cut off
    got = []
    threading.Thread(target=lambda: got.append(f.next()), daemon=True).start()
    _t.sleep(0.4)
    assert not got                                   # nothing plays while the driver has the floor
    f.set_driver(False)
    _t.sleep(0.8)
    assert got and got[0].text == "Checking."        # ack first after the driver
    f.done(got[0])
    resumed = f.next()
    assert resumed.text == "Turn four cost you a tenth." and resumed.resumed   # picked back up, not thrown away
    f.done(resumed)
    assert f.next().text == "Brake later there."


def test_preferences_latest_wins_and_soft_filter(tmp_path):
    from f1engineer import brain, radio
    p = brain.DriverProfile(tmp_path / "p.json")
    p.set_preference("coasting", "don't let me coast")
    r = p.set_preference("coasting", "coasting is fine on keyboard")
    assert r["previous"] == "don't let me coast" and p.data["preferences"]["coasting"]["history"]
    assert "coasting is fine" in brain.DriverProfile(tmp_path / "p.json").summary()
    assert p.preference_for("Saw coasting into Brooklands.")[0] == "coasting"
    from types import SimpleNamespace as NS
    from f1engineer.engine import Engine
    eng = NS(profile=p, pipeline=NS(waiting_long=lambda: False))
    weak = radio.Message(3, 0, "Saw coasting into Brooklands.", evidence=0.05)
    strong = radio.Message(3, 0, "Village, three tenths: coasted 40 m.", evidence=0.3)
    assert Engine._radio_filter(eng, weak) is None                       # their call, small stakes: stay quiet
    assert "village" in Engine._radio_filter(eng, strong).lower()       # costs real time: still said, framed


def test_quick_commands_are_instant():
    from f1engineer import quick
    assert quick.classify("say again") == "repeat" and quick.classify("Stop") == "stop"
    assert quick.classify("go quiet for a bit") == "quiet" and quick.classify("radio on") == "radio_on"
    assert quick.instant("tyre temps?") and not quick.instant("why am I slow in turn 4?")
    assert quick.lead_in("what setup changes should I make").lower().count("setup") == 1


def test_practice_plan_tracks_targets_lap_by_lap():
    from types import SimpleNamespace as NS
    from f1engineer.plan import PracticePlan
    p = PracticePlan("fix Village and get a clean lap", [
        {"metric": "apex_speed", "corner": "Village", "target": 115, "laps": 6},
        {"metric": "clean_lap", "laps": 2}])
    assert "Village" in p.intro()
    rep = lambda v, valid=True, mistakes=(), causes=("braked 12 m earlier",): NS(  # noqa: E731
        lap_time=90.0, valid=valid, mistakes=list(mistakes), delta=0.3, sectors=None,
        corners=[{"corner": "Village", "min_speed": v, "time_loss": 0.1, "causes": list(causes), "lockup": False,
                  "wheelspin": False, "off_track": False}])
    msg = p.on_report(rep(110))
    assert "110" in msg and "target 115" in msg and "braked 12 m earlier" in msg and "later marker" in msg   # why + fix
    msg = p.on_report(rep(111))
    assert "Closer: 110 to 111" in msg and "Let's get 113 first" in msg          # stuck -> halfway target
    msg = p.on_report(rep(113))
    assert "Bar's up" in msg and "115" in msg                                    # done -> stretch once
    msg = p.on_report(rep(116))
    assert "Nailed it" in msg and "clean lap" in msg and p.idx == 1
    p.on_report(rep(116, valid=False))
    msg = p.on_report(rep(116))
    assert p.finished and "Plan complete: 2 of 2" in msg


def test_setup_changes_respect_slider_limits():
    from types import SimpleNamespace as NS
    from f1engineer.analysis import setup_advisor, setup_model
    setup = {"brakeBias": 50, "brakePressure": 100, "frontAntiRollBar": 1, "frontSuspension": 5, "onThrottle": 58}
    rep = NS(corners=[{"corner": "Village", "lockup": True, "wheelspin": False, "off_track": False, "time_loss": 0.2,
                       "causes": []}], balance={"slow": {"tendency": "more understeer"}})
    adv = setup_advisor.advise([rep], setup, "keyboard", {"abs": 1}, 2020)
    changes = [a["change"] for a in adv]
    assert not any(c.startswith("brake bias") for c in changes)        # already at the 50% minimum: not suggested
    assert "brake pressure 100% -> 99%" not in changes and any(c.startswith("brake pressure 100%") for c in changes)
    assert not any(c.startswith("front anti-roll bar") for c in changes)   # at 1: the front suspension is used instead
    assert any(c == "front suspension 5 -> 4" for c in changes)
    assert setup_model.change({"frontWing": 11}, 2020, "frontWing", 1) is None


def test_voice_gate_ends_in_a_noisy_room():
    """Fans at -24 dBFS (above the old fixed 'silence' level of threshold-8) must not hold a segment open to the cap."""
    import numpy as np
    from f1engineer.wake import VoiceGate

    def blk(db):
        return np.full(1600, 10 ** (db / 20), np.float32)
    g = VoiceGate(-19, hang=0.6, max_len=15.0)
    for _ in range(50):                       # 5 s of fan noise before you speak
        assert g.feed(blk(-24)) is None
    for _ in range(8):                        # "Hey Bono"
        assert g.feed(blk(-10)) is None
    seg = None
    for i in range(30):                       # back to fan noise: must end after ~0.6 s, not 15 s
        seg = g.feed(blk(-24))
        if seg is not None:
            break
    assert seg is not None and i <= 7


def test_rival_questions_route_to_data_answers():
    """'Where am I losing time to Verstappen' is a data answer (works with the AI offline), incl. speech slips."""
    from types import SimpleNamespace as NS

    from f1engineer import quick
    from f1engineer.analysis import rival
    assert quick.classify("Where am I losing time to Verstappen?") == "rival"
    assert quick.classify("Race update") == "race_update"
    assert quick.classify("where am I losing time") == "losing"
    lapdata = [{"carPosition": p} for p in (2, 1, 3)] + [{"carPosition": 0}] * 19
    tr = NS(lapdata=lapdata, player_idx=0, participants=[{}] * 3,
            car_name=lambda i: ["Player", "VERSTAPPEN", "BOTTAS"][i])
    eng = NS(tr=tr)
    assert rival.find_car(eng, "where am I losing time towards to happen") == 1
    assert rival.find_car(eng, "how do I compare to Bottas") == 2
    assert rival.find_car(eng, "where am I losing time") == 1          # no name: the car ahead
    c = {"loss_s": 0.3, "min_kmh": [100, 112], "top_kmh": [300, 300], "exit_kmh": [150, 151],
         "brake_m": [800.0, 812.0], "full_throttle_m": [None, None]}
    w = rival.why(c, "VERSTAPPEN")
    assert "Verstappen brakes 12 m later" in w and "12 km/h more" in w


def test_gap_calls_say_the_right_direction():
    """Gap trend calls: gaining/losing on the car ahead, closing/dropping behind - never the wrong way round."""
    from f1engineer.engine import Engine
    base = {"trend_3_sectors_s": None, "rate_per_lap_s": 0.0, "where": []}
    t = Engine._gap_text
    assert t(None, {**base, "side": "ahead", "driver": "Bottas", "gap_s": 1.2, "change_s": -0.3}).startswith("Bottas is pulling away")
    assert t(None, {**base, "side": "ahead", "driver": "Bottas", "gap_s": 2.0, "change_s": 0.3,
                    "rate_per_lap_s": 0.5}).startswith("Gaining on Bottas")
    assert "defend" in t(None, {**base, "side": "behind", "driver": "Russell", "gap_s": 0.7, "change_s": -0.4})
    assert t(None, {**base, "side": "behind", "driver": "Russell", "gap_s": 1.7, "change_s": 0.4}).startswith("Pulling away from")


def test_live_radio_stays_fresh_and_tidy():
    """Newer call on a topic replaces the queued one; stale live calls aren't resumed; names keep their capitals;
    short races don't box; a temperature warning isn't repeated unless it gets worse."""
    from types import SimpleNamespace as NS

    from f1engineer import brain, radio
    from f1engineer.floor import Floor
    from f1engineer.race import RaceEngineer
    f = Floor()
    f.put("Albon is closing, 0.3 that sector.", key="battle_behind")
    f.put("Albon 0.6 behind, in DRS.", key="battle_behind")
    assert [i.text for i in f.items] == ["Albon 0.6 behind, in DRS."]
    it = f.next()
    it.created -= 30
    f.set_driver(True)
    assert f.items == []                       # a 30 s old live call isn't resumed
    assert radio.follow_on("Albon 0.3 behind.") == "Albon 0.3 behind."
    assert radio.follow_on("The Loop, 0.4.") == "the Loop, 0.4."
    best = brain.evaluate_strategy(None, total_laps=5, current_lap=3)["best_alternatives"][0]
    assert best["stops"] == []                 # 5-lap race: never "box next lap"
    long_best = brain.evaluate_strategy(None, total_laps=30, current_lap=3)["best_alternatives"][0]
    assert long_best["stops"]                  # a full race still uses two compounds
    said = []
    eng = NS(sched=NS(push=lambda ms: said.extend(m.text for m in ms)))
    r = RaceEngineer(eng)
    t = [0.0]
    old = radio.CLOCK
    radio.CLOCK = lambda: t[0]
    try:
        for t[0], deg in ((0, 113), (6, 113), (130, 116), (260, 121), (400, 112)):
            r._temp("fronts_hot", True, deg, f"hot {deg}", f"still {deg}")
    finally:
        radio.CLOCK = old
    assert said == ["hot 113", "still 121"]
