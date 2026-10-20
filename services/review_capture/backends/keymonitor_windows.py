"""Windows global key backend: the 1.0 RegisterHotKey/GetAsyncKeyState behaviour.

This module is imported only on Windows (or by tests that install a fake
``ctypes.windll``), and keeps the original calls and constants unchanged.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

HOTKEYS = {"F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A}
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
HOTKEY_ID = 0xAEC8

PREMERE_EXE = "adobe premiere pro.exe"
CEP_ENGINE_EXE = "cephtmlengine.exe"


def premiere_in_foreground() -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return False
    try:
        path = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(path))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
            return False
        executable = Path(path.value)
        if executable.name.casefold() == PREMERE_EXE:
            return True
        # A floating CEP panel has its own foreground process, although it is
        # part of the active Premiere installation.
        return (executable.name.casefold() == CEP_ENGINE_EXE and
                (executable.parent.parent / "Adobe Premiere Pro.exe").is_file())
    finally:
        kernel32.CloseHandle(handle)


class WindowsKeyMonitor:
    backend = "windows-hotkey"

    def __init__(self, key: str = "F8", *, register_hotkey: bool = True):
        self.key = key if key in HOTKEYS else "F8"
        self.registered_key: str | None = None
        self.register_hotkey = register_hotkey

    # -- arming ---------------------------------------------------------
    def set_key(self, key: str) -> None:
        self.key = key if key in HOTKEYS else "F8"

    def set_armed(self, armed: bool) -> None:
        if not armed and self.registered_key:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered_key = None

    @property
    def armed(self) -> bool:
        return self.registered_key is not None

    def register(self, key: str) -> bool:
        """Register the hotkey when nothing holds it yet; True if it is ours.

        With ``register_hotkey=False`` no global registration is attempted (the
        key state is observed only); useful when another component owns the key
        or when a test diagnoses key delivery.
        """
        if not self.register_hotkey:
            return True
        if self.registered_key is None:
            if ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, 0x4000, HOTKEYS[key]):
                self.registered_key = key
                return True
            return False
        return self.registered_key == key

    def unregister(self) -> None:
        if self.registered_key:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered_key = None

    def frontmost(self) -> bool:
        return premiere_in_foreground()

    # -- polling --------------------------------------------------------
    def drain_messages(self, key: str) -> bool:
        """Consume WM_HOTKEY messages; True if one belongs to this key."""
        hotkey_message = False
        message = wintypes.MSG()
        while ctypes.windll.user32.PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            if self.registered_key == key and message.wParam == HOTKEY_ID:
                hotkey_message = True
        return hotkey_message

    def key_state(self, key: str) -> int:
        return ctypes.windll.user32.GetAsyncKeyState(HOTKEYS[key])

    def poll(self, key: str) -> tuple[bool, bool, bool]:
        hotkey_message = self.drain_messages(key)
        key_state = self.key_state(key)
        down = bool(key_state & 0x8000)
        pressed_since_poll = bool(key_state & 0x0001)
        return down, pressed_since_poll, hotkey_message

    def poll_for(self, key: str, foreground: bool) -> tuple[bool, bool, bool]:
        """1.0 used the key state as "still holding, Premiere in front".

        Windows' ``GetAsyncKeyState`` only reflects the foreground queue, so
        when Premiere is not the active application the key reads as released
        and the take ends. Say that explicitly (instead of relying on that
        side effect) so both platforms share one stop condition.
        """
        down, pressed, message = self.poll(key)
        return (down and foreground), pressed, message

    def close(self) -> None:
        self.unregister()
