"""Frame-accurate, versioned podcast edit decisions for a Premiere FCP XML round trip.

The XML is an interchange snapshot, never the source of truth after import: a
Premiere export of the imported sequence must be compared before completion.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FPS = 25
TICKS_PER_FRAME = 10_160_640_000
TC = re.compile(r"(?<!\d)(\d{1,2}:\d{2}:\d{2}:\d{2})(?!\d)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frame_of(timecode: str, fps: int = FPS) -> int:
    parts = [int(part) for part in timecode.split(":")]
    if len(parts) != 4 or parts[1] >= 60 or parts[2] >= 60 or parts[3] >= fps:
        raise ValueError(f"无效时间码：{timecode}")
    return ((parts[0] * 60 + parts[1]) * 60 + parts[2]) * fps + parts[3]


def tc_of(frame: int, fps: int = FPS) -> str:
    seconds, ff = divmod(frame, fps)
    minutes, ss = divmod(seconds, 60)
    hh, mm = divmod(minutes, 60)
    return f"{hh:02}:{mm:02}:{ss:02}:{ff:02}"


def parse_instruction(text: str, note: dict[str, Any] | None = None) -> dict[str, Any]:
    """Conservative language gate: ambiguous speech is kept for review."""
    raw = text.strip()
    if not raw:
        raise ValueError("意见文字为空")
    if re.search(r"(读回|核验|检查.*结果|现在.*什么样)", raw):
        return {"action": "readback", "text": raw, "source": "f8" if note else "text"}
    if re.search(r"(回滚|撤销|恢复上次剪辑|还原上次修改)", raw):
        return {"action": "rollback", "text": raw, "source": "f8" if note else "text"}
    has_delete = bool(re.search(r"(删掉|删除|剪掉|去掉|移除|掐掉)", raw))
    times = TC.findall(raw)
    if has_delete and len(times) == 2:
        start, end = frame_of(times[0]), frame_of(times[1])
        if end <= start:
            raise ValueError("删除终点必须晚于起点")
        return {"action": "delete_range", "start_frame": start, "end_frame": end,
                "text": raw, "source": "f8" if note else "text"}
    if has_delete and note and note.get("mode") == "range":
        location = note.get("location") or {}
        norm = location.get("normalized") or {}
        if location.get("range_status") == "locked" and norm.get("start_ticks") and norm.get("end_ticks"):
            start, end = int(norm["start_ticks"]) // TICKS_PER_FRAME, int(norm["end_ticks"]) // TICKS_PER_FRAME
            if 0 < end - start <= 250 and int(norm["start_ticks"]) % TICKS_PER_FRAME == 0 and int(norm["end_ticks"]) % TICKS_PER_FRAME == 0:
                return {"action": "delete_range", "start_frame": start, "end_frame": end,
                        "text": raw, "source": "f8", "note_id": note.get("note_id")}
    return {"action": "manual_review", "text": raw, "source": "f8" if note else "text",
            "reason": "未给出明确的短区间删除指令；保留原意见，需结合画面、声音及当前时间线判断"}


def _sequence(root: ET.Element) -> ET.Element:
    sequences = list(root.iter("sequence"))
    if not sequences:
        raise ValueError("XML 没有 sequence")
    if len(sequences) != 1:
        raise ValueError("XML 包含多个或嵌套序列；请先用 select-sequence 提取唯一目标序列")
    sequence = sequences[0]
    if sequence.findtext("./rate/timebase") != "25" or sequence.findtext("./rate/ntsc") != "FALSE":
        raise ValueError("当前只支持 25fps 非 drop-frame 序列")
    return sequence


def _set(clip: ET.Element, name: str, value: int) -> None:
    item = clip.find(name)
    if item is None:
        raise ValueError(f"片段缺少 {name}")
    item.text = str(value)


def _bounds(clip: ET.Element) -> tuple[int, int, int, int]:
    values = tuple(int(clip.findtext(name, "-1")) for name in ("start", "end", "in", "out"))
    start, end, source_in, source_out = values
    if start < 0 or end <= start or source_out - source_in != end - start:
        raise ValueError(f"不支持变速或无效片段：{clip.get('id')}: {values}")
    return values


def _update(clip: ET.Element, start: int, end: int, source_in: int, source_out: int) -> None:
    for key, value in (("start", start), ("end", end), ("in", source_in), ("out", source_out)):
        _set(clip, key, value)
    for key, value in (("pproTicksIn", source_in), ("pproTicksOut", source_out)):
        node = clip.find(key)
        if node is not None:
            node.text = str(value * TICKS_PER_FRAME)


def timeline_digest(xml_path: Path) -> dict[str, Any]:
    sequence = _sequence(ET.parse(xml_path).getroot())
    # Premiere rewrites XML file IDs on import/export. Resolve each clip to its
    # media URL so readback cannot pass merely because a replacement has the
    # same clip name and timing.
    media_urls = {file.get("id"): file.findtext("pathurl", "")
                  for file in sequence.iter("file") if file.get("id") and file.findtext("pathurl")}
    tracks = []
    for kind in ("video", "audio"):
        for index, track in enumerate(sequence.findall(f"./media/{kind}/track"), 1):
            tracks.append({"kind": kind, "index": index, "enabled": track.findtext("enabled"),
                           "clips": [[*_bounds(clip), clip.findtext("name", ""),
                                      media_urls.get((clip.find("file").get("id") if clip.find("file") is not None else None), "")]
                                     for clip in track.findall("clipitem")],
                           "transitions": len(track.findall("transitionitem"))})
    return {"name": sequence.findtext("name"), "duration_frames": int(sequence.findtext("duration", "-1")), "tracks": tracks}


def dry_run(xml_path: Path, instruction: dict[str, Any], project_path: Path) -> dict[str, Any]:
    baseline = timeline_digest(xml_path)
    action = instruction["action"]
    result: dict[str, Any] = {"schema_version": "1.0", "created_at": datetime.now(timezone.utc).isoformat(),
                              "instruction": instruction, "project_path": str(project_path.resolve()),
                              "project_sha256": sha256(project_path), "baseline_xml": str(xml_path.resolve()),
                              "baseline_sha256": sha256(xml_path), "sequence": baseline["name"],
                              "duration_before_frames": baseline["duration_frames"], "targets": [],
                              "status": ("MANUAL_REVIEW" if action == "manual_review" else
                                         "ROUTE" if action in {"readback", "rollback"} else "READY")}
    if action != "delete_range":
        return result
    start, end = instruction["start_frame"], instruction["end_frame"]
    if not 0 <= start < end <= baseline["duration_frames"]:
        raise ValueError("删除区间不在当前序列内")
    if end - start > 250:
        raise ValueError("自动删除上限为 10 秒；更长范围需拆分并审片")
    for track in baseline["tracks"]:
        if track["transitions"]:
            raise ValueError("序列存在转场，当前 XML 编辑器不支持安全移动")
        hit = [c for c in track["clips"] if max(c[0], start) < min(c[1], end)]
        if hit:
            result["targets"].append({"track": f"{track['kind']}{track['index']}",
                                      "enabled": track["enabled"], "clips": hit})
    if not any(target["track"].startswith("video") and target["enabled"] != "FALSE" for target in result["targets"]):
        raise ValueError("删除区间未命中启用的视频轨")
    if not any(target["track"].startswith("audio") and target["enabled"] != "FALSE" for target in result["targets"]):
        raise ValueError("删除区间未命中启用的音频轨")
    result["duration_after_frames"] = baseline["duration_frames"] - (end - start)
    result["removed_frames"] = end - start
    return result


def apply_delete(plan: dict[str, Any], output_xml: Path) -> dict[str, Any]:
    if plan.get("status") != "READY" or plan.get("instruction", {}).get("action") != "delete_range":
        raise ValueError("只能执行已通过 dry-run 的删除动作")
    src = Path(plan["baseline_xml"])
    if sha256(src) != plan["baseline_sha256"] or sha256(Path(plan["project_path"])) != plan["project_sha256"]:
        raise ValueError("基准 XML 或 Premiere 工程已变化，请重新生成 dry-run")
    if output_xml.exists():
        raise FileExistsError(output_xml)
    start, end = plan["instruction"]["start_frame"], plan["instruction"]["end_frame"]
    delta = end - start
    tree = ET.parse(src)
    sequence = _sequence(tree.getroot())
    sequence.find("name").text = f"{plan['sequence']}_NL_{uuid.uuid4().hex[:8]}"
    sequence.find("uuid").text = str(uuid.uuid4())
    next_id = max((int(c.get("id").split("-")[-1]) for c in tree.getroot().iter("clipitem")
                   if re.fullmatch(r"clipitem-\d+", c.get("id", ""))), default=0) + 1
    for kind in ("video", "audio"):
        for track in sequence.findall(f"./media/{kind}/track"):
            for clip in list(track.findall("clipitem")):
                clip_start, clip_end, source_in, source_out = _bounds(clip)
                if clip_end <= start:
                    continue
                if clip_start >= end:
                    _update(clip, clip_start - delta, clip_end - delta, source_in, source_out)
                    continue
                left = max(0, start - clip_start)
                right = max(0, clip_end - end)
                if left and right:
                    second = copy.deepcopy(clip)
                    second.set("id", f"clipitem-{next_id}")
                    next_id += 1
                    _update(clip, clip_start, start, source_in, source_in + left)
                    _update(second, start, start + right, source_out - right, source_out)
                    track.insert(list(track).index(clip) + 1, second)
                elif left:
                    _update(clip, clip_start, start, source_in, source_in + left)
                elif right:
                    _update(clip, start, start + right, source_out - right, source_out)
                else:
                    track.remove(clip)
    sequence.find("duration").text = str(plan["duration_after_frames"])
    output_xml.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_xml, encoding="utf-8", xml_declaration=True)
    after = timeline_digest(output_xml)
    if after["duration_frames"] != plan["duration_after_frames"]:
        raise AssertionError("生成的 XML 时长不符")
    return {"candidate_xml": str(output_xml), "candidate_sha256": sha256(output_xml),
            "candidate_sequence": after["name"], "duration_frames": after["duration_frames"],
            "premiere_readback_status": "PENDING"}


def compare_readback(candidate_xml: Path, premiere_xml: Path) -> dict[str, Any]:
    expected, actual = timeline_digest(candidate_xml), timeline_digest(premiere_xml)
    mismatches = []
    if expected["name"] != actual["name"]:
        mismatches.append("sequence_name")
    if expected["duration_frames"] != actual["duration_frames"]:
        mismatches.append("duration_frames")
    if len(expected["tracks"]) != len(actual["tracks"]):
        mismatches.append("track_count")
    for left, right in zip(expected["tracks"], actual["tracks"]):
        if left["kind"] != right["kind"] or left["index"] != right["index"] or left["enabled"] != right["enabled"]:
            mismatches.append(f"{left['kind']}{left['index']}:track")
        if left["transitions"] != right["transitions"]:
            mismatches.append(f"{left['kind']}{left['index']}:transitions")
        if left["clips"] != right["clips"]:
            mismatches.append(f"{left['kind']}{left['index']}:clips")
    return {"status": "PASS" if not mismatches else "FAIL", "mismatches": mismatches,
            "candidate_sha256": sha256(candidate_xml), "premiere_readback_sha256": sha256(premiere_xml),
            "expected_duration_frames": expected["duration_frames"], "actual_duration_frames": actual["duration_frames"]}


def write_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
