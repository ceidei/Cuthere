// The panel's shortcut recorder and the shared binding grammar.
//
// Two things are verified here:
//
// 1. `js/hotkey_binding.js` produces exactly the values in
//    tests/fixtures/shortcut_vectors.json - the same file the Python test uses,
//    so the panel preview and the helper's validation cannot drift apart.
// 2. The real `js/review_panel.js` recorder state machine, driven through a fake
//    CEP host: a stored value is adopted, a captured value is sent to the helper
//    as a `shortcut_set` request, the arm file is disabled while capturing, an
//    unsupported key is refused with a message, and a stale `localStorage`
//    preference can never overwrite the authoritative file.
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const test = require('node:test');

const root = path.join(__dirname, '..');
const panel = path.join(root, 'premiere_cep/review_capture_panel');
const binding = require(path.join(panel, 'js/hotkey_binding.js'));
const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/shortcut_vectors.json'), 'utf8'));

const adapterCode = fs.readFileSync(path.join(panel, 'js/cep_adapter.js'), 'utf8');
const bindingCode = fs.readFileSync(path.join(panel, 'js/hotkey_binding.js'), 'utf8');
const panelCode = fs.readFileSync(path.join(panel, 'js/review_panel.js'), 'utf8');

const IPC_ROOT = 'C:/Plugin/runtime/ipc/f8-1.1.0-alpha.5';
const APP_ROOT = 'C:/Plugin/runtime';
const norm = value => String(value).replaceAll('\\', '/').replace(/\/$/, '').toLowerCase();

const SNAPSHOT = {
  project: { path: 'F:/Shows/Episode 01.prproj', name: 'Episode 01' },
  sequence: { name: 'Cut v3', host_id: 'sequence-1' },
  location: { ticks: '1000', time_label: '00:00:01:00' },
  content_fingerprint: 'fnv32x2:abc',
  content_fingerprint_status: 'partial',
};

