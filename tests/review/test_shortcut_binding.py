"""One authoritative recording shortcut, shared by panel, helper and CLI.

The grammar and the storage layer are tested here; the platform event handling
is in ``tests/platform/test_shortcut_backends.py``. Both suites are logic tests:
they prove what the code does with the events it is given, not that macOS or
Windows delivers them, and not that a microphone captured anything.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services.review_capture import shortcut  # noqa: E402
from scripts import shortcut as shortcut_cli  # noqa: E402

VECTORS = json.loads(
    (REPOSITORY_ROOT / "tests" / "fixtures" / "shortcut_vectors.json").read_text(encoding="utf-8"))


class GrammarTests(unittest.TestCase):
    """The supported grammar is small, explicit and identical in the panel."""

    def test_shared_vectors_agree_with_the_parser(self):
        self.assertTrue(VECTORS["cases"])
        for row in VECTORS["cases"]:
            for platform in ("darwin", "win32"):
                expected = row[platform]
                with self.subTest(binding=row["input"], platform=platform):
                    if "code" in expected:
                        with self.assertRaises(shortcut.ShortcutError) as caught:
                            shortcut.parse(row["input"], platform=platform)
                        self.assertEqual(caught.exception.code, expected["code"])
                        continue
                    parsed = shortcut.parse(row["input"], platform=platform)
                    self.assertEqual(parsed.canonical, expected["binding"])
                    self.assertEqual(parsed.display(platform), expected["display"])

    def test_fresh_default_is_right_option_on_macos_and_f8_on_windows(self):
        self.assertEqual(shortcut.default_binding("darwin").canonical, "RightAlt")
        self.assertEqual(shortcut.default_binding("darwin").display("darwin"), "右 Option")
        self.assertEqual(shortcut.default_binding("win32").canonical, "F8")
        self.assertEqual(shortcut.default_binding("win32").display("win32"), "F8")

    def test_modifier_order_is_host_independent(self):
        self.assertEqual(shortcut.parse("shift+control+k").canonical, "Control+Shift+K")
        self.assertEqual(shortcut.parse("cmd+alt+shift+ctrl+k").canonical,
                         "Control+Alt+Shift+Meta+K")

    def test_aliases_accept_the_words_a_user_or_agent_would_say(self):
        for text in ("RightOption", "right option", "RIGHT_OPTION", "RightAlt", "ropt"):
            self.assertEqual(shortcut.parse(text, platform="darwin").canonical, "RightAlt", text)
        self.assertEqual(shortcut.parse("Option+S", platform="darwin").canonical, "Alt+S")
        self.assertEqual(shortcut.parse("Cmd+K", platform="win32").canonical, "Meta+K")

    def test_old_f_keys_stay_usable(self):
        for name in ("F8", "F9", "F10", "F11"):
            self.assertEqual(shortcut.parse(name).canonical, name)
            self.assertEqual(shortcut.parse(name).legacy_key(), name)

    def test_f12_is_reserved_on_windows_but_bindable_on_macos(self):
        self.assertEqual(shortcut.parse("F12", platform="darwin").canonical, "F12")
        for text in ("F12", "Control+F12", "Shift+Alt+F12"):
            with self.assertRaises(shortcut.ShortcutError) as caught:
                shortcut.parse(text, platform="win32")
            self.assertEqual(caught.exception.code, "reserved_windows_f12", text)
            self.assertIn("调试器", caught.exception.message)
        self.assertNotIn("F12", shortcut.describe_support("win32")["base_keys"])
        self.assertIn("F12", shortcut.describe_support("darwin")["base_keys"])
        self.assertIn("F12", shortcut.describe_support("win32")["reserved_keys"])
        self.assertNotIn("F12", shortcut.describe_support("darwin")["reserved_keys"])

    def test_modifier_only_and_sided_chords_are_macos_only(self):
        with self.assertRaises(shortcut.ShortcutError) as caught:
            shortcut.parse("RightOption", platform="win32")
        self.assertEqual(caught.exception.code, "modifier_only_unsupported")
        self.assertIn("macOS", caught.exception.message)
        with self.assertRaises(shortcut.ShortcutError) as caught:
            shortcut.parse("RightShift+K", platform="win32")
        self.assertEqual(caught.exception.code, "sided_modifier_unsupported")
        # The same right-hand chord is fine on macOS.
        self.assertEqual(shortcut.parse("RightShift+K", platform="darwin").canonical,
                         "RightShift+K")

    def test_a_generic_modifier_cannot_be_a_standalone_shortcut(self):
        with self.assertRaises(shortcut.ShortcutError) as caught:
            shortcut.parse("Option", platform="darwin")
        self.assertEqual(caught.exception.code, "modifier_only_not_sided")
        with self.assertRaises(shortcut.ShortcutError) as caught:
            shortcut.parse("Control+Shift", platform="darwin")
        self.assertEqual(caught.exception.code, "ambiguous_modifier_only")

    def test_escape_and_the_globe_key_are_reserved(self):
        for text, code in (("Escape", "reserved_escape"), ("Fn", "reserved_fn"), ("Globe", "reserved_fn")):
            with self.assertRaises(shortcut.ShortcutError) as caught:
                shortcut.parse(text, platform="darwin")
            self.assertEqual(caught.exception.code, code, text)

    def test_rejections_are_specific_not_a_silent_f8(self):
        cases = {
            "": "empty",
            "Ctrl+": "empty_token",
            "Control+Ctrl+K": "duplicate_modifier",
            "A+B": "multiple_keys",
            "F13": "unsupported_token",
            "Hyper+K": "unsupported_token",
        }
        for text, code in cases.items():
            with self.assertRaises(shortcut.ShortcutError) as caught:
                shortcut.parse(text, platform="darwin")
            self.assertEqual(caught.exception.code, code, text)
            self.assertTrue(caught.exception.message)
            self.assertNotIn("F8", caught.exception.message)

    def test_every_known_conflict_is_a_bindable_value(self):
        # An entry the parser rejects could never fire, so the warning list
        # cannot drift into describing a key the product does not support.
        for text in shortcut.KNOWN_CONFLICTS:
            for platform in ("darwin", "win32"):
                shortcut.parse(text, platform=platform)

    def test_conflicts_warn_but_never_block(self):
        binding = shortcut.parse("Meta+Space", platform="darwin")
        notes = shortcut.conflicts(binding, platform="darwin")
        self.assertTrue(any("Spotlight" in note for note in notes))
        self.assertTrue(shortcut.conflicts(shortcut.parse("F8", platform="win32"),
                                           platform="win32"))
        # A plain function key is not a known system conflict; it only carries
        # the honest "the tap does not suppress the key" note.
        plain = shortcut.conflicts(shortcut.parse("F9", platform="darwin"), platform="darwin")
        self.assertTrue(plain)
        self.assertFalse(any("常被系统或应用占用" in note for note in plain))

    def test_darwin_listen_only_limit_is_stated(self):
        notes = shortcut.conflicts(shortcut.parse("F9", platform="darwin"), platform="darwin")
        self.assertTrue(any("只观察" in note for note in notes),
                        "the panel must not imply the tap suppresses the key")

    def test_describe_support_matches_what_the_backends_accept(self):
        mac = shortcut.describe_support("darwin")
        self.assertTrue(mac["modifier_only_allowed"])
        self.assertTrue(mac["sided_modifiers_allowed"])
        windows = shortcut.describe_support("win32")
        self.assertFalse(windows["modifier_only_allowed"])
        self.assertFalse(windows["sided_modifiers_allowed"])
        self.assertIn("F8", windows["base_keys"])
        self.assertEqual(windows["reserved_keys"], ["Escape", "F12", "Fn"])
        for platform in ("darwin", "win32"):
            # Every advertised base key really parses.
            for name in shortcut.describe_support(platform)["base_keys"]:
                shortcut.parse(name, platform=platform)


class StoreTests(unittest.TestCase):
    """Persistence, migration, reset and the corrupt-file guarantee."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "shortcut.json"

    def store(self, platform="darwin"):
        return shortcut.ShortcutStore(self.path, platform=platform)

    def test_first_run_reports_the_platform_default_and_writes_nothing(self):
        state = self.store().load()
        self.assertEqual(state.binding.canonical, "RightAlt")
        self.assertEqual(state.status, "default")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store("win32").load().binding.canonical, "F8")

    def test_set_persists_the_canonical_value_for_every_reader(self):
        store = self.store()
        state = store.set("right option", source="panel")
        self.assertEqual(state.binding.canonical, "RightAlt")
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(record["schema_version"], "1.0")
        self.assertEqual(record["binding"], "RightAlt")
        self.assertEqual(record["source"], "panel")
        self.assertEqual(record["previous_binding"], "RightAlt")
        # A different object (another process) reads the same value back.
        again = shortcut.ShortcutStore(self.path, platform="darwin").load()
        self.assertEqual(again.binding.canonical, "RightAlt")
        self.assertEqual(again.status, "ok")

    def test_reset_returns_to_the_platform_default_and_keeps_history(self):
        store = self.store()
        store.set("Control+Shift+K")
        state = store.reset(source="reset")
        self.assertEqual(state.binding.canonical, "RightAlt")
        self.assertEqual(store.load().source, "reset")
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(record["previous_binding"], "Control+Shift+K")
        self.assertEqual([entry["binding"] for entry in record["history"]],
                         ["Control+Shift+K", "RightAlt"])

    def test_restore_previous_undoes_the_last_change(self):
        store = self.store()
        store.set("F9", source="cli")
        store.set("Control+Shift+K", source="cli")
        state = store.restore_previous()
        self.assertEqual(state.binding.canonical, "F9")

    def test_restore_previous_without_history_is_rejected(self):
        with self.assertRaises(shortcut.ShortcutError) as caught:
            self.store().restore_previous()
        self.assertEqual(caught.exception.code, "no_previous_binding")

    def test_an_explicit_legacy_f9_is_migrated_once(self):
        store = self.store()
        state = store.migrate_legacy("F9")
        self.assertIsNotNone(state)
        self.assertEqual(state.binding.canonical, "F9")
        self.assertEqual(state.source, "migration")
        record = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(record["migrated_from"],
                         "localStorage:autovideoedit.review.ptt.hotkey")
        # A second migration call is a no-op: the file now wins.
        self.assertIsNone(store.migrate_legacy("F8"))

    def test_no_legacy_value_keeps_the_mac_default(self):
        store = self.store()
        self.assertIsNone(store.migrate_legacy(""))
        self.assertIsNone(store.migrate_legacy(None))
        self.assertFalse(self.path.exists())
        self.assertEqual(store.load().binding.canonical, "RightAlt")

    def test_migration_never_overwrites_a_newer_setting(self):
        store = self.store()
        store.set("Control+Shift+K", source="cli")
        self.assertIsNone(store.migrate_legacy("F9"))
        self.assertEqual(store.load().binding.canonical, "Control+Shift+K")

    def test_invalid_input_does_not_touch_the_file(self):
        store = self.store()
        store.set("F9")
        before = self.path.read_text(encoding="utf-8")
        with self.assertRaises(shortcut.ShortcutError):
            store.set("Escape")
        with self.assertRaises(shortcut.ShortcutError):
            store.set("NotAKey+Y")
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(store.load().binding.canonical, "F9")

    def test_dry_run_previews_without_writing(self):
        store = self.store()
        state = store.set("Control+Shift+K", dry_run=True)
        self.assertEqual(state.binding.canonical, "Control+Shift+K")
        self.assertFalse(self.path.exists())
        self.assertEqual(store.load().status, "default")

    def test_a_corrupt_file_falls_back_to_the_last_successfully_effective_value(self):
        store = self.store()
        store.set("Control+Shift+K", source="cli")
        store.set("F9", source="cli")
        self.assertTrue((self.root / shortcut.BACKUP_NAME).is_file())
        # The backup is the value that was last in force, not the one before it.
        backup = json.loads((self.root / shortcut.BACKUP_NAME).read_text(encoding="utf-8"))
        self.assertEqual(backup["binding"], "F9")
        self.path.write_text("{ this is not json", encoding="utf-8")
        fresh = shortcut.ShortcutStore(self.path, platform="darwin")
        state = fresh.load()
        self.assertEqual(state.status, "recovered")
        self.assertEqual(state.binding.canonical, "F9",
                         "the latest successfully effective setting must survive")
        self.assertEqual(state.recovered_from, shortcut.BACKUP_NAME)
        self.assertTrue(state.problems)

    def test_multiple_successful_updates_then_corruption_keep_the_latest_value(self):
        # Codex's reproducer, both through a warm store and a fresh one.
        store = shortcut.ShortcutStore(self.path, platform="win32")
        store.set("F9", source="cli")
        store.set("F10", source="cli")
        self.assertEqual(store.load().binding.canonical, "F10")
        self.path.write_text("{broken", encoding="utf-8")
        warm = store.load()
        self.assertEqual((warm.status, warm.binding.canonical), ("recovered", "F10"))
        self.assertEqual(warm.previous_binding, "F9",
                         "the undo value stays separate from corruption recovery")
        fresh = shortcut.ShortcutStore(self.path, platform="win32").load()
        self.assertEqual((fresh.status, fresh.binding.canonical), ("recovered", "F10"))
        self.assertTrue(fresh.problems)
        # restore-previous still uses the recorded previous value, not a default.
        restored = store.restore_previous()
        self.assertEqual(restored.binding.canonical, "F9")

    def test_a_valid_but_unsupported_value_is_also_recovered(self):
        store = self.store()
        store.set("F9")
        store.set("F10")  # a valid latest value, so the backup holds it
        self.path.write_text(json.dumps({"binding": "Escape"}), encoding="utf-8")
        state = shortcut.ShortcutStore(self.path, platform="darwin").load()
        self.assertEqual(state.status, "recovered")
        self.assertEqual(state.binding.canonical, "F10")

    def test_corruption_without_a_backup_reports_the_default(self):
        self.path.write_text("not json at all", encoding="utf-8")
        state = self.store().load()
        self.assertEqual(state.status, "default")
        self.assertEqual(state.binding.canonical, "RightAlt")
        self.assertTrue(state.problems)

    def test_a_change_from_another_writer_is_picked_up(self):
        first = self.store()
        first.load()
        self.assertEqual(first.load().binding.canonical, "RightAlt")
        second = shortcut.ShortcutStore(self.path, platform="darwin")
        second.set("Control+Shift+K", source="cli")
        # The cached reader must notice the new mtime, not keep its old value.
        self.assertEqual(first.load().binding.canonical, "Control+Shift+K")
        self.assertEqual(first.load().source, "cli")

    def test_the_config_location_follows_the_documented_layout(self):
        runtime = self.root / "runtime"
        ipc = runtime / "ipc" / "f8-1.1.0-alpha.5"
        ipc.mkdir(parents=True)
        store = shortcut.ShortcutStore.for_ipc_root(ipc)
        self.assertEqual(store.path, runtime / shortcut.CONFIG_NAME)
        portable = shortcut.ShortcutStore.for_ipc_root(self.root / "portable")
        self.assertEqual(portable.path, self.root / "portable" / shortcut.CONFIG_NAME)


