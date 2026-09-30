"""Command line entry point.

  cua app        --tenant riverbend                   run the demo target app
  cua discover   goals/read_savings_balance.yaml      LLM discovery -> artifact
  cua replay     artifacts/<id>/<ver>.json --input member_id=100234
  cua review     artifacts/<id>/<ver>.json            human-readable review of an artifact
  cua approve    artifacts/<id>/<ver>.json --reviewer alice
  cua catalog                                         capabilities as agent tools
  cua invoke     <tool name> --args '{"member_id": "100234"}'   agent-facing, unattended
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .artifact import Capability, Status
from .config import ROOT, load_dotenv, load_tenant


# --------------------------------------------------------------------------- demo app helpers
def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def ensure_demo_app(tenant_id: str) -> None:
    """Start the demo target in-process if nothing is listening on the tenant's URL."""
    t = load_tenant(tenant_id)
    u = urlparse(t.base_url)
    if _port_open(u.hostname, u.port):
        return
    sys.path.insert(0, str(ROOT))
    from demo_app.server import make_server

    srv = make_server(u.hostname, u.port, tenant_id)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[cua] started demo app for tenant {tenant_id} at {t.base_url}", file=sys.stderr)


def inject_fault(tenant_id: str, spec: dict) -> None:
    """Test harness only: arm a fault in the demo app. Not reachable by the agent
    (/__admin is on the policy deny list and blocked at the network layer)."""
    import urllib.request

    t = load_tenant(tenant_id)
    req = urllib.request.Request(t.base_url + "/__admin/faults", data=json.dumps(spec).encode(), method="POST")
    urllib.request.urlopen(req, timeout=5).read()


# --------------------------------------------------------------------------- commands
async def cmd_discover(a) -> int:
    from .discovery import Discovery, GoalSpec
    from .planner import AnthropicPlanner, ScriptedPlanner
    from .runtime import Session

    spec = GoalSpec.load(a.goal)
    tenant = a.tenant or spec.tenant
    if a.start_app:
        ensure_demo_app(tenant)
    if a.planner == "anthropic":
        planner = AnthropicPlanner(model=a.model)
    elif a.planner.startswith("scripted:"):
        import yaml

        planner = ScriptedPlanner(yaml.safe_load(Path(a.planner.split(":", 1)[1]).read_text()))
    else:
        raise SystemExit(f"unknown planner {a.planner}")
    session = Session(tenant, kind="discover", headed=a.headed,
                      evidence_root=Path(a.evidence_root) if a.evidence_root else None)
    console = await _maybe_console(session, a.console)
    await session.start()
    try:
        result = await Discovery(session, spec, planner).run()
    finally:
        if console:
            await console.stop()
        await session.close()
    print(json.dumps(result.__dict__, indent=2))
    return 0 if result.status == "succeeded" else 1


async def _maybe_console(session, port):
    if not port:
        return None
    from .console import OperatorConsole

    console = OperatorConsole(session, port)
    url = await console.start()
    print(f"[cua] operator console: {url}", file=sys.stderr)
    return console


def parse_inputs(pairs: list[str] | None) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k] = v
    return out


async def run_replay(artifact: Capability, tenant: str, inputs: dict[str, Any], *, escalation: str = "fail",
                     attended: bool = True, allow_irreversible: bool = False, console_port: int | None = None,
                     headed: bool = False, overlay: bool = True, evidence_root: Path | None = None,
                     on_console=None):
    from .config import Tenant
    from .replay import replay
    from .runtime import Session

    session = Session(tenant, kind="replay", headed=headed, evidence_root=evidence_root)
    if not overlay:
        session.tenant = Tenant.model_validate({**session.tenant.model_dump(), "text_aliases": {}, "step_overrides": {}})
    console = await _maybe_console(session, console_port)
    if console and on_console:
        on_console(console)
    await session.start()
    try:
        return await replay(session, artifact, inputs, attended=attended, allow_irreversible=allow_irreversible,
                            escalation=escalation)
    finally:
        if console:
            await console.stop()
        await session.close()