function makePanel({ legacyHotkey = null, storedShortcut = null, statusAge = 0 } = {}) {
  const files = new Map();
  const dirs = new Set();
  const nodes = new Map();
  const documentListeners = new Map();
  const windowListeners = new Map();
  const timers = new Map();
  const evalCalls = [];
  let nextTimer = 0;

  const node = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id, textContent: '', className: '', value: '', disabled: false, hidden: false,
        listeners: {},
        addEventListener(type, fn) { this.listeners[type] = fn; },
        click() { if (this.listeners.click) this.listeners.click({ preventDefault() {} }); },
      });
    }
    return nodes.get(id);
  };
  files.set(norm('C:/Plugin/runtime.json'), JSON.stringify({
    launcher: 'C:/Plugin/python.exe', worker_entry: 'C:/Plugin/worker_entry.py',
    ipc_root: IPC_ROOT, bridge_dir: 'C:/Bridge', transcription_language: 'zh',
  }));
  const writeStatus = () => files.set(norm(IPC_ROOT + '/status.json'), JSON.stringify({
    worker_version: '1.3.0', updated_at: new Date(Date.now() - statusAge).toISOString(),
    push_to_talk: null, push_to_talk_armed: true, push_to_talk_capability: { available: true, backend: 'windows-hotkey' },
  }));
  writeStatus();
  if (storedShortcut) files.set(norm(APP_ROOT + '/shortcut.json'), JSON.stringify(storedShortcut));

  const document = {
    readyState: 'complete',
    getElementById: id => node(id),
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: () => node('created'),
    addEventListener(type, fn) { documentListeners.set(type, fn); },
    removeEventListener(type) { documentListeners.delete(type); },
    body: { children: [], innerText: '' },
    styleSheets: [],
  };
  const window = {
    localStorage: {
      getItem: key => (key === 'autovideoedit.review.ptt.hotkey' ? legacyHotkey : null),
      setItem() {},
    },
    addEventListener(type, fn) { windowListeners.set(type, fn); },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', color: '#fff', backgroundColor: '#000' }),
    innerWidth: 420, innerHeight: 720,
    cep: {
      fs: {
        stat: p => ({ err: dirs.has(norm(p)) || files.has(norm(p)) ? 0 : 3 }),
        makedir: p => { dirs.add(norm(p)); return { err: 0 }; },
        readFile: p => ({ err: files.has(norm(p)) ? 0 : 3, data: files.get(norm(p)) }),
        writeFile: (p, value) => { files.set(norm(p), String(value)); return { err: 0 }; },
        deleteFile: p => { files.delete(norm(p)); return { err: 0 }; },
        rename: (from, to) => { files.set(norm(to), files.get(norm(from))); files.delete(norm(from)); return { err: 0 }; },
        readdir: () => ({ err: 0, data: [] }),
      },
      process: { createProcess: () => ({ err: 0 }) },
    },
    __adobe_cep__: {
      getSystemPath: () => 'C:/Plugin',
      evalScript(script, callback) {
        evalCalls.push(script);
        callback(JSON.stringify({ ok: true, data: SNAPSHOT }));
      },
    },
  };
  const module = { exports: {} };
  const context = vm.createContext({
    window, document, module, console,
    navigator: { platform: 'Win32' },
    process: { platform: 'win32' },
    cep: window.cep,
    setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
    setInterval(fn) { timers.set(++nextTimer, fn); return nextTimer; },
    clearTimeout(id) { timers.delete(id); },
    clearInterval(id) { timers.delete(id); },
    Date, JSON, Math, isFinite, String, Number, Object, Array, Error, RegExp,
  });
  vm.runInContext(adapterCode, context);
  context.CSInterface = window.CSInterface;
  context.SystemPath = window.SystemPath;
  vm.runInContext(bindingCode, context);
  // The panel reads the grammar as a global, exactly as a CEP <script> would
  // expose it; the module also sets window.CuthereShortcut for the browser.
  context.CuthereShortcut = window.CuthereShortcut;
  vm.runInContext(panelCode, context);

  const api = {
    files, nodes, node, context, writeStatus,
    tick(times = 1) { for (let i = 0; i < times; i += 1) for (const fn of [...timers.values()]) fn(); },
    start() { const fn = documentListeners.get('DOMContentLoaded'); assert.ok(fn, 'the panel must register init'); fn(); },
    keyEvent(type, event) {
      const fn = documentListeners.get(type);
      if (fn) fn({ preventDefault() {}, stopPropagation() {}, ...event });
      return Boolean(fn);
    },
    keydown(event) { return api.keyEvent('keydown', event); },
    keyup(event) { return api.keyEvent('keyup', event); },
    hasKeyListener() { return documentListeners.has('keydown'); },
    commands(action) {
      const found = [];
      for (const [key, value] of files) {
        if (!key.endsWith('.json') || value == null) continue;
        let parsed;
        try { parsed = JSON.parse(value); } catch { continue; }
        if (parsed && parsed.action && (!action || parsed.action === action)) found.push(parsed);
      }
      return found;
    },
    reply(command, result) {
      files.set(norm(IPC_ROOT + '/outbox/' + command.request_id + '.json'),
        JSON.stringify({ request_id: command.request_id, ok: true, result }));
    },
    replyError(command, code, message) {
      files.set(norm(IPC_ROOT + '/outbox/' + command.request_id + '.json'),
        JSON.stringify({ request_id: command.request_id, ok: false, error: 'ShortcutError', code, message }));
    },
    arm() { return JSON.parse(files.get(norm(IPC_ROOT + '/ptt_arm.json')) || 'null'); },
    lastCommand(action) { const list = api.commands(action); return list[list.length - 1]; },
    storageFiles: () => [...files.keys()],
  };
  return api;
}

test('the panel grammar mirrors the shared vectors on both platforms', () => {
  assert.ok(vectors.cases.length);
  for (const row of vectors.cases) {
    for (const platform of ['darwin', 'win32']) {
      const expected = row[platform];
      if (expected.code) {
        assert.throws(() => binding.parse(row.input, platform), error => error.code === expected.code,
          `${platform} ${JSON.stringify(row.input)} should fail with ${expected.code}`);
        continue;
      }
      const parsed = binding.parse(row.input, platform);
      assert.equal(parsed.binding, expected.binding, `${platform} ${row.input}`);
      assert.equal(parsed.display, expected.display, `${platform} ${row.input}`);
    }
  }
});

test('Right Option is the macOS default and F8 the Windows default', () => {
  assert.equal(binding.defaultBinding('darwin').binding, 'RightAlt');
  assert.equal(binding.defaultBinding('darwin').display, '右 Option');
  assert.equal(binding.defaultBinding('win32').binding, 'F8');
});

test('a DOM event maps onto the side-qualified modifier token', () => {
  assert.equal(binding.modifierToken({ key: 'Alt', code: 'AltRight', location: 2 }), 'RightAlt');
  assert.equal(binding.modifierToken({ key: 'Alt', code: 'AltLeft', location: 1 }), 'LeftAlt');
  assert.equal(binding.modifierToken({ key: 'Shift', code: 'ShiftRight' }), 'RightShift');
  assert.equal(binding.modifierToken({ key: 'k', code: 'KeyK' }), null);
  const chord = binding.chordFromEvent({ key: 'k', code: 'KeyK', ctrlKey: true, shiftKey: true }, 'darwin');
  assert.equal(chord.ok, true);
  assert.equal(chord.preview.binding, 'Control+Shift+K');
  assert.equal(binding.isEscape({ key: 'Escape', code: 'Escape' }), true);
});

