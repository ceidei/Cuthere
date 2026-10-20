"""One execution path for typed instructions and transcribed Premiere F8 notes.

Examples:
  python scripts/podcast_edit.py plan --project P.prproj --xml baseline.xml --text "把 00:47:28:20 到 00:47:31:15 删除" --job work/ave/jobs/001
  python scripts/podcast_edit.py apply --job work/ave/jobs/001
  python scripts/podcast_edit.py verify --job work/ave/jobs/001 --premiere-xml exported.xml
  python scripts/podcast_edit.py rollback --job work/ave/jobs/001
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.podcast_edit_runtime import (apply_delete, compare_readback, dry_run,
                                         parse_instruction, sha256, timeline_digest, write_json)
from engine.review_notes import ReviewBatchError, load_current_batch


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def _note(path: Path) -> dict:
    note = _read(path)
    if note.get("status", {}).get("recording") not in {"saved", "interrupted"}:
        raise ValueError("F8 录音未保存")
    audio = note.get("audio") or {}
    audio_path = (path.parent / audio.get("path", "")).resolve()
    if not audio_path.is_file() or not audio_path.is_relative_to(path.parent.resolve()) or sha256(audio_path) != audio.get("sha256"):
        raise ValueError("F8 原录音哈希校验失败")
    return note


def _intake(args: argparse.Namespace) -> dict:
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
    if not isinstance(snapshot, list):
        raise ValueError("F8 意见快照必须为列表")
    decisions = _read(args.resolved) if args.resolved else {}
    items = []
    for note in snapshot:
        note_id = str(note["note_id"])
        text = str(note.get("text") or "")
        decision = decisions.get(note_id, {})
        items.append({"note_id": note_id, "source": "f8", "mode": note.get("mode"),
                      "anchor": note.get("point") or note.get("start"), "end": note.get("end"),
                      "text": text, "source_note_sha256": note.get("note_sha256"),
                      "resolution": decision.get("status", "unresolved"),
                      "resolution_evidence": decision.get("evidence"),
                      "suggested_action": "no_change" if decision.get("status") == "already_applied" else parse_instruction(text)["action"]})
    report = {"schema_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(),
              "source_snapshot": str(args.snapshot.resolve()), "source_sha256": sha256(args.snapshot),
              "notes": items}
    write_json(args.output, report)
    return {"report": str(args.output), "notes": len(items), "already_applied": sum(i["resolution"] == "already_applied" for i in items)}


def _batch_plan(args: argparse.Namespace) -> dict:
    """Bridge the installed F8 panel's verified batch pointer to edit decisions."""
    if args.job_root.exists():
        raise FileExistsError(args.job_root)
    batch = load_current_batch(args.project_root)
    resolved = _read(args.resolved) if args.resolved else {}
    args.job_root.mkdir(parents=True)
    rows = []
    for note in batch["notes"]:
        note_id = str(note["note_id"])
        resolution = resolved.get(note_id, {})
        row = {"note_id": note_id,
               "source_note": note["_verified_bundle_note_path"],
               "source_note_sha256": sha256(Path(note["_verified_bundle_note_path"])),
               "resolution": resolution.get("status", "unresolved"),
               "resolution_evidence": resolution.get("evidence")}
        if row["resolution"] == "already_applied":
            row.update({"status": "SKIPPED_ALREADY_APPLIED", "action": "no_change"})
        else:
            note_args = argparse.Namespace(project=args.project, xml=args.xml,
                                           text=None, f8_note=Path(row["source_note"]),
                                           job=args.job_root / note_id)
            try:
                outcome = _plan(note_args)
                row.update({"status": outcome["status"], "action": outcome["action"],
                            "decision": outcome["decision"]})
            except (ValueError, FileNotFoundError, FileExistsError, KeyError) as exc:
                row.update({"status": "BLOCKED", "error": str(exc)})
        rows.append(row)
    report = {"schema_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(),
              "batch_manifest": batch["manifest_path"],
              "batch_manifest_sha256": batch["manifest_sha256"],
              "project_path": str(args.project.resolve()), "project_sha256": sha256(args.project),
              "baseline_xml": str(args.xml.resolve()), "baseline_sha256": sha256(args.xml),
              "notes": rows}
    write_json(args.job_root / "decision_batch.json", report)
    return {"report": str(args.job_root / "decision_batch.json"),
            "notes": len(rows), "ready": sum(r["status"] == "READY" for r in rows),
            "manual_review": sum(r["status"] == "MANUAL_REVIEW" for r in rows),
            "already_applied": sum(r["status"] == "SKIPPED_ALREADY_APPLIED" for r in rows)}


