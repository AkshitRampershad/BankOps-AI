# Design report

## 1. Architecture

```
                  ┌──────────── discovery (once) ────────────┐        ┌──── replay (every invocation) ────┐
 goal + inputs ──▶│ Planner (Claude) ─▶ Recorder ─▶ artifact │──────▶ │ Replayer: gate → resolve → act →  │──▶ ReplayResult
                  └───────┬───────────────────────────────────┘        │ await checkpoint | outcome rule   │
                          │         Session (one live browser)          └────────────┬───────────────────┘
                          └──▶ PolicyGate · SecretProvider · Redactor · Evidence · Controller(lease) ◀── Operator console
                                           │
                                  Surface protocol  ◀── WebSurface (Playwright); desktop = new implementation
```

The system is a single Python process with explicit seams. It is not a set of services. The seams are:

- **Surface.** Perception and action, the only code that knows about Playwright.
- **Planner.** The only code that knows about the LLM.
- **Artifact.** The only thing that crosses from discovery into replay.
- **Controller.** Who holds the live session.

Discovery and replay both run on one `Session` object. That means policy, redaction, secrets, evidence and handoff behave identically on both paths. Evaluators can also run everything with `pip install` and one command.

**Key decisions**

- **Structured perception, not pixels.** The planner sees a flattened, redacted text view of every frame: controls with their role and name, plus the context a human uses on legacy screens (the label in the same table row, the column header above a cell). This works on markup with no semantics. It is cheap and auditable, since the screen the model saw is stored verbatim, and it maps directly onto desktop accessibility trees (UIA/AX). Screenshots are kept for evidence and for the operator. Vision is the fallback for surfaces with no tree at all (§4).
- **Claude Opus 5.5 with adaptive thinking, one tool call per turn.** Every action is observed before the next one is chosen. The loop is a manual Messages API loop so that each tool call can be policy-checked, recorded and escalated.
- **"Record what you replay."** When the model picks element `[ref]`, the recorder builds ranked locator strategies and verifies each one against the live page: it must match exactly that element and nothing else. The action is then executed *through those strategies*, on the same code path replay uses. A locator that couldn't replay never enters an artifact.
- **Synchronous and in-process.** A queue would add nothing to a one-session vertical slice. The abstractions (per-session `Session`, per-tenant config, serialisable artifacts and results) are what a worker pool would need later.

## 2. Artifact schema

`src/cua/artifact.py` (`cua.capability/v1`) is strict: pydantic with `extra=forbid`, so unknown fields are rejected. An example is in `evidence/artifacts/`. An artifact has two parts.

**The contract**, which is all a calling agent needs:

| field | purpose |
|---|---|
| `id`, `version` (semver), `status` | Lifecycle: `draft → approved → deprecated`. |
| `digest()` | A hash of the executable parts. Approval stores it, so any edit after review voids the approval. |
| `description` | Agent-facing text. `tool_schema()` turns the artifact into a function-calling tool. |
| `inputs[]` | Typed `ParamSpec`: type, pattern/enum and **data classification** (`public/internal/pii/secret`). The classification drives redaction everywhere. |
| `outputs[]` | Typed `OutputSpec` with classification. Money is normalised (`"1,234.56"` becomes `"1234.56"`). |
| `risk` | `read` or `irreversible` at capability level. Every step also carries its own risk. |
| `outcomes[]` | Capability-specific outcome rules, merged with the app profile's rules. |
| `app` | Binds the artifact to a *product* and version range (`heritage_core >=4.2,<5`), not to a tenant. |
| `provenance` | Model, run id, tenant recorded on, count of human steps, reviewer. |

**The procedure.** Ordered `steps[]`, each with:

- `action` (`navigate/click/fill/select/press/extract`), `intent` in plain language, `risk`, and `actor` (`agent` or `human`, since human steps are flagged for review).
- `target`: a frame plus **ranked, verified locator strategies**, with a rationale for the choice.
- `value`: `{"param"}`, `{"secret"}` or `{"literal"}`. Inputs and credentials are never inlined.
- `expect`: a checkpoint (all-of text / element / url conditions).

