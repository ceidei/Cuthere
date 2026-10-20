"""Verify and read the current Premiere review-note handoff bundle.

This command is read-only with respect to the bundle. It writes an intake report
under the project's work directory so Codex can inspect one verified batch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点不是对象：{path}")
    return value


def is_within(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_child(base: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError(f"{label} 为空")
    normalized = relative.replace("\\", "/")
    posix = PurePosixPath(normalized)
    if posix.is_absolute() or re.match(r"^[A-Za-z]:", normalized) or any(part in {"..", ""} for part in posix.parts):
        raise ValueError(f"{label} 必须是安全的相对路径：{relative}")
    destination = (base / Path(*posix.parts)).resolve()
    if not is_within(base, destination):
        raise ValueError(f"{label} 越出批次目录：{relative}")
    return destination


def verify_snapshot(note_dir: Path, reference: Any, label: str, errors: list[str]) -> bool:
    if not isinstance(reference, dict):
        errors.append(f"{label} 快照引用缺失")
        return False
    try:
        snapshot = safe_child(note_dir, reference.get("path"), f"{label} 快照路径")
        if not snapshot.is_file():
            errors.append(f"{label} 快照文件不存在")
            return False
        expected = reference.get("sha256")
        actual = sha256_file(snapshot)
        if not isinstance(expected, str) or actual.lower() != expected.lower():
            errors.append(f"{label} 快照 SHA-256 不匹配")
            return False
        return True
    except Exception as exc:
        errors.append(f"{label} 快照引用无效：{exc}")
        return False


def inspect_note(bundle: Path, manifest_item: dict[str, Any], seen_ids: set[str]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    note_id = str(manifest_item.get("note_id", ""))
    if not note_id:
        errors.append("manifest 条目缺少 note_id")
    elif note_id in seen_ids:
        errors.append(f"note_id 重复：{note_id}")
    seen_ids.add(note_id)

    try:
        note_path = safe_child(bundle, manifest_item.get("note_path"), f"{note_id or 'note'} 记录路径")
        if not note_path.is_file():
            raise FileNotFoundError("note.json 不存在")
        if sha256_file(note_path).lower() != str(manifest_item.get("note_sha256", "")).lower():
            errors.append("note.json SHA-256 不匹配")
        note = read_object(note_path)
    except Exception as exc:
        return {"note_id": note_id, "status": "FAIL", "errors": [str(exc)], "warnings": [], "execution_eligible": False}

    if note.get("note_id") != note_id:
        errors.append("note_id 与 manifest 不一致")
    if note.get("revision_id") != manifest_item.get("revision_id"):
        errors.append("revision_id 与 manifest 不一致")
    note_dir = note_path.parent
    location = note.get("location") if isinstance(note.get("location"), dict) else {}
    mode = note.get("mode")
    recording = (note.get("status") or {}).get("recording")
    range_status = location.get("range_status")
    if recording != manifest_item.get("recording_status"):
        errors.append("录音状态与 manifest 不一致")
    if range_status != manifest_item.get("range_status"):
        errors.append("范围状态与 manifest 不一致")

    audio = note.get("audio") if isinstance(note.get("audio"), dict) else {}
    audio_path = None
    if recording in {"saved", "interrupted"}:
        try:
            audio_path = safe_child(note_dir, audio.get("path"), "录音路径")
            if not audio_path.is_file():
                errors.append("录音文件不存在")
            elif not audio.get("sha256") or sha256_file(audio_path).lower() != str(audio.get("sha256")).lower():
                errors.append("录音 SHA-256 不匹配")
        except Exception as exc:
            errors.append(f"录音路径无效：{exc}")
    elif recording not in {"failed", "cancelled"}:
        warnings.append(f"录音状态为 {recording or 'unknown'}，需要先确认是否可回听")

    anchors: list[tuple[str, dict[str, Any]]] = []
    if mode == "range":
        endpoints = location.get("endpoints") or {}
        for side in ("start", "end"):
            anchor = endpoints.get(side)
            if isinstance(anchor, dict):
                anchors.append((side, anchor))
        if range_status != "locked" or len(anchors) != 2:
            warnings.append("区间未完整锁定，不能进入可执行意见队列")
        if location.get("version_status") == "partial" or location.get("automatic_edit_allowed") is False:
            warnings.append("时间线版本核验覆盖不全，剪辑前需人工回看")
        if location.get("version_status") not in {"matched", "partial"} and range_status == "locked":
            warnings.append("范围虽标为 locked，但缺少可确认的版本状态")
    elif mode == "point":
        point = location.get("point")
        if isinstance(point, dict):
            anchors.append(("point", point))
        else:
            warnings.append("单点位置缺失")
    else:
        warnings.append(f"定位方式 {mode or 'unknown'} 不在当前交接读取器支持范围内")

    for side, anchor in anchors:
        snapshot_ref = anchor.get("snapshot_ref")
        verify_snapshot(note_dir, snapshot_ref, side, errors)
    if not anchors:
        warnings.append("没有可核对的宿主定位锚点")

    transcript = note.get("transcript") if isinstance(note.get("transcript"), dict) else {}
    transcript_state = transcript.get("state", "not_started")
    transcript_text = transcript.get("edited_text") or transcript.get("text")
    if not transcript_text:
        warnings.append("尚无可读转写；需从录音理解意见或补录文字")
    elif transcript_state not in {"transcribed", "edited"}:
        warnings.append(f"转写状态为 {transcript_state}，需人工核对文字")

    status = "FAIL" if errors else ("NEEDS_REVIEW" if warnings else "PASS")
    return {
        "note_id": note_id,
        "revision_id": note.get("revision_id"),
        "mode": mode,
        "recording_status": recording,
        "range_status": range_status,
        "version_status": location.get("version_status"),
        "automatic_edit_allowed": location.get("automatic_edit_allowed"),
        "location": location,
        "transcript_state": transcript_state,
        "transcript_text": transcript_text,
        "note_path": str(note_path),
        "audio_path": str(audio_path) if audio_path else None,
        "errors": errors,
        "warnings": warnings,
        "status": status,
        "execution_eligible": False,
    }


def verify_current_batch(project_root: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    pointer_path = project_root / "work" / "premiere-review" / "CURRENT_REVIEW_BATCH.json"
    output_root = (project_root / "output").resolve()
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "pointer_path": str(pointer_path),
        "status": "FAIL",
        "errors": [],
        "warnings": [],
        "notes": [],
        "execution_eligible": False,
    }
    if not pointer_path.is_file():
        report["errors"].append("没有 CURRENT_REVIEW_BATCH.json；先在 Premiere 面板整理并导出一批意见")
        return report
    try:
        pointer = read_object(pointer_path)
        pointer_project = Path(str(pointer.get("project_path", ""))).resolve()
        if pointer_project.parent != project_root:
            raise ValueError("交接入口指向的工程目录与 --project-root 不一致")
        bundle = Path(str(pointer.get("bundle_path", ""))).resolve()
        manifest_path = Path(str(pointer.get("manifest_path", ""))).resolve()
        if not is_within(output_root, bundle) or not bundle.is_dir():
            raise ValueError("批次目录不存在或越出项目 output 目录")
        if not is_within(bundle, manifest_path) or not manifest_path.is_file():
            raise ValueError("manifest 不存在或不在批次目录中")
        if sha256_file(manifest_path).lower() != str(pointer.get("manifest_sha256", "")).lower():
            raise ValueError("manifest SHA-256 与固定交接入口不一致")
        manifest = read_object(manifest_path)
        if manifest.get("session_id") != pointer.get("session_id"):
            raise ValueError("session_id 与固定交接入口不一致")
        entries = manifest.get("notes")
        if not isinstance(entries, list):
            raise ValueError("manifest.notes 不是列表")
        session_path = bundle / "session.json"
        if not session_path.is_file():
            raise ValueError("批次包缺少 session.json")
        session = read_object(session_path)
        if session.get("session_id") != manifest.get("session_id"):
            report["errors"].append("session.json 的 session_id 与 manifest 不一致")
        session_notes = session.get("notes") if isinstance(session.get("notes"), list) else []
        manifest_ids = [str(item.get("note_id", "")) for item in entries if isinstance(item, dict)]
        if len(manifest_ids) != len(entries) or len(manifest_ids) != len(set(manifest_ids)):
            report["errors"].append("manifest 含非对象或重复 note_id 条目")
        if sorted(map(str, session_notes)) != sorted(manifest_ids):
            report["errors"].append("session.json 与 manifest 的意见数量或编号不一致")
        seen: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                report["notes"].append({"status": "FAIL", "errors": ["manifest 条目不是对象"], "warnings": [], "execution_eligible": False})
                continue
            note_result = inspect_note(bundle, item, seen)
            report["notes"].append(note_result)
        report["bundle_path"] = str(bundle)
        report["manifest_path"] = str(manifest_path)
        report["manifest_sha256"] = pointer.get("manifest_sha256")
        report["session_id"] = manifest.get("session_id")
    except Exception as exc:
        report["errors"].append(f"交接入口或批次包无效：{type(exc).__name__}: {exc}")

    note_errors = sum(len(item.get("errors", [])) for item in report["notes"])
    note_warnings = sum(len(item.get("warnings", [])) for item in report["notes"])
    report["summary"] = {"note_count": len(report["notes"]), "note_errors": note_errors, "note_warnings": note_warnings}
    if report["errors"] or note_errors:
        report["status"] = "FAIL"
    elif note_warnings:
        report["status"] = "NEEDS_REVIEW"
        report["warnings"].append("完整性核对通过，但存在未转写、待定位或需人工核对的意见")
    else:
        report["status"] = "PASS"
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Premiere 审片意见批次读取报告", "", f"- 状态：**{report['status']}**", f"- 工程：`{report['project_root']}`"]
    if report.get("bundle_path"):
        lines.append(f"- 批次：`{report['bundle_path']}`")
    if report.get("summary"):
        lines.append(f"- 意见数：{report['summary']['note_count']}；错误 {report['summary']['note_errors']}；待核对 {report['summary']['note_warnings']}")
    for error in report.get("errors", []):
        lines.append(f"- 错误：{error}")
    for warning in report.get("warnings", []):
        lines.append(f"- 提示：{warning}")
    for number, note in enumerate(report.get("notes", []), 1):
        lines.extend(["", f"## 意见 {number:02d} · {note.get('note_id', 'unknown')}", ""])
        lines.append(f"- 状态：{note.get('status')}；录音 {note.get('recording_status')}；定位 {note.get('range_status') or note.get('mode')}")
        lines.append(f"- 版本核验：{note.get('version_status') or '不适用/未知'}；可自动剪辑：否")
        if note.get("transcript_text"):
            lines.append(f"- 转写：{note['transcript_text']}")
        if note.get("audio_path"):
            lines.append(f"- 录音：`{note['audio_path']}`")
        for error in note.get("errors", []):
            lines.append(f"- 错误：{error}")
        for warning in note.get("warnings", []):
            lines.append(f"- 待核对：{warning}")
    lines.extend(["", "此报告只核对批次完整性。它不会把口述自动解释为剪辑指令，也不会修改工程。", ""])
    return "\n".join(lines)


def write_report(report_dir: Path, report: dict[str, Any]) -> None:
    report_dir = report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=False)
    (report_dir / "review_batch.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (report_dir / "review_batch.md").write_text(render_markdown(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 Premiere 审片意见当前批次，并生成 Codex 可读报告")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    report = verify_current_batch(project_root)
    report_dir = args.report_dir.resolve() if args.report_dir else (
        project_root / "work" / "premiere-review" / ("intake-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    )
    work_root = (project_root / "work" / "premiere-review").resolve()
    if not is_within(work_root, report_dir):
        raise SystemExit("报告目录只能写入项目 work/premiere-review 目录")
    write_report(report_dir, report)
    print(json.dumps({"status": report["status"], "report_dir": str(report_dir), "summary": report.get("summary")}, ensure_ascii=False))
    return 0 if report["status"] in {"PASS", "NEEDS_REVIEW"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
