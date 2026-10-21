"""Hold-to-record lifecycle on the macOS path, and the shared guarantees.

Covers the Darwin backend's own logic (event edges, arming, frontmost check,
unavailable PyObjC) with fakes, plus the guarantees that must hold on every
platform: a recording never stays "stuck" after focus loss, a missing panel, a
shutdown request or a backend failure.

These are logic tests against a fake Quartz surface. They do not, and cannot,
prove that macOS delivers key events, that a permission is granted, or that a
microphone records. See docs/MACOS.md for the hardware checklist.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.review_capture.backends import keymonitor, keymonitor_darwin
from services.review_capture.ptt import PushToTalk
from services.review_capture.storage import atomic_write_json
from services.review_capture.worker import ReviewCaptureWorker


class FakeRecorder:
    def __init__(self, output_dir: Path, device: dict, sd_module=None):
        self.output_dir = output_dir
        self.samples = 4800
        self.started_monotonic = 0.0
        self.last_level = 0.1
        self.stopped = False

    def start(self):
        return {"sample_rate": 48000, "samples_input": 4800, "channels": 1, "device": self.device_for()}

    def device_for(self):
        return {"name": "Fixture mic"}

    def stop(self, status="saved"):
        self.stopped = True
        path = self.output_dir / "audio.wav"
        path.write_bytes(b"RIFF fixture")
        return {"path": "audio.wav", "sha256": "0" * 64, "samples": 4800, "capture_status": "saved"}

    def summary(self):
        return {"samples_input": self.samples}


def darwin_backend() -> keymonitor_darwin.DarwinKeyMonitor:
    """A Darwin backend with the tap replaced by direct test input."""
    backend = keymonitor_darwin.DarwinKeyMonitor("F8")
    backend._thread = object()  # mark as "started" so armed is meaningful
    return backend


class StartableBackend:
    """Backend with the start/close hooks the real Qt backend uses.

    Windows' hotkey backend has no start hook; the macOS tap does. PushToTalk
    must call start() when it creates a backend and close() when it releases it.
    """

    backend = "fake-startable"

    def __init__(self, fail_start: bool = False):
        self.fail_start = fail_start
        self.started = 0
        self.closed = 0
        self.registered_key = None
        self.key = "F8"

    def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("macOS did not allow the key event tap")

    def close(self):
        self.closed += 1

    @property
    def armed(self):
        return self.registered_key is not None

    def set_key(self, key):
        self.key = key

    def set_armed(self, armed):
        if not armed:
            self.registered_key = None

    def register(self, key):
        self.registered_key = key
        return True

    def unregister(self):
        self.registered_key = None

    def frontmost(self):
        return True

    def poll(self, key):
        return False, False, False

    def poll_for(self, key, foreground):
        return False, False, False


class DarwinEventEdgeTests(unittest.TestCase):
    def test_press_is_reported_once_and_hold_continues(self):
        backend = darwin_backend()
        backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
        self.assertEqual(backend.poll("F8"), (True, True, True))
        self.assertEqual(backend.poll("F8"), (True, False, False))
        backend.feed(keymonitor_darwin.KEY_CODES["F8"], False)
        self.assertEqual(backend.poll("F8"), (False, False, False))

    def test_the_configured_key_is_the_only_one_that_starts_a_take(self):
        # A note may be armed on F9; pressing it must not start an F8 take.
        backend = darwin_backend()
        backend.set_key("F9")
        backend.feed(keymonitor_darwin.KEY_CODES["F9"], True)
        self.assertEqual(backend.poll("F9"), (True, True, True))
        # An F8 press must not satisfy an F9-armed take.
        backend = darwin_backend()
        backend.set_key("F9")
        backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
        self.assertEqual(backend.poll("F9"), (False, False, False))
        backend = darwin_backend()
        backend.set_key("F9")
        backend.feed(keymonitor_darwin.KEY_CODES["F9"], True)
        backend.set_armed(False)
        self.assertEqual(backend.poll("F9"), (False, False, False))

    def test_non_function_keys_are_ignored(self):
        backend = darwin_backend()
        backend.feed(0, True)  # 'a'
        self.assertEqual(backend.poll("F8"), (False, False, False))

    def test_disarming_drops_a_held_key(self):
        backend = darwin_backend()
        backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
        backend.set_armed(False)
        self.assertEqual(backend.poll("F8"), (False, False, False))

    def test_a_complete_short_press_survives_the_next_poll(self):
        # Regression: key-up used to clear the unconsumed press edge, so a tap
        # completed between two 20 ms polls disappeared completely.
        backend = darwin_backend()
        backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
        backend.feed(keymonitor_darwin.KEY_CODES["F8"], False)
        self.assertEqual(backend.poll("F8"), (False, True, True))
        self.assertEqual(backend.poll("F8"), (False, False, False))

    def test_an_unconsumed_press_latch_is_dropped_by_disarm_or_key_change(self):
        for reset in ("disarm", "key"):
            backend = darwin_backend()
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], False)
            if reset == "disarm":
                backend.set_armed(False)
            else:
                backend.set_key("F9")
                backend.set_key("F8")
            self.assertEqual(backend.poll("F8"), (False, False, False), reset)

    def test_armed_requires_a_running_tap(self):
        backend = keymonitor_darwin.DarwinKeyMonitor("F8")
        self.assertFalse(backend.armed)
        self.assertTrue(darwin_backend().armed)

    def test_explicit_failure_is_not_armed_and_reports_why(self):
        backend = keymonitor_darwin.DarwinKeyMonitor("F8")
        backend._thread = object()
        backend._error = "macOS did not allow the key event tap"
        self.assertFalse(backend.armed)


class FrontmostDetectionTests(unittest.TestCase):
    class App:
        def __init__(self, name, path):
            self._name, self._path = name, path

        def localizedName(self):
            return self._name

        def bundleIdentifier(self):
            return ""

        def executableURL(self):
            return self

        def path(self):
            return self._path

    class Workspace:
        def __init__(self, app):
            self._app = app

        def frontmostApplication(self):
            return self._app

    def test_premiere_and_its_cep_engine_count(self):
        premiere = self.Workspace(self.App("Adobe Premiere Pro 2026",
                                          "/Applications/Adobe Premiere Pro 2026.app/Contents/MacOS/Adobe Premiere Pro 2026"))
        engine = self.Workspace(self.App("CEPHtmlEngine",
                                         "/Applications/Adobe Premiere Pro 2026.app/Contents/MacOS/CEPHtmlEngine.app/Contents/MacOS/CEPHtmlEngine"))
        self.assertTrue(keymonitor_darwin.is_premiere_frontmost(premiere))
        self.assertTrue(keymonitor_darwin.is_premiere_frontmost(engine))

    def test_other_applications_and_bare_cep_engines_do_not(self):
        safari = self.Workspace(self.App("Safari", "/Applications/Safari.app/Contents/MacOS/Safari"))
        stray = self.Workspace(self.App("CEPHtmlEngine", "/Applications/SomeEditor.app/Contents/MacOS/CEPHtmlEngine"))
        none = self.Workspace(None)
        self.assertFalse(keymonitor_darwin.is_premiere_frontmost(safari))
        self.assertFalse(keymonitor_darwin.is_premiere_frontmost(stray))
        self.assertFalse(keymonitor_darwin.is_premiere_frontmost(none))


class CapabilityTests(unittest.TestCase):
    def test_quartz_backend_is_selected_for_darwin(self):
        self.assertEqual(keymonitor.normalize_platform("darwin"), "darwin")
        capability = keymonitor.capability("darwin")
        self.assertEqual(capability["backend"], "quartz-event-tap")

    def test_unknown_platform_is_reported_not_silently_ignored(self):
        with self.assertRaises(keymonitor.BackendUnavailable):
            keymonitor.create_backend("linux")

    def test_windows_backend_keeps_the_1_0_key_codes(self):
        from services.review_capture.backends.keymonitor_windows import HOTKEYS

        self.assertEqual(HOTKEYS, {"F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A})


class PollRobustnessTests(unittest.TestCase):
    """The poll loop runs every ~20 ms: nothing in it may raise or take the panel down."""

    def test_poll_survives_a_missing_or_corrupt_arm_file(self):
        with tempfile.TemporaryDirectory() as folder:
            ipc = Path(folder) / "ipc"
            ptt = PushToTalk(ipc)
            ptt.backend = darwin_backend()
            ptt.poll(panel_alive=True, manual_recording=False)  # no arm file at all
            atomic_write_json(ipc / "ptt_arm.json", {"hotkey": "F8", "enabled": True})
            (ipc / "ptt_arm.json").write_text("{not json", encoding="utf-8")
            ptt.poll(panel_alive=True, manual_recording=False)  # corrupt arm file
            self.assertIsNone(ptt.current)
            ptt.close()


class WorkerShutdownLockTests(unittest.TestCase):
    """Closing a worker must release its single-instance lock.

    The regression: shutdown/close left ``_lock_handle`` open, so starting a new
    worker against the same IPC directory reported "the recording helper is
    already running" until the old process exited.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ipc = Path(self.temp.name) / "ipc"

    def test_close_releases_the_lock_and_a_new_worker_can_start(self):
        first = ReviewCaptureWorker(self.ipc)
        self.assertFalse(first._lock_handle.closed)
        first.close()
        self.assertTrue(first._lock_handle.closed, "close() must release the single-instance lock")
        second = ReviewCaptureWorker(self.ipc)
        self.addCleanup(second.shutdown)
        self.assertFalse(second._lock_handle.closed)

    def test_shutdown_releases_the_lock_when_status_publication_raises(self):
        worker = ReviewCaptureWorker(self.ipc)
        with patch.object(worker, "_write_status", side_effect=RuntimeError("disk full")):
            worker.shutdown()
        self.assertTrue(worker._lock_handle.closed)
        ReviewCaptureWorker(self.ipc).shutdown()

    def test_shutdown_releases_the_lock_when_backend_cleanup_raises(self):
        worker = ReviewCaptureWorker(self.ipc)
        with patch.object(worker.ptt, "close", side_effect=RuntimeError("tap stuck")):
            worker.shutdown()
        self.assertTrue(worker._lock_handle.closed)
        ReviewCaptureWorker(self.ipc).shutdown()

    def test_shutdown_releases_the_lock_when_recording_cleanup_raises(self):
        from types import SimpleNamespace

        worker = ReviewCaptureWorker(self.ipc)
        worker.recorder = SimpleNamespace(started_monotonic=0.0, samples=0, last_level=0.0)
        with patch.object(worker, "_interrupt_active", side_effect=RuntimeError("recorder stuck")):
            worker.shutdown()
        self.assertTrue(worker._lock_handle.closed)
        ReviewCaptureWorker(self.ipc).shutdown()

    def test_repeated_close_is_safe(self):
        worker = ReviewCaptureWorker(self.ipc)
        worker.close()
        worker.close()
        self.assertTrue(worker._lock_handle.closed)

    def test_a_failed_constructor_does_not_keep_the_lock(self):
        with patch("services.review_capture.worker.PushToTalk", side_effect=RuntimeError("ptt init failed")):
            with self.assertRaises(RuntimeError):
                ReviewCaptureWorker(self.ipc)
        ReviewCaptureWorker(self.ipc).shutdown()


