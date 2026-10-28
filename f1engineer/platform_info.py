"""What this machine can do, so features adapt instead of failing (Windows / Linux / macOS)."""
from __future__ import annotations

import os
import platform
import sys


def os_name() -> str:
    return {"win32": "Windows", "linux": "Linux", "darwin": "macOS"}.get(sys.platform, sys.platform)


def capabilities() -> dict:
    """Feature switches by OS. The engineer runs everywhere; only some conveniences are platform specific."""
    win, linux, mac = sys.platform == "win32", sys.platform.startswith("linux"), sys.platform == "darwin"
    wayland = linux and os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    return {
        "os": os_name(),
        "os_version": platform.release(),
        # pynput global hotkeys: Windows yes, X11 yes, Wayland no, macOS after granting Accessibility permission
        "keyboard_ptt": win or (linux and not wayland) or mac,
        "keyboard_ptt_note": ("Wayland blocks global hotkeys: use a controller button (sent by the game) instead"
                              if wayland else "macOS: allow your terminal in System Settings > Privacy > Accessibility"
                              if mac else ""),
        "xinput_pad": win,              # direct controller polling; elsewhere the game's own button telemetry is used
        "game_button_ptt": True,        # the F1 game reports controller buttons in its telemetry on every platform
        "autostart": win or linux or mac,
        "game_process_watch": win or linux,   # Linux: Proton/Wine games show their .exe name
        "cuda": win or linux,
        "local_game": win or linux,     # macOS: play on console/another PC and point its UDP at this machine
    }
