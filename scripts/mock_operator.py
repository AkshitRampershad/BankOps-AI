"""Stand-in for a human at the operator console: waits for an intervention, takes control
of the live session, performs the given commands, and hands control back.

    python scripts/mock_operator.py --console http://127.0.0.1:8790 \
        --plan '[{"click": "Remind Me Later"}, {"release": "resume", "note": "dismissed training reminder"}]'
"""
import argparse
import asyncio
import json

from cua.operator_client import run_operator

ap = argparse.ArgumentParser()
ap.add_argument("--console", default="http://127.0.0.1:8790")
ap.add_argument("--plan", required=True, help="JSON list of commands")
ap.add_argument("--operator", default="op-jlee")
ap.add_argument("--wait", type=float, default=120)
a = ap.parse_args()
print(json.dumps(asyncio.run(run_operator(a.console, json.loads(a.plan), operator=a.operator, wait_s=a.wait)), indent=2))
