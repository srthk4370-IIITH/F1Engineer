"""Windows push-to-talk key detection that keeps working while a game has focus.

Three independent sources; any one is enough (games differ in how they grab the keyboard):
  - Raw Input with RIDEV_INPUTSINK: the OS sends us keyboard input even when another window is in front
  - GetAsyncKeyState polling
  - (pynput low-level hook, set up by the caller)
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes as W

WM_INPUT, RID_INPUT, RIM_TYPEKEYBOARD, RIDEV_INPUTSINK, RI_KEY_BREAK = 0x00FF, 0x10000003, 1, 0x00000100, 1


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", W.USHORT), ("usUsage", W.USHORT), ("dwFlags", W.DWORD), ("hwndTarget", W.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", W.DWORD), ("dwSize", W.DWORD), ("hDevice", W.HANDLE), ("wParam", W.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", W.USHORT), ("Flags", W.USHORT), ("Reserved", W.USHORT), ("VKey", W.USHORT),
                ("Message", W.UINT), ("ExtraInformation", W.ULONG)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", W.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", ctypes.c_void_p), ("hbrBackground", W.HBRUSH),
                ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]


def raw_input_listener(vk: int, on_change, log) -> None:
    """Background thread: a hidden window registered for keyboard Raw Input in sink mode."""
    def run():
        try:
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            user32.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
            user32.DefWindowProcW.restype = ctypes.c_ssize_t
            user32.GetRawInputData.argtypes = [W.HANDLE, W.UINT, ctypes.c_void_p, ctypes.POINTER(W.UINT), W.UINT]
            user32.CreateWindowExW.restype = W.HWND
            user32.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, W.HWND, W.HMENU, W.HINSTANCE, W.LPVOID]
            state = {"down": False}
            hdr = ctypes.sizeof(RAWINPUTHEADER)

            def proc(hwnd, msg, wp, lp):
                if msg == WM_INPUT:
                    size = W.UINT(0)
                    user32.GetRawInputData(lp, RID_INPUT, None, ctypes.byref(size), hdr)
                    buf = ctypes.create_string_buffer(size.value)
                    if size.value and user32.GetRawInputData(lp, RID_INPUT, buf, ctypes.byref(size), hdr) == size.value:
                        ri = ctypes.cast(buf, ctypes.POINTER(RAWINPUT)).contents
                        if ri.header.dwType == RIM_TYPEKEYBOARD and ri.keyboard.VKey == vk:
                            down = not (ri.keyboard.Flags & RI_KEY_BREAK)
                            if down != state["down"]:
                                state["down"] = down
                                on_change(down)
                return user32.DefWindowProcW(hwnd, msg, wp, lp)

            wndproc = WNDPROC(proc)   # keep a reference for the thread's lifetime
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=hinst, lpszClassName="F1EngineerPTT")
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(0, "F1EngineerPTT", "F1 Engineer PTT", 0, 0, 0, 0, 0, None, None, hinst, None)
            rid = RAWINPUTDEVICE(1, 6, RIDEV_INPUTSINK, hwnd)   # generic desktop / keyboard, even when not in front
            if not user32.RegisterRawInputDevices(ctypes.byref(rid), 1, ctypes.sizeof(rid)):
                log(f"raw input registration failed ({ctypes.GetLastError()})")
                return
            msg = W.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log(f"raw input listener failed: {e!r}")
    threading.Thread(target=run, name="ptt-rawinput", daemon=True).start()


def poll_listener(vk: int, on_change) -> None:
    def run():
        user32, last = ctypes.windll.user32, False
        while True:
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down != last:
                on_change(down)
                last = down
            time.sleep(0.015)
    threading.Thread(target=run, name="ptt-poll", daemon=True).start()
