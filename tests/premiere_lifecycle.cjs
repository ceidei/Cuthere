// Public Premiere API contract tests; run with node tests/premiere_lifecycle.cjs.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const hostDir = path.join(__dirname, '../premiere_cep/review_capture_panel/js');
const normalize = x => String(x).replaceAll('\\', '/');

function fixture() {
  const files = new Map();
  const app = {version: '25.0'};
  let imports = 0;
  function Folder(p) { this.fsName = normalize(p); this.exists = true; }
  function File(p) { this.fsName = normalize(p?.fsName || p); this.encoding = ''; }
  Object.defineProperties(File.prototype, {
    exists: {get() { return files.has(this.fsName); }},
    parent: {get() { return new Folder(this.fsName.slice(0, this.fsName.lastIndexOf('/'))); }},
    length: {get() { return Buffer.byteLength(files.get(this.fsName) || ''); }},
    modified: {get() { return '2026-10-18'; }},
  });
  File.prototype.open = function(mode) { this.mode = mode; return mode === 'w' || this.exists; };
  File.prototype.write = function(s) { files.set(this.fsName, s); };
  File.prototype.read = function() { return files.get(this.fsName); };
  File.prototype.close = function() {};
  File.prototype.copy = function(to) { files.set(normalize(to), this.read()); return true; };
  File.prototype.rename = function(name) {
    const next=this.fsName.slice(0,this.fsName.lastIndexOf('/')+1)+name;
    if(!this.exists || files.has(next)) return false;
    files.set(next,this.read()); files.delete(this.fsName); this.fsName=next; return true;
  };
  File.prototype.remove = function() { return files.delete(this.fsName); };
  function project(p, sequences) {
    const obj = {path: p, name: p, sequences: {numSequences: sequences.length}, activeSequence: sequences[0]};
    sequences.forEach((s, i) => {obj.sequences[i] = {...s, setPlayerPosition(t) {this.position = t;}};});
    obj.activeSequence = obj.sequences[0];
    obj.save = () => {const current=[];for(let i=0;i<obj.sequences.numSequences;i++){const s=obj.sequences[i];current.push({id:s.id,name:s.name,sequenceID:s.sequenceID});}files.set(normalize(obj.path), JSON.stringify(current)); return 0;};
    obj.saveAs = to => {obj.path = to; app.project = obj; return obj.save();};
    obj.openSequence = id => {obj.activeSequence = Object.values(obj.sequences).find(s=>s?.sequenceID === id); return !!obj.activeSequence;};
    obj.rootItem = {};
    obj.importFiles = () => {imports++; const added={name:'candidate',sequenceID:'new-id',setPlayerPosition(t){this.position=t;}};obj.sequences[obj.sequences.numSequences++]=added;return true;};
    obj.closeDocument = () => {app.project = null;};
    return obj;
  }
  app.project = project('C:/work/source.prproj', [{name:'baseline',sequenceID:'base-id'}]);
  app.project.save();
  app.openDocument = p => {
    const saved = JSON.parse(files.get(normalize(p)));
    app.project = project(p, saved);
    return true;
  };
  files.set('C:/work/input.xml', '<xmeml/>');
  function reload() {
    const ctx = vm.createContext({app,File,Folder,JSON,Date,$:{fileName:'C:/plugin/js/hostscript.jsx',os:'Windows'}});
    ctx.$.evalFile = () => vm.runInContext(fs.readFileSync(path.join(hostDir,'project_lifecycle.jsx'),'utf8'),ctx);
    vm.runInContext(fs.readFileSync(path.join(hostDir,'hostscript.jsx'),'utf8'),ctx);
    ctx.$.evalFile();
    vm.runInContext(fs.readFileSync(path.join(hostDir,'review_host.jsx'),'utf8'),ctx);
    assert.equal(ctx.$._AECapture.__reviewBridgeVersion, '1.2.6');
    const route=(name, params={}) => JSON.parse(ctx.routeCommand(name,JSON.stringify(params)));
    route.dispatch=(id,name,params)=>JSON.parse(ctx.dispatchBridgeRequest(id,name,JSON.stringify(params),'C:/bridge'));
    return route;
  }
  return {files,app,reload,get imports(){return imports;}};
}
const f=fixture(); let call=f.reload();
assert.equal(call('ping').data.version,'1.1.0');
const source=f.files.get('C:/work/source.prproj');
assert.equal(call('save_project',{expected_project_path:'C:/wrong.prproj'}).success,false);
assert.equal(call('save_project',{expected_project_path:'C:/work/source.prproj',output_path:'C:/work/working.prproj'}).success,true);
assert.equal(f.files.get('C:/work/source.prproj'),source);
const cp=call('create_checkpoint',{expected_project_path:'C:/work/working.prproj',checkpoint_path:'C:/work/cp.prproj'});
assert.equal(cp.success,true);
const params={expected_project_path:'C:/work/working.prproj',xml_path:'C:/work/input.xml',sequence_name:'candidate',operation_dir:'C:/work',operation_id:'import_0001',xml_sha256:'a'};
assert.equal(call('import_timeline',params).success,true);
call=f.reload(); // In-memory deduplication is gone; persisted receipt must still work.
assert.equal(call('import_timeline',params).data.replayed,true);
assert.equal(f.imports,1);
assert.equal(call('import_timeline',{...params,xml_sha256:'changed'}).success,false);
assert.equal(call('import_timeline',{...params,operation_id:'import_0002'}).success,false);
assert.equal(f.imports,1);
assert.equal(call('activate_sequence',{expected_project_path:'C:/work/working.prproj',sequence_id:'new-id',position_ticks:'-1'}).success,false);
assert.equal(f.app.project.activeSequence.sequenceID,'base-id');
assert.equal(call('activate_sequence',{expected_project_path:'C:/work/working.prproj',sequence_id:'new-id',position_ticks:'254016000000'}).success,true);
assert.equal(f.app.project.activeSequence.position,'254016000000');
assert.equal(call('save_project',{expected_project_path:'C:/work/working.prproj'}).success,true);
assert.equal(call('rollback_project',{expected_project_path:'C:/work/working.prproj',checkpoint_path:'C:/work/cp.prproj',recovery_path:'C:/work/recovery.prproj'}).success,true);
assert.equal(f.app.project.activeSequence.sequenceID,'base-id');
assert.equal(f.app.project.sequences.numSequences,1);
assert.equal(JSON.parse(f.files.get('C:/work/recovery.prproj')).length,2);
assert.equal(call('save_project',{expected_project_path:'C:/work/cp.prproj'}).success,false);
assert.equal(f.files.get('C:/work/source.prproj'),source);
const requestId='durable_request_01';
const requestParams={expected_project_path:'C:/work/cp.prproj',output_path:'C:/work/resumed.prproj'};
f.files.set('C:/bridge/commands/'+requestId+'.json',JSON.stringify({request_id:requestId,command:'save_project',params:requestParams,expires_at:Date.now()/1000+60}));
assert.equal(call.dispatch(requestId,'save_project',requestParams).success,true);
assert.equal(f.files.has('C:/bridge/commands/'+requestId+'.json'),false);
assert.equal(f.files.has('C:/bridge/commands/'+requestId+'.running'),false);
assert.equal(JSON.parse(f.files.get('C:/bridge/responses/'+requestId+'.json')).success,true);
call=f.reload();
assert.equal(call.dispatch(requestId,'save_project',requestParams).success,true); // no second saveAs
f.files.delete('C:/bridge/responses/'+requestId+'.json');
assert.equal(call.dispatch(requestId,'save_project',requestParams).success,false); // claimed request cannot mutate twice
console.log('PASS: project guards, non-overwriting copies, boolean import, persistent import replay, ID activation, full rollback, recovery preservation');
