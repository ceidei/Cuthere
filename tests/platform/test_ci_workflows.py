"""CI structure: independent checks, and no way to mask a failure.

Both candidate workflows used to stop at the first failing step, so a Python
failure hid the Node, native-import, shell and packaging checks until the next
round. These tests pin the property that made that happen unnecessary: every
check after the environment setup runs on ``!cancelled()`` (a check whose input
failed to install is skipped *by name* through ``outcome``), no step uses
``continue-on-error``, and each workflow still runs on its maintained runner.

The YAML is read line by line because the CI runner installs only
``requirements-dev.txt`` (pytest); requiring PyYAML here would make this test
itself a CI dependency.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"

MACOS_CHECKS = (
    "Python tests (real Darwin imports included)",
    "Node panel and bridge tests",
    "Install macOS runtime dependencies (no transcription model)",
    "Real native and runtime imports (no tap, microphone stream or permission prompt)",
    "Installer check-only on the real Darwin interpreter",
    "Shell syntax",
    "Installer plan and sandboxed install/upgrade/restore/purge",
    "Release allowlist and manifest check",
)
WINDOWS_CHECKS = (
    "Python tests",
    "Node panel and bridge tests",
    "PowerShell syntax and filesystem cases",
    "Release allowlist and manifest check",
)
SETUP_STEP = "Install test dependencies"


def workflow_text(name: str) -> str:
    path = WORKFLOWS / name
    if not path.is_file():
        raise AssertionError(f"missing workflow: {name}")
    return path.read_text(encoding="utf-8")


def steps(name: str) -> list[dict[str, str]]:
    """Split the ``steps:`` block into one small record per step."""
    parsed: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in workflow_text(name).splitlines():
        if re.match(r"^      - ", line):
            current = {"first": line[8:].strip(), "body": ""}
            parsed.append(current)
            continue
        if current is None:
            continue
        if not line.strip():
            continue
        if line.startswith("        "):
            current["body"] += line.strip() + "\n"
            continue
        # Any less-indented content ends the step list.
        current = None
    for step in parsed:
        match = re.search(r"(?m)^name:\s*(.+)$", step["first"] + "\n" + step["body"])
        step["name"] = match.group(1).strip().strip('"') if match else ""
        match = re.search(r"(?m)^if:\s*(.+)$", step["body"])
        step["if"] = match.group(1).strip() if match else ""
        match = re.search(r"(?m)^id:\s*(\S+)$", step["body"])
        step["id"] = match.group(1).strip() if match else ""
    return parsed


def step_named(name: str, wanted: str) -> dict[str, str]:
    for step in steps(name):
        if step["name"] == wanted:
            return step
    raise AssertionError(f"{name}: no step named {wanted!r}")


class WorkflowIndependenceTests(unittest.TestCase):
    def test_both_workflows_keep_their_runner_and_scope(self):
        macos = workflow_text("macos.yml")
        self.assertIn("runs-on: macos-15", macos)
        self.assertIn('assert platform.machine() == "arm64"', macos)
        self.assertIn(">= (14, 0)", macos)
        self.assertIn("import AppKit", macos)
        self.assertIn("import Quartz", macos)
        self.assertIn("import sounddevice", macos)
        self.assertIn("runs-on: windows-latest", workflow_text("windows.yml"))

    def test_every_required_check_still_exists(self):
        for name, required in (("macos.yml", MACOS_CHECKS), ("windows.yml", WINDOWS_CHECKS)):
            present = [step["name"] for step in steps(name)]
            for wanted in required:
                self.assertIn(wanted, present, f"{name}: {wanted}")

    def test_every_check_after_setup_runs_even_when_another_check_failed(self):
        for name in ("macos.yml", "windows.yml"):
            parsed = steps(name)
            names = [step["name"] for step in parsed]
            self.assertIn(SETUP_STEP, names, name)
            first_check = names.index(SETUP_STEP) + 1
            self.assertLess(first_check, len(parsed), name)
            for step in parsed[first_check:]:
                self.assertIn("!cancelled()", step["if"],
                              f"{name}: {step['name']!r} must run after an unrelated failure")

    def test_a_check_whose_setup_failed_is_skipped_by_name(self):
        for name, dependent, dependency in (
            ("macos.yml", MACOS_CHECKS[0], "test_deps"),
            ("macos.yml", MACOS_CHECKS[3], "runtime_deps"),
            ("windows.yml", WINDOWS_CHECKS[0], "test_deps"),
        ):
            step = step_named(name, dependent)
            self.assertIn("!cancelled()", step["if"], dependent)
            self.assertIn(f"steps.{dependency}.outcome == 'success'", step["if"], dependent)
            dependency_step = next((item for item in steps(name) if item["id"] == dependency), None)
            self.assertIsNotNone(dependency_step, f"{name}: {dependency} step with an id")

    def test_no_check_can_mask_a_failure(self):
        for name in ("macos.yml", "windows.yml"):
            # Comments may explain the rule; only real directives count.
            directives = "\n".join(
                line for line in workflow_text(name).splitlines()
                if not line.lstrip().startswith("#")
            )
            self.assertIsNone(re.search(r"(?m)^\s*continue-on-error\s*:", directives), name)
            self.assertIsNone(re.search(r"\|\|\s*true\b", directives), name)
            self.assertNotIn("if: always()", directives, name)

    def test_the_python_suite_is_a_plain_failing_step(self):
        for name in ("macos.yml", "windows.yml"):
            step = step_named(name, MACOS_CHECKS[0] if name == "macos.yml" else WINDOWS_CHECKS[0])
            self.assertIn("pytest tests", step["body"], name)
            self.assertNotIn("continue-on-error", step["body"], name)


if __name__ == "__main__":
    unittest.main()