def _plan(args: argparse.Namespace) -> dict:
    if args.job.exists():
        raise FileExistsError(f"工作目录已存在：{args.job}")
    note = _note(args.f8_note) if args.f8_note else None
    text = args.text if args.text else ((note.get("transcript") or {}).get("edited_text") or (note.get("transcript") or {}).get("text"))
    if not text:
        raise ValueError("请提供文字指令，或传入已有转写的 F8 note.json")
    instruction = parse_instruction(text, note)
    if note:
        instruction["note_id"] = note.get("note_id")
        instruction["note_sha256"] = sha256(args.f8_note)
        instruction["note_path"] = str(args.f8_note.resolve())
        if instruction["action"] == "delete_range":
            captured_project = str((note.get("project") or {}).get("path") or "")
            captured_sequence = str((note.get("sequence") or {}).get("name") or "")
            current_sequence = timeline_digest(args.xml)["name"]
            location = note.get("location") or {}
            if (captured_project.casefold() != str(args.project.resolve()).casefold()
                    or captured_sequence != current_sequence
                    or location.get("automatic_edit_allowed") is not True):
                instruction = {"action": "manual_review", "text": text, "source": "f8",
                               "note_id": note.get("note_id"), "note_sha256": sha256(args.f8_note),
                               "note_path": str(args.f8_note.resolve()),
                               "reason": "录音捕获工程、序列或版本与当前基准不一致，不能按旧时间码自动剪"}
    plan = dry_run(args.xml.resolve(), instruction, args.project.resolve())
    args.job.mkdir(parents=True)
    write_json(args.job / "decision_dry_run.json", plan)
    markdown = ["# 播客剪辑决策表（dry-run）", "", f"原话：{text}", f"动作：{instruction['action']}",
                f"状态：{plan['status']}", f"基准序列：{plan['sequence']} ({plan['duration_before_frames']} 帧)"]
    if instruction["action"] == "delete_range":
        markdown += [f"区间：[ {instruction['start_frame']}, {instruction['end_frame']} ) 帧",
                     f"预计删除：{plan['removed_frames']} 帧；预计时长：{plan['duration_after_frames']} 帧",
                     "", "受影响轨道："]
        markdown += [f"- {item['track']}（enabled={item['enabled']}）：{len(item['clips'])} 段" for item in plan["targets"]]
    markdown += ["", f"工程 SHA-256：{plan['project_sha256']}", f"XML SHA-256：{plan['baseline_sha256']}",
                 "后续：先生成新 XML，在 Premiere 导入为新序列并导出 XML 读回；旧工程和旧序列保留。"]
    (args.job / "decision_dry_run.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return {"job": str(args.job), "status": plan["status"], "action": instruction["action"],
            "decision": str(args.job / "decision_dry_run.md")}


def _apply(args: argparse.Namespace) -> dict:
    plan = _read(args.job / "decision_dry_run.json")
    candidate = args.job / "candidate_for_premiere.xml"
    receipt = apply_delete(plan, candidate)
    receipt.update({"created_at": datetime.now(timezone.utc).isoformat(),
                    "plan_sha256": sha256(args.job / "decision_dry_run.json"),
                    "rollback_source_xml": plan["baseline_xml"],
                    "rollback_source_sha256": plan["baseline_sha256"],
                    "project_checkpoint": plan["project_path"],
                    "project_checkpoint_sha256": plan["project_sha256"]})
    write_json(args.job / "candidate_receipt.json", receipt)
    return receipt


