"""Premiere-only hold-to-record key capture. Audio starts before CEP reads the timeline.

The platform-specific parts live behind a small backend interface
(``services.review_capture.backends.keymonitor``): Windows keeps the 1.0
RegisterHotKey/GetAsyncKeyState behaviour, macOS uses an optional listen-only
Quartz event tap. Everything below -- arming, starting/stopping the recorder,
parking unclaimed captures, recovering an interrupted capture -- is shared and
unchanged in behaviour.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .audio import MicrophoneRecorder
from .backends import keymonitor
from .storage import atomic_write_json, new_id, read_json, utc_now


HOTKEYS = keymonitor.SUPPORTED_KEYS
# Kept for compatibility with existing imports and diagnostics.
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
HOTKEY_ID = 0xAEC8


def backend_capability() -> dict[str, Any]:
    """What hold-to-record can do on this machine (used by status and diagnostics)."""
    try:
        return keymonitor.capability()
    except Exception as exc:  # a broken optional dependency must not stop the panel
        return {"platform": keymonitor.normalize_platform(), "backend": None, "available": False,
                "reason": f"{type(exc).__name__}: {exc}"}


def _make_backend(key: str, *, register_hotkey: bool = True) -> Any:
    return keymonitor.create_backend(key=key, register_hotkey=register_hotkey)


class PushToTalk:
    def __init__(self, ipc_root: Path, backend: Any | None = None):
        self.ipc_root = ipc_root
        self.capture_root = ipc_root / "ptt"
        self.capture_root.mkdir(parents=True, exist_ok=True)
        self.current_path = self.capture_root / "current.json"
        self.current: dict[str, Any] | None = None
        self.recorder: MicrophoneRecorder | None = None
        self.was_down = False
        self.backend = backend
        self.backend_error: str | None = None
        self.arm: dict[str, Any] = {}
        if self.current_path.is_file():
            previous = read_json(self.current_path)
            if previous.get("state") in {"complete", "failed"}:
                self.current = previous
            elif previous.get("state") == "recording":
                previous["state"] = "interrupted"
                previous["error"] = "录音助手异常退出；原始采集文件仍在 capture_dir"
                self.current = previous
                self._save()

    # -- compatibility ---------------------------------------------------
    @property
    def registered_key(self) -> str | None:
        return getattr(self.backend, "registered_key", None)

    # -- persistence -----------------------------------------------------
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

    # -- backend handling ------------------------------------------------
    @property
    def armed(self) -> bool:
        """True while the key backend is watching for the hold (arm file fresh)."""
        backend = self.backend
        if backend is not None and hasattr(type(backend), "armed"):
            try:
                return bool(backend.armed)
            except Exception:
                return False
        # A test double without an ``armed`` attribute counts as armed while the
        # arm file is fresh, which is what the 1.0 behaviour reported.
        return bool(self.arm.get("enabled"))

    def _ensure_backend(self, key: str) -> Any:
        backend = self.backend
        if backend is not None:
            # A backend that reported a fatal failure (for example a dead
            # Quartz run loop) must not be polled forever: release it and build a
            # fresh one below, so hold-to-record can recover without restarting
            # the helper.
            try:
                failed = bool(getattr(backend, "failed", False))
            except Exception:
                failed = False
            if failed:
                self.backend = None
                self._reported_ready = False
                try:
                    backend.close()
                except Exception:
                    pass
        if self.backend is None:
            try:
                backend = _make_backend(key, register_hotkey=bool(self.arm.get("register_hotkey", True)))
                # Windows 1.0 registered a hotkey lazily and has no start hook;
                # the macOS Quartz backend creates its event tap here. A failed
                # tap (missing permission) is reported, never left half-created.
                starter = getattr(backend, "start", None)
                if callable(starter):
                    try:
                        starter()
                    except Exception as exc:
                        try:
                            backend.close()
                        except Exception:
                            pass
                        raise keymonitor.BackendUnavailable(f"{type(exc).__name__}: {exc}") from exc
                self.backend = backend
                self.backend_error = None
            except keymonitor.BackendUnavailable as exc:
                self.backend_error = str(exc)
                self.backend = None
                raise
            except Exception as exc:
                self.backend_error = f"{type(exc).__name__}: {exc}"
                self.backend = None
                raise
        # Real backends have a ``set_key``; in-memory test doubles and the legacy
        # Windows doubles in the 1.0 test suite may not, and must be left alone.
        setter = getattr(self.backend, "set_key", None)
        if callable(setter):
            setter(key)
        return self.backend

    def _disarm_backend(self, *, release: bool = False) -> None:
        """Release the key registration; optionally close the backend entirely.

        Focus loss only disarms (the tap/hotkey object is reused when Premiere
        comes back). A stale arm file, a closed panel or shutdown releases the
        object, so the macOS event tap does not stay open behind the user's back.
        """
        self.arm = {}
        backend = self.backend
        if backend is not None:
            try:
                backend.set_armed(False)
            except Exception:
                pass
            if release:
                self.backend = None
                self._reported_ready = False
                try:
                    backend.close()
                except Exception:
                    pass

    def _diagnostic(self, payload: dict[str, Any]) -> None:
        try:
            atomic_write_json(self.ipc_root / "ptt_key_diagnostic.json", payload)
        except OSError:
            pass

    def backend_health(self) -> dict[str, Any]:
        """Health of the live key backend, for status.json and diagnostics."""
        backend = self.backend
        probe = getattr(backend, "health", None)
        if callable(probe):
            try:
                return dict(probe())
            except Exception as exc:
                return {"backend": getattr(backend, "backend", None),
                        "error": f"{type(exc).__name__}: {exc}"}
        return {"backend": getattr(backend, "backend", None), "armed": self.armed,
                "error": self.backend_error}

    # -- polling ---------------------------------------------------------
    def poll(self, panel_alive: bool, manual_recording: bool) -> None:
        arm_path = self.ipc_root / "ptt_arm.json"
        try:
            arm = read_json(arm_path) if arm_path.is_file() else {}
            key = str(arm.get("hotkey", "F8"))
            fresh = time.time() - arm_path.stat().st_mtime < 2.0
        except (OSError, ValueError, json.JSONDecodeError):
            arm, key, fresh = {}, "F8", False
        if key not in HOTKEYS:
            key = "F8"
        self.arm = dict(arm)
        should_register = bool(panel_alive and fresh and arm.get("enabled") and not manual_recording)
        if not should_register:
            self._disarm_backend(release=True)
            self.was_down = False
            # An active take may still be running when the panel goes away or
            # the arm file goes stale: end it here, not on the next enabled poll.
            if self.current and self.current.get("state") == "recording":
                self._stop()
            return
        try:
            backend = self._ensure_backend(key)
        except Exception as exc:
            self._disarm_backend(release=True)
            self._reported_ready = False
            if not getattr(self, "_reported_unavailable", False):
                self._reported_unavailable = True
                self._diagnostic({
                    "at": utc_now(), "hotkey": key, "backend": "unavailable",
                    "permission": "unknown", "reason": str(exc),
                    "capability": backend_capability(),
                    "backend_health": self.backend_health(),
                    "panel_alive": panel_alive, "arm_fresh": fresh,
                    "arm_enabled": bool(arm.get("enabled")),
                    "manual_recording": manual_recording,
                })
            return
        self._reported_unavailable = False
        foreground = bool(backend.frontmost())
        if not foreground:
            # Not Premiere in front: release the hotkey/tap and end any active
            # take; the next press becomes a new one when the editor comes back.
            self._disarm_backend()
            self.was_down = False
            if self.current and self.current.get("state") == "recording":
                self._stop()
            return
        backend.register(key)
        if not getattr(self, "_reported_ready", False):
            # Clear a stale "unavailable" diagnostic after permission is granted
            # or PyObjC is installed; the panel reads this file.
            self._reported_ready = True
            self._diagnostic({
                "at": utc_now(), "hotkey": key, "state": "armed",
                "backend": getattr(backend, "backend", None), "permission": "ok",
                "backend_health": self.backend_health(),
                "panel_alive": panel_alive, "arm_fresh": fresh,
                "arm_enabled": bool(arm.get("enabled")),
                "premiere_foreground": foreground,
                "hotkey_registered": self.registered_key,
            })
        # "Held" always means held *while Premiere is in front*: both backends
        # report that through poll_for, so focus loss ends the take everywhere.
        down, pressed_since_poll, hotkey_message = backend.poll_for(key, foreground)
        active_key = str(self.current.get("hotkey", key)) if self.current and self.current.get("state") == "recording" else key
        if (down or pressed_since_poll or hotkey_message) and not self.was_down:
            self._diagnostic({
                "at": utc_now(), "hotkey": active_key, "down": down,
                "pressed_since_poll": pressed_since_poll,
                "hotkey_message": hotkey_message,
                "backend": getattr(backend, "backend", None),
                "backend_health": self.backend_health(),
                "hotkey_registered": self.registered_key,
                "panel_alive": panel_alive, "arm_fresh": fresh,
                "arm_enabled": bool(arm.get("enabled")),
                "premiere_foreground": foreground,
                "manual_recording": manual_recording,
                "capture_pending": self.current is not None,
            })
        if self.current and self.current.get("state") == "recording":
            # Focus loss, a closed panel or a released key always ends the take on
            # every platform: a recording may never stay "stuck in progress".
            if not down or not panel_alive or not foreground:
                self._stop()
        elif (down or pressed_since_poll or hotkey_message) and not self.was_down and not manual_recording and panel_alive and fresh and arm.get("enabled") and foreground:
            if self.current and self.current.get("state") == "complete":
                self._park_completed()
            if self.current is None:
                self._start(arm, key)
                # A short press may already have ended while the audio device opened.
                if not backend.poll(key)[0]:
                    self._stop()
        self.was_down = down

    # -- recording -------------------------------------------------------
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

    def stop_active(self, reason: str) -> None:
        """Stop (and save) an active recording; used on shutdown and heartbeat loss."""
        if self.current and self.current.get("state") == "recording":
            self._stop()
            if self.current and self.current.get("state") != "complete":
                self.current.setdefault("error", reason)

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

    def close(self) -> None:
        """Release the key backend; safe to call more than once."""
        self.stop_active("录音助手关闭，录音已自动停止")
        self._disarm_backend(release=True)
