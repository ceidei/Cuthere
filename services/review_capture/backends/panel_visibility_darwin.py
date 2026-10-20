"""macOS panel visibility: nothing to repair, and say so honestly.

The Windows problem is a hidden ``CefBrowserWindow`` child that the 1.0 panel
worked around with Win32 calls. macOS has no equivalent: if the panel is blank
on macOS the cause is a missing/loading extension, ``PlayerDebugMode`` or a CEP
engine problem, so this backend records a diagnostic entry instead of touching
any other application's windows. Granting no permission is required and none is
attempted.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


def reveal_panel() -> dict[str, Any]:
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "platform": "darwin",
        "supported": False,
        "panels": [],
        "reason": (
            "The Windows-only hidden CEP wrapper repair does not apply on macOS. If the panel is "
            "blank, check that the extension folder and PlayerDebugMode are present and restart "
            "Premiere; see docs/MACOS.md."
        ),
        "pid": os.getpid(),
    }
