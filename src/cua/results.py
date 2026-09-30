"""The replay result contract returned to the calling agent.

Three things the caller must never confuse:

* SUCCEEDED         — the flow ran and the success checkpoint held; `outputs` are typed.
* BUSINESS_OUTCOME  — the system gave a legitimate answer that is not the happy path
                      ("no such member", "restricted account", "validation error").
                      Nothing is broken; the caller should act on `outcome.code`.
* FAILED            — something is wrong (app error, unrecognised screen, missing
                      element, policy violation). `failure` says which step, what was
                      expected, what was observed, and where the evidence is.

REJECTED is a FAILED-before-start: invalid inputs or an unapprovable request, reported
without touching the UI. Recoveries (interstitials dismissed, re-authentication,
retries) and human interventions are reported alongside any status, so a success that
needed help is visible as such.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    succeeded = "SUCCEEDED"
    business_outcome = "BUSINESS_OUTCOME"
    failed = "FAILED"
    rejected = "REJECTED"


class FailureCode(str, Enum):
    INPUT_INVALID = "INPUT_INVALID"  # caller supplied bad/missing params (pre-flight)
    NOT_APPROVED = "NOT_APPROVED"  # unattended replay of a draft / irreversible without approval
    POLICY_VIOLATION = "POLICY_VIOLATION"  # step outside allowlist
    APPROVAL_DENIED = "APPROVAL_DENIED"  # human declined an irreversible step
    AUTH_FAILED = "AUTH_FAILED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"  # control absent (drift, or wrong screen)
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"  # locator matched several controls
    CHECKPOINT_TIMEOUT = "CHECKPOINT_TIMEOUT"  # expected state never appeared; no rule explained it
    APP_ERROR = "APP_ERROR"  # application reported an internal error
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"  # known interstitial kept recurring
    RESYNC_FAILED = "RESYNC_FAILED"  # after recovery/handoff, screen matched no known step
    EXTRACTION_FAILED = "EXTRACTION_FAILED"  # output missing or failed type validation
    ESCALATION_TIMEOUT = "ESCALATION_TIMEOUT"  # no human picked up the intervention
    ABORTED_BY_OPERATOR = "ABORTED_BY_OPERATOR"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    INTERNAL = "INTERNAL"


class Outcome(BaseModel):
    code: str
    message: str
    detail: str | None = None  # captured app text (redacted)
    step_id: str | None = None
    rule: str | None = None


class Failure(BaseModel):
    code: FailureCode
    message: str
    step_id: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: str | None = None
    retryable: bool = False
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class Recovery(BaseModel):
    step_id: str | None
    rule: str
    code: str
    action: str
    resumed_at: str | None = None


class InterventionSummary(BaseModel):
    id: str
    kind: str
    reason: str
    step_id: str | None
    operator: str | None
    resolution: str | None
    human_actions: int = 0
    note: str | None = None


class StepTrace(BaseModel):
    step_id: str
    action: str
    status: str  # ok | skipped | failed
    strategy: str | None = None  # which locator strategy resolved the target
    fallback: bool = False  # true when a non-primary strategy was used (drift signal)
    ms: int = 0
    actor: str = "automation"


class ReplayResult(BaseModel):
    run_id: str
    capability: str
    version: str
    digest: str
    tenant: str
    status: RunStatus
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: Outcome | None = None
    failure: Failure | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    interventions: list[InterventionSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    steps: list[StepTrace] = Field(default_factory=list)
    duration_ms: int = 0
    evidence_dir: str | None = None
