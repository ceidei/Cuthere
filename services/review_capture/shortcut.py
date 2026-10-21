"""One authoritative recording-shortcut setting, shared by panel, helper and CLI.

The old panel kept the hold-to-record key in CEP ``localStorage`` with two values
(``F8``/``F9``) and the helper silently rewrote anything it did not recognise to
``F8``. This module replaces that with a single per-user file that the panel, the
capture worker and the agent CLI all read:

    <per-user runtime root>/shortcut.json

The location is the *runtime* root, not the version-scoped IPC folder
(``ipc/f8-<version>``), so a chosen key survives an upgrade. Writes are atomic
and the value that was just successfully written is kept as
``shortcut.last-good.json``: a malformed or half-written update can never
silently reset a user to ``F8`` or roll back to the value before the last good
one. The undo value (``previous_binding``) is separate from this recovery copy.

The string grammar is deliberately small and explicit, and every value that the
key backends cannot implement end to end is rejected with a message instead of
being accepted and then ignored:

    binding        := modifier_only | chord
    chord          := modifier "+" {modifier "+"} key        (0..4 modifiers)
    modifier       := Control | Alt | Shift | Meta           (generic)
                    | Left|Right + (Control|Alt|Shift|Meta)  (side-qualified, macOS)
    modifier_only  := one side-qualified modifier            (macOS only)
    key            := F1..F12 | A..Z | 0..9 | Space | Enter | Tab | Backspace
                    | Insert | Delete | Home | End | PageUp | PageDown
                    | Up | Down | Left | Right
                    (Windows refuses F12 and combinations whose base key is F12)

Canonical output always lists modifiers in ``Control+Alt+Shift+Meta`` order, so a
value written by the panel, the CLI or a hand edit compares equal.

Platform limits are real and are refused, not papered over: Windows registers a
global hotkey, which cannot express "only the right-hand Alt" and cannot observe
a modifier on its own, so modifier-only and side-qualified bindings are macOS
only. ``Escape`` is reserved because the panel's recorder uses it to cancel, and
``Fn``/Globe has no dependable event on either platform.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
CONFIG_NAME = "shortcut.json"
BACKUP_NAME = "shortcut.last-good.json"
SOURCE_MIGRATION = "migration"

# Order used for the canonical string; also the order shown in the panel.
MODIFIER_ORDER = ("Control", "Alt", "Shift", "Meta")

# Side-qualified names. ``LeftAlt`` is what macOS calls Left Option and what the
# Windows documentation calls left Alt; the value is platform-neutral.
SIDED_MODIFIERS = {
    f"{side}{base}": base
    for base in MODIFIER_ORDER
    for side in ("Left", "Right")
}

BASE_KEYS = (
    tuple(f"F{number}" for number in range(1, 13))
    + tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    + tuple("0123456789")
    + ("Space", "Enter", "Tab", "Backspace", "Insert", "Delete", "Home", "End",
       "PageUp", "PageDown", "Up", "Down", "Left", "Right")
)
BASE_KEY_SET = frozenset(BASE_KEYS)

#: Input spellings that map onto a canonical token. Matching is case-insensitive
#: and ignores spaces, ``-`` and ``_``. Modifiers and base keys are separate maps
#: so ``Space`` can never be mistaken for a modifier.
MODIFIER_ALIASES = {
    "ctrl": "Control", "control": "Control",
    "alt": "Alt", "option": "Alt", "opt": "Alt",
    "shift": "Shift",
    "meta": "Meta", "cmd": "Meta", "command": "Meta", "win": "Meta", "super": "Meta",
    "leftctrl": "LeftControl", "leftcontrol": "LeftControl",
    "rightctrl": "RightControl", "rightcontrol": "RightControl",
    "leftalt": "LeftAlt", "leftoption": "LeftAlt", "leftopt": "LeftAlt", "lopt": "LeftAlt",
    "rightalt": "RightAlt", "rightoption": "RightAlt", "rightopt": "RightAlt", "ropt": "RightAlt",
    "leftshift": "LeftShift", "rightshift": "RightShift",
    "leftmeta": "LeftMeta", "leftcmd": "LeftMeta", "leftcommand": "LeftMeta", "lcmd": "LeftMeta",
    "rightmeta": "RightMeta", "rightcmd": "RightMeta", "rightcommand": "RightMeta", "rcmd": "RightMeta",
}

KEY_ALIASES = {
    "return": "Enter", "enter": "Enter", "tab": "Tab",
    "del": "Delete", "backspace": "Backspace", "bksp": "Backspace",
    "pgup": "PageUp", "pageup": "PageUp", "pgdn": "PageDown", "pagedown": "PageDown",
    "arrowup": "Up", "up": "Up", "arrowdown": "Down", "down": "Down",
    "arrowleft": "Left", "left": "Left", "arrowright": "Right", "right": "Right",
    "spacebar": "Space", "space": "Space",
    "esc": "Escape", "escape": "Escape",
    "fn": "Fn", "globe": "Fn",
}

#: Not offered: ``Escape`` cancels the panel recorder, ``Fn`` has no reliable
#: event, and a bare modifier is a real shortcut only on macOS.
RESERVED_KEYS = {
    "Escape": ("reserved_escape",
               "Escape 用于取消录制，不能作为录音快捷键；请换一个按键。"),
    "Fn": ("reserved_fn",
           "Fn / Globe 键不能可靠地作为录音快捷键；请用其他按键。"),
}

#: Windows always keeps F12 for debuggers. Microsoft's ``RegisterHotKey``
#: documentation says the key is reserved and must not be registered, so the
#: parser refuses it before the backend ever tries; macOS is unaffected.
WINDOWS_RESERVED_KEYS = {
    "F12": ("reserved_windows_f12",
            "Windows 把 F12 保留给调试器（Microsoft 要求不要注册），不能作为录音快捷键；"
            "请改用 F1–F11 或其他按键。"),
}

#: Widely known system shortcuts. Reported as warnings only: Cuthere cannot see
#: every conflict, and Premiere's own Keyboard Shortcuts dialog is authoritative.
#: Every key here must be a bindable value, so an entry cannot silently describe
#: a shortcut the parser would reject.
KNOWN_CONFLICTS = {
    "Control+Space": "输入法切换",
    "Meta+Space": "Spotlight / 聚焦",
    "Meta+Tab": "应用切换",
    "Meta+Q": "退出应用",
    "Meta+W": "关闭窗口",
    "Meta+L": "锁定屏幕",
    "Alt+Tab": "窗口切换",
    "Control+Alt+Delete": "系统安全界面",
}


class ShortcutError(ValueError):
    """A rejected binding; ``code`` is stable, ``message`` is shown to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_platform() -> str:
    """This host, in the same spelling ``platform_paths`` and the backends use."""
    try:  # Imported lazily so this module stays importable in isolation.
        from services.platform_paths import current_platform

        return current_platform()
    except Exception:  # pragma: no cover - platform_paths is always shipped
        return "win32" if os.name == "nt" else "darwin"


