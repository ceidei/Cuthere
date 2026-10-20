"""Per-user locations for the Cuthere panel, runtime and bridge.

Windows values are exactly the ones 1.0 shipped with, so existing installs,
restore points and the ``f8.ps1`` launcher keep working. macOS values follow the
per-user CEP layout documented in docs/MACOS.md.

Paths are built as strings and only turned into ``Path`` objects at the end, so
the macOS layout is produced correctly (and comparably) even when the tests run
on Windows. Every function accepts an explicit platform, defaulting to the one
this interpreter really runs on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WINDOWS_RUNTIME_SEGMENTS = ("AutoVideoEdit", "PremiereReview")
MAC_RUNTIME_SEGMENTS = ("Library", "Application Support", "Cuthere")
MAC_CEP_SEGMENTS = ("Library", "Application Support", "Adobe", "CEP", "extensions")
PANEL_FOLDER = "AutoVideoEditReviewCapture"
LEGACY_PANEL_FOLDER = "autovideoedit"
PANEL_BUNDLE_ID = "com.autovideoedit.reviewcapture"
LEGACY_BUNDLE_ID = "com.autovideoedit.hermes"

# Documented overrides for tests and portable installs. AVE_BRIDGE_DIR is kept
# because the 1.0 Windows installer and f8.ps1 already use it.
OVERRIDE_VARS = {
    "bridge": ("CUTHERE_BRIDGE_DIR", "AVE_BRIDGE_DIR"),
    "runtime": ("CUTHERE_RUNTIME_ROOT",),
    "extension": ("CUTHERE_EXTENSION_ROOT",),
}


def current_platform() -> str:
    return "win32" if os.name == "nt" else sys.platform


def _environ(environ: dict[str, str] | None) -> dict[str, str]:
    return os.environ if environ is None else environ


def _override(kind: str, environ: dict[str, str]) -> str | None:
    for name in OVERRIDE_VARS[kind]:
        value = environ.get(name)
        if value:
            return value
    return None


def home_dir(platform: str | None = None, environ: dict[str, str] | None = None) -> Path:
    environ = _environ(environ)
    platform = current_platform() if platform is None else platform
    if platform == "win32":
        for name in ("USERPROFILE", "LOCALAPPDATA"):
            if environ.get(name):
                return Path(environ[name])
        return Path.home()
    return Path(environ.get("HOME") or Path.home())


def _home_text(platform: str, environ: dict[str, str]) -> str:
    if platform == "win32":
        for name in ("USERPROFILE", "LOCALAPPDATA"):
            if environ.get(name):
                return environ[name]
        return str(Path.home())
    return environ.get("HOME") or str(Path.home())


def _join_win(base: str, *parts: str) -> str:
    return base.rstrip("\\/") + "\\" + "\\".join(parts)


def _join_posix(base: str, *parts: str) -> str:
    return base.rstrip("/") + "/" + "/".join(parts)


def extension_root_text(platform: str | None = None, environ: dict[str, str] | None = None) -> str:
    """Directory Premiere loads per-user (unsigned, development) CEP extensions from."""
    environ = _environ(environ)
    platform = current_platform() if platform is None else platform
    override = _override("extension", environ)
    if override:
        return override
    home = _home_text(platform, environ)
    if platform == "win32":
        appdata = environ.get("APPDATA")
        base = appdata if appdata else _join_win(home, "AppData", "Roaming")
        return _join_win(base, "Adobe", "CEP", "extensions")
    return _join_posix(home, *MAC_CEP_SEGMENTS)


def runtime_root_text(platform: str | None = None, environ: dict[str, str] | None = None) -> str:
    """Versioned runtimes: venv, code copies, ipc, backups and install receipts."""
    environ = _environ(environ)
    platform = current_platform() if platform is None else platform
    override = _override("runtime", environ)
    if override:
        return override
    home = _home_text(platform, environ)
    if platform == "win32":
        localappdata = environ.get("LOCALAPPDATA")
        base = localappdata if localappdata else _join_win(home, "AppData", "Local")
        return _join_win(base, *WINDOWS_RUNTIME_SEGMENTS)
    return _join_posix(home, *MAC_RUNTIME_SEGMENTS)


def bridge_dir_text(platform: str | None = None, environ: dict[str, str] | None = None) -> str:
    """Folder shared by the agent CLI and the panel for commands/responses/locks."""
    environ = _environ(environ)
    platform = current_platform() if platform is None else platform
    override = _override("bridge", environ)
    if override:
        return override
    if platform == "win32":
        return r"C:\Users\Public\Documents\ave-bridge"
    return _child_text(platform, runtime_root_text(platform, environ), "ave-bridge")


def _child_text(platform: str, parent: str, *parts: str) -> str:
    return _join_win(parent, *parts) if platform == "win32" else _join_posix(parent, *parts)


def _platform_of(platform: str | None) -> str:
    return current_platform() if platform is None else platform


def bin_dir_text(platform: str | None = None, environ: dict[str, str] | None = None) -> str:
    return _child_text(_platform_of(platform), runtime_root_text(platform, environ), "bin")


def cli_wrapper_path_text(platform: str | None = None, environ: dict[str, str] | None = None) -> str:
    platform = _platform_of(platform)
    name = "cuthere.ps1" if platform == "win32" else "cuthere"
    return _child_text(platform, bin_dir_text(platform, environ), name)


def extension_root(platform: str | None = None, environ: dict[str, str] | None = None) -> Path:
    return Path(extension_root_text(platform, environ))


def runtime_root(platform: str | None = None, environ: dict[str, str] | None = None) -> Path:
    return Path(runtime_root_text(platform, environ))


def bridge_dir(platform: str | None = None, environ: dict[str, str] | None = None) -> Path:
    return Path(bridge_dir_text(platform, environ))


def install_record_path_text(platform: str | None = None, environ: dict[str, str] | None = None) -> str:
    return _child_text("win32" if (platform or current_platform()) == "win32" else "darwin", runtime_root_text(platform, environ), "current_install.json")


def install_record_path(platform: str | None = None, environ: dict[str, str] | None = None) -> Path:
    return Path(install_record_path_text(platform, environ))


def bin_dir(platform: str | None = None, environ: dict[str, str] | None = None) -> Path:
    return Path(bin_dir_text(platform, environ))


def cli_wrapper_path(platform: str | None = None, environ: dict[str, str] | None = None) -> Path:
    return Path(cli_wrapper_path_text(platform, environ))


def ssxs_defaults_domain(cep_version: str = "12") -> str:
    """User-defaults domain that CEP reads for the unsigned-development flag."""
    return f"com.adobe.CSXS.{cep_version}"


def is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
