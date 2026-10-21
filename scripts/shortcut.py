"""Deterministic local interface for the recording shortcut.

    cuthere shortcut show                 # effective binding, source and diagnostics
    cuthere shortcut set RightOption      # apply a binding (shared with the panel)
    cuthere shortcut set "Ctrl+Shift+K" --dry-run
    cuthere shortcut reset                # platform default (Right Option / F8)
    cuthere shortcut restore-previous     # undo the last change
    cuthere shortcut test-state           # backend, permission, activation readback
    cuthere shortcut grammar              # the supported values and platform limits

The file this reads and writes is the same one the CEP panel and the capture
helper use, so an agent change is visible in the running panel and is never
overwritten by a stale browser preference. Nothing here edits minified UI code,
guesses a private ``localStorage`` key or touches system keyboard settings.

Exit codes: 0 success, 2 rejected/unavailable/malformed (the same "failed" code
the rest of this CLI uses). Every command prints one JSON object.
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
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from services import platform_paths  # noqa: E402
from services.review_capture import shortcut as shortcut_module  # noqa: E402

#: A helper writes status.json every 0.1–0.5 s while it runs. Ten seconds
#: tolerates a busy machine (and a slow disk) while a file left behind by an
#: exited helper can never be mistaken for a live readback.
HEARTBEAT_FRESH_SECONDS = 10.0
#: A clock can move slightly backwards; a status timestamp far in the future is
#: not evidence of a live helper either.
HEARTBEAT_FUTURE_TOLERANCE_SECONDS = 5.0


def default_config_path(runtime_root: Path | None = None) -> Path:
    root = Path(runtime_root) if runtime_root is not None else platform_paths.runtime_root()
    return root / shortcut_module.CONFIG_NAME


def _timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _path_identity(value: Any) -> str:
    try:
        return os.path.normcase(str(Path(value).resolve(strict=False)))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(str(Path(value).absolute()))


def _probe_status(path: Path, *, config_path: Path | None,
                  platform: str | None, now: datetime) -> dict[str, Any]:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": None, "path": str(path), "malformed": True,
                "age_seconds": None, "fresh": False, "config_matches": False,
                "platform_matches": False, "timestamp": None,
                "mtime": path.stat().st_mtime if path.exists() else 0.0}
    if not isinstance(status, dict):
        return {"status": None, "path": str(path), "malformed": True,
                "age_seconds": None, "fresh": False, "config_matches": False,
                "platform_matches": False, "timestamp": None,
                "mtime": path.stat().st_mtime if path.exists() else 0.0}
    timestamp = _timestamp(status.get("updated_at"))
    age = (now - timestamp).total_seconds() if timestamp is not None else None
    fresh = (age is not None
             and -HEARTBEAT_FUTURE_TOLERANCE_SECONDS <= age <= HEARTBEAT_FRESH_SECONDS)
    status_config = status.get("shortcut_config")
    config_matches = (bool(config_path) and bool(status_config)
                      and _path_identity(status_config) == _path_identity(config_path))
    status_platform = status.get("platform")
    platform_matches = (bool(platform) and bool(status_platform)
                        and shortcut_module.normalize_platform(status_platform)
                        == shortcut_module.normalize_platform(platform))
    return {
        "status": status,
        "path": str(path),
        "malformed": False,
        "age_seconds": age,
        "fresh": fresh,
        "config_matches": config_matches,
        "platform_matches": platform_matches,
        "timestamp": timestamp,
        "mtime": path.stat().st_mtime if path.exists() else 0.0,
    }


def find_live_status(runtime_root: Path | None = None, *, config_path: Path | None = None,
                     platform: str | None = None,
                     now: datetime | None = None) -> dict[str, Any] | None:
    """Newest readable helper status, with freshness and identity attached.

    A file that merely exists is not a running helper: ``updated_at`` must fall
    inside the documented heartbeat window, and the status must name the same
    ``shortcut.json`` and platform the caller asked about. A custom ``--config``
    therefore cannot be "confirmed" by an unrelated default-runtime helper.
    """
    root = Path(runtime_root) if runtime_root is not None else platform_paths.runtime_root()
    candidates = sorted((root / "ipc").glob("*/status.json"),
                        key=lambda path: path.stat().st_mtime if path.exists() else 0,
                        reverse=True)
    probes = [_probe_status(path, config_path=config_path, platform=platform,
                            now=now or datetime.now(timezone.utc))
              for path in candidates]
    probes = [probe for probe in probes if probe is not None]
    if not probes:
        return None
    readable = [probe for probe in probes if not probe.get("malformed")]
    if not readable:
        return probes[0]
    # Prefer a fresh matching helper; then freshness; then config/platform
    # identity. This keeps an old version folder from shadowing the live one.
    readable.sort(key=lambda probe: (bool(probe["fresh"]), bool(probe["config_matches"]),
                                     bool(probe["platform_matches"]), probe["mtime"]),
                  reverse=True)
    return readable[0]


def live_readback(probe: dict[str, Any] | None, *, config_path: Path | None = None,
                  platform: str | None = None) -> dict[str, Any]:
    """Describe what a helper file proves, never more than it proves."""
    result: dict[str, Any] = {
        "helper_running": False,
        "fresh": False,
        "freshness_seconds": HEARTBEAT_FRESH_SECONDS,
        "config_matches": False,
        "platform_matches": False,
        "reason": "no_status_file",
        "status_path": None,
        "updated_at": None,
        "age_seconds": None,
        "worker_version": None,
        "reported_effective": None,
        "reported_applied": None,
        "effective": None,
        "applied": False,
        "pending": False,
        "pending_binding": None,
        "active_binding": None,
        "configured_reported": None,
        "registration": None,
    }
    if not probe:
        result["note"] = "没有找到运行中的录音助手；面板打开后此诊断才有数据。"
        return result
    result["status_path"] = probe.get("path")
    result["age_seconds"] = (round(probe["age_seconds"], 3)
                             if probe.get("age_seconds") is not None else None)
    result["fresh"] = bool(probe.get("fresh"))
    result["config_matches"] = bool(probe.get("config_matches"))
    result["platform_matches"] = bool(probe.get("platform_matches"))
    if probe.get("malformed"):
        result["reason"] = "malformed_status"
        return result
    status = probe.get("status") or {}
    result["updated_at"] = status.get("updated_at")
    result["worker_version"] = status.get("worker_version")
    result["status_platform"] = status.get("platform")
    result["status_config"] = status.get("shortcut_config")
    saved = status.get("shortcut") if isinstance(status.get("shortcut"), dict) else {}
    result.update({
        "armed": status.get("push_to_talk_armed"),
        "backend": status.get("push_to_talk_backend"),
        "reported_effective": saved.get("effective") or saved.get("binding"),
        "reported_applied": saved.get("applied"),
        "configured_reported": saved.get("configured") or saved.get("binding"),
        "pending": bool(saved.get("pending")),
        "pending_binding": saved.get("pending_binding"),
        "active_binding": saved.get("active_binding"),
        "registration": saved.get("registration"),
    })
    timestamp = probe.get("timestamp")
    if timestamp is None:
        result["reason"] = "malformed_heartbeat"
    elif probe["age_seconds"] > HEARTBEAT_FRESH_SECONDS:
        result["reason"] = "stale_heartbeat"
    elif probe["age_seconds"] < -HEARTBEAT_FUTURE_TOLERANCE_SECONDS:
        result["reason"] = "heartbeat_in_future"
    elif not result["config_matches"]:
        result["reason"] = "config_mismatch"
    elif not result["platform_matches"]:
        result["reason"] = "platform_mismatch"
    else:
        result["reason"] = "live"
        result["helper_running"] = True
        result["effective"] = result["reported_effective"]
        registration = result.get("registration") or {}
        result["applied"] = bool(
            result["reported_applied"]
            and str(result["reported_applied"]) == str(result["reported_effective"])
            and registration.get("applied", True))
    return result


def application_state(configured: str, live: dict[str, Any],
                      *, written_after: datetime | None = None) -> dict[str, Any]:
    """Distinguish configured, effective, applied and pending.

    A successful write proves only that the file was saved. A live helper status
    proves what that helper has read; it proves ``applied`` only when the helper
    also reports the binding as owned/registered. A status written before the
    write is not an acknowledgment of that write.
    """
    if not live.get("helper_running"):
        reason = live.get("reason") or "no_status_file"
        state = "not_running" if reason in {
            "no_status_file", "stale_heartbeat", "malformed_heartbeat",
            "malformed_status", "heartbeat_in_future"} else reason
        return {"state": state, "effective": None, "applied": False,
                "pending": False, "reason": reason,
                "freshness_seconds": live.get("freshness_seconds")}
    effective = live.get("effective")
    timestamp = _timestamp(live.get("updated_at"))
    if written_after is not None and timestamp is not None and timestamp < written_after:
        return {"state": "awaiting_next_poll", "effective": effective,
                "applied": False, "pending": True, "reason": "status_predates_write",
                "freshness_seconds": live.get("freshness_seconds")}
    if effective == configured and live.get("applied"):
        return {"state": "applied", "effective": effective, "applied": True,
                "pending": False, "reason": "helper_reports_applied",
                "freshness_seconds": live.get("freshness_seconds")}
    if live.get("pending") and live.get("pending_binding") == configured:
        return {"state": "deferred_until_take_ends", "effective": effective,
                "applied": False, "pending": True, "reason": "take_in_progress",
                "freshness_seconds": live.get("freshness_seconds")}
    registration = live.get("registration") or {}
    if effective == configured:
        reason = registration.get("reason") or "helper_has_not_registered_it"
        return {"state": "effective_not_applied", "effective": effective,
                "applied": False, "pending": False, "reason": reason,
                "freshness_seconds": live.get("freshness_seconds")}
    return {"state": "pending", "effective": effective, "applied": False,
            "pending": True, "reason": "helper_reports_a_different_binding",
            "freshness_seconds": live.get("freshness_seconds")}


def capability_report(platform: str) -> dict[str, Any]:
    try:
        from services.review_capture.ptt import backend_capability

        return backend_capability()
    except Exception as exc:  # a broken optional dependency must not hide the setting
        return {"platform": platform, "backend": None, "available": False,
                "reason": f"{type(exc).__name__}: {exc}"}


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def parse_args(argv: list[str]) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None,
                        help="path to the authoritative shortcut.json")
    common.add_argument("--runtime-root", type=Path, default=None,
                        help="Cuthere per-user runtime root (overrides the default location)")
    common.add_argument("--platform", default=None,
                        help="validate against this platform (default: this host)")
    parser = argparse.ArgumentParser(prog="cuthere shortcut", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show", parents=[common], help="print the effective binding and diagnostics")
    setter = sub.add_parser("set", parents=[common], help="apply a binding")
    setter.add_argument("binding")
    setter.add_argument("--dry-run", action="store_true", help="validate and preview only")
    resetter = sub.add_parser("reset", parents=[common], help="reset to the platform default")
    resetter.add_argument("--dry-run", action="store_true")
    restore = sub.add_parser("restore-previous", parents=[common], help="undo the last change")
    restore.add_argument("--dry-run", action="store_true")
    sub.add_parser("test-state", parents=[common],
                   help="backend, permission and activation readback")
    sub.add_parser("grammar", parents=[common], help="supported values and platform limits")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(sys.argv[1:] if argv is None else argv)
    platform = shortcut_module.normalize_platform(
        args.platform if args.platform else platform_paths.current_platform())
    config_path = args.config or default_config_path(args.runtime_root)
    store = shortcut_module.ShortcutStore(config_path, platform=platform)
    arguments = {"config_path": str(config_path), "platform": platform}

    if args.command == "grammar":
        support = shortcut_module.describe_support(platform)
        return emit({"ok": True, **arguments, "support": support})

    if args.command in {"set", "reset", "restore-previous"}:
        # Take the marker before the write. A status.json whose updated_at is
        # older than this was produced before the setting existed and cannot be
        # post-write acknowledgment.
        written_after = datetime.now(timezone.utc)
        try:
            if args.command == "set":
                state = store.set(args.binding, source="cli", dry_run=args.dry_run)
            elif args.command == "reset":
                state = store.reset(source="reset", dry_run=args.dry_run)
            else:
                state = store.restore_previous(source="cli", dry_run=args.dry_run)
        except shortcut_module.ShortcutError as error:
            probe = find_live_status(args.runtime_root, config_path=config_path,
                                     platform=platform)
            live = live_readback(probe, config_path=config_path, platform=platform)
            return emit({"ok": False, **arguments, "code": error.code,
                         "message": error.message, "saved": False,
                         "configured": store.load(force=True).binding.canonical,
                         "live": live}, 2)
        after = store.load(force=True) if not args.dry_run else state
        probe = find_live_status(args.runtime_root, config_path=config_path,
                                 platform=platform)
        live = live_readback(probe, config_path=config_path, platform=platform)
        application = ({"state": "dry_run", "effective": None, "applied": False,
                        "pending": False, "reason": "dry_run",
                        "freshness_seconds": HEARTBEAT_FRESH_SECONDS}
                       if args.dry_run
                       else application_state(after.binding.canonical, live,
                                              written_after=written_after))
        return emit({
            "ok": True,
            **arguments,
            "dry_run": bool(args.dry_run),
            "saved": not args.dry_run,
            "binding": after.binding.canonical,
            "configured": after.binding.canonical,
            "display": after.binding.display(platform),
            "previous_binding": state.previous_binding,
            "source": after.source if not args.dry_run else "dry-run",
            "effective": application["effective"],
            "applied": application["applied"],
            "pending": application["pending"],
            "application": application,
            "conflicts": shortcut_module.conflicts(after.binding, platform=platform),
            "live": live,
            "hint": ("预览通过，尚未写入设置。"
                     if args.dry_run else
                     "已保存到设置文件；是否已被运行中的录音助手注册/生效见 applied 与 "
                     "application。录音中不会中途换键，新设置会在本次录音结束后应用。"),
        })

    state = store.load(force=True)
    probe = find_live_status(args.runtime_root, config_path=config_path, platform=platform)
    live = live_readback(probe, config_path=config_path, platform=platform)
    application = application_state(state.binding.canonical, live)
    # A first run (no file yet) is healthy; an unreadable file that had to fall
    # back to the default is not, and must not look like a normal default.
    healthy = state.status == "ok" or (state.status == "default" and not state.problems)
    report: dict[str, Any] = {
        "ok": healthy,
        **arguments,
        **state.snapshot(platform),
        "configured": state.binding.canonical,
        "effective": application["effective"],
        "applied": application["applied"],
        "pending": application["pending"],
        "application": application,
        "conflicts": shortcut_module.conflicts(state.binding, platform=platform),
        "live": live,
    }
    if args.command == "show":
        report["support"] = shortcut_module.describe_support(platform)
        return emit(report, 0 if report["ok"] else 2)

    report["capability"] = capability_report(platform)
    report["physical_test"] = (
        "命令行只能证明设置已保存并被读取。真实按键（含 Mac 右 Option）与麦克风仍需在本机"
        "Premiere 前台时手动按一次确认；面板的“按住录音已就绪/最近一次按下”与 "
        "ptt_key_diagnostic.json 是这项手动测试的证据。")
    return emit(report, 0 if report["ok"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
