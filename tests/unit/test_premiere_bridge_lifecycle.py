from __future__ import annotations
import json
import threading
import time
from pathlib import Path
from engine.premiere_bridge import PremiereBridgeClient, PremiereCommand


def test_client_waits_for_complete_response_and_writes_non_json_temp(tmp_path, monkeypatch):
    client = PremiereBridgeClient(str(tmp_path))
    client.ensure_dirs()
    import engine.premiere_bridge as module
    replace = module.os.replace
    written = []
    def atomic(src, dest):
        written.append(Path(src).name)
        assert Path(src).suffix == '.tmp' and '.json' not in Path(src).name
        replace(src, dest)
    monkeypatch.setattr(module.os, 'replace', atomic)
    errors = []
    def panel():
        try:
            deadline = time.monotonic()+3
            while not list((tmp_path/'commands').glob('*.json')) and time.monotonic()<deadline:
                time.sleep(.01)
            path = next((tmp_path/'commands').glob('*.json'))
            cmd = json.loads(path.read_text(encoding='utf-8'))
            assert cmd['expires_at'] > cmd['timestamp']
            out = tmp_path/'responses'/path.name
            out.write_text('{',encoding='utf-8')
            time.sleep(.2)
            out.write_text(json.dumps({'request_id':cmd['request_id'],'success':True,'data':{'saved':True}}),encoding='utf-8')
        except Exception as exc: errors.append(exc)
    worker=threading.Thread(target=panel);worker.start()
    response=client.send_command(PremiereCommand.SAVE_PROJECT,{},timeout=3)
    worker.join()
    assert not errors
    assert response.success and response.data['saved'] and written
    assert not list((tmp_path/'commands').glob('*.json'))


def test_lifecycle_convenience_methods_forward_guards(tmp_path, monkeypatch):
    client=PremiereBridgeClient(str(tmp_path)); calls=[]
    monkeypatch.setattr(client,'send_command',lambda c,p,**kw:calls.append((c,p,kw)))
    client.save_project('base.prproj','new.prproj')
    client.create_checkpoint('new.prproj','cp.prproj')
    client.import_timeline('new.prproj','edit.xml','new','work','operation_123','sha')
    client.activate_sequence('new.prproj','id-1','0')
    client.rollback_project('new.prproj','cp.prproj','recovery.prproj')
    assert all(p['expected_project_path'] for _,p,_ in calls)
    assert calls[2][1]['operation_id']=='operation_123'
    assert calls[3][1]['sequence_id']=='id-1'
    assert calls[4][1]['recovery_path']=='recovery.prproj'


def test_multi_sequence_export_selects_requested_media(tmp_path):
    from engine.premiere_roundtrip import snapshots,select
    xml=tmp_path/'multi.xml'
    xml.write_text('''<xmeml><sequence><name>old</name><duration>25</duration><media/></sequence>
    <sequence><name>new</name><duration>50</duration><rate><timebase>25</timebase></rate><media><audio><track><enabled>TRUE</enabled><clipitem><name>voice</name><start>0</start><end>50</end><in>10</in><out>60</out><file id="f"/></clipitem></track></audio></media></sequence>
    <file id="f"><pathurl>file://localhost/F%3a/work/voice.wav</pathurl></file></xmeml>''',encoding='utf-8')
    new=select(snapshots(xml),'new')
    assert new['frames']==50 and new['tracks'][0]['clips'][0][-1]=='f:/work/voice.wav'


def test_live_readback_detects_wrong_microphone_track_and_frame_rate():
    from engine.premiere_roundtrip import active_matches
    expected={'fps':'25','ntsc':'FALSE','tracks':[
        {'kind':'audio','index':0,'enabled':'FALSE','clips':[1]},
        {'kind':'audio','index':1,'enabled':'TRUE','clips':[1]}]}
    actual={'frame_rate':25,'total_clip_count':2,'tracks':[
        {'kind':'audio','index':0,'muted':True,'clips':[1]},
        {'kind':'audio','index':1,'muted':False,'clips':[1]}]}
    assert active_matches(expected,actual)
    actual['tracks'][0]['muted']=False
    assert not active_matches(expected,actual)
    actual['tracks'][0]['muted']=True
    actual['frame_rate']=30
    assert not active_matches(expected,actual)
