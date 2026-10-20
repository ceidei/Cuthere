const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../premiere_cep/review_capture_panel/js/cep_adapter.js'), 'utf8');

test('native CEP calls receive scripts and decode local extension paths', () => {
  const seen = [];
  const window = {__adobe_cep__: {
    getSystemPath: kind => { assert.equal(kind, 'extension'); return 'file:///C:/Tools/F8%20Review/'; },
    evalScript: (script, callback) => { seen.push(script); callback('host-result'); }
  }};
  vm.runInNewContext(code, {window});
  const host = new window.CSInterface();
  assert.equal(host.getSystemPath(window.SystemPath.EXTENSION), 'C:/Tools/F8 Review/');
  host.evalScript('getSnapshot()', value => assert.equal(value, 'host-result'));
  host.evalScript('ping()');
  assert.deepEqual(seen, ['getSnapshot()', 'ping()']);
  window.__adobe_cep__.getSystemPath = () => 'file:///Users/example/F8%20Review';
  assert.equal(host.getSystemPath('extension'), '/Users/example/F8 Review');
});

test('missing native host raises rather than fabricating a response', () => {
  const window = {};
  vm.runInNewContext(code, {window});
  assert.throws(() => new window.CSInterface().evalScript('ping()'), /CEP host/);
});
