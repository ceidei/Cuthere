"""Cuthere installer for macOS (also used by uninstall.sh).

Usage (normally through ./install.sh):

    python3.11 scripts/install_macos.py                  # plan only (default)
    python3.11 scripts/install_macos.py --apply          # install
    python3.11 scripts/install_macos.py --check-only     # environment report
    python3.11 scripts/install_macos.py --uninstall [--restore-previous] [--purge-runtime]

Guarantees, all enforced here rather than by shell quoting:

* default invocation writes nothing; every mutation needs ``--apply`` (or the
  explicit ``--uninstall`` action);
* everything lives under the current user's home: no sudo, no /Library, no
  system Python changes, no Homebrew;
* the previous panel folder, the previous ``PlayerDebugMode`` value, the
  previous install record and the previous CLI wrapper are moved/copied into a
  versioned backup first (also with ``--no-defaults``);
* any failure rolls the extension folder, the defaults value, the installation
  record and the CLI wrapper back;
* uninstalling removes the generated ``bin/cuthere`` command and, with
  ``--restore-previous``, restores the previous record and wrapper target;
  ``--purge-runtime`` only deletes Cuthere's own runtime artifacts;
* paths with spaces and non-ASCII characters are handled as Path objects;
* model weights are downloaded only with ``--download-model``.

The logic is deliberately importable so the Windows development host can test
plan rendering, staging, rollback and receipts against a sandboxed HOME.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

RELEASE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE_ROOT))

from services import platform_paths  # noqa: E402

SUPPORTED_PYTHON = (3, 11)
CODE_DIRS = ("engine", "services", "scripts", "docs")
DEFAULT_MODELS = ("tiny", "base", "small", "medium")
DEFAULTS_DOMAIN_PREFIX = "com.adobe.CSXS."
OPTIONAL_REQUIREMENTS = RELEASE_ROOT / "requirements-macos-optional.txt"
LANGUAGE_PATTERN = re.compile(r"^(auto|[a-z]{2,3}([_-][A-Za-z0-9]{2,8})*)$")
# This first experimental macOS candidate is scoped to Apple silicon + macOS 14.
# The pinned runtime set cannot be installed on an Intel Mac: official PyPI
# metadata for onnxruntime==1.31.0 lists macOS wheels only for macOS 14+ arm64
# (no macOS x86_64 wheel), and the av==18.1.0 arm64 wheel also targets macOS 14.
# The exact wheel lists were captured by an independent review in
# pypi-wheel-evidence.json; the public URLs below are what the guide cites.
MINIMUM_MACOS = (14, 0)
MINIMUM_MACOS_TEXT = "14.0"
MACOS_WHEEL_EVIDENCE = {
    "onnxruntime==1.31.0": {
        "url": "https://pypi.org/project/onnxruntime/1.31.0/#files",
        "detail": "official macOS wheels: cp311..cp314 macosx_14_0_arm64 only; no macOS x86_64 wheel",
    },
    "av==18.1.0": {
        "url": "https://pypi.org/project/av/18.1.0/#files",
        "detail": "arm64 wheel is macosx_14_0_arm64; the Intel wheel does not help while onnxruntime has no Intel macOS wheel",
    },
    "PyObjC 11.1": {
        "url": "https://pypi.org/project/PyObjC-Framework-Quartz/11.1/#files",
        "detail": "universal2 wheels; the optional key backend still needs macOS permissions",
    },
}
# GPU switches that made the panel render on Windows. CEP on macOS uses its own
# Metal/CoreAnimation configuration, so the staged macOS panel drops the whole
# CEFCommandLine block instead of passing Direct3D/Vulkan flags it cannot use.
CEF_COMMAND_LINE = re.compile(r"\s*<CEFCommandLine>.*?</CEFCommandLine>", re.S)


class InstallError(RuntimeError):
    """A user-facing installation failure; the message says what to do next."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def version() -> str:
    return (RELEASE_ROOT / "VERSION").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Platform commands (injectable so tests never touch the real machine)
# ---------------------------------------------------------------------------


