from __future__ import annotations

import hashlib
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from services.review_capture.audio import _finalize_raw_pcm, recover_interrupted_capture
from services.review_capture.storage import atomic_write_json, read_json, sha256_file
from services.review_capture.timebase import normalize_interval, parse_timecode, ticks_to_frame
from services.review_capture.worker import ReviewCaptureWorker


class FakeRecorder:
    start_count = 0

    def __init__(self, output_dir: Path, device: dict, sd_module=None):
        self.output_dir = output_dir
        self.samples = 0
        self.started_monotonic = 0.0
        self.last_level = 0.2
        self.final_status = "saved"

    def start(self):
        type(self).start_count += 1
        self.samples = 4800
        return {"sample_rate": 48000, "sample_rate_input": 48000, "samples_input": self.samples, "channels": 1, "duration_seconds": 0.1, "device": {"name": "Fixture mic"}}

    def stop(self, status="saved"):
        path = self.output_dir / "audio.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(b"\0\0" * 4800)
        payload = path.read_bytes()
        return {"path": "audio.wav", "sha256": hashlib.sha256(payload).hexdigest(), "samples": 4800, "samples_input": 4800, "sample_rate": 48000, "channels": 1, "duration_seconds": 0.1, "dropped_frames": 0, "input_overflows": 0, "capture_status": self.final_status}

    def summary(self):
        return {"samples_input": self.samples, "dropped_frames": 0}


class FakeKeyBackend:
    """Platform-neutral stand-in for a key backend (Windows or macOS)."""

    backend = "fake"

    def __init__(self, down: bool = True, foreground: bool = True, press_on_register: bool = True):
        self.down = down
        self.foreground = foreground
        self.register_accepts = press_on_register
        self.registered: list[str] = []
        self.registered_key = None
        self.pending_press = False

    @property
    def armed(self) -> bool:
        return self.registered_key is not None

    def set_key(self, key: str) -> None:
        self.key = key

    def set_armed(self, armed: bool) -> None:
        if not armed:
            self.registered_key = None

    def register(self, key: str) -> bool:
        if not self.register_accepts:
            return False
        if self.registered_key != key:
            self.registered.append(key)
        self.registered_key = key
        return True

    def unregister(self) -> None:
        self.registered_key = None

    def frontmost(self) -> bool:
        return self.foreground

    def poll(self, key: str):
        pressed = self.pending_press
        self.pending_press = False
        return self.down, pressed, pressed

    def poll_for(self, key: str, foreground: bool):
        down, pressed, message = self.poll(key)
        return (down and foreground), pressed, message

    def close(self) -> None:
        self.registered_key = None


class ReviewCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "SampleProject.prproj"
        self.project.write_bytes(b"test fixture only")
        self.worker = ReviewCaptureWorker(self.root / "ipc")
        self.patch_recorder = patch("services.review_capture.worker.MicrophoneRecorder", FakeRecorder)
        self.patch_recorder.start()
        self.addCleanup(self.worker._lock_handle.close)
        self.addCleanup(self.patch_recorder.stop)

    def snapshot(self, ticks: str, *, sequence: str = "seq-1", fingerprint: str = "fnv32x2:a1b2c3d4"):
        return {
            "read_at": "2026-10-14T10:00:00Z",
            "project": {"name": "SampleProject", "path": str(self.project), "host_id": "project-1"},
            "sequence": {"name": "Sequence 01", "host_id": sequence, "timebase": "8475667200", "frame_rate": {"numerator": 30000, "denominator": 1001}},
            "location": {"ticks": str(ticks), "time_label": "00:00:00:00"},
            "content_fingerprint": fingerprint,
            "content_fingerprint_status": "complete",
        }

    def start_range(self, ticks="5080320000000"):
        return self.worker._start({
            "mode": "range",
            "snapshot": self.snapshot(ticks),
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
            "session_id": "session_fixture_0001",
            "note_id": "note_fixture_0001",
            "operation_id": "start_operation_0001",
        })

    def prepared_ptt_capture(self, capture_id="capture_drag_fixture_0001"):
        capture_dir = self.worker.ipc_root / "ptt" / capture_id
        capture_dir.mkdir(parents=True)
        audio = FakeRecorder(capture_dir, {}).stop()
        self.worker.ptt.current = {
            "id": capture_id, "state": "complete", "project_path": str(self.project),
            "session_id": "session_drag_fixture_0001", "capture_dir": str(capture_dir),
            "audio": audio, "hotkey": "F8", "pressed_at": "2026-10-15T01:00:00Z",
            "released_at": "2026-10-15T01:00:05Z",
        }
        return capture_id

    def test_ptt_playhead_drag_saves_both_endpoint_snapshots(self):
        capture_id = self.prepared_ptt_capture()
        result = self.worker._ptt_commit({
            "capture_id": capture_id,
            "snapshot": self.snapshot("5080320000000"),
            "end_snapshot": self.snapshot("8890560000000"),
        })
        self.assertEqual(result["mode"], "range")
        location = result["location"]
        self.assertEqual(location["range_method"], "drag_playhead")
        self.assertEqual(location["normalized"]["start_ticks"], "5080320000000")
        self.assertEqual(location["normalized"]["end_ticks"], "8890560000000")
        note = read_json(Path(result["note_path"]))
        self.assertNotEqual(note["snapshots"]["start"], note["snapshots"]["end"])
        self.assertTrue((Path(result["note_path"]).parent / note["snapshots"]["end"]["path"]).is_file())

    def test_ptt_reverse_playhead_drag_keeps_direction(self):
        capture_id = self.prepared_ptt_capture()
        result = self.worker._ptt_commit({
            "capture_id": capture_id,
            "snapshot": self.snapshot("8890560000000"),
            "end_snapshot": self.snapshot("5080320000000"),
        })
        self.assertEqual(result["location"]["normalized"]["direction"], "reverse")

    def test_ptt_release_preview_is_marked_partial(self):
        capture_id = self.prepared_ptt_capture()
        end = self.snapshot("8890560000000")
        end["capture_quality"] = "release_preview_only"
        result = self.worker._ptt_commit({
            "capture_id": capture_id,
            "snapshot": self.snapshot("5080320000000"),
            "end_snapshot": end,
        })
        self.assertEqual(result["location"]["version_status"], "partial")
        self.assertFalse(result["location"]["automatic_edit_allowed"])

    def test_ptt_selected_clips_still_take_priority_over_playhead_drag(self):
        capture_id = self.prepared_ptt_capture()
        start = self.snapshot("5080320000000")
        start["selection"] = [{"start_ticks": "3000000000000", "end_ticks": "4000000000000"}]
        result = self.worker._ptt_commit({
            "capture_id": capture_id, "snapshot": start,
            "end_snapshot": self.snapshot("8890560000000"),
        })
        self.assertEqual(result["location"]["range_method"], "selected_clips")
        self.assertEqual(result["location"]["normalized"]["start_ticks"], "3000000000000")

    def test_ptt_drag_rejects_a_different_sequence_and_preserves_audio(self):
        capture_id = self.prepared_ptt_capture()
        with self.assertRaisesRegex(ValueError, "不属于同一时间线"):
            self.worker._ptt_commit({
                "capture_id": capture_id,
                "snapshot": self.snapshot("5080320000000"),
                "end_snapshot": self.snapshot("8890560000000", sequence="other-sequence"),
            })
        self.assertTrue((self.worker.ipc_root / "ptt" / capture_id / "audio.wav").is_file())

    def test_drag_interval_uses_only_start_and_confirmed_end(self):
        started = self.start_range()
        note_id = started["note_id"]
        result = self.worker._lock_end({"note_id": note_id, "operation_id": "lock_operation_001", "snapshot": self.snapshot("8890560000000")})
        self.assertEqual(result["location"]["range_status"], "locked")
        self.assertEqual(result["location"]["normalized"]["start_ticks"], "5080320000000")
        self.assertEqual(result["location"]["normalized"]["end_ticks"], "8890560000000")

        stopped = self.worker._stop({"note_id": note_id, "operation_id": "stop_operation_001"})
        self.assertEqual(stopped["location"]["endpoints"]["end"]["ticks"], "8890560000000")
        self.assertEqual(stopped["status"]["recording"], "saved")

    def test_direct_finish_saves_audio_before_endpoint_attach(self):
        started = self.start_range()
        note_id = started["note_id"]
        stopped = self.worker._stop({"note_id": note_id, "operation_id": "stop_operation_002"})
        self.assertEqual(stopped["status"]["recording"], "saved")
        self.assertEqual(stopped["location"]["range_status"], "needs_endpoint")
        self.assertTrue(Path(stopped["note_path"]).is_file())

        attached = self.worker._attach_endpoint({
            "note_id": note_id,
            "note_path": stopped["note_path"],
            "operation_id": "stop_operation_002",
            "snapshot": self.snapshot("8890560000000"),
        })
        self.assertEqual(attached["location"]["range_status"], "locked")
        self.assertEqual(attached["location"]["endpoints"]["end"]["ticks"], "8890560000000")

    def test_reverse_drag_is_normalized_and_direction_is_retained(self):
        started = self.start_range("8890560000000")
        result = self.worker._lock_end({"note_id": started["note_id"], "operation_id": "lock_operation_002", "snapshot": self.snapshot("5080320000000")})
        normalized = result["location"]["normalized"]
        self.assertEqual(normalized["start_ticks"], "5080320000000")
        self.assertEqual(normalized["end_ticks"], "8890560000000")
        self.assertEqual(normalized["direction"], "reverse")

    def test_partial_timeline_fingerprint_locks_range_but_requires_manual_review(self):
        started_snapshot = self.snapshot("5080320000000")
        started_snapshot["content_fingerprint_status"] = "partial"
        started = self.worker._start({
            "mode": "range", "snapshot": started_snapshot,
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
            "session_id": "session_partial_0001", "note_id": "note_partial_0001",
            "operation_id": "start_partial_0001",
        })
        end_snapshot = self.snapshot("8890560000000")
        end_snapshot["content_fingerprint_status"] = "partial"
        locked = self.worker._lock_end({
            "note_id": started["note_id"], "operation_id": "lock_partial_0001", "snapshot": end_snapshot,
        })
        self.assertEqual(locked["location"]["range_status"], "locked")
        self.assertEqual(locked["location"]["version_status"], "partial")
        self.assertFalse(locked["location"]["automatic_edit_allowed"])
        self.worker._stop({"note_id": started["note_id"], "operation_id": "stop_partial_0001"})

    def test_missing_timeline_fingerprint_does_not_lock_range(self):
        started_snapshot = self.snapshot("5080320000000")
        started_snapshot["content_fingerprint"] = None
        started = self.worker._start({
            "mode": "range", "snapshot": started_snapshot,
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
            "session_id": "session_missing_0001", "note_id": "note_missing_0001",
            "operation_id": "start_missing_0001",
        })
        end_snapshot = self.snapshot("8890560000000")
        end_snapshot["content_fingerprint"] = None
        result = self.worker._lock_end({
            "note_id": started["note_id"], "operation_id": "lock_missing_0001", "snapshot": end_snapshot,
        })
        self.assertEqual(result["location"]["range_status"], "stale")
        self.assertEqual(result["location"]["stale_reason"], "content_fingerprint_unavailable")
        self.worker._stop({"note_id": started["note_id"], "operation_id": "stop_missing_0001"})

    def test_duplicate_request_id_does_not_start_a_second_recording(self):
        command = {
            "request_id": "request_dedupe_0001",
            "action": "start",
            "payload": {
                "mode": "point", "snapshot": self.snapshot("5080320000000"),
                "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
                "session_id": "session_dedupe_0001", "note_id": "note_dedupe_0001",
                "operation_id": "start_dedupe_0001",
            },
        }
        inbox_file = self.worker.inbox / "first.json"
        atomic_write_json(inbox_file, command)
        before = FakeRecorder.start_count
        self.worker._process_file(inbox_file)
        first = read_json(self.worker.outbox / "request_dedupe_0001.json")
        self.assertTrue(first["ok"])
        self.assertEqual(FakeRecorder.start_count, before + 1)

        duplicate = self.worker.inbox / "duplicate.json"
        atomic_write_json(duplicate, command)
        self.worker._process_file(duplicate)
        second = read_json(self.worker.outbox / "request_dedupe_0001.json")
        self.assertEqual(second, first)
        self.assertEqual(FakeRecorder.start_count, before + 1)
        self.worker._stop({"note_id": "note_dedupe_0001", "operation_id": "stop_dedupe_0001"})

    def test_package_creates_bundle_and_current_handoff_pointer(self):
        started = self.start_range()
        stopped = self.worker._stop({"note_id": started["note_id"], "operation_id": "stop_package_0001"})
        result = self.worker._package({"project_path": str(self.project), "session_id": started["session_id"]})
        bundle = Path(result["path"])
        self.assertTrue(bundle.is_dir())
        self.assertTrue((bundle / "审片意见清单.md").is_file())
        self.assertEqual(sha256_file(bundle / "manifest.json"), result["manifest_sha256"])
        self.assertTrue(Path(stopped["note_path"]).is_file(), "packaging must preserve the editable work record")
        pointer = read_json(Path(result["handoff_pointer"]))
        self.assertEqual(pointer["bundle_path"], str(bundle.resolve()))
        self.assertEqual(pointer["manifest_sha256"], result["manifest_sha256"])
        intake = read_json(Path(result["decision_intake"]))
        self.assertEqual(intake["manifest_sha256"], result["manifest_sha256"])
        self.assertEqual(len(intake["notes"]), 1)
        self.assertEqual(intake["notes"][0]["status"], "PENDING_TIMELINE_DRY_RUN")

    def test_audio_writer_warning_never_appears_as_fully_saved(self):
        started = self.start_range()
        self.worker.recorder.final_status = "interrupted"
        stopped = self.worker._stop({"note_id": started["note_id"], "operation_id": "stop_interrupted_0001"})
        self.assertEqual(stopped["status"]["recording"], "interrupted")
        self.assertTrue(Path(stopped["note_path"]).is_file())

    def test_content_or_sequence_change_does_not_lock_range(self):
        started = self.start_range()
        changed_sequence = self.worker._lock_end({"note_id": started["note_id"], "operation_id": "lock_operation_003", "snapshot": self.snapshot("8890560000000", sequence="seq-2")})
        self.assertEqual(changed_sequence["location"]["range_status"], "stale")
        self.assertEqual(changed_sequence["location"]["stale_reason"], "sequence_changed")

    def test_unlocked_response_cannot_attach_after_reselection(self):
        started = self.start_range()
        note_id = started["note_id"]
        stopped = self.worker._stop({"note_id": note_id, "operation_id": "stop_operation_003"})
        note = read_json(Path(stopped["note_path"]))
        note["location"]["pending_end_operation_id"] = "newer_operation_0001"
        from services.review_capture.storage import atomic_write_json
        atomic_write_json(Path(stopped["note_path"]), note)
        with self.assertRaisesRegex(RuntimeError, "已过期"):
            self.worker._attach_endpoint({
                "note_id": note_id,
                "note_path": stopped["note_path"],
                "operation_id": "stop_operation_003",
                "snapshot": self.snapshot("8890560000000"),
            })

    def test_interval_normalization_and_drop_frame_conversion(self):
        self.assertEqual(normalize_interval(9, 3)["direction"], "reverse")
        self.assertFalse(normalize_interval(3, 3)["valid"])
        ticks = parse_timecode("00:01:00;02", (30000, 1001))
        self.assertEqual(ticks_to_frame(ticks, (30000, 1001)), 1800)

    def test_raw_pcm_finalizes_and_crash_recovery_writes_readable_wave(self):
        folder = self.root / "audio_fixture"
        folder.mkdir()
        raw = folder / "audio.capture.pcm"
        raw.write_bytes((b"\x10\x00" * 4410))
        result = recover_interrupted_capture(folder, 44100)
        self.assertGreater(result["samples"], 0)
        with wave.open(str(folder / "audio.wav"), "rb") as audio:
            self.assertEqual(audio.getframerate(), 48000)
            self.assertEqual(audio.getnchannels(), 1)

    def test_push_to_talk_holds_until_key_release(self):
        state = FakeKeyBackend(down=True)
        atomic_write_json(self.worker.ipc_root / "ptt_arm.json", {
            "hotkey": "F8", "enabled": True,
            "project_path": str(self.project), "session_id": "session_fixture_0001",
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
        })
        with patch("services.review_capture.ptt.MicrophoneRecorder", FakeRecorder):
            self.worker.ptt.backend = state
            self.worker.ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(self.worker.ptt.current["state"], "recording")
            self.worker.ptt.poll(panel_alive=True, manual_recording=False)
            self.assertEqual(self.worker.ptt.current["state"], "recording")
            state.down = False
            self.worker.ptt.poll(panel_alive=True, manual_recording=False)
        self.assertEqual(self.worker.ptt.current["state"], "complete")
        self.assertTrue((Path(self.worker.ptt.current["capture_dir"]) / "audio.wav").is_file())
        self.assertEqual(state.registered, ["F8"])

    def test_new_f8_after_unresolved_range_preserves_old_audio_and_starts_recording(self):
        old_id = self.prepared_ptt_capture("capture_stuck_fixture_0001")
        self.worker.ptt._save()
        old_audio = Path(self.worker.ptt.current["capture_dir"]) / "audio.wav"

        state = FakeKeyBackend(down=True)
        atomic_write_json(self.worker.ipc_root / "ptt_arm.json", {
            "hotkey": "F8", "enabled": True,
            "project_path": str(self.project), "session_id": "session_drag_fixture_0001",
            "device": {"index": 0, "key": "fixture", "name": "Fixture mic"},
        })
        with patch("services.review_capture.ptt.MicrophoneRecorder", FakeRecorder):
            self.worker.ptt.backend = state
            self.worker.ptt.poll(panel_alive=True, manual_recording=False)

        self.assertNotEqual(self.worker.ptt.current["id"], old_id)
        self.assertEqual(self.worker.ptt.current["state"], "recording")
        self.assertTrue(old_audio.is_file())
        self.assertTrue((self.worker.ptt.capture_root / "pending" / f"{old_id}.json").is_file())
        new_id = self.worker.ptt.current["id"]
        recovered = self.worker._ptt_commit({
            "capture_id": old_id,
            "snapshot": self.snapshot("5080320000000"),
            "end_snapshot": self.snapshot("8890560000000"),
        })
        self.assertEqual(recovered["mode"], "range")
        self.assertEqual(self.worker.ptt.current["id"], new_id)
        self.assertFalse((self.worker.ptt.capture_root / "pending" / f"{old_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
