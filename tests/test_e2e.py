"""End-to-end: real browser, real demo app, scripted planner standing in for the model."""

import asyncio

import pytest
import yaml

from cua.artifact import Capability, Status
from cua.cli import run_replay
from cua.config import ROOT
from cua.discovery import Discovery, GoalSpec
from cua.operator_client import run_operator
from cua.planner import ScriptedPlanner
from cua.runtime import Session

pytestmark = pytest.mark.usefixtures("demo_apps")


async def discover(tmp_path, name: str, operator_plan=None, port=None) -> Capability:
    spec = GoalSpec.load(ROOT / "goals" / f"{name}.yaml")
    script = yaml.safe_load((ROOT / "goals" / "scripted" / f"{name}.yaml").read_text())
    s = Session(spec.tenant, kind="discover", evidence_root=tmp_path / "runs")
    console = None
    if operator_plan is not None:
        from cua.console import OperatorConsole
        console = OperatorConsole(s, port)
        await console.start()
    await s.start()
    try:
        tasks = [Discovery(s, spec, ScriptedPlanner(script), artifacts_dir=tmp_path / "artifacts").run()]
        if operator_plan is not None:
            tasks.append(run_operator(f"http://127.0.0.1:{port}", operator_plan, log=lambda *_: None))
        res = (await asyncio.gather(*tasks))[0]
    finally:
        if console:
            await console.stop()
        await s.close()
    assert res.status == "succeeded", res.reason
    return Capability.load(res.artifact_path)


@pytest.fixture(scope="module")
def balance_cap(tmp_path_factory, demo_apps):
    return asyncio.run(discover(tmp_path_factory.mktemp("disc"), "read_savings_balance"))


def rr(cap, tmp_path, inputs, **kw):
    return run_replay(cap, kw.pop("tenant", "riverbend"), inputs, evidence_root=tmp_path, **kw)


async def test_recorded_artifact_shape(balance_cap):
    cap = balance_cap
    assert [s.action for s in cap.steps] == ["click", "fill", "click", "extract"]
    assert cap.steps[1].value.param == "member_id"
    assert cap.steps[1].target.strategies[0].kind == "anchored"  # unlabelled legacy field
    assert cap.steps[3].target.strategies[0].kind == "table_cell"
    assert not cap.steps[2].expect.empty and cap.status == Status.draft
    assert "100234" not in cap.to_json()  # discovery input value never persisted


async def test_replay_success_and_second_member(balance_cap, tmp_path):
    r = await rr(balance_cap, tmp_path, {"member_id": "100234"})
    assert r.status.value == "SUCCEEDED" and r.outputs == {"savings_balance": "1234.56"}
    r = await rr(balance_cap, tmp_path, {"member_id": "100377"})
    assert r.outputs == {"savings_balance": "58002.10"}
    events = (tmp_path / r.run_id / "events.jsonl").read_text()
    assert "100377" not in events and "demo-pass-123" not in events


@pytest.mark.parametrize("member,code", [("999999", "RECORD_NOT_FOUND"), ("100555", "ACCESS_DENIED")])
async def test_business_outcomes(balance_cap, tmp_path, member, code):
    r = await rr(balance_cap, tmp_path, {"member_id": member})
    assert r.status.value == "BUSINESS_OUTCOME" and r.outcome.code == code and r.failure is None


async def test_bad_input_rejected_before_ui(balance_cap, tmp_path):
    r = await rr(balance_cap, tmp_path, {"member_id": "12AB"})
    assert r.status.value == "REJECTED" and r.failure.code.value == "INPUT_INVALID" and not r.steps


@pytest.mark.parametrize("fault,code", [
    ({"notice_once": True, "path": "MBR0100"}, "SYSTEM_NOTICE"),
    ({"session_expire_once": True, "path": "MBR0100"}, "SESSION_EXPIRED"),
])
async def test_recoverable_states(balance_cap, tmp_path, faults, fault, code):
    faults(fault)
    r = await rr(balance_cap, tmp_path, {"member_id": "100234"})
    assert r.status.value == "SUCCEEDED", r.failure
    assert [x.code for x in r.recoveries] == [code]


