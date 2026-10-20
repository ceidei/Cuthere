"""Choose the global hold-to-record backend for the running platform.

The backends expose the same three methods, so ``PushToTalk`` has one code path:

* ``poll(key)`` -> ``(down, pressed_since_poll, hotkey_message)``
* ``frontmost()`` -> bool (is Premiere the active application?)
* ``close()``

Windows: ``ctypes`` user32 hotkey + asynchronous key state (unchanged behaviour).
macOS: optional PyObjC Quartz listen-only event tap; unavailable when PyObjC is
not installed, and reported as such instead of silently doing nothing.

Backends are imported lazily and only for the platform that will use them, so
neither platform's native imports can break the other platform.
"""

from __future__ import annotations

import sys
from typing import Any

SUPPORTED_KEYS = ("F8", "F9", "F10", "F11")


class BackendUnavailable(RuntimeError):
    """The platform backend cannot run; ``reason`` is user-facing and lands in status.json."""


def normalize_platform(platform: str | None = None) -> str:
    value = sys.platform if platform is None else platform
    if value.startswith("win32") or value == "nt":
        return "win32"
    if value == "darwin":
        return "darwin"
    return value


def create_backend(platform: str | None = None, *, key: str = "F8", register_hotkey: bool = True) -> Any:
    platform = normalize_platform(platform)
    if platform == "win32":
        from .keymonitor_windows import WindowsKeyMonitor

        return WindowsKeyMonitor(key, register_hotkey=register_hotkey)
    if platform == "darwin":
        from .keymonitor_darwin import DarwinKeyMonitor

        return DarwinKeyMonitor(key)
    raise BackendUnavailable(f"no global key backend for platform {platform!r}")


def capability(platform: str | None = None) -> dict[str, Any]:
    """Describe what hold-to-record can do here, without opening any tap."""
    platform = normalize_platform(platform)
    if platform == "win32":
        return {"platform": platform, "backend": "windows-hotkey", "available": True, "reason": ""}
    if platform == "darwin":
        from .keymonitor_darwin import probe

        return probe()
    return {"platform": platform, "backend": None, "available": False, "reason": "unsupported platform"}
