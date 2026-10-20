"""Import isolation between platforms.

Two kinds of evidence, because a mocked ``sys.platform`` alone proves little:

1. **Simulated Darwin** (``CUTHERE_SIMULATE_PLATFORM=darwin`` read by
   ``services.review_capture`` package init) in a *separate interpreter*: the
   real captured-source modules must import, and the code that would select a
   backend must choose the macOS one. Every ``windll``/``wintypes`` call is
   trapped, so a Windows code path executing under Darwin fails loudly.
2. **Real platform**: with no simulation, the modules import on whatever
   interpreter runs the tests (Windows on the development host, macOS in CI).

Limitations are stated in docs/MACOS.md and the delivery report: neither kind
proves that Premiere, a microphone or a Function key works on a Mac.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

MODULES = [
    "services.platform_paths",
    "services.review_capture",
    "services.review_capture.audio",
    "services.review_capture.backends",
    "services.review_capture.backends.keymonitor",
    "services.review_capture.backends.panel_visibility",
    "services.review_capture.ptt",
    "services.review_capture.show_panel",
    "services.review_capture.storage",
    "services.review_capture.timebase",
    "services.review_capture.worker",
    "services.review_transcription",
    "engine.premiere_bridge",
    "engine.premiere_roundtrip",
    "engine.review_edit_plan",
    "scripts.transcribe_review_batch",
    "scripts.transcribe_review_note",
    "scripts.review_doctor",
    "scripts.cli",
    "scripts.install_macos",
]


def run_python_to_file(code: str, env_extra: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a probe in a fresh interpreter, capturing output through files.

    Files rather than pipes: some CI sandboxes (and the development host used
    for this port) forbid creating pipes for child processes, and the exit code
    and output are all this test needs.
    """
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "out.txt"
        with out.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(
                [sys.executable, "-c", textwrap.dedent(code)],
                cwd=str(REPOSITORY_ROOT), stdout=handle, stderr=subprocess.STDOUT,
                env=env, timeout=180,
            )
        return completed.returncode, out.read_text(encoding="utf-8", errors="replace")


SIMULATED_DARWIN_PROBE = r"""
import importlib, json, sys, types

errors = []

# 1. Trap Windows-only native access so a Windows code path cannot pass silently.
class _NoWindll:
    def __getattr__(self, name):
        raise AssertionError("ctypes.windll was touched under simulated Darwin: " + name)

import ctypes
ctypes.windll = _NoWindll()

# 2. Pretend PyObjC is absent (the default on a machine without it) and the
#    platform is macOS.
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split(".")[0] in {"Quartz", "AppKit", "objc", "Foundation"}:
        raise ImportError("PyObjC is not installed (simulated)")
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded

modules = json.loads(sys.argv[1])
imported = []
for name in modules:
    try:
        importlib.import_module(name)
        imported.append(name)
    except Exception as exc:
        errors.append({"module": name, "error": type(exc).__name__ + ": " + str(exc)})
builtins.__import__ = real_import

report = {"imported": imported, "errors": errors}
try:
    from services.review_capture.ptt import backend_capability
    report["capability"] = backend_capability()
except Exception as exc:
    report["capability_error"] = type(exc).__name__ + ": " + str(exc)
try:
    from services.review_capture.backends import panel_visibility
    report["visibility_platform"] = panel_visibility.platform_name()
    result = panel_visibility.reveal_panel()
    report["visibility"] = {"supported": result.get("supported"), "panels": result.get("panels")}
except Exception as exc:
    report["visibility_error"] = type(exc).__name__ + ": " + str(exc)
try:
    # Importing the package applies CUTHERE_SIMULATE_PLATFORM before any path
    # helper evaluates the default platform.
    import services.review_capture  # noqa: F401
    from services import platform_paths
    report["sys_platform"] = sys.platform
    report["current_platform"] = platform_paths.current_platform()
    report["flags"] = {"CUTHERE_SIMULATE_PLATFORM": __import__("os").environ.get("CUTHERE_SIMULATE_PLATFORM"),
                       "HOME": __import__("os").environ.get("HOME")}
    report["runtime_root"] = platform_paths.runtime_root_text()
    report["extension_root"] = platform_paths.extension_root_text()
    report["bridge_dir"] = platform_paths.bridge_dir_text()
except Exception as exc:
    report["paths_error"] = type(exc).__name__ + ": " + str(exc)
print("<<<REPORT>>>" + json.dumps(report))
"""


class SimulatedDarwinImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        home = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp") / "cuthere-darwin-home"
        code = "MODULES = " + json.dumps(MODULES) + "\n" + SIMULATED_DARWIN_PROBE.replace(
            'modules = json.loads(sys.argv[1])', "modules = MODULES")
        cls.exit_code, cls.stdout = run_python_to_file(
            code, env_extra={"CUTHERE_SIMULATE_PLATFORM": "darwin", "HOME": str(home), "USERPROFILE": str(home)},
        )
        marker = "<<<REPORT>>>"
        line = next((row for row in cls.stdout.splitlines() if row.startswith(marker)), None)
        cls.report = json.loads(line[len(marker):]) if line else None

    def test_probe_ran(self):
        self.assertEqual(self.exit_code, 0, msg=self.stdout)
        self.assertIsNotNone(self.report, msg=self.stdout)

    def test_every_module_imports_under_simulated_darwin(self):
        self.assertEqual(self.report["errors"], [])

    def test_darwin_key_backend_is_selected_and_degrades_honestly(self):
        capability = self.report["capability"]
        self.assertEqual(capability["platform"], "darwin")
        self.assertEqual(capability["backend"], "quartz-event-tap")
        self.assertFalse(capability["available"])
        self.assertIn("PyObjC", capability["reason"])

    def test_panel_visibility_does_not_claim_windows_repair_on_darwin(self):
        self.assertEqual(self.report["visibility_platform"], "darwin")
        self.assertEqual(self.report["visibility"], {"supported": False, "panels": []})

    def test_paths_use_the_macos_layout(self):
        # Only enforceable where the host itself is macOS: sys.platform is the
        # simulation, but os.name and pathlib stay native, so on Windows this
        # interpreter (correctly) still resolves Windows folders.
        if sys.platform != "darwin":
            self.assertIn(self.report["current_platform"], {"win32", "darwin"})
            return
        self.assertIn("Library/Application Support/Cuthere", self.report["runtime_root"])
        self.assertIn("Library/Application Support/Adobe/CEP/extensions", self.report["extension_root"])
        self.assertTrue(self.report["bridge_dir"].endswith("Cuthere/ave-bridge"))


class RealPlatformImportTests(unittest.TestCase):
    def test_modules_import_on_this_interpreter(self):
        code = "import importlib, json, sys\n" + "\n".join(
            f"importlib.import_module({name!r})" for name in MODULES
        ) + "\nprint('OK')"
        exit_code, output = run_python_to_file(code)
        self.assertEqual(exit_code, 0, msg=output)
        self.assertIn("OK", output)

    def test_shared_modules_do_not_import_windows_only_modules_at_module_level(self):
        """Platform backends may import Windows modules; shared code may not."""
        shared = ("services/review_capture/ptt.py", "services/review_capture/worker.py",
                  "services/review_capture/show_panel.py", "services/review_capture/audio.py",
                  "services/review_capture/backends/__init__.py",
                  "services/review_capture/backends/keymonitor.py",
                  "services/review_capture/backends/panel_visibility.py",
                  "services/platform_paths.py", "engine/premiere_bridge.py")
        forbidden = ("from ctypes import", "import ctypes", "import winreg", "import msvcrt", "import win32api")
        for name in shared:
            source = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
            for number, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(forbidden) and not line.startswith((" ", "\t")):
                    self.fail(f"{name}:{number} imports a Windows-only module at module level: {stripped}")

    def test_simulation_flag_is_the_only_way_to_pose_as_darwin(self):
        from services.review_capture import simulated_platform

        os.environ.pop("CUTHERE_SIMULATE_PLATFORM", None)
        self.assertEqual(simulated_platform(), "")


if __name__ == "__main__":
    unittest.main()