async def cmd_replay(a) -> int:
    cap = Capability.load(a.artifact)
    if a.start_app:
        ensure_demo_app(a.tenant)
    if a.fault:
        inject_fault(a.tenant, json.loads(a.fault))
    result = await run_replay(cap, a.tenant, parse_inputs(a.input), escalation=a.escalation,
                              attended=not a.unattended, allow_irreversible=a.allow_irreversible,
                              console_port=a.console, headed=a.headed, overlay=not a.no_overlay,
                              evidence_root=Path(a.evidence_root) if a.evidence_root else None)
    print(result.model_dump_json(indent=2))
    return {"SUCCEEDED": 0, "BUSINESS_OUTCOME": 0}.get(result.status.value, 1)


def load_catalog(root: Path = ROOT / "artifacts") -> dict[str, tuple[Capability, Path]]:
    """Latest version of each capability, keyed by its tool name."""
    best: dict[str, tuple[Capability, Path]] = {}
    for p in sorted(root.glob("*/*.json")):
        try:
            cap = Capability.load(p)
        except Exception:
            continue
        if cap.status == Status.deprecated:
            continue
        name = cap.tool_schema()["name"]
        ver = tuple(int(x) for x in cap.version.split("."))
        if name not in best or ver > tuple(int(x) for x in best[name][0].version.split(".")):
            best[name] = (cap, p)
    return best


def cmd_catalog(a) -> int:
    from .replay import is_approved

    cat = load_catalog()
    if a.tools:
        print(json.dumps([c.tool_schema() for c, _ in cat.values()], indent=2))
        return 0
    for name, (cap, path) in cat.items():
        flag = "approved" if is_approved(cap) else cap.status.value
        print(f"{name}  v{cap.version}  [{flag}, risk={cap.risk.value}]  {path.relative_to(ROOT)}")
        print(f"    {cap.description}")
        print(f"    inputs: {', '.join(f'{i.name}:{i.type.value}' for i in cap.inputs)}  "
              f"outputs: {', '.join(f'{o.name}:{o.type.value}' for o in cap.outputs)}")
    return 0


async def cmd_invoke(a) -> int:
    """What an AI agent calls: capability by tool name, typed args, unattended."""
    cat = load_catalog()
    if a.name not in cat:
        print(json.dumps({"error": f"unknown capability {a.name}", "available": sorted(cat)}))
        return 2
    cap, _ = cat[a.name]
    if a.start_app:
        ensure_demo_app(a.tenant)
    result = await run_replay(cap, a.tenant, json.loads(a.args), attended=False, escalation="fail",
                              evidence_root=Path(a.evidence_root) if a.evidence_root else None)
    # The agent gets the contract-shaped result, not the step trace.
    print(json.dumps(result.model_dump(mode="json", include={"status", "outputs", "outcome", "failure", "run_id",
                                                             "capability", "version", "warnings"}), indent=2))
    return 0 if result.status.value in ("SUCCEEDED", "BUSINESS_OUTCOME") else 1


def cmd_review(a) -> int:
    cap = Capability.load(a.artifact)
    from .replay import is_approved

    print(f"# {cap.title}  ({cap.id} v{cap.version})")
    print(f"status: {cap.status.value}{' (digest verified)' if is_approved(cap) else ''}   risk: {cap.risk.value}   "
          f"digest: {cap.digest()}")
    print(f"app: {cap.app.product} {cap.app.versions} ({cap.app.surface})   recorded on: "
          f"{cap.provenance.recorded_on_tenant} by {cap.provenance.discovered_by} run {cap.provenance.run_id}")
    print(f"\n{cap.description}\n\nInputs:")
    for i in cap.inputs:
        print(f"  - {i.name}: {i.type.value} [{i.classification.value}] {i.description}"
              + (f" /{i.pattern}/" if i.pattern else ""))
    print("Outputs:")
    for o in cap.outputs:
        print(f"  - {o.name}: {o.type.value} [{o.classification.value}] {o.description}")
    print("\nSteps:")
    for st in cap.steps:
        tgt = getattr(st, "target", None)
        val = getattr(st, "value", None)
        line = f"  {st.id} [{st.risk.value}{', HUMAN' if st.actor == 'human' else ''}] {st.action}: {st.intent}"
        print(line)
        if tgt:
            strat = " > ".join(s.kind for s in tgt.strategies)
            print(f"       target: {tgt.description} (frame {tgt.frame}) strategies: {strat}")
        if val is not None:
            print(f"       value: {val.model_dump()}")
        if not st.expect.empty:
            print(f"       expect: {st.expect.description or st.expect.all_of}")
    print(f"\nSuccess: {cap.success.description}")
    return 0


