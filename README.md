# Computer-Use Automation System for Legacy Banking

Built an AI-powered computer-use system that learns workflows from legacy banking UIs using Claude, converts successful interactions into versioned capability artifacts, and deterministically replays them without an LLM. Includes policy-controlled execution, fault recovery, tenant-aware UI adaptation, structured outcomes, and human-in-the-loop takeover for ambiguous or irreversible actions.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the agent invokes it.

The design write-up is in **[REPORT.md](REPORT.md)**. Evidence from the runs is in **[evidence/](evidence/INDEX.md)**. A one-page overview is at **https://akshitrampershad.github.io/Computer-Use-Automation-System/** (source: `docs/index.html`).

The target is **Heritage Core** (`demo_app/`), a deliberately hostile local stand-in for a core-banking back office:

- It uses a `<frameset>`, table layouts and `<font>` tags, with no `<label for>`, ids or test ids.
- Screens POST back to the same URL, so URLs alone can't tell you which screen you're on.
- Business outcomes are shown as red text.
- Faults can be injected: interstitial notices, session expiry, slow responses, application errors, and an "unknown" reminder screen.

Two tenants, `riverbend` and `lakeside`, run the same vendor product with different labels and versions. All data is fictional.

## Setup

Requires Python ≥ 3.11 and Chromium for Playwright.

```bash
make setup            # venv, deps (incl. dev), `playwright install chromium`, creates .env from .env.example
# or manually:
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/playwright install chromium
cp .env.example .env
```

`.env` (git-ignored):

| variable | needed for | notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | discovery with the real model | Not needed for replay, tests, or the scripted planner. |
| `HC_USERNAME`, `HC_PASSWORD` | signing in to the demo app | Fake demo credentials (`operator1` / `demo-pass-123`). They are bound to secret refs in `tenants/*.yaml` and resolved at run time. The model never sees them, and they are never written to artifacts or logs. |
| `CUA_MODEL`, `CUA_EFFORT` | optional | Defaults are `claude-opus-5-5` and `medium`. |
| `CUA_CHROMIUM` | optional | Path to a Chromium binary if you don't use Playwright's bundled one. |

**Running without live services:** replay, the test suite and `--planner scripted:<file>` need no network or API key. The demo app runs locally. Add `--start-app` to any command to start it in-process.

## Demo path

```bash
# 1. Discovery: Claude drives the live app once and records a capability (draft).
.venv/bin/cua discover goals/read_savings_balance.yaml --start-app --console 8790
#    -> artifacts/heritage_core.member.read_savings_balance/1.0.0.json
#    -> runs/discover-*/ (events.jsonl, redacted screens the model saw, masked screenshots)

# 2. Review what was recorded (steps, locator strategies, checkpoints, contract).
.venv/bin/cua review artifacts/heritage_core.member.read_savings_balance/1.0.0.json

# 3. Replay deterministically (no LLM) with different inputs.
A=artifacts/heritage_core.member.read_savings_balance/1.0.0.json
.venv/bin/cua replay $A --input member_id=100234 --start-app   # SUCCEEDED  {"savings_balance": "1234.56"}
.venv/bin/cua replay $A --input member_id=100377 --start-app   # SUCCEEDED  {"savings_balance": "58002.10"}
.venv/bin/cua replay $A --input member_id=999999 --start-app   # BUSINESS_OUTCOME RECORD_NOT_FOUND
.venv/bin/cua replay $A --input member_id=12AB   --start-app   # REJECTED INPUT_INVALID (UI never touched)

# 4. Runtime faults (test harness arms them in the demo app first).
.venv/bin/cua replay $A --input member_id=100234 --start-app --fault '{"session_expire_once":true,"path":"MBR0100"}'  # recovered
.venv/bin/cua replay $A --input member_id=100234 --start-app --fault '{"error_once":true,"path":"MBR0100"}'          # FAILED APP_ERROR + evidence

# 5. Human handoff: unknown screen -> intervention -> operator takes the live session -> resume.
make handoff            # scripted operator; or open http://127.0.0.1:8790 yourself while this runs:
.venv/bin/cua replay $A --input member_id=100234 --start-app --fault '{"unknown_dialog_once":true}' --escalation human --console 8790

# 6. Same artifact on a second tenant (label overlay), and drift detection without it.
.venv/bin/cua replay $A --tenant lakeside --input member_id=100234 --start-app
.venv/bin/cua replay $A --tenant lakeside --input member_id=100234 --start-app --no-overlay   # FAILED TARGET_NOT_FOUND + drift warnings

# 7. Agent-facing: approve (binds approval to the artifact digest), list as tools, invoke unattended.
.venv/bin/cua approve $A --reviewer your.name
.venv/bin/cua catalog --tools
.venv/bin/cua invoke heritage_core__member__read_savings_balance --args '{"member_id":"100234"}' --start-app
```

