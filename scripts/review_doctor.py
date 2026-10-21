"""Read-only diagnostics for the standalone Premiere review capture add-on."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from services import platform_paths


def write_report(report_dir: Path, report: dict[str, Any]) -> None:
    if report_dir.exists() and any(report_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite a non-empty report directory: {report_dir}")
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (report_dir / "doctor.json").write_text(payload, encoding="utf-8")
    lines = ["# Premiere 审片意见环境检查", "", f"检查时间：{report['checked_at']}", ""]
    for check in report["checks"]:
        lines.extend([f"- **{check['status']}** · {check['name']}：{check['detail']}"])
    lines.extend(["", "这是一份只读环境检查；没有采集麦克风，也没有修改 Premiere 工程。", ""])
    (report_dir / "doctor.md").write_text("\n".join(lines), encoding="utf-8")


def player_debug_windows(cep_version: str) -> tuple[str | None, str]:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Adobe\CSXS.{cep_version}")
        value, _kind = winreg.QueryValueEx(key, "PlayerDebugMode")
        winreg.CloseKey(key)
        return str(value), ""
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def player_debug_darwin(cep_version: str) -> tuple[str | None, str]:
    domain = platform_paths.ssxs_defaults_domain(cep_version)
    try:
        completed = subprocess.run(
            ["defaults", "read", domain, "PlayerDebugMode"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return None, (completed.stderr or "not set").strip()
    return completed.stdout.strip(), ""


def cep_versions_for(current: str, cep_version: str | None) -> tuple[str, ...]:
    """CEP generations a diagnosis covers.

    macOS checks both supported generations by default, matching the installer,
    so a working CEP 12 setting cannot hide a missing CEP 11 one that Premiere
    2024 needs. An explicit ``--cep-version`` restricts the check to one
    generation; Windows keeps its existing single-version default.
    """
    if cep_version:
        return (str(cep_version),)
    if current == "darwin":
        return platform_paths.DEFAULT_CEP_VERSIONS
    return ("12",)


def cep_role(version: str) -> str:
    if version == "11":
        return "Premiere Pro 2024（24.x）使用 CEP 11"
    if version == "12":
        return "Premiere Pro 2025（25.x）使用 CEP 12"
    return f"CEP {version}"


def _defaults_detail(current: str, version: str, value: str | None, why: str) -> tuple[str, str]:
    """Explain what was actually read, not just whether it equals 1.

    Three states must stay distinguishable: unreadable/absent (``value`` is
    None), read but different from the target (for example ``0``), and enabled.
    The old wording called every non-1 value "not read", which hid a concrete
    ``0`` behind an empty reason.
    """
    domain = platform_paths.ssxs_defaults_domain(version)
    if current == "win32":
        source = f"CSXS.{version}"
    else:
        source = f"defaults {domain}"
    if value == "1":
        label = "PlayerDebugMode=1（目标 1）" if current == "win32" else f"{source} PlayerDebugMode=1（目标 1）"
        return label, "enabled"
    if value is not None:
        label = (f"PlayerDebugMode={value}（目标为 1）" if current == "win32"
                 else f"{source} PlayerDebugMode={value}（目标为 1）")
        state = "value"
        label = f"{label}；读到的是 {value!r}，不是 1"
    else:
        label = (f"CSXS.{version} 的 PlayerDebugMode 未读取到：{why}" if current == "win32"
                 else f"{source} PlayerDebugMode 未读取到：{why}")
        state = "unreadable"
    if current == "darwin":
        label = f"{label}；{cep_role(version)}。"
        if state != "enabled":
            label += (f"重新运行安装器（默认同时启用 CEP 11 与 CEP 12），或执行 "
                      f"defaults write {domain} PlayerDebugMode -string 1，然后完整退出并重开 Premiere")
    return label, state


def player_debug_checks(current: str, cep_version: str | None = None) -> list[dict[str, Any]]:
    """One check per CEP domain; every value carries its CEP generation."""
    versions = cep_versions_for(current, cep_version)
    if current not in {"win32", "darwin"}:
        return [{"status": "BLOCKED", "name": "CEP 开发扩展加载设置",
                 "detail": "仅检查了 Windows 与 macOS 环境", "version": None,
                 "value": None, "why": "", "state": "unsupported"}]
    entries: list[dict[str, Any]] = []
    for version in versions:
        if current == "win32":
            value, why = player_debug_windows(version)
            name = "CEP 开发扩展加载设置"
        else:
            value, why = player_debug_darwin(version)
            name = f"CEP {version} 开发扩展加载设置"
        detail, state = _defaults_detail(current, version, value, why)
        entries.append({"status": "PASS" if state == "enabled" else "BLOCKED", "name": name,
                        "detail": detail, "version": version, "value": value, "why": why,
                        "state": state})
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--cep-version", default=None,
                        help="check one CSXS version (macOS default: 11 and 12; Windows default: 12)")
    args = parser.parse_args()
    project_root = args.project_root.resolve()
    extension = platform_paths.extension_root() / platform_paths.PANEL_FOLDER
    runtime_root = platform_paths.runtime_root()
    checks: list[dict[str, Any]] = []

    def add(status: str, name: str, detail: str, evidence: Any = None) -> None:
        checks.append({"status": status, "name": name, "detail": detail, "evidence": evidence})

    py_supported = (3, 11) <= sys.version_info[:2] < (3, 13)
    add("PASS" if py_supported else "FAIL", "Python 版本", platform.python_version(), sys.executable)
    add("INFO", "平台", platform_paths.current_platform(), {"release": platform.release(), "machine": platform.machine()})
    add("PASS" if project_root.is_dir() else "FAIL", "项目目录", str(project_root))
    add("PASS" if (extension / "CSXS" / "manifest.xml").is_file() else "BLOCKED", "Premiere 扩展安装", str(extension))
    runtime_file = extension / "runtime.json"
    runtime = None
    if runtime_file.is_file():
        try:
            runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
            launcher_key = "pythonw" if runtime.get("pythonw") else "launcher"
            paths_ok = Path(runtime[launcher_key]).is_file() and Path(runtime["worker_entry"]).is_file()
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

    try:
        from services.review_capture.ptt import backend_capability

        capability = backend_capability()
        add("PASS" if capability.get("available") else "INFO", "按住录音后端",
            f"{capability.get('backend')}：{'可用' if capability.get('available') else capability.get('reason', '')}", capability)
    except Exception as exc:
        add("FAIL", "按住录音后端", f"{type(exc).__name__}: {exc}")

    player_debug = None
    player_debug_modes: dict[str, str | None] = {}
    current = platform_paths.current_platform()
    cep_checks = player_debug_checks(current, args.cep_version)
    checks.extend(cep_checks)
    for entry in cep_checks:
        if entry.get("version"):
            value = entry.get("value")
            player_debug_modes[str(entry["version"])] = str(value) if value is not None else None
    if cep_checks:
        # The primary value is the explicitly checked version, or the newest
        # generation of the default set (CEP 12), matching the old report field.
        player_debug = cep_checks[0].get("value") if args.cep_version else cep_checks[-1].get("value")

    checks.append({"status": "NOT_RUN", "name": "Premiere 宿主读取", "detail": "需要打开本机 Premiere 和专用测试序列，不能由静态检查替代。"})
    checks.append({"status": "NOT_RUN", "name": "真实麦克风录音", "detail": "doctor 不采集声音；需通过明确的录音验收操作验证。"})
    report = {
        "schema_version": "1.0",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": current,
        "project_root": str(project_root),
        "python": {"version": platform.python_version(), "executable": sys.executable},
        "extension_path": str(extension),
        "runtime_root": str(runtime_root),
        "player_debug_mode": player_debug,
        "player_debug_modes": player_debug_modes,
        "microphones": devices,
        "checks": checks,
        "overall": "PASS" if all(c["status"] == "PASS" for c in checks) else "BLOCKED",
    }
    write_report(args.report_dir.resolve(), report)
    print(json.dumps({"overall": report["overall"], "report": str(args.report_dir.resolve()), "checks": len(checks)}, ensure_ascii=False))
    return 0 if report["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
