"""Windows global key backend: RegisterHotKey plus asynchronous key state.

This module is imported only on Windows (or by tests that patch its native
lookups), and keeps the 1.0 calls and constants for the F-key path unchanged.
What is new is that a binding may be a chord: the whole combination is registered
with ``RegisterHotKey`` (so Premiere does not also receive it, exactly as with
F8 in 1.0) and the held state is the main key *and* the registered modifiers, so
releasing a modifier stops the take instead of leaving a recording running.

Windows cannot express "only the right-hand Alt" through ``RegisterHotKey`` and
has no event for a modifier on its own, so side-qualified and modifier-only
bindings are rejected by ``services.review_capture.shortcut`` before they reach
this backend. That limit is documented rather than emulated.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from ..shortcut import parse as parse_binding

# The 1.0 table, kept verbatim: an existing F8/F9/F10/F11 preference must keep
# exactly the virtual key code it registered before.
HOTKEYS = {"F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A}

# Full virtual-key table for the supported base keys (WinUser.h). F12 is not
# here: Windows reserves it for debuggers and RegisterHotKey must not use it,
# so services.review_capture.shortcut rejects it before this backend is reached.
VK = {
    **{letter: 0x41 + index for index, letter in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
    **{digit: 0x30 + int(digit) for digit in "0123456789"},
    **{f"F{number}": 0x6F + number for number in range(1, 12)},
    "Space": 0x20, "Enter": 0x0D, "Tab": 0x09, "Backspace": 0x08,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23,
    "PageUp": 0x21, "PageDown": 0x22,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
}

# RegisterHotKey modifier flags; MOD_NOREPEAT keeps auto-repeat from retriggering.
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
MOD_MASKS = {"Alt": MOD_ALT, "Control": MOD_CONTROL, "Shift": MOD_SHIFT, "Meta": MOD_WIN}

# Left/right virtual keys, used to observe which modifiers are physically down.
MODIFIER_VKS = {
    "Control": (0xA2, 0xA3),
    "Shift": (0xA0, 0xA1),
    "Alt": (0xA4, 0xA5),
    "Meta": (0x5B, 0x5C),
}

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
HOTKEY_ID = 0xAEC8

PREMERE_EXE = "adobe premiere pro.exe"
CEP_ENGINE_EXE = "cephtmlengine.exe"


def _user32():
    """Indirection so a test can substitute a fake user32 without a real Win32 host."""
    return ctypes.windll.user32


def _kernel32():
    return ctypes.windll.kernel32


def premiere_in_foreground() -> bool:
    if os.name != "nt":
        return False
    user32 = _user32()
    kernel32 = _kernel32()
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
        self._binding = self._parse_key(key)
        self.registered_key: str | None = None
        self.register_hotkey = register_hotkey
        self.observe_only = not register_hotkey
        self.registration_error: str | None = None
        self.registration_attempts = 0

    # -- binding ---------------------------------------------------------
    @staticmethod
    def _parse_key(key):
        try:
            return parse_binding(key, platform="win32")
        except Exception:
            return parse_binding("F8", platform="win32")

    @property
    def key(self) -> str:
        return self._binding.canonical

    @key.setter
    def key(self, value) -> None:
        self.set_key(value)

    @property
    def binding(self):
        return self._binding

    def modifier_mask(self) -> int:
        """RegisterHotKey mask for the configured binding (without MOD_NOREPEAT)."""
        mask = 0
        for name in self._binding.modifiers:
            base = name[4:] if name.startswith("Left") else name[5:] if name.startswith("Right") else name
            mask |= MOD_MASKS.get(base, 0)
        return mask

    def _modifiers_now(self) -> int:
        """Which modifier families are physically down right now."""
        user32 = _user32()
        mask = 0
        for base, codes in MODIFIER_VKS.items():
            if any(user32.GetAsyncKeyState(code) & 0x8000 for code in codes):
                mask |= MOD_MASKS[base]
        return mask

    def modifiers_held(self) -> bool:
        return self._modifiers_now() == self.modifier_mask()

    # -- arming ---------------------------------------------------------
    def set_key(self, key) -> None:
        new_binding = self._parse_key(key)
        if new_binding == self._binding:
            return
        if self.registered_key is not None:
            # A different chord cannot reuse the old registration.
            self.unregister()
        self._binding = new_binding

    def set_armed(self, armed: bool) -> None:
        if not armed and self.registered_key:
            _user32().UnregisterHotKey(None, HOTKEY_ID)
            self.registered_key = None

    @property
    def armed(self) -> bool:
        return self.registered_key is not None

    def register(self, key) -> bool:
        """Register the binding when nothing holds it yet; True if it is ours.

        With ``register_hotkey=False`` no global registration is attempted (the
        key state is observed only); that explicit diagnostic mode is reported
        separately and is never presented as a successful registration. A
        ``RegisterHotKey`` failure is a collision or reservation: it returns
        False, records a stable reason and leaves ``registered_key`` as None, so
        the caller cannot claim the shortcut is applied or ready.
        """
        self.set_key(key)
        if not self.register_hotkey:
            self.registration_error = None
            return True
        target = self._binding.canonical
        if self.registered_key == target:
            self.registration_error = None
            return True
        if self.registered_key is not None:
            self.unregister()
        virtual_key = VK.get(self._binding.key or "")
        if virtual_key is None:
            self.registration_error = (
                f"no virtual-key code for {self._binding.key!r}; the binding was not registered")
            return False
        self.registration_attempts += 1
        if _user32().RegisterHotKey(None, HOTKEY_ID, self.modifier_mask() | MOD_NOREPEAT, virtual_key):
            self.registered_key = target
            self.registration_error = None
            return True
        self.registration_error = (
            "RegisterHotKey returned 0: the combination is already registered by another "
            "application, reserved by Windows, or refused by the session")
        return False

    def unregister(self) -> None:
        if self.registered_key:
            _user32().UnregisterHotKey(None, HOTKEY_ID)
            self.registered_key = None

    def frontmost(self) -> bool:
        return premiere_in_foreground()

    # -- polling --------------------------------------------------------
    def drain_messages(self, key: str) -> bool:
        """Consume WM_HOTKEY messages; True if one belongs to this binding."""
        hotkey_message = False
        message = wintypes.MSG()
        while _user32().PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            if self.registered_key == key and message.wParam == HOTKEY_ID:
                hotkey_message = True
        return hotkey_message

    def key_state(self, key: str) -> int:
        virtual_key = VK.get(key)
        if virtual_key is None:
            return 0
        return _user32().GetAsyncKeyState(virtual_key)

    def poll(self, key: str) -> tuple[bool, bool, bool]:
        target = self._binding.canonical
        if key != target:
            return False, False, False
        if self.register_hotkey and not self.registered_key:
            # A shortcut Cuthere does not own must never be treated as held:
            # recording on an unregistered combination would silently miss the
            # real shortcut and could capture a take the editor never intended.
            return False, False, False
        hotkey_message = self.drain_messages(target)
        key_state = self.key_state(self._binding.key or "")
        down = bool(key_state & 0x8000)
        pressed_since_poll = bool(key_state & 0x0001)
        # A chord is only "held" while its modifiers are still down; otherwise a
        # released modifier would leave the take recording. The same exact-match
        # rule gates the press edge, so Control+F8 cannot start a plain F8 note.
        if not self.modifiers_held():
            return False, False, hotkey_message
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

    def health(self) -> dict:
        return {
            "backend": self.backend,
            "platform": "win32",
            "binding": self._binding.canonical,
            "display": self._binding.display("win32"),
            "registered": self.registered_key,
            "register_hotkey": self.register_hotkey,
            "observe_only": self.observe_only,
            "registration_error": self.registration_error,
            "registration_attempts": self.registration_attempts,
            "modifier_mask": self.modifier_mask(),
        }

    def close(self) -> None:
        self.unregister()
