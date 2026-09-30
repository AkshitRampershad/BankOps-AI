"""Planners decide the next action during discovery. They see only redacted observations
and parameter *names* (never credentials or PII values), and act only through a small set
of tools whose effects the runtime validates, policy-checks, and records.

* AnthropicPlanner — Claude via the Messages API with a manual tool loop (one action per
  turn, so every action is observed before the next is chosen).
* ScriptedPlanner  — deterministic stand-in with the same interface, used by tests and for
  running the pipeline without model access. It is *not* a substitute for the real
  discovery run and its evidence is labelled as scripted.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

SYSTEM_PROMPT = """\
You are the discovery agent of a computer-use automation system used by banks and credit unions.
You operate a legacy back-office application through a structured view of the screen, to achieve a goal ONCE.
Your successful run is recorded and turned into a deterministic, replayable capability, so HOW you do it matters:

- Act one step at a time. After each action you will see the new screen.
- The screen is given as frames. Each element has a [ref] number, a role and its visible text. Controls without
  their own name show the label next to them (label="..."). Table cells show their row and column context.
- Prefer the most direct, stable path a trained operator would take: menu links, labelled fields, named buttons.
  Avoid incidental elements.
- Inputs are provided as named parameters. To type an input, call `fill` with `param` set to the parameter name.
  Never type parameter values literally, and never invent data. Values shown as «...» are redacted on purpose.
- Read the requested data with `extract`, naming the output. For table cells, give `row_key`: the text in the same row
  that identifies WHICH row to read and stays the same for any input (e.g. an account type, not an amount).
- Do not press buttons that commit changes (Submit, Post, Delete, Transfer, Approve...) unless the goal explicitly
  requires it; such actions pause for human approval.
- If the screen shows an error, a notice or something unexpected, handle it if it is safe and obvious
  (e.g. acknowledge a notice); otherwise call `request_human` with a clear reason.
- When the goal is achieved, call `done` with a short summary and `success_text`: a short text visible on the final
  screen that proves the goal state was reached (a screen title, not data).
