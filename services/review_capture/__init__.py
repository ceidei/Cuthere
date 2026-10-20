"""Local capture and durable review-note primitives for the Premiere panel.

``CUTHERE_SIMULATE_PLATFORM`` exists for one purpose: the CI job that checks the
macOS code paths on a Windows runner. When it is ``darwin`` (or ``win32``), only
``sys.platform`` is overridden inside this interpreter, which is enough to make
``services.platform_paths.current_platform()`` and the backend factory take the
other platform's branch. The tests that use it also trap ``ctypes.windll`` so a
Windows code path cannot run unnoticed, and they say plainly that this is not a
substitute for running on a Mac.
"""

from __future__ import annotations

import os
import sys

__version__ = "1.1.0-alpha.4"

SUPPORTED_SIMULATIONS = ("darwin", "win32")


def simulated_platform() -> str:
    value = os.environ.get("CUTHERE_SIMULATE_PLATFORM", "").strip()
    return value if value in SUPPORTED_SIMULATIONS else ""


_simulation = simulated_platform()
if _simulation and sys.platform != _simulation:
    sys.platform = _simulation
