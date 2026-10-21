"""Choose the global hold-to-record backend for the running platform.

The backends expose the same methods, so ``PushToTalk`` has one code path:

* ``poll(key)`` -> ``(down, pressed_since_poll, hotkey_message)``
* ``frontmost()`` -> bool (is Premiere the active application?)
* ``close()``

A binding is the canonical string produced by
``services.review_capture.shortcut`` (``F8``, ``Control+Shift+K``, ``RightAlt``),
not a free-form key name: every value is parsed and validated before it reaches
a backend, and an unsupported value is reported instead of being rewritten to F8.

Windows: ``ctypes`` user32 hotkey + asynchronous key state.
macOS: optional PyObjC Quartz listen-only event tap; unavailable when PyObjC is
not installed, and reported as such instead of silently doing nothing.

Backends are imported lazily and only for the platform that will use them, so
neither platform's native imports can break the other platform.
"""

from __future__ import annotations

import sys
from typing import Any

from .. import shortcut

#: Base keys a hold-to-record binding may use. ``ptt`` no longer rewrites an
#: unknown value to F8; this tuple is what "supported" means for diagnostics.
SUPPORTED_KEYS = shortcut.BASE_KEYS
SUPPORTED_MODIFIERS = shortcut.MODIFIER_ORDER


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
        return {"platform": platform, "backend": "windows-hotkey", "available": True, "reason": "",
                "shortcuts": shortcut.describe_support(platform)}
    if platform == "darwin":
        from .keymonitor_darwin import probe

        report = probe()
        report["shortcuts"] = shortcut.describe_support(platform)
        return report
    return {"platform": platform, "backend": None, "available": False, "reason": "unsupported platform",
            "shortcuts": shortcut.describe_support(platform)}