def _run(command: list[str], *, env: dict[str, str] | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command with file-based capture.

    Pipes are avoided on purpose: some sandboxes deny creating pipes for child
    processes, and stdout/stderr plus the exit code are all the installer reads.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        out_path = Path(folder) / "out.txt"
        with out_path.open("w+", encoding="utf-8", errors="replace") as out:
            completed = subprocess.run(command, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                       env=env, timeout=timeout, check=False)
            out.seek(0)
            text = out.read()
    return subprocess.CompletedProcess(args=command, returncode=completed.returncode, stdout=text, stderr=text)


@dataclass
class HostCommands:
    """Everything that touches the OS outside our own folders."""

    read_defaults: Callable[[str, str], str | None] = field(default=None)  # type: ignore[assignment]
    write_defaults: Callable[[str, str, str], None] = field(default=None)  # type: ignore[assignment]
    delete_defaults: Callable[[str, str], None] = field(default=None)  # type: ignore[assignment]
    run_python: Callable[..., subprocess.CompletedProcess[str]] = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.read_defaults = self.read_defaults or read_defaults
        self.write_defaults = self.write_defaults or write_defaults
        self.delete_defaults = self.delete_defaults or delete_defaults
        self.run_python = self.run_python or _run


def read_defaults(domain: str, key: str) -> str | None:
    completed = _run(["defaults", "read", domain, key])
    return completed.stdout.strip() if completed.returncode == 0 else None


def write_defaults(domain: str, key: str, value: str) -> None:
    completed = _run(["defaults", "write", domain, key, "-string", value])
    if completed.returncode != 0:
        raise InstallError(f"could not write {domain} {key}: {completed.stderr.strip() or completed.returncode}")


def delete_defaults(domain: str, key: str) -> None:
    _run(["defaults", "delete", domain, key])


# ---------------------------------------------------------------------------
# Options and environment checks
# ---------------------------------------------------------------------------


@dataclass
class Options:
    apply: bool = False
    check_only: bool = False
    download_model: bool = False
    model: str = "medium"
    # None means "not given on the command line": the previous installation's
    # recorded language is preserved, or auto detection is used for a new install.
    language: str | None = None
    python: Path | None = None
    cep_version: str = "12"
    runtime_root: Path | None = None
    extension_root: Path | None = None
    no_defaults: bool = False
    uninstall: bool = False
    restore_previous: bool = False
    purge_runtime: bool = False
    skip_dependencies: bool = False
    skip_pip: bool = False
    json_out: Path | None = None


def _human(path: Path) -> str:
    home = Path(os.path.expanduser("~"))
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def find_python(explicit: Path | None, *, host: HostCommands | None = None) -> tuple[Path | None, str]:
    """Locate a Python 3.11 interpreter; returns (path, explanation)."""

    def probe(interpreter: Path) -> subprocess.CompletedProcess[str]:
        command = [str(interpreter), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"]
        return host.run_python(command, timeout=60) if host else _run(command, timeout=60)

    if explicit is not None:
        if not explicit.is_file():
            return None, f"--python {explicit} does not exist"
        completed = probe(explicit)
        if completed.returncode != 0:
            return None, f"--python {explicit} could not be executed: {completed.stderr.strip()}"
        if completed.stdout.strip() != "3.11":
            return None, f"--python {explicit} is Python {completed.stdout.strip()}; 3.11 is required"
        return explicit, "explicit"
    candidates: list[Path] = []
    found = shutil.which("python3.11")
    if found:
        candidates.append(Path(found))
    framework = Path("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11")
    if framework.is_file():
        candidates.append(framework)
    for candidate in candidates:
        completed = probe(candidate)
        if completed.returncode == 0 and completed.stdout.strip() == "3.11":
            return candidate, "found"
    if candidates:
        return None, "python3.11 was found but is not a Python 3.11 interpreter"
    return None, "python3.11 was not found (install it from python.org, or pass --python)"


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def host_platform_info() -> dict[str, str]:
    """The host facts the architecture/OS policy is based on (injectable)."""
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "macos_version": platform.mac_ver()[0] or "",
    }


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(text or "").split(".")[:3]:
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts)


def host_support(info: dict[str, str] | None = None) -> dict[str, Any]:
    """Is this host inside the evidence-backed scope of the candidate?

    The pinned runtime cannot be installed on an Intel Mac and the arm64 wheels
    require macOS 14, so an unsupported host is refused *before* anything is
    written instead of failing halfway through pip.
    """
    info = dict(info or host_platform_info())
    machine = str(info.get("machine") or "").lower()
    system = str(info.get("system") or "")
    version_text = str(info.get("macos_version") or "")
    version = _version_tuple(version_text)
    problems: list[str] = []
    if system != "Darwin":
        problems.append(f"the host is {system or 'unknown'}, not macOS")
    else:
        if machine not in {"arm64", "aarch64"}:
            problems.append(f"the CPU is {machine or 'unknown'}; onnxruntime==1.31.0 has no macOS Intel wheel")
        if not version or version < MINIMUM_MACOS:
            problems.append(
                f"macOS {version_text or 'unknown'} is older than {MINIMUM_MACOS_TEXT}; the pinned "
                "onnxruntime==1.31.0 arm64 wheel requires macOS 14.0"
            )
    return {
        "supported": not problems,
        "system": system,
        "machine": machine,
        "macos_version": version_text,
        "minimum": "Apple silicon (arm64) on macOS 14.0 or later",
        "reason": "; ".join(problems),
        "evidence": {name: entry["url"] for name, entry in MACOS_WHEEL_EVIDENCE.items()},
    }


def runtime_roots(options: Options) -> tuple[Path, Path, Path]:
    # Absolute paths everywhere: a relative --runtime-root would otherwise put
    # relative interpreter paths in runtime.json and in the CLI wrapper.
    extension = Path(os.path.abspath(options.extension_root or platform_paths.extension_root("darwin")))
    runtime = Path(os.path.abspath(options.runtime_root or platform_paths.runtime_root(
        "darwin", {"HOME": str(Path.home())}
    )))
    bridge = runtime / "ave-bridge"
    return extension, runtime, bridge


