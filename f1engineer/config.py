"""Settings from environment / .env (see .env.example). Everything has a safe default."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def _env(name: str, default, cast=str):
    v = os.getenv(name)
    if v is None or v == "":
        return default
    if cast is bool:
        return v.strip().lower() in ("1", "true", "yes", "on")
    return cast(v)


@dataclass
class Config:
    udp_host: str = field(default_factory=lambda: _env("F1E_UDP_HOST", "127.0.0.1"))
    udp_port: int = field(default_factory=lambda: _env("F1E_UDP_PORT", 20777, int))
    web_port: int = field(default_factory=lambda: _env("F1E_WEB_PORT", 8020, int))
    sass: int = field(default_factory=lambda: _env("F1E_SASS", 2, int))
    ptt_key: str = field(default_factory=lambda: _env("F1E_PTT_KEY", "t"))
    ptt_game_button: str = field(default_factory=lambda: _env("F1E_PTT_BUTTON", "RS"))  # see packets_2020.BUTTONS
    stt_model: str = field(default_factory=lambda: _env("F1E_STT_MODEL", "base.en"))
    tts_voice: str = field(default_factory=lambda: _env("F1E_TTS_VOICE", "en_GB-alan-medium"))
    gpu: str = field(default_factory=lambda: _env("F1E_GPU", "auto"))  # auto | off
    speech: bool = field(default_factory=lambda: _env("F1E_SPEECH", True, bool))
    record_raw: bool = field(default_factory=lambda: _env("F1E_RECORD_RAW", False, bool))
    # guardrails
    min_free_disk_gb: float = field(default_factory=lambda: _env("F1E_MIN_FREE_GB", 5.5, float))
    max_data_gb: float = field(default_factory=lambda: _env("F1E_MAX_DATA_GB", 2.0, float))
    max_rss_mb: int = field(default_factory=lambda: _env("F1E_MAX_RSS_MB", 1500, int))
    keep_sessions: int = field(default_factory=lambda: _env("F1E_KEEP_SESSIONS", 2, int))
    data_dir: Path = ROOT / "data"
    models_dir: Path = ROOT / "models"
    logs_dir: Path = ROOT / "logs"
