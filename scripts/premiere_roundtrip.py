"""Headless publish/rollback after the user opens Premiere and the CEP bridge."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.premiere_bridge import PremiereBridgeClient
from engine.premiere_roundtrip import PremiereRoundTrip


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir")
    sub = parser.add_subparsers(dest="action", required=True)
    publish = sub.add_parser("publish")
    publish.add_argument("--xml", type=Path, required=True)
    publish.add_argument("--sequence", required=True)
    publish.add_argument("--expected-project", type=Path, required=True)
    publish.add_argument("--working-project", type=Path, required=True)
    publish.add_argument("--job", type=Path, required=True)
    publish.add_argument("--position-ticks", default="0")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--job", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    runtime = PremiereRoundTrip(PremiereBridgeClient(args.bridge_dir))
    try:
        result = (runtime.publish(args.xml, args.sequence, args.expected_project, args.working_project,
                                  args.job, args.position_ticks) if args.action == "publish" else
                  runtime.rollback(args.job) if args.action == "rollback" else runtime.verify(args.job))
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
