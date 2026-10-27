"""Find F1 game hardware configs and enable UDP telemetry safely.

Safety: only when the game is NOT running, one-time backup (*.f1engineer.bak), only the <udp> element's
attributes are touched via exact string substitution, and the result must still parse as XML.
"""
from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from f1engineer.watcher import game_running

UDP_RE = re.compile(r"<udp\s[^>]*?/>")


def find_configs() -> list[Path]:
    docs = Path.home() / "Documents" / "My Games"
    if not docs.exists():
        return []
    return sorted(p for p in docs.glob("F1*/hardwaresettings/hardware_settings_config.xml"))


def read_udp(path: Path) -> dict | None:
    m = UDP_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return dict(re.findall(r'(\w+)="([^"]*)"', m.group(0))) if m else None


def enable_udp(path: Path, port: int = 20777, rate: int = 60) -> str:
    if game_running():
        return "game is running: close it first (the game rewrites this file on exit)"
    text = path.read_text(encoding="utf-8")
    m = UDP_RE.search(text)
    if not m:
        return "no <udp> element in this config (newer games: enable UDP telemetry in the in-game Telemetry Settings)"
    old = m.group(0)
    new = old
    for attr, val in (("enabled", "true"), ("port", str(port)), ("ip", "127.0.0.1")):
        new = re.sub(rf'{attr}="[^"]*"', f'{attr}="{val}"', new)
    cur_rate = re.search(r'sendRate="(\d+)"', new)
    if cur_rate and int(cur_rate.group(1)) < rate:
        new = re.sub(r'sendRate="\d+"', f'sendRate="{rate}"', new)
    if new == old:
        return "already enabled"
    backup = path.with_name(path.name + ".f1engineer.bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    out = text.replace(old, new, 1)
    ET.fromstring(out.encode("utf-8"))  # must still be valid XML or we don't write
    path.write_text(out, encoding="utf-8")
    return f"enabled (backup: {backup.name})"


def restore(path: Path) -> str:
    backup = path.with_name(path.name + ".f1engineer.bak")
    if not backup.exists():
        return "no backup found"
    if game_running():
        return "game is running: close it first"
    shutil.copy2(backup, path)
    return "restored original config"
