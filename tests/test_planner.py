from types import SimpleNamespace as NS

from cua.planner import AnthropicPlanner, ScriptedPlanner


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def reply(*blocks, stop="tool_use"):
    return NS(stop_reason=stop, content=list(blocks), usage=NS(input_tokens=10, output_tokens=3))


def tool(name, inp, id_="tu1"):
    return NS(type="tool_use", id=id_, name=name, input=inp)


async def test_anthropic_planner_loop_shape(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    p = AnthropicPlanner(model="claude-opus-5-5")
    fake = FakeMessages([
        reply(NS(type="text", text="Open the menu."), tool("click", {"ref": 4, "why": "menu"})),
        reply(NS(type="text", text="I need to think"), stop="end_turn"),  # no tool call -> nudged once
        reply(tool("done", {"summary": "ok", "success_text": "MEMBER PROFILE"}, "tu2")),
    ])
    p.client = NS(beta=NS(messages=fake))
    d = await p.first("GOAL: x", "[4] link \"Member Inquiry\"")
    assert (d.tool, d.args["ref"], d.rationale) == ("click", 4, "Open the menu.")
    kw = fake.calls[0]
    assert kw["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert kw["thinking"] == {"type": "adaptive"} and kw["fallbacks"] == "default"
    d = await p.next("ok: click done", "[9] button \"Inquire\"")
    assert d.tool == "done"
    msgs = fake.calls[-1]["messages"]
    # append-only history: user, assistant(tool_use), user(tool_result), assistant(text), user(nudge)...
    assert msgs[2]["content"][0]["type"] == "tool_result" and msgs[2]["content"][0]["tool_use_id"] == "tu1"
    assert "Current screen" in msgs[2]["content"][0]["content"]
    assert msgs[4]["content"].startswith("Respond by calling")


def test_scripted_planner_selects_by_semantics():
    screen = '[3] link "Member Inquiry"\n[7] textbox label="Member Number:" value=""\n[8] button "Inquire"'
    p = ScriptedPlanner([{"tool": "fill", "select": {"role": "textbox", "label": "Member Number:"}, "param": "m"}])
    d = p._decide(screen)
    assert d.tool == "fill" and d.args["ref"] == 7