The capability also has a final `success` check.

Locator strategies, most semantic first:

| strategy | how it finds the control | notes |
|---|---|---|
| `role` | Accessible role + name | Survives restyling; maps to UIA/AX. |
| `anchored` | Control in the unique table row whose cell reads "Member Number:" | How people find unlabelled legacy fields. |
| `table_cell` | (row key, column header) | Robust to row order. The model is asked for a row key that is stable across inputs. |
| `text` | Visible text, or a stable "Label:" prefix | |
| `css` | Structural DOM path | Brittle. Recorded for diagnosis and ranked last. |

`anchored` and `table_cell` are custom Playwright selector engines (`surface/web.py`).

Two further choices:

- **Canonicalisation.** Concrete input values inside locators and checkpoints are rewritten to `{{param}}` (`NO RECORD FOUND FOR MEMBER {{member_id}}`, `/hc/SUB0100?m={{member_id}}`). That is what makes one recording work for every member.
- **Artifact separate from transcript.** The artifact is separate from the model transcript and from the app profile. Sign-on, interstitials, error screens and PII labels are *product* knowledge in `profiles/heritage_core.yaml`, written once and shared by every capability and tenant. This keeps artifacts short and reviewable (`cua review` prints one in a screen) and means a new known interstitial is added in one place.

## 3. Determinism & error handling

**Determinism.** Replay never sleeps. Each step works like this:

1. Pass the policy gate.
2. Resolve the target to *exactly one* element. Strategies are tried in rank order. A count greater than 1 is `TARGET_AMBIGUOUS`, never "click the first one".
3. Act.
4. `await_state`: poll for the step's checkpoint *and* for every known outcome rule at the same time. The first state that appears decides what happens next:

| state observed | action |
|---|---|
| checkpoint holds | Next step. |
| **business** rule (`RECORD_NOT_FOUND`, `ACCESS_DENIED`, `VALIDATION_ERROR`) | Return `BUSINESS_OUTCOME` with the app's own text as detail. |
| **recoverable** rule (`SYSTEM_NOTICE` → click Acknowledge, `SESSION_EXPIRED` → re-auth) | Recover (bounded attempts), then **re-synchronise**. |
| **failure** rule (`APP_ERROR`) | `FAILED` with captured error text and a `retryable` hint. |
| none within the timeout | `CHECKPOINT_TIMEOUT`, or escalate to a human if the caller allows it. |

Checking known states *while* waiting is what separates "slow" from "wrong". A slow screen just takes longer (and adds a `slow` warning). A timeout only happens when the screen matches nothing the system knows.

**Re-synchronisation** handles recoveries that don't land where they started. After a session expiry, for example, the app returns to the inquiry screen, not the profile. Replay finds the latest step whose checkpoint holds *now* and resumes after it. It will never resume before an irreversible step that has already run, so a re-sync can't double-submit.

Checkpoints come from discovery: after each action the recorder diffs the screen and keeps new screen titles or codes and a changed frame URL. A checkpoint is kept only if it holds on the live page at that moment. A draft can be edited before approval.

**Result contract** (`results.py`): `SUCCEEDED` with typed `outputs`, `BUSINESS_OUTCOME` with `outcome{code, message, detail, step}`, `FAILED`, or `REJECTED` (pre-flight, UI never touched). A failure carries:

- `code`, one of 16: `TARGET_NOT_FOUND/AMBIGUOUS`, `CHECKPOINT_TIMEOUT`, `APP_ERROR`, `RECOVERY_EXHAUSTED`, `RESYNC_FAILED`, `EXTRACTION_FAILED`, `POLICY_VIOLATION`, `NOT_APPROVED`, `APPROVAL_DENIED`, …
- `step_id` and intent, `expected` vs `observed` (redacted screen text), and per-strategy locator `attempts`
- paths to a masked screenshot and redacted per-frame DOM snapshots

Recoveries, interventions and warnings are reported even on success, so a success that needed help is visible as such.