async def test_app_error_is_hard_failure_with_evidence(balance_cap, tmp_path, faults):
    faults({"error_once": True, "path": "MBR0100"})
    r = await rr(balance_cap, tmp_path, {"member_id": "100234"})
    assert r.status.value == "FAILED" and r.failure.code.value == "APP_ERROR"
    assert r.failure.step_id == "s01" and r.failure.retryable
    assert any(e.endswith(".png") for e in r.failure.evidence)
    assert any(e.endswith(".html") for e in r.failure.evidence)


async def test_unknown_state_fails_or_hands_off(balance_cap, tmp_path, faults, free_port):
    faults({"unknown_dialog_once": True})
    r = await rr(balance_cap, tmp_path, {"member_id": "100234"})
    assert r.failure.code.value == "CHECKPOINT_TIMEOUT" and r.failure.step_id == "s03"

    faults({"unknown_dialog_once": True})
    plan = [{"click": "Remind Me Later"}, {"release": "resume", "note": "dismissed"}]
    r, _ = await asyncio.gather(
        rr(balance_cap, tmp_path, {"member_id": "100234"}, escalation="human", console_port=free_port),
        run_operator(f"http://127.0.0.1:{free_port}", plan, log=lambda *_: None))
    assert r.status.value == "SUCCEEDED", r.failure
    assert r.interventions[0].resolution == "resume" and r.interventions[0].human_actions >= 1
    assert (tmp_path / r.run_id / "proposed_profile_rules.yaml").exists()


async def test_cross_tenant_overlay_and_drift_detection(balance_cap, tmp_path):
    r = await rr(balance_cap, tmp_path, {"member_id": "100234"}, tenant="lakeside")
    assert r.status.value == "SUCCEEDED" and r.outputs == {"savings_balance": "1234.56"}
    r = await rr(balance_cap, tmp_path, {"member_id": "100234"}, tenant="lakeside", overlay=False)
    # Navigation may limp along on structural fallbacks (reported as drift) but data is
    # never read through a positional locator.
    assert r.status.value == "FAILED" and r.failure.code.value == "TARGET_NOT_FOUND" and r.failure.step_id == "s04"
    assert any("drift" in w for w in r.warnings)
    # The failure evidence describes the profile screen without leaking what is on it.
    persisted = "".join(p.read_text() for p in (tmp_path / r.run_id).rglob("*") if p.suffix in (".json", ".jsonl", ".html"))
    for secret in ("JANE Q SAMPLE", "123-45-6789", "1984-03-17", "12 ELM ST", "100234", "demo-pass-123"):
        assert secret not in persisted


async def test_irreversible_flow_gating(tmp_path, free_port):
    cap = await discover(tmp_path, "open_sub_account", operator_plan=[{"release": "approve"}], port=free_port)
    assert cap.risk.value == "irreversible"
    inputs = {"member_id": "100377", "share_type": "HOLIDAY CLUB", "nickname": "Gifts", "initial_deposit": "40.00"}
    r = await rr(cap, tmp_path, inputs)
    assert r.status.value == "REJECTED" and r.failure.code.value == "NOT_APPROVED"
    r = await rr(cap, tmp_path, inputs, attended=False, allow_irreversible=True)  # draft: still rejected
    assert r.status.value == "REJECTED"
    r, _ = await asyncio.gather(
        rr(cap, tmp_path, inputs, escalation="human", console_port=free_port),
        run_operator(f"http://127.0.0.1:{free_port}", [{"release": "deny"}], log=lambda *_: None))
    assert r.failure.code.value == "APPROVAL_DENIED" and r.failure.step_id == "s09"
    cap.status = Status.approved
    cap.provenance.approved_digest = cap.digest()
    r = await rr(cap, tmp_path, inputs, attended=False, allow_irreversible=True)
    assert r.status.value == "SUCCEEDED" and r.outputs["confirmation_number"].startswith("SA")
    r = await rr(cap, tmp_path, {**inputs, "initial_deposit": "2.00"}, attended=False, allow_irreversible=True)
    assert r.status.value == "BUSINESS_OUTCOME" and r.outcome.code == "VALIDATION_ERROR"
