"""Verified read-only access to a Premiere review batch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.read_review_batch import read_object, safe_child, sha256_file, verify_current_batch


class ReviewBatchError(RuntimeError):
    pass


def load_current_batch(project_root: Path) -> dict[str, Any]:
    """Load the fixed current batch after validating its hashes and path bounds.

    NEEDS_REVIEW batches can be drafted into a decision sheet, but any warning
    remains attached to the result and automatic execution stays disabled.
    """
    root = project_root.resolve()
    verification = verify_current_batch(root)
    if verification.get("status") == "FAIL":
        errors = "; ".join(verification.get("errors", [])) or "批次完整性核验失败"
        raise ReviewBatchError(errors)

    pointer_path = root / "work" / "premiere-review" / "CURRENT_REVIEW_BATCH.json"
    pointer = read_object(pointer_path)
    bundle = Path(str(pointer.get("bundle_path", ""))).resolve()
    manifest_path = Path(str(pointer.get("manifest_path", ""))).resolve()
    if str(bundle) != str(Path(str(verification.get("bundle_path", ""))).resolve()):
        raise ReviewBatchError("读取期间当前批次入口已变化；请重新读取")
    if not manifest_path.is_file() or sha256_file(manifest_path).lower() != str(pointer.get("manifest_sha256", "")).lower():
        raise ReviewBatchError("当前批次 manifest 缺失或哈希不匹配")
    manifest = read_object(manifest_path)
    if manifest.get("session_id") != pointer.get("session_id"):
        raise ReviewBatchError("交接入口与批次 session_id 不一致")

    entries = manifest.get("notes")
    if not isinstance(entries, list):
        raise ReviewBatchError("manifest.notes 格式无效")
    notes: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            raise ReviewBatchError("manifest 中存在无效意见条目")
        note_path = safe_child(bundle, item.get("note_path"), "意见记录路径")
        if not note_path.is_file() or sha256_file(note_path).lower() != str(item.get("note_sha256", "")).lower():
            raise ReviewBatchError(f"意见记录缺失或哈希不匹配：{item.get('note_id', '')}")
        note = read_object(note_path)
        if note.get("note_id") != item.get("note_id") or note.get("revision_id") != item.get("revision_id"):
            raise ReviewBatchError(f"意见编号或修订号与 manifest 不一致：{item.get('note_id', '')}")

        note_dir = note_path.parent
        loaded_snapshots: dict[str, Any] = {}
        anchors = []
        location = note.get("location") if isinstance(note.get("location"), dict) else {}
        if note.get("mode") == "range":
            endpoints = location.get("endpoints") if isinstance(location.get("endpoints"), dict) else {}
            anchors.extend((side, endpoints.get(side)) for side in ("start", "end"))
        elif note.get("mode") == "point":
            anchors.append(("point", location.get("point")))
        for side, anchor in anchors:
            if not isinstance(anchor, dict):
                continue
            reference = anchor.get("snapshot_ref")
            if not isinstance(reference, dict):
                continue
            snapshot_path = safe_child(note_dir, reference.get("path"), f"{side} 快照路径")
            if not snapshot_path.is_file() or sha256_file(snapshot_path).lower() != str(reference.get("sha256", "")).lower():
                raise ReviewBatchError(f"意见快照缺失或哈希不匹配：{item.get('note_id', '')}/{side}")
            loaded_snapshots[side] = read_object(snapshot_path)
        note["_verified_bundle_note_path"] = str(note_path)
        note["_verified_snapshots"] = loaded_snapshots
        note["_manifest_item"] = item
        notes.append(note)

    return {
        "project_root": str(root),
        "project_path": pointer.get("project_path"),
        "session_id": manifest.get("session_id"),
        "bundle_path": str(bundle),
        "manifest_path": str(manifest_path),
        "manifest_sha256": pointer.get("manifest_sha256"),
        "verification_status": verification.get("status"),
        "verification_warnings": verification.get("warnings", []),
        "verification_errors": verification.get("errors", []),
        "notes": notes,
    }
