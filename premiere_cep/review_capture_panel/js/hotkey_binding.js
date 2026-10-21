// Shared shortcut grammar for the CEP panel.
//
// This is the JavaScript mirror of services/review_capture/shortcut.py. The
// Python module owns validation and persistence; this file only produces the
// same canonical string, the same readable preview and the same rejection codes
// so the recorder can show a preview before anything is saved and so the panel
// can render what the helper actually stored. Both sides are covered by the same
// test vectors (tests/review/test_shortcut_binding.py and
// tests/panel_shortcut.cjs read tests/fixtures/shortcut_vectors.json).
(function (root, factory) {
    "use strict";
    var api = factory();
    if (typeof module !== "undefined" && module.exports) { module.exports = api; }
    if (root) { root.CuthereShortcut = api; }
}(typeof window !== "undefined" ? window : null, function () {
    "use strict";

    var MODIFIER_ORDER = ["Control", "Alt", "Shift", "Meta"];
    var SIDED = {};
    MODIFIER_ORDER.forEach(function (base) {
        SIDED["Left" + base] = base;
        SIDED["Right" + base] = base;
    });
    var BASE_KEYS = [];
    for (var n = 1; n <= 12; n += 1) { BASE_KEYS.push("F" + n); }
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").forEach(function (c) { BASE_KEYS.push(c); });
    "0123456789".split("").forEach(function (c) { BASE_KEYS.push(c); });
    ["Space", "Enter", "Tab", "Backspace", "Insert", "Delete", "Home", "End",
        "PageUp", "PageDown", "Up", "Down", "Left", "Right"].forEach(function (k) { BASE_KEYS.push(k); });

    var MODIFIER_ALIASES = {
        ctrl: "Control", control: "Control",
        alt: "Alt", option: "Alt", opt: "Alt", shift: "Shift",
        meta: "Meta", cmd: "Meta", command: "Meta", win: "Meta", super: "Meta",
        leftctrl: "LeftControl", leftcontrol: "LeftControl",
        rightctrl: "RightControl", rightcontrol: "RightControl",
        leftalt: "LeftAlt", leftoption: "LeftAlt", leftopt: "LeftAlt", lopt: "LeftAlt",
        rightalt: "RightAlt", rightoption: "RightAlt", rightopt: "RightAlt", ropt: "RightAlt",
        leftshift: "LeftShift", rightshift: "RightShift",
        leftmeta: "LeftMeta", leftcmd: "LeftMeta", leftcommand: "LeftMeta", lcmd: "LeftMeta",
        rightmeta: "RightMeta", rightcmd: "RightMeta", rightcommand: "RightMeta", rcmd: "RightMeta"
    };
    var KEY_ALIASES = {
        return: "Enter", enter: "Enter", tab: "Tab",
        del: "Delete", backspace: "Backspace", bksp: "Backspace",
        pgup: "PageUp", pageup: "PageUp", pgdn: "PageDown", pagedown: "PageDown",
        arrowup: "Up", up: "Up", arrowdown: "Down", down: "Down",
        arrowleft: "Left", left: "Left", arrowright: "Right", right: "Right",
        spacebar: "Space", space: "Space", esc: "Escape", escape: "Escape",
        fn: "Fn", globe: "Fn"
    };
    var RESERVED_KEYS = {
        Escape: { code: "reserved_escape", message: "Escape 用于取消录制，不能作为录音快捷键；请换一个按键。" },
        Fn: { code: "reserved_fn", message: "Fn / Globe 键不能可靠地作为录音快捷键；请用其他按键。" }
    };
    // Windows keeps F12 for debuggers; RegisterHotKey documentation says it must
    // not be registered. macOS can still bind it.
    var WINDOWS_RESERVED_KEYS = {
        F12: { code: "reserved_windows_f12",
            message: "Windows 把 F12 保留给调试器（Microsoft 要求不要注册），不能作为录音快捷键；请改用 F1–F11 或其他按键。" }
    };
    var KNOWN_CONFLICTS = {
        "Control+Space": "输入法切换",
        "Meta+Space": "Spotlight / 聚焦",
        "Meta+Tab": "应用切换",
        "Meta+Q": "退出应用",
        "Meta+W": "关闭窗口",
        "Meta+L": "锁定屏幕",
        "Alt+Tab": "窗口切换",
        "Control+Alt+Delete": "系统安全界面"
    };
    var MAC_SYMBOLS = { Control: "⌃", Alt: "⌥", Shift: "⇧", Meta: "⌘" };
    var WINDOWS_WORDS = { Control: "Ctrl", Alt: "Alt", Shift: "Shift", Meta: "Win" };
    var MAC_WORDS = { Control: "Control", Alt: "Option", Shift: "Shift", Meta: "Command" };

    function normalizePlatform(platform) {
        var value = String(platform || "");
        if (/^win/i.test(value) || value === "nt") { return "win32"; }
        if (/^mac|darwin/i.test(value)) { return "darwin"; }
        return value || "win32";
    }
    function squash(token) { return String(token).replace(/[^0-9a-z\u4e00-\u9fff]/gi, "").toLowerCase(); }
    function modBase(name) { return SIDED[name] || name; }
    function sideWord(name) { return name.indexOf("Left") === 0 ? "左" : (name.indexOf("Right") === 0 ? "右" : ""); }
    function rejection(code, message) {
        var error = new Error(message);
        error.code = code;
        error.message = message;
        return error;
    }
    function sortKey(name) {
        var base = modBase(name);
        var order = MODIFIER_ORDER.indexOf(base);
        var side = name.indexOf("Left") === 0 ? 0 : (name.indexOf("Right") === 0 ? 1 : 2);
        return (order < 0 ? MODIFIER_ORDER.length : order) * 10 + side;
    }
    function canonicalOf(modifiers, key) {
        return key === null || key === undefined ? modifiers[0] : modifiers.concat([key]).join("+");
    }
    function displayOf(binding, platform) {
        platform = normalizePlatform(platform);
        if (binding.modifier_only) {
            var only = binding.modifiers[0];
            return platform === "darwin" ? sideWord(only) + " " + MAC_WORDS[modBase(only)] : only;
        }
        if (platform === "darwin") {
            var symbols = binding.modifiers.map(function (name) { return MAC_SYMBOLS[modBase(name)]; }).join("");
            var core = symbols ? symbols + " + " + binding.key : String(binding.key);
            var detail = binding.modifiers.filter(function (name) { return SIDED[name]; })
                .map(sideWord).join(", ");
            return core + (detail ? " (" + detail + "键)" : "");
        }
        var words = binding.modifiers.map(function (name) { return WINDOWS_WORDS[modBase(name)]; });
        return words.concat([String(binding.key)]).join("+");
    }
    function makeBinding(modifiers, key, platform) {
        return {
            modifiers: modifiers,
            key: key === undefined ? null : key,
            modifier_only: key === null || key === undefined,
            platform: normalizePlatform(platform),
            get binding() { return canonicalOf(this.modifiers, this.key); },
            get display() { return displayOf(this, this.platform); }
        };
    }

    function validate(binding, platform) {
        platform = normalizePlatform(platform);
        if (binding.modifier_only) {
            if (binding.modifiers.length !== 1) {
                throw rejection("ambiguous_modifier_only", "只支持单个修饰键作为独立快捷键。");
            }
            if (!SIDED[binding.modifiers[0]]) {
                throw rejection("modifier_only_not_sided",
                    "“" + binding.modifiers[0] + "”左右两键无法区分；要按住修饰键说话，请选左侧或右侧按键（例如 RightOption）。");
            }
            if (platform !== "darwin") {
                throw rejection("modifier_only_unsupported",
                    "单独一个修饰键只能用于 macOS 录音快捷键；Windows 请带一个主键，例如 F8 或 Ctrl+Shift+K。");
            }
            return binding;
        }
        if (RESERVED_KEYS[binding.key]) { throw rejection(RESERVED_KEYS[binding.key].code, RESERVED_KEYS[binding.key].message); }
        if (platform === "win32" && WINDOWS_RESERVED_KEYS[binding.key]) {
            throw rejection(WINDOWS_RESERVED_KEYS[binding.key].code, WINDOWS_RESERVED_KEYS[binding.key].message);
        }
        if (BASE_KEYS.indexOf(binding.key) < 0) {
            throw rejection("unsupported_key", "不支持主键“" + binding.key + "”。");
        }
        if (platform !== "darwin" && binding.modifiers.some(function (name) { return !!SIDED[name]; })) {
            throw rejection("sided_modifier_unsupported",
                "Windows 无法区分左右修饰键；请使用 Ctrl、Alt、Shift、Win 的通用写法。");
        }
        return binding;
    }

    function parse(value, platform) {
        platform = normalizePlatform(platform);
        if (value && typeof value === "object" && value.modifiers) {
            return validate(makeBinding(value.modifiers.slice(), value.key === undefined ? null : value.key, platform), platform);
        }
        var text = (value === null || value === undefined) ? "" : String(value).trim();
        if (!text) { throw rejection("empty", "没有读到按键；请按下一个支持的按键或组合。"); }
        var tokens = text.split("+").map(function (token) { return token.trim(); });
        if (tokens.some(function (token) { return !token; })) {
            throw rejection("empty_token", "无法识别“" + text + "”中的空按键段。");
        }
        var modifiers = [];
        var keys = [];
        tokens.forEach(function (token) {
            var modifier = MODIFIER_ALIASES[squash(token)];
            if (modifier) {
                if (modifiers.indexOf(modifier) >= 0) {
                    throw rejection("duplicate_modifier", "修饰键“" + token + "”重复了。");
                }
                modifiers.push(modifier);
                return;
            }
            var key = null;
            for (var i = 0; i < BASE_KEYS.length; i += 1) {
                if (BASE_KEYS[i].toLowerCase() === squash(token)) { key = BASE_KEYS[i]; break; }
            }
            if (!key) { key = KEY_ALIASES[squash(token)] || null; }
            if (!key) {
                throw rejection("unsupported_token",
                    "不支持“" + token + "”。可用：功能键 " + (platform === "win32" ? "F1–F11" : "F1–F12") +
                    "、字母、数字，加 Control/Option/Shift/Command（Windows 为 Ctrl/Alt/Win）。");
            }
            keys.push(key);
        });
        if (keys.length > 1) {
            throw rejection("multiple_keys", "一次只能绑定一个主键，读到 " + keys.length + " 个：" + keys.join(", ") + "。");
        }
        if (modifiers.length > 4) { throw rejection("too_many_modifiers", "最多支持四个修饰键。"); }
        if (!keys.length) {
            if (modifiers.length !== 1) {
                throw rejection("ambiguous_modifier_only",
                    "只支持单个修饰键作为独立快捷键（例如右 Option）；多个修饰键请再带一个主键。");
            }
            return validate(makeBinding(modifiers, null, platform), platform);
        }
        modifiers.sort(function (a, b) { return sortKey(a) - sortKey(b); });
        return validate(makeBinding(modifiers, keys[0], platform), platform);
    }

    function defaultBinding(platform) {
        return parse(normalizePlatform(platform) === "darwin" ? "RightAlt" : "F8", platform);
    }

    function conflicts(binding, platform) {
        platform = normalizePlatform(platform);
        var notes = [];
        var known = KNOWN_CONFLICTS[binding.binding];
        if (known) {
            notes.push(binding.binding + " 常被系统或应用占用（" + known + "），可能被提前拦截。");
        }
        if (platform === "darwin" && binding.modifier_only) {
            notes.push("单独修饰键的录音会在按下时立即开始；若随后按下别的按键（比如做快捷键），本次录音会被取消且不生成意见。");
        }
        if (platform === "darwin" && !binding.modifier_only) {
            notes.push("macOS 的按键监听只观察、不拦截：Premiere 或其他应用仍可能收到同一个按键。");
        }
        if (platform === "win32") {
            notes.push("Windows 会全局注册此组合，Premiere 将收不到它；请在 Premiere 的“键盘快捷键”中确认没有别的用途。");
        }
        return notes;
    }

    // Map a DOM KeyboardEvent onto a grammar token. The panel recorder uses this
    // interactively: it is a preview only, never the saved value.
    var CODE_KEYS = {
        Space: "Space", Enter: "Enter", NumpadEnter: "Enter", Tab: "Tab",
        Backspace: "Backspace", Insert: "Insert", Delete: "Delete", Home: "Home", End: "End",
        PageUp: "PageUp", PageDown: "PageDown", ArrowUp: "Up", ArrowDown: "Down",
        ArrowLeft: "Left", ArrowRight: "Right", Escape: "Escape"
    };
    function tokenFromEvent(event) {
        var code = String(event.code || "");
        var key = String(event.key || "");
        if (/^Key[A-Z]$/.test(code)) { return code.slice(3); }
        if (/^Digit[0-9]$/.test(code)) { return code.slice(5); }
        if (/^F([1-9]|1[0-2])$/.test(code)) { return code; }
        if (CODE_KEYS[code]) { return CODE_KEYS[code]; }
        if (/^[A-Za-z]$/.test(key)) { return key.toUpperCase(); }
        if (/^[0-9]$/.test(key)) { return key; }
        if (key === " " || key === "Spacebar") { return "Space"; }
        if (key === "Esc") { return "Escape"; }
        return null;
    }
    // Chromium spells the side differently per key ("AltRight" but
    // "ShiftRight"), so the mapping is explicit instead of a prefix test.
    var MODIFIER_CODES = {
        ShiftLeft: "LeftShift", ShiftRight: "RightShift",
        ControlLeft: "LeftControl", ControlRight: "RightControl",
        AltLeft: "LeftAlt", AltRight: "RightAlt",
        MetaLeft: "LeftMeta", MetaRight: "RightMeta",
        OSLeft: "LeftMeta", OSRight: "RightMeta"
    };
    function modifierFromEvent(event) {
        var code = String((event && event.code) || "");
        var key = String((event && event.key) || "");
        var base = { Control: "Control", Alt: "Alt", Shift: "Shift", Meta: "Meta" }[key];
        if (!base) { return null; }
        if (MODIFIER_CODES[code]) { return MODIFIER_CODES[code]; }
        var side = "";
        if (event.location === 1) { side = "Left"; }
        else if (event.location === 2) { side = "Right"; }
        return side + base;
    }
    // The recorder state machine lives in review_panel.js; these two helpers only
    // translate one DOM event. `modifier` returns the side-qualified token of a
    // pressed modifier, `chord` the full combination of the pressed modifiers and
    // the base key of a non-modifier event.
    function modifier(event) { return modifierFromEvent(event || {}); }
    function chord(event, platform) {
        platform = normalizePlatform(platform);
        var key = tokenFromEvent(event || {});
        if (!key) {
            return { ok: false, code: "unsupported_token",
                message: "这个按键不能作为录音快捷键；请换一个功能键、字母或数字。" };
        }
        var names = { ctrlKey: "Control", altKey: "Alt", shiftKey: "Shift", metaKey: "Meta" };
        var modifiers = [];
        Object.keys(names).forEach(function (flag) {
            if (event && event[flag]) { modifiers.push(names[flag]); }
        });
        var text = modifiers.concat([key]).join("+");
        try {
            return { ok: true, preview: parse(text, platform), text: text };
        } catch (error) {
            return { ok: false, code: error.code, message: error.message };
        }
    }

    return {
        MODIFIER_ORDER: MODIFIER_ORDER,
        BASE_KEYS: BASE_KEYS,
        normalizePlatform: normalizePlatform,
        parse: parse,
        defaultBinding: defaultBinding,
        conflicts: conflicts,
        modifierToken: modifier,
        chordFromEvent: chord,
        isEscape: function (event) {
            return !!event && (event.key === "Escape" || event.key === "Esc" || event.code === "Escape");
        },
        describeSupport: function (platform) {
            platform = normalizePlatform(platform);
            var fallback = defaultBinding(platform);
            var baseKeys = BASE_KEYS.filter(function (name) {
                return platform !== "win32" || !WINDOWS_RESERVED_KEYS[name];
            });
            var reserved = Object.keys(RESERVED_KEYS);
            if (platform === "win32") {
                reserved = reserved.concat(Object.keys(WINDOWS_RESERVED_KEYS));
            }
            return {
                platform: platform,
                default_binding: fallback.binding,
                default_display: fallback.display,
                modifiers: MODIFIER_ORDER.slice(),
                sided_modifiers_allowed: platform === "darwin",
                modifier_only_allowed: platform === "darwin",
                base_keys: baseKeys,
                reserved_keys: reserved.sort()
            };
        }
    };
}));