def normalize_platform(platform: str | None = None) -> str:
    value = default_platform() if platform is None else str(platform)
    if value.startswith("win32") or value == "nt":
        return "win32"
    if value in {"darwin", "mac", "macos"} or value.startswith("darwin"):
        return "darwin"
    return value


def default_binding(platform: str | None = None) -> "Binding":
    """Fresh default: Right Option on macOS, F8 on Windows."""
    return parse("RightAlt" if normalize_platform(platform) == "darwin" else "F8",
                 platform=platform)


def _squash(token: str) -> str:
    return "".join(character for character in token if character.isalnum()).lower()


def _resolve_modifier(token: str) -> str | None:
    return MODIFIER_ALIASES.get(_squash(token))


def _resolve_key(token: str) -> str | None:
    squashed = _squash(token)
    if not squashed:
        return None
    for name in BASE_KEYS:
        if name.lower() == squashed:
            return name
    return KEY_ALIASES.get(squashed)


@dataclass(frozen=True)
class Binding:
    """A parsed shortcut. ``key`` is ``None`` for a modifier-only binding."""

    modifiers: tuple[str, ...]
    key: str | None

    @property
    def modifier_only(self) -> bool:
        return self.key is None

    @property
    def sided(self) -> bool:
        return any(name in SIDED_MODIFIERS for name in self.modifiers)

    @property
    def canonical(self) -> str:
        if self.key is None:
            return self.modifiers[0]
        return "+".join((*self.modifiers, self.key))

    @property
    def base_modifiers(self) -> frozenset[str]:
        return frozenset(SIDED_MODIFIERS.get(name, name) for name in self.modifiers)

    def display(self, platform: str | None = None) -> str:
        platform = normalize_platform(platform)
        if self.modifier_only:
            name = self.modifiers[0]
            if platform == "darwin":
                return f"{_side_word(name)} {_mac_modifier_word(name)}"
            return name
        if platform == "darwin":
            symbols = "".join(_MAC_SYMBOLS[SIDED_MODIFIERS.get(name, name)]
                              for name in self.modifiers)
            core = f"{symbols} + {self.key}" if symbols else str(self.key)
            detail = ", ".join(_side_word(name) for name in self.modifiers if name in SIDED_MODIFIERS)
            return core + (f" ({detail}键)" if detail else "")
        words = [_WINDOWS_WORDS[SIDED_MODIFIERS.get(name, name)] for name in self.modifiers]
        return "+".join([*words, str(self.key)])

    def legacy_key(self) -> str | None:
        """The 1.0-style key string for a plain function key, else ``None``."""
        if self.modifiers or self.key is None:
            return None
        if self.key.startswith("F") and self.key[1:].isdigit() and 1 <= int(self.key[1:]) <= 12:
            return self.key
        return None

    def as_dict(self, platform: str | None = None) -> dict[str, Any]:
        platform = normalize_platform(platform)
        return {
            "binding": self.canonical,
            "display": self.display(platform),
            "modifiers": list(self.modifiers),
            "key": self.key,
            "modifier_only": self.modifier_only,
            "platform": platform,
        }


