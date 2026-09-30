"""Deterministic replay: execute a capability artifact with caller inputs, no LLM.

Per step:  policy gate → resolve target (ranked strategies) → act → await checkpoint
while watching for known outcome states. What happens next is decided by *which* state
appeared, never by guessing:

    checkpoint holds        → next step
    business rule matched   → return BUSINESS_OUTCOME (a legitimate answer)
    recoverable rule        → run its recovery, then re-synchronise on checkpoints
    failure rule            → FAILED with the app's own error text
    nothing matched (t/o)   → FAILED, or escalate to a human if the caller allows it

Re-synchronisation finds the latest step whose checkpoint currently holds and resumes
after it. It never restarts across an already-executed irreversible step.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .artifact import (
    AnchoredLocator,
    Capability,
    DataClass,
    ElementCondition,
    ExtractStep,
    OutcomeKind,
    OutcomeRule,
    ParamSpec,
    Recovery as RecoveryRule,
    Risk,
    RoleLocator,
    Status,
    TableCellLocator,
    Target,
    TextCondition,
    TextLocator,
    ValueType,
    interpolate,
)
from .config import Tenant
from .handoff import ControlError, Resolution
from .results import (
    Failure,
    FailureCode,
    InterventionSummary,
    Outcome,
    Recovery,
    ReplayResult,
    RunStatus,
    StepTrace,
)
from .runtime import PolicyDenied, Session, rules_for
from .surface.base import TargetError

MAX_ESCALATIONS = 2


# --------------------------------------------------------------------------- inputs & outputs
def validate_inputs(cap: Capability, inputs: dict[str, Any]) -> list[str]:
    errors = []
    known = {p.name for p in cap.inputs}
    for extra in set(inputs) - known:
        errors.append(f"unknown input {extra!r}")
    for p in cap.inputs:
        if p.name not in inputs or inputs[p.name] in (None, ""):
            if p.required:
                errors.append(f"missing required input {p.name!r}")
            continue
        errors += _check_type(p, str(inputs[p.name]))
    return errors


def _check_type(p: ParamSpec, v: str) -> list[str]:
    errs = []
    if p.pattern and not re.fullmatch(p.pattern, v):
        errs.append(f"input {p.name!r} does not match pattern {p.pattern}")
    if p.enum and v not in p.enum:
        errs.append(f"input {p.name!r} must be one of {p.enum}")
    if p.type in (ValueType.decimal, ValueType.money):
        try:
            Decimal(v.replace(",", "").replace("$", ""))
        except InvalidOperation:
            errs.append(f"input {p.name!r} is not a {p.type.value}")
    if p.type == ValueType.integer and not re.fullmatch(r"-?\d+", v):
        errs.append(f"input {p.name!r} is not an integer")
    return errs


def parse_value(raw: str, typ: ValueType, pattern: str | None = None) -> Any:
    s = raw.strip()
    if pattern:
        m = re.search(pattern, s)
        if not m:
            raise ValueError(f"value does not match extraction pattern {pattern}")
        s = (m.group(1) if m.groups() else m.group(0)).strip()
    if typ in (ValueType.money, ValueType.decimal):
        neg = s.startswith("(") and s.endswith(")")
        cleaned = re.sub(r"[,$\s()]", "", s)
        if not re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
            raise ValueError(f"{raw!r} is not a {typ.value}")
        d = Decimal(cleaned)
        return str(-d if neg else d)
    if typ == ValueType.integer:
        if not re.fullmatch(r"-?[\d,]+", s):
            raise ValueError(f"{raw!r} is not an integer")
        return int(s.replace(",", ""))
    if typ == ValueType.boolean:
        return s.lower() in ("y", "yes", "true", "1", "on", "checked")
    if not s:
        raise ValueError("empty value")
    return s


# --------------------------------------------------------------------------- tenant overlay
def apply_overlay(cap: Capability, tenant: Tenant) -> tuple[Capability, list[str]]:
    """Specialise a product-level capability for one tenant: label aliases + step overrides.

    Returns the executable copy and notes on what was changed (reported as warnings)."""
    notes: list[str] = []
    aliases = tenant.text_aliases
    c = cap.model_copy(deep=True)

    def alias(s: str) -> str:
        if s in aliases:
            notes.append(f"alias {s!r} -> {aliases[s]!r}")
            return aliases[s]
        return s

    def fix_target(t):
        if t is None:
            return
        for strat in t.strategies:
            if isinstance(strat, RoleLocator):
                strat.name = alias(strat.name)
            elif isinstance(strat, AnchoredLocator):
                strat.anchor_text = alias(strat.anchor_text)
            elif isinstance(strat, TableCellLocator):
                strat.column, strat.row_key = alias(strat.column), alias(strat.row_key)
            elif isinstance(strat, TextLocator):
                strat.text = alias(strat.text)

    def fix_check(chk):
        for cond in chk.all_of:
            if isinstance(cond, TextCondition):
                cond.text = alias(cond.text)
            elif isinstance(cond, ElementCondition):
                fix_target(cond.target)

    overrides = tenant.step_overrides.get(cap.id, {})
    for step in c.steps:
        if step.id in overrides and hasattr(step, "target"):
            step.target = overrides[step.id].model_copy(deep=True)
            notes.append(f"step {step.id} target overridden for tenant {tenant.id}")
        else:
            fix_target(getattr(step, "target", None))
        fix_check(step.expect)
    fix_check(c.success)
    for r in c.outcomes:
        if isinstance(r.when, TextCondition):
            r.when.text = alias(r.when.text)
    return c, sorted(set(notes))


def version_ok(spec: str, version: str) -> bool:
    def tup(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    v = tup(version)
    for clause in spec.split(","):
        clause = clause.strip()
        m = re.match(r"(>=|<=|>|<|==)?\s*([\d.]+)", clause)
        if not m:
            continue
        op, ref = m.group(1) or "==", tup(m.group(2))
        vv = v[: len(ref)]
        ok = {">=": vv >= ref, "<=": vv <= ref, ">": vv > ref, "<": vv < ref, "==": vv == ref}[op]
        if not ok:
            return False
    return True


def is_approved(cap: Capability) -> bool:
    """Approved *and* unchanged since approval (the digest binds review to content)."""
    return cap.status == Status.approved and cap.provenance.approved_digest == cap.digest()


# --------------------------------------------------------------------------- engine
class Replayer:
    def __init__(self, session: Session, cap: Capability, inputs: dict[str, Any], *,
                 attended: bool = True, allow_irreversible: bool = False, escalation: str = "fail") -> None:
        self.s = session
        self.original = cap
        self.inputs = {k: str(v) for k, v in inputs.items()}
        self.attended = attended
        self.allow_irreversible = allow_irreversible
        self.escalation = escalation
        self.result = ReplayResult(
            run_id=session.run_id, capability=cap.id, version=cap.version, digest=cap.digest(),
            tenant=session.tenant.id, status=RunStatus.failed, evidence_dir=str(session.evidence.dir),
        )
        self.outputs: dict[str, Any] = {}
        self.executed_irreversible: set[int] = set()
        self.escalations = 0

    # ------------------------------------------------------------------ helpers
    def _log(self, kind: str, /, **f) -> None:
        self.s.evidence.event(kind, **f)

    def _finish(self, status: RunStatus) -> ReplayResult:
        self.result.status = status
        self.result.duration_ms = self.s.elapsed_ms()
        cap = self.original
        # Outputs go back to the caller in clear; the persisted copy is redacted by class.
        persisted = self.result.model_dump(mode="json")
        for name, _ in list(persisted.get("outputs", {}).items()):
            spec = cap.output_spec(name)
            if spec and spec.classification in (DataClass.pii, DataClass.secret):
                persisted["outputs"][name] = f"«{spec.classification.value}»"
        persisted["inputs"] = {
            k: (f"«{p.classification.value}»" if (p := cap.input_spec(k)) and p.classification in
                (DataClass.pii, DataClass.secret) else v) for k, v in self.inputs.items()}
        self.s.evidence.write_json("result.json", persisted)
        self._log("run_finished", status=status.value,
                  outcome=self.result.outcome.code if self.result.outcome else None,
                  failure=self.result.failure.code.value if self.result.failure else None)
        return self.result

    async def _fail(self, code: FailureCode, message: str, step=None, *, expected: str | None = None,
                    observed: str | None = None, attempts=None, retryable: bool = False) -> ReplayResult:
        evidence = await self.s.capture(f"failure-{code.value.lower()}", dom=True)
        if observed is None:
            observed = await self.s.observed_summary()
        self.result.failure = Failure(
            code=code, message=message, step_id=getattr(step, "id", None), step_intent=getattr(step, "intent", None),
            expected=expected, observed=observed, attempts=attempts or [], retryable=retryable, evidence=evidence)
        self._log("failure", **self.result.failure.model_dump(mode="json"))
        return self._finish(RunStatus.failed)

    def _describe(self, step) -> str:
        if step.expect.empty:
            return f"step {step.id} completes ({step.intent})"
        return "; ".join(
            (f"text '{interpolate(c.text, self.inputs)}' in frame {c.frame}" if c.kind == "text"
             else f"url {c.pattern}" if c.kind == "url" else f"element {c.target.description}")
            for c in step.expect.all_of)

    # ------------------------------------------------------------------ main
    async def run(self) -> ReplayResult:
        cap, s = self.original, self.s
        self._log("replay_started", capability=cap.id, version=cap.version, digest=cap.digest(),
                  status=cap.status.value, attended=self.attended, escalation=self.escalation)

        # ---- pre-flight: nothing touches the UI until these pass
        errors = validate_inputs(cap, self.inputs)
        if errors:
            self.result.failure = Failure(code=FailureCode.INPUT_INVALID, message="; ".join(errors))
            return self._finish(RunStatus.rejected)
        if cap.app.product != s.tenant.product:
            self.result.failure = Failure(code=FailureCode.INPUT_INVALID,
                                          message=f"capability is for {cap.app.product}, tenant runs {s.tenant.product}")
            return self._finish(RunStatus.rejected)
        if not version_ok(cap.app.versions, s.tenant.product_version):
            self.result.warnings.append(
                f"tenant version {s.tenant.product_version} outside validated range {cap.app.versions}")
            if not self.attended:
                self.result.failure = Failure(code=FailureCode.NOT_APPROVED,
                                              message="unvalidated product version; attended replay required")
                return self._finish(RunStatus.rejected)
        if not self.attended and not is_approved(cap):
            self.result.failure = Failure(
                code=FailureCode.NOT_APPROVED,
                message="unattended replay requires an approved capability whose digest matches its approval")
            return self._finish(RunStatus.rejected)
        irreversible = [st.id for st in cap.steps if s.gate.classify(st) == Risk.irreversible]
        if irreversible and self.escalation != "human" and not (self.allow_irreversible and is_approved(cap)):
            self.result.failure = Failure(
                code=FailureCode.NOT_APPROVED,
                message=f"steps {irreversible} are irreversible: invoke an approved capability with "
                        "allow_irreversible, or run attended with human escalation")
            return self._finish(RunStatus.rejected)
        for p in cap.inputs:
            if p.classification in (DataClass.pii, DataClass.secret) and p.name in self.inputs:
                s.register_sensitive(self.inputs[p.name], f"{p.classification.value}:{p.name}")

        plan, notes = apply_overlay(cap, s.tenant)
        self.result.warnings += notes
        self.rules = rules_for(s.profile, plan.outcomes)
        self.steps = plan.steps

        try:
            if not await s.ensure_authenticated():
                return await self._fail(FailureCode.AUTH_FAILED, "could not establish an authenticated session")
        except KeyError as e:
            return await self._fail(FailureCode.SECRET_UNAVAILABLE, str(e))
        except TargetError as e:
            return await self._fail(FailureCode.AUTH_FAILED, f"sign-on screen not recognised: {e}", attempts=e.attempts)

        try:
            return await self._run_steps(plan)
        except ControlError as e:
            return await self._fail(FailureCode.INTERNAL, f"control violation: {e}")

    async def _run_steps(self, plan: Capability) -> ReplayResult:
        s, steps = self.s, self.steps
        recovery_attempts: dict[str, int] = {}
        # A known state can already be on screen before the first action (e.g. a notice).
        i = 0
        guard = 0
        while i < len(steps):
            guard += 1
            if guard > len(steps) * 6:
                return await self._fail(FailureCode.RECOVERY_EXHAUSTED, "replay is not converging", steps[i])
            step = steps[i]
            t_step = s.elapsed_ms()
            pre_approved = self.allow_irreversible and is_approved(self.original)
            approved_now = False
            decision = s.gate.check(step, pre_approved=pre_approved)
            if decision.verdict.value == "deny":
                return await self._fail(FailureCode.POLICY_VIOLATION, decision.reason, step)
            if decision.verdict.value == "needs_approval":
                if self.escalation != "human":
                    return await self._fail(
                        FailureCode.NOT_APPROVED,
                        "irreversible step needs approval: pass --allow-irreversible on an approved capability, "
                        "or run with --escalation human", step)
                iv = await self._escalate("approval", f"approve irreversible step: {step.intent}", step)
                if iv.resolution == Resolution.approve.value:
                    approved_now = True
                elif iv.resolution == Resolution.deny.value:
                    return await self._fail(FailureCode.APPROVAL_DENIED, f"operator {iv.operator} denied the step", step)
                elif iv.resolution == Resolution.timeout.value:
                    return await self._fail(FailureCode.ESCALATION_TIMEOUT, "no operator responded to the approval", step)
                else:
                    return await self._fail(FailureCode.ABORTED_BY_OPERATOR, iv.note or "aborted", step)

            # ---- act
            res = None
            try:
                if isinstance(step, ExtractStep):
                    self._log("step_started", step_id=step.id, action=step.action, intent=step.intent)
                    await s.execute(step, self.inputs, pre_approved=pre_approved)
                    raw, res = await s.surface.read(step.target, self.inputs, step.timeout_ms)
                    try:
                        self.outputs[step.output] = parse_value(raw, step.parse, step.pattern)
                    except ValueError as e:
                        return await self._fail(FailureCode.EXTRACTION_FAILED, f"{step.output}: {e}", step,
                                                expected=f"{step.parse.value} value", observed=s.redactor.text(raw))
                    self._log("extracted", step_id=step.id, output=step.output,
                              strategy=res.strategy_kind, chars=len(raw))
                else:
                    self._log("step_started", step_id=step.id, action=step.action, intent=step.intent)
                    res = await s.execute(step, self.inputs, pre_approved=pre_approved, approved_now=approved_now)
                    if step.risk == Risk.irreversible or s.gate.classify(step) == Risk.irreversible:
                        self.executed_irreversible.add(i)
            except PolicyDenied as e:
                return await self._fail(FailureCode.POLICY_VIOLATION, e.decision.reason, step)
            except TargetError as e:
                # Wrong screen? Let a known state explain it before calling it a failure.
                m = await s.match_rule(self.rules, self.inputs)
                if m:
                    outcome = await self._on_rule(m[0], m[1], step, i, recovery_attempts)
                    if isinstance(outcome, ReplayResult):
                        return outcome
                    i = outcome
                    continue
                nxt = await self._stuck(FailureCode(e.code), f"{e.detail}", step, attempts=e.attempts,
                                        expected=f"exactly one '{e.target.description}'")
                if isinstance(nxt, ReplayResult):
                    return nxt
                i = nxt
                continue
            except KeyError as e:
                return await self._fail(FailureCode.SECRET_UNAVAILABLE, str(e), step)

            if res is not None:
                fallback = res.strategy_index > 0
                if fallback:
                    self.result.warnings.append(
                        f"step {step.id}: primary locator failed, matched by fallback '{res.strategy_kind}' (drift)")
                    self._log("locator_fallback", step_id=step.id, strategy=res.strategy_kind)
            # ---- checkpoint
            m = await s.await_state(step.expect, self.rules, self.inputs, step.timeout_ms)
            if m.kind == "expected":
                took = s.elapsed_ms() - t_step
                if not step.expect.empty and took > step.timeout_ms / 2:
                    self.result.warnings.append(f"step {step.id}: slow ({took} ms of {step.timeout_ms} ms budget)")
                self.result.steps.append(StepTrace(
                    step_id=step.id, action=step.action, status="ok",
                    strategy=res.strategy_kind if res else None, fallback=bool(res and res.strategy_index > 0),
                    ms=s.elapsed_ms() - t_step))
                self._log("checkpoint_passed", step_id=step.id)
                i += 1
                continue
            if m.kind == "rule":
                outcome = await self._on_rule(m.rule, m.detail, step, i, recovery_attempts)
                if isinstance(outcome, ReplayResult):
                    return outcome
                i = outcome
                continue
            nxt = await self._stuck(FailureCode.CHECKPOINT_TIMEOUT, "expected state did not appear", step,
                                    expected=self._describe(step), observed=m.observed)
            if isinstance(nxt, ReplayResult):
                return nxt
            i = nxt

        return await self._verify_success(plan)

    async def _verify_success(self, plan: Capability) -> ReplayResult:
        s = self.s
        m = await s.await_state(plan.success, self.rules, self.inputs, 5_000)
        if m.kind == "rule" and m.rule.kind == OutcomeKind.business:
            return await self._business(m.rule, m.detail, None)
        if m.kind != "expected":
            return await self._fail(FailureCode.CHECKPOINT_TIMEOUT, "success checkpoint not satisfied",
                                    expected=plan.success.description or "success checkpoint", observed=m.observed)
        for o in plan.outputs:
            if o.name not in self.outputs and not o.nullable:
                return await self._fail(FailureCode.EXTRACTION_FAILED, f"declared output {o.name!r} was not produced")
        self.result.outputs = self.outputs
        await s.capture("success")
        return self._finish(RunStatus.succeeded)

    # ------------------------------------------------------------------ outcome handling
    async def _business(self, rule: OutcomeRule, detail: str | None, step) -> ReplayResult:
        self.result.outcome = Outcome(code=rule.code, message=interpolate(rule.message, self.inputs)
                                      if "{{" in rule.message else rule.message,
                                      detail=detail, step_id=getattr(step, "id", None), rule=rule.id)
        self._log("business_outcome", **self.result.outcome.model_dump())
        await self.s.capture(f"outcome-{rule.code.lower()}")
        return self._finish(RunStatus.business_outcome)

    async def _on_rule(self, rule: OutcomeRule, detail: str | None, step, i: int,
                       attempts: dict[str, int]) -> ReplayResult | int:
        s = self.s
        self._log("outcome_rule_matched", rule=rule.id, rule_kind=rule.kind.value, code=rule.code, step_id=step.id,
                  detail=detail)
        if rule.kind == OutcomeKind.business:
            return await self._business(rule, detail, step)
        if rule.kind == OutcomeKind.failure:
            return await self._fail(FailureCode.APP_ERROR, f"{rule.message} {detail or ''}".strip(), step,
                                    expected=self._describe(step), retryable=rule.retryable)
        # recoverable
        attempts[rule.id] = attempts.get(rule.id, 0) + 1
        rec = rule.recovery
        if attempts[rule.id] > rec.max_attempts:
            return await self._fail(FailureCode.RECOVERY_EXHAUSTED,
                                    f"{rule.code} persisted after {rec.max_attempts} recovery attempt(s)", step)
        await s.capture(f"recoverable-{rule.code.lower()}")
        if rec.kind == "click":
            from .artifact import ClickStep  # local: tiny synthetic step for the recovery action
            await s.execute(ClickStep(id="s99", intent=f"recover from {rule.code}", target=rec.target), {})
        elif rec.kind == "reauth":
            if not await s.reauthenticate():
                return await self._fail(FailureCode.AUTH_FAILED, "re-authentication after session expiry failed", step)
        elif rec.kind == "wait_retry":
            import asyncio
            await asyncio.sleep(rec.backoff_ms / 1000)
        await s.surface.settle()
        j = await self._resync(i, allow_forward=False)
        self.result.recoveries.append(Recovery(step_id=step.id, rule=rule.id, code=rule.code, action=rec.kind,
                                               resumed_at=self.steps[j].id if j is not None and j < len(self.steps) else None))
        self._log("recovered", rule=rule.id, action=rec.kind, resume_index=j)
        if j is None:
            return await self._fail(FailureCode.RESYNC_FAILED,
                                    f"after handling {rule.code}, the screen matched no known step checkpoint", step)
        return j

    async def _resync(self, i: int, *, allow_forward: bool) -> int | None:
        """Where are we? Resume after the latest step whose checkpoint holds now."""
        s = self.s
        upper = len(self.steps) - 1 if allow_forward else i
        for k in range(upper, -1, -1):
            st = self.steps[k]
            if st.expect.empty:
                continue
            if await s.check_all(st.expect, self.inputs):
                # Never resume *before* an irreversible step that already ran (would repeat it).
                if any(x > k for x in self.executed_irreversible):
                    return None
                return k + 1
        if not self.executed_irreversible and await s.check_all(s.profile.session_check, {}):
            return 0
        return None

    async def _escalate(self, kind: str, reason: str, step):
        s = self.s
        shots = await s.capture(f"escalation-{kind}")
        self.escalations += 1
        self._escalation_obs = await s.surface.observe()
        iv = await s.controller.escalate(
            kind=kind, reason=reason, subject=f"{self.original.id}@{self.original.version}", step_id=step.id,
            step_intent=step.intent, screen_text=self._escalation_obs.render(s.redactor.text),
            screenshot=shots[0] if shots else None)
        self.result.interventions.append(InterventionSummary(
            id=iv.id, kind=iv.kind, reason=iv.reason, step_id=iv.step_id, operator=iv.operator,
            resolution=iv.resolution, human_actions=len(iv.human_actions), note=iv.note))
        return iv

    async def _stuck(self, code: FailureCode, message: str, step, **kw) -> ReplayResult | int:
        """Unexplained state. Fail with evidence, or hand the live session to a human."""
        if self.escalation != "human" or self.escalations >= MAX_ESCALATIONS:
            return await self._fail(code, message, step, **kw)
        iv = await self._escalate("stuck", f"{code.value}: {message}", step)
        if iv.resolution == Resolution.resume.value:
            self._propose_rule(iv)
            await self.s.surface.settle()
            j = await self._resync(len(self.steps) - 1, allow_forward=True)
            self._log("resumed_after_handoff", intervention_id=iv.id, resume_index=j)
            if j is None:
                return await self._fail(FailureCode.RESYNC_FAILED,
                                        "after the human handoff the screen matched no known step checkpoint", step)
            return j
        if iv.resolution == Resolution.timeout.value:
            return await self._fail(FailureCode.ESCALATION_TIMEOUT, f"{message} (no operator responded)", step, **kw)
        return await self._fail(FailureCode.ABORTED_BY_OPERATOR, iv.note or "operator aborted the run", step, **kw)


    def _propose_rule(self, iv) -> None:
        """A human just got us past a screen no rule knew about. If they did it with a single
        recordable click, propose a recoverable profile rule so the next run handles it
        unattended. Proposals are written to evidence for review — never applied silently."""
        clicks = [a for a in iv.human_actions if a.get("source") == "operator" and a.get("action") == "click"
                  and "target" in a]
        if len(clicks) != 1:
            return
        target = Target.model_validate(clicks[0]["target"])
        known = {c.text for st in self.steps for c in st.expect.all_of if isinstance(c, TextCondition)}
        cands = [i.name for i in self._escalation_obs.items
                 if i.frame == target.frame and i.kind == "text" and not i.sensitive
                 and 3 <= len(i.name) <= 40 and re.search(r"[A-Z]{3}", i.name)
                 and i.name not in known and not re.search(r"\d+\.\d+", i.name)]
        # Prefer a screen title ("COMPLIANCE REMINDER") over a screen code ("TRN9000").
        landmark = next((c for c in cands if not re.search(r"\d", c)), cands[0] if cands else None)
        if not landmark:
            return
        code = re.sub(r"[^A-Z0-9]+", "_", landmark.upper()).strip("_")
        rule = OutcomeRule(
            id="proposed_" + code.lower(), kind=OutcomeKind.recoverable, code=code,
            when=TextCondition(text=landmark, frame=target.frame),
            message=f"Proposed from intervention {iv.id} by {iv.operator}: {iv.note or ''}".strip(),
            recovery=RecoveryRule(kind="click", target=target, max_attempts=2))
        import yaml

        self.s.evidence.write_text("proposed_profile_rules.yaml", yaml.safe_dump(
            [rule.model_dump(mode="json", exclude_none=True)], sort_keys=False))
        self.result.warnings.append(
            f"human resolved unknown state '{landmark}'; proposed profile rule written to proposed_profile_rules.yaml")
        self._log("rule_proposed", rule=rule.model_dump(mode="json", exclude_none=True))


async def replay(session: Session, cap: Capability, inputs: dict[str, Any], **kw) -> ReplayResult:
    return await Replayer(session, cap, inputs, **kw).run()
