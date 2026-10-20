"""Platform backends: global key capture and panel visibility.

Nothing in this package imports a platform-specific module at import time.
``keymonitor.create_backend`` and ``panel_visibility.reveal_panel`` import the
matching implementation lazily, so the package (and the worker that imports it)
starts on any platform.
"""

from . import keymonitor, panel_visibility

__all__ = ["keymonitor", "panel_visibility"]
