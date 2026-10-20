"""`cuthere bridge-status`: file-bridge health check used as the macOS smoke test.

The panel side is simulated by a thread that watches the commands folder and
writes a response, which is exactly what the CEP panel's bridge.js does.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from engine.premiere_bridge import PremiereBridgeClient, PremiereCommand


def fake_panel(bridge: Path, stop: threading.Event, *, answer: bool = True) -> threading.Thread:
    commands = bridge / "commands"
    responses = bridge / "responses"

    def run() -> None:
        while not stop.is_set():
            for path in sorted(commands.glob("*.json")):
                request = json.loads(path.read_text(encoding="utf-8"))
                if answer:
                    (responses / path.name).write_text(json.dumps({
                        "request_id": request["request_id"], "success": request["command"] == "ping",
                        "data": {"version": "1.1.0", "premiere_version": "25.6.4", "os": "MacIntel"},
                        "timestamp": time.time(),
                    }), encoding="utf-8")
                path.unlink(missing_ok=True)
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class BridgeStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = Path(self.temp.name) / "bridge with space"

    def test_client_reaches_a_panel_that_answers(self):
        stop = threading.Event()
        fake_panel(self.bridge, stop)
        self.addCleanup(stop.set)
        client = PremiereBridgeClient(str(self.bridge))
        client.ensure_dirs()
        self.assertTrue(client.is_ready)
        reply = client.send_command(PremiereCommand.PING, {}, timeout=5)
        self.assertTrue(reply.success, reply.error)
        self.assertEqual(reply.data["premiere_version"], "25.6.4")

    def test_timeout_is_reported_and_cleans_up_the_command_file(self):
        client = PremiereBridgeClient(str(self.bridge))
        reply = client.send_command(PremiereCommand.PING, {}, timeout=0.3)
        self.assertFalse(reply.success)
        self.assertIn("timed out", reply.error)
        self.assertFalse(list((self.bridge / "commands").glob("*.json")))

    def test_cli_reports_json_and_exit_codes_without_python_capture_helpers(self):
        # Run the CLI in a separate interpreter with file-based capture, which
        # works on sandboxes that deny pipes.
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "out.txt"
            with out.open("w", encoding="utf-8") as handle:
                completed = subprocess.run(
                    [sys.executable, str(REPOSITORY_ROOT / "scripts" / "cli.py"), "bridge-status",
                     "--bridge-dir", str(self.bridge), "--timeout", "0.4"],
                    cwd=str(REPOSITORY_ROOT), stdout=handle, stderr=subprocess.STDOUT, timeout=120,
                )
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(completed.returncode, 2)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["premiere"])
        self.assertIn("host state is unknown", report["hint"])


if __name__ == "__main__":
    unittest.main()
