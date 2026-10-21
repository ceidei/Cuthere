"""Premiere-only hold-to-record key capture. Audio starts before CEP reads the timeline.

The platform-specific parts live behind a small backend interface
(``services.review_capture.backends.keymonitor``): Windows keeps the 1.0
RegisterHotKey/GetAsyncKeyState behaviour and adds chords, macOS uses an optional
listen-only Quartz event tap that can tell Right Option from Left Option.
Everything below -- arming, starting/stopping the recorder, parking unclaimed
captures, recovering an interrupted capture -- is shared.

The recording shortcut itself is not stored here: it is read from the one
authoritative per-user file through ``shortcut.ShortcutStore`` on every poll, so
a change made by the panel or by ``cuthere shortcut set`` becomes effective
without restarting anything. A change that arrives while a take is running is
never applied mid-take: the take keeps the binding it started with and the new
value is reported as ``pending`` until the take ends.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import shortcut
from .audio import MicrophoneRecorder
from .backends import keymonitor
from .storage import atomic_write_json, new_id, read_json, utc_now


#: Kept for compatibility with existing imports and diagnostics.
HOTKEYS = keymonitor.SUPPORTED_KEYS
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
HOTKEY_ID = 0xAEC8

TAP_STATES = {"preparing", "recording"}


def backend_capability() -> dict[str, Any]:
    """What hold-to-record can do on this machine (used by status and diagnostics)."""
    try:
        return keymonitor.capability()
    except Exception as exc:  # a broken optional dependency must not stop the panel
        return {"platform": keymonitor.normalize_platform(), "backend": None, "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "shortcuts": shortcut.describe_support(keymonitor.normalize_platform())}


def _make_backend(key: str, *, register_hotkey: bool = True) -> Any:
    return keymonitor.create_backend(key=key, register_hotkey=register_hotkey)


class PushToTalk:
    def __init__(self, ipc_root: Path, backend: Any | None = None,
                 shortcut_store: shortcut.ShortcutStore | None = None,
                 platform: str | None = None):
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
        self.platform = shortcut.normalize_platform(
            platform if platform is not None else keymonitor.normalize_platform())
        self.store = shortcut_store or shortcut.ShortcutStore.for_ipc_root(
            ipc_root, platform=self.platform)
        self.applied_binding: shortcut.Binding | None = None
        self.take_binding: shortcut.Binding | None = None
        self.pending_binding: str | None = None
        self.shortcut_snapshot: dict[str, Any] = {}
        self._reported_ready = False
        self._reported_unavailable = False
        #: Whether the effective binding is actually owned/applied by the
        #: backend. A saved setting is not the same as a registered hotkey.
        self.registration: dict[str, Any] = {
            "mode": "idle", "registered": False, "applied": False,
            "reason": "not_polled", "error": None,
        }
        self._registration_reported: str | None = None
        if self.current_path.is_file():
            previous = read_json(self.current_path)
            if previous.get("state") in {"complete", "failed", "cancelled"}:
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
        """Keep an unlinked recording recoverable while allowing the next press."""
        if not self.current or self.current.get("state") not in {"complete", "cancelled"}:
            return
        pending = self.capture_root / "pending" / f"{self.current['id']}.json"
        pending.parent.mkdir(parents=True, exist_ok=True)
        if not pending.exists():
            atomic_write_json(pending, dict(self.current))
        self.current = None
        self._save()

    # -- shortcut --------------------------------------------------------
    def _load_shortcut(self) -> shortcut.ShortcutState:
        try:
            return self.store.load()
        except Exception:  # a broken store must not stop the recording helper
            return shortcut.ShortcutState(
                binding=shortcut.default_binding(self.platform),
                source="default", status="default", config_path=self.store.path,
                updated_at="", problems=("shortcut store unavailable",))

    @staticmethod
    def _arm_binding(arm: dict[str, Any], platform: str) -> shortcut.Binding | None:
        value = str(arm.get("shortcut") or arm.get("hotkey") or "").strip()
        if not value:
            return None
        try:
            return shortcut.parse(value, platform=platform)
        except shortcut.ShortcutError:
            return None

    def _effective_binding(self, state: shortcut.ShortcutState, arm: dict[str, Any]) -> shortcut.Binding:
        """The binding the backend must use on this poll.

        A running take keeps the binding it started with. Otherwise the stored
        value wins; only when no file exists yet does a value written by a 1.0
        panel into the arm file still count (that value is never persisted).
        """
        if self.current and self.current.get("state") in TAP_STATES and self.take_binding is not None:
            return self.take_binding
        if state.status == "default":
            legacy = self._arm_binding(arm, self.platform)
            if legacy is not None and legacy != state.binding:
                return legacy
        return state.binding

    def _apply_binding(self, binding: shortcut.Binding) -> None:
        if self.applied_binding == binding and self.backend is not None:
            return
        self.applied_binding = binding
        setter = getattr(self.backend, "set_key", None)
        if callable(setter):
            setter(binding.canonical)

    def shortcut_state(self) -> dict[str, Any]:
        """Readback for the panel, the CLI and status.json."""
        return dict(self.shortcut_snapshot) if self.shortcut_snapshot else {}

    def _snapshot_shortcut(self, state: shortcut.ShortcutState,
                           binding: shortcut.Binding) -> dict[str, Any]:
        snapshot = state.snapshot(self.platform)
        registration = dict(self.registration)
        snapshot["configured"] = state.binding.canonical
        snapshot["effective"] = binding.canonical
        # "applied" means the backend owns this binding, not merely that the file
        # was saved: a failed RegisterHotKey or an observe-only diagnostic run is
        # never reported as applied/ready.
        snapshot["applied"] = (
            self.applied_binding.canonical
            if self.applied_binding is not None and registration.get("applied") else None)
        snapshot["registration"] = registration
        snapshot["pending"] = self.pending_binding is not None
        snapshot["pending_binding"] = self.pending_binding
        snapshot["active_binding"] = (
            self.take_binding.canonical if self.current and self.current.get("state") in TAP_STATES
            and self.take_binding is not None else None)
        return snapshot

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
                self.applied_binding = None
                self.registration = {"mode": "idle", "registered": False, "applied": False,
                                     "reason": "backend_restarted", "error": None}
                self._registration_reported = None
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
                self.applied_binding = None
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
        self.applied_binding = self._binding_or_none(key)
        return self.backend

    def _binding_or_none(self, key: str) -> shortcut.Binding | None:
        try:
            return shortcut.parse(key, platform=self.platform)
        except shortcut.ShortcutError:
            return None

    def _disarm_backend(self, *, release: bool = False) -> None:
        """Release the key registration; optionally close the backend entirely.

        Focus loss only disarms (the tap/hotkey object is reused when Premiere
        comes back). A stale arm file, a closed panel or shutdown releases the
        object, so the macOS event tap does not stay open behind the user's back.
        """
        self.arm = {}
        self.registration = {"mode": "idle", "registered": False, "applied": False,
                             "reason": "not_armed", "error": None}
        self._registration_reported = None
        backend = self.backend
        if backend is not None:
            try:
                backend.set_armed(False)
            except Exception:
                pass
            if release:
                self.backend = None
                self._reported_ready = False
                self.applied_binding = None
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

    def _take_cancel_requested(self) -> bool:
        """One-shot cancel flag from a backend that detected a chord collision."""
        probe = getattr(self.backend, "take_cancel_requested", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def _register_backend(self, backend: Any, key: str) -> bool:
        """Register/own the binding; False when it is not actually applied.

        The saved value is authoritative for *what* should be used, but a
        backend can still refuse it (a Windows hotkey collision, an OS-reserved
        combination, a missing virtual key). The caller must not emit
        ``armed``/``permission: ok`` or start a take for a shortcut Cuthere does
        not own. ``register_hotkey=False`` is the explicit observe-only
        diagnostic mode: it is reported as such, never as registered/applied.
        """
        register = getattr(backend, "register", None)
        if not callable(register):
            self.registration = {"mode": "unknown", "registered": None, "applied": True,
                                 "reason": "", "error": None}
            return True
        backend_observe_only = getattr(backend, "register_hotkey", None)
        observe_only = (not bool(backend_observe_only)) if backend_observe_only is not None \
            else not bool(self.arm.get("register_hotkey", True))
        try:
            accepted = bool(register(key))
        except Exception as exc:
            accepted = False
            error: str | None = f"{type(exc).__name__}: {exc}"
        else:
            error = None
        if not accepted:
            detail = (error or getattr(backend, "registration_error", None)
                      or "the backend refused the shortcut")
            self.registration = {
                "mode": "failed", "registered": False, "applied": False,
                "reason": "hotkey_registration_failed", "error": str(detail),
            }
            return False
        if observe_only:
            self.registration = {
                "mode": "observe_only", "registered": False, "applied": False,
                "reason": "observe_only_configured", "error": None,
            }
            return True
        registered = getattr(backend, "registered_key", None)
        if registered is not None and registered != key:
            self.registration = {
                "mode": "failed", "registered": False, "applied": False,
                "reason": "hotkey_registration_missing",
                "error": f"backend registered {registered!r}, not the configured {key!r}",
            }
            return False
        self.registration = {
            "mode": "registered" if registered is not None else "listen_only",
            "registered": registered == key if registered is not None else None,
            "applied": True,
            "reason": "",
            "error": None,
        }
        return True

    def _report_registration(self, backend: Any, key: str, *, foreground: bool,
                             panel_alive: bool, fresh: bool, arm: dict[str, Any]) -> None:
        """Publish one diagnostic per registration-state transition."""
        mode = self.registration.get("mode")
        if mode == "failed":
            if self._registration_reported == "failed":
                return
            self._registration_reported = "failed"
            self._reported_ready = False
            self._diagnostic({
                "at": utc_now(), "hotkey": key, "state": "registration_failed",
                "reason": "hotkey_registration_failed",
                "error": self.registration.get("error"),
                "backend": getattr(backend, "backend", None), "permission": "ok",
                "backend_health": self.backend_health(),
                "panel_alive": panel_alive, "arm_fresh": fresh,
                "arm_enabled": bool(arm.get("enabled")),
                "premiere_foreground": foreground,
                "hotkey_registered": self.registered_key,
            })
            return
        label = "observing" if mode == "observe_only" else "armed"
        if self._registration_reported == label:
            return
        self._registration_reported = label
        self._reported_ready = True
        self._diagnostic({
            "at": utc_now(), "hotkey": key, "state": label,
            "backend": getattr(backend, "backend", None),
            "permission": "observe_only" if mode == "observe_only" else "ok",
            "backend_health": self.backend_health(),
            "panel_alive": panel_alive, "arm_fresh": fresh,
            "arm_enabled": bool(arm.get("enabled")),
            "premiere_foreground": foreground,
            "hotkey_registered": self.registered_key,
        })

    # -- polling ---------------------------------------------------------
    def poll(self, panel_alive: bool, manual_recording: bool) -> None:
        arm_path = self.ipc_root / "ptt_arm.json"
        try:
            arm = read_json(arm_path) if arm_path.is_file() else {}
            fresh = time.time() - arm_path.stat().st_mtime < 2.0
        except (OSError, ValueError, json.JSONDecodeError):
            arm, fresh = {}, False
        self.arm = dict(arm)
        state = self._load_shortcut()
        binding = self._effective_binding(state, arm)
        self._refresh_shortcut_readback(state, binding)
        key = binding.canonical
        should_register = bool(panel_alive and fresh and arm.get("enabled") and not manual_recording)
        if not should_register:
            # Read the collision flag before the disarm boundary clears it: the
            # take about to end is the one a chord collision belongs to.
            cancel = self._take_cancel_requested()
            self._disarm_backend(release=True)
            self.was_down = False
            # An active take may still be running when the panel goes away or
            # the arm file goes stale: end it here, not on the next enabled poll.
            if self.current and self.current.get("state") == "recording":
                self._stop(cancel=cancel)
            self._refresh_shortcut_readback(state, binding)
            return
        try:
            backend = self._ensure_backend(key)
            self._apply_binding(binding)
        except Exception as exc:
            self._disarm_backend(release=True)
            self._reported_ready = False
            self._refresh_shortcut_readback(state, binding)
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
            cancel = self._take_cancel_requested()
            self._disarm_backend()
            self.was_down = False
            if self.current and self.current.get("state") == "recording":
                self._stop(cancel=cancel)
            self._refresh_shortcut_readback(state, binding)
            return
        if not self._register_backend(backend, key):
            # The combination is not ours (a Windows collision or a reserved
            # key). Do not poll the key state or start a take: that would record
            # on a shortcut Cuthere does not own. An in-flight take is stopped
            # and its audio saved, never left stuck and never discarded.
            if self.current and self.current.get("state") == "recording":
                self._stop()
            self.was_down = False
            self._report_registration(backend, key, foreground=foreground,
                                      panel_alive=panel_alive, fresh=fresh, arm=arm)
            self._refresh_shortcut_readback(state, binding)
            return
        self._report_registration(backend, key, foreground=foreground,
                                  panel_alive=panel_alive, fresh=fresh, arm=arm)
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
                "registration": dict(self.registration),
            })
        if self.current and self.current.get("state") == "recording":
            # Focus loss, a closed panel or a released key always ends the take on
            # every platform: a recording may never stay "stuck in progress".
            if not down or not panel_alive or not foreground:
                self._stop(cancel=self._take_cancel_requested())
        elif (down or pressed_since_poll or hotkey_message) and not self.was_down and not manual_recording and panel_alive and fresh and arm.get("enabled") and foreground:
            if self.current and self.current.get("state") in {"complete", "cancelled"}:
                self._park_completed()
            if self.current is None:
                # A cancel that arrived without a take (a chord pressed in the
                # same poll as the modifier, or while disarmed) belongs to no
                # recording and must not discard the next one.
                self._take_cancel_requested()
                self._start(arm, binding)
                # A short press may already have ended while the audio device opened.
                if not backend.poll(key)[0]:
                    self._stop(cancel=self._take_cancel_requested())
        self.was_down = down
        # Recompute the published readback with the final state of this poll: a
        # deferred change stops being "pending" the moment the take ends.
        self._refresh_shortcut_readback(state, binding)

    def _refresh_shortcut_readback(self, state: shortcut.ShortcutState,
                                   binding: shortcut.Binding) -> None:
        """Publish the effective/pending binding the panel and the CLI read back.

        A take never changes key halfway through: a stored value that differs
        from the running take's binding is reported as pending and applies after
        the take ends.
        """
        if (self.current and self.current.get("state") in TAP_STATES
                and self.take_binding is not None and state.binding != self.take_binding):
            self.pending_binding = state.binding.canonical
        else:
            self.pending_binding = None
        self.shortcut_snapshot = self._snapshot_shortcut(state, binding)

    # -- recording -------------------------------------------------------
    def _start(self, arm: dict[str, Any], binding: shortcut.Binding) -> None:
        capture_id = new_id()
        capture_dir = self.capture_root / capture_id
        capture_dir.mkdir(parents=True, exist_ok=False)
        self.take_binding = binding
        self.current = {
            "id": capture_id, "state": "preparing", "hotkey": binding.canonical,
            "binding": binding.as_dict(self.platform),
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

    def _stop(self, *, cancel: bool = False) -> None:
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
        if cancel and self.current.get("state") == "complete":
            # A modifier-only binding became part of another shortcut: the audio
            # is kept for audit, but no note is created from this take.
            self.current["state"] = "cancelled"
            self.current["cancel_reason"] = "modifier_chord_avoided"
            self.current["error"] = (
                "按住修饰键时按下了其他按键，本次快捷录音已取消（音频保留，不生成意见）。")
        self.current["released_at"] = utc_now()
        self.recorder = None
        self.take_binding = None
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
