"""Reveal Premiere's hidden CEP browser wrapper where the platform needs it.

On Premiere Pro 25.6.4 for Windows the native panel can be visible while the
GUID-named child window that owns ``CefBrowserWindow`` remains
``WS_VISIBLE=false``: the page and helper run, but the user sees a blank panel.
The Windows helper changes only that exact child window.

macOS does not have that windowing behaviour; the macOS implementation records
a clear explanation instead of pretending it did something. Keeping the choice
in one lazy dispatch means ``services.review_capture.show_panel`` imports on
every platform.
"""

from __future__ import annotations

import sys
from typing import Any


def platform_name(platform: str | None = None) -> str:
    value = sys.platform if platform is None else platform
    if value.startswith("win32") or value == "nt":
        return "win32"
    if value == "darwin":
        return "darwin"
    return value


def reveal_panel(platform: str | None = None) -> dict[str, Any]:
    name = platform_name(platform)
    if name == "win32":
        from .panel_visibility_windows import reveal_panel as reveal

        return reveal()
    if name == "darwin":
        from .panel_visibility_darwin import reveal_panel as reveal

        return reveal()
    return {"platform": name, "supported": False, "panels": [],
            "reason": "panel visibility repair is only needed on Windows"}