_MAC_SYMBOLS = {"Control": "⌃", "Alt": "⌥", "Shift": "⇧", "Meta": "⌘"}
_WINDOWS_WORDS = {"Control": "Ctrl", "Alt": "Alt", "Shift": "Shift", "Meta": "Win"}
_MAC_MODIFIER_WORDS = {"Control": "Control", "Alt": "Option", "Shift": "Shift", "Meta": "Command"}


def _side_word(name: str) -> str:
    if name.startswith("Left"):
        return "左"
    if name.startswith("Right"):
        return "右"
    return ""


def _mac_modifier_word(name: str) -> str:
    return _MAC_MODIFIER_WORDS[SIDED_MODIFIERS.get(name, name)]


def modifiers_match(binding: Binding, active: Iterable[str]) -> bool:
    """True when ``active`` (side-qualified where known) satisfies ``binding``.

    The comparison is exact: a different modifier set does not match, so a plain
    ``F8`` binding is not stolen by ``Command+F8`` and ``Shift+F9`` is not fired
    by ``Command+Shift+F9``.
    """
    active = frozenset(active)
    if frozenset(SIDED_MODIFIERS.get(name, name) for name in active) != binding.base_modifiers:
        return False
    for name in binding.modifiers:
        if name in SIDED_MODIFIERS:
            if name not in active:
                return False
        elif not any(SIDED_MODIFIERS.get(entry, entry) == name for entry in active):
            return False
    return True


