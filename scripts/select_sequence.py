"""Extract one explicitly named sequence from Premiere's whole-project FCP7 XML."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path

def select_sequence(source: Path, name: str, output: Path) -> dict:
    receipt=output.with_suffix(output.suffix+'.selection.json')
    if output.exists() or receipt.exists(): raise FileExistsError(output)
    source_bytes=source.read_bytes()
    root=ET.fromstring(source_bytes)
    matches=[s for s in root.iter('sequence') if s.find('media') is not None and s.findtext('name')==name]
    if len(matches)!=1: raise ValueError(f'目标序列名必须唯一：{name}（{len(matches)}个）')
    chosen=copy.deepcopy(matches[0])
    if chosen.find('.//sequence') is not None: raise ValueError('当前提取工具仅支持单个普通序列，目标含嵌套序列')
    definitions={f.get('id'):f for f in root.iter('file') if f.get('id') and f.findtext('pathurl')}
    resolved=0
    for parent in chosen.iter():
        for index,node in enumerate(list(parent)):
            if node.tag!='file' or node.findtext('pathurl'): continue
            full=definitions.get(node.get('id'))
            if full is None: raise ValueError(f'媒体引用无法解析：{node.get("id")}')
            parent.remove(node); parent.insert(index,copy.deepcopy(full)); resolved+=1
    result=ET.Element('xmeml',version=root.get('version','4')); result.append(chosen)
    content=b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'+ET.tostring(result,encoding='utf-8')
    data={'status':'SELECTED','sequence_name':name,'sequence_id':chosen.get('id'),
          'source_xml':str(source.resolve()),'source_sha256':hashlib.sha256(source_bytes).hexdigest(),
          'output_xml':str(output.resolve()),'output_sha256':hashlib.sha256(content).hexdigest(),
          'duration_frames':int(chosen.findtext('duration','-1')),'resolved_file_references':resolved}
    if source.read_bytes()!=source_bytes: raise ValueError('提取期间源XML变化，请重新读取')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('xb') as stream: stream.write(content)
    with receipt.open('x',encoding='utf-8') as stream: stream.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    return data

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--xml',type=Path,required=True); p.add_argument('--sequence',required=True); p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    try: result=select_sequence(args.xml,args.sequence,args.output)
    except (ValueError,OSError,ET.ParseError) as exc:
        print(json.dumps({'status':'ERROR','error':str(exc)},ensure_ascii=False)); return 2
    print(json.dumps(result,ensure_ascii=False)); return 0

if __name__=='__main__': raise SystemExit(main())
