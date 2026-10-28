"""Download/locate local speech models into models/ (gitignored). Checks free disk first."""
from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

from f1engineer.config import Config

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
WHISPER_REPOS = {"tiny.en": "Systran/faster-whisper-tiny.en", "base.en": "Systran/faster-whisper-base.en",
                 "small.en": "Systran/faster-whisper-small.en"}


def piper_paths(cfg: Config) -> tuple[Path, Path]:
    d = cfg.models_dir / "piper"
    return d / f"{cfg.tts_voice}.onnx", d / f"{cfg.tts_voice}.onnx.json"


def _guard(cfg: Config, need_gb: float) -> None:
    free = shutil.disk_usage(cfg.models_dir.parent).free / 1e9
    if free - need_gb < cfg.min_free_disk_gb:
        raise RuntimeError(f"Not enough disk: {free:.1f} GB free, need {need_gb} GB + {cfg.min_free_disk_gb} GB reserve")


def ensure_piper(cfg: Config) -> tuple[Path, Path]:
    onnx, js = piper_paths(cfg)
    if onnx.exists() and js.exists():
        return onnx, js
    _guard(cfg, 0.15)
    onnx.parent.mkdir(parents=True, exist_ok=True)
    lang_full, name, quality = cfg.tts_voice.split("-")
    lang = lang_full.split("_")[0]
    for path, suffix in ((onnx, ".onnx"), (js, ".onnx.json")):
        url = f"{PIPER_BASE}/{lang}/{lang_full}/{name}/{quality}/{cfg.tts_voice}{suffix}"
        print(f"downloading {url}")
        tmp = path.with_suffix(path.suffix + ".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)
    return onnx, js


def ensure_whisper(cfg: Config) -> Path:
    d = cfg.models_dir / "whisper" / cfg.stt_model
    if (d / "model.bin").exists():
        return d
    _guard(cfg, 0.6)
    from huggingface_hub import snapshot_download
    snapshot_download(WHISPER_REPOS.get(cfg.stt_model, cfg.stt_model), local_dir=d,
                      allow_patterns=["*.bin", "*.json", "*.txt", "vocabulary*"])
    return d


if __name__ == "__main__":
    c = Config()
    print(ensure_piper(c))
    import copy
    for m in ("base.en", "small.en"):
        cc = copy.copy(c)
        cc.stt_model = m
        print(ensure_whisper(cc))