def cmd_approve(a) -> int:
    cap = Capability.load(a.artifact)
    human = [s.id for s in cap.steps if s.actor == "human"]
    if human and not a.accept_human_steps:
        print(f"refusing: steps {human} were performed by a human during discovery; review them and pass "
              f"--accept-human-steps", file=sys.stderr)
        return 1
    cap.status = Status.approved
    cap.provenance.reviewer = a.reviewer
    cap.provenance.reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cap.provenance.approved_digest = cap.digest()
    cap.save(a.artifact)
    print(f"approved {cap.id} v{cap.version} digest {cap.digest()} by {a.reviewer}")
    return 0


def cmd_app(a) -> int:
    sys.path.insert(0, str(ROOT))
    from demo_app.server import make_server

    t = load_tenant(a.tenant)
    u = urlparse(t.base_url)
    srv = make_server(u.hostname, a.port or u.port, a.tenant)
    print(f"Heritage Core ({a.tenant}) on http://{u.hostname}:{a.port or u.port}/", flush=True)
    srv.serve_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="cua", description="computer-use automation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("app", help="run the demo target application")
    p.add_argument("--tenant", default="riverbend")
    p.add_argument("--port", type=int)

    p = sub.add_parser("discover", help="LLM-driven discovery run that records a capability")
    p.add_argument("goal")
    p.add_argument("--tenant")
    p.add_argument("--planner", default="anthropic", help="anthropic | scripted:<file.yaml>")
    p.add_argument("--model")
    p.add_argument("--headed", action="store_true")
    p.add_argument("--console", type=int, help="start the operator console on this port")
    p.add_argument("--start-app", action="store_true", help="start the demo app if it is not running")
    p.add_argument("--evidence-root")

    p = sub.add_parser("replay", help="deterministic replay of an artifact")
    p.add_argument("artifact")
    p.add_argument("--tenant", default="riverbend")
    p.add_argument("--input", action="append", metavar="NAME=VALUE")
    p.add_argument("--escalation", choices=["fail", "human"], default="fail")
    p.add_argument("--unattended", action="store_true", help="production mode: requires an approved artifact")
    p.add_argument("--allow-irreversible", action="store_true", help="caller pre-approves irreversible steps")
    p.add_argument("--console", type=int)
    p.add_argument("--headed", action="store_true")
    p.add_argument("--no-overlay", action="store_true", help="ignore the tenant overlay (to demonstrate drift)")
    p.add_argument("--fault", help="TEST ONLY: JSON fault spec to arm in the demo app first")
    p.add_argument("--start-app", action="store_true")
    p.add_argument("--evidence-root")

    p = sub.add_parser("review", help="print an artifact for human review")
    p.add_argument("artifact")
    p = sub.add_parser("approve", help="mark an artifact approved (binds approval to its digest)")
    p.add_argument("artifact")
    p.add_argument("--reviewer", required=True)
    p.add_argument("--accept-human-steps", action="store_true")

    p = sub.add_parser("catalog", help="list capabilities an agent can invoke")
    p.add_argument("--tools", action="store_true", help="print as tool definitions")
    p = sub.add_parser("invoke", help="invoke a capability by tool name (agent path, unattended)")
    p.add_argument("name")
    p.add_argument("--args", default="{}")
    p.add_argument("--tenant", default="riverbend")
    p.add_argument("--start-app", action="store_true")
    p.add_argument("--evidence-root")

    a = ap.parse_args(argv)
    if a.cmd == "discover":
        return asyncio.run(cmd_discover(a))
    if a.cmd == "replay":
        return asyncio.run(cmd_replay(a))
    if a.cmd == "invoke":
        return asyncio.run(cmd_invoke(a))
    return {"catalog": cmd_catalog, "review": cmd_review, "approve": cmd_approve, "app": cmd_app}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
