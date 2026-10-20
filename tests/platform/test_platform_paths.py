"""Per-user paths for each platform.

These run on any host: the platform and the environment mapping are explicit
arguments, so the macOS layout is checked without pretending to be a Mac.
The ``*_text`` helpers return the path as a string, so a POSIX path is not
rewritten by Windows' ``Path`` class while the tests run on Windows.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services import platform_paths as paths

WIN = {"APPDATA": r"C:\Users\fixture\AppData\Roaming",
       "LOCALAPPDATA": r"C:\Users\fixture\AppData\Local",
       "USERPROFILE": r"C:\Users\fixture"}
MAC = {"HOME": "/Users/Fixture User"}
MAC_CUTERE = "/Users/Fixture User/Library/Application Support/Cuthere"
MAC_CEP = "/Users/Fixture User/Library/Application Support/Adobe/CEP/extensions"


class WindowsLayoutTests(unittest.TestCase):
    """The Windows values must stay byte-identical to 1.0: installs depend on them."""

    def test_runtime_extension_and_bridge_layout(self):
        self.assertEqual(paths.runtime_root_text("win32", WIN),
                         r"C:\Users\fixture\AppData\Local\AutoVideoEdit\PremiereReview")
        self.assertEqual(paths.extension_root_text("win32", WIN),
                         r"C:\Users\fixture\AppData\Roaming\Adobe\CEP\extensions")
        self.assertEqual(paths.bridge_dir_text("win32", WIN), r"C:\Users\Public\Documents\ave-bridge")
        self.assertEqual(paths.install_record_path_text("win32", WIN),
                         r"C:\Users\fixture\AppData\Local\AutoVideoEdit\PremiereReview\current_install.json")

    def test_matches_the_shipped_windows_installer(self):
        installer = (REPOSITORY_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("'AutoVideoEdit\\PremiereReview'", installer)
        self.assertIn("'Adobe\\CEP\\extensions'", installer)
        self.assertIn("C:\\Users\\Public\\Documents\\ave-bridge", installer)
        self.assertIn("AutoVideoEditReviewCapture", installer)

    def test_bridge_env_override_keeps_the_ave_prefix_for_existing_installs(self):
        environ = dict(WIN, AVE_BRIDGE_DIR=r"D:\Bridge Dir")
        self.assertEqual(paths.bridge_dir_text("win32", environ), r"D:\Bridge Dir")
        environ["CUTHERE_BRIDGE_DIR"] = r"D:\Other"
        self.assertEqual(paths.bridge_dir_text("win32", environ), r"D:\Other")

    def test_wrapper_name_is_powershell_compatible(self):
        self.assertTrue(paths.cli_wrapper_path_text("win32", WIN).endswith("cuthere.ps1"))


class DarwinLayoutTests(unittest.TestCase):
    def test_extension_folder_matches_cep_documentation(self):
        self.assertEqual(paths.extension_root_text("darwin", MAC), MAC_CEP)

    def test_runtime_bridge_and_cli_are_per_user(self):
        self.assertEqual(paths.runtime_root_text("darwin", MAC), MAC_CUTERE)
        self.assertEqual(paths.bridge_dir_text("darwin", MAC), MAC_CUTERE + "/ave-bridge")
        self.assertEqual(paths.cli_wrapper_path_text("darwin", MAC), MAC_CUTERE + "/bin/cuthere")

    def test_nothing_points_outside_the_user_home(self):
        home = "/Users/Fixture User"
        for function in (paths.extension_root_text, paths.runtime_root_text,
                         paths.bridge_dir_text, paths.bin_dir_text):
            value = function("darwin", MAC)
            self.assertTrue(value.startswith(home + "/"), f"{function.__name__}: {value}")
            for forbidden in ("/usr/", "/System/", "/opt/", "/private/", "/Library/"):
                self.assertFalse(value.startswith(forbidden), function.__name__)

    def test_bridge_override_still_wins(self):
        installed = MAC_CUTERE + "/ave-bridge"
        self.assertEqual(paths.bridge_dir_text("darwin", dict(MAC, CUTHERE_BRIDGE_DIR=installed)), installed)

    def test_paths_with_spaces_and_non_ascii_round_trip(self):
        environ = {"HOME": "/Users/测试 User/My 目录"}
        root = paths.runtime_root_text("darwin", environ)
        self.assertEqual(root, "/Users/测试 User/My 目录/Library/Application Support/Cuthere")
        self.assertTrue(paths.bridge_dir_text("darwin", environ).startswith(root + "/ave-bridge"))

    def test_ssxs_defaults_domain(self):
        self.assertEqual(paths.ssxs_defaults_domain("12"), "com.adobe.CSXS.12")


class LayoutSeparationTests(unittest.TestCase):
    environ = {"HOME": "/Users/f", "APPDATA": r"C:\Users\f\AppData\Roaming",
               "LOCALAPPDATA": r"C:\Users\f\AppData\Local", "USERPROFILE": r"C:\Users\f"}

    def test_platforms_do_not_share_folders(self):
        for function in (paths.runtime_root_text, paths.extension_root_text, paths.bridge_dir_text):
            self.assertNotEqual(function("win32", self.environ), function("darwin", self.environ), function.__name__)

    def test_current_platform_is_detected_from_reality(self):
        expected = "win32" if os.name == "nt" else sys.platform
        self.assertEqual(paths.current_platform(), expected)

    def test_is_within_rejects_sibling_prefixes(self):
        self.assertTrue(paths.is_within(Path("/a/b").parent, Path("/a/b/c").parent))
        self.assertFalse(paths.is_within(Path("/a/bb"), Path("/a/bbc")))


if __name__ == "__main__":
    unittest.main()