The irreversible flow is `goals/open_sub_account.yaml`. During discovery the agent pauses at **Submit** until an operator approves in the console. Replay rejects the capability unless it is approved and the caller passes `--allow-irreversible`, or runs with `--escalation human`.

Other commands:

- **Scripted planner (no API key):** `make discover-scripted`
- **Full evidence set:** `make evidence`. It uses Claude when `ANTHROPIC_API_KEY` is set, otherwise the scripted planner, and labels the output accordingly.
- **Tests:** `make test`. 31 tests, including end-to-end runs against the real browser and demo app.

### Taking control as a human

`--console PORT` serves a minimal operator console. It lists interventions with their context (capability, step, reason, masked screenshot and redacted screen text). **Take control** gives you the lease and a live view of the same Playwright page: click the image to click the page, or type text. **Return control** resumes the run. Approval requests are resolved with **Approve** or **Deny**.

With `--headed` you can operate the real browser window directly. Claim and release through the console, and your clicks and changes are captured by an injected listener either way.

## Layout

```
src/cua/
  artifact.py      capability schema (the contract + the procedure)
  results.py       replay result contract and failure taxonomy
  surface/base.py  surface seam: Observation / UIItem / Surface protocol
  surface/web.py   Playwright surface: frame-walking perception, custom legacy locator engines,
                   masked screenshots, redacted DOM snapshots, human-action capture
  planner.py       Claude planner (manual tool loop) + scripted planner
  discovery.py     discovery loop + recorder (locator synthesis, checkpoints, canonicalisation)
  replay.py        deterministic replay engine (rules, recovery, resync, escalation, overlay)
  runtime.py       live session: policy gate, secrets, evidence, waits, auth
  policy.py        allowlist + risk classification
  handoff.py       control lease state machine + interventions
  console.py       operator console (HTTP); operator_client.py = scripted operator
  redaction.py     value / pattern / label-based redaction
profiles/          per vendor product: auth, known screens -> outcome rules, PII labels
tenants/           per institution: base URL, version, secret bindings, label overlay
policies/          allowlists and irreversible-action handling
goals/             discovery requests (+ scripted plans for the no-API-key planner)
artifacts/         recorded capabilities
evidence/          discovery + replay runs (see evidence/INDEX.md)
demo_app/          the Heritage Core target (stdlib only)
```

## What is mocked, and why

- **The target app.** It stands in for a real core. The brief rules out real bank systems, and a local app lets faults be injected deterministically.
- **The operator console.** It is minimal: polled screenshots and click forwarding, not a co-browsing product. The lease, context routing, action capture and resume/resync are real (`handoff.py`, `console.py`).
- **The human operator in the evidence runs.** `operator_client.py` drives the console over HTTP, exactly as a browser would.
- **Intervention routing.** It goes to stderr and the console only. A production system would use a queue or paging integration, which is a hook on `Controller.notifier`.
- **The secret store.** It reads environment variables (`SecretProvider`). A vault client would drop in behind the same interface.
- **Desktop surfaces.** They are designed but not built. See REPORT.md §4.
