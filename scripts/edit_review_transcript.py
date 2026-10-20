"""Apply a user-supplied transcript correction and publish a new review bundle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.read_review_batch import read_object, safe_child, sha256_file, verify_current_batch
from services.review_capture.storage import atomic_write_json, package_session, utc_now
from services.review_transcription import correct_transcript


def same_generation(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("session_id", "bundle_path", "manifest_path", "manifest_sha256", "project_path", "updated_at")
    return all(left.get(key) == right.get(key) for key in keys)


def main() -> int:
    parser = argparse.ArgumentParser(description="修订审片意见转写并保留原始 ASR 与历史修订")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--note-id", required=True)
    parser.add_argument("--expected-transcript-revision", default="none", help="当前版本号；首次人工听写可填 none")
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--editor", required=True)
    args = parser.parse_args()

    if args.expected_transcript_revision.lower() == "none":
        expected_revision = None
    else:
        try:
            expected_revision = int(args.expected_transcript_revision)
        except ValueError:
            raise SystemExit("--expected-transcript-revision 必须是非负整数或 none")
        if expected_revision < 0:
            raise SystemExit("--expected-transcript-revision 不能小于 0")

    project_root = args.project_root.resolve()
    verification = verify_current_batch(project_root)
    if verification.get("status") == "FAIL":
        raise SystemExit("当前意见批次未通过完整性核对；没有改动任何记录。")
    handoff_root = project_root / "work" / "premiere-review"
    pointer_path = handoff_root / "CURRENT_REVIEW_BATCH.json"
    pointer = read_object(pointer_path)
    session_id = str(pointer.get("session_id", ""))
    if not session_id or session_id != verification.get("session_id"):
        raise SystemExit("读取期间当前批次变化；请重新运行。")
    session_root = (handoff_root / session_id).resolve()
    note_id = args.note_id
    note_path = safe_child(session_root / "notes", f"{note_id}/note.json", "工作意见路径")
    if not note_path.is_file():
        raise SystemExit(f"工作记录不存在：{note_id}")
    corrected_text = args.text_file.resolve().read_text(encoding="utf-8-sig")
    correction = correct_transcript(
        note_path,
        corrected_text,
        expected_transcript_revision=expected_revision,
        editor=args.editor,
    )

    manifest_path = Path(str(pointer.get("manifest_path", ""))).resolve()
    if not manifest_path.is_file() or sha256_file(manifest_path).lower() != str(pointer.get("manifest_sha256", "")).lower():
        raise SystemExit("当前交接包在修订时发生变化；文字修订已保存在 work，请重新整理批次。")
    destination = package_session(session_root, project_root / "output")
    new_manifest = destination / "manifest.json"
    next_pointer = {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "project_path": pointer.get("project_path"),
        "session_id": session_id,
        "bundle_path": str(destination.resolve()),
        "manifest_path": str(new_manifest.resolve()),
        "manifest_sha256": sha256_file(new_manifest),
        "supersedes_session_id": session_id,
        "supersedes_bundle_path": pointer.get("bundle_path"),
    }
    current = read_object(pointer_path)
    if not same_generation(current, pointer):
        raise SystemExit(f"交接入口已变化；新版本包已保存在 {destination}，但没有覆盖当前入口。")
    history_root = handoff_root / "handoff-history"
    history_root.mkdir(parents=True, exist_ok=True)
    history_path = history_root / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{session_id}_before-transcript-edit.json"
    atomic_write_json(history_path, pointer)
    atomic_write_json(pointer_path, next_pointer)
    print(json.dumps({
        "status": "TRANSCRIPT_EDITED_AND_REPACKAGED",
        "note_id": note_id,
        "transcript_revision": correction["revision"],
        "bundle_path": str(destination),
        "manifest_sha256": next_pointer["manifest_sha256"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
