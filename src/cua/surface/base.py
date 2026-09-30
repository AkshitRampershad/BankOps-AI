"""The surface seam: how we perceive and act on an application, independent of the flow.

Everything above this layer (discovery loop, recorder, replay engine, policy, handoff)
speaks in terms of `Observation`, `UIItem`, `Target` and `Step` — never Playwright, UIA,
or pixels. A new surface (desktop via UI Automation, a terminal emulator, a VDI stream
via screenshot + OCR) implements `Surface`; the artifact schema and replay engine do not
change. Locator strategies that a surface can't honour are skipped (e.g. `css` on desktop).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..artifact import Condition, Step, Target


@dataclass
class FrameInfo:
    name: str | None
    url: str
    title: str


@dataclass
class UIItem:
    ref: int
    frame: str | None
    kind: str  # "control" | "text"
    role: str
    name: str
    value: str | None = None
    label: str | None = None  # visible label anchoring this control (same row / wrapping label)
    row: list[str] = field(default_factory=list)  # other cell texts in the same table row
    column: str | None = None  # column header, for table cells
    options: list[str] | None = None
    disabled: bool = False
    sensitive: bool = False
    key: str = ""  # surface-private handle (css path on web)


@dataclass
class Observation:
    frames: list[FrameInfo]
    items: list[UIItem]
    dialogs: list[str] = field(default_factory=list)
    pii_labels: list[str] = field(default_factory=list)

    def item(self, ref: int) -> UIItem | None:
        return next((i for i in self.items if i.ref == ref), None)

    def texts(self, frame: str | None = None) -> list[str]:
        return [i.name for i in self.items if (frame is None or i.frame == frame) and i.name]

    def render(self, redact) -> str:
        """Compact, redacted text rendering for the model and for evidence."""
        out: list[str] = []
        for f in self.frames:
            fitems = [i for i in self.items if i.frame == f.name]
            if not fitems and f.name is None:
                continue
            path = f.url.split("//", 1)[-1].split("/", 1)[-1] if "//" in f.url else f.url
            out.append(f'== frame "{f.name or "top"}" url=/{path} title="{f.title}"')
            for i in fitems:
                name = "«pii»" if i.sensitive else redact(i.name)
                # A PII label's row context is the PII value itself.
                row = ["«pii»"] if i.name in self.pii_labels and i.row else i.row
                s = f"[{i.ref}] {i.role}"
                if name:
                    s += f' "{name}"'
                if i.label:
                    s += f' label="{redact(i.label)}"'
                if i.value is not None and i.kind == "control":
                    s += f' value="{"«pii»" if i.sensitive else redact(i.value)}"'
                if i.options:
                    s += " options=" + "|".join(redact(o) for o in i.options)
                if i.column or row:
                    ctx = []
                    if row:
                        ctx.append("row: " + " | ".join(redact(r) for r in row))
                    if i.column:
                        ctx.append(f"col: {redact(i.column)}")
                    s += " (" + "; ".join(ctx) + ")"
                if i.disabled:
                    s += " [disabled]"
                out.append(s)
        for d in self.dialogs:
            out.append(f"!! native dialog: {redact(d)}")
        return "\n".join(out)


class TargetError(Exception):
    def __init__(self, code: str, target: Target, detail: str, attempts: list[dict[str, Any]]):
        super().__init__(f"{code}: {target.description}: {detail}")
        self.code = code  # TARGET_NOT_FOUND | TARGET_AMBIGUOUS
        self.target = target
        self.detail = detail
        self.attempts = attempts


@dataclass
class Resolution:
    handle: Any
    strategy_index: int
    strategy_kind: str


class Surface(Protocol):
    async def observe(self) -> Observation: ...

    async def synthesize(self, item: UIItem, *, row_key: str | None = None) -> Target: ...

    async def resolve(self, target: Target, params: dict[str, str], timeout_ms: int) -> Resolution: ...

    async def perform(self, step: Step, value: str | None, params: dict[str, str]) -> Resolution | None: ...

    async def read(self, target: Target, params: dict[str, str], timeout_ms: int) -> tuple[str, Resolution]: ...

    async def check(self, cond: Condition, params: dict[str, str]) -> bool: ...

    async def frame_text(self, frame: str | None) -> str: ...

    async def screenshot(self, path: str, *, masked: bool = True) -> None: ...

    async def dom_snapshot(self, directory: str) -> list[str]: ...
