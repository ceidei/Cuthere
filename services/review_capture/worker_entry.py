"""Stable launcher copied into the isolated local runtime by the installer."""

from __future__ import annotations

import sys
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CODE_ROOT))

from services.review_capture.worker import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