def parse(value: str | Binding | None, *, platform: str | None = None) -> Binding:
    """Parse and validate one binding for ``platform`` (default: this host)."""
    platform = normalize_platform(platform)
    if isinstance(value, Binding):
        validate_binding(value, platform=platform)
        return value
    text = "" if value is None else str(value).strip()
    if not text:
        raise ShortcutError("empty", "没有读到按键；请按下一个支持的按键或组合。")
    tokens = [token.strip() for token in text.replace("–", "-").split("+")]
    if any(not token for token in tokens):
        raise ShortcutError("empty_token", f"无法识别“{text}”中的空按键段。")
    modifiers: list[str] = []
    keys: list[str] = []
    for token in tokens:
        modifier = _resolve_modifier(token)
        if modifier is not None:
            if modifier in modifiers:
                raise ShortcutError("duplicate_modifier", f"修饰键“{token}”重复了。")
            modifiers.append(modifier)
            continue
        key = _resolve_key(token)
        if key is None:
            function_range = "F1–F11" if platform == "win32" else "F1–F12"
            raise ShortcutError("unsupported_token",
                                f"不支持“{token}”。可用：功能键 {function_range}、字母、数字，"
                                f"加 Control/Option/Shift/Command（Windows 为 Ctrl/Alt/Win）。")
        keys.append(key)
    if len(keys) > 1:
        raise ShortcutError("multiple_keys",
                            f"一次只能绑定一个主键，读到 {len(keys)} 个：{', '.join(keys)}。")
    if len(modifiers) > 4:
        raise ShortcutError("too_many_modifiers", "最多支持四个修饰键。")
    if not keys:
        if len(modifiers) != 1:
            raise ShortcutError(
                "ambiguous_modifier_only",
                "只支持单个修饰键作为独立快捷键（例如右 Option）；"
                "多个修饰键请再带一个主键。")
        binding = Binding((modifiers[0],), None)
    else:
        ordered = tuple(sorted(modifiers, key=_modifier_sort_key))
        binding = Binding(ordered, keys[0])
    validate_binding(binding, platform=platform)
    return binding


def _modifier_sort_key(name: str) -> tuple[int, int]:
    base = SIDED_MODIFIERS.get(name, name)
    order = MODIFIER_ORDER.index(base) if base in MODIFIER_ORDER else len(MODIFIER_ORDER)
    side = 0 if name.startswith("Left") else 1 if name.startswith("Right") else 2
    return order, side


def validate_binding(binding: Binding, *, platform: str | None = None) -> None:
    """Raise :class:`ShortcutError` when the backend cannot implement it."""
    platform = normalize_platform(platform)
    if binding.key is None:
        if len(binding.modifiers) != 1:
            raise ShortcutError("ambiguous_modifier_only",
                                "只支持单个修饰键作为独立快捷键。")
        if binding.modifiers[0] not in SIDED_MODIFIERS:
            raise ShortcutError(
                "modifier_only_not_sided",
                f"“{binding.modifiers[0]}”左右两键无法区分；"
                f"要按住修饰键说话，请选左侧或右侧按键（例如 RightOption）。")
        if platform != "darwin":
            raise ShortcutError(
                "modifier_only_unsupported",
                "单独一个修饰键只能用于 macOS 录音快捷键；Windows 请带一个主键，例如 F8 或 Ctrl+Shift+K。")
        return
    if binding.key in RESERVED_KEYS:
        raise ShortcutError(*RESERVED_KEYS[binding.key])
    if platform == "win32" and binding.key in WINDOWS_RESERVED_KEYS:
        raise ShortcutError(*WINDOWS_RESERVED_KEYS[binding.key])
    if binding.key not in BASE_KEY_SET:
        raise ShortcutError("unsupported_key", f"不支持主键“{binding.key}”。")
    if platform != "darwin" and binding.sided:
        raise ShortcutError(
            "sided_modifier_unsupported",
            "Windows 无法区分左右修饰键；请使用 Ctrl、Alt、Shift、Win 的通用写法。")


def platform_base_keys(platform: str | None = None) -> tuple[str, ...]:
    """Base keys this platform can bind (F12 is reserved on Windows)."""
    if normalize_platform(platform) == "win32":
        return tuple(name for name in BASE_KEYS if name not in WINDOWS_RESERVED_KEYS)
    return BASE_KEYS