class PushToTalkLifecycleTests(unittest.TestCase):
    """Shared worker guarantees, driven through a Darwin backend."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "Episode 01.prproj"
        self.project.write_bytes(b"fixture")
        self.worker = None
        self.patcher = patch("services.review_capture.ptt.MicrophoneRecorder", FakeRecorder)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def make_worker(self, backend):
        worker = ReviewCaptureWorker(self.root / "ipc")
        worker.ptt.backend = backend
        self.worker = worker
        self.addCleanup(worker._lock_handle.close)
        return worker

    def arm(self, enabled=True):
        atomic_write_json(self.root / "ipc" / "ptt_arm.json", {
            "hotkey": "F8", "enabled": enabled, "project_path": str(self.project),
            "session_id": "session_hotkey_fixture_000001",
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
        })

    def test_hold_starts_recording_and_release_saves(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        with patch.object(backend, "frontmost", return_value=True):
            self.arm()
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(worker.ptt.current["state"], "recording")
            self.assertIsNotNone(worker.ptt.recorder)
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], False)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "complete")
        self.assertTrue((Path(worker.ptt.current["capture_dir"]) / "audio.wav").is_file())

    def test_a_disabled_tap_stops_an_active_recording(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        with patch.object(backend, "frontmost", return_value=True):
            self.arm()
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(worker.ptt.current["state"], "recording")
            # macOS reports the tap as disabled while the take is recording.
            backend.handle_tap_disabled("timeout")
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "complete")
        worker._write_status()
        status = json.loads((self.root / "ipc" / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["push_to_talk_backend"]["disabled_events"], 1)
        self.assertEqual(status["push_to_talk_backend"]["state"], "disabled-timeout")

    def test_a_complete_short_tap_records_once_without_sticking(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        with patch.object(backend, "frontmost", return_value=True):
            self.arm()
            # Both edges arrive between two polls: the press latch must survive.
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], False)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "complete")
        capture_id = worker.ptt.current["id"]
        with patch.object(backend, "frontmost", return_value=True):
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "complete")
        self.assertEqual(worker.ptt.current["id"], capture_id, "a consumed tap must not start a second take")

    def test_focus_loss_stops_and_saves_a_held_recording(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        with patch.object(backend, "frontmost", return_value=True):
            self.arm()
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "recording")
        # The user switches to another application while still holding F8.
        with patch.object(backend, "frontmost", return_value=False):
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "complete")
        # The tap is released (PushToTalk disarmed) so the next press starts fresh.
        self.assertEqual(worker.ptt.arm, {})
        # Coming back with F8 still held must not immediately restart a take:
        # the editor has to press again.
        with patch.object(backend, "frontmost", return_value=True):
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], False)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(worker.ptt.current["id"], Path(worker.ptt.current["capture_dir"]).name)

    def test_losing_the_panel_stops_the_recording(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        with patch.object(backend, "frontmost", return_value=True):
            self.arm()
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
            worker.ptt.poll(panel_alive=False, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "complete")

    def test_stale_arm_file_does_nothing(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        with patch.object(backend, "frontmost", return_value=True):
            self.arm(enabled=False)
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertIsNone(worker.ptt.current)

    def test_worker_shutdown_saves_an_active_recording_and_releases_the_backend(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        with patch.object(backend, "frontmost", return_value=True):
            self.arm()
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "recording")
        worker.shutdown()
        self.assertEqual(worker.ptt.current["state"], "complete")
        self.assertIsNone(worker.ptt.backend)

    def test_microphone_failure_is_visible_and_not_stuck(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)

        class BrokenRecorder(FakeRecorder):
            def start(self):
                raise RuntimeError("no microphone permission")

        with patch.object(backend, "frontmost", return_value=True), \
                patch("services.review_capture.ptt.MicrophoneRecorder", BrokenRecorder):
            self.arm()
            backend.feed(keymonitor_darwin.KEY_CODES["F8"], True)
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(worker.ptt.current["state"], "failed")
        self.assertIn("no microphone permission", worker.ptt.current["error"])
        self.assertIsNone(worker.ptt.recorder)

    def test_unavailable_backend_is_reported_in_diagnostics_not_silent(self):
        worker = self.make_worker(None)

        def unavailable(*_args, **_kwargs):
            raise keymonitor.BackendUnavailable("PyObjC is not installed")

        with patch("services.review_capture.ptt._make_backend", unavailable):
            self.arm()
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        diagnostic = json.loads((self.root / "ipc" / "ptt_key_diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["backend"], "unavailable")
        self.assertIn("PyObjC", diagnostic["reason"])
        self.assertIsNone(worker.ptt.current)
        self.assertFalse(worker.ptt.armed)

    def test_status_reports_backend_capability(self):
        backend = darwin_backend()
        worker = self.make_worker(backend)
        worker._write_status()
        status = json.loads((self.root / "ipc" / "status.json").read_text(encoding="utf-8"))
        self.assertIn("push_to_talk_capability", status)
        self.assertIn("push_to_talk_armed", status)

    def test_arming_starts_the_backend_and_disarming_releases_it(self):
        backend = StartableBackend()
        worker = self.make_worker(None)
        self.arm()
        with patch("services.review_capture.ptt._make_backend", return_value=backend):
            worker.ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(backend.started, 1, "the macOS tap must be created when the panel arms")
            self.assertIs(worker.ptt.backend, backend)
            self.assertTrue(worker.ptt.armed)
            # The panel goes away: the tap is released instead of staying open.
            worker.ptt.poll(panel_alive=False, manual_recording=False)
        self.assertEqual(backend.closed, 1)
        self.assertIsNone(worker.ptt.backend)
        self.assertFalse(worker.ptt.armed)

    def test_a_denied_tap_is_reported_and_retried_after_permission(self):
        denied = StartableBackend(fail_start=True)
        allowed = StartableBackend()
        worker = self.make_worker(None)
        self.arm()
        with patch("services.review_capture.ptt._make_backend", side_effect=[denied, allowed]):
            worker.ptt.poll(panel_alive=True, manual_recording=False)
            diagnostic = json.loads((self.root / "ipc" / "ptt_key_diagnostic.json").read_text(encoding="utf-8"))
            self.assertEqual(diagnostic["backend"], "unavailable")
            self.assertIn("key event tap", diagnostic["reason"])
            self.assertEqual(denied.closed, 1, "a failed tap must not be left half-open")
            self.assertIsNone(worker.ptt.backend)
            # Granting the permission and polling again creates the tap and
            # clears the stale warning instead of keeping "unavailable" forever.
            worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(allowed.started, 1)
        diagnostic = json.loads((self.root / "ipc" / "ptt_key_diagnostic.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostic["backend"], "fake-startable")
        self.assertEqual(diagnostic["permission"], "ok")


class FakeTap:
    def __init__(self) -> None:
        self.enabled: list[bool] = []
        self.invalidated = 0


class FakeEvent:
    def __init__(self, code, flags=0, repeat=False) -> None:
        self.code = code
        self.flags = flags
        self.repeat = repeat


class FakeQuartz:
    """The small Quartz surface the backend uses, with recorded side effects."""

    kCGEventKeyDown = 10
    kCGEventKeyUp = 11
    kCGEventFlagsChanged = 12
    kCGEventTapDisabledByTimeout = 0xFFFFFFFE
    kCGEventTapDisabledByUserInput = 0xFFFFFFFF
    kCGSessionEventTap = 1
    kCGHeadInsertEventTap = 2
    kCGEventTapOptionListenOnly = 3
    kCGKeyboardEventKeycode = 9
    kCGKeyboardEventAutorepeat = 8
    kCFRunLoopCommonModes = "common"
    kCGEventSourceStateCombinedSessionState = 0

    @staticmethod
    def CGEventMaskBit(bit):
        return 1 << bit

    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.create_gate = threading.Event()
        self.create_gate.set()
        self.run_started = threading.Event()
        self.run_gate = threading.Event()
        self.callback = None
        self.tap = FakeTap()
        self.create_calls = 0
        self.removed_sources = 0
        self.sources_added = 0
        #: Physical side state, as CGEventSourceKeyState reports it. The
        #: aggregate flags are derived from it so the fake behaves like macOS:
        #: releasing one Option while the other stays down keeps the Alt flag.
        self.physical_modifiers: dict[int, bool] = {}
        self.native_state_reads = 0

    def CGEventSourceKeyState(self, _state_id, code):
        self.native_state_reads += 1
        return 1 if self.physical_modifiers.get(int(code), False) else 0

    def set_physical_modifier(self, code, down):
        self.physical_modifiers[int(code)] = bool(down)

    def modifier_flags(self):
        flags = 0
        for code, name in keymonitor_darwin.MODIFIER_NAMES.items():
            if self.physical_modifiers.get(code, False):
                flags |= keymonitor_darwin.MODIFIER_FLAGS[keymonitor_darwin.modifier_base(name)]
        return flags

    def CGEventTapCreate(self, *_args, **_kwargs):
        self.create_calls += 1
        self.create_gate.wait(5)
        if self.deny:
            return None
        self.callback = _args[4]
        return self.tap

    def CFMachPortCreateRunLoopSource(self, *_args):
        return object()

    def CFRunLoopGetCurrent(self):
        return object()

    def CFRunLoopAddSource(self, *_args):
        self.sources_added += 1

    def CFRunLoopRemoveSource(self, *_args):
        self.removed_sources += 1

    def CGEventTapEnable(self, tap, enabled):
        tap.enabled.append(bool(enabled))

    def CFMachPortInvalidate(self, tap):
        tap.invalidated += 1

    def CFRunLoopRun(self):
        self.run_started.set()
        self.run_gate.wait(5)

    def CFRunLoopStop(self, _run_loop):
        self.run_gate.set()

    def CGEventGetIntegerValueField(self, event, field):
        if field == self.kCGKeyboardEventAutorepeat:
            return 1 if getattr(event, "repeat", False) else 0
        return event.code

    def CGEventGetFlags(self, event):
        return int(getattr(event, "flags", 0))

    def key_event(self, code, down, *, flags=None, repeat=False):
        if flags is None:
            flags = self.modifier_flags()
        event_type = self.kCGEventKeyDown if down else self.kCGEventKeyUp
        return self.callback(None, event_type, FakeEvent(code, flags=flags, repeat=repeat), None)

    def modifier_event(self, code, down=None, *, flags=None):
        """A flagsChanged edge with macOS semantics: the side changes, the
        aggregate family flag is derived from all physically down sides."""
        if down is not None:
            self.set_physical_modifier(code, down)
        if flags is None:
            flags = self.modifier_flags()
        return self.callback(None, self.kCGEventFlagsChanged, FakeEvent(code, flags=flags), None)

    def disable_event(self, event_type):
        return self.callback(None, event_type, None, None)


class FakeApplication:
    def localizedName(self):
        return "Adobe Premiere Pro 2026"

    def bundleIdentifier(self):
        return "com.adobe.PremierePro"

    def executableURL(self):
        return None


class FakeWorkspace:
    def frontmostApplication(self):
        return FakeApplication()


class FakeAppKit:
    class NSWorkspace:
        @staticmethod
        def sharedWorkspace():
            return FakeWorkspace()


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class DarwinTapLifecycleTests(unittest.TestCase):
    """Fake-Quartz lifecycle: disabled taps, dead loops and native cleanup.

    These exercise the backend's own state machine with the same callback and
    run-loop shape the operating system uses. They cannot prove macOS delivers
    key events or grants a permission; see docs/MACOS.md.
    """

    def start(self, quartz: FakeQuartz | None = None, *, timeout: float | None = None,
              binding: str = "F8"):
        quartz = quartz or FakeQuartz()
        backend = keymonitor_darwin.DarwinKeyMonitor(binding)
        patches = [patch.object(keymonitor_darwin, "_quartz", return_value=quartz),
                   patch.object(keymonitor_darwin, "_appkit", return_value=FakeAppKit())]
        if timeout is not None:
            patches.append(patch.object(keymonitor_darwin, "START_TIMEOUT", timeout))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(backend.stop)
        backend.start()
        self.assertTrue(quartz.run_started.wait(5), "the fake run loop must start")
        return backend, quartz

    def test_a_timeout_disable_ends_the_held_key_and_reenables_once(self):
        backend, quartz = self.start()
        quartz.key_event(keymonitor_darwin.KEY_CODES["F8"], True)
        self.assertEqual(backend.poll("F8"), (True, True, True))
        quartz.disable_event(quartz.kCGEventTapDisabledByTimeout)
        # The cached "down" state is gone: an active take stops instead of
        # recording forever with a monitor macOS has disabled.
        self.assertEqual(backend.poll("F8"), (False, False, False))
        health = backend.health()
        self.assertEqual(health["disabled_events"], 1)
        self.assertEqual(health["reenabled"], 1)
        self.assertEqual(health["last_disabled"]["reason"], "timeout")
        self.assertEqual(health["state"], "running")
        self.assertEqual(quartz.tap.enabled.count(True), 2, "initial enable plus one recovery")

    def test_a_user_input_disable_is_reported_and_recovers_on_the_next_register(self):
        backend, quartz = self.start()
        quartz.key_event(keymonitor_darwin.KEY_CODES["F8"], True)
        self.assertEqual(backend.poll("F8"), (True, True, True))
        quartz.disable_event(quartz.kCGEventTapDisabledByUserInput)
        self.assertEqual(backend.poll("F8"), (False, False, False))
        health = backend.health()
        self.assertEqual(health["state"], "disabled-user-input")
        self.assertTrue(health["needs_reenable"])
        self.assertEqual(health["reenabled"], 0)
        self.assertFalse(backend.armed)
        # The worker re-arms on its next poll with Premiere in front; that is
        # the deliberate recovery point for a user-input disable.
        self.assertTrue(backend.register("F8"))
        health = backend.health()
        self.assertEqual(health["state"], "running")
        self.assertFalse(health["needs_reenable"])
        self.assertEqual(health["reenabled"], 1)

    def test_a_callback_error_drops_the_cached_down_state(self):
        backend, quartz = self.start()
        quartz.key_event(keymonitor_darwin.KEY_CODES["F8"], True)
        self.assertEqual(backend.poll("F8"), (True, True, True))
        quartz.callback(None, quartz.kCGEventKeyUp, FakeEvent("not-a-keycode"), None)
        self.assertEqual(backend.poll("F8"), (False, False, False))
        health = backend.health()
        self.assertEqual(health["callback_errors"], 1)
        self.assertIn("ValueError", health["last_callback_error"])

    def test_a_dead_run_loop_fails_visibly_and_stops_reporting_frontmost(self):
        backend, quartz = self.start()
        quartz.key_event(keymonitor_darwin.KEY_CODES["F8"], True)
        self.assertEqual(backend.poll("F8"), (True, True, True))
        quartz.run_gate.set()  # CFRunLoopRun returns without stop() being called
        self.assertTrue(wait_for(lambda: backend.failed), backend.health())
        self.assertEqual(backend.poll("F8"), (False, False, False))
        self.assertFalse(backend.frontmost(), "a dead tap must not claim Premiere is active")
        self.assertFalse(backend.armed)
        self.assertEqual(backend.health()["state"], "failed")

    def test_stop_releases_the_tap_source_and_run_loop(self):
        backend, quartz = self.start()
        backend.stop()
        self.assertEqual(quartz.tap.invalidated, 1)
        self.assertGreaterEqual(quartz.removed_sources, 1)
        self.assertIsNone(backend._tap)
        self.assertIsNone(backend._source)
        self.assertIsNone(backend._run_loop)
        self.assertTrue(backend.health()["closed"])
        self.assertFalse(backend.armed)

    def test_a_denied_tap_reports_why_and_leaves_nothing_behind(self):
        quartz = FakeQuartz(deny=True)
        backend = keymonitor_darwin.DarwinKeyMonitor("F8")
        for patcher in (patch.object(keymonitor_darwin, "_quartz", return_value=quartz),
                        patch.object(keymonitor_darwin, "_appkit", return_value=FakeAppKit())):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(RuntimeError) as caught:
            backend.start()
        self.assertIn("did not allow the key event tap", str(caught.exception))
        self.assertIsNone(backend._tap)
        self.assertIsNone(backend._thread)
        self.assertFalse(backend.armed)
        self.assertEqual(backend.health()["state"], "unavailable")

    def test_a_late_tap_after_a_start_timeout_is_released(self):
        quartz = FakeQuartz()
        quartz.create_gate.clear()  # hold CGEventTapCreate inside the thread
        backend = keymonitor_darwin.DarwinKeyMonitor("F8")
        for patcher in (patch.object(keymonitor_darwin, "_quartz", return_value=quartz),
                        patch.object(keymonitor_darwin, "_appkit", return_value=FakeAppKit()),
                        patch.object(keymonitor_darwin, "START_TIMEOUT", 0.05)):
            patcher.start()
            self.addCleanup(patcher.stop)
        with self.assertRaises(RuntimeError):
            backend.start()
        self.assertIsNone(backend._start_token, "a timed-out start must be withdrawn")
        quartz.create_gate.set()  # the thread now receives the tap
        self.assertTrue(wait_for(lambda: quartz.tap.invalidated == 1), "late tap must be released")
        self.assertIsNone(backend._tap)
        self.assertIsNone(backend._thread)

    def test_native_side_state_resolves_a_release_while_the_other_side_is_down(self):
        # macOS keeps the aggregate Alt flag set while either Option is down.
        # Releasing Right Option with Left Option still held must be resolved
        # from CGEventSourceKeyState, not from the flag.
        backend, quartz = self.start(binding="RightAlt+K")
        quartz.set_physical_modifier(0x3D, True)
        quartz.modifier_event(0x3D)  # Right Option down
        quartz.key_event(keymonitor_darwin.KEY_CODES["K"], True)
        self.assertEqual(backend.poll("RightAlt+K"), (True, True, True))
        quartz.set_physical_modifier(0x3A, True)  # Left Option down
        quartz.modifier_event(0x3A)
        quartz.set_physical_modifier(0x3D, False)  # Right Option released
        quartz.modifier_event(0x3D)
        self.assertEqual(backend.poll("RightAlt+K"), (False, False, False))
        self.assertGreater(quartz.native_state_reads, 0,
                           "the ambiguous release must use the native side query")

    def test_rearm_reconciles_physical_modifier_state_before_accepting_an_edge(self):
        backend, quartz = self.start(binding="RightAlt")
        # While hold-to-record is disarmed the tap observes but does not track.
        backend.set_armed(False)
        quartz.set_physical_modifier(0x3D, True)  # Right Option is physically held
        backend.set_armed(True)
        self.assertTrue(backend.health()["modifier_state_known"])
        self.assertEqual(backend.health()["modifiers_down"], ["RightAlt"])
        # Its release is a release, not a spurious new take from the Alt flag.
        quartz.set_physical_modifier(0x3D, False)
        quartz.modifier_event(0x3D)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))

    def test_unknown_side_state_is_not_guessed_into_a_take(self):
        backend, _quartz = self.start(binding="RightAlt")
        backend.set_armed(False)
        # No native query is available (a minimal legacy Quartz surface).
        backend._quartz = object()
        backend.set_armed(True)
        self.assertFalse(backend.health()["modifier_state_known"])
        backend.modifier_changed(0x3D, keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        self.assertEqual(backend.poll("RightAlt"), (False, False, False),
                         "an unknown side state must not fabricate a press")
        self.assertEqual(backend.health()["last_event"]["kind"], "modifier_unknown")
        # The explicit test surface can establish the edge and then works.
        backend.feed(0x3D, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))


if __name__ == "__main__":
    unittest.main()
