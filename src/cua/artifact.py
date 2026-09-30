"""The capability artifact: a typed, versioned, reviewable description of a recorded flow.

Design notes (see REPORT.md §2 for the long form):

* The artifact is a *contract* first (inputs, outputs, outcomes, risk, success condition)
  and a *procedure* second (steps). A calling agent only needs the contract; a reviewer
  reads both.
* Every step names its target with an ordered list of locator strategies, most semantic
  first. Strategies are surface-neutral where possible (role/name, label anchor, table cell,
  visible text); surface-specific ones (css) are allowed but ranked last.
* Values are never inlined when they came from the caller or a vault: steps reference
  `{"param": ...}` or `{"secret": ...}`. Text used in locators/checkpoints can interpolate
  params with `{{name}}`.
* Checkpoints (`expect`) are recorded per step; replay waits on them instead of sleeping,
  and uses them to re-synchronise after interstitials or a human handoff.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_ID = "cua.capability/v1"

IDENT = r"^[a-z][a-z0-9_]*$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- types
class ValueType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    money = "money"  # decimal with currency formatting stripped
    date = "date"
    boolean = "boolean"
    enum = "enum"


class DataClass(str, Enum):
    """Drives redaction. `pii` and `secret` never reach logs, artifacts, or the LLM in clear."""

    public = "public"
    internal = "internal"
    pii = "pii"
    secret = "secret"


class ParamSpec(Strict):
    name: str = Field(pattern=IDENT)
    type: ValueType = ValueType.string
    description: str
    required: bool = True
    pattern: str | None = None
    enum: list[str] | None = None
    classification: DataClass = DataClass.internal


class OutputSpec(Strict):
    name: str = Field(pattern=IDENT)
    type: ValueType = ValueType.string
    description: str
    classification: DataClass = DataClass.internal
    nullable: bool = False


# --------------------------------------------------------------------------- values
class Literal_(Strict):
    literal: str


class ParamRef(Strict):
    param: str


class SecretRef(Strict):
    """Resolved at execution time from the secret provider (env/vault). Never stored."""

    secret: str


Value = Union[ParamRef, SecretRef, Literal_]


# --------------------------------------------------------------------------- targeting
class RoleLocator(Strict):
    """Accessibility role + accessible name. Most semantic; maps to UIA/AX on desktop."""

    kind: Literal["role"] = "role"
    role: str
    name: str
    exact: bool = True


class AnchoredLocator(Strict):
    """A control of `role` in the same table row / form line as a visible label.

    This is how humans find unlabeled fields on legacy screens ("the box next to
    'Member Number:'"). Maps to "nearest control right of text" on a desktop surface.
    """

    kind: Literal["anchored"] = "anchored"
    role: str
    anchor_text: str
    nth: int = 0


class TableCellLocator(Strict):
    """A cell addressed by (row key text, column header text) — robust to row order."""

    kind: Literal["table_cell"] = "table_cell"
    row_key: str
    column: str


class TextLocator(Strict):
    kind: Literal["text"] = "text"
    text: str
    exact: bool = True


class CssLocator(Strict):
    """Structural fallback. Web-only and brittle: recorded for diagnosis, ranked last."""

    kind: Literal["css"] = "css"
    selector: str


Locator = Annotated[
    Union[RoleLocator, AnchoredLocator, TableCellLocator, TextLocator, CssLocator],
    Field(discriminator="kind"),
]


class Target(Strict):
    frame: str | None = Field(None, description="Named frame/window the control lives in (None = top).")
    description: str = Field(description="Human-readable name of the control, for review and errors.")
    strategies: list[Locator] = Field(min_length=1)
    rationale: str = Field("", description="Why the primary strategy was chosen.")


# --------------------------------------------------------------------------- checkpoints
class TextCondition(Strict):
    kind: Literal["text"] = "text"
    text: str
    frame: str | None = None


class ElementCondition(Strict):
    kind: Literal["element"] = "element"
    target: Target


class UrlCondition(Strict):
    kind: Literal["url"] = "url"
    pattern: str  # glob against path(+query), e.g. "/hc/SUB0100*"
    frame: str | None = None


Condition = Annotated[Union[TextCondition, ElementCondition, UrlCondition], Field(discriminator="kind")]


class StateCheck(Strict):
    """All conditions must hold. An empty check means 'no observable change expected'."""

    all_of: list[Condition] = Field(default_factory=list)
    description: str = ""

    @property
    def empty(self) -> bool:
        return not self.all_of


# --------------------------------------------------------------------------- steps
class Risk(str, Enum):
    read = "read"  # no state change in the system of record
    reversible = "reversible"  # changes UI/session state only (navigation, typing into a form)
    irreversible = "irreversible"  # commits to the system of record (submit, post, delete)


class StepBase(Strict):
    id: str = Field(pattern=r"^s\d{2,3}$")
    intent: str = Field(description="What this step is for, in plain language.")
    risk: Risk = Risk.reversible
    actor: Literal["agent", "human"] = "agent"
    expect: StateCheck = Field(default_factory=StateCheck)
    timeout_ms: int = 10_000


class NavigateStep(StepBase):
    action: Literal["navigate"] = "navigate"
    path: str
    frame: str | None = None


class ClickStep(StepBase):
    action: Literal["click"] = "click"
    target: Target


class FillStep(StepBase):
    action: Literal["fill"] = "fill"
    target: Target
    value: Value


class SelectStep(StepBase):
    action: Literal["select"] = "select"
    target: Target
    value: Value  # option label


class PressStep(StepBase):
    action: Literal["press"] = "press"
    key: str
    target: Target | None = None


class ExtractStep(StepBase):
    action: Literal["extract"] = "extract"
    risk: Risk = Risk.read
    target: Target
    output: str = Field(pattern=IDENT)
    parse: ValueType = ValueType.string
    pattern: str | None = Field(None, description="Optional regex; group 1 (or whole match) is the value.")


Step = Annotated[
    Union[NavigateStep, ClickStep, FillStep, SelectStep, PressStep, ExtractStep],
    Field(discriminator="action"),
]


# --------------------------------------------------------------------------- outcomes
class OutcomeKind(str, Enum):
    business = "business"  # legitimate answer for the caller (not found, denied, validation)
    recoverable = "recoverable"  # known interstitial / transient: handle and continue
    failure = "failure"  # stop with a hard, debuggable error


class Recovery(Strict):
    """How to get past a recoverable state. After recovery the engine re-synchronises
    against step checkpoints, so recovery does not have to land on the same screen."""

    kind: Literal["click", "reauth", "wait_retry"]
    target: Target | None = None  # for click
    backoff_ms: int = 1_000
    max_attempts: int = 2


class OutcomeRule(Strict):
    id: str = Field(pattern=IDENT)
    kind: OutcomeKind
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    when: Condition
    message: str = Field(description="Caller-facing message; may reference {{params}}.")
    capture: str | None = Field(None, description="Regex applied to frame text; group 1 → outcome detail.")
    recovery: Recovery | None = None
    retryable: bool = False  # for failures: is a retry of the whole invocation reasonable?

    @field_validator("recovery")
    @classmethod
    def _recovery_only_for_recoverable(cls, v, info):
        if v is not None and info.data.get("kind") != OutcomeKind.recoverable:
            raise ValueError("recovery only allowed on recoverable rules")
        return v


# --------------------------------------------------------------------------- capability
class Status(str, Enum):
    draft = "draft"  # produced by discovery; replay allowed attended only
    approved = "approved"  # reviewed; unattended replay allowed
    deprecated = "deprecated"


class AppBinding(Strict):
    product: str = Field(description="Vendor product the flow was recorded on, e.g. heritage_core.")
    versions: str = Field(description="Product versions this artifact is valid for, e.g. '>=4.2,<5'.")
    surface: Literal["web", "legacy_web", "desktop"] = "legacy_web"
    entry: str = Field("/", description="Entry path relative to the tenant base URL.")
    requires_auth: bool = True


class Provenance(Strict):
    discovered_by: str
    run_id: str
    recorded_at: str
    recorded_on_tenant: str
    goal: str
    human_steps: int = 0
    reviewer: str | None = None
    reviewed_at: str | None = None
    approved_digest: str | None = Field(None, description="digest() at approval; any later edit voids approval.")


class Capability(Strict):
    schema_: Literal["cua.capability/v1"] = Field(SCHEMA_ID, alias="schema")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: Status = Status.draft
    title: str
    description: str = Field(description="Agent-facing description of what the capability does.")
    app: AppBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    risk: Risk = Risk.read
    steps: list[Step] = Field(min_length=1)
    success: StateCheck
    outcomes: list[OutcomeRule] = Field(default_factory=list)
    provenance: Provenance

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # -- helpers ------------------------------------------------------------------
    def digest(self) -> str:
        """Content hash of the executable parts (excludes status and review metadata), so an
        approval is bound to exactly the steps that were reviewed."""
        body = self.model_dump(mode="json", by_alias=True, exclude={"status", "provenance"})
        return "sha256:" + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json", by_alias=True, exclude_none=True), indent=2) + "\n"

    @classmethod
    def load(cls, path: str | Path) -> "Capability":
        return cls.model_validate_json(Path(path).read_text())

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_json())
        return p

    def output_spec(self, name: str) -> OutputSpec | None:
        return next((o for o in self.outputs if o.name == name), None)

    def input_spec(self, name: str) -> ParamSpec | None:
        return next((i for i in self.inputs if i.name == name), None)

    def tool_schema(self) -> dict:
        """The capability as a function-calling tool definition (agent-facing contract)."""
        props: dict = {}
        for p in self.inputs:
            s: dict = {"type": "string", "description": p.description}
            if p.pattern:
                s["pattern"] = p.pattern
            if p.enum:
                s["enum"] = p.enum
            props[p.name] = s
        outcomes = sorted({r.code for r in self.outcomes if r.kind == OutcomeKind.business})
        desc = self.description
        if outcomes:
            desc += " Possible business outcomes: " + ", ".join(outcomes) + "."
        if self.risk == Risk.irreversible:
            desc += " IRREVERSIBLE: commits changes to the system of record; requires approval."
        return {
            "name": self.id.replace(".", "__"),
            "description": desc,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in self.inputs if p.required],
                "additionalProperties": False,
            },
        }


PARAM_RE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")


def interpolate(text: str, params: dict[str, str]) -> str:
    """Replace {{name}} placeholders with parameter values."""

    def sub(m: re.Match) -> str:
        if m.group(1) not in params:
            raise KeyError(f"unknown parameter in template: {m.group(1)}")
        return str(params[m.group(1)])

    return PARAM_RE.sub(sub, text)
