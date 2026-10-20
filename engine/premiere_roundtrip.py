"""Publish generated timelines through CEP, verify media/timing, restore checkpoints.

Original project files and media are read-only. All mutations target a new
working project. The job contains decisions, hashes, host responses and exports.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from engine.premiere_bridge import BridgeResponse, PremiereBridgeClient


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _path(value: str) -> str:
    value = unquote(value).replace("\\", "/")
    for prefix in ("file://localhost/", "file:///"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    return value.casefold()


def snapshots(xml: Path) -> list[dict[str, Any]]:
    root = ET.parse(xml).getroot()
    files = {f.get("id"): f.findtext("pathurl") for f in root.iter("file") if f.findtext("pathurl")}
    result = []
    for seq in root.iter("sequence"):
        if seq.find("media") is None:
            continue
        tracks = []
        for kind in ("video", "audio"):
            for i, track in enumerate(seq.findall(f"media/{kind}/track")):
                clips = []
                for clip in track.findall("clipitem"):
                    file = clip.find("file")
                    url = files.get(file.get("id"), "") if file is not None else ""
                    clips.append([clip.findtext("name"), *(int(clip.findtext(k, "-1")) for k in ("start", "end", "in", "out")),
                                  clip.findtext("enabled", "TRUE"), _path(url)])
                tracks.append({"kind": kind, "index": i, "enabled": track.findtext("enabled", "TRUE"),
                               "clips": clips, "transitions": len(track.findall("transitionitem"))})
        result.append({"name": seq.findtext("name"), "frames": int(seq.findtext("duration", "-1")),
                       "fps": seq.findtext("rate/timebase"), "ntsc": seq.findtext("rate/ntsc"), "tracks": tracks})
    return result


def select(snapshots_: list[dict], name: str) -> dict:
    matches = [s for s in snapshots_ if s["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"序列名必须唯一：{name}（{len(matches)} 个）")
    return matches[0]


def inventory(values: list[dict]) -> Counter:
    return Counter(json.dumps(v, ensure_ascii=False, sort_keys=True) for v in values)


def active_matches(expected: dict, active: dict) -> bool:
    rate = float(expected["fps"]) / (1.001 if expected["ntsc"] == "TRUE" else 1)
    tracks = {(t["kind"], t["index"]): t for t in active["tracks"]}
    return (abs(active["frame_rate"] - rate) < .001
            and active["total_clip_count"] == sum(len(t["clips"]) for t in expected["tracks"])
            and len(tracks) == len(expected["tracks"])
            and all((t["kind"], t["index"]) in tracks
                    and len(tracks[t["kind"], t["index"]]["clips"]) == len(t["clips"])
                    and tracks[t["kind"], t["index"]]["muted"] == (t["enabled"] == "FALSE")
                    for t in expected["tracks"]))


class PremiereRoundTrip:
    def __init__(self, bridge: PremiereBridgeClient | None = None):
        self.bridge = bridge or PremiereBridgeClient()

    def _call(self, job: Path, label: str, response: BridgeResponse) -> Any:
        _write(job / f"{label}.json", asdict(response))
        if not response.success:
            raise RuntimeError(f"{label}: {response.error}")
        return response.data

    def publish(self, xml: Path, sequence_name: str, expected_project: Path, working_project: Path,
                job: Path, position_ticks: str = "0") -> dict:
        xml, expected_project, working_project, job = (p.resolve() for p in (xml, expected_project, working_project, job))
        if job.exists() or working_project.exists():
            raise FileExistsError("任务目录和新工程路径必须尚不存在")
        expected = select(snapshots(xml), sequence_name)
        if len(snapshots(xml)) != 1:
            raise ValueError("导入文件只能包含一条完整时间线")
        # Stop before any mutation if source media is unavailable.
        for track in expected["tracks"]:
            for clip in track["clips"]:
                if not clip[-1] or not Path(clip[-1]).is_file():
                    raise FileNotFoundError(f"素材不可用：{clip[-1]}")
        original_hash = digest_file(expected_project)
        job.mkdir(parents=True); working_project.parent.mkdir(parents=True, exist_ok=True)
        state = {"status": "PREFLIGHT", "xml": str(xml), "xml_sha256": digest_file(xml),
                 "sequence_name": sequence_name, "source_project": str(expected_project),
                 "source_project_sha256": original_hash, "working_project": str(working_project),
                 "operation_id": uuid.uuid4().hex, "position_ticks": position_ticks}
        _write(job / "decision_dry_run.json", {**state, "expected_timeline": expected})
        _write(job / "state.json", state)
        checkpoint_created = False
        try:
            info = self._call(job, "01_project_before", self.bridge.get_project_info())
            if _path(info["path"]) != _path(str(expected_project)):
                raise ValueError("当前 Premiere 工程与指定基准不一致")
            if info.get("bridge_version") != "1.1.0":
                raise RuntimeError("请加载 1.1.0 版 Premiere 桥")
            self._call(job, "02_save_working_copy", self.bridge.save_project(str(expected_project), str(working_project)))
            self._call(job, "03_baseline_export", self.bridge.export_xml(str(job / "baseline.xml")))
            checkpoint = job / "checkpoint.prproj"
            self._call(job, "04_checkpoint", self.bridge.create_checkpoint(str(working_project), str(checkpoint)))
            state.update({"checkpoint": str(checkpoint), "checkpoint_sha256": digest_file(checkpoint), "status": "CHECKPOINTED"})
            _write(job / "state.json", state); checkpoint_created = True
            imported = self._call(job, "05_import", self.bridge.import_timeline(str(working_project), str(xml), sequence_name,
                                  str(job), state["operation_id"], state["xml_sha256"]))
            state["imported_sequence_id"] = imported["imported_sequence_id"]
            _write(job / "state.json", state)
            self._call(job, "06_activate", self.bridge.activate_sequence(str(working_project), state["imported_sequence_id"], position_ticks))
            self._call(job, "07_save", self.bridge.save_project(str(working_project)))
            self._call(job, "08_readback", self.bridge.export_xml(str(job / "readback.xml")))
            actual = snapshots(job / "readback.xml")
            actual_new = select(actual, sequence_name)
            old = [s for s in actual if s["name"] != sequence_name]
            checks = {"candidate_matches": expected == actual_new,
                      "existing_sequences_unchanged": inventory(snapshots(job / "baseline.xml")) == inventory(old),
                      "source_project_unchanged": digest_file(expected_project) == original_hash,
                      "candidate_file_unchanged": digest_file(xml) == state["xml_sha256"]}
            _write(job / "verification.json", checks)
            if not all(checks.values()):
                raise RuntimeError(f"Premiere 读回不符：{checks}")
            active = self._call(job, "09_active_sequence", self.bridge.get_sequence())
            if active["sequence_id"] != state["imported_sequence_id"]:
                raise RuntimeError("活动序列核对失败")
            checks["host_tracks_and_fps_match"] = active_matches(expected, active)
            checks["playhead_matches"] = int(active["player_position_ticks"]) == int(position_ticks)
            _write(job / "verification.json", checks)
            if not all(checks.values()):
                raise RuntimeError(f"Premiere 活动时间线不符：{checks}")
            state.update({"status": "PUBLISHED_VERIFIED", "project_sha256": digest_file(working_project),
                          "duration_frames": expected["frames"], "fps": expected["fps"],
                          "clip_count": sum(len(t["clips"]) for t in expected["tracks"])})
            _write(job / "state.json", state)
            return state
        except Exception as exc:
            state.update({"status": "FAILED", "error": str(exc)})
            _write(job / "state.json", state)
            if checkpoint_created:
                try:
                    self.rollback(job)
                except Exception as rollback_error:
                    state["rollback_error"] = str(rollback_error)
                    _write(job / "state.json", state)
            raise

    def verify(self, job: Path) -> dict:
        """Read the live project again; preserve each verification separately."""
        job = job.resolve()
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        restored = state["status"] == "ROLLED_BACK_VERIFIED"
        expected_project = state["restored_project"] if restored else state["working_project"]
        evidence = job / ("verify_" + uuid.uuid4().hex)
        evidence.mkdir()
        info = self._call(evidence, "01_project", self.bridge.get_project_info())
        if _path(info["path"]) != _path(expected_project):
            raise ValueError("读回工程与工作记录不一致")
        self._call(evidence, "02_export", self.bridge.export_xml(str(evidence / "readback.xml")))
        actual = snapshots(evidence / "readback.xml")
        if restored:
            checks = {"all_sequences_restored": inventory(actual) == inventory(snapshots(job / "baseline.xml")),
                      "checkpoint_unchanged": digest_file(Path(state["checkpoint"])) == state["checkpoint_sha256"]}
        else:
            if digest_file(Path(state["xml"])) != state["xml_sha256"]:
                raise ValueError("候选 XML 哈希不符")
            expected = select(snapshots(Path(state["xml"])), state["sequence_name"])
            active = self._call(evidence, "03_active_sequence", self.bridge.get_sequence())
            checks = {"candidate_matches": select(actual, state["sequence_name"]) == expected,
                      "existing_sequences_unchanged": inventory(snapshots(job / "baseline.xml")) == inventory([s for s in actual if s["name"] != state["sequence_name"]]),
                      "source_project_unchanged": digest_file(Path(state["source_project"])) == state["source_project_sha256"],
                      "active_sequence_matches": active["sequence_id"] == state["imported_sequence_id"],
                      "host_tracks_and_fps_match": active_matches(expected, active)}
        result = {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
                  "project_path": expected_project, "evidence": str(evidence)}
        _write(evidence / "verification.json", result)
        return result

    def rollback(self, job: Path) -> dict:
        job = job.resolve()
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        if state["status"] == "ROLLED_BACK_VERIFIED":
            return state
        checkpoint = Path(state["checkpoint"])
        if digest_file(checkpoint) != state["checkpoint_sha256"]:
            raise ValueError("恢复点工程哈希不符")
        recovery = job / "before_rollback.prproj"
        self._call(job, "rollback_01_restore", self.bridge.rollback_project(state["working_project"], str(checkpoint), str(recovery)))
        self._call(job, "rollback_02_readback", self.bridge.export_xml(str(job / "rollback_readback.xml")))
        checks = {"all_sequences_restored": inventory(snapshots(job / "baseline.xml")) == inventory(snapshots(job / "rollback_readback.xml")),
                  "checkpoint_unchanged": digest_file(checkpoint) == state["checkpoint_sha256"],
                  "new_version_preserved": recovery.is_file() and recovery.stat().st_size > 0}
        _write(job / "rollback_verification.json", checks)
        if not all(checks.values()):
            raise RuntimeError(f"回滚读回不符：{checks}")
        state.update({"status": "ROLLED_BACK_VERIFIED", "restored_project": str(checkpoint),
                      "recovery_project": str(recovery), "recovery_sha256": digest_file(recovery)})
        _write(job / "state.json", state)
        return state
