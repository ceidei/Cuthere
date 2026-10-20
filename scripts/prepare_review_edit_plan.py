"""Create a human-editable decision template or a read-only Premiere edit dry-run."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from engine.review_edit_plan import build_dry_run, make_decision_template, write_dry_run
from engine.review_notes import ReviewBatchError, load_current_batch
from services.review_capture.storage import atomic_write_json


def read_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("决策文件必须是 JSON 对象")
    return value


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}_r{index}{suffix}"
        index += 1
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description="读取已校验的 Premiere 意见批次，生成决策模板或只读剪辑 dry-run")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, help="经人工填写的决策 JSON；省略时只生成空白决策模板")
    parser.add_argument("--timeline-check", type=Path, help="Premiere 面板生成的当前时间线核对 JSON；剪辑类决策必须提供")
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    try:
        batch = load_current_batch(project_root)
    except (ReviewBatchError, OSError, ValueError) as exc:
        raise SystemExit(f"当前意见批次不能使用：{exc}")

    session_root = project_root / "work" / "premiere-review" / str(batch["session_id"])
    plans_root = session_root / "plans"
    plans_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if args.decisions is None:
        template = make_decision_template(batch)
        template_path = unique_path(plans_root, f"decision-template-{stamp}", ".json")
        atomic_write_json(template_path, template)
        print(json.dumps({"status": "TEMPLATE_CREATED", "path": str(template_path), "note_count": len(batch["notes"])}, ensure_ascii=False))
        return 0

    decisions_path = args.decisions.resolve()
    if not decisions_path.is_file():
        raise SystemExit(f"决策文件不存在：{decisions_path}")
    decisions = read_object(decisions_path)
    timeline_check = None
    timeline_check_path = None
    if args.timeline_check is not None:
        timeline_check_file = args.timeline_check.resolve()
        if not timeline_check_file.is_file():
            raise SystemExit(f"时间线核对报告不存在：{timeline_check_file}")
        timeline_check = read_object(timeline_check_file)
        timeline_check_path = str(timeline_check_file)
    plan = build_dry_run(batch, decisions, timeline_check, timeline_check_path)
    json_path, csv_path = write_dry_run(plan, session_root)
    print(json.dumps({
        "status": plan["status"],
        "execution_eligible": False,
        "operations": plan["decision_count"],
        "blocked": plan["blocked_count"],
        "json": str(json_path),
        "csv": str(csv_path),
    }, ensure_ascii=False))
    return 1 if plan["status"] == "BLOCKED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
