"""A live automation session: one browser, one tenant, one policy, one evidence directory,
one control lease. Discovery and replay are both thin loops over this object, so policy
enforcement, secret handling, redaction and handoff behave identically in both."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .artifact import (
    ExtractStep,
    Literal_,
    OutcomeRule,
    ParamRef,
    SecretRef,
    StateCheck,
    Step,
    Value,
    interpolate,
)
from .config import AppProfile, SecretProvider, Tenant, load_policy, load_profile, load_tenant, ROOT
from .evidence import Evidence, new_run_id
from .handoff import Controller, env_timeout
from .artifact import Risk
from .policy import Decision, PolicyGate, Verdict
from .redaction import Redactor
from .surface.web import WebSurface


@dataclass
class StateMatch:
    kind: str  # expected | rule | timeout
    rule: OutcomeRule | None = None
    detail: str | None = None
    observed: str | None = None


class PolicyDenied(Exception):
    def __init__(self, decision: Decision):
        super().__init__(decision.reason)
        self.decision = decision


class Session:
    def __init__(self, tenant_id: str, *, kind: str, headed: bool = False,
                 evidence_root: Path | None = None, run_id: str | None = None) -> None:
        self.tenant: Tenant = load_tenant(tenant_id)
        self.profile: AppProfile = load_profile(self.tenant.product)
        self.policy = load_policy(self.tenant.policy, self.tenant)
        self.redactor = Redactor()
        self.run_id = run_id or new_run_id(kind)
        self.evidence = Evidence(evidence_root or ROOT / "runs", self.run_id, self.redactor)
        self.gate = PolicyGate(self.policy, self.tenant.base_url)
        self.controller = Controller(self.run_id, log=self.evidence.event, timeout_s=env_timeout())
        self.surface = WebSurface(
            self.tenant.base_url,
            headed=headed,
            pii_labels=self.profile.pii_labels,
            url_guard=self.policy.url_allowed,
            on_blocked=lambda url, why: self.evidence.event("policy_blocked_request", url=url, reason=why),
            on_human_event=lambda ev: self.controller.record_human_action({"source": "dom", **ev}),
            on_dialog=lambda typ, msg: self.evidence.event("native_dialog_dismissed", type=typ, message=msg),
        )
        self.secrets = SecretProvider(self.tenant, on_resolve=self._register_secret)
        self._shot = 0
        self.t0 = time.monotonic()

    def _register_secret(self, value: str) -> None:
        self.redactor.register(value, "secret")
        self.surface.sensitive_values.add(value)

    def register_sensitive(self, value: str, label: str) -> None:
        self.redactor.register(value, label)
        self.surface.sensitive_values.add(value)

    async def start(self) -> None:
        self.evidence.event("session_started", tenant=self.tenant.id, product=self.tenant.product,
                            version=self.tenant.product_version, policy=self.policy.name)
        await self.surface.start()

    async def close(self) -> None:
        try:
            await self.surface.close()
        finally:
            self.evidence.event("session_closed")
            self.evidence.close()

    # ----------------------------------------------------------------- values
    def resolve_value(self, v: Value | None, params: dict[str, str]) -> str | None:
        if v is None:
            return None
        if isinstance(v, ParamRef):
            return params[v.param]
        if isinstance(v, SecretRef):
            return self.secrets.get(v.secret)
        if isinstance(v, Literal_):
            return interpolate(v.literal, params)
        raise TypeError(v)

    # ----------------------------------------------------------------- execution
    async def execute(self, step: Step, params: dict[str, str], *, pre_approved: bool = False,
                      approved_now: bool = False):
        """Policy-check and perform one step. Returns the locator Resolution (or None)."""
        self.controller.assert_automation()
        decision = self.gate.check(step, pre_approved=pre_approved or approved_now)
        self.evidence.event("policy_decision", step_id=step.id, action=step.action, verdict=decision.verdict.value,
                            risk=decision.risk.value, reason=decision.reason)
        if decision.verdict != Verdict.allow:
            raise PolicyDenied(decision)
        value = self.resolve_value(getattr(step, "value", None), params)
        if isinstance(step, ExtractStep):
            return None
        return await self.surface.perform(step, value, params,
                                          allow_structural=decision.risk != Risk.irreversible)

    async def check_all(self, check: StateCheck, params: dict[str, str]) -> bool:
        for c in check.all_of:
            if not await self.surface.check(c, params):
                return False
        return True

    async def match_rule(self, rules: list[OutcomeRule], params: dict[str, str]) -> tuple[OutcomeRule, str | None] | None:
        for r in rules:
            try:
                hit = await self.surface.check(r.when, params)
            except KeyError:
                continue
            if hit:
                detail = None
                if r.capture:
                    text = self.redactor.labeled(await self.surface.frame_text(getattr(r.when, "frame", None)),
                                                 self.profile.pii_labels)
                    m = re.search(r.capture, text)
                    if m:
                        detail = self.redactor.text((m.group(1) if m.groups() else m.group(0)).strip())
                return r, detail
        return None

    async def await_state(self, expect: StateCheck, rules: list[OutcomeRule], params: dict[str, str],
                          timeout_ms: int) -> StateMatch:
        """Wait for the expected state OR a known outcome state — never a fixed sleep.

        Checking known states while waiting is what lets replay tell 'slow' from 'wrong':
        a timeout only happens when the screen matches neither the checkpoint nor any
        rule the profile/capability knows about."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if not expect.empty and await self.check_all(expect, params):
                return StateMatch("expected")
            m = await self.match_rule(rules, params)
            if m:
                return StateMatch("rule", rule=m[0], detail=m[1])
            if expect.empty:
                return StateMatch("expected")
            if time.monotonic() >= deadline:
                return StateMatch("timeout", observed=await self.observed_summary())
            await asyncio.sleep(0.2)

    async def observed_summary(self, limit: int = 400) -> str:
        parts = []
        if self.surface.page is None:
            return ""
        for f in self.surface.page.frames:
            try:
                raw = await f.locator("body").inner_text(timeout=1000)
                txt = re.sub(r"\s+", " ", self.redactor.labeled(raw, self.profile.pii_labels)).strip()
            except Exception:
                continue
            if txt:
                parts.append(f"[{f.name or 'top'}] {txt[:limit]}")
        return " || ".join(parts)

    # ----------------------------------------------------------------- auth
    async def ensure_authenticated(self) -> bool:
        if self.surface.page.url.startswith(self.tenant.base_url) and await self.check_all(self.profile.session_check, {}):
            return True
        self.evidence.event("auth_started", flow="profile.auth")
        for step in self.profile.auth.steps:
            await self.execute(step, {})
            if not step.expect.empty:
                m = await self.await_state(step.expect, [], {}, step.timeout_ms)
                if m.kind != "expected":
                    self.evidence.event("auth_failed", step_id=step.id, observed=m.observed)
                    return False
        ok = await self.check_all(self.profile.auth.expect, {})
        self.evidence.event("auth_finished", ok=ok)
        return ok

    async def reauthenticate(self) -> bool:
        flow = self.profile.reauth or self.profile.auth
        self.evidence.event("reauth_started")
        for step in flow.steps:
            await self.execute(step, {})
        m = await self.await_state(flow.expect, [], {}, 10_000)
        self.evidence.event("reauth_finished", ok=m.kind == "expected")
        return m.kind == "expected"

    # ----------------------------------------------------------------- evidence
    async def capture(self, tag: str, *, dom: bool = False) -> list[str]:
        self._shot += 1
        out = []
        shot = self.evidence.path(f"screens/{self._shot:02d}-{tag}.png")
        try:
            await self.surface.screenshot(str(shot), masked=True)
            out.append(self.evidence.rel(shot))
        except Exception as e:
            self.evidence.event("capture_failed", what="screenshot", error=str(e)[:200])
        if dom:
            d = self.evidence.path(f"dom/{self._shot:02d}-{tag}")
            try:
                files = await self.surface.dom_snapshot(str(d))
                for f in files:  # belt and braces: pattern-redact the already structurally-redacted DOM
                    Path(f).write_text(self.redactor.text(Path(f).read_text()))
                out += [self.evidence.rel(Path(f)) for f in files]
            except Exception as e:
                self.evidence.event("capture_failed", what="dom", error=str(e)[:200])
        return out

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)


def rules_for(profile: AppProfile, extra: list[OutcomeRule]) -> list[OutcomeRule]:
    """Capability rules take precedence over profile rules; failures are checked first so
    an error page is never mistaken for a business answer."""
    rules = list(extra) + [r for r in profile.outcomes if r.id not in {x.id for x in extra}]
    order = {"failure": 0, "recoverable": 1, "business": 2}
    return sorted(rules, key=lambda r: order[r.kind.value])
