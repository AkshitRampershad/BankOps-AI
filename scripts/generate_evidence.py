"""Regenerate /evidence: discovery runs, then replays covering success, business outcomes,
recoverable states, hard failures, human handoff, cross-tenant reuse and approval gating.

    python scripts/generate_evidence.py            # uses Claude if ANTHROPIC_API_KEY is set
    python scripts/generate_evidence.py --planner scripted
    python scripts/generate_evidence.py --only discovery

With the Anthropic planner the discovery runs are genuine LLM-driven runs; with the
scripted planner they are labelled `-scripted` and exist only to exercise the pipeline.
Replays are always LLM-free.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cua.artifact import Capability, Status  # noqa: E402
from cua.cli import ensure_demo_app, inject_fault, run_replay  # noqa: E402
from cua.config import load_dotenv  # noqa: E402
from cua.discovery import Discovery, GoalSpec  # noqa: E402
from cua.operator_client import run_operator  # noqa: E402
from cua.planner import AnthropicPlanner, ScriptedPlanner  # noqa: E402
from cua.runtime import Session  # noqa: E402

EVID = ROOT / "evidence"
CONSOLE_PORT = 8790
CONSOLE = f"http://127.0.0.1:{CONSOLE_PORT}"
TMP = ROOT / "runs" / "_evidence"
index: list[dict] = []


def keep(name: str, run_dir: str | Path, summary: dict) -> None:
    dst = EVID / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(run_dir, dst)
    (dst / "scenario.json").write_text(json.dumps(summary, indent=2) + "\n")
    index.append({"dir": name, **{k: summary.get(k) for k in ("scenario", "status", "code", "outputs")}})
    print(f"  -> evidence/{name}: {summary.get('status')} {summary.get('code') or ''}")


async def discover(goal: str, planner_kind: str, operator_plan: list | None = None) -> Capability:
    spec = GoalSpec.load(ROOT / "goals" / f"{goal}.yaml")
    if planner_kind == "anthropic":
        planner = AnthropicPlanner()
    else:
        planner = ScriptedPlanner(yaml.safe_load((ROOT / "goals" / "scripted" / f"{goal}.yaml").read_text()))
    s = Session(spec.tenant, kind="discover", evidence_root=TMP)
    from cua.console import OperatorConsole

    console = OperatorConsole(s, CONSOLE_PORT)
    await console.start()
    await s.start()
    try:
        tasks = [Discovery(s, spec, planner).run()]
        if operator_plan:
            tasks.append(run_operator(CONSOLE, operator_plan, wait_s=600))
        res = (await asyncio.gather(*tasks, return_exceptions=True))[0]
    finally:
        await console.stop()
        await s.close()
    if isinstance(res, Exception):
        raise res
    label = "llm" if planner_kind == "anthropic" else "scripted"
    keep(f"discovery-{goal.replace('_', '-')}-{label}", res.evidence_dir, {
        "scenario": f"discovery ({planner.name}): {spec.goal}", "status": res.status, "code": None,
        "reason": res.reason, "planner": planner.name, "usage": res.usage, "artifact": res.artifact_path and
        str(Path(res.artifact_path).relative_to(ROOT))})
    if res.status != "succeeded":
        raise SystemExit(f"discovery failed: {res.reason}")
    return Capability.load(res.artifact_path)


async def replay_case(name: str, scenario: str, cap: Capability, inputs: dict, *, tenant="riverbend",
                      fault: dict | None = None, operator_plan: list | None = None, **kw) -> None:
    if fault:
        inject_fault(tenant, fault)
    coros = [run_replay(cap, tenant, inputs, evidence_root=TMP,
                        console_port=CONSOLE_PORT if operator_plan else None, **kw)]
    if operator_plan:
        coros.append(run_operator(CONSOLE, operator_plan, wait_s=60))
    r = (await asyncio.gather(*coros))[0]
    if fault:
        inject_fault(tenant, {})
    code = (r.outcome and r.outcome.code) or (r.failure and r.failure.code.value)
    keep(name, r.evidence_dir, {"scenario": scenario, "tenant": tenant, "fault_injected": fault,
                                "status": r.status.value, "code": code, "outputs": r.outputs,
                                "recoveries": [x.code for x in r.recoveries],
                                "interventions": [f"{i.kind}:{i.resolution}" for i in r.interventions],
                                "warnings": r.warnings, "duration_ms": r.duration_ms})


async def main(planner_kind: str, only: str | None) -> None:
    load_dotenv()
    ensure_demo_app("riverbend")
    ensure_demo_app("lakeside")
    EVID.mkdir(exist_ok=True)
    for old in EVID.glob("discovery-*"):  # don't leave a previous planner's discovery runs behind
        shutil.rmtree(old)
    print(f"planner: {planner_kind}")

    # ---------------------------------------------------------------- discovery
    bal = await discover("read_savings_balance", planner_kind)
    sub = await discover("open_sub_account", planner_kind,
                         operator_plan=[{"release": "approve", "note": "member consent verified (demo)"}])
    (EVID / "artifacts").mkdir(exist_ok=True)
    (EVID / "artifacts" / "read_savings_balance.json").write_text(bal.to_json())
    (EVID / "artifacts" / "open_sub_account.json").write_text(sub.to_json())
    if only == "discovery":
        return

    m = {"member_id": "100234"}
    print("replays:")
    await replay_case("replay-01-success", "happy path", bal, m)
    await replay_case("replay-02-success-other-member", "same artifact, different input", bal,
                      {"member_id": "100377"})
    await replay_case("replay-03-business-not-found", "no such member -> business outcome", bal,
                      {"member_id": "999999"})
    await replay_case("replay-04-business-access-denied", "restricted account -> business outcome", bal,
                      {"member_id": "100555"})
    await replay_case("replay-05-rejected-bad-input", "input fails the declared pattern; UI untouched", bal,
                      {"member_id": "12AB"})
    await replay_case("replay-06-recovered-system-notice", "interstitial notice acknowledged via profile rule",
                      bal, m, fault={"notice_once": True, "path": "MBR0100"})
    await replay_case("replay-07-recovered-session-expired", "session expiry -> re-auth -> resync on checkpoints",
                      bal, m, fault={"session_expire_once": True, "path": "MBR0100"})
    await replay_case("replay-08-slow-load", "slow responses waited on by checkpoint, not sleeps", bal, m,
                      fault={"slow_seconds": 3, "slow_count": 2, "path": "MBR0100"})
    await replay_case("replay-09-failed-app-error", "application error -> hard failure with evidence", bal, m,
                      fault={"error_once": True, "path": "MBR0100"})
    await replay_case("replay-10-failed-unknown-screen", "unknown interstitial, no human -> CHECKPOINT_TIMEOUT",
                      bal, m, fault={"unknown_dialog_once": True})
    await replay_case("replay-11-human-handoff-unknown-screen",
                      "unknown interstitial -> operator takes the live session -> resume", bal, m,
                      fault={"unknown_dialog_once": True}, escalation="human",
                      operator_plan=[{"click": "Remind Me Later", "note": "dismiss training reminder"},
                                     {"release": "resume", "note": "dismissed compliance reminder"}])
    await replay_case("replay-12-tenant-lakeside-overlay", "same artifact on a second tenant via overlay", bal, m,
                      tenant="lakeside")
    await replay_case("replay-13-tenant-lakeside-no-overlay-drift",
                      "second tenant without overlay: drift detected, data never read positionally", bal, m,
                      tenant="lakeside", overlay=False)
    sub_in = {"member_id": "100377", "share_type": "HOLIDAY CLUB", "nickname": "Gifts",
              "initial_deposit": "40.00"}
    await replay_case("replay-14-irreversible-rejected-unattended",
                      "irreversible capability without approval: rejected before touching the UI", sub, sub_in)
    await replay_case("replay-15-irreversible-human-approval", "irreversible step approved by an operator", sub,
                      sub_in, escalation="human",
                      operator_plan=[{"release": "approve", "note": "consent verified"}])
    await replay_case("replay-16-irreversible-validation-outcome", "core rejects the deposit -> business outcome",
                      sub, {**sub_in, "initial_deposit": "2.00"}, escalation="human")

    # Agent-facing path: approved capability invoked by tool name, unattended.
    approved = bal.model_copy(deep=True)
    approved.status = Status.approved
    approved.provenance.reviewer = "demo-reviewer"
    approved.provenance.approved_digest = approved.digest()
    (EVID / "artifacts" / "read_savings_balance.approved.json").write_text(approved.to_json())
    (EVID / "artifacts" / "catalog_tools.json").write_text(json.dumps([approved.tool_schema(), sub.tool_schema()],
                                                                     indent=2) + "\n")
    await replay_case("replay-17-agent-invoke-unattended", "agent invokes approved capability, unattended",
                      approved, m, attended=False)

    lines = ["# Evidence index", "",
             "Generated by `python scripts/generate_evidence.py`. Each directory holds `events.jsonl` (structured,",
             "redacted log), `result.json` (the contract returned to the caller, inputs redacted by classification),",
             "`scenario.json` (what was injected / expected) and `screens/` (masked screenshots; discovery also has",
             "the redacted screen text the model saw). Failures also carry redacted DOM snapshots in `dom/`.", "",
             "| dir | scenario | status | code | outputs |", "|---|---|---|---|---|"]
    if planner_kind != "anthropic":
        lines[2:2] = ["> **Discovery here used the scripted planner** (pipeline check, no model). Run",
                      "> `ANTHROPIC_API_KEY=... python scripts/generate_evidence.py` to produce the genuine LLM",
                      "> discovery runs (`discovery-*-llm`) and replays of the model-recorded artifacts.", ""]
    for row in index:
        lines.append(f"| `{row['dir']}` | {row['scenario']} | {row['status']} | {row['code'] or ''} | "
                     f"{json.dumps(row['outputs']) if row.get('outputs') else ''} |")
    (EVID / "INDEX.md").write_text("\n".join(lines) + "\n")
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--planner", choices=["anthropic", "scripted"],
                    default="anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "scripted")
    ap.add_argument("--only", choices=["discovery"])
    a = ap.parse_args()
    asyncio.run(main(a.planner, a.only))
