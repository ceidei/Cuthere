"""Reveal Premiere's hidden CEP browser wrapper without changing the project.

Launched once by the panel. The platform work lives in
``services.review_capture.backends.panel_visibility`` so this module imports on
every platform; on macOS it records that the Windows-only repair does not apply.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PANEL_TITLE = "Cuthere · 剪这儿"


def _reveal() -> dict[str, object]:
    # The panel launches this file directly (see runtime.json visibility_entry),
    # so a relative import would fail with "no known parent package". Import the
    # shared dispatcher absolutely in that case and keep the package-relative
    # import for `python -m services.review_capture.show_panel`.
    if __package__ in (None, ""):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from services.review_capture.backends.panel_visibility import reveal_panel
    else:
        from .backends.panel_visibility import reveal_panel

    return reveal_panel()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ipc-root", type=Path, required=True)
    args = parser.parse_args()
    result: dict[str, object] = {"at": datetime.now(timezone.utc).isoformat(), "panels": []}
    for _attempt in range(40):
        result = _reveal()
        if result.get("panels") or result.get("supported") is False:
            break
        time.sleep(0.1)
    args.ipc_root.mkdir(parents=True, exist_ok=True)
    (args.ipc_root / "panel_visibility.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
