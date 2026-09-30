from cua.artifact import ClickStep, ExtractStep, NavigateStep, Risk, Target
from cua.config import load_policy, load_tenant
from cua.policy import PolicyGate, Verdict


def gate(handling="require_approval"):
    t = load_tenant("riverbend")
    p = load_policy(t.policy, t)
    p.irreversible_handling = handling
    return PolicyGate(p, t.base_url), p, t


def click(name, risk=Risk.reversible):
    return ClickStep(id="s01", intent="i", risk=risk,
                     target=Target(description=name, strategies=[{"kind": "role", "role": "button", "name": name}]))


def test_url_allowlist():
    _, p, t = gate()
    assert p.url_allowed(t.base_url + "/hc/MBR0100")[0]
    assert not p.url_allowed(t.base_url + "/__admin/faults")[0]  # denied path wins
    assert not p.url_allowed(t.base_url + "/hc/TRN0100")[0]
    assert not p.url_allowed("https://evil.example/hc/MBR0100")[0]
    assert not p.url_allowed(t.base_url + "/elsewhere")[0]


def test_navigate_outside_allowlist_denied():
    g, _, _ = gate()
    d = g.check(NavigateStep(id="s01", intent="i", path="/__admin/faults"))
    assert d.verdict == Verdict.deny


def test_irreversible_needs_approval_and_cannot_be_downgraded():
    g, _, _ = gate()
    step = click("Submit", risk=Risk.read)  # artifact claims 'read' — policy still infers irreversible
    assert g.classify(step) == Risk.irreversible
    assert g.check(step).verdict == Verdict.needs_approval
    assert g.check(step, pre_approved=True).verdict == Verdict.allow
    assert g.check(click("Inquire")).verdict == Verdict.allow
    assert g.classify(ExtractStep(id="s02", intent="i", output="x", target=step.target)) == Risk.read


def test_block_mode():
    g, _, _ = gate("block")
    assert g.check(click("Submit"), pre_approved=True).verdict == Verdict.deny
