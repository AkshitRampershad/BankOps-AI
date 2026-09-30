"""Control transfer between automation and a human operator on one live session.

The model is a single-holder lease with an explicit state machine:

    AUTOMATION ──escalate()──▶ AWAITING_HUMAN ──claim()──▶ HUMAN ──release()──▶ AUTOMATION
         ▲                            │                                  │
         └──────── timeout / abort ◀──┘◀──────────── abort ──────────────┘

* Exactly one party holds control. Automation checks `assert_automation()` before every
  action, so it physically cannot act while a human holds the lease; operator commands
  carry a lease token that is checked on every call and invalidated on release.
* An intervention carries the context the operator needs: capability/goal, the step and
  its intent, why automation stopped, a (masked) screenshot and redacted screen text.
* Everything the human does is recorded (DOM-level click/change capture on the page,
  plus operator commands), with values never captured — only that a field was changed.
* On release, automation does not assume where the human left the screen; the caller
  re-synchronises against step checkpoints (replay) or re-observes (discovery).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


class ControlState(str, Enum):
    automation = "AUTOMATION"
    awaiting_human = "AWAITING_HUMAN"
    human = "HUMAN"


class Resolution(str, Enum):
    resume = "resume"  # human fixed things; automation should continue
    approve = "approve"  # approval request granted
    deny = "deny"  # approval request declined
    abort = "abort"  # stop the run
    timeout = "timeout"  # nobody picked it up


class ControlError(Exception):
    pass


@dataclass
class Intervention:
    id: str
    kind: str  # stuck | approval | failure
    reason: str
    subject: str  # capability id or discovery goal
    step_id: str | None
    step_intent: str | None
    screen_text: str
    screenshot: str | None
    created_at: str
    status: str = "pending"  # pending | claimed | resolved
    operator: str | None = None
    resolution: str | None = None
    note: str | None = None
    human_actions: list[dict] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("extra", None)
        return d


class Controller:
    def __init__(self, session_id: str, *, log: Callable[..., Any], timeout_s: float = 900) -> None:
        self.session_id = session_id
        self.state = ControlState.automation
        self.holder = "automation"
        self._token: str | None = None
        self._log = log
        self.timeout_s = timeout_s
        self.interventions: dict[str, Intervention] = {}
        self._done: dict[str, asyncio.Future] = {}
        self.console_url: str | None = None
        self.notifier: Callable[[Intervention], Any] | None = None

    # ----------------------------------------------------------------- automation side
    def assert_automation(self) -> None:
        if self.state != ControlState.automation:
            raise ControlError(f"automation may not act: control is {self.state.value} ({self.holder})")

    async def escalate(self, *, kind: str, reason: str, subject: str, step_id: str | None,
                       step_intent: str | None, screen_text: str, screenshot: str | None,
                       extra: dict | None = None) -> Intervention:
        """Pause automation and wait for a human to resolve the intervention."""
        self.assert_automation()
        iid = f"int-{secrets.token_hex(3)}"
        iv = Intervention(
            id=iid, kind=kind, reason=reason, subject=subject, step_id=step_id, step_intent=step_intent,
            screen_text=screen_text, screenshot=screenshot,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), extra=extra or {},
        )
        self.interventions[iid] = iv
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._done[iid] = fut
        self._set(ControlState.awaiting_human, "unassigned")
        self._log("intervention_requested", intervention=iv.public())
        self._route(iv)
        try:
            await asyncio.wait_for(asyncio.shield(fut), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            iv.status, iv.resolution = "resolved", Resolution.timeout.value
            self._token = None
            self._set(ControlState.automation, "automation")
            self._log("intervention_timeout", intervention_id=iid)
        return iv

    def _route(self, iv: Intervention) -> None:
        """Route the request to a human. Here: stderr + console URL (+ optional notifier
        hook). In production: the operator queue / paging system, keyed by tenant."""
        where = f"{self.console_url}/#{iv.id}" if self.console_url else "(no operator console running)"
        print(f"\n>>> HUMAN INTERVENTION NEEDED [{iv.kind}] {iv.id}: {iv.reason}\n>>> step {iv.step_id}: "
              f"{iv.step_intent}\n>>> take control at {where}\n", file=sys.stderr, flush=True)
        if self.notifier:
            self.notifier(iv)

    # ----------------------------------------------------------------- operator side
    def pending(self) -> list[Intervention]:
        return [iv for iv in self.interventions.values() if iv.status != "resolved"]

    def claim(self, iid: str, operator: str) -> str:
        iv = self.interventions.get(iid)
        if iv is None or iv.status == "resolved":
            raise ControlError("no such open intervention")
        if iv.status == "claimed":
            raise ControlError(f"already claimed by {iv.operator}")
        iv.status, iv.operator = "claimed", operator
        self._token = secrets.token_urlsafe(16)
        self._set(ControlState.human, f"human:{operator}")
        self._log("control_transferred", to=f"human:{operator}", intervention_id=iid)
        return self._token

    def check_token(self, token: str | None) -> None:
        if self.state != ControlState.human or not token or token != self._token:
            raise ControlError("invalid or expired control lease")

    def record_human_action(self, action: dict) -> None:
        """Called for DOM events captured on the page and for operator commands."""
        if self.state != ControlState.human:
            return
        iv = next((i for i in self.interventions.values() if i.status == "claimed"), None)
        if iv is None:
            return
        iv.human_actions.append(action)
        self._log("human_action", intervention_id=iv.id, operator=iv.operator, action=action)

    def release(self, iid: str, token: str, resolution: str, note: str | None = None) -> Intervention:
        self.check_token(token)
        iv = self.interventions[iid]
        res = Resolution(resolution)
        if iv.kind == "approval" and res not in (Resolution.approve, Resolution.deny, Resolution.abort):
            raise ControlError("approval requests resolve with approve, deny or abort")
        iv.status, iv.resolution, iv.note = "resolved", res.value, note
        self._token = None
        self._set(ControlState.automation, "automation")
        self._log("control_returned", intervention_id=iid, resolution=res.value, note=note,
                  human_actions=len(iv.human_actions))
        fut = self._done.get(iid)
        if fut and not fut.done():
            fut.set_result(res)
        return iv

    def _set(self, state: ControlState, holder: str) -> None:
        self.state, self.holder = state, holder

    def status(self) -> dict:
        return {"session": self.session_id, "state": self.state.value, "holder": self.holder,
                "pending": [i.id for i in self.pending()]}


def env_timeout() -> float:
    return float(os.environ.get("CUA_ESCALATION_TIMEOUT_S", "900"))