def environment_report(options: Options, *, host: HostCommands | None = None,
                       info: dict[str, str] | None = None) -> dict[str, Any]:
    python, python_note = find_python(options.python, host=host)
    ffmpeg = find_ffmpeg()
    extension, runtime, bridge = runtime_roots(options)
    support = host_support(info)
    free_gb = round(shutil.disk_usage(runtime.parent if runtime.parent.exists() else Path.home()).free / 1e9, 1)
    return {
        "checks": [
            {"name": "macOS host support", "status": "PASS" if support["supported"] else "FAIL",
             "detail": support["minimum"] if support["supported"]
             else f"{support['reason']}. Supported: {support['minimum']}."},
            {"name": "Python 3.11", "status": "PASS" if python else "FAIL",
             "detail": f"{python}" if python else python_note},
            {"name": "FFmpeg", "status": "PASS" if ffmpeg else "WARN",
             "detail": ffmpeg or "not found on PATH; recording and simple edits still work, "
                                 "complex media edits need it (brew install ffmpeg)"},
            {"name": "CEP extension folder", "status": "PASS" if extension.parent.is_dir() else "WARN",
             "detail": str(extension) + ("" if extension.parent.is_dir() else " (parent folder missing; Premiere will create it)")},
            {"name": "Runtime folder", "status": "PASS", "detail": str(runtime)},
            {"name": "Bridge folder", "status": "PASS", "detail": str(bridge)},
            {"name": "Free disk", "status": "PASS" if free_gb > 3 else "WARN", "detail": f"{free_gb} GB"},
            {"name": "User scope", "status": "PASS", "detail": "per-user installation; sudo is never used"},
        ],
        "python": str(python) if python else None,
        "host_support": support,
        "platform": {"machine": support["machine"], "system": support["system"],
                     "macos_version": support["macos_version"], "release": platform.release()},
    }


# ---------------------------------------------------------------------------
# Plan and apply
# ---------------------------------------------------------------------------


def panel_source() -> Path:
    return RELEASE_ROOT / "premiere_cep" / "review_capture_panel"


def venv_bin(venv: Path) -> Path:
    """Interpreter inside a virtualenv: macOS keeps it in bin/, Windows in Scripts/."""
    if os.name == "nt":
        candidate = venv / "Scripts" / "python.exe"
        return candidate if candidate.is_file() else venv / "bin" / "python"
    return venv / "bin" / "python"


def install_destination(options: Options) -> Path:
    extension, _, _ = runtime_roots(options)
    return extension / platform_paths.PANEL_FOLDER


def normalize_language(value: object) -> str:
    """Validate a faster-whisper language code; ``auto`` means detect."""
    text = str(value or "").strip()
    if not text:
        return "auto"
    if not LANGUAGE_PATTERN.fullmatch(text):
        raise InstallError(f"unsupported transcription language {text!r}; use auto or an ISO code such as zh/en")
    return text


def previous_runtime_config(options: Options) -> dict[str, Any]:
    """The runtime.json of the panel currently installed, if any (read-only)."""
    path = install_destination(options) / "runtime.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def resolve_language(options: Options) -> tuple[str, str]:
    """Return (language, source) without changing a saved preference.

    An explicit ``--language`` wins. Otherwise an installation that already
    recorded a language keeps it; only a fresh install (or an installation
    predating the setting) falls back to automatic detection.
    """
    if options.language is not None:
        return normalize_language(options.language), "explicit"
    previous = previous_runtime_config(options)
    recorded = previous.get("transcription_language") or previous.get("language")
    if recorded:
        return normalize_language(recorded), "previous-install"
    return "auto", "default-auto"


