import pytest

from cua.artifact import Capability, ValueType
from cua.config import load_tenant
from cua.replay import apply_overlay, parse_value, validate_inputs, version_ok

from .test_artifact import BASE


def test_parse_value():
    assert parse_value("$1,234.56", ValueType.money) == "1234.56"
    assert parse_value("(9.10)", ValueType.money) == "-9.10"
    assert parse_value("-9,410.77", ValueType.money) == "-9410.77"
    assert parse_value("1,204", ValueType.integer) == 1204
    assert parse_value("Confirmation #: SA70001", ValueType.string, r"Confirmation #:\s*(.+)$") == "SA70001"
    with pytest.raises(ValueError):
        parse_value("N/A", ValueType.money)
    with pytest.raises(ValueError):
        parse_value("   ", ValueType.string)


def test_validate_inputs():
    cap = Capability.model_validate(BASE)
    assert validate_inputs(cap, {"member_id": "100234"}) == []
    assert "pattern" in validate_inputs(cap, {"member_id": "12AB"})[0]
    assert "missing" in validate_inputs(cap, {})[0]
    assert "unknown" in validate_inputs(cap, {"member_id": "100234", "x": "1"})[0]


def test_version_ranges():
    assert version_ok(">=4.2,<5", "4.2.7") and version_ok(">=4.2,<5", "4.3.1")
    assert not version_ok(">=4.2,<5", "5.0.0") and not version_ok(">=4.2,<5", "4.1.9")


def test_overlay_maps_vocabulary_without_touching_original():
    cap = Capability.model_validate(BASE)
    t = load_tenant("lakeside")
    t.text_aliases = {"Member:": "Account No.:", "Bal": "Current Bal"}
    plan, notes = apply_overlay(cap, t)
    assert plan.steps[0].target.strategies[0].anchor_text == "Account No.:"
    assert plan.steps[1].target.strategies[0].column == "Current Bal"
    assert cap.steps[0].target.strategies[0].anchor_text == "Member:"  # original untouched
    assert len(notes) == 2
