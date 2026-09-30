"""Discovery: an LLM drives the live application once; the recorder turns what worked into
a capability artifact.

The important property is *record what you replay*: when the model picks element [ref],
the recorder first synthesises ranked locator strategies for it (each verified to match
exactly that element), and the action is then executed through those strategies — the
same resolution path replay uses. A locator that could not replay is never recorded.

After each action the recorder derives a checkpoint from what changed on screen (new
screen titles/codes, frame URL), so replay can wait on state instead of time.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .artifact import (
    AppBinding,
    Capability,
    ClickStep,
    DataClass,
    ExtractStep,
    FillStep,
    Literal_,
    OutputSpec,
    ParamRef,
    ParamSpec,
    PressStep,
    Provenance,
    Risk,
    SelectStep,
    StateCheck,
    Step,
    Target,
    TextCondition,
    UrlCondition,
    ValueType,
)
from .config import ROOT
from .handoff import Resolution
from .planner import Planner, PlannerDecision
from .policy import Verdict
from .replay import parse_value
from .runtime import PolicyDenied, Session
from .surface.base import Observation, TargetError, UIItem


class GoalInput(ParamSpec):
    value: str = Field(description="Example value used during discovery only; never written to the artifact.")


class GoalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability_id: str
    title: str
    description: str
    tenant: str
    goal: str
    entry: str = "/"
    version: str = "1.0.0"
    inputs: list[GoalInput] = Field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "GoalSpec":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


@dataclass
class DiscoveryResult:
    status: str  # succeeded | failed
    reason: str
    artifact_path: str | None
    run_id: str
    evidence_dir: str
    steps: int
    usage: dict[str, int]


MONEYISH = re.compile(r"^[-$()\d.,\s%]+$")


def _path(url: str) -> str:
    m = re.match(r"^[a-z]+://[^/]+(/[^#]*)?", url)
    return (m.group(1) if m and m.group(1) else "/") if m else url


class Recorder:
    def __init__(self, spec: GoalSpec, session: Session) -> None:
        self.spec = spec
        self.s = session
        self.params = {i.name: i.value for i in spec.inputs}
        self.steps: list[Step] = []
        self.outputs: list[OutputSpec] = []
        self.output_values: dict[str, Any] = {}

    def next_id(self) -> str:
        return f"s{len(self.steps) + 1:02d}"

    # ---------------------------------------------------------------- canonicalisation
    def canonicalize(self, obj: Any) -> Any:
        """Replace concrete input values inside locator/checkpoint text with {{param}}.

        This is what makes '/hc/SUB0100?m=100234' or 'NO RECORD FOUND FOR MEMBER 100234'
        reusable for any member."""
        if isinstance(obj, str):
            for name, val in sorted(self.params.items(), key=lambda kv: -len(kv[1])):
                if len(val) >= 3 and val in obj:
                    obj = obj.replace(val, "{{" + name + "}}")
            return obj
        if isinstance(obj, dict):
            return {k: (v if k in ("value",) else self.canonicalize(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.canonicalize(v) for v in obj]
        return obj

    def canon_target(self, t: Target) -> Target:
        return Target.model_validate(self.canonicalize(t.model_dump(mode="json")))

    # ---------------------------------------------------------------- checkpoints
    def _landmark(self, item: UIItem) -> bool:
        t = item.name
        if item.sensitive or item.kind != "text" or not (3 <= len(t) <= 40):
            return False
        if MONEYISH.match(t) or not re.search(r"[A-Za-z]", t):
            return False
        if self.s.redactor.contains_sensitive(t):
            return False
        return True

    def derive_expect(self, before: Observation, after: Observation) -> StateCheck:
        conds: list = []
        notes = []
        for f in after.frames:
            bf = next((x for x in before.frames if x.name == f.name), None)
            before_texts = set(before.texts(f.name)) if bf else set()
            new = [i for i in after.items if i.frame == f.name and i.name not in before_texts and self._landmark(i)]
            url_changed = bf is None or _path(bf.url) != _path(f.url)
            if not new and not url_changed:
                continue
            for i in new[:2]:
                conds.append(TextCondition(text=i.name, frame=f.name))
            if url_changed:
                path = _path(f.url).split("?")[0]
                conds.append(UrlCondition(pattern=path + "*", frame=f.name))
            notes.append(f"frame {f.name or 'top'}: " + ", ".join(i.name for i in new[:2]))
        chk = StateCheck(all_of=conds, description="; ".join(notes))
        return StateCheck.model_validate(self.canonicalize(chk.model_dump(mode="json")))

    # ---------------------------------------------------------------- build
    def build(self, success: StateCheck, planner_name: str, human_steps: int) -> Capability:
        t = self.s.tenant
        major, minor = (t.product_version.split(".") + ["0"])[:2]
        # Capability-level risk answers "does invoking this change the system of record?"
        overall = Risk.irreversible if any(st.risk == Risk.irreversible for st in self.steps) else Risk.read
        inputs = [ParamSpec.model_validate(i.model_dump(exclude={"value"})) for i in self.spec.inputs]
        cap = Capability(
            id=self.spec.capability_id,
            version=self.spec.version,
            title=self.spec.title,
            description=self.spec.description,
            app=AppBinding(product=t.product, versions=f">={major}.{minor},<{int(major) + 1}",
                           surface="legacy_web", entry=self.spec.entry),
            inputs=inputs,
            outputs=self.outputs,
            risk=overall,
            steps=self.steps,
            success=success,
            outcomes=[],
            provenance=Provenance(
                discovered_by=planner_name, run_id=self.s.run_id,
                recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                recorded_on_tenant=t.id, goal=self.spec.goal, human_steps=human_steps),
        )
        lint = lint_artifact(cap, self.s)
        if lint:
            raise ValueError("artifact failed lint: " + "; ".join(lint))
        return cap


def lint_artifact(cap: Capability, session: Session) -> list[str]:
    """Refuse to persist an artifact containing literal sensitive data."""
    problems = []
    body = cap.to_json()
    if session.redactor.contains_sensitive(body):
        problems.append("contains a registered secret/PII value or a sensitive pattern")
    for st in cap.steps:
        v = getattr(st, "value", None)
        if isinstance(v, Literal_) and session.redactor.contains_sensitive(v.literal):
            problems.append(f"{st.id}: literal value looks sensitive")
    return problems


class Discovery:
    def __init__(self, session: Session, spec: GoalSpec, planner: Planner, *, max_steps: int | None = None,
                 artifacts_dir: Path | None = None) -> None:
        self.s = session
        self.spec = spec
        self.planner = planner
        self.rec = Recorder(spec, session)
        self.max_steps = max_steps or session.policy.max_steps
        self.artifacts_dir = artifacts_dir or ROOT / "artifacts"
        self.human_steps = 0
        self._recent: list[str] = []
        self._errors = 0

    def _log(self, kind: str, /, **f) -> None:
        self.s.evidence.event(kind, **f)

    def task_text(self) -> str:
        goal = re.sub(r"\{\{\s*(\w+)\s*\}\}", r"<\1>", self.spec.goal)
        lines = [f"GOAL: {goal}", "", "INPUT PARAMETERS (use `fill` with `param`):"]
        for i in self.spec.inputs:
            shown = "«redacted»" if i.classification in (DataClass.pii, DataClass.secret) else i.value
            lines.append(f"- {i.name} ({i.type.value}, {i.classification.value}): {i.description} value={shown}")
        lines.append("\nYou are already signed in. Achieve the goal, extract the requested data, then call done.")
        return "\n".join(lines)

    async def observe(self, tag: str) -> tuple[Observation, str]:
        obs = await self.s.surface.observe()
        screen = obs.render(self.s.redactor.text)
        n = len(list((self.s.evidence.dir / "screens").glob("*.txt")))
        self.s.evidence.write_text(f"screens/{n + 1:02d}-{tag}.txt", screen)
        return obs, screen

    async def run(self) -> DiscoveryResult:
        s = self.s
        for i in self.spec.inputs:
            if i.classification in (DataClass.pii, DataClass.secret):
                s.register_sensitive(i.value, f"{i.classification.value}:{i.name}")
        self._log("discovery_started", goal=self.spec.goal, capability=self.spec.capability_id,
                  planner=self.planner.name, tenant=s.tenant.id)
        await s.surface.goto(self.spec.entry)
        if not await s.ensure_authenticated():
            return self._result("failed", "could not sign in", None)

        obs, screen = await self.observe("start")
        await s.capture("start")
        t_end = time.monotonic() + s.policy.max_seconds
        decision = await self.planner.first(self.task_text(), screen)
        steps_taken = 0
        while True:
            steps_taken += 1
            self._log("llm_decision", n=steps_taken, tool=decision.tool, args=decision.args,
                      rationale=decision.rationale, usage=decision.usage)
            if steps_taken > self.max_steps or time.monotonic() > t_end:
                return self._result("failed", "stopping condition: step/time budget exhausted", None)

            if decision.tool == "done":
                return await self._done(decision, obs)
            if decision.tool == "give_up":
                await s.capture("give-up", dom=True)
                return self._result("failed", f"agent gave up: {decision.args.get('reason')}", None)
            if decision.tool == "request_human":
                result, is_err = await self._handoff("stuck", decision.args.get("reason", "agent asked for help"), obs)
            else:
                result, is_err = await self._act(decision, obs)

            self._errors = self._errors + 1 if is_err else 0
            key = f"{decision.tool}:{sorted(decision.args.items())}:{hashlib.md5(screen.encode()).hexdigest()}"
            self._recent = (self._recent + [key])[-3:]
            if self._errors >= 3 or (len(self._recent) == 3 and len(set(self._recent)) == 1):
                why = "3 consecutive failed actions" if self._errors >= 3 else "agent is repeating the same action"
                self._log("stuck_detected", reason=why)
                hres, _ = await self._handoff("stuck", f"automatic stuck detection: {why}", obs)
                result += "\n" + hres
                self._errors, self._recent = 0, []

            if result.startswith("ABORT"):
                return self._result("failed", result, None)
            obs, screen = await self.observe(f"after-{steps_taken:02d}")
            decision = await self.planner.next(result, screen, is_error=is_err)

    # ---------------------------------------------------------------- actions
    async def _act(self, d: PlannerDecision, obs: Observation) -> tuple[str, bool]:
        s, rec = self.s, self.rec
        a = d.args
        item = obs.item(int(a.get("ref", -1))) if "ref" in a else None
        if item is None:
            return f"error: there is no element [{a.get('ref')}] on the current screen", True
        try:
            target = await s.surface.synthesize(item, row_key=a.get("row_key"))
        except TargetError as e:
            return f"error: cannot build a reliable locator for [{item.ref}]: {e.detail}", True
        target = rec.canon_target(target)
        why = a.get("why") or d.rationale or d.tool
        sid = rec.next_id()
        try:
            if d.tool == "click":
                step: Step = ClickStep(id=sid, intent=why, target=target)
            elif d.tool == "fill":
                value = self._value(a)
                step = FillStep(id=sid, intent=why, target=target, value=value)
            elif d.tool == "select_option":
                value = self._value(a, key="option")
                step = SelectStep(id=sid, intent=why, target=target, value=value)
            elif d.tool == "press_key":
                step = PressStep(id=sid, intent=why, target=target, key=a["key"])
            elif d.tool == "extract":
                name = re.sub(r"[^a-z0-9_]", "_", a["output_name"].lower()).strip("_") or "value"
                pattern = a.get("pattern")
                if not pattern and (m := re.match(r"^([A-Za-z][^:\d]{1,40}:)\s*\S", item.name)):
                    pattern = re.escape(m.group(1)) + r"\s*(.+)$"  # "Label: value" -> value
                step = ExtractStep(id=sid, intent=why, target=target, output=name, parse=ValueType(a["type"]),
                                   pattern=pattern)
            else:
                return f"error: unknown tool {d.tool}", True
        except ValueError as e:
            return f"error: {e}", True

        step.risk = s.gate.classify(step)
        approved_now = False
        verdict = s.gate.check(step)
        if verdict.verdict == Verdict.deny:
            self._log("policy_denied", step_id=sid, reason=verdict.reason)
            return f"error: blocked by policy: {verdict.reason}", True
        if verdict.verdict == Verdict.needs_approval:
            iv = await self._escalate("approval", f"approve irreversible action: {why} ({target.description})", obs, sid, why)
            if iv.resolution != Resolution.approve.value:
                return f"error: a human operator did not approve this irreversible action ({iv.resolution})", True
            approved_now = True

        before = obs
        try:
            if isinstance(step, ExtractStep):
                await s.execute(step, rec.params)
                raw, _ = await s.surface.read(step.target, rec.params, 5_000)
                try:
                    val = parse_value(raw, step.parse, step.pattern)
                except ValueError as e:
                    return f"error: extracted text does not parse as {step.parse.value}: {e}", True
                cls = DataClass.pii if item.sensitive else DataClass.internal
                if cls == DataClass.pii:
                    s.register_sensitive(str(val), f"pii:{step.output}")
                rec.outputs = [o for o in rec.outputs if o.name != step.output] + [OutputSpec(
                    name=step.output, type=step.parse, description=a.get("description", step.output),
                    classification=cls)]
                rec.output_values[step.output] = val
                rec.steps.append(step)
                self._log("step_recorded", step=step.model_dump(mode="json"))
                shown = "«pii»" if cls == DataClass.pii else val
                return f"ok: extracted {step.output} = {shown}", False
            await s.execute(step, rec.params, approved_now=approved_now)
        except PolicyDenied as e:
            return f"error: blocked by policy: {e.decision.reason}", True
        except TargetError as e:
            return f"error: action failed, element not actionable: {e.detail}", True
        except KeyError as e:
            return f"error: {e}", True
        except Exception as e:  # playwright timeouts etc.
            return f"error: action failed: {str(e).splitlines()[0][:200]}", True

        after = await s.surface.observe()
        if isinstance(step, (ClickStep, PressStep, SelectStep)):
            step.expect = rec.derive_expect(before, after)
            if not step.expect.empty and not await s.check_all(step.expect, rec.params):
                step.expect = StateCheck()  # derived checkpoint must hold right now, or we don't keep it
        rec.steps.append(step)
        self._log("step_recorded", step=step.model_dump(mode="json"))
        await s.capture(f"step-{sid}")
        note = f" (checkpoint: {step.expect.description})" if not step.expect.empty else ""
        return f"ok: {d.tool} done{note}", False

    def _value(self, a: dict, key: str = "text"):
        if a.get("param"):
            if a["param"] not in self.rec.params:
                raise ValueError(f"unknown parameter {a['param']!r}; available: {list(self.rec.params)}")
            return ParamRef(param=a["param"])
        if a.get(key) is None:
            raise ValueError(f"provide `param` or `{key}`")
        text = str(a[key])
        for name, val in self.rec.params.items():
            if text == val:
                return ParamRef(param=name)  # canonicalise a literal that is actually an input
        if self.s.redactor.contains_sensitive(text):
            raise ValueError("literal text looks sensitive; use a parameter")
        return Literal_(literal=text)

    # ---------------------------------------------------------------- handoff
    async def _escalate(self, kind: str, reason: str, obs: Observation, step_id: str | None, intent: str | None):
        shots = await self.s.capture(f"escalation-{kind}")
        return await self.s.controller.escalate(
            kind=kind, reason=reason, subject=f"discovery: {self.spec.goal}", step_id=step_id, step_intent=intent,
            screen_text=obs.render(self.s.redactor.text), screenshot=shots[0] if shots else None)

    async def _handoff(self, kind: str, reason: str, obs: Observation) -> tuple[str, bool]:
        iv = await self._escalate(kind, reason, obs, self.rec.next_id(), reason)
        if iv.resolution in (Resolution.abort.value, Resolution.timeout.value):
            return f"ABORT: intervention {iv.id} ended with {iv.resolution}", True
        # Operator commands arrive with synthesised targets; record them as human steps.
        n = 0
        for act in iv.human_actions:
            if act.get("source") != "operator" or "target" not in act:
                continue
            target = self.rec.canon_target(Target.model_validate(act["target"]))
            sid = self.rec.next_id()
            if act["action"] == "click":
                st: Step = ClickStep(id=sid, intent=f"human: {act.get('note') or 'operator click'}", target=target,
                                     actor="human")
            elif act["action"] in ("fill", "select"):
                val = act.get("param")
                value = ParamRef(param=val) if val else Literal_(literal=act.get("literal", ""))
                cls = FillStep if act["action"] == "fill" else SelectStep
                st = cls(id=sid, intent="human: operator input", target=target, value=value, actor="human")
            else:
                continue
            st.risk = self.s.gate.classify(st)
            self.rec.steps.append(st)
            n += 1
            self.human_steps += 1
        self._log("handoff_recorded", intervention_id=iv.id, human_steps=n, dom_events=len(iv.human_actions) - n)
        return (f"A human operator ({iv.operator}) took control ({len(iv.human_actions)} actions) and returned it: "
                f"{iv.note or 'no note'}. Continue from the current screen."), False

    # ---------------------------------------------------------------- finish
    async def _done(self, d: PlannerDecision, obs: Observation) -> DiscoveryResult:
        s, rec = self.s, self.rec
        if not rec.steps:
            return self._result("failed", "agent reported done without taking any action", None)
        success_text = (d.args.get("success_text") or "").strip()
        conds = []
        last = next((st.expect for st in reversed(rec.steps) if not st.expect.empty), None)
        if last:
            conds += last.all_of
        known = {getattr(c, "text", None) for c in conds}
        if (success_text and success_text not in known and success_text in await s.surface.frame_text(None)
                and not s.redactor.contains_sensitive(success_text)):
            conds.append(TextCondition(text=rec.canonicalize(success_text), frame=None))
        success = StateCheck(all_of=conds, description=f"final screen reached: {d.args.get('summary', '')}"[:200])
        if success.empty or not await s.check_all(success, rec.params):
            self._log("done_rejected", reason="no verifiable success checkpoint")
            return self._result("failed", "could not derive a success checkpoint that holds on the final screen", None)
        await s.capture("final")
        cap = rec.build(success, self.planner.name, self.human_steps)
        path = self.artifacts_dir / cap.id / f"{cap.version}.json"
        cap.save(path)
        self.s.evidence.write_text("artifact.json", cap.to_json())
        self._log("artifact_saved", path=str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                  digest=cap.digest(), steps=len(cap.steps), outputs=[o.name for o in cap.outputs])
        return self._result("succeeded", d.args.get("summary", "done"), str(path))

    def _result(self, status: str, reason: str, path: str | None) -> DiscoveryResult:
        usage = getattr(self.planner, "usage_total", {})
        self._log("discovery_finished", status=status, reason=reason, artifact=path, usage=usage)
        r = DiscoveryResult(status=status, reason=reason, artifact_path=path, run_id=self.s.run_id,
                            evidence_dir=str(self.s.evidence.dir), steps=len(self.rec.steps), usage=usage)
        self.s.evidence.write_json("result.json", r.__dict__)
        return r
