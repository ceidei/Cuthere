const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const root = path.join(__dirname, '..');
const panel = path.join(root, 'premiere_cep/review_capture_panel');
const code = fs.readFileSync(path.join(panel, 'js/bridge.js'), 'utf8');
const adapter = fs.readFileSync(path.join(panel, 'js/cep_adapter.js'), 'utf8');

function fixture({badVersion = false, denyMkdir = false} = {}) {
  const norm = value => value.replaceAll('\\', '/').replace(/\/$/, '');
  const files = new Map([['C:/Plugin/runtime.json', JSON.stringify({bridge_dir: 'C:\\Custom Bridge'})]]);
  const dirs = new Set();
  const elements = new Map();
  const events = new Map();
  const timers = new Map();
  const calls = [];
  const pending = [];
  let next = 0;
  const node = id => {
    if (!elements.has(id)) elements.set(id, {textContent: '', className: '', disabled: false, addEventListener(type, fn) {this[type] = fn;}});
    return elements.get(id);
  };
  const document = {readyState: 'complete', getElementById: node};
  const window = {addEventListener(type, fn) {events.set(type, fn);}, __adobe_cep__: {
    getSystemPath: () => 'C:/Plugin',
    evalScript(script, callback) {
      calls.push(script);
      if (script.startsWith('dispatchBridgeRequest(')) pending.push(callback);
      else callback(JSON.stringify({success: true, data: {version: badVersion ? 'old' : '1.1.0'}}));
    }
  }};
  const cep = {fs: {
    stat: p => ({err: dirs.has(norm(p)) || files.has(norm(p)) ? 0 : 3}),
    makedir(p) {if (denyMkdir) return {err: 5}; dirs.add(norm(p)); return {err: 0};},
    readFile: p => ({err: files.has(norm(p)) ? 0 : 3, data: files.get(norm(p))}),
    writeFile(p, value) {files.set(norm(p), value); return {err: 0};},
    deleteFile(p) {files.delete(norm(p)); return {err: 0};},
    readdir(p) {const prefix = norm(p) + '/'; return {err: 0, data: [...files.keys()].filter(x => x.startsWith(prefix) && !x.slice(prefix.length).includes('/')).map(x => x.slice(prefix.length))};}
  }};
  const context = vm.createContext({window, document, navigator: {platform: 'Win32'}, cep,
    setTimeout(fn) {timers.set(++next, fn); return next;}, setInterval(fn) {timers.set(++next, fn); return next;},
    clearTimeout(id) {timers.delete(id);}, clearInterval(id) {timers.delete(id);}});
  vm.runInContext(adapter, context);
  context.CSInterface = window.CSInterface;
  context.SystemPath = window.SystemPath;
  vm.runInContext(code, context);
  function command(id) {files.set('C:/Custom Bridge/commands/' + id + '.json', JSON.stringify({request_id: id, command: 'get_project_info', params: {}}));}
  function tick() {for (const fn of [...timers.values()]) fn();}
  return {files, dirs, node, events, timers, calls, pending, command, tick};
}

test('one extension contains the review UI, bridge, and all host resources', () => {
  const folders = fs.readdirSync(path.join(root, 'premiere_cep'));
  assert.deepEqual(folders, ['review_capture_panel']);
  const xml = fs.readFileSync(path.join(panel, 'CSXS/manifest.xml'), 'utf8');
  assert.equal([...xml.matchAll(/<Extension Id=/g)].length, 2); // one declaration and one dispatch
  assert.match(xml, /<Menu>Cuthere · 剪这儿<\/Menu>/);
  const html = fs.readFileSync(path.join(panel, 'index.html'), 'utf8');
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(x => x[1]);
  assert.equal(ids.length, new Set(ids).size);
  for (const match of html.matchAll(/(?:src|href)="((?:js|css)\/[^"#]+)"/g)) assert.ok(fs.existsSync(path.join(panel, match[1])), match[1]);
  assert.equal([...html.matchAll(/src="js\/bridge.js"/g)].length, 1);
  for (const match of code.matchAll(/\$\("([^"]+)"\)/g)) assert.ok(ids.includes(match[1]), match[1]);
  for (const file of ['hostscript.jsx', 'project_lifecycle.jsx', 'review_host.jsx']) assert.ok(fs.existsSync(path.join(panel, 'js', file)));
});

test('bridge starts in the same panel, uses configured IPC, serializes commands, and stops on close', () => {
  const f = fixture();
  assert.equal(f.node('bridge-status').textContent, '已就绪');
  assert.ok(f.dirs.has('C:/Custom Bridge/commands'));
  const load = f.calls.find(x => x.includes('$.evalFile'));
  assert.ok(load.includes('C:/Plugin/js/hostscript.jsx'));
  assert.ok(load.includes('C:/Plugin/js/project_lifecycle.jsx'));
  f.command('a'); f.command('b');
  f.tick(); f.tick();
  assert.equal(f.pending.length, 1);
  f.pending.shift()(JSON.stringify({success: true}));
  f.tick();
  assert.equal(f.pending.length, 1);
  assert.equal(f.node('commands-processed').textContent, 1);
  f.pending.shift()(JSON.stringify({success: true}));
  f.events.get('beforeunload')();
  f.command('c'); f.tick();
  assert.equal(f.pending.length, 0);
  assert.equal(f.node('bridge-status').textContent, '已暂停');
});

test('host mismatch and inaccessible command directories do not start polling', () => {
  for (const config of [{badVersion: true}, {denyMkdir: true}]) {
    const f = fixture(config);
    assert.match(f.node('bridge-status').textContent, /连接失败/);
    f.command('x'); f.tick();
    assert.equal(f.pending.length, 0);
    assert.equal(f.timers.size, 0);
  }
});
