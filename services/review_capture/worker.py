"""Single-instance Windows file-queue worker for Premiere review recordings."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio import MicrophoneRecorder, enumerate_input_devices, recover_interrupted_capture
from .ptt import PushToTalk
from .storage import (
    atomic_write_json,
    new_id,
    package_session,
    read_json,
    sha256_file,
    update_note,
    utc_now,
    validate_note,
)
from .timebase import TICKS_PER_SECOND, normalize_interval
from engine.podcast_edit_runtime import parse_instruction

WORKER_VERSION = "1.2.6"
_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def _win_single_instance(lock_path: Path) -> Any:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    if os.name == "nt":
        import msvcrt

        if lock_path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            stream.close()
            raise RuntimeError("录音助手已经在运行") from exc
    else:
        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise RuntimeError("录音助手已经在运行") from exc
    return stream


def _safe_id(value: Any, label: str) -> str:
    text = str(value or "")
    if not _ID.fullmatch(text):
        raise ValueError(f"{label} 格式无效")
    return text


def _same_context(start: dict[str, Any], end: dict[str, Any]) -> tuple[bool, str]:
    if str(start.get("project_path", "")).casefold() != str(end.get("project_path", "")).casefold():
        return False, "project_changed"
    if str(start.get("sequence_id", "")) != str(end.get("sequence_id", "")):
        return False, "sequence_changed"
    if str(start.get("sequence_name", "")) != str(end.get("sequence_name", "")):
        return False, "sequence_changed"
    if str(start.get("timebase", "")) != str(end.get("timebase", "")):
        return False, "timebase_changed"
    start_status = start.get("content_fingerprint_status")
    end_status = end.get("content_fingerprint_status")
    if start_status not in {"complete", "partial"} or end_status not in {"complete", "partial"}:
        return False, "content_fingerprint_unavailable"
    if start_status != end_status:
        return False, "content_fingerprint_coverage_changed"
    baseline_a = start.get("content_fingerprint")
    baseline_b = end.get("content_fingerprint")
    if not isinstance(baseline_a, str) or not baseline_a or not isinstance(baseline_b, str) or not baseline_b:
        return False, "content_fingerprint_unavailable"
    if baseline_a != baseline_b:
        return False, "timeline_content_changed"
    # The release-time playhead preview reuses the press-time fingerprint when
    # a full second snapshot was unavailable. It locates the range but cannot
    # establish that the timeline content stayed unchanged.
    partial = start_status == "partial" or end.get("capture_quality") == "release_preview_only"
    return True, "content_fingerprint_partial" if partial else ""


def _save_snapshot(note_dir: Path, role: str, snapshot: dict[str, Any]) -> dict[str, str]:
    filename = f"snapshot-{role}-{new_id()}.json"
    snapshot_path = note_dir / filename
    atomic_write_json(snapshot_path, snapshot)
    digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    return {"path": filename, "sha256": digest}


def _elapsed_label(ticks: int) -> str:
    """Readable elapsed time; the exact Premiere position remains in ticks."""
    negative = ticks < 0
    milliseconds = (abs(ticks) * 1000) // TICKS_PER_SECOND
    hours, rem = divmod(milliseconds, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, fraction = divmod(rem, 1000)
    return ("-" if negative else "") + f"{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:03d}"


class ReviewCaptureWorker:
    def __init__(self, ipc_root: Path, idle_timeout: float = 5.0):
        self.ipc_root = ipc_root
        self.inbox = ipc_root / "inbox"
        self.outbox = ipc_root / "outbox"
        self.processing = ipc_root / "processing"
        self.results = ipc_root / "results"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.processing.mkdir(parents=True, exist_ok=True)
        self.results.mkdir(parents=True, exist_ok=True)
        self.recorder: MicrophoneRecorder | None = None
        self.active_note_path: Path | None = None
        self.active_session_root: Path | None = None
        self.active_note_id: str | None = None
        self.last_heartbeat = time.monotonic()
        self._heartbeat_mtime: int | None = None
        self._last_status_write = 0.0
        self.idle_timeout = idle_timeout
        self._shutdown_requested = False
        self._lock_handle = _win_single_instance(ipc_root / "worker.lock")
        self.ptt = PushToTalk(ipc_root)
        self._recover_stale_recording()
        self._write_status()

    def run(self, once: bool = False) -> int:
        while True:
            commands = sorted(self.inbox.glob("*.json"), key=lambda p: p.name)
            for path in commands:
                self._process_file(path)
            self._check_heartbeat_file()
            self.ptt.poll(panel_alive=time.monotonic() - self.last_heartbeat < self.idle_timeout,
                          manual_recording=self.recorder is not None)
            if time.monotonic() - self._last_status_write >= (0.1 if self.ptt.current else 0.5):
                self._write_status()
            if once:
                return 0
            if self._shutdown_requested:
                return 0
            if self.recorder and time.monotonic() - self.last_heartbeat > self.idle_timeout:
                self._interrupt_active("面板心跳超时，录音已自动停止")
            time.sleep(0.02)

    def _check_heartbeat_file(self) -> None:
        try:
            mtime = (self.ipc_root / "heartbeat.json").stat().st_mtime_ns
        except OSError:
            return
        if mtime != self._heartbeat_mtime:
            self._heartbeat_mtime = mtime
            self.last_heartbeat = time.monotonic()

    def _write_status(self) -> None:
        active: dict[str, Any] | None = None
        if self.recorder is not None:
            active = {
                "note_id": self.active_note_id,
                "note_path": str(self.active_note_path) if self.active_note_path else None,
                "elapsed_seconds": round(time.monotonic() - self.recorder.started_monotonic, 3),
                "samples_written": self.recorder.samples,
                "input_peak": round(self.recorder.last_level, 4),
            }
            if self.active_note_path and self.active_note_path.is_file():
                try:
                    note = read_json(self.active_note_path)
                    active["mode"] = note.get("mode")
                    active["location"] = note.get("location")
                except Exception:
                    active["location"] = None
        try:
            atomic_write_json(self.ipc_root / "status.json", {
                "worker_version": WORKER_VERSION,
                "pid": os.getpid(),
                "updated_at": utc_now(),
                "active_recording": active,
                "push_to_talk": self.ptt.status(),
            })
        except PermissionError as exc:
            # A stale status is recoverable on the next poll; crashing loses the hotkey.
            logging.warning("status publication delayed by a Windows file lock: %s", exc)
        self._last_status_write = time.monotonic()

    def _process_file(self, path: Path) -> None:
        try:
            command = read_json(path)
            request_id = _safe_id(command.get("request_id"), "request_id")
        except Exception as exc:
            logging.warning("invalid command file %s: %s", path.name, exc)
            bad = self.processing / (path.name + ".bad")
            try:
                os.replace(path, bad)
            except OSError:
                pass
            return
        result_path = self.results / f"{request_id}.json"
        reply_path = self.outbox / f"{request_id}.json"
        if result_path.exists():
            result = read_json(result_path)
            atomic_write_json(reply_path, result)
            path.unlink(missing_ok=True)
            return
        marker = self.processing / f"{request_id}.json"
        if marker.exists():
            result = {
                "request_id": request_id,
                "ok": False,
                "error": "outcome_unknown",
                "message": "请求在助手异常退出前已开始，未自动重做；请核对意见状态。",
            }
            atomic_write_json(reply_path, result)
            path.unlink(missing_ok=True)
            return
        atomic_write_json(marker, {"request_id": request_id, "started_at": utc_now(), "action": command.get("action")})
        try:
            result = self._dispatch(command)
            result = {"request_id": request_id, "ok": True, "result": result}
        except Exception as exc:
            logging.exception("command failed action=%s request_id=%s", command.get("action"), request_id)
            result = {
                "request_id": request_id,
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
            }
        atomic_write_json(result_path, result)
        atomic_write_json(reply_path, result)
        path.unlink(missing_ok=True)

    def _dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        action = command.get("action")
        payload = command.get("payload") or {}
        if action == "ping":
            return {"worker_version": WORKER_VERSION, "pid": os.getpid(), "active_note_id": self.active_note_id}
        if action == "devices":
            return {"devices": enumerate_input_devices()}
        if action == "heartbeat":
            self.last_heartbeat = time.monotonic()
            return {"heartbeat": "ok", "active_note_id": self.active_note_id}
        if action == "start":
            return self._start(payload)
        if action == "lock_end":
            return self._lock_end(payload)
        if action == "reselect_end":
            return self._reselect_end(payload)
        if action == "stop":
            return self._stop(payload)
        if action == "attach_endpoint":
            return self._attach_endpoint(payload)
        if action == "cancel":
            return self._cancel(payload)
        if action == "list_notes":
            return self._list_notes(payload)
        if action == "correct_transcript":
            return self._correct_transcript(payload)
        if action == "revalidate_batch":
            return self._revalidate_batch(payload)
        if action == "package":
            return self._package(payload)
        if action == "ptt_commit":
            return self._ptt_commit(payload)
        if action == "ptt_ack":
            self.ptt.acknowledge(_safe_id(payload.get("capture_id"), "capture_id"))
            return {"acknowledged": True}
        if action == "shutdown":
            if self.recorder:
                self._interrupt_active("录音助手收到关闭请求")
            if self.ptt.current and self.ptt.current.get("state") == "recording":
                self.ptt._stop()
            self._shutdown_requested = True
            return {"shutdown": True}
        raise ValueError(f"不支持的请求：{action}")

    def _project_context(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Path]:
        project = snapshot.get("project")
        sequence = snapshot.get("sequence")
        if not isinstance(project, dict) or not isinstance(sequence, dict):
            raise ValueError("Premiere 没有返回当前工程与序列信息")
        project_path = Path(str(project.get("path", "")))
        if not project_path.is_absolute() or not project_path.exists():
            raise ValueError("请先保存 Premiere 工程，再录制审片意见")
        sequence_name = str(sequence.get("name", "")).strip()
        if not sequence_name:
            raise ValueError("没有活动序列")
        project = dict(project)
        project["path"] = str(project_path.resolve())
        sequence = dict(sequence)
        return project, sequence, project_path.parent

    def _session_root(self, project_root: Path, session_id: str) -> Path:
        destination = (project_root / "work" / "premiere-review" / session_id).resolve()
        base = (project_root / "work" / "premiere-review").resolve()
        if not destination.is_relative_to(base):
            raise ValueError("意见目录越出项目 work 目录")
        return destination

    def _ensure_session(self, root: Path, session_id: str, project: dict[str, Any]) -> None:
        manifest = root / "session.json"
        root.mkdir(parents=True, exist_ok=True)
        if manifest.exists():
            current = read_json(manifest)
            if current.get("session_id") != session_id or str(current.get("project", {}).get("path", "")).casefold() != str(project["path"]).casefold():
                raise ValueError("已有意见批次与当前工程不一致")
        else:
            atomic_write_json(manifest, {
                "schema_version": "1.0",
                "session_id": session_id,
                "created_at": utc_now(),
                "project": project,
                "notes": [],
            })

    @staticmethod
    def _anchor(snapshot: dict[str, Any], event_type: str, operation_id: str) -> dict[str, Any]:
        location = snapshot.get("location")
        if not isinstance(location, dict):
            raise ValueError("宿主没有返回时间线位置")
        ticks = location.get("ticks")
        if ticks is None or not re.fullmatch(r"-?\d+", str(ticks)):
            raise ValueError("宿主 ticks 缺失或格式无效")
        project = snapshot.get("project") or {}
        sequence = snapshot.get("sequence") or {}
        return {
            "event_id": new_id(),
            "kind": event_type,
            "operation_id": operation_id,
            "button_source": event_type,
            "ticks": str(ticks),
            "time_label": location.get("time_label"),
            "project_path": project.get("path"),
            "project_id": project.get("host_id"),
            "sequence_id": sequence.get("host_id"),
            "sequence_name": sequence.get("name"),
            "timebase": sequence.get("timebase"),
            "frame_rate": sequence.get("frame_rate"),
            "read_at": snapshot.get("read_at") or utc_now(),
            "content_fingerprint": snapshot.get("content_fingerprint"),
            "content_fingerprint_status": snapshot.get("content_fingerprint_status"),
            "capture_quality": snapshot.get("capture_quality", "full_snapshot"),
            "snapshot_hash": snapshot.get("snapshot_hash"),
        }

    def _start(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.recorder is not None:
            raise RuntimeError("上一条意见还在录音，请先结束并保存")
        snapshot = payload.get("snapshot") or {}
        project, sequence, project_root = self._project_context(snapshot)
        mode = payload.get("mode")
        if mode not in {"point", "range"}:
            raise ValueError("当前版本只支持单点意见和区间意见")
        session_id = _safe_id(payload.get("session_id") or new_id(), "session_id")
        note_id = _safe_id(payload.get("note_id") or new_id(), "note_id")
        revision_id = new_id()
        session_root = self._session_root(project_root, session_id)
        self._ensure_session(session_root, session_id, project)
        note_dir = session_root / "notes" / note_id
        note_dir.mkdir(parents=True, exist_ok=False)
        operation_id = _safe_id(payload.get("operation_id") or new_id(), "operation_id")
        anchor = self._anchor(snapshot, "start" if mode == "range" else "point", operation_id)
        snapshot_ref = _save_snapshot(note_dir, "start", snapshot)
        anchor["snapshot_ref"] = snapshot_ref
        if str(anchor["project_path"]).casefold() != str(project["path"]).casefold():
            raise ValueError("工程位置在宿主读取期间发生变化")
        if mode == "range":
            location = {"coordinate_system": "sequence_ticks", "range_method": "drag_endpoints", "range_status": "awaiting_end", "endpoints": {"start": anchor}}
            anchor_type = "range_start"
        else:
            location = {"coordinate_system": "sequence_ticks", "range_status": "not_applicable", "point": anchor}
            anchor_type = "point_locked"
        now = utc_now()
        note = {
            "schema_version": "1.0",
            "session_id": session_id,
            "note_id": note_id,
            "revision_id": revision_id,
            "created_at": now,
            "updated_at": now,
            "mode": mode,
            "project": project,
            "sequence": sequence,
            "location": location,
            "snapshots": {"start": snapshot_ref, "end": None},
            "audio": {"path": "audio.wav", "raw_path": "audio.capture.pcm"},
            "transcript": {"state": "not_started", "text": None, "edited_text": None},
            "status": {"recording": "preparing", "editing": "unprocessed"},
            "events": [{"event_id": anchor["event_id"], "kind": anchor_type, "anchor": anchor, "button_clicked_at": payload.get("clicked_at"), "worker_received_at": now, "worker_monotonic_ns": time.monotonic_ns(), "created_at": now}],
        }
        note_path = note_dir / "note.json"
        atomic_write_json(note_path, note)
        session = read_json(session_root / "session.json")
        session.setdefault("notes", []).append(note_id)
        atomic_write_json(session_root / "session.json", session)
        device = payload.get("device")
        if not isinstance(device, dict):
            note["status"]["recording"] = "failed"
            note["status"]["error"] = "没有选择麦克风"
            atomic_write_json(note_path, note)
            raise ValueError("请先选择麦克风")
        recovery_pointer = self.ipc_root / "recover" / f"{note_id}.json"
        recovery_pointer.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(recovery_pointer, {"note_path": str(note_path), "note_id": note_id, "created_at": utc_now()})
        recorder = MicrophoneRecorder(note_dir, device)
        try:
            capture = recorder.start()
        except Exception as exc:
            note["status"]["recording"] = "failed"
            note["status"]["error"] = f"{type(exc).__name__}: {exc}"
            note["updated_at"] = utc_now()
            atomic_write_json(note_path, note)
            recovery_pointer.unlink(missing_ok=True)
            raise
        self.recorder = recorder
        self.active_note_path = note_path
        self.active_session_root = session_root
        self.active_note_id = note_id
        note["status"]["recording"] = "recording"
        note["audio"].update(capture)
        note["audio"]["path"] = "audio.wav"
        note["audio"]["raw_path"] = "audio.capture.pcm"
        note["updated_at"] = utc_now()
        try:
            atomic_write_json(note_path, note)
        except Exception:
            self._interrupt_active("意见记录落盘失败")
            raise
        self.last_heartbeat = time.monotonic()
        return {"note_id": note_id, "session_id": session_id, "revision_id": revision_id, "status": note["status"], "location": note["location"], "audio": capture, "note_path": str(note_path)}

    def _ptt_commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        capture_id = _safe_id(payload.get("capture_id"), "capture_id")
        capture = self.ptt.lookup(capture_id)
        if not capture or capture.get("id") != capture_id or capture.get("state") != "complete":
            raise RuntimeError("这段按键录音尚未结束或编号已变化")
        snapshot = payload.get("snapshot") or {}
        project, sequence, project_root = self._project_context(snapshot)
        if str(project["path"]).casefold() != str(capture.get("project_path", "")).casefold():
            raise ValueError("按键录音期间 Premiere 工程发生变化；原始录音已保留")
        session_id = _safe_id(capture.get("session_id"), "session_id")
        note_id = capture_id
        capture_dir = (self.ipc_root / "ptt" / capture_id).resolve()
        if not capture_dir.is_relative_to((self.ipc_root / "ptt").resolve()):
            raise ValueError("录音目录无效")
        source_wav = capture_dir / "audio.wav"
        if not source_wav.is_file() or not capture.get("audio", {}).get("sha256"):
            raise ValueError("按键录音文件未完成；原始采集仍保留")
        if sha256_file(source_wav) != capture["audio"]["sha256"]:
            raise ValueError("按键录音文件校验失败；未关联到时间线")

        session_root = self._session_root(project_root, session_id)
        self._ensure_session(session_root, session_id, project)
        note_dir = session_root / "notes" / note_id
        note_path = note_dir / "note.json"
        if note_path.is_file():
            existing = read_json(note_path)
            if existing.get("audio", {}).get("source_ptt_capture_id") != capture_id:
                raise FileExistsError("意见编号已被其他录音占用")
            session = read_json(session_root / "session.json")
            if note_id not in session.setdefault("notes", []):
                session["notes"].append(note_id)
                atomic_write_json(session_root / "session.json", session)
            self.ptt.acknowledge(capture_id)
            return {"note_id": note_id, "note_path": str(note_path), "mode": existing["mode"], "location": existing["location"]}

        inout = snapshot.get("in_out") or {}
        selection = snapshot.get("selection") or []
        spans = []
        for clip in selection:
            try:
                start, end = int(clip["start_ticks"]), int(clip["end_ticks"])
                if end > start:
                    spans.append((start, end))
            except (KeyError, TypeError, ValueError):
                pass
        range_method = None
        if spans:
            start_tick, end_tick = min(x[0] for x in spans), max(x[1] for x in spans)
            range_method = "selected_clips"
        else:
            try:
                start_tick, end_tick = int(inout["in_ticks"]), int(inout["out_ticks"])
                sequence_end = int(sequence.get("end_ticks", "0"))
                if end_tick > start_tick and (start_tick > 0 or (sequence_end > 0 and end_tick < sequence_end)):
                    range_method = "in_out"
            except (KeyError, TypeError, ValueError):
                pass
        end_snapshot = payload.get("end_snapshot")
        if range_method is None and isinstance(end_snapshot, dict):
            try:
                start_tick = int(snapshot["location"]["ticks"])
                end_tick = int(end_snapshot["location"]["ticks"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("拖动区间缺少有效的起点或终点；原始录音仍保留") from exc
            if start_tick != end_tick:
                range_method = "drag_playhead"
        snapshot_ref = None
        note_dir.mkdir(parents=True, exist_ok=True)
        snapshot_ref = _save_snapshot(note_dir, "press", snapshot)
        end_snapshot_ref = None
        operation_id = new_id()
        if range_method:
            start = self._anchor(snapshot, "ptt_range_start", operation_id)
            if range_method == "drag_playhead":
                end = self._anchor(end_snapshot, "ptt_range_end", operation_id)
                okay, reason = _same_context(start, end)
                if not okay:
                    raise ValueError("拖动区间的起点和终点不属于同一时间线（" + reason + "）；原始录音仍保留")
                end_snapshot_ref = _save_snapshot(note_dir, "release", end_snapshot)
                start["snapshot_ref"] = snapshot_ref
                end["snapshot_ref"] = end_snapshot_ref
                version_status = "partial" if reason == "content_fingerprint_partial" else "matched"
            else:
                end = self._anchor(snapshot, "ptt_range_end", operation_id)
                for anchor, ticks in ((start, start_tick), (end, end_tick)):
                    anchor["ticks"] = str(ticks)
                    anchor["time_label"] = _elapsed_label(ticks)
                    anchor["snapshot_ref"] = snapshot_ref
                end_snapshot_ref = snapshot_ref
                version_status = "partial"
            interval = normalize_interval(start["ticks"], end["ticks"])
            if not interval["valid"]:
                raise ValueError("选中范围无效；原始录音仍保留")
            mode = "range"
            location = {
                "coordinate_system": "sequence_ticks", "range_method": range_method,
                "range_status": "locked", "endpoints": {"start": start, "end": end},
                "normalized": interval, "version_status": version_status, "automatic_edit_allowed": False,
            }
            anchor_events = [start, end]
        else:
            point = self._anchor(snapshot, "ptt_point", operation_id)
            point["snapshot_ref"] = snapshot_ref
            mode = "point"
            location = {"coordinate_system": "sequence_ticks", "range_status": "not_applicable", "point": point}
            anchor_events = [point]

        for name in ("audio.wav", "audio.capture.pcm", "capture.json"):
            source = capture_dir / name
            if not source.is_file():
                continue
            partial = note_dir / (name + ".copying")
            shutil.copy2(source, partial)
            os.replace(partial, note_dir / name)
            if sha256_file(source) != sha256_file(note_dir / name):
                raise OSError("录音复制校验失败；原始录音仍保留")
        now = utc_now()
        audio = dict(capture["audio"])
        audio["source_ptt_capture_id"] = capture_id
        note = {
            "schema_version": "1.0", "session_id": session_id, "note_id": note_id,
            "revision_id": new_id(), "created_at": now, "updated_at": now,
            "mode": mode, "project": project, "sequence": sequence, "location": location,
            "snapshots": {"start": snapshot_ref, "end": end_snapshot_ref},
            "audio": audio, "transcript": {"state": "not_started", "text": None, "edited_text": None},
            "status": {"recording": "saved", "editing": "unprocessed"},
            "events": [
                {"event_id": anchor["event_id"], "kind": anchor["kind"], "anchor": anchor, "created_at": now}
                for anchor in anchor_events
            ] + [{"event_id": new_id(), "kind": "push_to_talk", "hotkey": capture["hotkey"],
                  "pressed_at": capture["pressed_at"], "released_at": capture.get("released_at"),
                  "capture_id": capture_id, "created_at": now}],
        }
        errors = validate_note(note, require_audio=True)
        if errors:
            raise ValueError("意见记录校验失败：" + "; ".join(errors))
        atomic_write_json(note_path, note)
        session = read_json(session_root / "session.json")
        if note_id not in session.setdefault("notes", []):
            session["notes"].append(note_id)
            atomic_write_json(session_root / "session.json", session)
        self.ptt.acknowledge(capture_id)
        return {"note_id": note_id, "note_path": str(note_path), "mode": mode, "location": location}

    def _active_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        note_id = _safe_id(payload.get("note_id"), "note_id")
        if self.active_note_path is None or self.active_note_id != note_id:
            raise RuntimeError("这条意见当前没有在录音")
        return read_json(self.active_note_path)

    def _lock_end(self, payload: dict[str, Any]) -> dict[str, Any]:
        note = self._active_note(payload)
        if note["mode"] != "range":
            raise ValueError("单点意见不需要锁定终点")
        if note["location"].get("range_status") != "awaiting_end":
            raise RuntimeError("当前区间不在等待终点状态")
        snapshot = payload.get("snapshot") or {}
        operation_id = _safe_id(payload.get("operation_id") or new_id(), "operation_id")
        end = self._anchor(snapshot, "lock_end", operation_id)
        end_snapshot_ref = _save_snapshot(self.active_note_path.parent, "end", snapshot)
        end["snapshot_ref"] = end_snapshot_ref
        end["worker_audio_samples_at_receipt"] = self.recorder.samples if self.recorder else None
        end["worker_received_at"] = utc_now()
        end["worker_monotonic_ns"] = time.monotonic_ns()
        start = note["location"]["endpoints"]["start"]
        okay, reason = _same_context(start, end)
        if not okay:
            note["location"]["range_status"] = "stale"
            note["location"]["stale_reason"] = reason
            note["events"].append({"event_id": end["event_id"], "kind": "rejected_end", "anchor": end, "reason": reason, "created_at": utc_now()})
        else:
            interval = normalize_interval(start["ticks"], end["ticks"])
            if not interval["valid"]:
                note["location"]["range_status"] = "needs_endpoint"
                note["location"]["stale_reason"] = "zero_length"
                note["events"].append({"event_id": end["event_id"], "kind": "rejected_end", "anchor": end, "reason": "zero_length", "created_at": utc_now()})
            else:
                note["location"]["endpoints"]["end"] = end
                note["snapshots"]["end"] = end_snapshot_ref
                note["location"]["normalized"] = interval
                note["location"]["range_status"] = "locked"
                note["location"]["version_status"] = "partial" if reason == "content_fingerprint_partial" else "matched"
                note["location"]["automatic_edit_allowed"] = reason != "content_fingerprint_partial"
                note["events"].append({"event_id": end["event_id"], "kind": "end_locked", "anchor": end, "button_clicked_at": payload.get("clicked_at"), "created_at": utc_now()})
        saved = update_note(self.active_note_path, location=note["location"], events=note["events"])
        return {"note_id": note["note_id"], "location": saved["location"]}

    def _reselect_end(self, payload: dict[str, Any]) -> dict[str, Any]:
        note = self._active_note(payload)
        if note["mode"] != "range" or note["location"].get("range_status") not in {"locked", "needs_endpoint"}:
            raise RuntimeError("当前区间不能重选终点")
        current = note["location"]["endpoints"].get("end")
        operation_id = _safe_id(payload.get("operation_id") or new_id(), "operation_id")
        if current:
            note["events"].append({"event_id": new_id(), "kind": "end_superseded", "anchor_event_id": current.get("event_id"), "operation_id": operation_id, "button_clicked_at": payload.get("clicked_at"), "created_at": utc_now()})
        note["location"]["endpoints"].pop("end", None)
        note["location"].pop("normalized", None)
        note["location"].pop("version_status", None)
        note["location"].pop("automatic_edit_allowed", None)
        note["location"]["range_status"] = "awaiting_end"
        note["location"]["pending_end_operation_id"] = operation_id
        saved = update_note(self.active_note_path, location=note["location"], events=note["events"])
        return {"note_id": note["note_id"], "location": saved["location"]}

    def _stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        note = self._active_note(payload)
        stop_operation_id = _safe_id(payload.get("operation_id") or new_id(), "operation_id")
        if self.recorder is None or self.active_note_path is None:
            raise RuntimeError("录音助手状态不一致")
        if note["mode"] == "range" and note["location"].get("range_status") == "awaiting_end":
            note["location"]["range_status"] = "needs_endpoint"
            note["location"]["pending_end_operation_id"] = stop_operation_id
        try:
            audio = self.recorder.stop(status="saved")
            if audio.get("capture_status") == "saved" and audio.get("path") and audio.get("sha256"):
                recording_status = "saved"
            else:
                recording_status = "interrupted" if audio.get("samples_input", 0) else "failed"
        except Exception as exc:
            audio = self.recorder.summary()
            audio["capture_error"] = f"{type(exc).__name__}: {exc}"
            recording_status = "interrupted" if audio.get("samples_input", 0) else "failed"
        note["audio"].update(audio)
        note["status"]["recording"] = recording_status
        note["status"]["stop_operation_id"] = stop_operation_id
        note["events"].append({
            "event_id": new_id(),
            "kind": "audio_stopped",
            "operation_id": stop_operation_id,
            "button_clicked_at": payload.get("clicked_at"),
            "worker_monotonic_ns": time.monotonic_ns(),
            "worker_recording_elapsed_seconds": round(time.monotonic() - self.recorder.started_monotonic, 6),
            "audio_samples_input": audio.get("samples_input"),
            "recording_status": recording_status,
            "created_at": utc_now(),
        })
        if note["mode"] == "range" and note["location"].get("range_status") == "locked":
            note["location"].pop("pending_end_operation_id", None)
        note["updated_at"] = utc_now()
        errors = validate_note(note, require_audio=recording_status == "saved")
        if errors and recording_status == "saved":
            note["status"]["recording"] = "interrupted"
            note["status"]["validation_errors"] = errors
        atomic_write_json(self.active_note_path, note)
        result = {"note_id": note["note_id"], "note_path": str(self.active_note_path), "status": note["status"], "location": note["location"], "audio": audio}
        self.recorder = None
        self.active_note_id = None
        self.active_note_path = None
        self.active_session_root = None
        (self.ipc_root / "recover" / f"{note['note_id']}.json").unlink(missing_ok=True)
        return result

    def _attach_endpoint(self, payload: dict[str, Any]) -> dict[str, Any]:
        note_id = _safe_id(payload.get("note_id"), "note_id")
        note_path = Path(str(payload.get("note_path", ""))).resolve()
        project_path = Path(str((payload.get("snapshot") or {}).get("project", {}).get("path", "")))
        if not note_path.is_file() or note_path.name != "note.json":
            raise ValueError("意见记录路径无效")
        note = read_json(note_path)
        if note.get("note_id") != note_id or note.get("mode") != "range":
            raise ValueError("意见编号与记录不匹配")
        expected_path = (
            Path(note.get("project", {}).get("path", "")).resolve().parent
            / "work" / "premiere-review" / note.get("session_id", "")
            / "notes" / note_id / "note.json"
        ).resolve()
        if note_path != expected_path:
            raise ValueError("意见记录不在该 Premiere 工程的审片工作目录中")
        expected_op = note.get("location", {}).get("pending_end_operation_id")
        if not expected_op or expected_op != payload.get("operation_id"):
            raise RuntimeError("终点请求已过期或已被替换")
        snapshot = payload.get("snapshot") or {}
        project, _sequence, _root = self._project_context(snapshot)
        if str(project.get("path", "")).casefold() != str(note.get("project", {}).get("path", "")).casefold() or str(project_path).casefold() != str(project["path"]).casefold():
            raise ValueError("补充终点时工程已改变")
        end = self._anchor(snapshot, "stop_recording", expected_op)
        end_snapshot_ref = _save_snapshot(note_path.parent, "end", snapshot)
        end["snapshot_ref"] = end_snapshot_ref
        start = note["location"].get("endpoints", {}).get("start")
        if not isinstance(start, dict):
            raise ValueError("意见记录缺少起点")
        okay, reason = _same_context(start, end)
        interval = normalize_interval(start["ticks"], end["ticks"]) if okay else {"valid": False}
        if not okay or not interval["valid"]:
            note["location"]["range_status"] = "stale" if not okay else "needs_endpoint"
            note["location"]["stale_reason"] = reason if not okay else "zero_length"
            note["events"].append({"event_id": end["event_id"], "kind": "rejected_end", "anchor": end, "reason": note["location"]["stale_reason"], "created_at": utc_now()})
        else:
            note["location"]["endpoints"]["end"] = end
            note["snapshots"]["end"] = end_snapshot_ref
            note["location"]["normalized"] = interval
            note["location"]["range_status"] = "locked"
            note["location"]["version_status"] = "partial" if reason == "content_fingerprint_partial" else "matched"
            note["location"]["automatic_edit_allowed"] = reason != "content_fingerprint_partial"
            note["location"].pop("pending_end_operation_id", None)
            note["events"].append({"event_id": end["event_id"], "kind": "end_locked_at_stop", "anchor": end, "button_clicked_at": payload.get("clicked_at"), "created_at": utc_now()})
        saved = update_note(note_path, location=note["location"], events=note["events"])
        return {"note_id": note_id, "location": saved["location"], "status": saved["status"]}

    def _cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        note = self._active_note(payload)
        if self.recorder is None:
            raise RuntimeError("没有正在录制的意见")
        try:
            audio = self.recorder.stop(status="interrupted")
        except Exception as exc:
            audio = self.recorder.summary()
            audio["capture_error"] = f"{type(exc).__name__}: {exc}"
        note["audio"].update(audio)
        note["status"]["recording"] = "interrupted" if audio.get("samples_input", 0) else "cancelled"
        note["status"]["editing"] = "cancelled"
        note["updated_at"] = utc_now()
        atomic_write_json(self.active_note_path, note)
        note_id = self.active_note_id
        self.recorder = None
        self.active_note_id = None
        self.active_note_path = None
        self.active_session_root = None
        (self.ipc_root / "recover" / f"{note_id}.json").unlink(missing_ok=True)
        return {"note_id": note_id, "status": note["status"], "audio": audio}

    def _list_notes(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_path = Path(str(payload.get("project_path", "")))
        session_id = _safe_id(payload.get("session_id"), "session_id")
        if not project_path.is_absolute():
            raise ValueError("工程目录路径必须是绝对路径")
        session_root = self._session_root(project_path.parent, session_id)
        files = (session_root / "notes").glob("*/note.json") if session_root.exists() else []
        notes = [read_json(p) for p in files]
        notes.sort(key=lambda note: (str(note.get("created_at", "")), str(note.get("note_id", ""))))
        return {"session_id": session_id, "notes": notes}

    def _correct_transcript(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_path = Path(str(payload.get("project_path", "")))
        session_id = _safe_id(payload.get("session_id"), "session_id")
        note_id = _safe_id(payload.get("note_id"), "note_id")
        if not project_path.is_absolute():
            raise ValueError("工程路径必须是绝对路径")
        session_root = self._session_root(project_path.parent, session_id)
        note_path = (session_root / "notes" / note_id / "note.json").resolve()
        if not note_path.is_relative_to((session_root / "notes").resolve()) or not note_path.is_file():
            raise ValueError("找不到这条已保存的意见记录")
        expected_revision = payload.get("expected_transcript_revision")
        if expected_revision is not None:
            try:
                expected_revision = int(expected_revision)
            except (TypeError, ValueError) as exc:
                raise ValueError("转写版本号无效；请刷新意见清单后重试") from exc
        from services.review_transcription import correct_transcript

        return correct_transcript(
            note_path,
            str(payload.get("text", "")),
            expected_transcript_revision=expected_revision,
            editor=str(payload.get("editor") or "Premiere 审片面板"),
        )

    def _revalidate_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("Premiere 没有提供当前时间线快照")
        project, sequence, project_root = self._project_context(snapshot)
        requested_project = Path(str(payload.get("project_path", ""))).resolve()
        if requested_project != Path(project["path"]).resolve():
            raise ValueError("传入工程与 Premiere 当前工程不一致")
        session_id = _safe_id(payload.get("session_id"), "session_id")
        session_root = self._session_root(project_root, session_id)
        session_path = session_root / "session.json"
        if not session_path.is_file():
            raise ValueError("本批意见记录不存在")
        session = read_json(session_path)
        if session.get("session_id") != session_id or str(session.get("project", {}).get("path", "")).casefold() != project["path"].casefold():
            raise ValueError("当前意见批次不属于 Premiere 打开的工程")

        handoff_path = project_root / "work" / "premiere-review" / "CURRENT_REVIEW_BATCH.json"
        if not handoff_path.is_file():
            raise ValueError("请先发送本批意见，再做时间线核对")
        handoff = read_json(handoff_path)
        if handoff.get("session_id") != session_id:
            raise ValueError("当前交接入口指向另一批意见；请先发送本批")
        bundle_path = Path(str(handoff.get("bundle_path", ""))).resolve()
        manifest_path = Path(str(handoff.get("manifest_path", ""))).resolve()
        manifest_hash = str(handoff.get("manifest_sha256", ""))
        output_root = (project_root / "output").resolve()
        if not bundle_path.is_dir() or not bundle_path.is_relative_to(output_root):
            raise ValueError("当前意见包目录不存在或不在项目 output 目录中")
        if not manifest_path.is_relative_to(bundle_path) or not manifest_path.is_file() or sha256_file(manifest_path).lower() != manifest_hash.lower():
            raise ValueError("当前意见包 manifest 不存在或哈希不匹配")
        manifest = read_json(manifest_path)
        manifest_notes = manifest.get("notes")
        if manifest.get("session_id") != session_id or not isinstance(manifest_notes, list):
            raise ValueError("意见包清单与当前批次不一致")

        # Compare the live work session with the exact version that was exported.
        # A text correction or a newly captured note requires re-packaging first.
        packaged_by_id = {str(item.get("note_id")): item for item in manifest_notes if isinstance(item, dict)}
        session_note_ids = {str(item) for item in session.get("notes", [])}
        if session_note_ids != set(packaged_by_id):
            raise ValueError("本批意见在发送后有新增或缺失；请重新发送本批意见")
        packaged_notes: list[dict[str, Any]] = []
        for note_id, manifest_item in packaged_by_id.items():
            relative = str(manifest_item.get("note_path", "")).replace("\\", os.sep).replace("/", os.sep)
            note_path = (bundle_path / relative).resolve()
            if not note_path.is_relative_to(bundle_path) or not note_path.is_file():
                raise ValueError(f"意见包中的记录不存在或路径越界：{note_id}")
            if sha256_file(note_path).lower() != str(manifest_item.get("note_sha256", "")).lower():
                raise ValueError(f"意见包中的记录哈希不匹配：{note_id}")
            note = read_json(note_path)
            if note.get("note_id") != note_id or note.get("revision_id") != manifest_item.get("revision_id"):
                raise ValueError(f"意见包中的记录编号或修订号不匹配：{note_id}")
            live_path = (session_root / "notes" / note_id / "note.json").resolve()
            if not live_path.is_file() or sha256_file(live_path).lower() != str(manifest_item.get("note_sha256", "")).lower():
                raise ValueError(f"意见 {note_id} 在发送后有修改；请重新发送本批意见再核对")
            packaged_notes.append(note)

        sequence_id = str(sequence.get("host_id") or "")
        timebase = str(sequence.get("timebase") or "")
        current_fingerprint = snapshot.get("content_fingerprint")
        current_fingerprint_status = snapshot.get("content_fingerprint_status")
        note_results: list[dict[str, Any]] = []
        for note in packaged_notes:
            location = note.get("location") if isinstance(note.get("location"), dict) else {}
            if note.get("mode") == "range":
                endpoints = location.get("endpoints") if isinstance(location.get("endpoints"), dict) else {}
                anchors = [endpoints.get("start"), endpoints.get("end")]
            else:
                anchors = [location.get("point")]
            reasons: list[str] = []
            baseline_fingerprints: list[str] = []
            if note.get("status", {}).get("recording") not in {"saved", "interrupted"}:
                reasons.append("录音尚未保存")
            if not anchors or any(not isinstance(anchor, dict) for anchor in anchors):
                reasons.append("意见缺少完整定位锚点")
            for anchor in anchors:
                if not isinstance(anchor, dict):
                    continue
                baseline_fingerprints.append(str(anchor.get("content_fingerprint") or ""))
                if str(anchor.get("project_path", "")).casefold() != project["path"].casefold():
                    reasons.append("捕获工程与当前工程不同")
                if str(anchor.get("sequence_id") or "") != sequence_id:
                    reasons.append("意见捕获序列与当前序列不同")
                if str(anchor.get("timebase") or "") != timebase:
                    reasons.append("意见捕获时基与当前时基不同")
            if not current_fingerprint or current_fingerprint_status not in {"complete", "partial"}:
                reasons.append("当前时间线指纹缺失或不完整")
            if any(not value for value in baseline_fingerprints):
                reasons.append("捕获时缺少时间线指纹")
            if baseline_fingerprints and any(value != current_fingerprint for value in baseline_fingerprints):
                reasons.append("当前时间线片段结构与录意见时的快照不同")
            all_complete = current_fingerprint_status == "complete" and all(
                anchor.get("content_fingerprint_status") == "complete"
                for anchor in anchors if isinstance(anchor, dict)
            )
            if reasons:
                status = "changed_or_incomplete"
            elif all_complete:
                status = "matched"
            else:
                status = "manual_review_required"
                reasons.append("指纹一致，但宿主只读取到部分时间线字段；剪辑前需人工核对")
            note_results.append({
                "note_id": note.get("note_id"),
                "revision_id": note.get("revision_id"),
                "sequence_id": sequence_id,
                "baseline_fingerprints": baseline_fingerprints,
                "current_fingerprint": current_fingerprint,
                "current_fingerprint_status": current_fingerprint_status,
                "status": status,
                "reasons": reasons,
            })

        snapshot_dir = session_root / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot_path = snapshot_dir / f"revalidation-{stamp}.json"
        atomic_write_json(snapshot_path, snapshot)
        report_dir = session_root / "plans"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"timeline-check-{stamp}.json"
        report = {
            "schema_version": "1.0",
            "created_at": utc_now(),
            "session_id": session_id,
            "project_path": project["path"],
            "sequence_id": sequence_id,
            "sequence_name": sequence.get("name"),
            "manifest_sha256": manifest_hash,
            "manifest_path": str(manifest_path),
            "current_snapshot": {
                "path": str(snapshot_path.resolve()),
                "sha256": sha256_file(snapshot_path),
            },
            "execution_eligible": False,
            "notes": note_results,
            "summary": {
                "matched": sum(item["status"] == "matched" for item in note_results),
                "manual_review_required": sum(item["status"] == "manual_review_required" for item in note_results),
                "changed_or_incomplete": sum(item["status"] == "changed_or_incomplete" for item in note_results),
            },
        }
        atomic_write_json(report_path, report)
        return {"report_path": str(report_path), "summary": report["summary"], "execution_eligible": False}

    def _package(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_path = Path(str(payload.get("project_path", "")))
        session_id = _safe_id(payload.get("session_id"), "session_id")
        if not project_path.is_absolute():
            raise ValueError("工程目录路径必须是绝对路径")
        session_root = self._session_root(project_path.parent, session_id)
        output_root = Path(str(payload.get("output_root") or (project_path.parent / "output")))
        expected_output = (project_path.parent / "output").resolve()
        if output_root.resolve() != expected_output:
            raise ValueError("意见包只能写入当前工程的 output 目录")
        destination = package_session(session_root, output_root)
        manifest_path = destination / "manifest.json"
        manifest_hash = sha256_file(manifest_path)
        handoff_root = project_path.parent / "work" / "premiere-review"
        handoff_root.mkdir(parents=True, exist_ok=True)
        handoff_path = handoff_root / "CURRENT_REVIEW_BATCH.json"
        previous = read_json(handoff_path) if handoff_path.exists() else None
        history_root = handoff_root / "handoff-history"
        history_root.mkdir(parents=True, exist_ok=True)
        if previous:
            history_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{previous.get('session_id', 'previous')}.json"
            atomic_write_json(history_root / history_name, previous)
        pointer = {
            "schema_version": "1.0",
            "updated_at": utc_now(),
            "project_path": str(project_path.resolve()),
            "session_id": session_id,
            "bundle_path": str(destination.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "manifest_sha256": manifest_hash,
            "supersedes_session_id": previous.get("session_id") if previous else None,
        }
        atomic_write_json(handoff_path, pointer)
        result = {"session_id": session_id, "path": str(destination), "manifest": str(manifest_path),
                  "manifest_sha256": manifest_hash, "handoff_pointer": str(handoff_path)}
        try:
            manifest = read_json(manifest_path)
            rows = []
            for item in manifest.get("notes", []):
                note_path = (destination / str(item["note_path"])).resolve()
                if not note_path.is_relative_to(destination.resolve()) or sha256_file(note_path) != item["note_sha256"]:
                    raise ValueError("意见包中的 note.json 哈希不符")
                note = read_json(note_path)
                transcript = note.get("transcript") or {}
                text = transcript.get("edited_text") or transcript.get("text") or ""
                try:
                    intent = parse_instruction(text, note) if text else {"action": "manual_review", "reason": "尚未转写"}
                except ValueError as exc:
                    intent = {"action": "manual_review", "reason": str(exc)}
                rows.append({"note_id": item["note_id"], "note_path": str(note_path),
                             "note_sha256": item["note_sha256"], "text": text,
                             "suggested_action": intent["action"], "intent": intent,
                             "status": "PENDING_TIMELINE_DRY_RUN"})
            intake_dir = handoff_root / "decisions" / session_id
            intake_dir.mkdir(parents=True, exist_ok=True)
            intake_path = intake_dir / ("intake-" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")
            atomic_write_json(intake_path, {"schema_version": "1.0", "created_at": utc_now(),
                                            "manifest_path": str(manifest_path), "manifest_sha256": manifest_hash,
                                            "notes": rows})
            result["decision_intake"] = str(intake_path)
        except Exception as exc:
            logging.exception("F8 意见预解析失败，原意见包已保存")
            result["decision_intake_error"] = str(exc)
        return result

    def _interrupt_active(self, reason: str) -> None:
        if self.recorder is None or self.active_note_path is None:
            return
        try:
            note = read_json(self.active_note_path)
            try:
                audio = self.recorder.stop(status="interrupted")
            except Exception as exc:
                audio = self.recorder.summary()
                audio["capture_error"] = f"{type(exc).__name__}: {exc}"
            note["audio"].update(audio)
            note["status"]["recording"] = "interrupted" if audio.get("samples_input", 0) else "failed"
            note["status"]["error"] = reason
            if note.get("mode") == "range" and note.get("location", {}).get("range_status") == "awaiting_end":
                note["location"]["range_status"] = "needs_endpoint"
            note["updated_at"] = utc_now()
            atomic_write_json(self.active_note_path, note)
            (self.ipc_root / "recover" / f"{note['note_id']}.json").unlink(missing_ok=True)
        finally:
            self.recorder = None
            self.active_note_id = None
            self.active_note_path = None
            self.active_session_root = None

    def _recover_stale_recording(self) -> None:
        """Mark orphaned session notes after a prior worker process died."""
        # The project-specific recovery pass is driven by recovery.json files written
        # when a note starts. It never guesses an endpoint from a new Premiere state.
        recovery_root = self.ipc_root / "recover"
        recovery_root.mkdir(exist_ok=True)
        for pointer in recovery_root.glob("*.json"):
            try:
                item = read_json(pointer)
                note_path = Path(str(item.get("note_path", "")))
                if not note_path.is_file():
                    pointer.unlink(missing_ok=True)
                    continue
                note = read_json(note_path)
                state = note.get("status", {}).get("recording")
                if state in {"recording", "preparing"}:
                    capture_path = note_path.parent / "capture.json"
                    raw_path = note_path.parent / "audio.capture.pcm"
                    rate = 0
                    if capture_path.is_file():
                        rate = int(read_json(capture_path).get("input_rate", 0))
                    if raw_path.is_file() and raw_path.stat().st_size and rate:
                        try:
                            note["audio"].update(recover_interrupted_capture(note_path.parent, rate))
                        except Exception as exc:
                            note["audio"]["recovery_error"] = f"{type(exc).__name__}: {exc}"
                        note["status"]["recording"] = "interrupted"
                        note["status"]["error"] = "录音助手异常退出；已恢复可读音频，尾部可能不完整"
                    else:
                        note["status"]["recording"] = "failed"
                        note["status"]["error"] = "录音助手异常退出；没有可恢复的完整音频块"
                    if note.get("mode") == "range" and note.get("location", {}).get("range_status") == "awaiting_end":
                        note["location"]["range_status"] = "needs_endpoint"
                    note["updated_at"] = utc_now()
                    atomic_write_json(note_path, note)
                pointer.unlink(missing_ok=True)
            except Exception:
                continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cuthere Premiere review capture worker")
    parser.add_argument("--ipc-root", required=True, type=Path)
    parser.add_argument("--once", action="store_true", help="process the current inbox once; used by acceptance harness")
    args = parser.parse_args(argv)
    try:
        args.ipc_root.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=str(args.ipc_root / "worker.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            encoding="utf-8",
        )
        logging.info("worker start version=%s python=%s", WORKER_VERSION, sys.version.split()[0])
        worker = ReviewCaptureWorker(args.ipc_root)
        result = worker.run(once=args.once)
        logging.info("worker exit code=%s", result)
        return result
    except Exception as exc:
        logging.exception("review worker failed")
        print(f"review worker failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