**UI drift (secondary).** When a non-primary locator matches, replay reports a `drift` warning per step. Structural (`css`) fallbacks are only consulted after a grace period, and **never for extraction or irreversible steps**: reading a balance from a positionally similar cell is worse than failing. `evidence/replay-13-*` shows this. On the second tenant without its overlay, navigation continues on fallbacks (with warnings), but the balance read fails with `TARGET_NOT_FOUND` and the column it looked for.

## 4. Heterogeneity & multi-tenant

**Surface seam.** Everything above `Surface` speaks in `Observation` / `UIItem` / `Target` / `Step`. Locator kinds are surface-neutral concepts, with each surface deciding how to honour them:

- `role` maps to the DOM accessibility tree, or to UIA `ControlType` + `Name` on Windows.
- `anchored` becomes "the edit control to the right of static text X", a common layout in desktop forms.
- `table_cell` maps to UIA Grid/Table patterns.

A desktop surface (pywinauto/UIA, or AX on macOS) implements `observe/resolve/perform/check`. Strategies it cannot honour, such as `css`, are skipped. Surfaces with no tree at all (Citrix/VDI, terminal emulators) get a vision surface: screenshot plus OCR, with `anchored` implemented as "control region to the right of OCR'd label". It is slower and would need a lower confidence threshold. The artifact schema, replay engine, rules, policy and handoff do not change. The legacy-web problems (frames, no labels, POST-back screens, table layout) are already handled by this implementation.

**Multi-tenant reuse** uses three layers, from most shared to most specific:

