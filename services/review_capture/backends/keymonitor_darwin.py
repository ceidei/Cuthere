"""macOS global key backend: an optional, listen-only Quartz event tap.

Design (see docs/MACOS.md):

* PyObjC's ``Quartz``/``AppKit`` frameworks are imported lazily; without them
  this backend reports ``available: False`` and the panel keeps working with its
  own Start/Stop buttons. Nothing is compiled and nothing is installed here.
* ``CGEventTapCreate`` uses ``kCGSessionEventTap`` + ``kCGHeadInsertEventTap`` +
  ``kCGEventTapOptionListenOnly``: Cuthere observes the configured shortcut but
  never swallows it, so Premiere still receives the key. That is deliberate -- a
  recording must never change what the edit does -- and it also means a key that
  Premiere (or macOS) uses for something else still does that thing.
* The tap needs Accessibility and (on current macOS) Input Monitoring approval
  for the process that hosts it. A denied tap is reported, never bypassed:
  Cuthere does not touch the TCC database and does not ask for Full Disk Access.
* Modifiers arrive as ``kCGEventFlagsChanged`` events, not as key down/up. The
  event's keycode identifies *which* side changed (58/61 Option, 59/62 Control,
  55/54 Command, 56/60 Shift); ``CGEventFlags`` only says "some Option is down",
  so the keycode is what makes Right Option distinguishable from Left Option.
  ``modifier_changed()`` records the side and re-evaluates the armed binding.
* A take starts on an exact modifier match, so a plain ``F8`` binding is not
  stolen by ``Command+F8``. It ends when the main key is released or when a
  modifier the binding requires is released, so both release orders stop it.
* A modifier-only binding (Right Option by default on macOS) starts on the
  modifier's own key-down. The first key-down cannot know that the modifier is
  about to become part of a chord, so if any other key or modifier goes down
  while it is held the take is cancelled (``take_cancel_requested``), no note is
  created, and ``health()`` records why. The audio is kept for audit.
* Front-application detection uses ``NSWorkspace.frontmostApplication()``; the
  executable path must belong to Premiere or to the CEP HTML Engine inside it.
* A tap can be disabled by macOS (``kCGEventTapDisabledByTimeout`` after a slow
  callback) or by user input (``kCGEventTapDisabledByUserInput``). Either
  notification ends the held-key state, so an active take stops, is recorded in
  ``health()``/status.json, and the tap is only re-enabled deliberately.
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

from ..shortcut import Binding, ShortcutError, modifiers_match, parse

# Virtual key codes (Carbon HIToolbox Events.h kVK_*), stable across macOS releases.
KEY_CODES = {
    "A": 0x00, "S": 0x01, "D": 0x02, "F": 0x03, "H": 0x04, "G": 0x05, "Z": 0x06,
    "X": 0x07, "C": 0x08, "V": 0x09, "B": 0x0B, "Q": 0x0C, "W": 0x0D, "E": 0x0E,
    "R": 0x0F, "Y": 0x10, "T": 0x11,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19,
    "7": 0x1A, "8": 0x1C, "0": 0x1D,
    "O": 0x1F, "U": 0x20, "I": 0x22, "P": 0x23,
    "Enter": 0x24, "L": 0x25, "J": 0x26, "K": 0x28,
    "N": 0x2D, "M": 0x2E,
    "Tab": 0x30, "Space": 0x31, "Backspace": 0x33,
    "Home": 0x73, "End": 0x77, "PageUp": 0x74, "PageDown": 0x79,
    "Delete": 0x75, "Insert": 0x72,
    "Left": 0x7B, "Right": 0x7C, "Down": 0x7D, "Up": 0x7E,
    "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61,
    "F7": 0x62, "F8": 0x64, "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F,
}
KEY_NAMES = {code: name for name, code in KEY_CODES.items()}

#: Left/right keycodes for each modifier. ``CGEventFlags`` has no side bit, so
#: the keycode carried by ``kCGEventFlagsChanged`` is the only reliable source.
MODIFIER_KEY_CODES = {
    0x37: "LeftMeta", 0x36: "RightMeta",
    0x38: "LeftShift", 0x3C: "RightShift",
    0x3B: "LeftControl", 0x3E: "RightControl",
    0x3A: "LeftAlt", 0x3D: "RightAlt",
}
MODIFIER_NAMES = dict(MODIFIER_KEY_CODES)

#: CGEventFlags masks per modifier family (CGEventTypes.h).
MODIFIER_FLAGS = {
    "Shift": 0x00020000,
    "Control": 0x00040000,
    "Alt": 0x00080000,
    "Meta": 0x00100000,
}


def modifier_base(name: str) -> str:
    """``RightAlt`` -> ``Alt``; a generic name is returned unchanged."""
    if name.startswith("Left"):
        return name[4:]
    if name.startswith("Right"):
        return name[5:]
    return name


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


def modifiers_from_flags(flags: int) -> tuple[str, ...]:
    """Generic modifier names present in ``CGEventFlags``.

    Used only as a fallback when a ``flagsChanged`` edge was missed: it cannot
    tell left from right, so it is never used to satisfy a side-qualified
    binding.
    """
    return tuple(name for name, mask in MODIFIER_FLAGS.items() if int(flags) & mask)


class DarwinKeyMonitor:
    backend = BACKEND_NAME

    def __init__(self, key: str = "F8", executable_paths: list[str] | None = None):
        self.executable_paths = executable_paths if executable_paths is not None else _premiere_executables()
        self._lock = threading.Lock()
        # (down, press_since_last_poll). The press bit is a latch: it survives a
        # key-up that arrives before the next poll, so a complete short tap is
        # still reported once. It is cleared only by a poll that consumes it, by
        # disarming, or by changing the armed key.
        self._snapshot: tuple[bool, bool] = (False, False)
        self._binding: Binding = self._safe_binding(key)
        self._modifiers: set[str] = set()
        self._keys_down: set[str] = set()
        #: Every non-modifier keycode seen down that is not in the bounded
        #: capture grammar (punctuation, Escape, media keys, unsupported binding
        #: keys). Collision observation must not be limited to the grammar: a
        #: comma pressed with Right Option is still a chord, even though a comma
        #: can never be a Cuthere binding.
        self._other_keys_down: set[int] = set()
        #: False after a disarm, tap disable, binding change or callback error:
        #: the physical modifier state may have changed while no reliable edge
        #: was delivered. A flagsChanged event is then resolved from the
        #: side-specific native key state, never from the aggregate family flag.
        self._modifier_state_known = True
        self._native_state_reads = 0
        self._active: str | None = None  # "modifier" or the base key name of a take
        #: Edges are only tracked for an armed hold-to-record. ``set_armed(False)``
        #: pauses tracking while the listen-only tap stays installed (Premiere is
        #: not in front or the panel is closed), so a shortcut pressed in another
        #: application can never start a take or cancel a later one.
        self._hotkey_armed = True
        self._avoided = 0
        self._cancels = 0
        self._consumed_cancels = 0
        self._last_event: dict[str, Any] | None = None
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

    # -- binding ---------------------------------------------------------
    @staticmethod
    def _safe_binding(key: str | Binding) -> Binding:
        try:
            return parse(key, platform="darwin")
        except (ShortcutError, ValueError):
            return parse("F8", platform="darwin")

    @property
    def key(self) -> str:
        """Canonical binding string; the 1.0 attribute name is kept."""
        return self._binding.canonical

    @key.setter
    def key(self, value: str) -> None:
        self.set_key(value)

    @property
    def binding(self) -> Binding:
        return self._binding

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
                    self._reset_state_locked()
            raise RuntimeError("timed out while creating the macOS key event tap")
        with self._lock:
            error = self._error
            superseded = self._start_token is not token
        if error:
            raise RuntimeError(error)
        if superseded:
            raise RuntimeError("the key event tap start was superseded")

    def _reset_state_locked(self) -> None:
        self._modifiers.clear()
        self._keys_down.clear()
        self._other_keys_down.clear()
        self._active = None
        self._snapshot = (False, False)
        # Edges recorded before this boundary cannot establish the side state
        # now; the next flagsChanged event must be reconciled natively (or is
        # treated as unknown and ignored) instead of being guessed from flags.
        self._modifier_state_known = False

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
                    self._reset_state_locked()
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
                self._reset_state_locked()
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
            self._reset_state_locked()
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
    def set_key(self, key: str | Binding) -> None:
        new_binding = self._safe_binding(key)
        if new_binding == self._binding:
            return
        self._binding = new_binding
        # A different binding is armed: drop edges recorded for the old one so
        # the next take cannot start from a key the user is no longer holding.
        with self._lock:
            self._reset_state_locked()
            self._consume_pending_cancels_locked()

    def set_armed(self, armed: bool) -> None:
        with self._lock:
            self._hotkey_armed = bool(armed)
            if not armed:
                self._reset_state_locked()
                self._consume_pending_cancels_locked()
            else:
                # Coming back from a disarm: a modifier may have been pressed or
                # released while edges were not tracked. Reconcile the sides from
                # the native session state when it is available; otherwise the
                # state stays unknown and a later flagsChanged event is ignored
                # rather than guessed from the aggregate flags.
                self._reconcile_modifier_state_locked()

    def _consume_pending_cancels_locked(self) -> None:
        """Forget a chord collision recorded before this arm boundary.

        A disarm (focus loss, a closed panel, a released registration) and a
        binding change both end a take. A cancel latched for that take must not
        reach the next one: the modifier may have been part of a chord in another
        application, which is not a reason to discard a later, unrelated note.
        """
        self._consumed_cancels = self._cancels

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
                "hotkey_armed": self._hotkey_armed,
                "tap_present": self._tap is not None,
                "needs_reenable": self._needs_reenable,
                "disabled_events": self._disabled_events,
                "reenabled": self._reenabled,
                "last_disabled": dict(self._last_disabled) if self._last_disabled else None,
                "callback_errors": self._callback_errors,
                "last_callback_error": self._last_callback_error,
                "error": self._error,
                "closed": self._closed,
                "binding": self._binding.canonical,
                "display": self._binding.display("darwin"),
                "modifier_only": self._binding.modifier_only,
                "modifiers_down": sorted(self._modifiers),
                "modifier_state_known": self._modifier_state_known,
                "unknown_keys_down": [f"0x{code:02x}" for code in sorted(self._other_keys_down)],
                "native_state_reads": self._native_state_reads,
                "active_key": self._active,
                "chord_avoided": self._avoided,
                "last_event": dict(self._last_event) if self._last_event else None,
            }

    def register(self, key: str) -> bool:
        """A listen-only tap has nothing to register; arming enables edge tracking."""
        self.set_key(key)
        with self._lock:
            self._hotkey_armed = True
            self._reconcile_modifier_state_locked()
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
        if event_type == Quartz.kCGEventFlagsChanged:
            self.modifier_changed(code, int(Quartz.CGEventGetFlags(event)))
        elif event_type == Quartz.kCGEventKeyDown:
            repeat = False
            try:
                repeat = bool(Quartz.CGEventGetIntegerValueField(
                    event, Quartz.kCGKeyboardEventAutorepeat))
            except Exception:
                repeat = False
            self.feed(code, True, modifiers=self._tap_modifiers(int(Quartz.CGEventGetFlags(event))),
                      autorepeat=repeat)
        elif event_type == Quartz.kCGEventKeyUp:
            self.feed(code, False, modifiers=self._tap_modifiers(int(Quartz.CGEventGetFlags(event))))

    def _tap_modifiers(self, flags: int) -> tuple[str, ...]:
        """Modifier snapshot for a key event.

        The ``flagsChanged`` stream is authoritative (it has the sides). If it
        has not reported anything yet but the event flags show a modifier, use
        the generic names as a fallback so a missed edge cannot make a chord
        unreachable; the fallback cannot satisfy a side-qualified binding.
        """
        with self._lock:
            tracked = tuple(sorted(self._modifiers))
        return tracked if tracked else modifiers_from_flags(flags)

    def _native_key_state(self, code: int) -> bool | None:
        """Side-specific physical state for one modifier keycode, or ``None``.

        ``CGEventSourceKeyState`` is the documented query for a physical key
        (Apple: CoreGraphics/CGEventSource.h). It is needed because
        ``CGEventFlags`` only carries the aggregate family bit: if Left Option
        is held, releasing Right Option leaves the Option flag set, so the flag
        cannot say that the right side went up. The keycode identifies the side
        and the native query answers for that exact key. Reads are bounded to
        the ambiguous ``flagsChanged`` events and the re-arm reconciliation.
        """
        quartz = self._quartz
        reader = getattr(quartz, "CGEventSourceKeyState", None) if quartz is not None else None
        if not callable(reader):
            return None
        try:
            state_id = int(getattr(quartz, "kCGEventSourceStateCombinedSessionState", 0))
            self._native_state_reads += 1
            return bool(reader(state_id, int(code)))
        except Exception:
            return None

    def _reconcile_modifier_state_locked(self) -> bool:
        """Seed the tracked sides from native state after a blind interval.

        Only the eight modifier keycodes are queried, so this stays bounded. If
        any read is unavailable the tracked state stays unknown; a press is then
        not fabricated from an aggregate flag.
        """
        if self._quartz is None:
            return False
        states: set[str] = set()
        for code, name in MODIFIER_KEY_CODES.items():
            value = self._native_key_state(code)
            if value is None:
                return False
            if value:
                states.add(name)
        self._modifiers = states
        self._modifier_state_known = True
        return True

    def _resolve_modifier_down_locked(self, name: str, code: int, family_active: bool) -> bool | None:
        """Whether this ``flagsChanged`` event is a press of ``name``'s side.

        A cleared family flag is definitive: that family has no key down, so the
        changed side is up. With the family flag set the event could be a press
        of this side or a release while the opposite side keeps the flag alive;
        the aggregate flag cannot tell them apart. The side-specific native
        state is authoritative when available; otherwise a side already tracked
        as down can only have gone up, and a currently tracked-down opposite
        side means an untracked side going down is a press. If the tracked state
        is unknown (re-arm after a blind interval) the event is not guessed.
        """
        if not family_active:
            return False
        native = self._native_key_state(code)
        if native is not None:
            self._modifier_state_known = True
            return native
        if name in self._modifiers:
            return False
        if not self._modifier_state_known:
            return None
        return True

    def modifier_changed(self, code: int, flags: int) -> None:
        """Record a ``flagsChanged`` edge for one specific side.

        ``code`` is the virtual key code of the modifier that changed and
        ``flags`` the event's ``CGEventFlags``. The keycode identifies the side;
        the aggregate flag identifies only the family, so the direction of the
        edge is resolved with the side-specific state above. A ``None`` result
        means the side state is unknown, and conservative handling records the
        ambiguity instead of starting or ending a take on a guess.
        """
        name = MODIFIER_NAMES.get(code)
        if name is None:
            return
        with self._lock:
            if not self._hotkey_armed:
                # The tap keeps observing while hold-to-record is disarmed; those
                # edges belong to other applications and must not be latched.
                return
            if not self._modifier_state_known:
                self._reconcile_modifier_state_locked()
            family_active = bool(flags & MODIFIER_FLAGS[modifier_base(name)])
            down = self._resolve_modifier_down_locked(name, code, family_active)
            if down is None:
                self._last_event = {"kind": "modifier_unknown", "code": code,
                                    "name": name, "family_active": family_active,
                                    "at": _utc_now()}
                return
            self._last_event = {"kind": "modifier", "code": code, "name": name,
                               "down": down, "at": _utc_now()}
            if down:
                self._modifiers.add(name)
            else:
                self._modifiers.discard(name)
            self._apply_modifier_locked(name, down)

    def _apply_modifier_locked(self, name: str, down: bool) -> None:
        binding = self._binding
        if binding.modifier_only:
            if self._active == "modifier":
                if down and name != binding.modifiers[0]:
                    # The held modifier became part of another chord.
                    self._avoid_chord_locked(f"额外按下 {name}")
                    return
                if not down and name == binding.modifiers[0]:
                    self._active = None
                    self._snapshot = (False, self._snapshot[1])
                return
            if down and name == binding.modifiers[0] and self._active is None:
                # The take starts only for a fresh, isolated press: another
                # modifier or any key already held forms a chord, and the
                # aggregate flag (or a re-armed, unknown state) is not evidence
                # that this side was pressed alone.
                if self._keys_down or self._other_keys_down:
                    return
                if any(entry != name for entry in self._modifiers):
                    return
                if not self._modifier_state_known:
                    return
                self._active = "modifier"
                self._snapshot = (True, True)
            return
        if self._active is None or self._active == "modifier":
            return
        if not down and self._is_required(name):
            # A modifier the take needs is no longer down: stop here rather than
            # wait for a main-key release that may never come.
            self._active = None
            self._snapshot = (False, self._snapshot[1])

    def _is_required(self, name: str) -> bool:
        """True when a modifier the active take needs was released."""
        binding = self._binding
        if name in binding.modifiers:
            return True
        base = modifier_base(name)
        # A generic requirement (``Control``) is satisfied by either side, so a
        # release of the specific side that was held must end the take.
        return base in binding.modifiers

    def _avoid_chord_locked(self, reason: str) -> None:
        self._active = None
        # Drop the unconsumed press latch as well: this take is cancelled, so a
        # later poll must not start a second one from the discarded edge.
        self._snapshot = (False, False)
        self._avoided += 1
        self._cancels += 1
        self._last_event = {"kind": "avoided", "reason": reason, "at": _utc_now()}

    def take_cancel_requested(self) -> bool:
        """One-shot: True when the last take must be discarded, not saved."""
        with self._lock:
            if self._cancels > self._consumed_cancels:
                self._consumed_cancels = self._cancels
                return True
            return False

    def handle_tap_disabled(self, reason: str) -> None:
        """End the held state and report a tap disabled by macOS."""
        with self._lock:
            self._reset_state_locked()
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
            self._reset_state_locked()

    # -- test surface ---------------------------------------------------
    def feed(self, code: int, down: bool, *, modifiers: Any = None,
             autorepeat: bool = False) -> None:
        """Record a key edge for the armed binding.

        The event tap calls this for every key event. ``modifiers`` is the
        caller's modifier snapshot; ``None`` means "this caller does not track
        modifiers", which keeps the 1.0 ``feed(code, down)`` test surface and
        treats the binding's modifier requirement as satisfied.

        Keys that do not match the armed binding are ignored, so a note armed on
        F8 can never start because F9 was pressed. The press bit latches:
        ``feed(down); feed(up); poll()`` still reports the press once, so a
        complete short tap between two 20 ms polls is not lost.
        """
        code = int(code)
        if code in MODIFIER_NAMES:
            base_name = MODIFIER_NAMES[code]
            with self._lock:
                if not self._hotkey_armed:
                    return
                # This surface is given the direction of the edge explicitly, so
                # it is authoritative for the changed side (the native event
                # path above resolves the direction from the side state).
                self._modifier_state_known = True
                self._last_event = {"kind": "modifier", "code": code, "name": base_name,
                                    "down": down, "at": _utc_now(), "source": "feed"}
                if down:
                    self._modifiers.add(base_name)
                else:
                    self._modifiers.discard(base_name)
                self._apply_modifier_locked(base_name, down)
            return
        name = KEY_NAMES.get(code)
        with self._lock:
            if not self._hotkey_armed:
                # Disarmed: the tap is still listening, but these edges belong to
                # another application and must not arm or cancel a take.
                return
            if name is None:
                # A real non-modifier key outside the bounded capture grammar
                # (punctuation, Escape, an unsupported binding key). It can
                # never start a take by itself, but it is a real chord member:
                # it cancels a modifier-only take and is tracked so a modifier
                # pressed while it is held does not start one.
                self._last_event = {"kind": "key", "code": code, "name": None,
                                    "down": down, "at": _utc_now()}
                if down:
                    if code in self._other_keys_down:
                        return
                    self._other_keys_down.add(code)
                    binding = self._binding
                    if binding.modifier_only and self._active == "modifier":
                        self._avoid_chord_locked(f"按住修饰键时按下未知键 0x{code:02x}")
                    return
                self._other_keys_down.discard(code)
                return
            self._last_event = {"kind": "key", "code": code, "name": name,
                                "down": down, "at": _utc_now()}
            binding = self._binding
            if down:
                if autorepeat or name in self._keys_down:
                    return
                self._keys_down.add(name)
                if binding.modifier_only:
                    # Any other key while a modifier-only take is held means the
                    # modifier became part of a chord.
                    if self._active == "modifier":
                        self._avoid_chord_locked(f"按住修饰键时按下 {name}")
                    return
                if self._active is not None and self._active != "modifier":
                    return
                if binding.key != name:
                    return
                if modifiers is not None and not modifiers_match(binding, modifiers):
                    return
                if self._active == "modifier":
                    return
                self._active = name
                self._snapshot = (True, True)
                return
            self._keys_down.discard(name)
            if self._active == name:
                self._active = None
                # Keep an unconsumed press latch for the next poll.
                self._snapshot = (False, self._snapshot[1])

    def poll(self, key: str) -> tuple[bool, bool, bool]:
        with self._lock:
            if key != self._binding.canonical:
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
