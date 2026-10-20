"""Transcribe one saved Premiere review note without publishing the batch.

The recording and its timeline anchor are already durable before this process
starts. This process only updates the note's transcript in work/; the user's
explicit Send action creates the handoff package in output/.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.read_review_batch import is_within, safe_child, sha256_file
from scripts.transcribe_review_batch import (
    AUTO_LANGUAGE,
    append_transcription,
    find_cached_model,
    normalize_language,
    runtime_language,
    transcribe_audio,
)
from services.review_capture.storage import atomic_write_json, new_id, read_json, utc_now

ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")


def _id(value: str, label: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{label} 无效")
    return value


def _pid_is_running(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _run(args: argparse.Namespace, note_path: Path) -> dict[str, object]:
    note = read_json(note_path)
    if note.get("note_id") != args.note_id or note.get("session_id") != args.session_id:
        raise ValueError("意见记录与请求的批次或编号不一致")
    session_path = note_path.parent.parent.parent / "session.json"
    session = read_json(session_path)
    if session.get("session_id") != args.session_id or session.get("project") != note.get("project"):
        raise ValueError("意见记录与 Premiere 工程批次不一致")
    project_path = Path(str((session.get("project") or {}).get("path", ""))).resolve()
    if project_path.parent != args.project_root.resolve() or not project_path.is_file():
        raise ValueError("本批意见的 Premiere 工程文件不存在或不在当前项目目录")
    if note.get("status", {}).get("recording") not in {"saved", "interrupted"}:
        raise ValueError("录音尚未完整保存，不能转写")
    transcript = note.get("transcript") or {}
    if transcript.get("edited_text"):
        return {"status": "SKIPPED_HUMAN_EDIT", "note_id": args.note_id}
    if transcript.get("state") == "transcribed" and transcript.get("text"):
        return {"status": "SKIPPED_ALREADY_TRANSCRIBED", "note_id": args.note_id}
    audio = note.get("audio") or {}
    audio_path = safe_child(note_path.parent, audio.get("path"), "意见录音")
    if not audio_path.is_file() or sha256_file(audio_path).lower() != str(audio.get("sha256", "")).lower():
        raise ValueError("意见录音缺失或哈希不匹配")
    if args.dry_run:
        return {"status": "WOULD_TRANSCRIBE", "note_id": args.note_id, "audio": str(audio_path)}
    model_path = find_cached_model(args.model)
    from faster_whisper import WhisperModel

    # Keep Premiere responsive if the user starts recording the next note.
    model = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=2)
    expected_hash = sha256_file(note_path)
    result = transcribe_audio(model, audio_path, args.language)
    return append_transcription(note_path, audio_path, result, args.model, model_path, expected_hash)


def main() -> int:
    parser = argparse.ArgumentParser(description="自动转写一条已保存的 Premiere 审片意见")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--note-id", required=True)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--model", choices=["tiny", "base", "small", "medium"], default="medium")
    parser.add_argument("--language", default=None,
                        help="转写语言：默认读取安装配置，未配置时 auto（自动检测）；也可用 ISO 代码如 zh、en")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.language is None:
        args.language = runtime_language()
    args.language = normalize_language(args.language)
    args.session_id = _id(args.session_id, "批次编号")
    args.note_id = _id(args.note_id, "意见编号")
    project_root = args.project_root.resolve()
    note_path = (project_root / "work" / "premiere-review" / args.session_id / "notes" / args.note_id / "note.json").resolve()
    if not is_within(project_root / "work" / "premiere-review", note_path) or not note_path.is_file():
        raise ValueError("找不到这条意见记录")
    job_path = note_path.parent / "transcription" / "job.json"
    job_path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_json(job_path) if job_path.is_file() else {}
    if existing.get("status") == "running" and _pid_is_running(existing.get("pid")):
        print(json.dumps({"status": "ALREADY_RUNNING", "job_id": existing.get("job_id")}, ensure_ascii=False))
        return 0
    job_id = args.job_id or new_id()
    atomic_write_json(job_path, {
        "schema_version": "1.0", "job_id": job_id, "session_id": args.session_id,
        "note_id": args.note_id, "status": "running", "pid": os.getpid(),
        "model": args.model, "started_at": utc_now(),
    })
    try:
        result = _run(args, note_path)
        atomic_write_json(job_path, {
            "schema_version": "1.0", "job_id": job_id, "session_id": args.session_id,
            "note_id": args.note_id, "status": "completed", "pid": os.getpid(),
            "model": args.model, "result": result, "finished_at": utc_now(),
        })
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        atomic_write_json(job_path, {
            "schema_version": "1.0", "job_id": job_id, "session_id": args.session_id,
            "note_id": args.note_id, "status": "failed", "pid": os.getpid(),
            "model": args.model, "error": f"{type(exc).__name__}: {exc}", "finished_at": utc_now(),
        })
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
