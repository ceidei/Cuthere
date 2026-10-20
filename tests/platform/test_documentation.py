"""Documentation consistency: links, commands, product wording and packaging.

A GitHub project page is only useful if every relative link resolves, every
copyable command matches the files that actually exist, and the installation
archive contains the pages it links to.
"""

from __future__ import annotations

import posixpath
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

DOCS = [
    "README.md", "README.zh-CN.md", "AGENTS.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md",
    "docs/DEVELOPMENT.md", "docs/MACOS.md", "docs/AGENT_HANDOFF.md",
    "docs/en/DEVELOPMENT.md", "docs/en/AGENT_WORKFLOW.md", "docs/en/RELEASE_STATUS.md",
    "docs/en/AGENT_COMPATIBILITY.md", "docs/en/TROUBLESHOOTING.md",
    "docs/en/LICENSING_AND_PRIVACY.md",
]
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Material the public guides must not carry (internal development/migration
# history, merged component versions, the removed provenance file).
INTERNAL_NARRATIVE = (
    "SOURCE_PROVENANCE", "source provenance", "provenance",
    "standalone bridge", "retire the standalone", "retiring the standalone", "独立桥接",
    "rename", "renamed", "改名", "原名", "重命名",
    "protocol version", "bundle version", "1.2.6", "1.4.0",
    "personal Git history", "旧Git历史", "Git history",
    "unsaved work", "未保存的工作", "这台工作电脑", "working machine",
    "collaboration narrative", "内部协作",
)


def links(text: str) -> list[str]:
    return [target for target in LINK.findall(text) if not re.match(r"^[a-z]+:", target, re.I)]


def headings(text: str) -> set[str]:
    return {
        re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", line.lstrip("#").strip().lower(), flags=re.UNICODE))
        for line in text.splitlines() if line.startswith("#")
    }


class LinkTests(unittest.TestCase):
    def test_every_relative_link_resolves(self):
        missing = []
        for relative in DOCS:
            path = REPOSITORY_ROOT / relative
            self.assertTrue(path.is_file(), relative)
            for target in links(path.read_text(encoding="utf-8")):
                if target.startswith("#"):
                    continue
                file_part = target.split("#", 1)[0]
                if not file_part:
                    continue
                resolved = (path.parent / file_part).resolve()
                if not resolved.exists():
                    missing.append(f"{relative} -> {target}")
        self.assertEqual(missing, [])

    def test_anchor_links_point_at_headings(self):
        problems = []
        for relative in DOCS:
            path = REPOSITORY_ROOT / relative
            text = path.read_text(encoding="utf-8")
            for target in links(text):
                if "#" not in target:
                    continue
                file_part, anchor = target.split("#", 1)
                if not anchor:
                    continue
                other = (path.parent / file_part).resolve() if file_part else path
                other_headings = headings(other.read_text(encoding="utf-8")) if other.is_file() else set()
                if anchor not in other_headings:
                    problems.append(f"{relative} -> {target}")
        self.assertEqual(problems, [])


class CommandTests(unittest.TestCase):
    def test_documented_commands_point_at_real_files(self):
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        for command in (r".\install.ps1", r".\f8.ps1", "./install.sh", "unzip"):
            self.assertIn(command, readme, command)
        troubleshooting = (REPOSITORY_ROOT / "docs/en/TROUBLESHOOTING.md").read_text(encoding="utf-8")
        for command in (r".\uninstall.ps1", "./uninstall.sh"):
            self.assertIn(command, troubleshooting, command)
        development = (REPOSITORY_ROOT / "docs/en/DEVELOPMENT.md").read_text(encoding="utf-8")
        for command in ("pytest tests", "node --test", r".\tests\panel_install.ps1",
                        "tests/shell/installer_tests.sh"):
            self.assertIn(command, development, command)
        for path in ("install.ps1", "uninstall.ps1", "f8.ps1", "install.sh", "uninstall.sh",
                     "tests/shell/installer_tests.sh", "tests/panel_install.ps1",
                     "tests/premiere_lifecycle.cjs", "requirements-dev.txt",
                     "docs/DEVELOPMENT.md", "docs/en/DEVELOPMENT.md"):
            self.assertTrue((REPOSITORY_ROOT / path).is_file(), path)

    def test_every_cli_command_named_in_the_agent_guide_exists(self):
        from scripts.cli import COMMANDS

        for guide in ("docs/AGENT_HANDOFF.md", "docs/en/AGENT_WORKFLOW.md", "README.md"):
            text = (REPOSITORY_ROOT / guide).read_text(encoding="utf-8")
            for name in re.findall(r"\bcuthere (handoff|edit|roundtrip|read-batch|decision-template|doctor|transcribe|select-sequence|bridge-status)\b", text):
                self.assertIn(name, {*COMMANDS, "bridge-status"}, guide)

    def test_installer_options_documented_in_macos_guide_exist(self):
        from scripts.install_macos import parse_args

        guide = (REPOSITORY_ROOT / "docs/MACOS.md").read_text(encoding="utf-8")
        documented = set(re.findall(r"^\| `(--[a-z-]+)", guide, re.M))
        self.assertTrue(documented)
        values = {"--model": "small", "--language": "en", "--python": "/tmp/python3.11",
                  "--cep-version": "12", "--runtime-root": "/tmp/runtime",
                  "--extension-root": "/tmp/extensions", "--json-out": "/tmp/out.json"}
        for option in documented:
            # parse_args exits on an unknown flag, so a successful parse proves
            # the documented option exists in the installer.
            options = parse_args([option, values[option]] if option in values else [option])
            self.assertTrue(hasattr(options, option.lstrip("-").replace("-", "_")), option)


class PlatformStatusTests(unittest.TestCase):
    def test_english_and_chinese_readmes_agree_on_platform_status(self):
        english = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        chinese = (REPOSITORY_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        current = (REPOSITORY_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        for text in (english, chinese):
            self.assertIn("1.0.1", text)
            self.assertIn(current, text)
            self.assertIn("docs/MACOS.md", text)
            self.assertIn("Apple silicon", text)
            self.assertIn("macOS 14", text)
        self.assertIn("docs/en/RELEASE_STATUS.md", english)
        self.assertIn("docs/RELEASE_STATUS.md", chinese)
        self.assertIn("experimental", english.lower())
        self.assertIn("实验性", chinese)
        self.assertIn("has not been exercised on real Mac hardware", english)
        self.assertIn("没有", chinese)

    def test_readme_does_not_overclaim(self):
        english = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        lowered = english.lower()
        for phrase in ("perfect transcription", "any timeline", "fully automatic visual editing",
                       "supports all frame rates", "works on every platform"):
            self.assertNotIn(phrase, lowered)
        # The honest limits stay stated.
        self.assertIn("25 fps", english)
        self.assertIn("2.0", english)
        self.assertIn("10 seconds", english)

    def test_readme_has_no_placeholder_badges_or_figures(self):
        english = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("img.shields.io", english)
        self.assertNotIn("badge", english.lower())
        self.assertNotIn("TODO", english)
        self.assertNotIn("<username>", english)

    def test_public_guides_carry_no_internal_migration_narrative(self):
        problems = []
        for relative in DOCS:
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            lowered = text.lower()
            for marker in INTERNAL_NARRATIVE:
                if marker.lower() in lowered:
                    problems.append(f"{relative}: contains internal wording {marker!r}")
        self.assertEqual(problems, [])

    def test_removed_source_provenance_file_is_not_reintroduced(self):
        self.assertFalse((REPOSITORY_ROOT / "SOURCE_PROVENANCE.json").exists(),
                         "the public provenance-history file was deliberately removed from main")
        for relative in (*DOCS, "VALIDATION.json", "FILE_MANIFEST.json"):
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("SOURCE_PROVENANCE", text, relative)

    def test_development_pages_exist_and_are_linked(self):
        self.assertIn("docs/en/DEVELOPMENT.md",
                      (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("docs/DEVELOPMENT.md",
                      (REPOSITORY_ROOT / "README.zh-CN.md").read_text(encoding="utf-8"))
        for relative in ("docs/DEVELOPMENT.md", "docs/en/DEVELOPMENT.md"):
            self.assertTrue((REPOSITORY_ROOT / relative).is_file(), relative)

    def test_panel_manifest_keeps_its_identity(self):
        import xml.etree.ElementTree as ElementTree

        manifest_path = REPOSITORY_ROOT / "premiere_cep" / "review_capture_panel" / "CSXS" / "manifest.xml"
        root = ElementTree.parse(manifest_path).getroot()
        self.assertEqual(root.get("ExtensionBundleId"), "com.autovideoedit.reviewcapture")
        self.assertEqual(root.get("ExtensionBundleVersion"), "1.4.0")
        menu = root.find(".//Menu")
        self.assertIsNotNone(menu)
        self.assertEqual(menu.text, "Cuthere · 剪这儿")
        # Component versions are implementation details, not product-page facts.
        for readme in ("README.md", "README.zh-CN.md"):
            text = (REPOSITORY_ROOT / readme).read_text(encoding="utf-8")
            self.assertNotIn("ExtensionBundleVersion", text)
            self.assertNotIn("1.4.0", text)
            self.assertNotIn("1.2.6", text)
        # The Windows GPU flags stay in the shared source; the macOS installer
        # removes them from its staged copy only.
        self.assertIn("--use-angle=d3d11", manifest_path.read_text(encoding="utf-8"))

    def test_release_status_keeps_the_real_evidence_and_limits(self):
        status = (REPOSITORY_ROOT / "docs/RELEASE_STATUS.md").read_text(encoding="utf-8")
        self.assertIn("1.0.0", status)
        self.assertIn("1.0.1", status)
        self.assertIn("25 fps", status)
        self.assertIn("未执行", status)
        translation = (REPOSITORY_ROOT / "docs/en/RELEASE_STATUS.md").read_text(encoding="utf-8")
        self.assertIn("1.0.0", translation)
        self.assertIn("not performed", translation.lower())
        self.assertIn("experimental", translation.lower())


class DistributionLinkTests(unittest.TestCase):
    """Validate links in the actual macOS archive, not only in the checkout."""

    def test_links_inside_the_packaged_documentation_resolve(self):
        from scripts import build_release

        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "out"
            build_release.build("macos", out_dir=out)
            archive_path = out / f"{build_release.package_name('macos')}.zip"
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                self.assertIn("docs/DEVELOPMENT.md", names)
                self.assertIn("docs/en/DEVELOPMENT.md", names)
                missing = []
                for name in sorted(names):
                    if not name.endswith(".md"):
                        continue
                    text = archive.read(name).decode("utf-8")
                    for target in links(text):
                        file_part = target.split("#", 1)[0]
                        if not file_part:
                            continue
                        resolved = posixpath.normpath(
                            posixpath.join(posixpath.dirname(name), file_part)
                        )
                        if resolved not in names:
                            missing.append(f"{name} -> {target}")
                self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
