"""macOS installer: plan, sandboxed staging, receipts and rollback.

Everything runs against a temporary "home" and a fake Python/virtualenv, so no
real CEP folder, no network and no system Python is touched. These tests check
the logic the shell entry point delegates to; the shell scripts themselves are
checked separately (``tests/shell/installer_tests.sh`` on macOS, plus a static
guard here).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import install_macos as installer

# The candidate is deliberately scoped to Apple silicon + macOS 14 (see
# docs/MACOS.md and work/supervision/codex-review-e/pypi-wheel-evidence.json).
APPLE_SILICON = {"system": "Darwin", "machine": "arm64", "macos_version": "15.1"}


class FakeHost:
    """Stand-in for `defaults`, `python -m venv` and `pip`."""

    def __init__(self, python: Path, *, fail_pip: bool = False, fail_model: bool = False,
                 fail_optional: bool = False, missing_model: bool = False,
                 defaults: dict[str, str] | None = None):
        self.python = python
        self.fail_pip = fail_pip
        self.fail_model = fail_model
        self.fail_optional = fail_optional
        self.missing_model = missing_model
        self.defaults = dict(defaults or {})
        self.calls: list[list[str]] = []

    def complete(self, code: int, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=code, stdout=out, stderr=err)

    def run_python(self, command, **kwargs):
        self.calls.append(list(command))
        if "-m" in command and "venv" in command:
            venv = Path(command[command.index("venv") + 1])
            bin_dir = venv / "bin"
            bin_dir.mkdir(parents=True, exist_ok=True)
            (bin_dir / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (bin_dir / "python").chmod(0o755)
            return self.complete(0)
        if any("huggingface_hub" in part for part in command):
            if self.fail_model:
                return self.complete(1, err="simulated download failure")
            return self.complete(0)
        if "pip" in command:
            optional = any("requirements-macos-optional.txt" in part for part in command)
            if optional:
                return self.complete(1, err="simulated PyObjC wheel failure") if self.fail_optional else self.complete(0, out="ok")
            return self.complete(1, err="simulated pip failure") if self.fail_pip else self.complete(0, out="ok")
        if "-c" in command and any("find_cached_model" in part for part in command):
            if self.missing_model:
                return self.complete(1, err="model is not cached")
            return self.complete(0, out="/Users/fixture/.cache/huggingface/hub/faster-whisper-medium/snapshots/abc")
        # Interpreter probes used by find_python().
        return self.complete(0, out="3.11")

    def read_defaults(self, domain, key):
        return self.defaults.get(f"{domain}/{key}")

    def write_defaults(self, domain, key, value):
        self.defaults[f"{domain}/{key}"] = value

    def delete_defaults(self, domain, key):
        self.defaults.pop(f"{domain}/{key}", None)

    def host(self) -> installer.HostCommands:
        return installer.HostCommands(read_defaults=self.read_defaults, write_defaults=self.write_defaults,
                                      delete_defaults=self.delete_defaults, run_python=self.run_python)


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home with space 测试"
        self.home.mkdir(parents=True)
        self.python = self.home / "bin" / "python3.11"
        self.python.parent.mkdir(parents=True)
        self.python.write_text("#!/bin/sh\n", encoding="utf-8")
        self.extension_root = self.home / "Library" / "Application Support" / "Adobe" / "CEP" / "extensions"
        self.runtime_root = self.home / "Library" / "Application Support" / "Cuthere"
        self.options = installer.Options(
            apply=False, python=self.python, runtime_root=self.runtime_root,
            extension_root=self.extension_root, skip_dependencies=False,
        )

    def fake(self, **kwargs) -> FakeHost:
        return FakeHost(self.python, **kwargs)


class PlanTests(InstallerTestCase):
    def host(self) -> installer.HostCommands:
        return self.fake().host()

    def test_plan_is_the_default_and_writes_nothing(self):
        plan = installer.build_plan(self.options, host=self.host())
        self.assertEqual(plan["mode"], "PLAN")
        self.assertEqual(plan["platform"], "darwin")
        self.assertIn("no sudo", plan["scope"])
        self.assertFalse(self.runtime_root.exists())
        self.assertFalse(self.extension_root.exists())

    def test_plan_lists_paths_that_support_spaces_and_non_ascii(self):
        plan = installer.build_plan(self.options, host=self.host())
        self.assertIn("home with space 测试", plan["panel"]["destination"])
        self.assertIn("Cuthere", plan["runtime"]["root"])

    def test_plan_says_model_download_is_opt_in(self):
        plan = installer.build_plan(self.options, host=self.host())
        self.assertFalse(plan["model"]["download"])
        self.assertIn("--download-model", plan["model"]["note"])
        self.assertIn("requirements-macos.txt", plan["python"]["requirements"])

    def test_main_without_apply_does_not_install(self):
        fake = self.fake()
        with patch.object(installer, "find_ffmpeg", return_value=None):
            exit_code = installer.main(["--python", str(self.python),
                                        "--runtime-root", str(self.runtime_root),
                                        "--extension-root", str(self.extension_root)], host=fake.host())
        self.assertIn(exit_code, (0, 3))
        self.assertFalse((self.runtime_root / "current_install.json").exists())
        self.assertTrue(all("venv" not in call for call in fake.calls), "the plan must not create a venv")

    def test_check_only_reports_python_and_ffmpeg(self):
        with patch.object(installer, "find_ffmpeg", return_value=None):
            report = installer.environment_report(self.options, host=self.host())
        names = {check["name"]: check for check in report["checks"]}
        self.assertEqual(names["Python 3.11"]["status"], "PASS")
        self.assertEqual(names["FFmpeg"]["status"], "WARN")
        self.assertEqual(names["User scope"]["status"], "PASS")

    def test_wrong_python_version_is_a_failure_not_a_guess(self):
        host = installer.HostCommands(run_python=lambda command, **kw: FakeHost(self.python).complete(0, out="3.13"))
        python, note = installer.find_python(self.python, host=host)
        self.assertIsNone(python)
        self.assertIn("3.11 is required", note)


class InstallTests(InstallerTestCase):
    def setUp(self):
        super().setUp()
        # A previous installation with a different file, plus a values default.
        self.destination = self.extension_root / installer.platform_paths.PANEL_FOLDER
        (self.destination / "CSXS").mkdir(parents=True)
        (self.destination / "CSXS" / "manifest.xml").write_text('<ExtensionManifest ExtensionBundleId="com.autovideoedit.reviewcapture"/>', encoding="utf-8")
        (self.destination / "previous.txt").write_text("previous panel", encoding="utf-8")
        self.previous_defaults = {"com.adobe.CSXS.12/PlayerDebugMode": "0"}

    def list_within(self, root: Path) -> list[Path]:
        return sorted(p.relative_to(root) for p in root.rglob("*")) if root.exists() else []

    def test_apply_installs_panel_runtime_and_receipts(self):
        host = self.fake(defaults=self.previous_defaults)
        result = installer.install(self.options, host=host.host(), info=APPLE_SILICON)
        record = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "INSTALLED")
        self.assertEqual(record["version"], installer.version())
        self.assertTrue((self.destination / "CSXS" / "manifest.xml").is_file())
        self.assertTrue((self.destination / "index.html").is_file())
        runtime = json.loads((self.destination / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["platform"], "darwin")
        self.assertEqual(runtime["transcription_language"], "auto")
        self.assertIn("bin", Path(runtime["launcher"]).parts)
        self.assertTrue(runtime["launcher"].endswith("python"))
        self.assertTrue(runtime["worker_entry"].endswith("worker_entry.py"))
        self.assertTrue((self.runtime_root / "ave-bridge" / "commands").is_dir())
        self.assertTrue((self.runtime_root / "ave-bridge" / "responses").is_dir())
        self.assertTrue((self.runtime_root / "ave-bridge" / "locks").is_dir())
        self.assertEqual(host.defaults["com.adobe.CSXS.12/PlayerDebugMode"], "1")
        self.assertEqual(record["previous_defaults"], {"com.adobe.CSXS.12": "0"})
        wrapper = Path(record["cli_wrapper"])
        self.assertTrue(wrapper.is_file())
        self.assertEqual(record["cli_wrapper_mode"], "0755", "the CLI wrapper must be executable on macOS")
        if os.name != "nt":  # NTFS has no Unix execute bit, so this is macOS/Linux only
            self.assertTrue(wrapper.stat().st_mode & stat.S_IXUSR)
        text = wrapper.read_text(encoding="utf-8")
        self.assertIn("CUTHERE_BRIDGE_DIR=", text)
        self.assertIn('"$@"', text)
        # The previous panel is preserved, not deleted.
        backups = [p for p in (self.runtime_root / "backups").iterdir() if p.is_dir()]
        self.assertTrue(any((backup / "previous.txt").is_file() for backup in backups))

    def test_model_is_never_downloaded_unless_requested(self):
        host = self.fake()
        installer.install(self.options, host=host.host(), info=APPLE_SILICON)
        self.assertFalse(any("huggingface_cli" in call for call in host.calls))

    def test_download_model_requests_weights(self):
        host = self.fake()
        options = installer.Options(apply=True, python=self.python, runtime_root=self.runtime_root,
                                    extension_root=self.extension_root, download_model=True, model="small")
        installer.install(options, host=host.host(), info=APPLE_SILICON)
        download = [call for call in host.calls if any("huggingface_hub" in part for part in call)]
        self.assertTrue(download, host.calls)
        self.assertIn("Systran/faster-whisper-small", download[0])
        record = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertEqual(record["model"], "small")

    def test_missing_model_is_reported_but_does_not_block_the_panel(self):
        host = self.fake(missing_model=True)
        installer.install(self.options, host=host.host(), info=APPLE_SILICON)
        record = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertEqual(record["model_status"], "missing")
        runtime = json.loads((self.destination / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["model_status"], "missing")
        self.assertTrue((self.destination / "index.html").is_file())

    def test_optional_key_backend_failure_is_recorded_not_fatal(self):
        host = self.fake(fail_optional=True)
        installer.install(self.options, host=host.host(), info=APPLE_SILICON)
        record = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertEqual(record["optional_dependencies"]["key_backend"], "unavailable")
        runtime = json.loads((self.destination / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["optional_dependencies"]["key_backend"], "unavailable")
        self.assertTrue((self.destination / "index.html").is_file())

    def test_macos_staged_manifest_drops_windows_gpu_flags(self):
        installer.install(self.options, host=self.fake().host(), info=APPLE_SILICON)
        staged = (self.destination / "CSXS" / "manifest.xml").read_text(encoding="utf-8")
        self.assertNotIn("CEFCommandLine", staged)
        self.assertNotIn("--use-angle=d3d11", staged)
        self.assertIn('ExtensionBundleId="com.autovideoedit.reviewcapture"', staged)
        self.assertIn("Cuthere · 剪这儿", staged)
        # The shared source manifest still carries the Windows workaround.
        source = (REPOSITORY_ROOT / "premiere_cep" / "review_capture_panel" / "CSXS" / "manifest.xml").read_text(encoding="utf-8")
        self.assertIn("--use-angle=d3d11", source)
        record = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertEqual(record["panel_manifest"]["cef_command_line"], "removed")

    def test_existing_language_is_preserved_and_backed_up(self):
        (self.destination / "CSXS").mkdir(parents=True, exist_ok=True)
        (self.destination / "CSXS" / "manifest.xml").write_text(
            '<ExtensionManifest ExtensionBundleId="com.autovideoedit.reviewcapture"/>', encoding="utf-8"
        )
        (self.destination / "runtime.json").write_text('{"transcription_language": "en"}', encoding="utf-8")
        installer.install(self.options, host=self.fake().host(), info=APPLE_SILICON)
        runtime = json.loads((self.destination / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["language"], "en")
        self.assertEqual(runtime["transcription_language"], "en")
        self.assertEqual(runtime["language_source"], "previous-install")
        record = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        previous = list((self.runtime_root / "versions").glob("*/previous_runtime.json"))
        self.assertTrue(previous, "the previous runtime configuration must be kept for audit")
        self.assertIn("en", previous[0].read_text(encoding="utf-8"))

    def test_explicit_language_wins_over_a_previous_value(self):
        (self.destination / "CSXS").mkdir(parents=True, exist_ok=True)
        (self.destination / "CSXS" / "manifest.xml").write_text(
            '<ExtensionManifest ExtensionBundleId="com.autovideoedit.reviewcapture"/>', encoding="utf-8"
        )
        (self.destination / "runtime.json").write_text('{"transcription_language": "en"}', encoding="utf-8")
        options = installer.Options(python=self.python, runtime_root=self.runtime_root,
                                    extension_root=self.extension_root, language="zh")
        installer.install(options, host=self.fake().host(), info=APPLE_SILICON)
        runtime = json.loads((self.destination / "runtime.json").read_text(encoding="utf-8"))
        self.assertEqual(runtime["language"], "zh")
        self.assertEqual(runtime["language_source"], "explicit")

    def test_invalid_language_changes_nothing(self):
        options = installer.Options(apply=True, python=self.python, runtime_root=self.runtime_root,
                                    extension_root=self.extension_root, language="English please!")
        with self.assertRaises(installer.InstallError):
            installer.install(options, host=self.fake().host(), info=APPLE_SILICON)
        self.assertEqual((self.destination / "previous.txt").read_text(encoding="utf-8"), "previous panel")
        self.assertFalse((self.runtime_root / "current_install.json").exists())

    def test_failed_dependency_install_rolls_back_and_keeps_previous_panel(self):
        host = self.fake(fail_pip=True, defaults=self.previous_defaults)
        with self.assertRaises(installer.InstallError):
            installer.install(self.options, host=host.host(), info=APPLE_SILICON)
        self.assertEqual((self.destination / "previous.txt").read_text(encoding="utf-8"), "previous panel")
        self.assertFalse((self.destination / "runtime.json").exists())
        self.assertEqual(host.defaults["com.adobe.CSXS.12/PlayerDebugMode"], "0")
        self.assertFalse((self.runtime_root / "current_install.json").exists())
        log = next((self.runtime_root / "versions").glob("*/install.log")).read_text(encoding="utf-8")
        self.assertIn("failure", log)

    def test_failed_install_over_an_existing_record_restores_that_record(self):
        self.runtime_root.mkdir(parents=True)
        (self.runtime_root / "current_install.json").write_text('{"version": "0.9.9"}', encoding="utf-8")
        host = self.fake(fail_pip=True)
        with self.assertRaises(installer.InstallError):
            installer.install(self.options, host=host.host(), info=APPLE_SILICON)
        self.assertEqual(json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))["version"], "0.9.9")

    def test_failed_install_after_wrapper_replacement_restores_the_previous_wrapper(self):
        self.runtime_root.mkdir(parents=True)
        (self.runtime_root / "bin").mkdir(parents=True)
        wrapper = self.runtime_root / "bin" / "cuthere"
        wrapper.write_text("#!/bin/sh\n# previous wrapper\n", encoding="utf-8")
        (self.runtime_root / "current_install.json").write_text('{"version": "0.9.9"}', encoding="utf-8")

        def broken_wrapper(*_args, **_kwargs):
            wrapper.write_text("#!/bin/sh\n# new wrapper that must not survive the rollback\n", encoding="utf-8")
            raise installer.InstallError("simulated wrapper failure")

        with patch.object(installer, "write_cli_wrapper", side_effect=broken_wrapper):
            with self.assertRaises(installer.InstallError):
                installer.install(self.options, host=self.fake().host(), info=APPLE_SILICON)
        self.assertEqual(wrapper.read_text(encoding="utf-8"), "#!/bin/sh\n# previous wrapper\n")

    def test_missing_python_changes_nothing(self):
        options = installer.Options(apply=True, python=self.home / "missing-python",
                                    runtime_root=self.runtime_root, extension_root=self.extension_root)
        with self.assertRaises(installer.InstallError):
            installer.install(options, host=self.fake().host(), info=APPLE_SILICON)
        self.assertEqual((self.destination / "previous.txt").read_text(encoding="utf-8"), "previous panel")

    def test_no_defaults_leaves_the_cep_flag_alone(self):
        host = self.fake(defaults=self.previous_defaults)
        options = installer.Options(apply=True, python=self.python, runtime_root=self.runtime_root,
                                    extension_root=self.extension_root, no_defaults=True)
        installer.install(options, host=host.host(), info=APPLE_SILICON)
        self.assertEqual(host.defaults["com.adobe.CSXS.12/PlayerDebugMode"], "0")

    def test_legacy_bundle_is_retired_only_when_it_is_ours(self):
        legacy = self.extension_root / installer.platform_paths.LEGACY_PANEL_FOLDER
        (legacy / "CSXS").mkdir(parents=True)
        (legacy / "CSXS" / "manifest.xml").write_text('<ExtensionManifest ExtensionBundleId="com.autovideoedit.hermes"/>', encoding="utf-8")
        foreign = self.extension_root / "somethingelse"
        (foreign / "CSXS").mkdir(parents=True)
        (foreign / "CSXS" / "manifest.xml").write_text('<ExtensionManifest ExtensionBundleId="other.vendor"/>', encoding="utf-8")
        installer.install(self.options, host=self.fake().host(), info=APPLE_SILICON)
        self.assertFalse(legacy.exists())
        self.assertTrue(foreign.is_dir())


class UninstallTests(InstallerTestCase):
    def install(self, host: FakeHost, **overrides) -> dict:
        options = installer.Options(python=self.python, runtime_root=self.runtime_root,
                                    extension_root=self.extension_root, **overrides)
        return installer.install(options, host=host.host(), info=APPLE_SILICON)

    def uninstall_options(self, **overrides) -> installer.Options:
        values = dict(runtime_root=self.runtime_root, extension_root=self.extension_root,
                      uninstall=True, apply=True)
        values.update(overrides)
        return installer.Options(**values)

    def test_plan_writes_nothing(self):
        plan = installer.uninstall_plan(self.options)
        self.assertFalse(plan["installed"])
        self.assertFalse(self.runtime_root.exists())

    def test_uninstall_moves_panel_but_keeps_project_folders(self):
        host = self.fake()
        self.install(host)
        project = self.home / "Projects" / "Episode 01"
        notes = project / "work" / "premiere-review" / "session_fixture_0001"
        notes.mkdir(parents=True)
        (notes / "note.json").write_text("{}", encoding="utf-8")
        result = installer.uninstall(self.options, host=host.host())
        self.assertEqual(result["status"], "UNINSTALLED")
        self.assertFalse((self.extension_root / installer.platform_paths.PANEL_FOLDER).exists())
        self.assertTrue((notes / "note.json").is_file())
        self.assertNotEqual(host.defaults.get("com.adobe.CSXS.12/PlayerDebugMode"), "1")

    def test_restore_previous_puts_the_old_panel_back(self):
        self.extension_root.mkdir(parents=True, exist_ok=True)
        old = self.extension_root / installer.platform_paths.PANEL_FOLDER
        (old / "CSXS").mkdir(parents=True)
        (old / "CSXS" / "manifest.xml").write_text('<ExtensionManifest ExtensionBundleId="com.autovideoedit.reviewcapture"/>', encoding="utf-8")
        (old / "previous.txt").write_text("previous panel", encoding="utf-8")
        host = self.fake(defaults={"com.adobe.CSXS.12/PlayerDebugMode": "0"})
        self.install(host)
        options = installer.Options(runtime_root=self.runtime_root, extension_root=self.extension_root,
                                    uninstall=True, apply=True, restore_previous=True)
        installer.uninstall(options, host=host.host())
        self.assertTrue((old / "previous.txt").is_file())
        self.assertEqual(host.defaults["com.adobe.CSXS.12/PlayerDebugMode"], "0")

    def test_purge_runtime_is_explicit(self):
        host = self.fake()
        self.install(host)
        version_dirs = list((self.runtime_root / "versions").iterdir())
        self.assertTrue(version_dirs)
        result = installer.uninstall(self.uninstall_options(purge_runtime=True), host=host.host())
        self.assertTrue(result["purged"])
        self.assertFalse(any(path.is_dir() for path in version_dirs))

    def test_uninstall_removes_the_generated_command(self):
        host = self.fake()
        record = self.install(host)["record"]
        wrapper = Path(record["cli_wrapper"])
        self.assertTrue(wrapper.is_file())
        installer.uninstall(self.uninstall_options(), host=host.host())
        self.assertFalse(wrapper.exists(), "a plain uninstall must not leave the cuthere command behind")

    def test_no_defaults_upgrade_restores_the_record_and_wrapper(self):
        # Regression: previous_install.json used to be written only without
        # --no-defaults, so a second --no-defaults install could restore the old
        # panel but lost the record. The generated CLI wrapper was never backed
        # up either, so it kept pointing at the new runtime after a restore.
        host = self.fake()
        first = self.install(host)["record"]
        first_wrapper = Path(first["cli_wrapper"]).read_text(encoding="utf-8")
        second = self.install(host, no_defaults=True)["record"]
        self.assertNotEqual(first["runtime"], second["runtime"])
        previous_record = Path(second["previous_install_record"])
        self.assertTrue(previous_record.is_file(), "--no-defaults must still preserve the previous record")
        self.assertEqual(json.loads(previous_record.read_text(encoding="utf-8"))["runtime"], first["runtime"])
        self.assertIn(second["runtime"], Path(second["cli_wrapper"]).read_text(encoding="utf-8"))

        installer.uninstall(self.uninstall_options(restore_previous=True), host=host.host())
        restored = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["runtime"], first["runtime"], "the previous install record must be restored")
        self.assertEqual(Path(restored["cli_wrapper"]).read_text(encoding="utf-8"), first_wrapper,
                         "the restored wrapper must target the restored runtime")

    def test_restore_after_two_installs_returns_the_first_then_the_dummy_panel(self):
        # The shell smoke test runs this sequence with the real entry points;
        # this keeps the same two transactions executable without a POSIX
        # shell. The first restore belongs to the *first* installation (panel
        # with its runtime.json, install record and wrapper target), not to the
        # panel that existed before Cuthere was installed.
        self.extension_root.mkdir(parents=True, exist_ok=True)
        dummy = self.extension_root / installer.platform_paths.PANEL_FOLDER
        (dummy / "CSXS").mkdir(parents=True)
        (dummy / "CSXS" / "manifest.xml").write_text(
            '<ExtensionManifest ExtensionBundleId="com.autovideoedit.reviewcapture"/>', encoding="utf-8")
        (dummy / "previous.txt").write_text("dummy panel", encoding="utf-8")
        host = self.fake()
        first = self.install(host)["record"]
        first_panel = (dummy / "runtime.json").read_text(encoding="utf-8")
        first_wrapper = Path(first["cli_wrapper"]).read_text(encoding="utf-8")
        second = self.install(host, no_defaults=True)["record"]
        self.assertNotEqual(first["runtime"], second["runtime"])

        installer.uninstall(self.uninstall_options(restore_previous=True), host=host.host())
        self.assertEqual((dummy / "runtime.json").read_text(encoding="utf-8"), first_panel,
                         "the first installation's panel configuration must be restored")
        restored_config = json.loads(first_panel)
        self.assertIn(first["runtime"], restored_config["worker_entry"],
                      "the restored panel config must target the first installation")
        self.assertFalse((dummy / "previous.txt").exists(),
                         "restoring the second install must not reach past the first installation")
        restored = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertEqual(restored["runtime"], first["runtime"])
        self.assertEqual(Path(restored["cli_wrapper"]).read_text(encoding="utf-8"), first_wrapper)

        # A second, separate restore transaction returns the pre-install panel.
        installer.uninstall(self.uninstall_options(restore_previous=True), host=host.host())
        self.assertEqual((dummy / "previous.txt").read_text(encoding="utf-8"), "dummy panel")
        self.assertFalse((dummy / "runtime.json").exists())
        self.assertFalse((self.runtime_root / "current_install.json").exists(),
                         "a restore to the pre-install panel must not leave an installation record")
        self.assertFalse((self.runtime_root / "bin" / "cuthere").exists(),
                         "a restore to the pre-install panel must not leave the generated command")

    def test_restore_and_purge_together_are_rejected_before_any_change(self):
        host = self.fake()
        self.install(host)
        record_path = self.runtime_root / "current_install.json"
        before = record_path.read_text(encoding="utf-8")
        with self.assertRaises(installer.InstallError):
            installer.uninstall(self.uninstall_options(restore_previous=True, purge_runtime=True), host=host.host())
        self.assertEqual(record_path.read_text(encoding="utf-8"), before)
        self.assertTrue((self.extension_root / installer.platform_paths.PANEL_FOLDER).is_dir())

    def test_restore_is_refused_when_its_backup_is_missing(self):
        host = self.fake()
        self.install(host)
        second = self.install(host, no_defaults=True)["record"]
        Path(second["previous_cli_wrapper"]).unlink()
        record_path = self.runtime_root / "current_install.json"
        before = record_path.read_text(encoding="utf-8")
        with self.assertRaises(installer.InstallError):
            installer.uninstall(self.uninstall_options(restore_previous=True), host=host.host())
        self.assertEqual(record_path.read_text(encoding="utf-8"), before)
        self.assertTrue((self.extension_root / installer.platform_paths.PANEL_FOLDER).is_dir())

    def test_purge_keeps_unrelated_contents_of_a_custom_runtime_root(self):
        host = self.fake()
        self.install(host)
        unrelated = self.runtime_root / "unrelated.txt"
        unrelated.write_text("keep me", encoding="utf-8")
        notes = self.runtime_root / "notes" / "keep.txt"
        notes.parent.mkdir(parents=True)
        notes.write_text("keep me too", encoding="utf-8")
        other_tool = self.runtime_root / "bin" / "other-tool"
        other_tool.write_text("#!/bin/sh\n", encoding="utf-8")
        installer.uninstall(self.uninstall_options(purge_runtime=True), host=host.host())
        self.assertTrue(unrelated.is_file())
        self.assertTrue(notes.is_file())
        self.assertTrue(other_tool.is_file(), "purge must not delete other files in bin/")
        self.assertFalse((self.runtime_root / "versions").exists())
        self.assertFalse((self.runtime_root / "ipc").exists())
        self.assertFalse((self.runtime_root / "bin" / "cuthere").exists())


class HostSupportTests(InstallerTestCase):
    """R6: the documented macOS scope must match the pinned wheels."""

    def test_apple_silicon_on_macos_14_and_later_is_supported(self):
        for version in ("14.0", "14.7.2", "15.1", "26.0"):
            support = installer.host_support({"system": "Darwin", "machine": "arm64", "macos_version": version})
            self.assertTrue(support["supported"], (version, support))
        self.assertIn("Apple silicon", support["minimum"])
        self.assertEqual(support["minimum"], "Apple silicon (arm64) on macOS 14.0 or later")

    def test_intel_macs_are_refused_with_the_wheel_evidence(self):
        support = installer.host_support({"system": "Darwin", "machine": "x86_64", "macos_version": "15.1"})
        self.assertFalse(support["supported"])
        self.assertIn("onnxruntime==1.31.0 has no macOS Intel wheel", support["reason"])
        self.assertIn("onnxruntime==1.31.0", support["evidence"])
        self.assertIn("https://pypi.org/", support["evidence"]["onnxruntime==1.31.0"])

    def test_older_macos_is_refused(self):
        support = installer.host_support({"system": "Darwin", "machine": "arm64", "macos_version": "13.6.7"})
        self.assertFalse(support["supported"])
        self.assertIn("older than 14.0", support["reason"])
        self.assertIn("requires macOS 14.0", support["reason"])

    def test_a_non_darwin_host_is_refused_with_one_clear_reason(self):
        support = installer.host_support({"system": "Windows", "machine": "arm64", "macos_version": ""})
        self.assertFalse(support["supported"])
        self.assertIn("not macOS", support["reason"])
        self.assertNotIn("older than", support["reason"])

    def test_an_unsupported_host_is_refused_before_any_mutation(self):
        fake = self.fake()
        with self.assertRaises(installer.InstallError) as caught:
            installer.install(self.options, host=fake.host(),
                              info={"system": "Darwin", "machine": "x86_64", "macos_version": "15.1"})
        self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(fake.calls, [], "the host must be checked before any command runs")
        self.assertFalse(self.runtime_root.exists())
        self.assertFalse(self.extension_root.exists())

    def test_plan_and_environment_report_state_the_policy(self):
        plan = installer.build_plan(self.options, host=self.fake().host(), info=APPLE_SILICON)
        self.assertTrue(plan["host_support"]["supported"])
        self.assertIn("macOS 14.0", plan["host_support"]["minimum"])
        self.assertIn("Apple silicon", plan["steps"][0])
        with patch.object(installer, "find_ffmpeg", return_value=None):
            report = installer.environment_report(self.options, host=self.fake().host(), info=APPLE_SILICON)
        checks = {check["name"]: check for check in report["checks"]}
        self.assertEqual(checks["macOS host support"]["status"], "PASS")
        self.assertIn("Apple silicon", checks["macOS host support"]["detail"])

    def test_the_install_record_records_the_host_policy(self):
        installer.install(self.options, host=self.fake().host(), info=APPLE_SILICON)
        record = json.loads((self.runtime_root / "current_install.json").read_text(encoding="utf-8"))
        self.assertTrue(record["host_support"]["supported"])
        self.assertEqual(record["host_support"]["machine"], "arm64")
        self.assertEqual(record["host_support"]["macos_version"], "15.1")


class ShellEntryPointTests(unittest.TestCase):
    """Static checks of the shell entry points (a real run needs macOS)."""

    def code_lines(self, name: str) -> list[str]:
        return [line for line in (REPOSITORY_ROOT / name).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")]

    def test_shell_scripts_are_posix_sh_without_bashisms(self):
        for name in ("install.sh", "uninstall.sh"):
            text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("#!/bin/sh"), name)
            self.assertIn("set -eu", text, name)
            code = self.code_lines(name)
            for line in code:
                for bashism in ("[[", "function ", "&>", "echo -e", "local ", "source "):
                    self.assertNotIn(bashism, line, f"{name} uses a bash-only construct on: {line}")
            for line in code:
                self.assertNotIn("sudo", line, f"{name} must never escalate privileges")
            self.assertIn('here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)', text, name)

    def test_installer_defaults_to_a_plan(self):
        code = self.code_lines("install.sh")
        self.assertEqual(code[-1], 'exec "$python" "$script" "$@"', "the wrapper must pass arguments through untouched")
        self.assertTrue(all("--apply" not in line for line in code), "the wrapper must not add --apply")
        parser = (REPOSITORY_ROOT / "scripts" / "install_macos.py").read_text(encoding="utf-8")
        self.assertIn("help=\"perform the installation (default: show the plan)\"", parser)
        self.assertIn("elif options.apply:", parser)

    def test_shell_scripts_refuse_other_operating_systems(self):
        for name in ("install.sh", "uninstall.sh"):
            text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
            self.assertIn('[ "$(uname -s)" != "Darwin" ]', text, name)
            self.assertIn("exit 2", text, name)

    def test_install_sh_honours_the_explicit_python_it_passes_on(self):
        text = (REPOSITORY_ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("--python=*", text)
        self.assertIn("next_is_python", text)
        self.assertIn('exec "$python" "$script" "$@"', text)
        # It must not silently ignore the user's interpreter and then fail
        # because no other 3.11 is on PATH.
        self.assertIn("is not an executable file", text)

    def test_uninstall_accepts_a_prepared_runtime_or_any_python_310(self):
        text = (REPOSITORY_ROOT / "uninstall.sh").read_text(encoding="utf-8")
        self.assertIn("python3.11", text)
        self.assertIn("python3", text)
        self.assertIn("(3, 10)", text)
        self.assertIn("CUTHERE_PYTHON", text)


if __name__ == "__main__":
    unittest.main()
