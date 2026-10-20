"""Revision-preserving transcript correction helpers for saved review notes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from services.review_capture.storage import atomic_write_json, new_id, read_json, utc_now, validate_note


def correct_transcript(
    note_path: Path,
    corrected_text: str,
    *,
    expected_transcript_revision: int | None,
    editor: str,
) -> dict[str, Any]:
    """Append a human correction while retaining ASR text and all prior revisions."""
    text = corrected_text.strip()
    if not text:
        raise ValueError("修订后的文字不能为空")
    if not editor.strip():
        raise ValueError("必须记录修订者")
    note = read_json(note_path)
    if note.get("status", {}).get("recording") not in {"saved", "interrupted"}:
        raise ValueError("录音还没有保存，暂不能修订转写")
    transcript = note.setdefault("transcript", {"state": "not_started", "text": None, "edited_text": None})
    current_revision = transcript.get("revision")
    if current_revision != expected_transcript_revision:
        raise RuntimeError(f"转写版本已变化：当前为 {current_revision!r}，提交的是 {expected_transcript_revision!r}；请重新读取后再修订")

    revisions = transcript.setdefault("revisions", [])
    if not isinstance(revisions, list):
        raise ValueError("transcript.revisions 格式无效")
    next_revision = max([int(item.get("revision", 0)) for item in revisions if isinstance(item, dict)] + [int(current_revision or 0)]) + 1
    now = utc_now()
    previous_text = transcript.get("edited_text") or transcript.get("text")
    revisions.append({
        "revision": next_revision,
        "kind": "manual_edit",
        "created_at": now,
        "editor": editor.strip(),
        "base_revision": current_revision,
        "previous_text": previous_text,
        "text": text,
    })
    transcript.update({
        "state": "edited",
        "edited_text": text,
        "revision": next_revision,
        "edited_at": now,
        "edited_by": editor.strip(),
        "error": None,
    })
    note.setdefault("status", {})["editing"] = "transcribed"
    note.setdefault("events", []).append({
        "event_id": new_id(),
        "kind": "transcription_edited",
        "transcript_revision": next_revision,
        "editor": editor.strip(),
        "created_at": now,
    })
    note["updated_at"] = now
    errors = validate_note(note)
    if errors:
        raise ValueError("意见记录不符合约束：" + "; ".join(errors))
    atomic_write_json(note_path, note)
    return {"note_id": note["note_id"], "revision": next_revision, "state": "edited", "text": text}