def _verify(args: argparse.Namespace) -> dict:
    if (args.job / "premiere_publish" / "state.json").exists() and not args.premiere_xml:
        from engine.premiere_roundtrip import PremiereRoundTrip
        return PremiereRoundTrip().verify(args.job / "premiere_publish")
    if not args.premiere_xml:
        raise ValueError("离线任务读回需提供 --premiere-xml；后台发布任务会自动从 Premiere 读取")
    receipt = _read(args.job / "candidate_receipt.json")
    if sha256(Path(receipt["candidate_xml"])) != receipt["candidate_sha256"]:
        raise ValueError("候选 XML 在导入后发生变化")
    result = compare_readback(Path(receipt["candidate_xml"]), args.premiere_xml)
    result.update({"checked_at": datetime.now(timezone.utc).isoformat(),
                   "premiere_xml": str(args.premiere_xml.resolve()),
                   "project_checkpoint": receipt["project_checkpoint"],
                   "rollback_source_xml": receipt["rollback_source_xml"]})
    output = args.job / "premiere_readback_receipt.json"
    if output.exists():
        prior = _read(output)
        if prior.get("premiere_readback_sha256") != result["premiere_readback_sha256"]:
            raise ValueError("已有不同的 Premiere 读回；请创建新工作记录以保留两次证据")
        return prior
    write_json(output, result)
    return result


def _rollback(args: argparse.Namespace) -> dict:
    if (args.job / "premiere_publish" / "state.json").exists():
        from engine.premiere_roundtrip import PremiereRoundTrip
        return PremiereRoundTrip().rollback(args.job / "premiere_publish")
    receipt = _read(args.job / "candidate_receipt.json")
    if sha256(Path(receipt["rollback_source_xml"])) != receipt["rollback_source_sha256"]:
        raise ValueError("恢复点 XML 哈希不符")
    if sha256(Path(receipt["project_checkpoint"])) != receipt["project_checkpoint_sha256"]:
        raise ValueError("恢复点工程哈希不符")
    restore = args.job / "rollback_for_premiere.xml"
    if restore.exists():
        prior = _read(args.job / "rollback_receipt.json")
        if sha256(restore) != prior.get("restore_sha256"):
            raise ValueError("已有恢复 XML 的哈希不符")
        return prior
    shutil.copyfile(receipt["rollback_source_xml"], restore)
    result = {"status": "RESTORE_XML_READY", "created_at": datetime.now(timezone.utc).isoformat(),
              "restore_xml": str(restore), "restore_sha256": sha256(restore),
              "checkpoint_project": receipt["project_checkpoint"],
              "checkpoint_sha256": receipt["project_checkpoint_sha256"],
              "instruction": "在 Premiere 打开恢复点工程或导入此 XML，导出序列 XML 再核验；此步没有修改现用序列。"}
    write_json(args.job / "rollback_receipt.json", result)
    return result


def _verify_rollback(args: argparse.Namespace) -> dict:
    receipt = _read(args.job / "rollback_receipt.json")
    restore = Path(receipt["restore_xml"])
    if sha256(restore) != receipt["restore_sha256"]:
        raise ValueError("恢复 XML 在导入后发生变化")
    result = compare_readback(restore, args.premiere_xml)
    result.update({"checked_at": datetime.now(timezone.utc).isoformat(),
                   "premiere_xml": str(args.premiere_xml.resolve()),
                   "restored_project": str(args.restored_project.resolve()),
                   "restored_project_sha256": sha256(args.restored_project),
                   "checkpoint_project": receipt["checkpoint_project"]})
    output = args.job / "premiere_rollback_readback_receipt.json"
    if output.exists():
        prior = _read(output)
        if (prior.get("premiere_readback_sha256") != result["premiere_readback_sha256"] or
                prior.get("restored_project_sha256") != result["restored_project_sha256"]):
            raise ValueError("已有不同的回滚读回；请创建新工作记录以保留两次证据")
        return prior
    write_json(output, result)
    return result