def platform_reserved_keys(platform: str | None = None) -> list[str]:
    names = set(RESERVED_KEYS)
    if normalize_platform(platform) == "win32":
        names.update(WINDOWS_RESERVED_KEYS)
    return sorted(names)


def conflicts(binding: Binding, *, platform: str | None = None) -> list[str]:
    """Known OS/Premiere conflicts for a binding; warnings, never blocks."""
    platform = normalize_platform(platform)
    notes: list[str] = []
    known = KNOWN_CONFLICTS.get(binding.canonical)
    if known:
        notes.append(f"{binding.canonical} 常被系统或应用占用（{known}），可能被提前拦截。")
    if platform == "darwin" and binding.modifier_only:
        notes.append("单独修饰键的录音会在按下时立即开始；若随后按下别的按键（比如做快捷键），"
                     "本次录音会被取消且不生成意见。")
    if platform == "darwin" and not binding.modifier_only:
        notes.append("macOS 的按键监听只观察、不拦截：Premiere 或其他应用仍可能收到同一个按键。")
    if platform == "win32":
        notes.append("Windows 会全局注册此组合，Premiere 将收不到它；请在 Premiere 的"
                     "“键盘快捷键”中确认没有别的用途。")
    return notes


def describe_support(platform: str | None = None) -> dict[str, Any]:
    """What this platform can bind, for ``cuthere shortcut grammar`` and status."""
    platform = normalize_platform(platform)
    return {
        "platform": platform,
        "default_binding": default_binding(platform).canonical,
        "default_display": default_binding(platform).display(platform),
        "modifiers": list(MODIFIER_ORDER),
        "aliases": {"Control": ["Ctrl"], "Alt": ["Option", "Opt"],
                    "Shift": ["Shift"], "Meta": ["Cmd", "Command", "Win", "Super"]},
        "sided_modifiers_allowed": platform == "darwin",
        "modifier_only_allowed": platform == "darwin",
        "base_keys": list(platform_base_keys(platform)),
        "reserved_keys": platform_reserved_keys(platform),
        "max_modifiers": 4,
        "limits": _platform_limits(platform),
    }


def _platform_limits(platform: str) -> list[str]:
    if platform == "darwin":
        return [
            "事件监听是只观察的：Cuthere 不会拦截按键，Premiere 仍会收到它，冲突时两边都会响应。",
            "修饰键经由 flagsChanged 事件识别，左右 Option/Control/Command/Shift 可以区分。",
            "Fn / Globe 与 Escape 不提供绑定。",
        ]
    if platform == "win32":
        return [
            "使用 RegisterHotKey 全局注册：被注册的组合不会再送到 Premiere。",
            "无法区分左右修饰键，也不支持单独一个修饰键作为快捷键。",
            "F12 被 Windows 保留给调试器，因此不提供绑定（macOS 不受此限制）。",
            "Fn / Globe 与 Escape 不提供绑定；其他被系统占用的组合仍可能注册失败，"
            "失败会在面板、命令行和 ptt_key_diagnostic.json 中报告，注册失败以 Windows 为准。",
        ]
    return ["此平台没有可用的按键后端。"]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@dataclass
class ShortcutState:
    """The value that is actually in force right now."""

    binding: Binding
    source: str
    status: str
    config_path: Path
    updated_at: str
    previous_binding: str | None = None
    problems: tuple[str, ...] = ()
    migrated_from: str | None = None
    recovered_from: str | None = None

    def snapshot(self, platform: str | None = None) -> dict[str, Any]:
        platform = normalize_platform(platform)
        return {
            **self.binding.as_dict(platform),
            "source": self.source,
            "status": self.status,
            "config_path": str(self.config_path),
            "updated_at": self.updated_at,
            "previous_binding": self.previous_binding,
            "migrated_from": self.migrated_from,
            "recovered_from": self.recovered_from,
            "problems": list(self.problems),
        }


