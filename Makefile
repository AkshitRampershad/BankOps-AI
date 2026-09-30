PY ?= .venv/bin/python
CUA ?= .venv/bin/cua
ART = artifacts/heritage_core.member.read_savings_balance/1.0.0.json

.PHONY: setup app test discover discover-scripted replay replay-errors handoff evidence

setup:            ## venv + deps + browser
	python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
	@test -n "$$CUA_CHROMIUM" || [ -e /opt/pw-browsers/chromium ] || .venv/bin/playwright install chromium
	@test -f .env || cp .env.example .env

app:              ## run the demo target (riverbend tenant) on :8765
	$(CUA) app --tenant riverbend

test:
	.venv/bin/pytest -q

discover:         ## genuine LLM discovery run (needs ANTHROPIC_API_KEY)
	$(CUA) discover goals/read_savings_balance.yaml --start-app --console 8790

discover-scripted: ## same pipeline, deterministic planner, no API key
	$(CUA) discover goals/read_savings_balance.yaml --start-app \
	  --planner scripted:goals/scripted/read_savings_balance.yaml

replay:           ## LLM-free replay of the recorded artifact
	$(CUA) replay $(ART) --input member_id=100234 --start-app

replay-errors:    ## business outcome + recoverable + hard failure
	$(CUA) replay $(ART) --input member_id=999999 --start-app || true
	$(CUA) replay $(ART) --input member_id=100234 --start-app --fault '{"session_expire_once":true,"path":"MBR0100"}' || true
	$(CUA) replay $(ART) --input member_id=100234 --start-app --fault '{"error_once":true,"path":"MBR0100"}' || true

handoff:          ## unknown screen -> human takes the live session via the console (scripted operator)
	($(PY) scripts/mock_operator.py --plan '[{"click":"Remind Me Later"},{"release":"resume","note":"dismissed"}]' &) ; \
	$(CUA) replay $(ART) --input member_id=100234 --start-app --fault '{"unknown_dialog_once":true}' \
	  --escalation human --console 8790

evidence:         ## regenerate /evidence (LLM discovery when ANTHROPIC_API_KEY is set)
	$(PY) scripts/generate_evidence.py
