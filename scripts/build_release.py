"""Build a clean source installation archive for one platform.

    python scripts/build_release.py --platform windows   # install.ps1 / f8.ps1
    python scripts/build_release.py --platform macos     # install.sh / uninstall.sh

Writes, into output/:

    <name>.zip          deterministic archive (fixed timestamps, sorted paths,
                        executable bit recorded for shell entry points)
    <name>.zip.sha256   the hash, in `sha256sum -c` format as well as readable
    <name>.manifest.json  file list with sizes/hashes, version, provenance and
                        the explicit evidence level of that platform

Contents are an **allowlist**; nothing else can end up in a package. A leak scan
then refuses to build if any absolute local path, credential, media, recording
or model weight is found. Package layout matches the documented unzip/install
commands: the zip root is the project folder itself.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

RELEASE_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = RELEASE_ROOT / "output"

# Files shipped on every platform. Anything not listed is excluded by default.
COMMON_FILES = [
    "AGENTS.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "README.zh-CN.md",
    "THIRD_PARTY_NOTICES.md",
    "VALIDATION.json",
    "VERSION",
    ".gitattributes",
    ".gitignore",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/bug_report_en.yml",
    ".agents/skills/cuthere/SKILL.md",
    ".agents/skills/cuthere/agents/openai.yaml",
    "requirements-common.txt",
    "requirements-dev.txt",
    "docs/AGENT_HANDOFF.md",
    "docs/AGENT_WORKFLOW.md",
    "docs/DEVELOPMENT.md",
    "docs/MACOS.md",
    "docs/RELEASE_STATUS.md",
    "docs/TROUBLESHOOTING.md",
    "docs/LICENSING_AND_PRIVACY.md",
    "docs/AGENT_COMPATIBILITY.md",
    "premiere_cep/review_capture_panel/CSXS/manifest.xml",
    "premiere_cep/review_capture_panel/index.html",
    "premiere_cep/review_capture_panel/css/bridge.css",
    "premiere_cep/review_capture_panel/css/review_panel.css",
    "premiere_cep/review_capture_panel/js/bridge.js",
    "premiere_cep/review_capture_panel/js/cep_adapter.js",
    "premiere_cep/review_capture_panel/js/hotkey_binding.js",
    "premiere_cep/review_capture_panel/js/hostscript.jsx",
    "premiere_cep/review_capture_panel/js/project_lifecycle.jsx",
    "premiere_cep/review_capture_panel/js/review_host.jsx",
    "premiere_cep/review_capture_panel/js/review_panel.js",
    "schemas/premiere_review/edit-decisions.schema.json",
    "services/review_capture/manifest.schema.json",
    ".github/workflows/macos.yml",
    ".github/workflows/windows.yml",
    "tests/shell/installer_tests.sh",
    "tests/premiere_lifecycle.cjs",
    "tests/review_ptt_release.cjs",
    "tests/cep_adapter.cjs",
    "tests/single_panel.cjs",
    "tests/panel_platform.cjs",
    "tests/panel_shortcut.cjs",
    "tests/fixtures/shortcut_vectors.json",
    "scripts/build_release.py",
    "scripts/update_manifests.py",
]
COMMON_TREES = ["engine", "services", "scripts", "docs/en"]

WINDOWS_FILES = [
    "install.ps1",
    "uninstall.ps1",
    "f8.ps1",
    "requirements.txt",
    "requirements-lock.txt",
    "scripts/panel_install.ps1",
    "tests/panel_install.ps1",
]
MACOS_FILES = [
    "install.sh",
    "uninstall.sh",
    "requirements-macos.txt",
    "requirements-macos-optional.txt",
    "scripts/install_macos.py",
]
# Files that exist in the repository but must never be packaged.
DENY_GLOBS = [
    "work/**", "output/**", ".git/**",
    "**/__pycache__/**", "**/*.pyc", "**/*.log", "**/*.wav", "**/*.mp3", "**/*.mp4",
    "**/*.mov", "**/*.prproj", "**/*.key", "**/*.pem", "**/runtime.json",
    "**/current_install.json", "**/*.zip", "FILE_MANIFEST.json", "SOURCE_PROVENANCE.json",
]
EXECUTABLE_SUFFIXES = {".sh"}
EXECUTABLE_FILES = {"install.sh", "uninstall.sh", "tests/shell/installer_tests.sh"}
FIXED_TIMESTAMP = (2026, 10, 20, 0, 0, 0)

# Patterns that must never appear in shipped text.
LEAK_PATTERNS = [
    # A real user's home: not the documented public folders and not test fixtures.
    (re.compile(r"[A-Za-z]:\\Users\\(?!Public\\|fixture\\)[A-Za-z0-9 ._-]+\\"),
     "absolute Windows user path"),
    # Must start a path (or a quoted/`file://` equivalent), not appear mid-path.
    (re.compile(r"(?:(?<=[\s'\"(=])|^)/Users/(?!fixture/?|example|测试|Fixture )[A-Za-z0-9 ._-]+/", re.M),
     "absolute macOS user path"),
    # This development checkout must never be baked into a public package.
    (re.compile(r"[A-Za-z]:[\\/]+work[\\/]+Cuthere", re.I), "development checkout path"),
    (re.compile(r"(?i)(api[_-]?key|secret|password|bearer)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}"),
     "credential-looking value"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), "API token"),
    (re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"), "private key"),
    # Session metadata from an agent run (request identifiers are public, sessions are not).
    (re.compile(r"session-[0-9a-f]{8}-[0-9a-f]{4}-"), "agent session identifier"),
]
TEXT_SUFFIXES = {".md", ".py", ".js", ".jsx", ".html", ".css", ".json", ".txt",
                 ".ps1", ".sh", ".yml", ".yaml", ".xml"}
# Synthetic values used by the tests that verify the leak scan itself and by
# the Windows path-layout fixtures. They are only tolerated inside tests/.
TEST_FIXTURE_MARKERS = (
    "C:\\Users\\f\\", "C:\\Users\\fixture\\", "/Users/fixture/",
    "sk-" + "abcdefghijklmnopqrstuvwxyz",
    "api_key = " + "sk-" + "abcdefghijklmnopqrstuvwxyz",
)


def version() -> str:
    return (RELEASE_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def package_name(platform: str) -> str:
    return f"cuthere-{version()}-{platform}-source"


def is_denied(relative: str) -> bool:
    """True for anything the packaging rules exclude before selection."""
    lowered = relative.replace("\\", "/")
    parts = lowered.split("/")
    if any(part in {"__pycache__", ".git", "work", "output", "node_modules"} for part in parts):
        return True
    return any(fnmatch.fnmatch(lowered, pattern) for pattern in DENY_GLOBS)


def _iter_tree(relative: str) -> list[str]:
    root = RELEASE_ROOT / relative
    if not root.exists():
        return []
    found = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            candidate = path.relative_to(RELEASE_ROOT).as_posix()
            if not is_denied(candidate):
                found.append(candidate)
    return found


def select_files(platform: str) -> list[str]:
    if platform not in {"windows", "macos"}:
        raise SystemExit(f"unknown platform {platform!r}")
    files: list[str] = list(COMMON_FILES)
    for tree in COMMON_TREES:
        files.extend(_iter_tree(tree))
    files.extend(WINDOWS_FILES if platform == "windows" else MACOS_FILES)
    # Keep the cross-platform documentation page on Windows too (it documents
    # the candidate) but never ship the other platform's entry points.
    selected = []
    for relative in sorted(set(files)):
        if platform == "windows" and relative in MACOS_FILES:
            continue
        if platform == "macos" and relative in WINDOWS_FILES:
            continue
        # A PowerShell filesystem test has no meaning in the macOS archive.
        if platform == "macos" and relative == "tests/panel_install.ps1":
            continue
        path = RELEASE_ROOT / relative
        if not path.is_file():
            raise SystemExit(f"missing required file: {relative}")
        selected.append(relative)
    return selected


def leak_scan(files: list[str]) -> list[str]:
    problems = []
    for relative in files:
        if is_denied(relative):
            problems.append(f"denied path in package: {relative}")
        path = RELEASE_ROOT / relative
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"non-UTF-8 file: {relative}")
            continue
        for pattern, label in LEAK_PATTERNS:
            for match in pattern.finditer(text):
                if relative.startswith("tests/") and any(marker in match.group(0) for marker in TEST_FIXTURE_MARKERS):
                    continue
                problems.append(f"{relative}: {label}: {match.group(0)[:60]}")
    return problems


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build(platform: str, *, out_dir: Path = OUTPUT_ROOT, dry_run: bool = False) -> dict:
    files = select_files(platform)
    problems = leak_scan(files)
    if problems:
        raise SystemExit("refusing to build:\n  " + "\n  ".join(problems))
    name = package_name(platform)
    zip_path = out_dir / f"{name}.zip"
    manifest_path = out_dir / f"{name}.manifest.json"
    summary = {
        "product": "Cuthere",
        "version": version(),
        "platform": platform,
        "package": zip_path.name,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": [],
    }
    if dry_run:
        summary["dry_run"] = True
        summary["files"] = [{"path": relative, "bytes": (RELEASE_ROOT / relative).stat().st_size,
                             "sha256": sha256(RELEASE_ROOT / relative)} for relative in files]
        return summary
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            source = RELEASE_ROOT / relative
            info = zipfile.ZipInfo(relative, date_time=FIXED_TIMESTAMP)
            info.create_system = 3  # Unix, so the mode bits are meaningful
            info.compress_type = zipfile.ZIP_DEFLATED
            executable = relative in EXECUTABLE_FILES or source.suffix in EXECUTABLE_SUFFIXES
            info.external_attr = ((0o755 if executable else 0o644) | 0o100000) << 16
            archive.writestr(info, source.read_bytes())
            summary["files"].append({
                "path": relative,
                "bytes": source.stat().st_size,
                "sha256": sha256(source),
                "mode": "0755" if executable else "0644",
            })
    digest = sha256(zip_path)
    (out_dir / f"{name}.zip.sha256").write_text(f"{digest}  {name}.zip\n", encoding="utf-8")
    summary["zip_bytes"] = zip_path.stat().st_size
    summary["zip_sha256"] = digest
    summary["file_count"] = len(summary["files"])
    summary["evidence"] = platform_evidence(platform)
    manifest_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def platform_evidence(platform: str) -> dict:
    if platform == "windows":
        return {
            "level": "REGRESSION_VERIFIED_ON_WINDOWS_DEVELOPMENT_HOST",
            "note": "Automated regression on the ported shared code; the real Premiere acceptance "
                    "remains the recorded 1.0.1 run in docs/RELEASE_STATUS.md.",
        }
    return {
        "level": "EXPERIMENTAL_NOT_ACCEPTED_ON_MAC_HARDWARE",
        "note": "macOS code paths are covered by automated checks only. A real Mac with Premiere, a "
                "microphone and a Function key must complete docs/MACOS.md#acceptance-checklist-for-a-real-mac "
                "before this package may be described as working on macOS.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a clean Cuthere source installation archive")
    parser.add_argument("--platform", choices=["windows", "macos"], required=True)
    parser.add_argument("--out", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--dry-run", action="store_true", help="list and scan the contents without writing a zip")
    args = parser.parse_args(argv)
    summary = build(args.platform, out_dir=args.out, dry_run=args.dry_run)
    print(json.dumps({key: summary.get(key) for key in
                      ("package", "platform", "version", "file_count", "zip_bytes", "zip_sha256", "dry_run")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
