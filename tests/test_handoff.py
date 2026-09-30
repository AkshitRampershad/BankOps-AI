import asyncio

import pytest

from cua.handoff import ControlError, ControlState, Controller


def ctl(timeout=5):
    events = []
    return Controller("sess", log=lambda k, **f: events.append((k, f)), timeout_s=timeout), events


async def test_lease_lifecycle():
    c, events = ctl()

    async def operator():
        while not c.pending():
            await asyncio.sleep(0.01)
        iv = c.pending()[0]
        with pytest.raises(ControlError):
            c.check_token("forged")
        token = c.claim(iv.id, "op-1")
        assert c.state == ControlState.human
        with pytest.raises(ControlError):  # automation cannot act while a human holds control
            c.assert_automation()
        with pytest.raises(ControlError):  # single holder
            c.claim(iv.id, "op-2")
        c.record_human_action({"source": "dom", "event": "click"})
        c.release(iv.id, token, "resume", "fixed it")
        with pytest.raises(ControlError):  # token dies with the lease
            c.check_token(token)

    op = asyncio.create_task(operator())
    iv = await c.escalate(kind="stuck", reason="r", subject="s", step_id="s01", step_intent="i",
                          screen_text="", screenshot=None)
    await op
    assert iv.resolution == "resume" and iv.operator == "op-1" and len(iv.human_actions) == 1
    assert c.state == ControlState.automation
    kinds = [k for k, _ in events]
    assert kinds == ["intervention_requested", "control_transferred", "human_action", "control_returned"]


async def test_approval_requires_explicit_decision():
    c, _ = ctl()

    async def operator():
        while not c.pending():
            await asyncio.sleep(0.01)
        iv = c.pending()[0]
        token = c.claim(iv.id, "op-1")
        with pytest.raises(ControlError):
            c.release(iv.id, token, "resume")
        c.release(iv.id, token, "deny", "no consent")

    op = asyncio.create_task(operator())
    iv = await c.escalate(kind="approval", reason="r", subject="s", step_id="s09", step_intent="submit",
                          screen_text="", screenshot=None)
    await op
    assert iv.resolution == "deny"


async def test_timeout_returns_control_to_automation():
    c, _ = ctl(timeout=0.2)
    iv = await c.escalate(kind="stuck", reason="r", subject="s", step_id=None, step_intent=None,
                          screen_text="", screenshot=None)
    assert iv.resolution == "timeout" and c.state == ControlState.automation
