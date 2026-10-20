"""Read-only diagnostics for the standalone Premiere review capture add-on."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))


def write_report(report_dir: Path, report: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=False)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (report_dir / "doctor.json").write_text(payload, encoding="utf-8")
    lines = ["# Premiere 审片意见环境检查", "", f"检查时间：{report['checked_at']}", ""]
    for check in report["checks"]:
        lines.extend([f"- **{check['status']}** · {check['name']}：{check['detail']}"])
    lines.extend(["", "这是一份只读环境检查；没有采集麦克风，也没有修改 Premiere 工程。", ""])
    (report_dir / "doctor.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    appdata = Path(os.environ.get("APPDATA", ""))
    localappdata = Path(os.environ.get("LOCALAPPDATA", ""))
    extension = appdata / "Adobe" / "CEP" / "extensions" / "AutoVideoEditReviewCapture"
    runtime_root = localappdata / "AutoVideoEdit" / "PremiereReview"
    checks: list[dict[str, Any]] = []

    def add(status: str, name: str, detail: str, evidence: Any = None) -> None:
        checks.append({"status": status, "name": name, "detail": detail, "evidence": evidence})

    py_supported = sys.version_info >= (3, 11) and sys.version_info < (3, 13)
    add("PASS" if py_supported else "FAIL", "Python 版本", platform.python_version(), sys.executable)
    add("PASS" if project_root.is_dir() else "FAIL", "项目目录", str(project_root))
    add("PASS" if (extension / "CSXS" / "manifest.xml").is_file() else "BLOCKED", "Premiere 扩展安装", str(extension))
    runtime_file = extension / "runtime.json"
    runtime = None
    if runtime_file.is_file():
        try:
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            paths_ok = Path(runtime["pythonw"]).is_file() and Path(runtime["worker_entry"]).is_file()
            add("PASS" if paths_ok else "FAIL", "录音助手运行文件", "路径有效" if paths_ok else "配置中的程序文件缺失", runtime)
        except Exception as exc:
            add("FAIL", "录音助手配置", f"读取失败：{type(exc).__name__}: {exc}")
    else:
        add("BLOCKED", "录音助手配置", "尚未安装扩展运行环境")

    devices: list[dict[str, Any]] = []
    try:
        from services.review_capture.audio import enumerate_input_devices

        devices = enumerate_input_devices()
        add("PASS" if devices else "BLOCKED", "麦克风枚举", f"找到 {len(devices)} 个输入设备", devices)
    except Exception as exc:
        add("BLOCKED", "麦克风枚举", f"未能读取设备：{type(exc).__name__}: {exc}")

    player_debug = None
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Adobe\CSXS.12")
            player_debug, _kind = winreg.QueryValueEx(key, "PlayerDebugMode")
            winreg.CloseKey(key)
            add("PASS" if str(player_debug) == "1" else "BLOCKED", "CEP 开发扩展加载设置", str(player_debug))
        except Exception:
            add("BLOCKED", "CEP 开发扩展加载设置", "当前用户的 CSXS.12 PlayerDebugMode 未读取到")
    else:
        add("BLOCKED", "CEP 开发扩展加载设置", "仅检查了 Windows 环境")

    checks.append({"status": "NOT_RUN", "name": "Premiere 宿主读取", "detail": "需要打开本机 Premiere 和专用测试序列，不能由静态检查替代。"})
    checks.append({"status": "NOT_RUN", "name": "真实麦克风录音", "detail": "doctor 不采集声音；需通过明确的录音验收操作验证。"})
    report = {
        "schema_version": "1.0",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_root": str(project_root),
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "extension_path": str(extension),
        "runtime_root": str(runtime_root),
        "player_debug_mode": player_debug,
        "microphones": devices,
        "checks": checks,
        "overall": "PASS" if all(c["status"] == "PASS" for c in checks) else "BLOCKED",
    }
    write_report(args.report_dir.resolve(), report)
    print(json.dumps({"overall": report["overall"], "report": str(args.report_dir.resolve()), "checks": len(checks)}, ensure_ascii=False))
    return 0 if report["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