test('the recorder saves the captured binding through the helper', () => {
  const f = makePanel();
  f.start();
  f.tick();
  // A fresh helper publishes no shortcut yet: the panel shows the mac default
  // only on macOS, so on this win32 context it shows F8.
  assert.equal(f.node('ptt-shortcut-display').textContent, 'F8');

  f.node('ptt-record').click();
  assert.equal(f.hasKeyListener(), true, 'the recorder must listen for keys');
  assert.equal(f.arm().enabled, false, 'hold-to-record is disabled while recording a key');
  assert.equal(f.node('ptt-save').disabled, true);

  f.keydown({ key: 'Control', code: 'ControlLeft', location: 1, ctrlKey: true });
  f.keydown({ key: 'k', code: 'KeyK', ctrlKey: true });
  assert.equal(f.node('ptt-shortcut-display').textContent, 'Ctrl+K');
  assert.equal(f.node('ptt-save').disabled, false);
  f.keyup({ key: 'k', code: 'KeyK' });
  f.keyup({ key: 'Control', code: 'ControlLeft' });

  f.node('ptt-save').click();
  const command = f.lastCommand('shortcut_set');
  assert.ok(command, 'saving must call the helper, not write a private preference');
  assert.equal(command.payload.binding, 'Control+K');

  f.reply(command, {
    binding: 'Control+K', display: 'Ctrl+K', source: 'panel', status: 'ok',
    effective: 'Control+K', pending: false, config_path: APP_ROOT + '/shortcut.json',
  });
  f.tick();
  assert.equal(f.node('ptt-shortcut-display').textContent, 'Ctrl+K');
  assert.match(f.node('ptt-shortcut-hint').textContent, /面板设置/);
  assert.equal(f.hasKeyListener(), false, 'saving closes the recorder');
  assert.equal(f.arm().shortcut, 'Control+K', 'the arm file carries the effective binding');
});

test('an unsupported key is refused with a readable message', () => {
  const f = makePanel();
  f.start();
  f.tick();
  f.node('ptt-record').click();
  f.keydown({ key: 'F13', code: 'F13' });
  assert.equal(f.node('ptt-save').disabled, true);
  assert.match(f.node('ptt-shortcut-hint').textContent, /不能作为录音快捷键|不支持/);
});

test('Escape cancels the recorder without changing anything', () => {
  const f = makePanel();
  f.start();
  f.tick();
  f.node('ptt-record').click();
  f.keydown({ key: 'F9', code: 'F9' });
  assert.equal(f.node('ptt-save').disabled, false);
  f.keydown({ key: 'Escape', code: 'Escape' });
  assert.equal(f.hasKeyListener(), false);
  assert.equal(f.node('ptt-shortcut-display').textContent, 'F8');
  assert.equal(f.lastCommand('shortcut_set'), undefined);
});

test('a refused save keeps the recorder open and shows the helper message', () => {
  const f = makePanel();
  f.start();
  f.tick();
  f.node('ptt-record').click();
  f.keydown({ key: 'k', code: 'KeyK' });
  f.node('ptt-save').click();
  const command = f.lastCommand('shortcut_set');
  f.replyError(command, 'reserved_escape', '这个组合不可用');
  f.tick();
  assert.equal(f.hasKeyListener(), true, 'the user can try another key');
  assert.match(f.node('ptt-shortcut-hint').textContent, /这个组合不可用/);
});

test('an agent change is adopted from the helper readback and never overwritten', () => {
  const f = makePanel({ legacyHotkey: 'F9', storedShortcut: { binding: 'Control+Shift+K', source: 'cli' } });
  f.start();
  f.tick(3);
  assert.equal(f.node('ptt-shortcut-display').textContent, 'Ctrl+Shift+K');
  // The stale browser preference must not be migrated over the stored value.
  assert.equal(f.lastCommand('shortcut_migrate'), undefined);
});

test('a fresh panel with an explicit old F9 asks the helper to migrate it once', () => {
  const f = makePanel({ legacyHotkey: 'F9' });
  f.start();
  f.tick(3);
  const command = f.lastCommand('shortcut_migrate');
  assert.ok(command, 'an explicit 1.0 choice must be carried over');
  assert.equal(command.payload.legacy_value, 'F9');
  f.reply(command, { binding: 'F9', display: 'F9', source: 'migration', status: 'ok', migrated: true });
  f.tick();
  assert.equal(f.node('ptt-shortcut-display').textContent, 'F9');
  assert.match(f.node('ptt-shortcut-hint').textContent, /沿用旧设置/);
});

test('a fresh panel without an old preference migrates nothing', () => {
  const f = makePanel();
  f.start();
  f.tick(3);
  assert.equal(f.lastCommand('shortcut_migrate'), undefined);
});
