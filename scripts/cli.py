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
            'doctor': 'review_doctor', 'transcribe': 'transcribe_review_batch', 'select-sequence':'select_sequence',
            'shortcut': 'shortcut'}


def product_version() -> str:
    """Product version from VERSION, falling back to an installed runtime copy."""
    for candidate in (ROOT / 'VERSION', ROOT.parent / 'VERSION'):
        try:
            text = candidate.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if text:
            return text
    return 'unknown'


def bridge_status(extra_args: list[str]) -> int:
    """Ping the panel through the file bridge; read-only, no Premiere change.

    Exit 0 when the panel answered, 2 when it did not (exit 2 is the same code
    agents already treat as "failed" elsewhere in this CLI). Diagnostic keys are
    English so any local agent can read them; see docs/MACOS.md.
    """
    import argparse
    from dataclasses import asdict
    from engine.premiere_bridge import PremiereBridgeClient, PremiereCommand

    parser = argparse.ArgumentParser(prog='cuthere bridge-status')
    parser.add_argument('--bridge-dir', default=None)
    parser.add_argument('--timeout', type=float, default=5.0)
    args = parser.parse_args(extra_args)
    client = PremiereBridgeClient(args.bridge_dir)
    ready = client.is_ready
    reply = client.send_command(PremiereCommand.PING, {}, timeout=args.timeout)
    report = {
        'ok': bool(reply.success),
        'bridge_dir': client.bridge_dir,
        'dirs_ready': ready,
        'premiere': (reply.data or {}).get('premiere_version') if reply.success else None,
        'host_os': (reply.data or {}).get('os') if reply.success else None,
        'error': reply.error or None,
        'hint': (None if reply.success else
                 'Start Premiere, open Window > Extensions > Cuthere and check the panel shows the edit '
                 'connection is ready. A timeout means the host state is unknown: read back before retrying '
                 'a mutation.'),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if reply.success else 2


def main() -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = sys.argv[1:]
    if not args or args[0] in {'-h', '--help'}:
        print(f'Cuthere {product_version()}\nCommands: ' + ', '.join([*COMMANDS, 'bridge-status']))
        return 0
    name, rest = args[0], args[1:]
    if name == 'bridge-status':
        return bridge_status(rest)
    if name not in COMMANDS:
        raise SystemExit('Unknown command. Use --help.')
    sys.argv = [str(ROOT / 'scripts' / (COMMANDS[name] + '.py')), *rest]
    runpy.run_module('scripts.' + COMMANDS[name], run_name='__main__')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