class ShortcutStore:
    """Reads and writes the single authoritative per-user shortcut file."""

    def __init__(self, config_path: Path | str, *, platform: str | None = None):
        self.path = Path(config_path)
        self.backup_path = self.path.with_name(BACKUP_NAME)
        self.platform = normalize_platform(platform)
        self._cached: ShortcutState | None = None
        self._cached_mtime: int | None = None
        #: A backup failure does not undo the setting that was just written, but
        #: it is kept for diagnostics instead of disappearing.
        self.last_backup_error: str | None = None

    # -- paths -----------------------------------------------------------
    @classmethod
    def for_runtime_root(cls, runtime_root: Path | str, *, platform: str | None = None) -> "ShortcutStore":
        return cls(Path(runtime_root) / CONFIG_NAME, platform=platform)

    @classmethod
    def for_ipc_root(cls, ipc_root: Path | str, *, platform: str | None = None) -> "ShortcutStore":
        """Derive the per-user runtime root the same way the panel does.

        The shipped layout is ``<runtime>/ipc/f8-<version>``, so the config sits
        two levels above the IPC folder; a caller that already passes
        ``<runtime>/ipc`` gets it one level up. A portable or test IPC folder
        without either shape keeps the file next to the IPC data instead.
        """
        ipc = Path(ipc_root)
        if ipc.name == "ipc":
            root = ipc.parent
        elif ipc.parent.name == "ipc":
            root = ipc.parent.parent
        else:
            root = ipc
        return cls(root / CONFIG_NAME, platform=platform)

    # -- reading ---------------------------------------------------------
    def _read_record(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object")
        return value

    def _parse_record(self, record: dict[str, Any]) -> Binding:
        return parse(record.get("binding"), platform=self.platform)

    def load(self, *, force: bool = False) -> ShortcutState:
        """Return the effective binding, re-reading the file when it changes."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if (not force and self._cached is not None and mtime is not None
                and mtime == self._cached_mtime):
            return self._cached
        state = self._load_uncached()
        self._cached = state
        self._cached_mtime = mtime
        return state

    def _load_uncached(self) -> ShortcutState:
        problems: list[str] = []
        if not self.path.is_file():
            return self._default_state(problems)
        try:
            record = self._read_record(self.path)
            binding = self._parse_record(record)
        except (OSError, ValueError, ShortcutError, json.JSONDecodeError) as exc:
            problems.append(f"{self.path.name}: {type(exc).__name__}: {exc}")
            return self._recovered_state(problems)
        return ShortcutState(
            binding=binding,
            source=str(record.get("source") or "unknown"),
            status="ok",
            config_path=self.path,
            updated_at=str(record.get("updated_at") or ""),
            previous_binding=_optional_text(record.get("previous_binding")),
            migrated_from=_optional_text(record.get("migrated_from")),
        )

    def _default_state(self, problems: list[str]) -> ShortcutState:
        return ShortcutState(
            binding=default_binding(self.platform),
            source="default",
            status="default",
            config_path=self.path,
            updated_at="",
            problems=tuple(problems),
        )

    def _recovered_state(self, problems: list[str]) -> ShortcutState:
        try:
            record = self._read_record(self.backup_path)
            binding = self._parse_record(record)
        except (OSError, ValueError, ShortcutError, json.JSONDecodeError) as exc:
            problems.append(f"{self.backup_path.name}: {type(exc).__name__}: {exc}")
            return self._default_state(problems)
        return ShortcutState(
            binding=binding,
            source=str(record.get("source") or "unknown"),
            status="recovered",
            config_path=self.path,
            updated_at=str(record.get("updated_at") or ""),
            previous_binding=_optional_text(record.get("previous_binding")),
            migrated_from=_optional_text(record.get("migrated_from")),
            recovered_from=BACKUP_NAME,
            problems=tuple(problems),
        )

    # -- writing ---------------------------------------------------------
    def set(self, value: str | Binding, *, source: str = "cli", dry_run: bool = False) -> ShortcutState:
        current = self.load(force=True)
        binding = parse(value, platform=self.platform)
        state = ShortcutState(
            binding=binding,
            source=source,
            status="ok",
            config_path=self.path,
            updated_at=utc_now(),
            previous_binding=current.binding.canonical,
            migrated_from=current.migrated_from,
            problems=(),
        )
        if dry_run:
            return state
        self._write(state)
        return state

    def reset(self, *, source: str = "reset", dry_run: bool = False) -> ShortcutState:
        current = self.load(force=True)
        state = ShortcutState(
            binding=default_binding(self.platform),
            source=source,
            status="ok",
            config_path=self.path,
            updated_at=utc_now(),
            previous_binding=current.binding.canonical,
            problems=(),
        )
        if dry_run:
            return state
        self._write(state)
        return state

    def restore_previous(self, *, source: str = "cli", dry_run: bool = False) -> ShortcutState:
        current = self.load(force=True)
        previous = current.previous_binding
        if not previous:
            raise ShortcutError("no_previous_binding", "没有可恢复的上一次快捷键设置。")
        return self.set(previous, source=source, dry_run=dry_run)

    def migrate_legacy(self, legacy_value: str | None, *, source: str = SOURCE_MIGRATION,
                       dry_run: bool = False) -> ShortcutState | None:
        """Carry over an explicitly chosen 1.0 key; never invent a preference.

        The 1.0 panel only wrote ``localStorage`` when the user changed the
        select, so a stored value is an explicit choice. An absent value is not
        a choice: the caller keeps the platform default (Right Option on macOS).
        """
        if self.path.is_file():
            return None
        text = (legacy_value or "").strip()
        if not text:
            return None
        try:
            binding = parse(text, platform=self.platform)
        except ShortcutError:
            return None
        state = ShortcutState(
            binding=binding,
            source=source,
            status="ok",
            config_path=self.path,
            updated_at=utc_now(),
            previous_binding=None,
            migrated_from="localStorage:autovideoedit.review.ptt.hotkey",
            problems=(),
        )
        if dry_run:
            return state
        self._write(state)
        return state

    def _write(self, state: ShortcutState) -> None:
        record = {
            "schema_version": SCHEMA_VERSION,
            "binding": state.binding.canonical,
            "display": state.binding.display(self.platform),
            "source": state.source,
            "updated_at": state.updated_at,
            "previous_binding": state.previous_binding,
            "display_platform": self.platform,
            "history": self._next_history(state),
        }
        if state.migrated_from:
            record["migrated_from"] = state.migrated_from
        text = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        # ``previous_binding`` is the undo history; ``last-good`` is corruption
        # recovery. They are different guarantees: the backup must hold the
        # value that was just successfully written (F10), not the value before
        # it (F9), or a truncated update silently rolls the user back one step.
        # Write the current file first, so a failed update cannot replace the
        # last-good copy with a value that was never in force.
        _atomic_write(self.path, text)
        try:
            _atomic_write(self.backup_path, text)
            self.last_backup_error = None
        except OSError as exc:
            # The setting is already in force in shortcut.json; keep it and
            # report the backup failure separately instead of pretending the
            # write failed or silently discarding the diagnostic.
            self.last_backup_error = f"{type(exc).__name__}: {exc}"
        self._cached = None
        self._cached_mtime = None

    def _next_history(self, state: ShortcutState) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        # Current file first; after corruption the backup carries the history up
        # to the last successful write, so a recovery does not drop it.
        for source in (self.path, self.backup_path):
            try:
                record = self._read_record(source)
                raw = record.get("history")
                if isinstance(raw, list):
                    for item in raw[-9:]:
                        if isinstance(item, dict) and item.get("binding"):
                            entries.append({"binding": str(item["binding"]),
                                            "source": str(item.get("source") or "unknown"),
                                            "updated_at": str(item.get("updated_at") or "")})
                    if entries:
                        break
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        entries.append({"binding": state.binding.canonical, "source": state.source,
                        "updated_at": state.updated_at})
        return entries[-10:]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
