"""Small command router; existing tools retain their own argument parsers."""
from __future__ import annotations
import json
import runpy
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
COMMANDS = {'handoff': 'agent_handoff', 'edit': 'podcast_edit', 'roundtrip': 'premiere_roundtrip',
            'read-batch': 'read_review_batch', 'decision-template': 'prepare_review_edit_plan',
            'doctor': 'review_doctor', 'transcribe': 'transcribe_review_batch', 'select-sequence':'select_sequence'}

def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:]
    if not args or args[0] in {'-h', '--help'}:
        print('Cuthere 1.0.1\nCommands: ' + ', '.join([*COMMANDS, 'bridge-status']))
        return 0
    name, rest = args[0], args[1:]
    if name == 'bridge-status':
        from engine.premiere_bridge import PremiereBridgeClient, PremiereCommand
        from dataclasses import asdict
        reply = PremiereBridgeClient().send_command(PremiereCommand.PING, {})
        print(json.dumps(asdict(reply), ensure_ascii=False))
        return 0 if reply.success else 2
    if name not in COMMANDS:
        raise SystemExit('Unknown command. Use --help.')
    sys.argv = [str(ROOT / 'scripts' / (COMMANDS[name] + '.py')), *rest]
    runpy.run_module('scripts.' + COMMANDS[name], run_name='__main__')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