def _operate(args: argparse.Namespace) -> dict:
    """Route spoken or typed commands through the same checked operations."""
    note = _note(args.f8_note) if args.f8_note else None
    text = args.text or (((note.get("transcript") or {}).get("edited_text") or
                         (note.get("transcript") or {}).get("text")) if note else None)
    if not text:
        raise ValueError("请提供文字，或传入已转写的 F8 note.json")
    action = parse_instruction(text, note)["action"]
    if action == "readback":
        if args.job and (args.job / "premiere_publish" / "state.json").exists():
            return _verify(args)
        if not args.job or not args.premiere_xml:
            raise ValueError("读回需指定 --job 和 Premiere 导出的 --premiere-xml")
        if (args.job / "rollback_receipt.json").exists() and args.restored_project:
            return _verify_rollback(args)
        return _verify(args)
    if action == "rollback":
        if not args.job:
            raise ValueError("回滚需指定 --job")
        return _rollback(args)
    if action == "delete_range":
        if not args.job or not args.project or not args.xml:
            raise ValueError("剪辑需指定 --job、--project 和可导入的基准 --xml")
        plan_args = argparse.Namespace(job=args.job, project=args.project, xml=args.xml,
                                       text=args.text, f8_note=args.f8_note)
        result = _plan(plan_args)
        if result["status"] == "READY":
            result["candidate"] = _apply(plan_args)
            result["status"] = "PREMIERE_IMPORT_REQUIRED"
            if getattr(args, "working_project", None):
                result["premiere"] = _publish(args)
                result["candidate"]["premiere_readback_status"] = "PASS"
                result["status"] = result["premiere"]["status"]
        return result
    if not args.job or not args.project or not args.xml:
        raise ValueError("意见预检需指定 --job、--project 和 --xml")
    return _plan(argparse.Namespace(job=args.job, project=args.project, xml=args.xml,
                                    text=args.text, f8_note=args.f8_note))


def _publish(args: argparse.Namespace) -> dict:
    from engine.premiere_roundtrip import PremiereRoundTrip
    receipt = _read(args.job / "candidate_receipt.json")
    candidate = Path(receipt["candidate_xml"])
    if sha256(candidate) != receipt["candidate_sha256"]:
        raise ValueError("候选 XML 哈希不符")
    if sha256(Path(receipt["project_checkpoint"])) != receipt["project_checkpoint_sha256"]:
        raise ValueError("基准工程在决策后发生变化，请重新读回并生成剪辑决策")
    return PremiereRoundTrip().publish(candidate, receipt["candidate_sequence"],
                                       Path(receipt["project_checkpoint"]), args.working_project,
                                       args.job / "premiere_publish")


def main() -> int:
    parser = argparse.ArgumentParser(description="播客 F8/文字意见统一剪辑入口")
    sub = parser.add_subparsers(dest="command", required=True)
    intake = sub.add_parser("intake")
    intake.add_argument("--snapshot", type=Path, required=True)
    intake.add_argument("--resolved", type=Path)
    intake.add_argument("--output", type=Path, required=True)
    batch_plan = sub.add_parser("batch-plan")
    batch_plan.add_argument("--project-root", type=Path, required=True)
    batch_plan.add_argument("--project", type=Path, required=True)
    batch_plan.add_argument("--xml", type=Path, required=True)
    batch_plan.add_argument("--resolved", type=Path)
    batch_plan.add_argument("--job-root", type=Path, required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--project", type=Path, required=True)
    plan.add_argument("--xml", type=Path, required=True)
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--f8-note", type=Path)
    plan.add_argument("--job", type=Path, required=True)
    operate = sub.add_parser("operate", help="统一接收 F8 转写或文字剪辑/读回/回滚指令")
    operate_source = operate.add_mutually_exclusive_group(required=True)
    operate_source.add_argument("--text")
    operate_source.add_argument("--f8-note", type=Path)
    operate.add_argument("--job", type=Path)
    operate.add_argument("--project", type=Path)
    operate.add_argument("--xml", type=Path)
    operate.add_argument("--premiere-xml", type=Path)
    operate.add_argument("--restored-project", type=Path)
    operate.add_argument("--working-project", type=Path, help="通过桥直接导入、激活、保存并读回；必须为新路径")
    publish = sub.add_parser("publish")
    publish.add_argument("--job", type=Path, required=True)
    publish.add_argument("--working-project", type=Path, required=True)
    for name in ("apply", "verify", "rollback", "verify-rollback"):
        command = sub.add_parser(name)
        command.add_argument("--job", type=Path, required=True)
        if name in {"verify", "verify-rollback"}:
            command.add_argument("--premiere-xml", type=Path, required=name == "verify-rollback")
        if name == "verify-rollback":
            command.add_argument("--restored-project", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = {"intake": _intake, "batch-plan": _batch_plan,
                  "plan": _plan, "operate": _operate, "apply": _apply,
                  "verify": _verify, "rollback": _rollback,
                  "publish": _publish,
                  "verify-rollback": _verify_rollback}[args.command](args)
    except (ValueError, FileNotFoundError, FileExistsError, KeyError, RuntimeError, ReviewBatchError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
