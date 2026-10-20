"""The helper files CEP launches must work as plain scripts.

`runtime.json` points at `worker_entry.py` and `show_panel.py`; CEP starts them
with an absolute interpreter path, not with `-m`. A package-relative import in
that position fails with "no known parent package", which no in-process test
notices. These checks execute both entry points in a fresh interpreter, using
file-based capture so they also run in sandboxes that deny pipes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run_script(relative: str, *args: str) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as folder:
        out = Path(folder) / "out.txt"
        with out.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(
                [sys.executable, str(REPOSITORY_ROOT / relative), *args],
                cwd=str(REPOSITORY_ROOT), stdout=handle, stderr=subprocess.STDOUT, timeout=180,
            )
        return completed.returncode, out.read_text(encoding="utf-8", errors="replace")


class HelperEntryPointTests(unittest.TestCase):
    def test_show_panel_runs_as_a_script_and_writes_its_diagnostic(self):
        with tempfile.TemporaryDirectory() as folder:
            ipc = Path(folder) / "ipc with space"
            exit_code, output = run_script("services/review_capture/show_panel.py", "--ipc-root", str(ipc))
            self.assertEqual(exit_code, 0, msg=output)
            report = json.loads((ipc / "panel_visibility.json").read_text(encoding="utf-8"))
        self.assertIn("panels", report)
        self.assertIn("supported", report)

    def test_worker_entry_parses_arguments_without_importing_defaults(self):
        exit_code, output = run_script("services/review_capture/worker_entry.py", "--help")
        self.assertEqual(exit_code, 0, msg=output)
        self.assertIn("usage", output.lower())


if __name__ == "__main__":
    unittest.main()
