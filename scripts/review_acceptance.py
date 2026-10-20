"""Acceptance runner. Host and microphone runs never silently degrade to mocks."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))


def _fresh_run_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.exists():
        raise FileExistsError(f"验收目录已存在，请新建目录：{resolved}")
    resolved.mkdir(parents=True)
    return resolved


def _run_unit() -> tuple[str, str, list[dict[str, Any]]]:
    output = io.StringIO()
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.review.test_review_capture")
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    status = "PASS" if result.wasSuccessful() else "FAIL"
    detail = f"{result.testsRun} 条程序逻辑用例；失败 {len(result.failures)}，错误 {len(result.errors)}"
    failures = []
    for test, reason in result.failures + result.errors:
        failures.append({"case": str(test), "reason": reason})
    return status, detail, [{"stdout": output.getvalue(), "failures": failures}]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True, choices=["unit", "host", "recorder", "e2e"])
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--device", type=int)
    parser.add_argument("--seconds", type=float, default=3.0)
    parser.add_argument("--confirm-local-recording", action="store_true", help="required before this runner captures microphone audio")
    args = parser.parse_args()
    run_dir = _fresh_run_dir(args.run_dir)
    results: list[dict[str, Any]] = []
    evidence: list[str] = []
    code = 0

    if args.suite == "unit":
        status, detail, result_evidence = _run_unit()
        results.append({"case_id": "U01-U12", "layer": "U", "status": status, "detail": detail, "evidence": result_evidence})
        (run_dir / "unit-test-output.txt").write_text(result_evidence[0]["stdout"], encoding="utf-8")
        evidence.append("unit-test-output.txt")
        code = 0 if status == "PASS" else 1
    elif args.suite == "host":
        if not args.fixture or not args.fixture.is_file():
            status, detail = "BLOCKED", "需要提供专用 Premiere 测试工程副本；此项不接受当前用户工程或模拟接口作为替代。"
        else:
            status, detail = "BLOCKED", f"测试工程已指定：{args.fixture.resolve()}；仍需真实 Premiere 面板回执与独立的时间码期望表。"
        results.append({"case_id": "H01", "layer": "H", "status": status, "detail": detail})
        code = 2
    elif args.suite == "recorder":
        if not args.confirm_local_recording:
            status, detail = "BLOCKED", "为避免误录环境声音，需要显式传入 --confirm-local-recording。"
        elif args.device is None:
            status, detail = "BLOCKED", "需要先选择设备编号 --device。"
        else:
            try:
                from services.review_capture.audio import MicrophoneRecorder, enumerate_input_devices

                device = next((d for d in enumerate_input_devices() if d["index"] == args.device), None)
                if device is None:
                    raise ValueError(f"没有输入设备编号 {args.device}")
                capture_dir = run_dir / "recording-evidence"
                recorder = MicrophoneRecorder(capture_dir, device)
                started = recorder.start()
                import time

                time.sleep(max(1.0, min(args.seconds, 30.0)))
                audio = recorder.stop()
                with wave.open(str(capture_dir / "audio.wav"), "rb") as check:
                    valid = check.getnchannels() == 1 and check.getframerate() == 48000 and check.getnframes() > 0
                good = valid and not audio.get("dropped_frames") and not audio.get("input_overflows")
                status = "PASS" if good else "FAIL"
                detail = f"设备 {device['name']}；采集 {audio.get('samples_input')} 个输入采样；解码检查={'通过' if valid else '失败'}；丢帧={audio.get('dropped_frames')}；溢出={audio.get('input_overflows')}"
                results.append({"case_id": "R01", "layer": "R", "status": status, "detail": detail, "audio": audio, "started": started})
                evidence.append("recording-evidence/audio.wav")
                code = 0 if good else 1
            except Exception as exc:
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
                results.append({"case_id": "R01", "layer": "R", "status": status, "detail": detail})
                code = 1
    else:
        status, detail = "BLOCKED", "端到端需要先通过真机宿主与真实录音检查，并在测试工程副本上验证；当前不会用假宿主报告通过。"
        results.append({"case_id": "E01", "layer": "E", "status": status, "detail": detail})
        code = 2

    report = {
        "schema_version": "1.0",
        "run_id": run_dir.name,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "suite": args.suite,
        "python": {"version": sys.version, "executable": sys.executable},
        "project_fixture": str(args.fixture.resolve()) if args.fixture else None,
        "test_results": results,
        "evidence_paths": evidence,
        "gate_results": {"U": "NOT_RUN", "H": "NOT_RUN", "R": "NOT_RUN", "E": "NOT_RUN"},
        "self_check_repair_round_limit": 3,
        "overall": results[0]["status"],
    }
    if args.suite == "unit":
        report["gate_results"]["U"] = report["overall"]
    if args.suite == "host":
        report["gate_results"]["H"] = report["overall"]
    if args.suite == "recorder":
        report["gate_results"]["R"] = report["overall"]
    if args.suite == "e2e":
        report["gate_results"]["E"] = report["overall"]
    (run_dir / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 审片意见验收报告", "", f"运行套件：{args.suite}", f"结果：**{report['overall']}**", ""]
    for item in results:
        lines.append(f"- **{item['status']}** · {item['case_id']}：{item['detail']}")
    lines.extend(["", "此报告只覆盖所列套件；未运行的真机项目保持 BLOCKED/NOT_RUN。", ""])
    (run_dir / "acceptance.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"overall": report["overall"], "run_dir": str(run_dir), "suite": args.suite}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
