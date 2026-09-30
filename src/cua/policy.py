"""Policy gate: every action — from the model, from replay, from the reauth flow — passes
through `PolicyGate.check` before it touches the surface. The network-layer route guard
(see WebSurface._guard) is a second, independent line: even an action the gate did not
anticipate (a link that navigates off-allowlist) is blocked at the request level.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .artifact import ClickStep, ExtractStep, NavigateStep, PressStep, Risk, Step
from .config import Policy


class Verdict(str, Enum):
    allow = "allow"
    deny = "deny"
    needs_approval = "needs_approval"


@dataclass
class Decision:
    verdict: Verdict
    risk: Risk
    reason: str


class PolicyGate:
    def __init__(self, policy: Policy, base_url: str) -> None:
        self.policy = policy
        self.base_url = base_url.rstrip("/")

    def classify(self, step: Step) -> Risk:
        """Risk is the max of what the artifact declares and what policy infers.
        An artifact can never downgrade a control that policy considers irreversible."""
        inferred = Risk.reversible
        if isinstance(step, ExtractStep):
            inferred = Risk.read
        elif isinstance(step, NavigateStep):
            inferred = Risk.read
        elif isinstance(step, (ClickStep, PressStep)):
            target = getattr(step, "target", None)
            names = [target.description] if target else []
            if target:
                names += [getattr(s, "name", "") or getattr(s, "text", "") for s in target.strategies]
            if isinstance(step, PressStep) and step.key.lower() == "enter":
                names.append(step.intent)
            if any(self.policy.is_irreversible_control(n) for n in names if n):
                inferred = Risk.irreversible
        order = [Risk.read, Risk.reversible, Risk.irreversible]
        return max(inferred, step.risk, key=order.index)

    def check(self, step: Step, *, pre_approved: bool = False) -> Decision:
        if step.action not in self.policy.allowed_actions:
            return Decision(Verdict.deny, step.risk, f"action type {step.action!r} not allowed by policy")
        if isinstance(step, NavigateStep):
            ok, why = self.policy.url_allowed(self.base_url + "/" + step.path.lstrip("/"))
            if not ok:
                return Decision(Verdict.deny, Risk.read, why)
        risk = self.classify(step)
        if risk == Risk.irreversible:
            if self.policy.irreversible_handling == "block":
                return Decision(Verdict.deny, risk, "irreversible actions are blocked by policy")
            if not pre_approved:
                return Decision(Verdict.needs_approval, risk, "irreversible action requires human approval")
            return Decision(Verdict.allow, risk, "irreversible action pre-approved by caller")
        return Decision(Verdict.allow, risk, "within policy")
