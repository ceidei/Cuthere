from pathlib import Path
import xml.etree.ElementTree as ET
import pytest
from engine.podcast_edit_runtime import dry_run

def multi_xml(path: Path):
    path.write_text('''<xmeml version="4"><project><children>
    <sequence id="first"><name>Unrelated</name><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><duration>300</duration><media><video><track><clipitem id="a"><name>a</name><start>0</start><end>300</end><in>0</in><out>300</out><file id="shared"><pathurl>file://localhost/C%3a/test.mp4</pathurl><duration>300</duration></file></clipitem></track></video></media></sequence>
    <sequence id="second"><name>Target</name><rate><timebase>25</timebase><ntsc>FALSE</ntsc></rate><duration>300</duration><media><video><track><clipitem id="b"><name>b</name><start>0</start><end>300</end><in>0</in><out>300</out><file id="shared"/></clipitem></track></video></media></sequence>
    </children></project></xmeml>''',encoding='utf-8')

def test_edit_refuses_multi_sequence_xml_instead_of_using_first(tmp_path):
    source=tmp_path/'all.xml'; multi_xml(source)
    project=tmp_path/'project.prproj'; project.write_bytes(b'test')
    with pytest.raises(ValueError,match='多个|单个|唯一'):
        dry_run(source,{'action':'manual_review'},project)

def test_selection_resolves_shared_media_and_preserves_source(tmp_path):
    from scripts.select_sequence import select_sequence
    source=tmp_path/'all.xml'; multi_xml(source); before=source.read_bytes()
    out=tmp_path/'selected.xml'
    result=select_sequence(source,'Target',out)
    root=ET.parse(out).getroot()
    assert root.find('sequence/name').text=='Target'
    assert len(list(root.iter('sequence')))==1
    assert root.find('.//file/pathurl').text=='file://localhost/C%3a/test.mp4'
    assert source.read_bytes()==before
    assert result['sequence_id']=='second'
    assert result['resolved_file_references']==1

def test_selection_rejects_duplicate_names_and_existing_output(tmp_path):
    from scripts.select_sequence import select_sequence
    source=tmp_path/'all.xml'; multi_xml(source)
    output=tmp_path/'keep.xml'; output.write_bytes(b'keep')
    with pytest.raises(FileExistsError): select_sequence(source,'Target',output)
    assert output.read_bytes()==b'keep'
    source.write_text(source.read_text().replace('Unrelated','Target'))
    with pytest.raises(ValueError,match='唯一'): select_sequence(source,'Target',tmp_path/'absent.xml')
    assert not (tmp_path/'absent.xml').exists()

def test_selection_rejects_unresolved_media(tmp_path):
    from scripts.select_sequence import select_sequence
    source=tmp_path/'all.xml'; multi_xml(source)
    source.write_text(source.read_text().replace('file id="shared"/>','file id="missing"/>'))
    with pytest.raises(ValueError,match='媒体'): select_sequence(source,'Target',tmp_path/'absent.xml')
