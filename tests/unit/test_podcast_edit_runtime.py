from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from engine.podcast_edit_runtime import (apply_delete, compare_readback, dry_run,
                                         parse_instruction, sha256, timeline_digest)
from scripts.podcast_edit import _apply, _plan, _operate


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = ET.Element("xmeml")
    sequence = ET.SubElement(root, "sequence")
    ET.SubElement(sequence, "uuid").text = "old"
    ET.SubElement(sequence, "name").text = "podcast"
    ET.SubElement(sequence, "duration").text = "100"
    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = "25"
    ET.SubElement(rate, "ntsc").text = "FALSE"
    media = ET.SubElement(sequence, "media")
    for kind, enabled in (("video", "TRUE"), ("audio", "FALSE"), ("audio", "TRUE")):
        tracks = media.find(kind) or ET.SubElement(media, kind)
        track = ET.SubElement(tracks, "track")
        ET.SubElement(track, "enabled").text = enabled
        clip = ET.SubElement(track, "clipitem", {"id": f"clipitem-{len(root.findall('.//clipitem')) + 1}"})
        for key, value in (("name", kind), ("start", 0), ("end", 100), ("in", 10), ("out", 110)):
            ET.SubElement(clip, key).text = str(value)
        ET.SubElement(ET.SubElement(clip, "file", {"id": f"file-{kind}-{enabled}"}), "pathurl").text = f"file:///{kind}-{enabled}.mov"
    xml = tmp_path / "baseline.xml"
    ET.ElementTree(root).write(xml, encoding="utf-8")
    project = tmp_path / "project.prproj"
    project.write_bytes(b"checkpoint")
    return xml, project


def test_text_delete_roundtrip_and_rollback_source(tmp_path: Path) -> None:
    xml, project = _fixture(tmp_path)
    intent = parse_instruction("把 00:00:01:00 到 00:00:01:10 删掉")
    assert (intent["start_frame"], intent["end_frame"]) == (25, 35)
    plan = dry_run(xml, intent, project)
    assert plan["status"] == "READY"
    assert {item["track"] for item in plan["targets"]} == {"video1", "audio1", "audio2"}
    candidate = tmp_path / "candidate.xml"
    receipt = apply_delete(plan, candidate)
    assert receipt["duration_frames"] == 90
    digest = timeline_digest(candidate)
    for track in digest["tracks"]:
        assert [clip[:4] for clip in track["clips"]] == [[0, 25, 10, 35], [25, 90, 45, 110]]
    assert compare_readback(candidate, candidate)["status"] == "PASS"
    assert compare_readback(candidate, xml)["status"] == "FAIL"
    assert timeline_digest(xml)["duration_frames"] == 100


def test_ambiguous_f8_note_stays_review_only() -> None:
    note = {"mode": "range", "location": {"range_status": "locked", "normalized": {
        "start_ticks": str(25 * 10_160_640_000), "end_ticks": str(800 * 10_160_640_000)}}}
    result = parse_instruction("这段开头拉回来，末尾黑场去掉", note)
    assert result["action"] == "manual_review"


def test_readback_rejects_same_named_replacement_media(tmp_path: Path) -> None:
    xml, _ = _fixture(tmp_path)
    changed = tmp_path / "replacement.xml"
    tree = ET.parse(xml)
    tree.getroot().find(".//file/pathurl").text = "file:///replacement.mov"
    tree.write(changed, encoding="utf-8")
    result = compare_readback(xml, changed)
    assert result["status"] == "FAIL"
    assert "video1:clips" in result["mismatches"]


def test_stale_project_blocks_execution(tmp_path: Path) -> None:
    xml, project = _fixture(tmp_path)
    plan = dry_run(xml, parse_instruction("删掉 00:00:01:00 到 00:00:01:10"), project)
    project.write_bytes(b"changed")
    with pytest.raises(ValueError, match="已变化"):
        apply_delete(plan, tmp_path / "candidate.xml")


def test_verified_f8_range_uses_same_edit_path(tmp_path: Path) -> None:
    xml, project = _fixture(tmp_path)
    note_dir = tmp_path / "notes" / "one"
    note_dir.mkdir(parents=True)
    audio = note_dir / "note.wav"
    audio.write_bytes(b"unaltered recorded opinion")
    note_path = note_dir / "note.json"
    note_path.write_text(json.dumps({
        "note_id": "one", "mode": "range", "status": {"recording": "saved"},
        "audio": {"path": "note.wav", "sha256": sha256(audio)},
        "transcript": {"state": "transcribed", "text": "这段删掉"},
        "project": {"path": str(project)}, "sequence": {"name": "podcast"},
        "location": {"automatic_edit_allowed": True, "range_status": "locked",
                     "normalized": {"start_ticks": str(25 * 10_160_640_000),
                                    "end_ticks": str(35 * 10_160_640_000)}},
    }), encoding="utf-8")
    job = tmp_path / "job"
    args = argparse.Namespace(project=project, xml=xml, text=None, f8_note=note_path, job=job)
    assert _plan(args)["status"] == "READY"
    assert _apply(args)["duration_frames"] == 90
    assert audio.read_bytes() == b"unaltered recorded opinion"


def test_f8_from_old_sequence_is_review_only(tmp_path: Path) -> None:
    xml, project = _fixture(tmp_path)
    note_dir = tmp_path / "notes" / "stale"
    note_dir.mkdir(parents=True)
    audio = note_dir / "note.wav"
    audio.write_bytes(b"old opinion")
    note_path = note_dir / "note.json"
    note_path.write_text(json.dumps({
        "note_id": "stale", "mode": "range", "status": {"recording": "saved"},
        "audio": {"path": "note.wav", "sha256": sha256(audio)},
        "transcript": {"state": "transcribed", "text": "这段删掉"},
        "project": {"path": str(project)}, "sequence": {"name": "older-podcast"},
        "location": {"automatic_edit_allowed": True, "range_status": "locked",
                     "normalized": {"start_ticks": str(25 * 10_160_640_000),
                                    "end_ticks": str(35 * 10_160_640_000)}},
    }), encoding="utf-8")
    args = argparse.Namespace(project=project, xml=xml, text=None, f8_note=note_path, job=tmp_path / "job")
    assert _plan(args)["status"] == "MANUAL_REVIEW"


def test_typed_operation_reaches_live_publish_without_f8_note(tmp_path, monkeypatch):
    xml, project = _fixture(tmp_path)
    import scripts.podcast_edit as command
    calls=[]
    def publish(args):
        calls.append(args.job)
        return {'status':'PUBLISHED_VERIFIED'}
    monkeypatch.setattr(command,'_publish',publish)
    args=argparse.Namespace(text='删掉 00:00:01:00 到 00:00:01:10',f8_note=None,
                            job=tmp_path/'live_job',project=project,xml=xml,
                            working_project=tmp_path/'working.prproj')
    result=_operate(args)
    assert result['status']=='PUBLISHED_VERIFIED' and calls==[args.job]
    assert result['candidate']['duration_frames']==90
    assert result['candidate']['premiere_readback_status']=='PASS'
