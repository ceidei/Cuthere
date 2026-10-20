"""Transcribe saved Premiere review notes from a locally cached faster-whisper model.

Run after stopping recordings. The script never downloads a model and never
overwrites a human-edited transcript. Audio remains the primary evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.read_review_batch import is_within, read_object, safe_child, sha256_file, verify_current_batch
from services.review_capture.storage import atomic_write_json, new_id, package_session, read_json, utc_now


MODEL_REPOSITORIES = {
    "tiny": "models--Systran--faster-whisper-tiny",
    "base": "models--Systran--faster-whisper-base",
    "small": "models--Systran--faster-whisper-small",
    "medium": "models--Systran--faster-whisper-medium",
}


def find_cached_model(model_name: str) -> Path:
    hub_cache = Path(os.environ.get("HF_HUB_CACHE") or (Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"))
    model_root = hub_cache / MODEL_REPOSITORIES[model_name]
    snapshots = model_root / "snapshots"
    if not snapshots.is_dir():
        raise FileNotFoundError(f"本机没有 faster-whisper {model_name} 模型缓存：{model_root}")
    candidates = []
    main_ref = model_root / "refs" / "main"
    try:
        main_snapshot = main_ref.read_text(encoding="utf-8").strip()
    except OSError:
        main_snapshot = ""
    for path in snapshots.iterdir():
        if not path.is_dir():
            continue
        model_file = path / "model.bin"
        config_file = path / "config.json"
        if model_file.is_file() and model_file.stat().st_size > 0 and config_file.is_file():
            candidates.append(path)
    if not candidates:
        raise FileNotFoundError(f"{model_name} 缓存不完整，缺少 model.bin 或 config.json：{snapshots}")
    candidates.sort(key=lambda item: (item.name != main_snapshot, item.stat().st_mtime_ns), reverse=True)
    return candidates[0].resolve()


def transcribe_audio(model: Any, audio_path: Path, language: str) -> dict[str, Any]:
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        word_timestamps=True,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    segments: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for segment in segments_iter:
        content = str(segment.text or "").strip()
        if content:
            text_parts.append(content)
        words = []
        for word in (getattr(segment, "words", None) or []):
            words.append({
                "word": str(getattr(word, "word", "")),
                "start": round(float(getattr(word, "start", 0.0)), 3),
                "end": round(float(getattr(word, "end", 0.0)), 3),
                "probability": round(float(getattr(word, "probability", 0.0)), 4),
            })
        segments.append({
            "start": round(float(segment.start), 3),
            "end": round(float(segment.end), 3),
            "text": content,
            "words": words,
            "avg_logprob": round(float(getattr(segment, "avg_logprob", 0.0)), 4),
            "no_speech_prob": round(float(getattr(segment, "no_speech_prob", 0.0)), 4),
        })
    return {
        "text": "".join(text_parts).strip(),
        "segments": segments,
        "language": str(getattr(info, "language", language)),
        "language_probability": round(float(getattr(info, "language_probability", 0.0)), 4),
        "duration_seconds": round(float(getattr(info, "duration", 0.0)), 3),
    }


def same_pointer_generation(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if left is None or right is None:
        return left is right
    fields = ("session_id", "bundle_path", "manifest_path", "manifest_sha256", "project_path", "updated_at")
    return all(left.get(field) == right.get(field) for field in fields)


def publish_transcribed_session(
    project_root: Path,
    session_root: Path,
    session_id: str,
    expected_pointer: dict[str, Any],
) -> dict[str, Any]:
    output_root = (project_root / "output").resolve()
    handoff_root = project_root / "work" / "premiere-review"
    pointer_path = handoff_root / "CURRENT_REVIEW_BATCH.json"
    previous = read_object(pointer_path) if pointer_path.is_file() else None
    if not same_pointer_generation(previous, expected_pointer):
        raise RuntimeError("转写期间当前批次入口已变化；转写保留在 work 中，未覆盖新批次入口")
    if not previous or previous.get("session_id") != session_id:
        raise RuntimeError("当前批次入口不再指向本次转写的 session；未更新交接入口")
    project_path = previous.get("project_path")
    if not isinstance(project_path, str) or not project_path.strip():
        raise RuntimeError("当前交接入口缺少 Premiere 工程路径；未生成新的交接入口")

    bundle_path = package_session(session_root, output_root)
    manifest_path = bundle_path / "manifest.json"
    manifest_hash = sha256_file(manifest_path)
    previous = read_object(pointer_path) if pointer_path.is_file() else None
    if not same_pointer_generation(previous, expected_pointer):
        raise RuntimeError("打包期间当前批次入口已变化；新包留在 output 中，但未覆盖交接入口")
    history_root = handoff_root / "handoff-history"
    history_root.mkdir(parents=True, exist_ok=True)
    old_id = str(previous.get("session_id", "previous"))
    history_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{old_id}_before-transcription.json"
    atomic_write_json(history_root / history_name, previous)
    pointer = {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "project_path": project_path,
        "session_id": session_id,
        "bundle_path": str(bundle_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": manifest_hash,
        "supersedes_session_id": previous.get("session_id") if previous else None,
        "supersedes_bundle_path": previous.get("bundle_path") if previous else None,
    }
    atomic_write_json(pointer_path, pointer)
    return {"bundle_path": str(bundle_path), "manifest_path": str(manifest_path), "manifest_sha256": manifest_hash, "pointer_path": str(pointer_path)}


def append_transcription(
    note_path: Path,
    audio_path: Path,
    result: dict[str, Any],
    model_name: str,
    model_path: Path,
    expected_note_sha256: str,
) -> dict[str, Any]:
    note = read_json(note_path)
    if sha256_file(note_path).lower() != expected_note_sha256.lower():
        transcript = note.get("transcript") or {}
        if transcript.get("edited_text"):
            return {"note_id": note.get("note_id"), "status": "SKIPPED_HUMAN_EDIT", "reason": "转写期间出现人工修订，未覆盖"}
        return {"note_id": note.get("note_id"), "status": "SKIPPED_NOTE_CHANGED", "reason": "转写期间记录发生变化，未覆盖"}
    transcript = note.setdefault("transcript", {"state": "not_started", "text": None, "edited_text": None})
    if transcript.get("edited_text"):
        return {"note_id": note.get("note_id"), "status": "SKIPPED_HUMAN_EDIT", "reason": "已有人工作者文本，未覆盖"}
    if note.get("status", {}).get("recording") not in {"saved", "interrupted"}:
        return {"note_id": note.get("note_id"), "status": "SKIPPED_NO_AUDIO", "reason": "录音不是可处理的已保存状态"}
    prior_revisions = transcript.setdefault("revisions", [])
    next_revision = max([int(item.get("revision", 0)) for item in prior_revisions if isinstance(item, dict)] + [0]) + 1
    entry = {
        "revision": next_revision,
        "kind": "asr",
        "created_at": utc_now(),
        "text": result["text"],
        "segments": result["segments"],
        "language": result["language"],
        "language_probability": result["language_probability"],
        "duration_seconds": result["duration_seconds"],
        "model": model_name,
        "model_path": str(model_path),
        "audio_sha256": sha256_file(audio_path),
    }
    prior_revisions.append(entry)
    transcript.update({
        "state": "transcribed",
        "text": result["text"],
        "edited_text": None,
        "revision": next_revision,
        "model": model_name,
        "language": result["language"],
        "transcribed_at": entry["created_at"],
        "segments": result["segments"],
        "duration_seconds": result["duration_seconds"],
        "error": None,
    })
    status = note.setdefault("status", {})
    if status.get("editing") in {None, "unprocessed", "transcription_pending", "transcribed"}:
        status["editing"] = "transcribed"
    note.setdefault("events", []).append({
        "event_id": new_id(),
        "kind": "transcription_completed",
        "transcript_revision": next_revision,
        "model": model_name,
        "audio_sha256": entry["audio_sha256"],
        "created_at": entry["created_at"],
    })
    note["updated_at"] = entry["created_at"]
    atomic_write_json(note_path, note)
    return {"note_id": note.get("note_id"), "status": "TRANSCRIBED", "revision": next_revision, "characters": len(result["text"])}


def _run_batch(args: argparse.Namespace) -> int:
    project_root = args.project_root.resolve()
    intake = verify_current_batch(project_root)
    if intake.get("status") == "FAIL":
        raise SystemExit("当前批次完整性检查失败；未开始转写。请先运行 read_review_batch.py 并修复报告中的错误。")
    pointer_path = project_root / "work" / "premiere-review" / "CURRENT_REVIEW_BATCH.json"
    pointer = read_object(pointer_path)
    session_id = str(pointer.get("session_id", ""))
    if args.session_id and session_id != args.session_id:
        raise SystemExit("当前交接入口已经切换到另一批；未转写。")
    session_root = (project_root / "work" / "premiere-review" / session_id).resolve()
    if not is_within(project_root / "work" / "premiere-review", session_root) or not session_root.is_dir():
        raise SystemExit("当前意见批次的 work 目录不存在或越界")
    bundle = Path(str(pointer.get("bundle_path", ""))).resolve()
    manifest = read_object(Path(str(pointer.get("manifest_path", ""))).resolve())
    manifest_note_ids = sorted(str(item.get("note_id", "")) for item in manifest.get("notes", []) if isinstance(item, dict))
    work_note_ids = sorted(path.parent.name for path in (session_root / "notes").glob("*/note.json"))
    if manifest_note_ids != work_note_ids:
        raise SystemExit("工作批次与已导出清单的意见数量或编号不同；请先重新整理本批意见，未开始转写。")
    model_path = None if args.dry_run else find_cached_model(args.model)
    model = None
    if not args.dry_run:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SystemExit(f"本机 Python 缺少 faster-whisper：{exc}")
        model = WhisperModel(str(model_path), device="cpu", compute_type="int8")

    results: list[dict[str, Any]] = []
    for item in manifest.get("notes", []):
        if not isinstance(item, dict):
            results.append({"status": "FAIL", "reason": "manifest 条目格式无效"})
            continue
        note_id = str(item.get("note_id", ""))
        bundle_note_path = safe_child(bundle, item.get("note_path"), f"{note_id} bundle note path")
        work_note_path = (session_root / "notes" / note_id / "note.json").resolve()
        if not is_within(session_root / "notes", work_note_path) or not work_note_path.is_file():
            results.append({"note_id": note_id, "status": "FAIL", "reason": "工作目录中的源记录不存在"})
            continue
        if sha256_file(work_note_path).lower() != str(item.get("note_sha256", "")).lower():
            results.append({"note_id": note_id, "status": "FAIL", "reason": "工作记录已不同于当前导出包；请先重新整理批次"})
            continue
        note = read_object(bundle_note_path)
        transcript = note.get("transcript") or {}
        if transcript.get("edited_text"):
            results.append({"note_id": note_id, "status": "SKIPPED_HUMAN_EDIT", "reason": "已有人工修订，未覆盖"})
            continue
        if transcript.get("state") == "transcribed" and transcript.get("text"):
            results.append({"note_id": note_id, "status": "SKIPPED_ALREADY_TRANSCRIBED"})
            continue
        audio_rel = (note.get("audio") or {}).get("path")
        audio_path = safe_child(work_note_path.parent, audio_rel, f"{note_id} audio path")
        if not audio_path.is_file() or sha256_file(audio_path).lower() != str((note.get("audio") or {}).get("sha256", "")).lower():
            results.append({"note_id": note_id, "status": "FAIL", "reason": "录音缺失或 SHA-256 不匹配"})
            continue
        if args.dry_run:
            results.append({"note_id": note_id, "status": "WOULD_TRANSCRIBE", "audio": str(audio_path)})
            continue
        expected_note_sha256 = sha256_file(work_note_path)
        try:
            result = transcribe_audio(model, audio_path, args.language)
            results.append(append_transcription(work_note_path, audio_path, result, args.model, model_path, expected_note_sha256))
        except Exception as exc:
            if sha256_file(work_note_path).lower() != expected_note_sha256.lower():
                results.append({"note_id": note_id, "status": "SKIPPED_NOTE_CHANGED", "reason": "转写失败期间记录发生变化，未改写当前记录"})
            else:
                results.append({"note_id": note_id, "status": "TRANSCRIPTION_FAILED", "reason": f"{type(exc).__name__}: {exc}"})

    output: dict[str, Any] = {
        "schema_version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id,
        "model": args.model,
        "model_path": str(model_path) if model_path else None,
        "dry_run": args.dry_run,
        "results": results,
        "transcribed": sum(item.get("status") == "TRANSCRIBED" for item in results),
        "failed": sum(item.get("status") in {"FAIL", "TRANSCRIPTION_FAILED"} for item in results),
    }
    report_root = project_root / "work" / "premiere-review" / session_id / "transcription"
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"transcription-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
    atomic_write_json(report_path, output)
    current_work_note_ids = sorted(path.parent.name for path in (session_root / "notes").glob("*/note.json"))
    if not args.dry_run and output["transcribed"] and current_work_note_ids != manifest_note_ids:
        output["publication_error"] = "转写期间本批新增或变化了意见；文字保留在 work，本次没有重打包或改动交接入口。"
        atomic_write_json(report_path, output)
    elif not args.dry_run and output["transcribed"]:
        try:
            published = publish_transcribed_session(project_root, session_root, session_id, pointer)
            output["published"] = published
        except Exception as exc:
            output["publication_error"] = f"{type(exc).__name__}: {exc}"
        atomic_write_json(report_path, output)
    print(json.dumps({"session_id": session_id, "transcribed": output["transcribed"], "failed": output["failed"], "dry_run": args.dry_run, "report": str(report_path), "published": output.get("published")}, ensure_ascii=False))
    return 1 if output["failed"] or output.get("publication_error") else 0


def _pid_is_running(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="转写当前审片批次的已保存录音，使用本地缓存模型")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--model", choices=sorted(MODEL_REPOSITORIES), default="medium")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    project_root = args.project_root.resolve()
    session_id = args.session_id
    if not session_id:
        pointer_path = project_root / "work" / "premiere-review" / "CURRENT_REVIEW_BATCH.json"
        try:
            session_id = str(read_object(pointer_path).get("session_id", ""))
        except Exception:
            session_id = ""
    job_path: Path | None = None
    job_id = args.job_id or new_id()
    if session_id:
        job_root = (project_root / "work" / "premiere-review" / session_id / "transcription").resolve()
        if is_within(project_root / "work" / "premiere-review", job_root):
            job_root.mkdir(parents=True, exist_ok=True)
            job_path = job_root / "job.json"
            existing = read_object(job_path) if job_path.is_file() else {}
            if existing.get("status") == "running" and _pid_is_running(existing.get("pid")):
                print(json.dumps({"status": "ALREADY_RUNNING", "session_id": session_id, "job_id": existing.get("job_id")}, ensure_ascii=False))
                return 0
            atomic_write_json(job_path, {
                "schema_version": "1.0",
                "job_id": job_id,
                "session_id": session_id,
                "status": "running",
                "pid": os.getpid(),
                "model": args.model,
                "started_at": utc_now(),
            })
    try:
        result = _run_batch(args)
        if job_path:
            atomic_write_json(job_path, {
                "schema_version": "1.0",
                "job_id": job_id,
                "session_id": session_id,
                "status": "completed" if result == 0 else "failed",
                "pid": os.getpid(),
                "model": args.model,
                "finished_at": utc_now(),
                "exit_code": result,
            })
        return result
    except BaseException as exc:
        if job_path:
            atomic_write_json(job_path, {
                "schema_version": "1.0",
                "job_id": job_id,
                "session_id": session_id,
                "status": "failed",
                "pid": os.getpid(),
                "model": args.model,
                "finished_at": utc_now(),
                "error": f"{type(exc).__name__}: {exc}",
            })
        raise


if __name__ == "__main__":
    raise SystemExit(main())