class CliTests(unittest.TestCase):
    """The agent-facing command shares the file and reports structured JSON."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "shortcut.json"

    def run_cli(self, *arguments):
        buffer = io.StringIO()
        with patch.dict(os.environ, {"CUTHERE_RUNTIME_ROOT": str(self.root)}, clear=False):
            with contextlib.redirect_stdout(buffer):
                code = shortcut_cli.main(list(arguments))
        return code, json.loads(buffer.getvalue())

    def test_show_reports_the_default_before_anything_is_set(self):
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "darwin")
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["binding"], "RightAlt")
        self.assertEqual(report["status"], "default")
        self.assertEqual(report["display"], "右 Option")
        self.assertIn("support", report)

    def test_set_then_show_then_reset(self):
        code, report = self.run_cli("set", "Control+Shift+K", "--config", str(self.config),
                                    "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertEqual(report["binding"], "Control+Shift+K")
        self.assertEqual(report["previous_binding"], "F8")
        self.assertEqual(report["display"], "Ctrl+Shift+K")
        code, shown = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual((code, shown["binding"], shown["source"]), (0, "Control+Shift+K", "cli"))
        code, reset = self.run_cli("reset", "--config", str(self.config), "--platform", "win32")
        self.assertEqual((code, reset["binding"], reset["previous_binding"]),
                         (0, "F8", "Control+Shift+K"))

    def test_dry_run_validates_without_writing(self):
        code, report = self.run_cli("set", "RightOption", "--dry-run",
                                    "--config", str(self.config), "--platform", "darwin")
        self.assertEqual(code, 0)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["binding"], "RightAlt")
        self.assertFalse(self.config.exists())

    def test_an_unsupported_value_fails_loudly(self):
        code, report = self.run_cli("set", "Escape", "--config", str(self.config),
                                    "--platform", "darwin")
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["code"], "reserved_escape")
        self.assertFalse(self.config.exists())

    def test_test_state_reports_backend_and_activation(self):
        self.run_cli("set", "F9", "--config", str(self.config), "--platform", "win32")
        code, report = self.run_cli("test-state", "--config", str(self.config),
                                    "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertEqual(report["binding"], "F9")
        self.assertIn("capability", report)
        self.assertIn("live", report)
        self.assertFalse(report["live"]["helper_running"])
        self.assertIn("真实按键", report["physical_test"])

    def test_a_corrupt_file_is_reported_with_a_non_zero_exit(self):
        self.config.write_text("{", encoding="utf-8")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "darwin")
        self.assertEqual(code, 2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["binding"], "RightAlt")
        self.assertTrue(report["problems"])

    def test_grammar_lists_the_platform_limits(self):
        code, report = self.run_cli("grammar", "--platform", "darwin")
        self.assertEqual(code, 0)
        self.assertTrue(report["support"]["modifier_only_allowed"])
        self.assertIn("F8", report["support"]["base_keys"])
        self.assertTrue(report["support"]["limits"])


class LiveReadbackTests(unittest.TestCase):
    """A status.json proves liveness/applied only when it is fresh and matching."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "shortcut.json"

    def run_cli(self, *arguments):
        buffer = io.StringIO()
        with patch.dict(os.environ, {"CUTHERE_RUNTIME_ROOT": str(self.root)}, clear=False):
            with contextlib.redirect_stdout(buffer):
                code = shortcut_cli.main(list(arguments))
        return code, json.loads(buffer.getvalue())

    def write_status(self, *, updated_at=None, config_path=None, platform="win32",
                     binding="F8", effective=None, applied=None, pending=False,
                     pending_binding=None, registration=None):
        ipc = self.root / "ipc" / "f8-1.1.0-alpha.6"
        ipc.mkdir(parents=True, exist_ok=True)
        effective = effective if effective is not None else binding
        status = {
            "worker_version": "1.1.0-alpha.6",
            "pid": 1234,
            "updated_at": updated_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "platform": platform,
            "shortcut_config": str(Path(config_path) if config_path is not None else self.config),
            "push_to_talk_armed": True,
            "push_to_talk_backend": {"backend": "windows-hotkey"},
            "shortcut": {
                "binding": binding,
                "configured": binding,
                "effective": effective,
                "applied": applied,
                "pending": pending,
                "pending_binding": pending_binding,
                "active_binding": None,
                "registration": registration or {"mode": "registered", "applied": True},
            },
        }
        path = ipc / "status.json"
        path.write_text(json.dumps(status), encoding="utf-8")
        return path

    def test_an_ancient_status_is_not_a_live_helper(self):
        self.write_status(updated_at="2000-01-01T00:00:00Z")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertFalse(report["live"]["helper_running"])
        self.assertEqual(report["live"]["reason"], "stale_heartbeat")
        self.assertEqual(report["application"]["state"], "not_running")
        self.assertFalse(report["applied"])
        self.assertEqual(report["live"]["freshness_seconds"], 10.0)

    def test_a_fresh_matching_status_proves_the_effective_binding(self):
        self.write_status(binding="F9", applied="F9")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        # The store has no file yet, so `show` reports the default but the live
        # helper is about F9: configured and effective stay distinct.
        self.assertEqual(report["configured"], "F8")
        self.assertTrue(report["live"]["helper_running"])
        self.assertEqual(report["live"]["effective"], "F9")
        self.assertEqual(report["application"]["state"], "pending")
        self.assertFalse(report["applied"])

    def test_a_fresh_matching_status_proves_applied_for_the_stored_value(self):
        self.run_cli("set", "F9", "--config", str(self.config), "--platform", "win32")
        self.write_status(binding="F9", applied="F9")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertTrue(report["live"]["helper_running"])
        self.assertEqual(report["configured"], "F9")
        self.assertEqual(report["effective"], "F9")
        self.assertTrue(report["applied"])
        self.assertEqual(report["application"]["state"], "applied")

    def test_a_custom_config_is_not_confirmed_by_the_default_helper(self):
        custom = self.root / "custom" / "shortcut.json"
        self.write_status(binding="F8")
        code, report = self.run_cli("show", "--config", str(custom), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertFalse(report["live"]["helper_running"])
        self.assertEqual(report["live"]["reason"], "config_mismatch")
        self.assertFalse(report["live"]["config_matches"])
        self.assertEqual(report["application"]["state"], "config_mismatch")

    def test_a_different_platform_status_is_not_used(self):
        self.write_status(platform="darwin")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertFalse(report["live"]["helper_running"])
        self.assertEqual(report["live"]["reason"], "platform_mismatch")

    def test_a_malformed_heartbeat_is_not_fresh(self):
        self.write_status(updated_at="not-a-timestamp")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertFalse(report["live"]["helper_running"])
        self.assertEqual(report["live"]["reason"], "malformed_heartbeat")

    def test_a_malformed_status_file_is_reported_not_trusted(self):
        ipc = self.root / "ipc" / "f8-1.1.0-alpha.6"
        ipc.mkdir(parents=True)
        (ipc / "status.json").write_text("{ not json", encoding="utf-8")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertFalse(report["live"]["helper_running"])
        self.assertEqual(report["live"]["reason"], "malformed_status")

    def test_a_saved_setting_is_not_reported_as_applied_before_the_next_poll(self):
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.run_cli("set", "F8", "--config", str(self.config), "--platform", "win32")
        self.write_status(updated_at=before.isoformat(timespec="milliseconds"),
                          binding="F8", effective="F8", applied="F8")
        code, report = self.run_cli("set", "F9", "--config", str(self.config),
                                    "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertTrue(report["saved"])
        self.assertFalse(report["applied"])
        self.assertTrue(report["pending"])
        self.assertEqual(report["application"]["state"], "awaiting_next_poll")
        self.assertEqual(report["application"]["reason"], "status_predates_write")
        self.assertEqual(report["effective"], "F8",
                         "a pre-write status cannot acknowledge the new value")
        self.assertEqual(report["configured"], "F9")

        # A post-write status is the acknowledgment.
        self.write_status(binding="F9", applied="F9")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertTrue(report["applied"])
        self.assertEqual(report["application"]["state"], "applied")

    def test_a_deferred_mid_take_change_is_distinct_from_applied(self):
        self.run_cli("set", "F8", "--config", str(self.config), "--platform", "win32")
        self.run_cli("set", "F9", "--config", str(self.config), "--platform", "win32")
        self.write_status(binding="F9", effective="F8", applied="F8",
                          pending=True, pending_binding="F9")
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertFalse(report["applied"])
        self.assertTrue(report["pending"])
        self.assertEqual(report["application"]["state"], "deferred_until_take_ends")

    def test_a_registration_failure_is_not_reported_as_applied(self):
        self.run_cli("set", "F9", "--config", str(self.config), "--platform", "win32")
        self.write_status(binding="F9", effective="F9", applied=None,
                          registration={"mode": "failed", "applied": False,
                                        "reason": "hotkey_registration_failed"})
        code, report = self.run_cli("show", "--config", str(self.config), "--platform", "win32")
        self.assertEqual(code, 0)
        self.assertFalse(report["applied"])
        self.assertEqual(report["application"]["state"], "effective_not_applied")
        self.assertEqual(report["application"]["reason"], "hotkey_registration_failed")



if __name__ == "__main__":
    unittest.main()
