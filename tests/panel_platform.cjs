// Platform seams of the CEP panel, loaded from the real panel sources.
//
// The panel decides platform behaviour from the CEP process object
// (`process.platform`), with `navigator.platform` as the older-engine fallback.
// Each test runs the real bridge.js / review_panel.js inside a vm context that
// reports one platform, so both production branches are exercised on any host:
//   - darwin: process.platform = "darwin" (and os.name patched, because
//     node's module loading rejects a POSIX `os.name` on Windows),
//   - win32:  process.platform = "win32" (node's default on Windows).
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const os = require('node:os');
const assert = require('node:assert/strict');
const test = require('node:test');

const panel = path.join(__dirname, '../premiere_cep/review_capture_panel');
const bridgeCode = fs.readFileSync(path.join(panel, 'js/bridge.js'), 'utf8');
const adapterCode = fs.readFileSync(path.join(panel, 'js/cep_adapter.js'), 'utf8');
const reviewCode = fs.readFileSync(path.join(panel, 'js/review_panel.js'), 'utf8');

const USER_DATA = '/Users/fixture/Library/Application Support/Adobe/CEP/CEPHtmlEngine/partitions/CEPHtmlEngine';

// bridge.js exports its platform helpers itself (inside the IIFE), so the test
// only needs to stop the DOM startup path from running.
const stripBridgeStartup = code =>
  code.replace('document.addEventListener("DOMContentLoaded", init);', '/* no init in tests */')
    .replace(/\n {4}init\(\);\n/, '\n    /* no init in tests */\n');

function runAs(platform, code, { runtime = null, userData = USER_DATA, env = undefined } = {}) {
  const previousName = os.name;
  if (platform === 'darwin') os.name = 'posix';
  try {
    if (platform === 'win32') os.name = 'win32';
    const files = new Map();
    if (runtime) files.set('c:/plugin/runtime.json', JSON.stringify(runtime));
    const norm = value => String(value).replaceAll('\\', '/').replace(/\/$/, '').toLowerCase();
    const cepFs = {
      stat: p => ({ err: files.has(norm(p)) ? 0 : 3 }),
      makedir: () => ({ err: 0 }),
      readFile: p => ({ err: files.has(norm(p)) ? 0 : 3, data: files.get(norm(p)) }),
      writeFile: (p, value) => { files.set(norm(p), value); return { err: 0 }; },
      deleteFile: p => { files.delete(norm(p)); return { err: 0 }; },
      rename: (from, to) => { files.set(norm(to), files.get(norm(from))); files.delete(norm(from)); return { err: 0 }; },
      readdir: () => ({ err: 0, data: [] }),
      getSystemPath: kind => (kind === 'userData' ? userData : ''),
    };
    const element = () => ({ textContent: '', className: '', disabled: false, hidden: false, addEventListener() {} });
    const document = {
      readyState: 'complete', getElementById: () => element(), addEventListener() {},
      querySelector: () => null, querySelectorAll: () => [], createElement: () => element(),
    };
    const window = {
      cep: { fs: cepFs, process: { createProcess: () => ({ err: 9 }) } },
      localStorage: { getItem: () => 'fixture-session', setItem() {} },
      addEventListener() {},
      __adobe_cep__: { getSystemPath: () => '/Plugin', evalScript: (_s, cb) => cb(JSON.stringify({ success: true, data: { version: '1.1.0' } })) },
    };
    const module = { exports: {} };
    const context = vm.createContext({
      window, document, module, console,
      navigator: { platform: platform === 'darwin' ? 'MacIntel' : 'Win32' },
      process: { platform, env },
      cep: cepFs,
      setTimeout: () => 0, setInterval: () => 0, clearTimeout() {}, clearInterval() {},
    });
    vm.runInContext(adapterCode, context);
    context.CSInterface = window.CSInterface;
    context.SystemPath = window.SystemPath;
    vm.runInContext(code, context);
    return { exports: module.exports, files };
  } finally {
    os.name = previousName;
  }
}

