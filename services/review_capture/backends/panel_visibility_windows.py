"""Windows-only CEP panel visibility repair (1.0 behaviour, moved unchanged)."""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from datetime import datetime, timezone


PANEL_TITLE = "Cuthere · 剪这儿"
HOST_CLASS = "WC_PLUGPLUG_HTMLEXTENSION_CLASS_NAME"
GUID_CLASS = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I)
SW_SHOWNA = 8


def reveal_panel() -> dict[str, object]:
    user32 = ctypes.windll.user32
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
    user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowExW.restype = wintypes.HWND
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    def class_name(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def title(hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(128)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def pid(hwnd: int) -> int:
        result = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(result))
        return result.value

    found: list[dict[str, object]] = []
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback
    def on_top(hwnd: int, _unused: int) -> bool:
        if class_name(hwnd) != "Premiere Pro":
            return True

        @callback
        def on_child(child: int, _unused_child: int) -> bool:
            if class_name(child) != HOST_CLASS or title(child) != PANEL_TITLE:
                return True
            previous = wintypes.HWND()
            while True:
                wrapper = user32.FindWindowExW(child, previous, None, None)
                if not wrapper:
                    break
                previous = wrapper
                if pid(wrapper) != pid(child) or not GUID_CLASS.fullmatch(class_name(wrapper)):
                    continue
                before = bool(user32.IsWindowVisible(wrapper))
                if not before:
                    user32.ShowWindow(wrapper, SW_SHOWNA)
                found.append({
                    "premiere_pid": pid(hwnd),
                    "host_hwnd": int(child),
                    "wrapper_hwnd": int(wrapper),
                    "was_visible": before,
                    "is_visible": bool(user32.IsWindowVisible(wrapper)),
                })
            return True

        user32.EnumChildWindows(hwnd, on_child, 0)
        return True

    user32.EnumWindows(on_top, 0)
    return {"at": datetime.now(timezone.utc).isoformat(), "platform": "win32",
            "supported": True, "panels": found}