1. **Capability**, recorded once per *product*, bound to a version range.
2. **App profile**, per product: auth, known screens, PII labels.
3. **Tenant overlay**, per institution (`tenants/*.yaml`): base URL, version, secret bindings, policy, `text_aliases` (the tenant's configured labels, e.g. `Balance` → `Current Bal`, `Inquire` → `Search`), and `step_overrides` for structural differences, keyed by capability id and step id.

`apply_overlay` builds the executable plan per run. The original artifact is untouched, and every substitution is reported. `evidence/replay-12-*` shows the riverbend-recorded artifact succeeding on lakeside.

**Detecting and managing drift:**

- Replay refuses unattended runs on a tenant whose product version is outside the artifact's range.
- Per-step `fallback` and `drift` signals, plus the failed strategy attempts, say exactly *which label* changed. That is the input for writing an overlay entry. The next step is a bounded LLM pass that proposes the alias for review.
- In production these signals feed a per-(capability, tenant) health record. A drop in the primary-locator hit rate flags the tenant for re-verification before callers see failures.

## 5. Escalation & handoff

**Detecting "stuck":**

- **Replay:** an unexplained timeout, an unresolvable target, or a failed re-sync.
- **Discovery:** the model calls `request_human`; 3 consecutive failed actions; the same action on the same screen 3 times. (Running out of the step or time budget stops the run rather than escalating.)
- **Both:** any irreversible action (an approval request).

**Routing.** An `Intervention` carries the subject (capability@version or goal), the step and its intent, the reason, a masked screenshot and the redacted screen text. It is routed to stderr and the operator console. The `notifier` hook is the integration point for a queue or pager.

**Control transfer** (`handoff.py`) is a single-holder lease with an explicit state machine:

```
AUTOMATION → AWAITING_HUMAN → HUMAN → AUTOMATION
```

- Automation calls `assert_automation()` before every action, so it *cannot* act while a person holds the session.
- Operator commands carry a lease token, checked on every call and invalidated on release.
- `claim` is exclusive, and a timeout returns control with `ESCALATION_TIMEOUT`.

The operator acts on the **same Playwright page**: the live view plus click-by-element, click-by-coordinates and typing, or the real window with `--headed`.

**What the human did is recorded two ways:**

- Operator commands, with a synthesised, replayable target.
- DOM click/change events captured by an injected listener, including a person clicking directly in a headed browser. Typed values are never captured, only their length.

**Handing back:**

- Replay does not assume where the human left the screen. It re-synchronises on checkpoints, allowing forward jumps if the human completed steps.
- Discovery tells the model what the human did and continues. Human steps enter the artifact as `actor: human`, and `approve` refuses such artifacts without explicit acknowledgement.
- When a human gets past an unknown screen with a single click, replay writes a **proposed profile rule** (`proposed_profile_rules.yaml`) so the next run can handle it unattended. It is proposed for review, never applied silently.

Evidence: `replay-11-*` (stuck → take over → resume), `replay-15-*` and the sub-account discovery (approval).

## 6. Safety

- **Allowlist, enforced twice.**
  - The `PolicyGate` checks every step (from the model, from replay, from the re-auth flow): action type, and for navigation the origin and path allow/deny globs.
  - A Playwright route guard aborts *every request* outside the allowlist. So a link the gate didn't anticipate, such as `/__admin` or `/signoff` in the demo, is blocked at the network layer and logged.
- **Risk classes.** `read`, `reversible` and `irreversible`. Irreversible is inferred from policy (control names like Submit/Post/Delete/Transfer) *and* declared by the artifact, taking the maximum, so an artifact cannot downgrade a control policy considers dangerous.
- **Irreversible handling: require approval** (configurable to `block`). I chose approval over blocking because banks do need these flows automated, and over "flag and proceed" because an irreversible action cannot be un-flagged.
  - Discovery pauses for a human decision.
  - Unattended replay requires an **approved** artifact whose digest still matches **and** a caller-supplied `allow_irreversible`. Otherwise it is rejected *before touching the UI*.
  - Native `confirm()` dialogs are dismissed, never accepted.
- **Data handling:**
  - **Secrets** are secret refs resolved at execution time. The model never sees them. Their values are registered with the redactor and the screenshot masker.
  - **Redaction at every sink** (log writer, artifact linter, LLM observation renderer, failure summaries, DOM snapshots), using registered values (inputs classified `pii`, secrets), patterns (SSN, Luhn-checked PAN, phone, email) and **profile PII labels** (the value beside `Name:`/`SSN:`/`Date of Birth:` is masked structurally).
  - **Screenshots** are masked with locator masks before being written.
  - **Persisted results** redact inputs and outputs by classification.
  - **The artifact linter** refuses to save an artifact containing any registered value or sensitive pattern.
  - Playwright traces and HARs are deliberately not captured, because they cannot be scrubbed. An e2e test asserts that no member id, name, SSN, DOB, address or password appears in a failure run's persisted files. The committed `evidence/` was audited the same way.

**Limits:**

- Label-based PII masking only covers labels the profile lists. Free-text PII in an unexpected place is caught only if it matches a pattern.
- The LLM provider still receives balances and non-PII screen text.
- Name-based risk inference can miss a dangerously named-benign control. Per-capability review is the backstop.
- The operator's live view is unmasked, by design (they are authorised staff), and never persisted.
- Evidence directories are local files. Production needs encrypted storage and retention.

## 7. Cuts

**Deliberately left out:**

- Desktop and vision surfaces (designed in §4).
- A real co-browsing console (the console is minimal; the lease model is real).
- Queue, paging and tenant plumbing.
- A vault client.
- Per-tenant health and stability storage.
- Encrypted evidence storage.
- An LLM-assisted single-step recovery.
- Multi-run flakiness scoring.

**Stretch goals done:**

- Agent-facing catalog: `cua catalog --tools` and `cua invoke`, the unattended agent path.
- Canonicalisation and cross-tenant overlay, with drift detection.
- Draft → approved gating, bound to the artifact digest.

**Next, in order:**

1. **Bounded assisted fallback.** On `TARGET_NOT_FOUND`, one policy-checked LLM call proposes a replacement target for *that step only*. The result is used once, recorded as evidence, and proposed as a tenant overlay entry.
2. Per-(capability, tenant) health records (fallback hit rate, outcome mix, duration) that gate unattended replay and trigger re-verification.
3. A UIA desktop surface against a WinForms sample, to prove the seam.
4. A review UI for proposed profile rules and overlays, closing the human-handoff learning loop.
5. Checkpoint editing in review, and negative checkpoints ("this error text must be absent").
