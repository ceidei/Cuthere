"""Backend and helper behaviour for the configurable hold-to-record shortcut.

Three layers are covered:

* the macOS backend's own event state machine (right vs left modifiers from
  ``flagsChanged``, chords, release ordering, auto-repeat, modifier-only chord
  avoidance) driven through the same callback surface the event tap uses;
* the Windows backend's registration and held-state logic against a fake
  ``user32``, including the unchanged 1.0 F8 codes;
* the shared ``PushToTalk`` logic that reads the one authoritative file: hot
  reload, no mid-take key swap, malformed files, cancellation and status
  readback.

These are logic tests with fakes. They do not prove that macOS or Windows
delivers these events, that a permission is granted, or that a microphone
recorded anything; see docs/MACOS.md for the hardware checklist.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.review_capture import shortcut  # noqa: E402
from services.review_capture.backends import keymonitor_darwin, keymonitor_windows  # noqa: E402
from services.review_capture.ptt import PushToTalk  # noqa: E402
from services.review_capture.storage import atomic_write_json  # noqa: E402
from services.review_capture.worker import ReviewCaptureWorker  # noqa: E402

F = keymonitor_darwin.KEY_CODES
OPTION_LEFT, OPTION_RIGHT = 0x3A, 0x3D
CONTROL_LEFT, SHIFT_LEFT, SHIFT_RIGHT = 0x3B, 0x38, 0x3C
COMMAND_LEFT = 0x37


class FakeRecorder:
    def __init__(self, output_dir: Path, device: dict, sd_module=None):
        self.output_dir = output_dir
        self.samples = 4800
        self.started_monotonic = 0.0
        self.last_level = 0.1

    def start(self):
        return {"sample_rate": 48000, "samples_input": 4800, "channels": 1,
                "device": {"name": "Fixture mic"}}

    def stop(self, status="saved"):
        (self.output_dir / "audio.wav").write_bytes(b"RIFF fixture")
        return {"path": "audio.wav", "sha256": "0" * 64, "samples": 4800,
                "capture_status": "saved"}

    def summary(self):
        return {"samples_input": self.samples}


def darwin_backend(binding="F8") -> keymonitor_darwin.DarwinKeyMonitor:
    backend = keymonitor_darwin.DarwinKeyMonitor(binding)
    backend._thread = object()  # mark as started so `armed` is meaningful
    return backend


class ControllableBackend:
    """Platform-neutral fake for the shared PushToTalk logic."""

    backend = "fake-controllable"

    def __init__(self):
        self.registered_key = None
        self.key = "F8"
        self.keys = []
        self.down = False
        self.pressed = False
        self.closed = 0
        self.foreground = True

    def start(self):
        pass

    def close(self):
        self.closed += 1

    @property
    def armed(self):
        return self.registered_key is not None

    def set_key(self, key):
        self.key = key
        self.keys.append(key)

    def set_armed(self, armed):
        if not armed:
            self.registered_key = None

    def register(self, key):
        self.set_key(key)
        self.registered_key = key
        return True

    def unregister(self):
        self.registered_key = None

    def frontmost(self):
        return self.foreground

    def poll(self, key):
        return self.down, self.pressed, False

    def poll_for(self, key, foreground):
        return (self.down and foreground), self.pressed, False


class DarwinModifierTests(unittest.TestCase):
    """Right Option is distinguished from Left Option by the flagsChanged keycode."""

    def test_the_right_option_starts_a_take_and_the_left_one_does_not(self):
        backend = darwin_backend("RightAlt")
        backend.feed(OPTION_LEFT, True)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        backend.feed(OPTION_LEFT, False)

        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))
        backend.feed(OPTION_RIGHT, False)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))

    def test_flags_changed_release_ends_the_hold(self):
        # The tap reports a modifier release as a flagsChanged event with the
        # flag cleared, never as a key-up.
        backend = darwin_backend("RightAlt")
        backend.modifier_changed(OPTION_RIGHT, keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))
        backend.modifier_changed(OPTION_RIGHT, 0)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))

    def test_one_side_releases_while_the_other_side_keeps_the_family_flag(self):
        # Right Alt + K, then Left Alt goes down. Releasing Right Alt leaves the
        # aggregate Alt flag set (Left Alt is still down), so the flag alone
        # would misread the release as a press. The side-specific state must
        # still end the take when a required modifier is released.
        backend = darwin_backend("RightAlt+K")
        backend.modifier_changed(OPTION_RIGHT, keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        backend.feed(F["K"], True, modifiers=("RightAlt",))
        self.assertEqual(backend.poll("RightAlt+K"), (True, True, True))
        backend.modifier_changed(0x3A, keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        backend.modifier_changed(OPTION_RIGHT, keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        self.assertEqual(backend.poll("RightAlt+K"), (False, False, False))
        self.assertEqual(backend.health()["modifiers_down"], ["LeftAlt"])

    def test_a_side_qualified_chord_needs_that_side(self):
        backend = darwin_backend("RightShift+K")
        backend.feed(SHIFT_LEFT, True)
        backend.feed(F["K"], True, modifiers=("LeftShift",))
        self.assertEqual(backend.poll("RightShift+K"), (False, False, False))
        backend.feed(F["K"], False)
        backend.feed(SHIFT_LEFT, False)

        backend.feed(SHIFT_RIGHT, True)
        backend.feed(F["K"], True, modifiers=("RightShift",))
        self.assertEqual(backend.poll("RightShift+K"), (True, True, True))

    def test_releasing_the_main_key_first_stops_the_take(self):
        backend = darwin_backend("Control+Shift+K")
        backend.feed(CONTROL_LEFT, True)
        backend.feed(SHIFT_LEFT, True)
        backend.feed(F["K"], True, modifiers=("LeftControl", "LeftShift"))
        self.assertEqual(backend.poll("Control+Shift+K"), (True, True, True))
        backend.feed(F["K"], False)
        self.assertEqual(backend.poll("Control+Shift+K"), (False, False, False))

    def test_releasing_a_required_modifier_first_also_stops_the_take(self):
        backend = darwin_backend("Control+Shift+K")
        backend.feed(CONTROL_LEFT, True)
        backend.feed(SHIFT_LEFT, True)
        backend.feed(F["K"], True, modifiers=("LeftControl", "LeftShift"))
        self.assertEqual(backend.poll("Control+Shift+K"), (True, True, True))
        # The main key is still held; the chord is no longer complete.
        backend.feed(SHIFT_LEFT, False)
        self.assertEqual(backend.poll("Control+Shift+K"), (False, False, False))

    def test_a_chord_does_not_start_with_the_wrong_modifiers(self):
        backend = darwin_backend("Control+K")
        backend.feed(F["K"], True, modifiers=("LeftMeta",))
        self.assertEqual(backend.poll("Control+K"), (False, False, False))
        backend.feed(F["K"], False)
        backend.feed(CONTROL_LEFT, True)
        backend.feed(F["K"], True, modifiers=("LeftControl",))
        self.assertEqual(backend.poll("Control+K"), (True, True, True))

    def test_a_plain_key_is_not_stolen_by_a_command_chord(self):
        backend = darwin_backend("F8")
        backend.feed(COMMAND_LEFT, True)
        backend.feed(F["F8"], True, modifiers=("LeftMeta",))
        self.assertEqual(backend.poll("F8"), (False, False, False))

    def test_auto_repeat_does_not_restart_a_take(self):
        backend = darwin_backend("F8")
        backend.feed(F["F8"], True)
        self.assertEqual(backend.poll("F8"), (True, True, True))
        backend.feed(F["F8"], True, autorepeat=True)
        self.assertEqual(backend.poll("F8"), (True, False, False))
        backend.feed(F["F8"], False)
        self.assertEqual(backend.poll("F8"), (False, False, False))

    def test_a_short_tap_between_polls_is_reported_once(self):
        backend = darwin_backend("F8")
        backend.feed(F["F8"], True)
        backend.feed(F["F8"], False)
        self.assertEqual(backend.poll("F8"), (False, True, True))
        self.assertEqual(backend.poll("F8"), (False, False, False))

    def test_the_health_report_names_the_binding_and_the_side(self):
        backend = darwin_backend("RightAlt")
        backend.feed(OPTION_RIGHT, True)
        health = backend.health()
        self.assertEqual(health["binding"], "RightAlt")
        self.assertTrue(health["modifier_only"])
        self.assertEqual(health["modifiers_down"], ["RightAlt"])
        self.assertEqual(health["last_event"]["name"], "RightAlt")


class DarwinChordAvoidanceTests(unittest.TestCase):
    """A modifier-only binding cannot know it is about to become a chord."""

    def test_another_key_while_the_modifier_is_held_cancels_the_take(self):
        backend = darwin_backend("RightAlt")
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))
        backend.feed(F["S"], True, modifiers=("RightAlt",))
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        self.assertTrue(backend.take_cancel_requested())
        self.assertFalse(backend.take_cancel_requested(), "the cancel flag is one-shot")
        self.assertEqual(backend.health()["chord_avoided"], 1)

    def test_a_punctuation_key_outside_the_grammar_cancels_and_is_tracked(self):
        # Codex's reproducer: a physical comma is not a bindable key, but it is
        # still a real chord member. The bounded grammar must not stop collision
        # observation, and an unconsumed press latch from the cancelled edge
        # must not start a second take.
        backend = darwin_backend("RightAlt")
        backend.register("RightAlt")
        backend.modifier_changed(OPTION_RIGHT, keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        backend.feed(0x2B, True, modifiers=("RightAlt",))  # kVK_ANSI_Comma
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        self.assertTrue(backend.take_cancel_requested())
        self.assertEqual(backend.health()["unknown_keys_down"], ["0x2b"])
        # Once the punctuation key is released the modifier starts a fresh take.
        backend.feed(0x2B, False, modifiers=("RightAlt",))
        backend.modifier_changed(OPTION_RIGHT, 0)
        backend.modifier_changed(OPTION_RIGHT, keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))

    def test_escape_is_observed_even_though_it_cannot_be_bound(self):
        backend = darwin_backend("RightAlt")
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))
        backend.feed(0x35, True, modifiers=("RightAlt",))  # kVK_Escape
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        self.assertTrue(backend.take_cancel_requested())

    def test_an_unsupported_key_held_before_the_modifier_blocks_the_take(self):
        backend = darwin_backend("RightAlt")
        backend.feed(0x2B, True)
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False),
                         "a modifier pressed on top of a held key is not a fresh press")
        backend.feed(0x2B, False)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        backend.feed(OPTION_RIGHT, False)
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))

    def test_a_shift_pressed_first_blocks_a_modifier_only_take(self):
        backend = darwin_backend("RightAlt")
        backend.modifier_changed(SHIFT_LEFT, keymonitor_darwin.MODIFIER_FLAGS["Shift"])
        backend.modifier_changed(OPTION_RIGHT,
                                 keymonitor_darwin.MODIFIER_FLAGS["Shift"]
                                 | keymonitor_darwin.MODIFIER_FLAGS["Alt"])
        self.assertEqual(backend.poll("RightAlt"), (False, False, False),
                         "another modifier already held means this is a chord")
        self.assertFalse(backend.take_cancel_requested())

    def test_a_modifier_pressed_second_cancels_an_existing_take(self):
        backend = darwin_backend("RightAlt")
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))
        backend.feed(SHIFT_LEFT, True)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        self.assertTrue(backend.take_cancel_requested(),
                        "the other ordering must avoid the chord too")

    def test_another_modifier_while_it_is_held_also_cancels(self):
        backend = darwin_backend("RightAlt")
        backend.feed(OPTION_RIGHT, True)
        backend.poll("RightAlt")
        backend.feed(SHIFT_LEFT, True)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        self.assertTrue(backend.take_cancel_requested())

    def test_a_modifier_pressed_after_a_key_never_starts_a_take(self):
        backend = darwin_backend("RightAlt")
        backend.feed(F["S"], True, modifiers=())
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        self.assertFalse(backend.take_cancel_requested())

    def test_the_next_press_still_works_after_an_avoided_chord(self):
        backend = darwin_backend("RightAlt")
        backend.feed(OPTION_RIGHT, True)
        backend.poll("RightAlt")
        backend.feed(F["S"], True, modifiers=("RightAlt",))
        backend.poll("RightAlt")
        backend.feed(F["S"], False, modifiers=("RightAlt",))
        backend.feed(OPTION_RIGHT, False)
        self.assertTrue(backend.take_cancel_requested())
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))

    def test_a_collision_outside_premiere_cannot_cancel_a_later_take(self):
        backend = darwin_backend("RightAlt")
        # Premiere is not in front, so the panel released hold-to-record; the
        # listen-only tap stays installed and keeps observing. The modifier is
        # then used as part of another application's chord.
        backend.set_armed(False)
        backend.feed(OPTION_RIGHT, True)
        backend.feed(F["S"], True, modifiers=("RightAlt",))
        self.assertFalse(backend.take_cancel_requested(),
                         "a chord outside Premiere must not be latched as a cancel")
        self.assertEqual(backend.poll("RightAlt"), (False, False, False))
        backend.feed(F["S"], False, modifiers=())
        backend.feed(OPTION_RIGHT, False)
        # Back in Premiere: the next take records normally and is not cancelled.
        backend.register("RightAlt")
        backend.feed(OPTION_RIGHT, True)
        self.assertEqual(backend.poll("RightAlt"), (True, True, True))
        self.assertFalse(backend.take_cancel_requested(),
                         "a stale collision must not discard this take")


class FakeUser32:
    def __init__(self):
        self.registrations = []
        self.unregistered = 0
        self.down: set[int] = set()
        self.registration_result = 1

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, virtual_key):
        self.registrations.append((hotkey_id, modifiers, virtual_key))
        return self.registration_result

    def UnregisterHotKey(self, hwnd, hotkey_id):
        self.unregistered += 1
        return 1

    def PeekMessageW(self, message, hwnd, low, high, remove):
        return 0

    def GetAsyncKeyState(self, virtual_key):
        return 0x8000 if virtual_key in self.down else 0


class WindowsBackendTests(unittest.TestCase):
    """F8 keeps its 1.0 registration; chords add modifiers to the same call."""

    def setUp(self):
        self.user32 = FakeUser32()
        patcher = patch.object(keymonitor_windows, "_user32", return_value=self.user32)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_f8_regression_uses_the_1_0_virtual_key_code(self):
        monitor = keymonitor_windows.WindowsKeyMonitor("F8")
        self.assertTrue(monitor.register("F8"))
        self.assertEqual(self.user32.registrations,
                         [(keymonitor_windows.HOTKEY_ID, keymonitor_windows.MOD_NOREPEAT, 0x77)])
        self.assertEqual(monitor.registered_key, "F8")
        self.assertTrue(monitor.armed)

    def test_a_configured_f9_still_works(self):
        monitor = keymonitor_windows.WindowsKeyMonitor("F9")
        monitor.register("F9")
        self.assertEqual(self.user32.registrations[0][2], 0x78)
        self.user32.down = {0x78}
        down, _pressed, message = monitor.poll("F9")
        self.assertTrue(down)
        self.assertFalse(message)
        monitor.close()
        self.assertEqual(self.user32.unregistered, 1)

    def test_a_chord_registers_its_modifiers(self):
        monitor = keymonitor_windows.WindowsKeyMonitor("Control+Shift+K")
        monitor.register("Control+Shift+K")
        hotkey_id, modifiers, virtual_key = self.user32.registrations[0]
        self.assertEqual(virtual_key, 0x4B)
        expected = (keymonitor_windows.MOD_CONTROL | keymonitor_windows.MOD_SHIFT
                    | keymonitor_windows.MOD_NOREPEAT)
        self.assertEqual(modifiers, expected)

    def test_the_hold_ends_when_a_required_modifier_is_released(self):
        monitor = keymonitor_windows.WindowsKeyMonitor("Control+K")
        monitor.register("Control+K")
        self.user32.down = {0x4B, 0xA2}
        self.assertEqual(monitor.poll("Control+K")[0], True)
        self.user32.down = {0x4B}  # control released, main key still down
        self.assertEqual(monitor.poll("Control+K")[0], False)

    def test_an_unexpected_modifier_does_not_start_the_take(self):
        monitor = keymonitor_windows.WindowsKeyMonitor("F8")
        monitor.register("F8")
        self.user32.down = {0x77, 0xA2}  # Ctrl+F8 with a plain F8 binding
        self.assertEqual(monitor.poll("F8"), (False, False, False))

    def test_changing_the_binding_re_registers_instead_of_reusing(self):
        monitor = keymonitor_windows.WindowsKeyMonitor("F8")
        monitor.register("F8")
        monitor.register("F9")
        self.assertEqual([entry[2] for entry in self.user32.registrations], [0x77, 0x78])
        self.assertEqual(self.user32.unregistered, 1)

    def test_a_refused_registration_is_reported_not_swallowed(self):
        self.user32.registration_result = 0
        monitor = keymonitor_windows.WindowsKeyMonitor("Control+K")
        self.assertFalse(monitor.register("Control+K"))
        self.assertIsNone(monitor.registered_key)
        self.assertFalse(monitor.armed)
        self.assertIn("RegisterHotKey returned 0", monitor.registration_error)
        # Even if a legacy caller polls anyway, an unowned shortcut is never held.
        self.user32.down = {0x4B, 0xA2}
        self.assertEqual(monitor.poll("Control+K"), (False, False, False))

    def test_observe_only_is_not_a_registration(self):
        monitor = keymonitor_windows.WindowsKeyMonitor("Control+K", register_hotkey=False)
        self.assertTrue(monitor.register("Control+K"))
        self.assertIsNone(monitor.registered_key)
        self.assertFalse(monitor.armed)
        self.assertEqual(self.user32.registrations, [])
        self.assertTrue(monitor.health()["observe_only"])
        self.assertIsNone(monitor.registration_error)


class WindowsRegistrationIntegrationTests(unittest.TestCase):
    """A worker must not record on a shortcut it failed to register."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ipc = self.root / "ipc"
        self.project = self.root / "Episode 01.prproj"
        self.project.write_bytes(b"fixture")
        self.user32 = FakeUser32()
        patcher = patch.object(keymonitor_windows, "_user32", return_value=self.user32)
        patcher.start()
        self.addCleanup(patcher.stop)
        recorder = patch("services.review_capture.ptt.MicrophoneRecorder", FakeRecorder)
        recorder.start()
        self.addCleanup(recorder.stop)
        self.worker = ReviewCaptureWorker(self.ipc)
        self.addCleanup(self.worker.shutdown)
        self.monitor = keymonitor_windows.WindowsKeyMonitor("F8")
        self.worker.ptt.backend = self.monitor
        front = patch.object(self.monitor, "frontmost", return_value=True)
        front.start()
        self.addCleanup(front.stop)

    def arm(self, **extra):
        payload = {
            "hotkey": "F8", "enabled": True, "project_path": str(self.project),
            "session_id": "session_windows_registration_fixture",
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
        }
        payload.update(extra)
        atomic_write_json(self.ipc / "ptt_arm.json", payload)

    def poll(self):
        self.worker.ptt.poll(panel_alive=True, manual_recording=False)

    def diagnostic(self):
        return json.loads((self.ipc / "ptt_key_diagnostic.json").read_text(encoding="utf-8"))

    def test_a_collision_is_visible_and_a_physically_down_key_starts_nothing(self):
        self.user32.registration_result = 0
        self.user32.down = {0x77}
        self.arm()
        self.poll()
        self.assertIsNone(self.worker.ptt.current,
                          "a key Cuthere does not own must not start a take")
        self.assertEqual(self.worker.ptt.registration["mode"], "failed")
        self.assertIsNone(self.worker.ptt.shortcut_snapshot["applied"])
        self.assertIsNone(self.monitor.registered_key)
        self.assertFalse(self.worker.ptt.armed)
        diagnostic = self.diagnostic()
        self.assertEqual(diagnostic["state"], "registration_failed")
        self.assertEqual(diagnostic["reason"], "hotkey_registration_failed")
        self.assertIn("RegisterHotKey", diagnostic["backend_health"]["registration_error"])

        # Recovery: owning the shortcut again is reported and the take starts.
        self.user32.registration_result = 1
        self.user32.down = set()
        self.poll()
        self.assertEqual(self.worker.ptt.registration["mode"], "registered")
        self.assertEqual(self.worker.ptt.shortcut_snapshot["applied"], "F8")
        self.assertEqual(self.diagnostic()["state"], "armed")
        self.user32.down = {0x77}
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "recording")
        self.user32.down = set()
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "complete")

    def test_an_active_take_is_stopped_and_saved_when_registration_is_lost(self):
        self.user32.registration_result = 1
        self.user32.down = {0x77}
        self.arm()
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "recording")
        capture_dir = Path(self.worker.ptt.current["capture_dir"])
        # The registration disappears while the key is still physically down.
        self.monitor.unregister()
        self.user32.registration_result = 0
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "complete",
                         "an unavailable registration must not leave a take stuck")
        self.assertTrue((capture_dir / "audio.wav").is_file(),
                        "the audio of the stopped take must be kept")
        self.assertIsNone(self.worker.ptt.shortcut_snapshot["applied"])
        self.assertEqual(self.worker.ptt.registration["mode"], "failed")

    def test_the_explicit_observe_only_mode_is_reported_separately(self):
        self.monitor = keymonitor_windows.WindowsKeyMonitor("F8", register_hotkey=False)
        self.worker.ptt.backend = self.monitor
        front = patch.object(self.monitor, "frontmost", return_value=True)
        front.start()
        self.addCleanup(front.stop)
        self.arm(register_hotkey=False)
        self.user32.down = set()
        self.poll()
        self.assertEqual(self.worker.ptt.registration["mode"], "observe_only")
        self.assertIsNone(self.worker.ptt.shortcut_snapshot["applied"])
        self.assertEqual(self.user32.registrations, [],
                         "observe-only mode must not call RegisterHotKey")
        diagnostic = self.diagnostic()
        self.assertEqual(diagnostic["state"], "observing")
        self.assertEqual(diagnostic["permission"], "observe_only")
        # The explicit diagnostic mode can still observe the physical key state.
        self.user32.down = {0x77}
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "recording")


