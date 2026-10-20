"""Language selection: auto detection, explicit codes, and saved preferences.

1.0 hard-coded Chinese for faster-whisper. The port exposes `auto` or an
explicit ISO code, reads the language the installer recorded, and must not
rewrite a value an earlier installation already saved. These tests exercise
that logic without loading faster-whisper.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.transcribe_review_batch import (
    AUTO_LANGUAGE,
    normalize_language,
    runtime_language,
    whisper_language_argument,
)


class NormalizationTests(unittest.TestCase):
    def test_auto_is_the_default_for_empty_or_auto(self):
        self.assertEqual(normalize_language(None), AUTO_LANGUAGE)
        self.assertEqual(normalize_language(""), AUTO_LANGUAGE)
        self.assertEqual(normalize_language("auto"), AUTO_LANGUAGE)
        self.assertEqual(whisper_language_argument("auto"), None)

    def test_explicit_codes_pass_through(self):
        self.assertEqual(normalize_language("zh"), "zh")
        self.assertEqual(normalize_language("en"), "en")
        self.assertEqual(whisper_language_argument("en"), "en")

    def test_unsupported_values_are_rejected_not_guessed(self):
        for value in ("Chinese", "zh CN", "english please!", "zh-CN extra"):
            with self.assertRaises(ValueError):
                normalize_language(value)


class RuntimeConfigurationTests(unittest.TestCase):
    def write_runtime(self, root: Path, payload: dict) -> None:
        panel = root / "AutoVideoEditReviewCapture"
        panel.mkdir(parents=True)
        (panel / "runtime.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_recorded_language_is_used(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write_runtime(root, {"transcription_language": "en"})
            with patch.dict(os.environ, {"CUTHERE_EXTENSION_ROOT": str(root)}):
                self.assertEqual(runtime_language(), "en")

    def test_older_language_key_is_also_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.write_runtime(root, {"language": "zh"})
            with patch.dict(os.environ, {"CUTHERE_EXTENSION_ROOT": str(root)}):
                self.assertEqual(runtime_language(), "zh")

    def test_missing_or_invalid_configuration_falls_back_to_auto(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {"CUTHERE_EXTENSION_ROOT": str(root)}):
                self.assertEqual(runtime_language(), AUTO_LANGUAGE)
            self.write_runtime(root, {"transcription_language": "not a language"})
            with patch.dict(os.environ, {"CUTHERE_EXTENSION_ROOT": str(root)}):
                self.assertEqual(runtime_language(), AUTO_LANGUAGE)


class InstallerPreservationTests(unittest.TestCase):
    """A saved preference is carried over; the PowerShell path must say so too."""

    def test_windows_installer_reads_before_replacing_the_language(self):
        installer = (REPOSITORY_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("$PSBoundParameters.ContainsKey('Language')", installer)
        self.assertIn("previous-install", installer)
        self.assertIn("language_source", installer)
        self.assertIn("previous_runtime.json", installer)
        self.assertIn("$planLanguageSource", installer)
        # It records the previous file before moving the panel away.
        self.assertLess(installer.index("previous_runtime.json"), installer.index("Install-F8PanelSet"))

    def test_macos_installer_preserves_a_previous_language(self):
        from scripts import install_macos

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            extension = root / "extensions"
            runtime = root / "runtime"
            destination = extension / "AutoVideoEditReviewCapture"
            destination.mkdir(parents=True)
            (destination / "runtime.json").write_text('{"transcription_language": "en"}', encoding="utf-8")
            options = install_macos.Options(extension_root=extension, runtime_root=runtime)
            language, source = install_macos.resolve_language(options)
        self.assertEqual((language, source), ("en", "previous-install"))


if __name__ == "__main__":
    unittest.main()
