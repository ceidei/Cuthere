"""Build a non-destructive dry-run plan from explicitly reviewed decisions."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from services.review_capture.storage import atomic_write_json


SUPPORTED_ACTIONS = {"delete_range", "gain_range", "no_change", "manual_review"}


def make_decision_template(batch: dict[str, Any]) -> dict[str, Any]:
    decisions = []
    for note in batch["notes"]:
        transcript = note.get("transcript") if isinstance(note.get("transcript"), dict) else {}
        decisions.append({
            "note_id": note.get("note_id"),
            "revision_id": note.get("revision_id"),
            "transcript_revision": transcript.get("revision"),
            "action": "manual_review",
            "approved": False,
            "range_override": None,
            "gain_db": None,
            "rationale": "",
            "review_notes": "请听原录音并核对文字后填写动作、范围和理由。",
        })
    return {
        "schema_version": "1.0",
        "session_id": batch["session_id"],
        "manifest_sha256": batch["manifest_sha256"],
        "project_path": batch.get("project_path"),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "approved_by": None,
        "approved_at": None,
        "decisions": decisions,
    }


def _note_range(note: dict[str, Any], decision: dict[str, Any]) -> tuple[int, int, str | None, list[str]]:
    errors: list[str] = []
    override = decision.get("range_override")
    if isinstance(override, dict):
        try:
            start = int(override["start_ticks"])
            end = int(override["end_ticks"])
        except (KeyError, TypeError, ValueError):
            return 0, 0, None, ["人工范围必须提供整数 start_ticks 与 end_ticks"]
        sequence_id = str(override.get("sequence_id") or note.get("sequence", {}).get("host_id") or "") or None
        if not decision.get("rationale"):
            errors.append("人工改写范围必须填写理由")
        if sequence_id != str(note.get("sequence", {}).get("host_id") or ""):
            errors.append("人工范围的序列 ID 与意见记录不一致")
        return start, end, sequence_id, errors

    location = note.get("location") if isinstance(note.get("location"), dict) else {}
    normalized = location.get("normalized") if isinstance(location.get("normalized"), dict) else {}
    if note.get("mode") != "range" or location.get("range_status") != "locked":
        return 0, 0, None, ["点位意见或未锁定区间不能直接生成剪辑范围；需人工填写 range_override"]
    endpoints = location.get("endpoints") if isinstance(location.get("endpoints"), dict) else {}
    start_anchor, end_anchor = endpoints.get("start"), endpoints.get("end")
    if not isinstance(start_anchor, dict) or not isinstance(end_anchor, dict):
        return 0, 0, None, ["锁定区间缺少起点或终点"]
    if start_anchor.get("sequence_id") != end_anchor.get("sequence_id"):
        errors.append("起点和终点不属于同一序列")
    try:
        start = int(normalized.get("start_ticks", start_anchor.get("ticks")))
        end = int(normalized.get("end_ticks", end_anchor.get("ticks")))
    except (TypeError, ValueError):
        return 0, 0, None, ["锁定区间的 ticks 无效"]
    if end <= start:
        errors.append("范围必须大于 0")
    sequence_id = str(start_anchor.get("sequence_id") or "") or None
    return start, end, sequence_id, errors


def _overlapping_clips(note: dict[str, Any], start: int, end: int) -> tuple[list[dict[str, Any]], list[str]]:
    snapshots = note.get("_verified_snapshots", {})
    source = snapshots.get("start") or snapshots.get("point") or snapshots.get("end")
    timeline = source.get("timeline_snapshot") if isinstance(source, dict) else None
    clips = timeline.get("clips") if isinstance(timeline, dict) else None
    if not isinstance(clips, list):
        return [], ["宿主快照没有可用的片段列表"]
    targets: list[dict[str, Any]] = []
    errors: list[str] = []
    for clip in clips:
        if not isinstance(clip, dict):
            errors.append("快照中存在无效片段记录")
            continue
        try:
            clip_start = int(clip["start_ticks"])
            clip_end = int(clip["end_ticks"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"片段边界不完整：{clip.get('name', 'unknown')}")
            continue
        overlap_start = max(start, clip_start)
        overlap_end = min(end, clip_end)
        if overlap_end > overlap_start:
            targets.append({
                "track_type": clip.get("track_type"),
                "track_index": clip.get("track_index"),
                "clip_index": clip.get("clip_index"),
                "name": clip.get("name"),
                "project_item_node_id": clip.get("project_item_node_id"),
                "clip_start_ticks": str(clip_start),
                "clip_end_ticks": str(clip_end),
                "overlap_start_ticks": str(overlap_start),
                "overlap_end_ticks": str(overlap_end),
            })
    if not targets:
        errors.append("范围内没有映射到可识别的时间线片段")
    return targets, errors


def build_dry_run(
    batch: dict[str, Any],
    decision_doc: dict[str, Any],
    timeline_check: dict[str, Any] | None = None,
    timeline_check_path: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    timeline_notes: dict[str, dict[str, Any]] = {}
    timeline_check_summary = None
    if timeline_check is not None:
        for field in ("session_id", "manifest_sha256"):
            if timeline_check.get(field) != batch.get(field):
                errors.append(f"时间线核对报告 {field} 与当前意见批次不一致")
        report_project = str(timeline_check.get("project_path", "")).casefold()
        batch_project = str(batch.get("project_path", "")).casefold()
        if report_project != batch_project:
            errors.append("时间线核对报告 project_path 与当前意见批次不一致")
        report_notes = timeline_check.get("notes")
        if not isinstance(report_notes, list):
            errors.append("时间线核对报告缺少意见状态列表")
        else:
            for report_note in report_notes:
                if isinstance(report_note, dict) and report_note.get("note_id"):
                    timeline_notes[str(report_note["note_id"])] = report_note
        timeline_check_summary = timeline_check.get("summary")

    if decision_doc.get("schema_version") != "1.0":
        errors.append("决策文件 schema_version 必须为 1.0")
    for field in ("session_id", "manifest_sha256", "project_path"):
        if decision_doc.get(field) != batch.get(field):
            errors.append(f"决策文件 {field} 与当前批次不一致")
    entries = decision_doc.get("decisions")
    if not isinstance(entries, list):
        raise ValueError("决策文件 decisions 必须是列表")
    by_note = {note["note_id"]: note for note in batch["notes"]}
    seen: set[str] = set()
    plans: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            errors.append("决策列表包含非对象条目")
            continue
        note_id = str(item.get("note_id", ""))
        if note_id in seen:
            errors.append(f"同一意见出现重复决策：{note_id}")
            continue
        seen.add(note_id)
        note = by_note.get(note_id)
        if not note:
            errors.append(f"决策引用了当前批次不存在的意见：{note_id}")
            continue
        item_errors: list[str] = []
        if item.get("revision_id") != note.get("revision_id"):
            item_errors.append("意见修订号已变化")
        transcript = note.get("transcript") if isinstance(note.get("transcript"), dict) else {}
        if item.get("transcript_revision") != transcript.get("revision"):
            item_errors.append("转写修订号已变化；请重新听审并确认文字")
        if transcript.get("state") not in {"transcribed", "edited"} or not (transcript.get("edited_text") or transcript.get("text")):
            item_errors.append("意见还没有可核对的转写文字")

        action = item.get("action")
        if action not in SUPPORTED_ACTIONS:
            item_errors.append(f"不支持的剪辑动作：{action}")
        start = end = 0
        sequence_id: str | None = None
        targets: list[dict[str, Any]] = []
        timeline_status = "not_required"
        timeline_reasons: list[str] = []
        if action in {"delete_range", "gain_range"}:
            start, end, sequence_id, range_errors = _note_range(note, item)
            item_errors.extend(range_errors)
            if end <= start:
                item_errors.append("操作范围无效")
            else:
                targets, map_errors = _overlapping_clips(note, start, end)
                item_errors.extend(map_errors)
            if action == "gain_range":
                try:
                    gain_db = float(item.get("gain_db"))
                    if not -24.0 <= gain_db <= 6.0:
                        item_errors.append("gain_db 必须在 -24 到 +6 dB 之间")
                except (TypeError, ValueError):
                    item_errors.append("gain_range 必须提供 gain_db")
            if timeline_check is None:
                timeline_status = "missing"
                item_errors.append("缺少当前时间线核对报告；请先在 Premiere 点“核对当前时间线”，再用 --timeline-check 生成预检")
            else:
                check_note = timeline_notes.get(note_id)
                if check_note is None:
                    timeline_status = "missing_note"
                    item_errors.append("时间线核对报告中没有这条意见")
                else:
                    timeline_status = str(check_note.get("status") or "unknown")
                    timeline_reasons = [str(reason) for reason in (check_note.get("reasons") or [])]
                    if check_note.get("revision_id") != note.get("revision_id"):
                        item_errors.append("时间线核对报告对应的意见修订号已过期")
                    if sequence_id and str(check_note.get("sequence_id") or "") != str(sequence_id):
                        item_errors.append("当前时间线核对报告中的活动序列与意见捕获序列不同")
                    if timeline_status != "matched":
                        item_errors.append("当前时间线核对状态为 " + timeline_status + ("：" + "；".join(timeline_reasons) if timeline_reasons else ""))
        else:
            gain_db = None

        location = note.get("location") if isinstance(note.get("location"), dict) else {}
        if location.get("version_status") != "matched" or location.get("automatic_edit_allowed") is not True:
            item_errors.append("捕获时的时间线版本未获准自动编辑；当前仅生成审阅草案")
        if action in {"delete_range", "gain_range"} and not item.get("rationale"):
            item_errors.append("剪辑动作必须写明理由")
        approved = item.get("approved") is True
        if approved and (not decision_doc.get("approved_by") or not decision_doc.get("approved_at")):
            item_errors.append("标为 approved 的决策必须填写 approved_by 与 approved_at")

        status = "READY_FOR_REVIEW" if not item_errors and not approved else ("BLOCKED" if item_errors else "APPROVED_HOST_DISABLED")
        plans.append({
            "note_id": note_id,
            "revision_id": note.get("revision_id"),
            "transcript_revision": transcript.get("revision"),
            "transcript": transcript.get("edited_text") or transcript.get("text"),
            "action": action,
            "approved": approved,
            "status": status,
            "start_ticks": str(start) if end > start else None,
            "end_ticks": str(end) if end > start else None,
            "sequence_id": sequence_id,
            "gain_db": item.get("gain_db") if action == "gain_range" else None,
            "rationale": item.get("rationale"),
            "target_clips": targets,
            "warnings": item_errors,
            "timeline_version_status": location.get("version_status"),
            "automatic_edit_allowed_at_capture": location.get("automatic_edit_allowed"),
            "current_timeline_check_status": timeline_status,
            "current_timeline_check_reasons": timeline_reasons,
        })
    missing = sorted(set(by_note) - seen)
    if missing:
        errors.append("以下意见没有决策记录：" + ", ".join(missing))

    mutating = [item for item in plans if item.get("action") in {"delete_range", "gain_range"} and item.get("start_ticks") and item.get("end_ticks")]
    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(mutating):
        left_start, left_end = int(left["start_ticks"]), int(left["end_ticks"])
        for right in mutating[index + 1:]:
            if left.get("sequence_id") != right.get("sequence_id"):
                continue
            right_start, right_end = int(right["start_ticks"]), int(right["end_ticks"])
            if max(left_start, right_start) < min(left_end, right_end):
                conflict = {"note_ids": [left["note_id"], right["note_id"]], "kind": "overlapping_ranges"}
                conflicts.append(conflict)
                left["warnings"].append(f"与意见 {right['note_id']} 的操作范围重叠，需人工合并")
                right["warnings"].append(f"与意见 {left['note_id']} 的操作范围重叠，需人工合并")
                left["status"] = right["status"] = "BLOCKED"

    ordered = sorted(mutating, key=lambda item: int(item["start_ticks"]), reverse=True)
    for order, item in enumerate(ordered, 1):
        item["suggested_order"] = order
    blocked_count = sum(item.get("status") == "BLOCKED" for item in plans)
    return {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": batch["session_id"],
        "manifest_sha256": batch["manifest_sha256"],
        "project_path": batch.get("project_path"),
        "batch_verification_status": batch.get("verification_status"),
        "batch_warnings": batch.get("verification_warnings", []),
        "timeline_revalidation": {
            "provided": timeline_check is not None,
            "path": timeline_check_path,
            "manifest_sha256": timeline_check.get("manifest_sha256") if timeline_check else None,
            "summary": timeline_check_summary,
        },
        "status": "BLOCKED" if errors or conflicts or blocked_count else "READY_FOR_REVIEW",
        "execution_eligible": False,
        "execution_disabled_reason": "旧版批次决策模板仅做审阅 dry-run；短区间 F8/文字剪辑请使用 scripts/podcast_edit.py 的可回滚流程，并在 Premiere 导出后读回核验。",
        "errors": errors,
        "conflicts": conflicts,
        "decision_count": len(plans),
        "blocked_count": blocked_count,
        "operations": plans,
    }


def write_dry_run(plan: dict[str, Any], work_root: Path) -> tuple[Path, Path]:
    output = work_root / "plans"
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    json_path = output / f"dry-run-{stamp}.json"
    csv_path = output / f"dry-run-{stamp}.csv"
    atomic_write_json(json_path, plan)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["意见ID", "动作", "起点ticks", "终点ticks", "序列ID", "增益dB", "状态", "理由", "命中片段", "警告"])
        for item in plan.get("operations", []):
            targets = " | ".join(
                f"{target.get('track_type')}轨{target.get('track_index')}:{target.get('name')}[{target.get('overlap_start_ticks')}-{target.get('overlap_end_ticks')}]"
                for target in item.get("target_clips", [])
            )
            writer.writerow([
                item.get("note_id"), item.get("action"), item.get("start_ticks"), item.get("end_ticks"),
                item.get("sequence_id"), item.get("gain_db"), item.get("status"), item.get("rationale"),
                targets, "；".join(item.get("warnings", [])),
            ])
    return json_path, csv_path
