"""Premiere-only hold-to-record key capture. Audio starts before CEP reads the timeline."""

from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from .audio import MicrophoneRecorder
from .storage import atomic_write_json, new_id, read_json, utc_now


HOTKEYS = {"F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A}
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
HOTKEY_ID = 0xAEC8


def _premiere_in_foreground() -> bool:
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
        if executable.name.casefold() == "adobe premiere pro.exe":
            return True
        # A floating CEP panel has its own foreground process, although it is
        # part of the active Premiere installation.
        return (executable.name.casefold() == "cephtmlengine.exe" and
                (executable.parent.parent / "Adobe Premiere Pro.exe").is_file())
    finally:
        kernel32.CloseHandle(handle)


class PushToTalk:
    def __init__(self, ipc_root: Path):
        self.ipc_root = ipc_root
        self.capture_root = ipc_root / "ptt"
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.current_path = self.capture_root / "current.json"
        self.current: dict[str, Any] | None = None
        self.recorder: MicrophoneRecorder | None = None
        self.was_down = False
        self.registered_key: str | None = None
        if self.current_path.is_file():
            previous = read_json(self.current_path)
            if previous.get("state") in {"complete", "failed"}:
                self.current = previous
            elif previous.get("state") == "recording":
                previous["state"] = "interrupted"
                previous["error"] = "录音助手异常退出；原始采集文件仍在 capture_dir"
                self.current = previous
                self._save()

    def _save(self) -> None:
        atomic_write_json(self.current_path, self.current or {"state": "idle", "updated_at": utc_now()})

    def status(self) -> dict[str, Any] | None:
        return dict(self.current) if self.current else None

    def lookup(self, capture_id: str) -> dict[str, Any] | None:
        if self.current and self.current.get("id") == capture_id:
            return dict(self.current)
        parked = self.capture_root / "pending" / f"{capture_id}.json"
        return read_json(parked) if parked.is_file() else None

    def _park_completed(self) -> None:
        """Keep an unlinked recording recoverable while allowing the next F8 press."""
        if not self.current or self.current.get("state") != "complete":
            return
        pending = self.capture_root / "pending" / f"{self.current['id']}.json"
        pending.parent.mkdir(parents=True, exist_ok=True)
        if not pending.exists():
            atomic_write_json(pending, dict(self.current))
        self.current = None
        self._save()

    def poll(self, panel_alive: bool, manual_recording: bool) -> None:
        if os.name != "nt":
            return
        arm_path = self.ipc_root / "ptt_arm.json"
        try:
            arm = read_json(arm_path) if arm_path.is_file() else {}
            key = str(arm.get("hotkey", "F8"))
            fresh = time.time() - arm_path.stat().st_mtime < 2.0
        except (OSError, ValueError, json.JSONDecodeError):
            arm, key, fresh = {}, "F8", False
        if key not in HOTKEYS:
            key = "F8"
        foreground = _premiere_in_foreground()
        should_register = bool(panel_alive and fresh and arm.get("enabled") and foreground and not manual_recording)
        if self.registered_key and (not should_register or self.registered_key != key):
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
            self.registered_key = None
        if should_register and self.registered_key is None:
            if ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, 0x4000, HOTKEYS[key]):
                self.registered_key = key
        hotkey_message = False
        message = wintypes.MSG()
        while ctypes.windll.user32.PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
            if self.registered_key == key and message.wParam == HOTKEY_ID:
                hotkey_message = True
        active_key = str(self.current.get("hotkey", key)) if self.current and self.current.get("state") == "recording" else key
        key_state = ctypes.windll.user32.GetAsyncKeyState(HOTKEYS[active_key])
        down = bool(key_state & 0x8000)
        pressed_since_poll = bool(key_state & 0x0001)
        if (down or pressed_since_poll or hotkey_message) and not self.was_down:
            try:
                atomic_write_json(self.ipc_root / "ptt_key_diagnostic.json", {
                    "at": utc_now(), "hotkey": active_key, "down": down,
                    "pressed_since_poll": pressed_since_poll,
                    "hotkey_message": hotkey_message,
                    "hotkey_registered": self.registered_key,
                    "panel_alive": panel_alive, "arm_fresh": fresh,
                    "arm_enabled": bool(arm.get("enabled")),
                    "premiere_foreground": foreground,
                    "manual_recording": manual_recording,
                    "capture_pending": self.current is not None,
                })
            except OSError:
                pass
        if self.current and self.current.get("state") == "recording":
            if not down or not panel_alive or not foreground:
                self._stop()
        elif (down or pressed_since_poll or hotkey_message) and not self.was_down and not manual_recording and panel_alive and fresh and arm.get("enabled") and foreground:
            if self.current and self.current.get("state") == "complete":
                self._park_completed()
            if self.current is None:
                self._start(arm, key)
                # A short press may already have ended while the audio device opened.
                if not (ctypes.windll.user32.GetAsyncKeyState(HOTKEYS[key]) & 0x8000):
                    self._stop()
        self.was_down = down

    def _start(self, arm: dict[str, Any], hotkey: str) -> None:
        capture_id = new_id()
        capture_dir = self.capture_root / capture_id
        capture_dir.mkdir(parents=True, exist_ok=False)
        self.current = {
            "id": capture_id, "state": "preparing", "hotkey": hotkey,
            "pressed_at": utc_now(), "capture_dir": str(capture_dir),
            "project_path": str(arm.get("project_path", "")),
            "session_id": str(arm.get("session_id", "")),
        }
        self._save()
        try:
            device = arm.get("device")
            if not isinstance(device, dict):
                raise ValueError("没有选择麦克风")
            self.recorder = MicrophoneRecorder(capture_dir, device)
            self.current["audio"] = self.recorder.start()
            self.current["state"] = "recording"
        except Exception as exc:
            self.recorder = None
            self.current["state"] = "failed"
            self.current["error"] = f"{type(exc).__name__}: {exc}"
        self._save()

    def _stop(self) -> None:
        if not self.current or self.current.get("state") != "recording" or self.recorder is None:
            return
        try:
            audio = self.recorder.stop(status="saved")
            self.current["audio"] = audio
            self.current["state"] = "complete" if audio.get("capture_status") == "saved" else "failed"
        except Exception as exc:
            self.current["audio"] = self.recorder.summary()
            self.current["state"] = "failed"
            self.current["error"] = f"{type(exc).__name__}: {exc}"
        self.current["released_at"] = utc_now()
        self.recorder = None
        self._save()

    def acknowledge(self, capture_id: str) -> None:
        if self.current and self.current.get("id") == capture_id:
            if self.current.get("state") == "recording":
                raise RuntimeError("录音中不能清除状态")
            self.current = None
            self._save()
            return
        parked = self.capture_root / "pending" / f"{capture_id}.json"
        if not parked.is_file():
            raise ValueError("录音编号已变化")
        parked.unlink()
