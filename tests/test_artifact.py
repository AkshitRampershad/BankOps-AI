import json

import pytest
from pydantic import ValidationError

from cua.artifact import Capability, OutcomeRule, Status, interpolate

BASE = {
    "schema": "cua.capability/v1",
    "id": "app.thing.read",
    "version": "1.0.0",
    "title": "t",
    "description": "d",
    "app": {"product": "heritage_core", "versions": ">=4.2,<5"},
    "inputs": [{"name": "member_id", "description": "m", "pattern": r"^\d{6}$", "classification": "pii"}],
    "outputs": [{"name": "bal", "type": "money", "description": "b"}],
    "steps": [
        {"id": "s01", "action": "fill", "intent": "enter",
         "target": {"description": "box", "frame": "main",
                    "strategies": [{"kind": "anchored", "role": "textbox", "anchor_text": "Member:"}]},
         "value": {"param": "member_id"}},
        {"id": "s02", "action": "extract", "intent": "read", "output": "bal", "parse": "money",
         "target": {"description": "cell", "strategies": [{"kind": "table_cell", "row_key": "SAV", "column": "Bal"}]}},
    ],
    "success": {"all_of": [{"kind": "text", "text": "PROFILE {{member_id}}"}]},
    "provenance": {"discovered_by": "x", "run_id": "r", "recorded_at": "t", "recorded_on_tenant": "t", "goal": "g"},
}


def test_roundtrip_and_digest():
    cap = Capability.model_validate(BASE)
    again = Capability.model_validate_json(cap.to_json())
    assert again == cap
    d = cap.digest()
    cap.status = Status.approved  # status/provenance are not part of the executable digest
    cap.provenance.reviewer = "someone"
    assert cap.digest() == d
    cap.steps[1].target.strategies[0].column = "Available"  # any executable change voids it
    assert cap.digest() != d


def test_strict_schema_rejects_unknown_and_bad_ids():
    with pytest.raises(ValidationError):
        Capability.model_validate({**BASE, "surprise": 1})
    bad = json.loads(json.dumps(BASE))
    bad["steps"][0]["id"] = "step-one"
    with pytest.raises(ValidationError):
        Capability.model_validate(bad)
    with pytest.raises(ValidationError):  # recovery only on recoverable rules
        OutcomeRule.model_validate({"id": "x", "kind": "business", "code": "X", "message": "m",
                                    "when": {"kind": "text", "text": "t"}, "recovery": {"kind": "wait_retry"}})


def test_tool_schema_is_agent_contract():
    cap = Capability.model_validate(BASE)
    tool = cap.tool_schema()
    assert tool["name"] == "app__thing__read"
    assert tool["input_schema"]["required"] == ["member_id"]
    assert tool["input_schema"]["properties"]["member_id"]["pattern"] == r"^\d{6}$"
    assert tool["input_schema"]["additionalProperties"] is False


def test_interpolate():
    assert interpolate("A {{ member_id }} B", {"member_id": "7"}) == "A 7 B"
    with pytest.raises(KeyError):
        interpolate("{{nope}}", {})
