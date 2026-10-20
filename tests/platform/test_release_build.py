"""Release packaging: allowlist, leak scan, deterministic zip and manifest.

The packages are built into a temporary directory, so the real output/ folder is
not touched by the test suite.
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import build_release


class PackageSelectionTests(unittest.TestCase):
    def test_platform_packages_carry_only_their_own_entry_points(self):
        mac = set(build_release.select_files("macos"))
        win = set(build_release.select_files("windows"))
        self.assertIn("install.sh", mac)
        self.assertIn("uninstall.sh", mac)
        self.assertIn("requirements-macos.txt", mac)
        self.assertIn("scripts/install_macos.py", mac)
        self.assertNotIn("install.ps1", mac)
        self.assertNotIn("tests/panel_install.ps1", mac)
        self.assertIn("install.ps1", win)
        self.assertIn("f8.ps1", win)
        self.assertNotIn("install.sh", win)
        self.assertNotIn("scripts/install_macos.py", win)

    def test_both_packages_share_the_product_sources(self):
        for platform in ("windows", "macos"):
            files = set(build_release.select_files(platform))
            for required in ("README.md", "README.zh-CN.md", "LICENSE", "VERSION",
                             "docs/DEVELOPMENT.md", "docs/en/DEVELOPMENT.md",
                             "docs/MACOS.md", "docs/AGENT_HANDOFF.md",
                             ".github/workflows/macos.yml", ".github/workflows/windows.yml",
                             ".gitattributes",
                             "tests/shell/installer_tests.sh",
                             "premiere_cep/review_capture_panel/CSXS/manifest.xml",
                             "premiere_cep/review_capture_panel/js/review_panel.js",
                             "services/review_capture/worker.py", "scripts/cli.py"):
                self.assertIn(required, files, platform)
        self.assertIn("requirements-macos-optional.txt", set(build_release.select_files("macos")))
        self.assertNotIn("requirements-macos-optional.txt", set(build_release.select_files("windows")))

    def test_repository_manifests_are_not_archived(self):
        for platform in ("windows", "macos"):
            files = set(build_release.select_files(platform))
            self.assertNotIn("FILE_MANIFEST.json", files)
            self.assertNotIn("SOURCE_PROVENANCE.json", files)
            self.assertTrue(build_release.is_denied("FILE_MANIFEST.json"))
            # The removed internal provenance file must stay out even if it is
            # ever recreated by mistake.
            self.assertTrue(build_release.is_denied("SOURCE_PROVENANCE.json"))

    def test_private_material_is_never_selected(self):
        for platform in ("windows", "macos"):
            files = build_release.select_files(platform)
            for relative in files:
                self.assertFalse(relative.startswith(("work/", "output/", ".git/")), relative)
                self.assertNotIn("__pycache__", relative)
                self.assertFalse(relative.endswith((".pyc", ".log", ".wav", ".prproj", ".zip")), relative)
                self.assertNotIn("runtime.json", relative)

    def test_leak_scan_flags_real_problems(self):
        with tempfile.TemporaryDirectory() as folder:
            probe = build_release.RELEASE_ROOT / "work" / "leak-probe.txt"
            probe.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text("home is C:\\Users\\someone\\Projects\napi_key = sk-abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8")
            try:
                problems = build_release.leak_scan(["work/leak-probe.txt"])
            finally:
                probe.unlink()
        text = "\n".join(problems)
        self.assertIn("denied path", text)
        self.assertIn("absolute Windows user path", text)
        self.assertIn("API token", text)

    def test_current_tree_passes_the_leak_scan(self):
        for platform in ("windows", "macos"):
            self.assertEqual(build_release.leak_scan(build_release.select_files(platform)), [])


class RepositoryManifestTests(unittest.TestCase):
    def test_file_manifest_matches_the_tree(self):
        from scripts import update_manifests

        problems = update_manifests.check()
        self.assertEqual(problems, [], "\n".join(problems))

    def test_text_detection_comes_from_the_content_not_the_suffix(self):
        from scripts import update_manifests

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Suffixless text: a fresh checkout writes LF, this worktree may
            # hold CRLF, and both must produce the same record.
            crlf = root / "sample.gitignore"
            crlf.write_bytes(b"build/\r\nwork/\r\n")
            lf = root / "sample-lf.gitignore"
            lf.write_bytes(b"build/\nwork/\n")
            self.assertEqual(update_manifests.normalized_bytes(crlf),
                             update_manifests.normalized_bytes(lf))
            self.assertEqual(update_manifests.file_entry(crlf, "sample.gitignore"),
                             update_manifests.file_entry(lf, "sample.gitignore"))
            # The CommonJS test drivers are text too (they are not .js).
            driver = root / "adapter.cjs"
            driver.write_bytes(b"const a = 1;\r\nconst b = 2;\r\n")
            self.assertNotIn(b"\r", update_manifests.normalized_bytes(driver))
            # A binary payload keeps its exact bytes even with a text suffix.
            binary = root / "not-really.txt"
            binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00binary\r\n")
            self.assertEqual(update_manifests.normalized_bytes(binary), binary.read_bytes())
            self.assertFalse(update_manifests.is_text(binary.read_bytes()))

    def test_file_manifest_records_checkout_independent_bytes(self):
        from scripts import update_manifests

        manifest = json.loads((REPOSITORY_ROOT / "FILE_MANIFEST.json").read_text(encoding="utf-8"))
        entries = {entry["path"]: entry for entry in manifest["files"]}
        # These six files differ between this Windows worktree and the committed
        # blobs on both GitHub runners: suffixless text and the .cjs drivers.
        for relative in (".gitignore", ".gitattributes", "LICENSE", "VERSION",
                         "tests/cep_adapter.cjs", "tests/premiere_lifecycle.cjs",
                         "tests/review_ptt_release.cjs", "tests/single_panel.cjs"):
            self.assertIn(relative, entries, relative)
            normalized = (REPOSITORY_ROOT / relative).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(entries[relative]["bytes"], len(normalized), relative)
            self.assertEqual(entries[relative]["sha256"],
                             hashlib.sha256(normalized).hexdigest(), relative)

    def test_file_manifest_version_is_the_candidate_version(self):
        manifest = json.loads((REPOSITORY_ROOT / "FILE_MANIFEST.json").read_text(encoding="utf-8"))
        version = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(manifest["version"], version)
        self.assertTrue(manifest["files"])
        paths = {entry["path"] for entry in manifest["files"]}
        for required in ("docs/DEVELOPMENT.md", "docs/en/DEVELOPMENT.md", "README.md"):
            self.assertIn(required, paths)

    def test_the_removed_provenance_file_is_not_reintroduced(self):
        self.assertFalse((REPOSITORY_ROOT / "SOURCE_PROVENANCE.json").exists())


class PackageBuildTests(unittest.TestCase):
    def build(self, platform: str) -> tuple[Path, dict]:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        out = Path(self.temp.name)
        summary = build_release.build(platform, out_dir=out)
        return out, summary

    def test_zip_layout_matches_documented_installation(self):
        out, summary = self.build("macos")
        name = build_release.package_name("macos")
        archive_path = out / f"{name}.zip"
        self.assertTrue(archive_path.is_file())
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            # Unzipping must give a folder you can run ./install.sh in.
            self.assertIn("install.sh", names)
            self.assertIn("uninstall.sh", names)
            self.assertIn("scripts/install_macos.py", names)
            executable = archive.getinfo("install.sh").external_attr >> 16
            self.assertTrue(executable & 0o111, "install.sh must stay executable after unzip")
            plain = archive.getinfo("README.md").external_attr >> 16
            self.assertFalse(plain & 0o111, "documents must not be executable")
            for info in archive.infolist():
                self.assertEqual(info.date_time, build_release.FIXED_TIMESTAMP, info.filename)

    def test_shell_entry_points_keep_lf_and_executable_modes(self):
        out, _summary = self.build("macos")
        name = build_release.package_name("macos")
        with zipfile.ZipFile(out / f"{name}.zip") as archive:
            attributes = archive.read(".gitattributes").decode("utf-8")
            self.assertIn("*.sh text eol=lf", attributes)
            for relative in ("install.sh", "uninstall.sh", "tests/shell/installer_tests.sh"):
                data = archive.read(relative)
                self.assertNotIn(b"\r\n", data, f"{relative} must keep LF endings")
                self.assertTrue(data.startswith(b"#!/bin/sh\n"), relative)
                self.assertEqual(stat.S_IMODE(archive.getinfo(relative).external_attr >> 16), 0o755, relative)

    def test_sha256_file_matches_the_archive(self):
        out, summary = self.build("macos")
        name = build_release.package_name("macos")
        digest = hashlib.sha256((out / f"{name}.zip").read_bytes()).hexdigest()
        self.assertEqual(digest, summary["zip_sha256"])
        self.assertEqual((out / f"{name}.zip.sha256").read_text(encoding="utf-8"),
                         f"{digest}  {name}.zip\n")

    def test_manifest_records_provenance_and_hashes(self):
        out, summary = self.build("windows")
        name = build_release.package_name("windows")
        manifest = json.loads((out / f"{name}.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip())
        self.assertEqual(manifest["platform"], "windows")
        self.assertEqual(manifest["evidence"]["level"], "REGRESSION_VERIFIED_ON_WINDOWS_DEVELOPMENT_HOST")
        paths = {entry["path"]: entry for entry in manifest["files"]}
        self.assertEqual(hashlib.sha256((REPOSITORY_ROOT / "README.md").read_bytes()).hexdigest(),
                         paths["README.md"]["sha256"])

    def test_macos_package_is_marked_experimental(self):
        _out, summary = self.build("macos")
        self.assertIn("EXPERIMENTAL", summary["evidence"]["level"])
        self.assertIn("acceptance-checklist-for-a-real-mac", summary["evidence"]["note"])

    def test_archive_is_reproducible(self):
        first = build_release.build("macos", out_dir=Path(tempfile.mkdtemp()))
        second = build_release.build("macos", out_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(first["zip_sha256"], second["zip_sha256"])


if __name__ == "__main__":
    unittest.main()