def prepare_panel_manifest(panel_stage: Path) -> dict[str, Any]:
    """Make the staged macOS panel manifest carry no Windows-only CEP flags.

    The source manifest is shared with Windows, where ``--use-angle=d3d11`` and
    the Vulkan switches fixed a blank panel. CEP on macOS uses its own graphics
    configuration, so the whole CEFCommandLine block is removed for the installed
    macOS copy. The bundle id, menu name, size and host list are untouched.
    """
    manifest = panel_stage / "CSXS" / "manifest.xml"
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"staged panel has no readable CSXS/manifest.xml: {exc}") from exc
    updated, removed = CEF_COMMAND_LINE.subn("", text)
    if removed:
        manifest.write_text(updated, encoding="utf-8")
    # Validate the result before it can replace a working panel, and make sure
    # the Windows-only switch is really gone.
    try:
        import xml.etree.ElementTree as ElementTree

        root = ElementTree.fromstring(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        raise InstallError(f"staged macOS manifest is not valid XML: {exc}") from exc
    if root.get("ExtensionBundleId") != platform_paths.PANEL_BUNDLE_ID:
        raise InstallError("staged panel manifest changed the extension bundle id")
    final_text = manifest.read_text(encoding="utf-8")
    if "--use-angle=d3d11" in final_text:
        raise InstallError("staged macOS manifest still contains the Windows-only Direct3D switch")
    return {
        "cef_command_line": "removed" if removed else "already absent",
        "removed_windows_gpu_flags": bool(removed),
        "bundle_id": root.get("ExtensionBundleId"),
    }


def build_plan(options: Options, *, host: HostCommands | None = None,
               info: dict[str, str] | None = None) -> dict[str, Any]:
    extension, runtime, bridge = runtime_roots(options)
    destination = extension / platform_paths.PANEL_FOLDER
    legacy = extension / platform_paths.LEGACY_PANEL_FOLDER
    record = runtime / "current_install.json"
    python, python_note = find_python(options.python, host=host)
    language, language_source = resolve_language(options)
    support = host_support(info)
    return {
        "mode": "PLAN",
        "product": "Cuthere",
        "version": version(),
        "platform": "darwin",
        "scope": "current user only; no sudo, no system-wide paths",
        "host_support": support,
        "panel": {"source": str(panel_source()), "destination": str(destination),
                  "bundle_id": platform_paths.PANEL_BUNDLE_ID,
                  "existing": destination.exists(),
                  "manifest": "the staged macOS copy removes the Windows-only CEFCommandLine "
                              "(Direct3D/Vulkan) switches; Windows keeps them",
                  "backup": "moved into <runtime>/backups/<panel>_<stamp>" if destination.exists() else None},
        "legacy_bundle": {"path": str(legacy), "action": "retire only if its manifest bundle id matches "
                          f"{platform_paths.LEGACY_BUNDLE_ID}"},
        "runtime": {"root": str(runtime), "versioned": str(runtime / "versions" / f"f8-{version()}-<stamp>"),
                    "record": str(record), "previous_record_backed_up": record.exists()},
        "bridge": str(bridge),
        "python": {"interpreter": str(python) if python else None, "note": python_note,
                   "venv": str(runtime / "versions" / f"f8-{version()}-<stamp>" / "venv"),
                   "requirements": "requirements-macos.txt",
                   "optional": "requirements-macos-optional.txt (PyObjC key backend; a failed optional "
                               "install is reported in the receipt and does not abort the install)"},
        "defaults": ({"domain": platform_paths.ssxs_defaults_domain(options.cep_version), "key": "PlayerDebugMode",
                      "value": "1", "previous_saved": True} if not options.no_defaults else "skipped (--no-defaults)"),
        "model": {"name": options.model, "download": bool(options.download_model),
                  "note": "weights are downloaded only with --download-model and are never bundled; "
                          "a missing cache is reported and does not abort the install"},
        "language": language,
        "language_source": language_source,
        "cli": {"wrapper": str(runtime / "bin" / "cuthere")},
        "steps": [
            "verify the host is Apple silicon on macOS 14 or later (clear refusal before any change)",
            "verify Python 3.11 (and FFmpeg when present)",
            "create the versioned runtime folder and copy engine/, services/, scripts/, docs/, VERSION",
            "create an isolated venv and install requirements-macos.txt, then try the optional PyObjC key backend",
            ("download and verify the transcription model" if options.download_model
             else "check the transcription model cache; warn (do not fail) when it is not prepared"),
            "write runtime.json into the staged panel and remove the Windows-only CEF switches",
            "back up the existing panel, retire a matching legacy bundle, move the staged panel into place",
            "save the previous PlayerDebugMode value, then enable it for the current user",
            "install current_install.json and print the next steps",
        ],
        "preserves": ["existing panel folders (backed up)", "installation records (copied)",
                      "media and project notes (never touched by install/uninstall)"],
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _is_within(root: Path, path: Path) -> bool:
    return platform_paths.is_within(root, path)


def assert_child(path: Path, root: Path, label: str) -> Path:
    resolved = Path(os.path.abspath(path))
    if not _is_within(root, resolved):
        raise InstallError(f"{label} is outside {root}: {resolved}")
    return resolved


def panel_bundle_id(panel: Path) -> str | None:
    manifest = panel / "CSXS" / "manifest.xml"
    if not manifest.is_file():
        return None
    try:
        import xml.etree.ElementTree as ElementTree

        root = ElementTree.parse(manifest).getroot()
    except Exception:
        return None
    return root.get("ExtensionBundleId")


def copy_code(stage: Path) -> None:
    code_root = stage / "code"
    code_root.mkdir(parents=True, exist_ok=False)
    for name in CODE_DIRS:
        source = RELEASE_ROOT / name
        if not source.is_dir():
            raise InstallError(f"release is incomplete: missing {name}/")
        shutil.copytree(
            source, code_root / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.log"),
        )
    shutil.copy2(RELEASE_ROOT / "VERSION", code_root / "VERSION")


def write_runtime_config(panel_stage: Path, options: Options, stamp: str, python: Path, venv_python: Path,
                         language: str, language_source: str, optional_status: str, model_status: str) -> dict[str, Any]:
    extension, runtime, bridge = runtime_roots(options)
    code_root = runtime / f"versions/f8-{version()}-{stamp}" / "code"
    config = {
        "schema_version": "1.0",
        "platform": "darwin",
        "launcher": str(venv_python),
        "python": str(venv_python),
        "worker_entry": str(code_root / "services/review_capture/worker_entry.py"),
        "visibility_entry": str(code_root / "services/review_capture/show_panel.py"),
        "ipc_root": str(runtime / f"ipc/f8-{version()}"),
        "transcription_python": str(venv_python),
        "transcription_entry": str(code_root / "scripts/transcribe_review_batch.py"),
        "note_transcription_entry": str(code_root / "scripts/transcribe_review_note.py"),
        "transcription_model": options.model,
        "transcription_language": language,
        "language": language,
        "language_source": language_source,
        "model_status": model_status,
        "optional_dependencies": {"key_backend": optional_status},
        "bridge_dir": str(bridge),
        "registration": "client" if options.no_defaults is False else "skipped",
        "build": f"f8-{version()}",
        "installed_at": utc_now(),
        "installer": str(python),
    }
    _write_json(panel_stage / "runtime.json", config)
    return config


def install(options: Options, *, host: HostCommands | None = None,
            info: dict[str, str] | None = None) -> dict[str, Any]:
    """Perform the installation. Raises InstallError after rolling back."""
    host = host or HostCommands()
    support = host_support(info)
    if not support["supported"]:
        # Refuse before creating a folder, a venv or a defaults value: an
        # unsupported CPU/OS would otherwise fail halfway through pip.
        raise InstallError(
            f"this experimental candidate supports only {support['minimum']}: {support['reason']}. "
            "Nothing was changed."
        )
    extension, runtime, bridge = runtime_roots(options)
    python, python_note = find_python(options.python, host=host)
    if python is None:
        raise InstallError(f"Python 3.11 is required: {python_note}. Nothing was changed.")
    # Resolve before writing anything; an invalid --language must change nothing.
    language, language_source = resolve_language(options)
    previous_config = previous_runtime_config(options)
    destination = extension / platform_paths.PANEL_FOLDER
    legacy = extension / platform_paths.LEGACY_PANEL_FOLDER
    tag = version()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    version_root = runtime / "versions" / f"f8-{tag}-{stamp}"
    backup_root = runtime / "backups"
    backups: list[tuple[Path, Path]] = []  # (original, backup)
    previous_defaults: dict[str, str] = {}
    previous_record = runtime / "current_install.json"
    previous_record_text = previous_record.read_text(encoding="utf-8") if previous_record.exists() else None
    wrapper_path = runtime / "bin" / "cuthere"
    previous_wrapper_path: Path | None = None
    wrapper_written = False
    defaults_written = False
    moved_panel = False
    log: list[str] = []

    def note(message: str) -> None:
        log.append(f"{utc_now()} {message}")
        try:
            print(message)
        except UnicodeEncodeError:
            # A legacy console code page must never abort an installation.
            print(message.encode("ascii", "backslashreplace").decode("ascii"))

    try:
        version_root.mkdir(parents=True, exist_ok=False)
        backup_root.mkdir(parents=True, exist_ok=True)
        note(f"runtime: {version_root}")
        # The previous install record is preserved on every path, including
        # --no-defaults: a later --restore-previous needs it to restore the full
        # runnable state (record, panel and CLI wrapper target).
        if previous_record_text is not None:
            (version_root / "previous_install.json").write_text(previous_record_text, encoding="utf-8")
            note("preserved the previous installation record")
        if wrapper_path.is_file():
            previous_wrapper_path = version_root / "previous_cli_wrapper"
            shutil.copy2(wrapper_path, previous_wrapper_path)
            note(f"preserved the previous CLI wrapper: {previous_wrapper_path}")
        if previous_config:
            _write_json(version_root / "previous_runtime.json", previous_config)
            note(f"preserved the previous runtime configuration (language={language!r}, source={language_source})")
        copy_code(version_root)
        venv = version_root / "venv"
        venv_command = [str(python), "-m", "venv", str(venv)]
        if options.skip_pip:
            venv_command.insert(3, "--without-pip")
        completed = host.run_python(venv_command, timeout=600)
        if completed.returncode != 0:
            raise InstallError(f"could not create the virtual environment: {completed.stderr.strip()}")
        venv_python = venv_bin(venv)
        optional_status = "skipped"
        if options.skip_dependencies:
            note("dependencies: skipped (--skip-dependencies)")
        else:
            requirements = RELEASE_ROOT / "requirements-macos.txt"
            note(f"installing dependencies from {requirements.name}")
            completed = host.run_python([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
                                         "-r", str(requirements)], timeout=3600)
            (version_root / "pip.log").write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
            if completed.returncode != 0:
                raise InstallError(
                    "dependency installation failed; the previous panel is still in place "
                    f"(see {version_root / 'pip.log'})"
                )
            # PyObjC powers the optional global F8/F9 tap. A missing wheel, an
            # old SDK or a blocked index must not make the panel uninstallable:
            # the buttons still record and the backend reports "unavailable".
            if OPTIONAL_REQUIREMENTS.is_file():
                note("installing the optional PyObjC key backend")
                completed = host.run_python([str(venv_python), "-m", "pip", "install", "--disable-pip-version-check",
                                             "-r", str(OPTIONAL_REQUIREMENTS)], timeout=1800)
                (version_root / "pip-optional.log").write_text(
                    (completed.stdout or "") + (completed.stderr or ""), encoding="utf-8"
                )
                if completed.returncode == 0:
                    optional_status = "installed"
                    note("optional key backend: PyObjC installed")
                else:
                    optional_status = "unavailable"
                    note("optional key backend: PyObjC could not be installed; the panel buttons still work "
                         f"(see {version_root / 'pip-optional.log'})")
        model_status = "not-requested"
        if options.download_model:
            note(f"downloading faster-whisper '{options.model}' weights")
            completed = host.run_python([str(venv_python), "-m", "huggingface_hub.commands.huggingface_cli",
                                         "download", f"Systran/faster-whisper-{options.model}"], timeout=3600)
            if completed.returncode != 0:
                raise InstallError("model download failed; the previous panel is still in place")
            model_status = "downloaded"
        note("checking the model cache")
        verify = host.run_python([
            str(venv_python), "-c",
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from scripts.transcribe_review_batch import find_cached_model;"
            "print(find_cached_model(sys.argv[2]))",
            str(version_root / "code"), options.model,
        ], timeout=300)
        if verify.returncode != 0:
            if options.download_model:
                raise InstallError(
                    "the transcription model was downloaded but is still not cached; nothing was replaced"
                )
            # Transcription stays unavailable until the weights are prepared;
            # installing the panel and recording notes must still work.
            model_status = "missing"
            note("transcription model is not cached yet. Install the panel now and prepare the model later with "
                 "--download-model, or transcribe on another machine.")
        else:
            model_status = "cached"
            note(f"model cache: {verify.stdout.strip()}")
        stage = version_root / "panel_stage"
        shutil.copytree(panel_source(), stage)
        manifest_status = prepare_panel_manifest(stage)
        config = write_runtime_config(stage, options, stamp, python, venv_python,
                                      language, language_source, optional_status, model_status)
        (runtime / f"ipc/f8-{tag}").mkdir(parents=True, exist_ok=True)
        for name in ("commands", "responses", "locks"):
            (bridge / name).mkdir(parents=True, exist_ok=True)
        extension.mkdir(parents=True, exist_ok=True)
        retired: list[dict[str, str]] = []
        if legacy.exists():
            bundle = panel_bundle_id(legacy)
            if bundle == platform_paths.LEGACY_BUNDLE_ID:
                backup = assert_child(backup_root / f"{platform_paths.LEGACY_PANEL_FOLDER}_{stamp}", runtime, "legacy backup")
                shutil.move(str(legacy), str(backup))
                backups.append((legacy, backup))
                retired.append({"destination": str(legacy), "backup": str(backup)})
                note(f"retired legacy bundle: {legacy} -> {backup}")
            else:
                note(f"left an unrelated folder untouched: {legacy} (bundle {bundle!r})")
        panel_backup = None
        if destination.exists():
            panel_backup = assert_child(backup_root / f"{platform_paths.PANEL_FOLDER}_{stamp}", runtime, "panel backup")
            shutil.move(str(destination), str(panel_backup))
            backups.append((destination, panel_backup))
            note(f"backed up previous panel: {panel_backup}")
        shutil.move(str(stage), str(destination))
        moved_panel = True
        note(f"installed panel: {destination}")
        if not options.no_defaults:
            domain = platform_paths.ssxs_defaults_domain(options.cep_version)
            current = host.read_defaults(domain, "PlayerDebugMode")
            if current is not None:
                previous_defaults[domain] = current
            host.write_defaults(domain, "PlayerDebugMode", "1")
            defaults_written = True
            note(f"enabled unsigned CEP development extension for the current user: {domain} PlayerDebugMode=1")
        wrapper = write_cli_wrapper(runtime, venv_python, version_root, bridge)
        wrapper_written = True
        record = {
            "version": tag,
            "platform": "darwin",
            "python": str(venv_python),
            "launcher": str(venv_python),
            "cli": str(version_root / "code" / "scripts" / "cli.py"),
            "cli_wrapper": str(wrapper),
            "cli_wrapper_mode": "0755",
            "runtime": str(version_root),
            "panels": [{"destination": str(destination), "backup": str(panel_backup) if panel_backup else None}],
            "retired_panels": retired,
            "bridge_dir": str(bridge),
            "model": options.model,
            "model_status": model_status,
            "language": language,
            "language_source": language_source,
            "host_support": support,
            "optional_dependencies": {"key_backend": optional_status},
            "panel_manifest": manifest_status,
            "cep_version": options.cep_version,
            "defaults_domain": (None if options.no_defaults else platform_paths.ssxs_defaults_domain(options.cep_version)),
            "previous_defaults": previous_defaults,
            "previous_install_record": (str(version_root / "previous_install.json") if previous_record_text else None),
            "previous_cli_wrapper": (str(previous_wrapper_path) if previous_wrapper_path else None),
            "installed_at": utc_now(),
            "config": config,
        }
        _write_json(version_root / "install_receipt.json", record)
        _write_json(previous_record, record)
        (version_root / "install.log").write_text("\n".join(log) + "\n", encoding="utf-8")
        note("installed. Quit and reopen Premiere, then open Window > Extensions > Cuthere · 剪这儿.")
        return {"status": "INSTALLED", "record": record, "log": log}
    except BaseException as exc:
        note(f"failure: {type(exc).__name__}: {exc}")
        rollback_error = None
        try:
            if defaults_written:
                domain = platform_paths.ssxs_defaults_domain(options.cep_version)
                if domain in previous_defaults:
                    host.write_defaults(domain, "PlayerDebugMode", previous_defaults[domain])
                else:
                    host.delete_defaults(domain, "PlayerDebugMode")
            if moved_panel and destination.exists():
                failed = assert_child(backup_root / f"failed_{platform_paths.PANEL_FOLDER}_{stamp}", runtime, "failed panel")
                shutil.move(str(destination), str(failed))
            for original, backup in reversed(backups):
                if backup.exists() and not original.exists():
                    shutil.move(str(backup), str(original))
            if previous_record_text is not None:
                previous_record.write_text(previous_record_text, encoding="utf-8")
            elif previous_record.exists():
                previous_record.unlink()
            # The CLI wrapper is part of the runnable installation: a failed
            # upgrade must not leave a command pointing at the new runtime.
            if previous_wrapper_path is not None and previous_wrapper_path.is_file():
                wrapper_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(previous_wrapper_path, wrapper_path)
                try:
                    wrapper_path.chmod(0o755)
                except OSError:
                    pass
            elif wrapper_written and wrapper_path.exists():
                wrapper_path.unlink()
        except Exception as rollback_exc:  # pragma: no cover - reported, not hidden
            rollback_error = f"{type(rollback_exc).__name__}: {rollback_exc}"
            note(f"ROLLBACK FAILED: {rollback_error}; backups are under {backup_root}")
        try:
            (version_root / "install.log").parent.mkdir(parents=True, exist_ok=True)
            (version_root / "install.log").write_text("\n".join(log) + "\n", encoding="utf-8")
        except OSError:
            pass
        raise InstallError(f"installation failed and was rolled back: {exc}"
                           + (f" (rollback problem: {rollback_error})" if rollback_error else "")) from exc


def write_cli_wrapper(runtime: Path, venv_python: Path, version_root: Path, bridge: Path) -> Path:
    """POSIX wrapper so `cuthere ...` works without activating the venv."""
    bin_dir = runtime / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "cuthere"
    cli = version_root / "code" / "scripts" / "cli.py"

    def q(value: Path) -> str:
        text = str(value)
        if "'" in text:
            raise InstallError(f"paths containing a single quote are not supported: {text}")
        return "'" + text + "'"

    wrapper.write_text(
        "#!/bin/sh\n"
        "# Cuthere CLI wrapper - generated by install.sh; re-runs install.sh on reinstall.\n"
        f"CUTHERE_BRIDGE_DIR={q(bridge)}\n"
        "export CUTHERE_BRIDGE_DIR\n"
        f"exec {q(venv_python)} {q(cli)} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _validate_uninstall(options: Options) -> None:
    """Reject uninstall combinations that cannot leave a coherent installation."""
    if options.restore_previous and options.purge_runtime:
        raise InstallError(
            "--restore-previous and --purge-runtime cannot be combined: restoring the previous "
            "installation needs its runtime folder, installation record and CLI wrapper. Run uninstall "
            "with --restore-previous first, then run it again without it and with --purge-runtime. "
            "Nothing was changed."
        )


def _rmdir_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def purge_owned_runtime(runtime: Path) -> list[str]:
    """Delete Cuthere's own runtime artifacts, never unrelated files.

    A custom ``--runtime-root`` may point at a folder that also holds other
    tools or the user's own files. Only ``versions/f8-*``, ``ipc/f8-*`` and the
    generated ``bin/cuthere`` wrapper are removed; everything else stays.
    The ``uninstalled_*.json`` audit record is deliberately kept.
    """
    purged: list[str] = []
    versions = runtime / "versions"
    if versions.is_dir():
        for child in sorted(versions.iterdir()):
            if child.is_dir() and child.name.startswith("f8-"):
                shutil.rmtree(child)
                purged.append(str(child))
        _rmdir_if_empty(versions)
    ipc = runtime / "ipc"
    if ipc.is_dir():
        for child in sorted(ipc.iterdir()):
            if child.is_dir() and child.name.startswith("f8-"):
                shutil.rmtree(child)
                purged.append(str(child))
        _rmdir_if_empty(ipc)
    bin_dir = runtime / "bin"
    wrapper = bin_dir / "cuthere"
    if wrapper.is_file():
        wrapper.unlink()
        purged.append(str(wrapper))
    _rmdir_if_empty(bin_dir)
    return purged


def uninstall(options: Options, *, host: HostCommands | None = None) -> dict[str, Any]:
    host = host or HostCommands()
    _validate_uninstall(options)
    extension, runtime, _ = runtime_roots(options)
    record_path = runtime / "current_install.json"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_root = runtime / "backups"
    if not record_path.exists():
        return {"status": "NOTHING_TO_DO", "reason": f"no installation record at {record_path}", "moved": []}
    record = json.loads(record_path.read_text(encoding="utf-8"))
    # Validate every restore input before moving anything: a half-restored
    # installation is worse than refusing the restore.
    if options.restore_previous:
        for key, label in (("previous_install_record", "the previous installation record"),
                           ("previous_cli_wrapper", "the previous CLI wrapper")):
            recorded = record.get(key)
            if recorded:
                candidate = assert_child(Path(str(recorded)), runtime, label)
                if not candidate.is_file():
                    raise InstallError(
                        f"{label} is missing ({candidate}); nothing was changed. "
                        "Uninstall without --restore-previous to remove only this installation."
                    )
    moved: list[dict[str, str]] = []
    for panel in record.get("panels", []):
        destination = Path(str(panel.get("destination")))
        assert_child(destination, extension, "installed panel")
        if destination.exists():
            target = assert_child(backup_root / f"removed_{destination.name}_{stamp}", runtime, "removed panel")
            shutil.move(str(destination), str(target))
            moved.append({"from": str(destination), "to": str(target)})
            if options.restore_previous and panel.get("backup"):
                backup = Path(str(panel["backup"]))
                if backup.is_dir():
                    assert_child(backup, runtime, "previous panel backup")
                    shutil.move(str(backup), str(destination))
                    moved.append({"from": str(backup), "to": str(destination)})
    if options.restore_previous:
        for panel in record.get("retired_panels", []):
            backup = Path(str(panel.get("backup")))
            original = Path(str(panel.get("destination")))
            if backup.is_dir() and not original.exists():
                assert_child(original, extension, "retired panel destination")
                assert_child(backup, runtime, "retired panel backup")
                shutil.move(str(backup), str(original))
                moved.append({"from": str(backup), "to": str(original)})
    if record.get("defaults_domain"):
        domain = str(record["defaults_domain"])
        previous = record.get("previous_defaults") or {}
        if domain in previous:
            host.write_defaults(domain, "PlayerDebugMode", str(previous[domain]))
        else:
            host.delete_defaults(domain, "PlayerDebugMode")
    shutil.move(str(record_path), str(runtime / f"uninstalled_{stamp}.json"))
    previous_record = record.get("previous_install_record")
    if options.restore_previous and previous_record:
        shutil.copy2(str(previous_record), record_path)
    # The generated command is part of this installation: remove it (with an
    # audit copy in backups/) and, when restoring, put the previous wrapper back
    # so it targets the runtime that the restored record describes.
    wrapper = Path(str(record.get("cli_wrapper") or (runtime / "bin" / "cuthere")))
    assert_child(wrapper, runtime, "installed CLI wrapper")
    if wrapper.is_file():
        kept = assert_child(backup_root / f"removed_cuthere_wrapper_{stamp}", runtime, "removed CLI wrapper")
        shutil.move(str(wrapper), str(kept))
        moved.append({"from": str(wrapper), "to": str(kept)})
    if options.restore_previous:
        previous_wrapper = record.get("previous_cli_wrapper")
        if previous_wrapper:
            source = assert_child(Path(str(previous_wrapper)), runtime, "previous CLI wrapper")
            if source.is_file():
                wrapper.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(source), str(wrapper))
                try:
                    wrapper.chmod(0o755)
                except OSError:
                    pass
                moved.append({"from": str(source), "to": str(wrapper)})
    else:
        _rmdir_if_empty(wrapper.parent)
    purged = purge_owned_runtime(runtime) if options.purge_runtime else []
    return {"status": "UNINSTALLED", "moved": moved, "restored_previous": options.restore_previous,
            "purged": purged, "record_moved_to": str(runtime / f"uninstalled_{stamp}.json")}


def uninstall_plan(options: Options, *, host: HostCommands | None = None) -> dict[str, Any]:
    _validate_uninstall(options)
    extension, runtime, _ = runtime_roots(options)
    record_path = runtime / "current_install.json"
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else None
    wrapper = str((record or {}).get("cli_wrapper") or (runtime / "bin" / "cuthere"))
    return {
        "mode": "PLAN", "action": "uninstall", "record": str(record_path), "installed": record is not None,
        "version": (record or {}).get("version"),
        "moves": [{"from": panel.get("destination"), "to": "<runtime>/backups/removed_<panel>_<stamp>"}
                  for panel in ((record or {}).get("panels") or [])],
        "wrapper": {"removes": wrapper,
                    "restores_previous": ((record or {}).get("previous_cli_wrapper")
                                          if options.restore_previous else None)},
        "restores_previous": bool(options.restore_previous),
        "restores_defaults": (record or {}).get("previous_defaults") if record else None,
        "purge_runtime": bool(options.purge_runtime),
        "purge_scope": "only versions/f8-*, ipc/f8-* and bin/cuthere; unrelated files in a custom "
                       "runtime root are kept",
        "keeps": ["project work/ and output/ notes, recordings and receipts",
                  "source media and Premiere projects"],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> Options:
    parser = argparse.ArgumentParser(description="Cuthere macOS installer (dry-run unless --apply)")
    parser.add_argument("--apply", action="store_true", help="perform the installation (default: show the plan)")
    parser.add_argument("--check-only", action="store_true", help="report the environment and exit")
    parser.add_argument("--download-model", action="store_true", help="download the transcription model")
    parser.add_argument("--model", choices=DEFAULT_MODELS, default="medium")
    parser.add_argument("--language", default=None,
                        help="transcription language for the panel (auto|zh|en|...); omit to keep an "
                             "existing recorded value, or auto on a new install")
    parser.add_argument("--python", type=Path, default=None, help="Python 3.11 interpreter")
    parser.add_argument("--cep-version", default="12", help="CSXS version for PlayerDebugMode (default 12)")
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--extension-root", type=Path, default=None)
    parser.add_argument("--no-defaults", action="store_true", help="do not change PlayerDebugMode")
    parser.add_argument("--skip-dependencies", action="store_true", help="tests/offline: do not run pip")
    parser.add_argument("--skip-pip", action="store_true",
                        help="create the venv without pip/ensurepip (restricted sandboxes; install dependencies later)")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--restore-previous", action="store_true")
    parser.add_argument("--purge-runtime", action="store_true")
    parser.add_argument("--json-out", type=Path, default=None)
    return Options(**{key: value for key, value in vars(parser.parse_args(argv)).items()})


def main(argv: list[str] | None = None, *, host: HostCommands | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (ValueError, OSError):
                pass
    options = parse_args(argv)
    host = host or HostCommands()
    info = host_platform_info()
    try:
        if options.check_only:
            result: dict[str, Any] = environment_report(options, host=host, info=info)
            status_ok = all(check["status"] != "FAIL" for check in result["checks"])
        elif options.uninstall:
            result = uninstall(options, host=host) if options.apply else uninstall_plan(options, host=host)
            status_ok = True
        elif options.apply:
            result = install(options, host=host, info=info)
            status_ok = True
        else:
            result = build_plan(options, host=host, info=info)
            result["environment"] = environment_report(options, host=host, info=info)
            status_ok = all(check["status"] != "FAIL" for check in result["environment"]["checks"])
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if options.json_out:
            _write_json(options.json_out, {"status": "FAILED", "error": str(exc)})
        return 2
    except OSError as exc:
        # Unexpected filesystem/permission failure: report it, never a traceback-only exit.
        message = f"{type(exc).__name__}: {exc}"
        print(f"error: {message}", file=sys.stderr)
        if options.json_out:
            _write_json(options.json_out, {"status": "FAILED", "error": message})
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False))
    if options.json_out:
        _write_json(options.json_out, result)
    return 0 if status_ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
