r"""Filesystem identity for the Premiere project a review note belongs to.

Premiere, the CEP panel and the worker can name the same project file with
different spellings. A macOS temporary directory is reported as
``/var/folders/...`` while its real target is ``/private/var/folders/...``; a
Windows runner may report an 8.3 short name such as ``RUNNER~1`` where the
canonical path is ``runneradmin``; a path may also contain ``.``/``..``,
duplicate separators or a ``\\?\`` prefix. Every project-path guard therefore
compares the *identity* of two paths instead of their raw text.

The comparison never uses a blanket ``casefold``: paths are resolved to their
canonical target and then compared under the platform's own case rules
(``os.path.normcase`` lowercases only on Windows). Two paths that exist are
compared with ``os.path.samefile``, which also catches case-insensitive
filesystems without conflating two genuinely different projects on a
case-sensitive one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_WINDOWS_EXTENDED_PREFIX = "\\\\?\\"
_WINDOWS_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


def canonical_project_path(value: Any) -> str:
    """Return the comparable identity form of a project path, or "" if unset.

    Resolution keeps working for a path whose file is gone (the guards also run
    after a project was moved, and the note must remain readable), so
    ``strict=False`` is deliberate.
    """
    text = str(value or "")
    if not text:
        return ""
    if os.name == "nt":
        # ``\\?\C:\...`` and ``C:\...`` denote the same file.
        if text.startswith(_WINDOWS_EXTENDED_UNC_PREFIX):
            text = "\\\\" + text[len(_WINDOWS_EXTENDED_UNC_PREFIX):]
        elif text.startswith(_WINDOWS_EXTENDED_PREFIX):
            text = text[len(_WINDOWS_EXTENDED_PREFIX):]
    try:
        resolved = Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # An unrepresentable host path stays comparable as the host wrote it.
        return os.path.normcase(text)
    return os.path.normcase(str(resolved))


def same_project_path(first: Any, second: Any) -> bool:
    """True only when both values name the same project file.

    A missing value is never the same as anything: an unset capture path must
    still be rejected instead of silently matching another project.
    """
    left = canonical_project_path(first)
    right = canonical_project_path(second)
    if not left or not right:
        return False
    if left == right:
        return True
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError):
        return False
