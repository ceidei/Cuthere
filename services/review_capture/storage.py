"""Append-only review notes with atomic manifests and independent file hashes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NOTE_STATES = {"preparing", "recording", "finalizing", "saved", "interrupted", "failed", "cancelled"}
RANGE_STATES = {"not_applicable", "awaiting_end", "capturing_end", "locked", "needs_endpoint", "stale"}
EDIT_STATES = {"unprocessed", "transcription_pending", "transcribed", "plan_ready", "applied", "reviewed", "blocked"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 1.5
        while True:
            try:
                os.replace(temp_name, path)
                break
            except PermissionError:
                # CEP frequently has the old JSON file open without delete sharing.
                # A short retry preserves the atomic manifest instead of killing the worker.
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.025)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def validate_note(note: dict[str, Any], require_audio: bool = False) -> list[str]:
    errors: list[str] = []
    for field in ("schema_version", "session_id", "note_id", "revision_id", "mode", "project", "sequence", "audio", "status"):
        if field not in note:
            errors.append(f"missing {field}")
    for key in ("session_id", "note_id", "revision_id"):
        if key in note and not _SAFE_ID.fullmatch(str(note[key])):
            errors.append(f"invalid {key}")
    status = note.get("status", {})
    if not isinstance(status, dict) or status.get("recording") not in NOTE_STATES:
        errors.append("invalid recording status")
    elif require_audio and status.get("recording") not in {"saved", "interrupted"}:
        errors.append("recording is not committed")
    mode = note.get("mode")
    if mode not in {"point", "range", "inout", "selection"}:
        errors.append("invalid mode")
    if mode == "range":
        location = note.get("location", {})
        state = location.get("range_status")
        if state not in RANGE_STATES:
            errors.append("invalid range_status")
        if state == "locked":
            endpoints = location.get("endpoints", {})
            start, end = endpoints.get("start"), endpoints.get("end")
            if not isinstance(start, dict) or not isinstance(end, dict):
                errors.append("locked range must have both endpoints")
            elif int(start.get("ticks", "0")) == int(end.get("ticks", "0")):
                errors.append("locked range cannot have zero length")
            elif start.get("sequence_id") != end.get("sequence_id"):
                errors.append("range endpoints must belong to the same sequence")
    audio = note.get("audio", {})
    if require_audio and (not isinstance(audio, dict) or not audio.get("sha256") or not audio.get("samples")):
        errors.append("committed audio needs sha256 and sample count")
    return errors


def update_note(note_path: Path, **patch: Any) -> dict[str, Any]:
    note = read_json(note_path)
    note.update(patch)
    note["updated_at"] = utc_now()
    errors = validate_note(note)
    # Transient endpoint-pending states intentionally remain valid.
    if errors:
        raise ValueError("; ".join(errors))
    atomic_write_json(note_path, note)
    return note


def package_session(session_dir: Path, output_root: Path) -> Path:
    """Create a versioned review bundle without modifying the work session."""
    session = read_json(session_dir / "session.json")
    notes_dir = session_dir / "notes"
    note_files = sorted(notes_dir.glob("*/note.json"), key=lambda path: (
        str(read_json(path).get("created_at", "")), path.parent.name,
    ))
    notes: list[dict[str, Any]] = []
    for path in note_files:
        note = read_json(path)
        errors = validate_note(note)
        if errors:
            raise ValueError(f"{path}: {'; '.join(errors)}")
        audio_path = path.parent / str(note.get("audio", {}).get("path", ""))
        if note.get("status", {}).get("recording") in {"saved", "interrupted"}:
            if not audio_path.is_file() or sha256_file(audio_path) != note["audio"].get("sha256"):
                raise ValueError(f"audio missing or hash mismatch: {path.parent}")
        notes.append(note)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_root / f"{timestamp}_审片意见_{session['session_id'][:8]}"
    destination = base
    suffix = 1
    while destination.exists():
        destination = Path(f"{base}_r{suffix}")
        suffix += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        raise FileExistsError(staging)
    shutil.copytree(session_dir, staging)
    manifest_notes = []
    for note in notes:
        note_dir = staging / "notes" / note["note_id"]
        note_path = note_dir / "note.json"
        manifest_notes.append({
            "note_id": note["note_id"],
            "revision_id": note["revision_id"],
            "mode": note["mode"],
            "recording_status": note["status"]["recording"],
            "range_status": note.get("location", {}).get("range_status"),
            "version_status": note.get("location", {}).get("version_status"),
            "automatic_edit_allowed": note.get("location", {}).get("automatic_edit_allowed"),
            "note_path": str(note_path.relative_to(staging)).replace("\\", "/"),
            "note_sha256": sha256_file(note_path),
        })
    manifest = {
        "schema_version": "1.0",
        "session_id": session["session_id"],
        "created_at": utc_now(),
        "source_session": str(session_dir.resolve()),
        "notes": manifest_notes,
    }
    atomic_write_json(staging / "manifest.json", manifest)
    (staging / "审片意见清单.md").write_text(render_notes_md(notes), encoding="utf-8")
    os.replace(staging, destination)
    return destination


def render_notes_md(notes: list[dict[str, Any]]) -> str:
    lines = ["# 审片意见清单", "", "以下意见均保留录音原件和定位快照。未锁定的区间不能作为自动剪辑范围；版本检查覆盖不完整时须在剪辑前人工复核。", ""]
    for number, note in enumerate(notes, 1):
        location = note.get("location", {})
        endpoints = location.get("endpoints", {})
        if note.get("mode") == "range" and endpoints.get("start") and endpoints.get("end"):
            start, end = endpoints["start"], endpoints["end"]
            where = f"{start.get('time_label', start.get('ticks'))} → {end.get('time_label', end.get('ticks'))} ({location.get('range_status')})"
        elif note.get("mode") == "range":
            where = f"区间待补充 ({location.get('range_status')})"
        else:
            point = location.get("point", {})
            where = point.get("time_label", point.get("ticks", "位置待确认"))
        transcript = note.get("transcript", {}).get("edited_text") or note.get("transcript", {}).get("text") or "（待转写）"
        version = location.get("version_status", "unknown")
        editable = location.get("automatic_edit_allowed") is True
        lines.extend([
            f"## {number:02d} · {note['note_id'][:8]}",
            "",
            f"- 位置：{where}",
            f"- 工程/序列：{note.get('project', {}).get('path', '')} · {note.get('sequence', {}).get('name', '')}",
            f"- 录音：{note.get('status', {}).get('recording')} · 文字：{note.get('status', {}).get('editing')}",
            f"- 版本核对：{version} · 可自动剪辑：{'是' if editable else '否，需人工核对'}",
            f"- 意见：{transcript}",
            f"- 音频：`notes/{note['note_id']}/{note.get('audio', {}).get('path', '')}`",
            "",
        ])
    return "\n".join(lines)

