"""Doctor diagnostics for the CEP development flag.

The macOS default must show every supported CEP generation (11 for Premiere
Pro 2024, 12 for 2025) so a working CEP 12 value cannot hide a missing CEP 11
one. The report must also distinguish "the key is absent/unreadable" from "the
key was read as 0", because that changes what the user has to fix.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import review_doctor


def darwin_reader(values: dict[str, str | None]):
    def read(version: str) -> tuple[str | None, str]:
        if values.get(version) is None:
            return None, f"The domain/default pair of (com.adobe.CSXS.{version}, PlayerDebugMode) does not exist"
        return values[version], ""

    return read


class CepDiagnosticsTests(unittest.TestCase):
    def test_macos_default_checks_both_and_keeps_missing_11_visible(self):
        with patch.object(review_doctor, "player_debug_darwin",
                          side_effect=darwin_reader({"11": None, "12": "1"})):
            checks = review_doctor.player_debug_checks("darwin", None)
        by_version = {check["version"]: check for check in checks}
        self.assertEqual(set(by_version), {"11", "12"})
        self.assertEqual(by_version["12"]["status"], "PASS")
        self.assertEqual(by_version["12"]["state"], "enabled")
        self.assertEqual(by_version["11"]["status"], "BLOCKED",
                         "a working CEP 12 flag must not make the CEP 11 gap disappear")
        self.assertEqual(by_version["11"]["state"], "unreadable")
        self.assertIn("com.adobe.CSXS.11", by_version["11"]["detail"])
        self.assertIn("未读取到", by_version["11"]["detail"])
        self.assertIn("CEP 11", by_version["11"]["name"])
        self.assertIn("CEP 12", by_version["12"]["name"])

    def test_macos_reports_a_concrete_zero_instead_of_calling_it_unreadable(self):
        with patch.object(review_doctor, "player_debug_darwin",
                          side_effect=darwin_reader({"11": "0", "12": "1"})):
            checks = review_doctor.player_debug_checks("darwin", None)
        cep11 = next(check for check in checks if check["version"] == "11")
        self.assertEqual(cep11["status"], "BLOCKED")
        self.assertEqual(cep11["state"], "value")
        self.assertEqual(cep11["value"], "0")
        self.assertIn("PlayerDebugMode=0（目标为 1）", cep11["detail"])
        self.assertNotIn("未读取到", cep11["detail"],
                         "a real 0 is not the same as an unset/unreadable key")
        self.assertIn("读到的是 '0'", cep11["detail"])

    def test_macos_default_passes_when_both_domains_are_enabled(self):
        with patch.object(review_doctor, "player_debug_darwin",
                          side_effect=darwin_reader({"11": "1", "12": "1"})):
            checks = review_doctor.player_debug_checks("darwin", None)
        self.assertEqual([check["status"] for check in checks], ["PASS", "PASS"])

    def test_explicit_version_checks_only_that_domain(self):
        reader = darwin_reader({"11": "1", "12": None})
        for version in ("11", "12"):
            with patch.object(review_doctor, "player_debug_darwin", side_effect=reader):
                checks = review_doctor.player_debug_checks("darwin", version)
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0]["version"], version)
            self.assertEqual(checks[0]["status"], "PASS" if version == "11" else "BLOCKED")

    def test_windows_default_still_checks_only_12_and_shows_a_zero(self):
        with patch.object(review_doctor, "player_debug_windows", return_value=("0", "")) as read:
            checks = review_doctor.player_debug_checks("win32", None)
        read.assert_called_once_with("12")
        self.assertEqual(len(checks), 1)
        check = checks[0]
        self.assertEqual(check["version"], "12")
        self.assertEqual(check["name"], "CEP 开发扩展加载设置")
        self.assertEqual(check["status"], "BLOCKED")
        self.assertEqual(check["state"], "value")
        self.assertIn("PlayerDebugMode=0（目标为 1）", check["detail"])
        self.assertNotIn("未读取到", check["detail"])

    def test_windows_unreadable_key_keeps_the_existing_hint(self):
        with patch.object(review_doctor, "player_debug_windows",
                          return_value=(None, "FileNotFoundError: missing key")):
            checks = review_doctor.player_debug_checks("win32", None)
        self.assertEqual(checks[0]["state"], "unreadable")
        self.assertIn("未读取到", checks[0]["detail"])
        self.assertIn("FileNotFoundError", checks[0]["detail"])

    def test_other_platforms_report_one_blocked_check(self):
        checks = review_doctor.player_debug_checks("linux", None)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["status"], "BLOCKED")
        self.assertIsNone(checks[0]["version"])

    def test_cep_role_names_the_known_releases_without_mislabelling_15_4(self):
        self.assertIn("2024（24.x）", review_doctor.cep_role("11"))
        self.assertIn("2025（25.x）", review_doctor.cep_role("12"))
        self.assertNotIn("15.4", review_doctor.cep_role("11"))


if __name__ == "__main__":
    unittest.main()
