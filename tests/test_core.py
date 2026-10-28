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
    s = radio.RadioScheduler(lambda t, p: said.append(t))
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
    s = radio.RadioScheduler(lambda t, p: said.append(t))
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
    e.speech.say = lambda text, prio=5: said.append(text)
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
    assert "No reference yet" in text                      # lap 1 gets a debrief, not just "banker"
    assert "Next lap, focus on" in text
    assert re.search(r"Sector (one|two)", text)
    assert "Lap 1:29.098" in text and "Lap 1:34.598" in text  # lap summaries are not crowded out
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
        for _, d in read_capture(REAL):
            for b in (enc.encode(d) if enc else [d]):
                e.feed(b)
        e.drain()
        return e
    a, b = run(2020), run(2024)
    key = lambda e: [(round(r.lap_time, 3), r.input_device, r.mistakes, [c["corner"] for c in r.losses]) for r in e.reports]  # noqa: E731
    assert key(a) == key(b) and b.game == "F1 24" and b.stats["parse_errors"] == 0
