"""Reveal Premiere's hidden CEP browser wrapper without changing the project.

On Premiere Pro 25.6.4 the native panel can be visible while the GUID-named
child that owns CefBrowserWindow remains WS_VISIBLE=false. The page and worker
run normally, but the user sees a blank panel. This helper is launched once
by the panel itself and changes only that exact child window.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path


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
    return {"at": datetime.now(timezone.utc).isoformat(), "panels": found}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipc-root", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object] = {"at": datetime.now(timezone.utc).isoformat(), "panels": []}
    for _attempt in range(40):
        result = reveal_panel()
        if result["panels"]:
            break
        time.sleep(0.1)
    args.ipc_root.mkdir(parents=True, exist_ok=True)
    (args.ipc_root / "panel_visibility.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
