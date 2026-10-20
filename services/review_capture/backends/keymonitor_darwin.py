"""macOS global key backend: an optional, listen-only Quartz event tap.

Design (see docs/MACOS.md):

* PyObjC's ``Quartz``/``AppKit`` frameworks are imported lazily; without them
  this backend reports ``available: False`` and the panel keeps working with its
  own Start/Stop buttons. Nothing is compiled and nothing is installed here.
* ``CGEventTapCreate`` uses ``kCGSessionEventTap`` + ``kCGHeadInsertEventTap`` +
  ``kCGEventTapOptionListenOnly``: Cuthere observes F8/F9 but never swallows it,
  so Premiere still receives the key and the edit never changes because a note
  was recorded.
* The tap needs Accessibility and (on current macOS) Input Monitoring approval
  for the process that hosts it. A denied tap is reported, never bypassed:
  Cuthere does not touch the TCC database and does not ask for Full Disk Access.
* Front-application detection uses ``NSWorkspace.frontmostApplication()``; the
  executable path must belong to Premiere or to the CEP HTML Engine that lives
  inside the Premiere bundle.
* A tap can be disabled by macOS (``kCGEventTapDisabledByTimeout`` after a slow
  callback) or by user input (``kCGEventTapDisabledByUserInput``, e.g. while a
  secure input field is focused). Either notification ends the held-key state,
  so an active take stops, is recorded in ``health()``/status.json, and the tap
  is only re-enabled deliberately — never silently left "down" forever.
* ``stop()``/``close()`` always run and release the native tap, source and run
  loop, including when the run-loop thread outlives a timed-out ``start()``.

Event-tap state is read from an immutable snapshot published by the callback
thread; polling never blocks on the callback.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Any

# Virtual key codes (Carbon HIToolbox Events.h), stable across macOS releases.
KEY_CODES = {"F8": 100, "F9": 101, "F10": 109, "F11": 103}
KEY_NAMES = {code: name for name, code in KEY_CODES.items()}

PREMERE_BUNDLE = "Adobe Premiere Pro"
CEP_ENGINE_BUNDLE = "CEPHtmlEngine"

BACKEND_NAME = "quartz-event-tap"

# Seconds to wait for the run-loop thread to report that the tap was created or
# denied. A slow host must not hang the worker's arm path.
START_TIMEOUT = 5.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _quartz():
    import Quartz  # type: ignore[import-not-found]

    return Quartz


def _appkit():
    import AppKit  # type: ignore[import-not-found]

    return AppKit


def probe() -> dict[str, Any]:
    """Report whether the backend can run here, without creating a tap."""
    if sys.platform != "darwin":
        return {"platform": sys.platform, "backend": BACKEND_NAME, "available": False,
                "reason": "the Quartz backend only runs on macOS"}
    try:
        _quartz()
        _appkit()
    except Exception as exc:  # pragma: no cover - exercised only on macOS
        return {"platform": "darwin", "backend": BACKEND_NAME, "available": False,
                "reason": f"PyObjC frameworks unavailable ({type(exc).__name__}); "
                          f"install 'pyobjc-framework-Quartz' or use the panel buttons",
                "framework_import": f"{type(exc).__name__}: {exc}"}
    return {"platform": "darwin", "backend": BACKEND_NAME, "available": True, "reason": "",
            "framework_import": "ok"}


def _premiere_executables() -> list[str]:
    """Executable paths whose frontmost status counts as 'Premiere is active'."""
    classical = "/Applications/Adobe Premiere Pro 2026/Adobe Premiere Pro 2026.app/Contents/MacOS/Adobe Premiere Pro 2026"
    paths = [classical]
    try:
        completed = subprocess.run(
            ["ls", "-d", "/Applications/Adobe Premiere Pro *.app", "/Applications/Adobe Premiere Pro /*.app"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        paths.extend(line.strip() for line in completed.stdout.splitlines() if line.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return paths


def is_premiere_frontmost(workspace: Any) -> bool:
    """True when the front application is Premiere or its CEP HTML Engine."""
    application = workspace.frontmostApplication()
    if application is None:
        return False
    name = str(application.localizedName() or "")
    bundle = str(application.bundleIdentifier() or "")
    path = str(application.executableURL().path()) if application.executableURL() else ""
    low_path = path.lower()
    if PREMERE_BUNDLE.lower() in name.lower() or PREMERE_BUNDLE.lower() in low_path:
        return True
    if CEP_ENGINE_BUNDLE.lower() in name.lower() or CEP_ENGINE_BUNDLE.lower() in low_path:
        # Only the engine installed inside Premiere counts.
        return "adobe premiere pro" in low_path
    if bundle.startswith("com.adobe.PremierePro"):
        return True
    return False


class DarwinKeyMonitor:
    backend = BACKEND_NAME

    def __init__(self, key: str = "F8", executable_paths: list[str] | None = None):
        self.key = key if key in KEY_CODES else "F8"
        self.executable_paths = executable_paths if executable_paths is not None else _premiere_executables()
        self._lock = threading.Lock()
        # (down, press_since_last_poll). The press bit is a latch: it survives a
        # key-up that arrives before the next poll, so a complete short tap is
        # still reported once. It is cleared only by a poll that consumes it, by
        # disarming, or by changing the armed key.
        self._snapshot: tuple[bool, bool] = (False, False)
        self._key_down: dict[int, bool] = {}
        self._seen_down = False
        self._tap = None
        self._source = None
        self._run_loop = None
        self._thread: threading.Thread | None = None
        self._start_token: object | None = None
        self._error: str | None = None
        self._closed = False
        self._quartz = None
        self._appkit = None
        self._tap_state = "stopped"
        self._needs_reenable = False
        self._disabled_events = 0
        self._reenabled = 0
        self._last_disabled: dict[str, str] | None = None
        self._callback_errors = 0
        self._last_callback_error: str | None = None

    # -- lifecycle ------------------------------------------------------
    def start(self) -> None:
        """Create the listen-only tap on its own run loop; raises if macOS denies it."""
        if self._thread is not None:
            return
        quartz = _quartz()
        appkit = _appkit()
        token = object()
        ready = threading.Event()
        with self._lock:
            self._quartz = quartz
            self._appkit = appkit
            self._start_token = token
            self._error = None
            self._closed = False
            self._tap_state = "starting"
        thread = threading.Thread(target=self._run, args=(token, ready), name="cuthere-key-tap", daemon=True)
        self._thread = thread
        thread.start()
        if not ready.wait(timeout=START_TIMEOUT):
            # Withdraw this start. The thread may still be inside
            # CGEventTapCreate; it observes the withdrawn token afterwards and
            # releases anything it created, so a late tap cannot leak.
            with self._lock:
                if self._start_token is token:
                    self._start_token = None
                    if self._thread is thread:
                        self._thread = None
                    self._error = "timed out while creating the macOS key event tap"
                    self._tap_state = "failed"
                    self._key_down.clear()
                    self._snapshot = (False, False)
            raise RuntimeError("timed out while creating the macOS key event tap")
        with self._lock:
            error = self._error
            superseded = self._start_token is not token
        if error:
            raise RuntimeError(error)
        if superseded:
            raise RuntimeError("the key event tap start was superseded")

    def _run(self, token: object, ready: threading.Event) -> None:  # pragma: no cover - needs macOS
        Quartz = self._quartz
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) |
                Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp) |
                Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))

        def callback(_proxy, event_type, event, _refcon):
            try:
                self._handle_event(Quartz, event_type, event)
            except Exception as exc:  # a failing callback must never kill the tap
                self._record_callback_error(exc)
            return event

        created_tap = None
        try:
            created_tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                mask,
                callback,
                None,
            )
            if not created_tap:
                self._fail_start(token, ready, "unavailable", (
                    "macOS did not allow the key event tap. Grant Accessibility (and Input Monitoring) "
                    "to the application running this helper, then quit and reopen it. Use the panel "
                    "buttons meanwhile; Cuthere will not change these permissions for you."
                ))
                return
            with self._lock:
                superseded = self._start_token is not token or self._closed
                if not superseded:
                    self._tap = created_tap
            if superseded:
                self._dispose_tap(Quartz, created_tap)
                return
            source = Quartz.CFMachPortCreateRunLoopSource(None, created_tap, 0)
            run_loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(run_loop, source, Quartz.kCFRunLoopCommonModes)
            with self._lock:
                superseded = self._start_token is not token or self._closed
                if not superseded:
                    self._source = source
                    self._run_loop = run_loop
            if superseded:
                self._release_native()
                return
            Quartz.CGEventTapEnable(created_tap, True)
            with self._lock:
                if self._start_token is token:
                    self._tap_state = "running"
            ready.set()
            Quartz.CFRunLoopRun()
            # CFRunLoopRun returns when stop() asked it to, or because the run
            # loop died. Only the latter is a failure: invalidate the held state
            # so a take cannot continue with a monitor that is no longer there.
            with self._lock:
                if not self._closed and self._error is None and self._start_token is token:
                    self._error = "the macOS key event tap run loop stopped unexpectedly"
                    self._tap_state = "failed"
                    self._key_down.clear()
                    self._snapshot = (False, False)
        except Exception as exc:
            self._fail_start(token, ready, "failed", f"{type(exc).__name__}: {exc}")
            if created_tap is not None:
                self._dispose_tap(Quartz, created_tap)
        finally:
            ready.set()
            self._release_native()
            with self._lock:
                if self._start_token is token:
                    # The thread ended on its own (denied tap, run-loop death):
                    # allow a later start() on the same object.
                    self._start_token = None
                    if self._thread is threading.current_thread():
                        self._thread = None

    def _fail_start(self, token: object, ready: threading.Event, state: str, message: str) -> None:
        with self._lock:
            if self._start_token is token:
                self._error = message
                self._tap_state = state
                self._key_down.clear()
                self._snapshot = (False, False)
        ready.set()

    @staticmethod
    def _dispose_tap(Quartz: Any, tap: Any) -> None:
        try:
            Quartz.CGEventTapEnable(tap, False)
        except Exception:
            pass
        try:
            Quartz.CFMachPortInvalidate(tap)
        except Exception:
            pass

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._start_token = None
            self._needs_reenable = False
            self._key_down.clear()
            self._snapshot = (False, False)
            tap, run_loop, quartz = self._tap, self._run_loop, self._quartz
            self._tap_state = "stopping"
        try:
            if tap is not None and quartz is not None:
                quartz.CGEventTapEnable(tap, False)
            if run_loop is not None and quartz is not None:
                quartz.CFRunLoopStop(run_loop)
        except Exception:
            pass
        thread = self._thread
        if (thread is not None and hasattr(thread, "is_alive") and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=2.0)
            if thread.is_alive():
                # The run-loop thread still owns the native objects; it releases
                # them in its own finally path. Invalidating them from here would
                # race with that thread.
                with self._lock:
                    self._tap_state = "stop-timeout"
                return
        self._release_native()

    close = stop

    def _release_native(self) -> None:
        """Release the tap, run-loop source and run-loop handle; idempotent."""
        with self._lock:
            tap, source, run_loop, quartz = self._tap, self._source, self._run_loop, self._quartz
            self._tap = None
            self._source = None
            self._run_loop = None
        if quartz is not None:
            if source is not None and run_loop is not None:
                try:
                    quartz.CFRunLoopRemoveSource(run_loop, source, quartz.kCFRunLoopCommonModes)
                except Exception:
                    pass
            if tap is not None:
                self._dispose_tap(quartz, tap)
        with self._lock:
            if self._closed:
                self._tap_state = "stopped"

    # -- arming ---------------------------------------------------------
    def set_key(self, key: str) -> None:
        new_key = key if key in KEY_CODES else "F8"
        if new_key == self.key:
            return
        self.key = new_key
        # A different key is armed: drop edges recorded for the old one so the
        # next take cannot start from a key the user is no longer holding.
        with self._lock:
            self._key_down.clear()
            self._snapshot = (False, False)

    def set_armed(self, armed: bool) -> None:
        if not armed:
            with self._lock:
                self._key_down.clear()
                self._snapshot = (False, False)
            self._seen_down = False

    @property
    def armed(self) -> bool:
        thread = self._thread
        return (thread is not None and not self._closed and self._error is None
                and not self._needs_reenable)

    @property
    def failed(self) -> bool:
        """True once the backend reported a fatal error and should be recreated."""
        return self._error is not None

    def health(self) -> dict[str, Any]:
        """Diagnostic snapshot for status.json and ptt_key_diagnostic.json."""
        with self._lock:
            return {
                "backend": BACKEND_NAME,
                "platform": "darwin",
                "state": self._tap_state,
                "armed": (self._thread is not None and not self._closed and self._error is None
                          and not self._needs_reenable),
                "tap_present": self._tap is not None,
                "needs_reenable": self._needs_reenable,
                "disabled_events": self._disabled_events,
                "reenabled": self._reenabled,
                "last_disabled": dict(self._last_disabled) if self._last_disabled else None,
                "callback_errors": self._callback_errors,
                "last_callback_error": self._last_callback_error,
                "error": self._error,
                "closed": self._closed,
            }

    def register(self, key: str) -> bool:
        """A listen-only tap has nothing to register; arming is a no-op."""
        self.set_key(key)
        if self._needs_reenable and not self._closed:
            # A tap disabled by user input is only re-enabled when the panel
            # asks for it again (next poll with Premiere in front); a timeout is
            # re-enabled in the callback itself.
            self._reenable_tap()
        return True

    def unregister(self) -> None:
        self.set_armed(False)

    def frontmost(self) -> bool:
        # A closed or failed monitor must not keep reporting that Premiere is
        # active: PushToTalk then releases the tap instead of recording forever.
        if self._closed or self._appkit is None or self._error is not None:
            return False
        return is_premiere_frontmost(self._appkit.NSWorkspace.sharedWorkspace())

    # -- event handling -------------------------------------------------
    def _handle_event(self, Quartz: Any, event_type: int, event: Any) -> None:
        # Disable notifications arrive through the same callback. They must be
        # handled before any keycode field is read: those events carry no key.
        if getattr(Quartz, "kCGEventTapDisabledByTimeout", None) is not None \
                and event_type == Quartz.kCGEventTapDisabledByTimeout:
            self.handle_tap_disabled("timeout")
            return
        if getattr(Quartz, "kCGEventTapDisabledByUserInput", None) is not None \
                and event_type == Quartz.kCGEventTapDisabledByUserInput:
            self.handle_tap_disabled("user-input")
            return
        code = int(Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode))
        if event_type == Quartz.kCGEventKeyDown:
            self.feed(code, True)
        elif event_type == Quartz.kCGEventKeyUp:
            self.feed(code, False)

    def handle_tap_disabled(self, reason: str) -> None:
        """End the held state and report a tap disabled by macOS.

        ``reason`` is ``"timeout"`` (the callback was too slow) or
        ``"user-input"`` (macOS disabled the tap because of user input such as a
        secure text field). Either way the cached down state is dropped so an
        active take stops; a timeout is re-enabled immediately, a user-input
        disable waits for the next explicit ``register()``/poll.
        """
        with self._lock:
            self._key_down.clear()
            self._snapshot = (False, False)
            self._disabled_events += 1
            self._tap_state = f"disabled-{reason}"
            self._last_disabled = {"at": _utc_now(), "reason": reason}
            self._needs_reenable = reason != "timeout"
        if reason == "timeout":
            self._reenable_tap()

    def _reenable_tap(self) -> bool:
        """Re-enable the tap; returns False when it cannot be recovered in place."""
        with self._lock:
            quartz, tap, closed = self._quartz, self._tap, self._closed
        if closed or quartz is None or tap is None:
            return False
        try:
            quartz.CGEventTapEnable(tap, True)
        except Exception as exc:
            with self._lock:
                self._error = f"could not re-enable the key event tap: {type(exc).__name__}: {exc}"
                self._tap_state = "failed"
                self._needs_reenable = False
            return False
        with self._lock:
            self._reenabled += 1
            self._needs_reenable = False
            if not self._closed and self._error is None:
                self._tap_state = "running"
        return True

    def _record_callback_error(self, exc: Exception) -> None:
        # Drop the held state too: a callback that failed cannot be trusted to
        # deliver the matching key-up, and a stuck "down" would record forever.
        with self._lock:
            self._callback_errors += 1
            self._last_callback_error = f"{type(exc).__name__}: {exc}"
            self._key_down.clear()
            self._snapshot = (False, False)

    def feed(self, code: int, down: bool) -> None:
        """Record a key edge for the currently armed key.

        The event tap calls this for every key-down/key-up. Keys that are not
        F8-F11, and function keys other than the armed one, are ignored, so a
        note armed on F8 can never start because F9 was pressed. Tests call it
        directly instead of creating a tap.

        The press bit latches: ``feed(down); feed(up); poll()`` still reports
        the press once, so a complete short tap between two 20 ms polls is not
        lost. Only the held bit ends on key-up.
        """
        if code not in KEY_NAMES or code != KEY_CODES.get(self.key):
            return
        with self._lock:
            was_down = self._key_down.get(code, False)
            self._key_down[code] = down
            if down and not was_down:
                self._snapshot = (True, True)
            elif not down and was_down:
                # Keep an unconsumed press latch for the next poll.
                self._snapshot = (False, self._snapshot[1])

    def poll(self, key: str) -> tuple[bool, bool, bool]:
        with self._lock:
            if key != self.key:
                return False, False, False
            down, pressed = self._snapshot
            # An event tap has no hotkey messages, so the edges reported by the
            # callback are handed to PushToTalk through both flags.
            self._snapshot = (down, False)
            return down, pressed, pressed

    def poll_for(self, key: str, foreground: bool) -> tuple[bool, bool, bool]:
        """Premiere not being frontmost ends the take, exactly as on Windows."""
        down, pressed, message = self.poll(key)
        return (down and foreground), pressed, message