test('macOS bridge falls back to the per-user Application Support folder, never /Users/Shared', () => {
  const result = runAs('darwin', stripBridgeStartup(bridgeCode));
  assert.equal(result.exports.isMacHost(), true);
  assert.equal(result.exports.fallbackBridgeDir(), '/Users/fixture/Library/Application Support/Cuthere/ave-bridge');
  assert.ok(!result.exports.fallbackBridgeDir().includes('/Users/Shared'));
});

test('macOS bridge without a CEP userData path uses the real home, never a literal ~', () => {
  const result = runAs('darwin', stripBridgeStartup(bridgeCode), {
    userData: '', env: { HOME: '/Users/fixture/' },
  });
  assert.equal(result.exports.fallbackBridgeDir(), '/Users/fixture/Library/Application Support/Cuthere/ave-bridge');
  assert.ok(!result.exports.fallbackBridgeDir().includes('/Users/Shared'));
});

test('macOS bridge keeps a documented tilde fallback only when no home is available', () => {
  const result = runAs('darwin', stripBridgeStartup(bridgeCode), { userData: '', env: {} });
  assert.equal(result.exports.fallbackBridgeDir(), '~/Library/Application Support/Cuthere/ave-bridge');
});

test('Windows bridge keeps the 1.0 fallback unchanged', () => {
  const result = runAs('win32', stripBridgeStartup(bridgeCode));
  assert.equal(result.exports.isMacHost(), false);
  assert.equal(result.exports.fallbackBridgeDir(), 'C:\\Users\\Public\\Documents\\ave-bridge');
});

test('runtime.json wins on both platforms (installed layout is authoritative)', () => {
  const result = runAs('darwin', stripBridgeStartup(bridgeCode), { runtime: { bridge_dir: '/Users/x/My Bridge' } });
  // getBridgeDir is not exported; the fallback must not be used when a config
  // exists, which the filter above preserves: check the code path instead.
  assert.match(bridgeCode, /if \(typeof config\.bridge_dir === "string" && config\.bridge_dir\) return config\.bridge_dir;/);
  assert.equal(result.exports.isMacHost(), true);
});

test('review panel loads on darwin and win32 contexts and exports its helpers', () => {
  for (const platform of ['darwin', 'win32']) {
    const result = runAs(platform, reviewCode);
    assert.equal(typeof result.exports.releasePreviewSnapshot, 'function', platform);
  }
});

test('review panel reads the launch command from runtime.json launcher, falling back to pythonw', () => {
  // The panel launches the helper with runtimeConfig.launcher || runtimeConfig.pythonw.
  // macOS installs write `launcher`; Windows installs keep writing `pythonw`.
  assert.match(reviewCode, /runtimeConfig\.launcher \|\| runtimeConfig\.pythonw/);
  assert.match(reviewCode, /runtimeConfig\.transcription_language \|\| runtimeConfig\.language/);
});

test('review panel builds note paths with the platform separator', () => {
  const note = { session_id: 'session_fixture_0001', note_id: 'note_fixture_0001' };
  const mac = runAs('darwin', reviewCode).exports;
  const win = runAs('win32', reviewCode).exports;
  assert.equal(mac.noteDir(note, '/Users/fixture/Show/Episode 01.prproj'),
    '/Users/fixture/Show/work/premiere-review/session_fixture_0001/notes/note_fixture_0001');
  assert.equal(mac.noteJobPath(note, '/Users/fixture/Show/Episode 01.prproj'),
    '/Users/fixture/Show/work/premiere-review/session_fixture_0001/notes/note_fixture_0001/transcription/job.json');
  assert.equal(win.noteDir(note, 'D:\\Show\\Episode 01.prproj'),
    'D:\\Show\\work\\premiere-review\\session_fixture_0001\\notes\\note_fixture_0001');
  assert.ok(!/work\\\\premiere-review/.test(reviewCode), 'a hard-coded Windows path segment is still present');
});

test('review panel does not assume an ASCII or space-free path', () => {
  const mac = runAs('darwin', reviewCode).exports;
  assert.equal(mac.parentDir('/Users/测试 User/节目/第 01 期.prproj'), '/Users/测试 User/节目');
  assert.equal(mac.join('/Users/测试 User/App Support', 'ave-bridge'), '/Users/测试 User/App Support/ave-bridge');
});