class PushToTalkShortcutTests(unittest.TestCase):
    """The helper reads the one authoritative value and never swaps mid-take."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ipc = self.root / "ipc"
        self.project = self.root / "Episode 01.prproj"
        self.project.write_bytes(b"fixture")
        patcher = patch("services.review_capture.ptt.MicrophoneRecorder", FakeRecorder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.worker = ReviewCaptureWorker(self.ipc)
        self.addCleanup(self.worker._lock_handle.close)
        self.backend = ControllableBackend()
        self.worker.ptt.backend = self.backend
        self.store_path = self.worker.shortcut_store.path

    def arm(self, enabled=True):
        atomic_write_json(self.ipc / "ptt_arm.json", {
            "hotkey": "F8", "enabled": enabled, "project_path": str(self.project),
            "session_id": "session_shortcut_fixture_0001",
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
        })

    def poll(self):
        self.worker.ptt.poll(panel_alive=True, manual_recording=False)

    def test_the_documented_location_is_used_for_the_shared_file(self):
        self.assertEqual(self.store_path, self.root / shortcut.CONFIG_NAME)
        self.assertEqual(self.worker.ptt.shortcut_snapshot, {})

    def test_a_stored_binding_is_applied_without_the_arm_file(self):
        self.worker.shortcut_store.set("F9", source="cli")
        self.arm()
        self.poll()
        self.assertEqual(self.worker.ptt.shortcut_snapshot["binding"], "F9")

    def test_an_agent_change_is_picked_up_without_a_restart(self):
        self.worker.shortcut_store.set("F8", source="cli")
        self.arm()
        self.poll()
        self.assertEqual(self.backend.key, "F8")
        # A different process (the CLI) writes the file.
        shortcut.ShortcutStore(self.store_path).set("F9", source="cli")
        self.poll()
        self.assertEqual(self.worker.ptt.shortcut_snapshot["binding"], "F9")
        self.assertEqual(self.backend.key, "F9")

    def test_a_change_during_a_take_is_deferred_until_the_take_ends(self):
        self.worker.shortcut_store.set("F8", source="cli")
        self.arm()
        self.poll()
        self.backend.down = True
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "recording")
        self.assertEqual(self.worker.ptt.current["hotkey"], "F8")

        shortcut.ShortcutStore(self.store_path).set("Control+Shift+K", source="cli")
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "recording",
                         "the take must not be interrupted by the new binding")
        self.assertEqual(self.worker.ptt.current["hotkey"], "F8")
        self.assertEqual(self.worker.ptt.pending_binding, "Control+Shift+K")
        self.assertTrue(self.worker.ptt.shortcut_snapshot["pending"])
        self.assertEqual(self.worker.ptt.shortcut_snapshot["active_binding"], "F8")

        self.backend.down = False
        self.poll()
        self.assertEqual(self.worker.ptt.current["state"], "complete")
        self.assertEqual(self.worker.ptt.current["hotkey"], "F8")
        self.assertFalse(self.worker.ptt.pending_binding)

        self.backend.down = True
        self.poll()
        self.assertEqual(self.worker.ptt.current["hotkey"], "Control+Shift+K")

    def test_a_malformed_file_keeps_the_last_working_binding(self):
        self.worker.shortcut_store.set("F9", source="cli")
        self.worker.shortcut_store.set("F8", source="cli")
        self.store_path.write_text("{ half", encoding="utf-8")
        self.arm()
        self.poll()
        self.assertEqual(self.worker.ptt.shortcut_snapshot["binding"], "F8")
        self.assertEqual(self.worker.ptt.shortcut_snapshot["status"], "recovered")
        self.assertEqual(self.worker.ptt.shortcut_snapshot["recovered_from"],
                         shortcut.BACKUP_NAME)
        self.backend.down = True
        self.poll()
        self.assertEqual(self.worker.ptt.current["hotkey"], "F8")

    def test_status_json_publishes_the_effective_binding(self):
        self.worker.shortcut_store.set("F9", source="cli")
        self.arm()
        self.poll()
        self.worker._write_status()
        status = json.loads((self.ipc / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["shortcut"]["binding"], "F9")
        self.assertEqual(status["shortcut"]["effective"], "F9")
        self.assertEqual(Path(status["shortcut_config"]), self.store_path)

    def test_the_shortcut_actions_share_one_validation_and_one_file(self):
        report = self.worker._dispatch({"action": "shortcut_set",
                                        "payload": {"binding": "Control+Shift+K"}})
        self.assertEqual(report["binding"], "Control+Shift+K")
        # A fresh store had no file, so the value in force before the change is
        # this host's default: F8 on Windows, Right Option on macOS.
        self.assertEqual(report["previous_binding"],
                         shortcut.default_binding(report["platform"]).canonical)
        self.assertTrue(self.store_path.is_file())
        with self.assertRaises(shortcut.ShortcutError) as caught:
            self.worker._dispatch({"action": "shortcut_set",
                                   "payload": {"binding": "Escape"}})
        self.assertEqual(caught.exception.code, "reserved_escape")
        self.assertEqual(shortcut.ShortcutStore(self.store_path).load().binding.canonical,
                         "Control+Shift+K")

    def test_a_dry_run_through_the_bridge_writes_nothing(self):
        report = self.worker._dispatch({"action": "shortcut_set",
                                        "payload": {"binding": "F9", "dry_run": True}})
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["binding"], "F9")
        self.assertFalse(self.store_path.exists())
        # The preview is valid, but nothing was stored.
        self.assertEqual(report["status"], "ok")
        self.assertEqual(self.worker.shortcut_store.load(force=True).status, "default")

    def test_the_panel_migration_cannot_overwrite_a_newer_value(self):
        self.worker.shortcut_store.set("Control+Shift+K", source="cli")
        report = self.worker._dispatch({"action": "shortcut_migrate",
                                        "payload": {"legacy_value": "F9"}})
        self.assertFalse(report["migrated"])
        self.assertEqual(report["binding"], "Control+Shift+K")

    def test_an_explicit_legacy_f9_is_migrated_on_a_fresh_helper(self):
        report = self.worker._dispatch({"action": "shortcut_migrate",
                                        "payload": {"legacy_value": "F9"}})
        self.assertTrue(report["migrated"])
        self.assertEqual(report["binding"], "F9")
        self.assertEqual(report["source"], "migration")

    def test_reset_restores_the_platform_default(self):
        self.worker._dispatch({"action": "shortcut_set", "payload": {"binding": "F9"}})
        report = self.worker._dispatch({"action": "shortcut_reset", "payload": {}})
        self.assertEqual(report["binding"], shortcut.default_binding(report["platform"]).canonical)

    def test_an_avoided_chord_marks_the_capture_cancelled_without_a_note(self):
        # macOS-only behaviour, driven through the ptt layer with a Darwin backend.
        ptt = PushToTalk(self.ipc, backend=darwin_backend("RightAlt"),
                         shortcut_store=shortcut.ShortcutStore(self.root / "mac" / shortcut.CONFIG_NAME,
                                                               platform="darwin"),
                         platform="darwin")
        self.addCleanup(ptt.close)
        atomic_write_json(self.ipc / "ptt_arm.json", {
            "hotkey": "RightAlt", "enabled": True, "project_path": str(self.project),
            "session_id": "session_shortcut_fixture_mac",
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
        })
        # A DarwinKeyMonitor that never created a real tap reports "not
        # frontmost"; the frontmost check itself is covered elsewhere.
        with patch.object(ptt.backend, "frontmost", return_value=True):
            ptt.backend.feed(OPTION_RIGHT, True)
            ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(ptt.current["state"], "recording")
            self.assertEqual(ptt.current["hotkey"], "RightAlt")
            ptt.backend.feed(F["S"], True, modifiers=("RightAlt",))
            ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(ptt.current["state"], "cancelled")
        self.assertEqual(ptt.current["cancel_reason"], "modifier_chord_avoided")
        self.assertTrue((Path(ptt.current["capture_dir"]) / "audio.wav").is_file(),
                        "the audio of an avoided take is kept for audit")
        self.assertFalse((self.ipc / "notes").exists(),
                         "an avoided chord must not create a review note")

    def test_a_collision_is_not_lost_when_premiere_loses_focus_at_the_same_moment(self):
        ptt = PushToTalk(self.ipc, backend=darwin_backend("RightAlt"),
                         shortcut_store=shortcut.ShortcutStore(self.root / "mac" / shortcut.CONFIG_NAME,
                                                               platform="darwin"),
                         platform="darwin")
        self.addCleanup(ptt.close)
        atomic_write_json(self.ipc / "ptt_arm.json", {
            "hotkey": "RightAlt", "enabled": True, "project_path": str(self.project),
            "session_id": "session_shortcut_fixture_focus",
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
        })
        with patch.object(ptt.backend, "frontmost", return_value=True):
            ptt.backend.feed(OPTION_RIGHT, True)
            ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(ptt.current["state"], "recording")
        # The modifier becomes part of another chord just as Premiere loses
        # focus. The collision is read before the disarm boundary clears it, so
        # the take is discarded instead of saved as a note.
        ptt.backend.feed(F["S"], True, modifiers=("RightAlt",))
        with patch.object(ptt.backend, "frontmost", return_value=False):
            ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(ptt.current["state"], "cancelled")
        self.assertFalse((self.ipc / "notes").exists())


if __name__ == "__main__":
    unittest.main()