- If the goal cannot be achieved (e.g. the record does not exist), call `give_up` with the reason.
"""

_REF = {"type": "integer", "description": "The [ref] number of the element on the current screen."}
_WHY = {"type": "string", "description": "One short sentence: why this action, for the audit log."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click a link, button, or other element.",
        "input_schema": {"type": "object", "properties": {"ref": _REF, "why": _WHY},
                         "required": ["ref", "why"], "additionalProperties": False},
    },
    {
        "name": "fill",
        "description": "Type into a text field. Use `param` for goal inputs; `text` only for fixed, non-sensitive text.",
        "input_schema": {"type": "object", "properties": {
            "ref": _REF,
            "param": {"type": "string", "description": "Name of the input parameter whose value to type."},
            "text": {"type": "string", "description": "Fixed literal text (not an input, not sensitive)."},
            "why": _WHY}, "required": ["ref", "why"], "additionalProperties": False},
    },
    {
        "name": "select_option",
        "description": "Choose an option in a drop-down, by its visible label, or from a parameter.",
        "input_schema": {"type": "object", "properties": {
            "ref": _REF, "option": {"type": "string"}, "param": {"type": "string"}, "why": _WHY},
            "required": ["ref", "why"], "additionalProperties": False},
    },
    {
        "name": "press_key",
        "description": "Press a keyboard key (e.g. Enter, Tab) in an element.",
        "input_schema": {"type": "object", "properties": {"ref": _REF, "key": {"type": "string"}, "why": _WHY},
                         "required": ["ref", "key", "why"], "additionalProperties": False},
    },
    {
        "name": "extract",
        "description": "Read the value of an element into a named output of the capability.",
        "input_schema": {"type": "object", "properties": {
            "ref": _REF,
            "output_name": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
            "type": {"type": "string", "enum": ["string", "integer", "decimal", "money", "date", "boolean"]},
            "description": {"type": "string", "description": "What this output means, for the calling agent."},
            "row_key": {"type": "string", "description": "For table cells: stable text identifying the row."},
            "pattern": {"type": "string",
                        "description": "Optional regex whose group 1 is the value, when the element holds a label too."},
            "why": _WHY}, "required": ["ref", "output_name", "type", "description", "why"],
            "additionalProperties": False},
    },
    {
        "name": "done",
        "description": "The goal is achieved. Outputs are the values you extracted.",
        "input_schema": {"type": "object", "properties": {
            "summary": {"type": "string"},
            "success_text": {"type": "string", "description": "Short text on the final screen proving success."}},
            "required": ["summary", "success_text"], "additionalProperties": False},
    },
    {
        "name": "request_human",
        "description": "Stop and ask a human operator to take over the live session.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}},
                         "required": ["reason"], "additionalProperties": False},
    },
    {
        "name": "give_up",
        "description": "The goal cannot be achieved; explain why.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}},
                         "required": ["reason"], "additionalProperties": False},
    },
]


@dataclass
class PlannerDecision:
    tool: str
    args: dict[str, Any]
    rationale: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class Planner(Protocol):
    name: str

    async def first(self, task: str, screen: str) -> PlannerDecision: ...

    async def next(self, result: str, screen: str, *, is_error: bool = False) -> PlannerDecision: ...


class AnthropicPlanner:
    def __init__(self, model: str | None = None, effort: str | None = None) -> None:
        import anthropic

        self.client = anthropic.AsyncAnthropic()
        self.model = model or os.environ.get("CUA_MODEL", "claude-opus-5-5")
        self.effort = effort or os.environ.get("CUA_EFFORT", "medium")
        self.fallbacks = os.environ.get("CUA_FALLBACKS", "1") != "0"
        self.name = f"anthropic:{self.model}"
        self.messages: list[dict[str, Any]] = []
        self._pending_tool_id: str | None = None
        self.usage_total = {"input_tokens": 0, "output_tokens": 0}

    async def _call(self) -> PlannerDecision:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            # One action per turn: the next decision must see the effect of this one.
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            messages=self.messages,
        )
        if self.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        for attempt in range(2):
            resp = await self.client.beta.messages.create(**kwargs)
            u = resp.usage
            usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens}
            for k in usage:
                self.usage_total[k] += usage[k]
            if resp.stop_reason == "refusal":
                return PlannerDecision("give_up", {"reason": "model declined the request"}, usage=usage)
            # Keep the full assistant content (incl. thinking blocks) — history stays append-only.
            self.messages.append({"role": "assistant", "content": resp.content})
            text = " ".join(b.text for b in resp.content if b.type == "text").strip()
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if tool_uses:
                tu = tool_uses[0]
                self._pending_tool_id = tu.id
                args = tu.input if isinstance(tu.input, dict) else json.loads(tu.input)
                return PlannerDecision(tu.name, args, rationale=text or args.get("why", ""), usage=usage)
            if resp.stop_reason == "max_tokens":
                return PlannerDecision("give_up", {"reason": "model output truncated (max_tokens)"}, usage=usage)
            self._pending_tool_id = None
            self.messages.append({"role": "user", "content": "Respond by calling exactly one of the tools."})
        return PlannerDecision("give_up", {"reason": "model did not call a tool"})

    async def first(self, task: str, screen: str) -> PlannerDecision:
        self.messages = [{"role": "user", "content": f"{task}\n\nCurrent screen:\n{screen}"}]
        return await self._call()

    async def next(self, result: str, screen: str, *, is_error: bool = False) -> PlannerDecision:
        content = f"{result}\n\nCurrent screen:\n{screen}"
        if self._pending_tool_id:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": self._pending_tool_id, "content": content,
                 "is_error": is_error}]})
        else:
            self.messages.append({"role": "user", "content": content})
        self._pending_tool_id = None
        return await self._call()


class ScriptedPlanner:
    """Replays a list of intents against the *current* observation, choosing refs by
    matching role/name/label/row text — so it exercises the same perception, recording,
    and policy paths as the model does, deterministically."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.name = "scripted"
        self.usage_total = {"input_tokens": 0, "output_tokens": 0}

    @staticmethod
    def _find(screen: str, sel: dict[str, str]) -> int | None:
        for line in screen.splitlines():
            m = re.match(r"\[(\d+)\] (\S+)(.*)", line)
            if not m:
                continue
            ref, role, rest = int(m.group(1)), m.group(2), m.group(3)
            if sel.get("role") and sel["role"] != role:
                continue
            if "name" in sel and f'"{sel["name"]}"' not in rest.split(" label=")[0].split(" (")[0]:
                continue
            if "name_prefix" in sel and not rest.strip().startswith(f'"{sel["name_prefix"]}'):
                continue
            if "label" in sel and f'label="{sel["label"]}"' not in rest:
                continue
            if "row" in sel and sel["row"] not in rest:
                continue
            if "col" in sel and f'col: {sel["col"]}' not in rest:
                continue
            return ref
        return None

    def _decide(self, screen: str) -> PlannerDecision:
        if not self.script:
            return PlannerDecision("give_up", {"reason": "script exhausted"})
        item = dict(self.script.pop(0))
        tool = item.pop("tool")
        sel = item.pop("select", None)
        if sel is not None:
            ref = self._find(screen, sel)
            if ref is None:
                return PlannerDecision("request_human", {"reason": f"scripted step could not find {sel}"})
            item["ref"] = ref
        item.setdefault("why", "scripted")
        return PlannerDecision(tool, item, rationale=item.get("why", ""))

    async def first(self, task: str, screen: str) -> PlannerDecision:
        return self._decide(screen)

    async def next(self, result: str, screen: str, *, is_error: bool = False) -> PlannerDecision:
        return self._decide(screen)
