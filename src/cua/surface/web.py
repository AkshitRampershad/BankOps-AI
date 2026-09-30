"""Web surface (Playwright/Chromium), built for legacy markup.

Perception is structural, not pixel-based: every frame is walked and flattened into an
ordered list of controls and text cells with the context a human uses to find things on
old screens — the label in the same table row, the column header above a cell. The
LLM sees that rendering; the recorder turns the chosen item into locator strategies and
verifies each one against the live page before it is written to the artifact.

Two custom selector engines carry the legacy-specific strategies:

* `cua-anchor={"text": "...", "role": "..."}` — controls in the unique table row whose
  cell text equals the anchor ("the box next to 'Member Number:'"). role=cell gives the
  cells after the anchor (label → value).
* `cua-cell={"row": "...", "col": "..."}` — the cell at (row key, column header).
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Frame, Locator, Page, Playwright, async_playwright

from ..artifact import (
    AnchoredLocator,
    ClickStep,
    Condition,
    CssLocator,
    ElementCondition,
    ExtractStep,
    FillStep,
    NavigateStep,
    PressStep,
    RoleLocator,
    SelectStep,
    Step,
    TableCellLocator,
    Target,
    TextCondition,
    TextLocator,
    UrlCondition,
    interpolate,
)
from ..redaction import PHONE, SSN
from .base import FrameInfo, Observation, Resolution, TargetError, UIItem

JS_COMMON = r"""
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const ROLE_SEL = {
  textbox: 'input:not([type]),input[type=text],input[type=password],input[type=email],input[type=number],input[type=tel],input[type=search],textarea',
  button: 'button,input[type=submit],input[type=button],input[type=reset],input[type=image]',
  combobox: 'select',
  checkbox: 'input[type=checkbox]',
  radio: 'input[type=radio]',
  link: 'a[href]',
};
function ownCells(tr) { return Array.from(tr.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH'); }
// A first row counts as a column header only if it looks like one (th, bold, or shaded).
function isHeaderRow(tr) {
  const cells = ownCells(tr).filter(c => norm(c.innerText));
  if (cells.length < 2) return false;
  if (tr.getAttribute('bgcolor') || tr.parentElement.tagName === 'THEAD') return true;
  return cells.every(c => c.tagName === 'TH' || (c.children.length === 1 && ['B','STRONG'].includes(c.children[0].tagName) && norm(c.children[0].innerText) === norm(c.innerText)));
}
"""

ANCHOR_ENGINE = (
    "(() => {\n"
    + JS_COMMON
    + r"""
  return { queryAll(root, body) {
    const {text, role} = JSON.parse(body);
    const rows = Array.from(root.querySelectorAll('tr')).filter(tr => ownCells(tr).some(c => norm(c.innerText) === text));
    if (rows.length !== 1) return [];
    const tr = rows[0];
    const cells = ownCells(tr);
    const idx = cells.findIndex(c => norm(c.innerText) === text);
    if (role === 'cell') return cells.slice(idx + 1);
    const sel = ROLE_SEL[role];
    if (!sel) return [];
    return cells.slice(idx + 1).flatMap(c => Array.from(c.querySelectorAll(sel)));
  },
  query(root, body) { return this.queryAll(root, body)[0] || null; } };
})()"""
)

CELL_ENGINE = (
    "(() => {\n"
    + JS_COMMON
    + r"""
  return { queryAll(root, body) {
    const {row, col} = JSON.parse(body);
    const out = [];
    for (const table of root.querySelectorAll('table')) {
      const rows = Array.from(table.rows);
      if (rows.length < 2) continue;
      const ci = Array.from(rows[0].cells).findIndex(c => norm(c.innerText) === col);
      if (ci < 0) continue;
      for (const r of rows.slice(1)) {
        const cells = Array.from(r.cells);
        if (cells.some(c => norm(c.innerText) === row) && cells[ci]) out.push(cells[ci]);
      }
    }
    return out;
  },
  query(root, body) { return this.queryAll(root, body)[0] || null; } };
})()"""
)

# Flatten a frame's DOM into ordered controls + text cells with their human context.
ENUMERATE_JS = (
    "() => {\n"
    + JS_COMMON
    + r"""
  const CONTROL = 'input,select,textarea,button,a[href]';
  const out = [];
  const seen = new Set();
  function visible(el) {
    if (!el.getClientRects().length) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  }
  function cssPath(el) {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      let i = 1, s = el;
      while ((s = s.previousElementSibling)) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
      el = el.parentElement;
    }
    return 'html > ' + parts.join(' > ');
  }
  function roleOf(el) {
    const t = el.tagName;
    const ty = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'A') return 'link';
    if (t === 'SELECT') return 'combobox';
    if (t === 'TEXTAREA') return 'textbox';
    if (t === 'BUTTON') return 'button';
    if (t === 'INPUT') {
      if (['submit','button','reset','image'].includes(ty)) return 'button';
      if (ty === 'checkbox') return 'checkbox';
      if (ty === 'radio') return 'radio';
      return 'textbox';
    }
    return el.getAttribute('role') || 'generic';
  }
  function nameOf(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return norm(aria);
    const t = el.tagName, ty = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'INPUT' && ['submit','button','reset'].includes(ty)) return norm(el.value || (ty === 'submit' ? 'Submit' : ''));
    if (t === 'INPUT' && ty === 'image') return norm(el.alt);
    if (t === 'A' || t === 'BUTTON') return norm(el.innerText);
    if (el.labels && el.labels.length) return norm(el.labels[0].innerText);
    return norm(el.getAttribute('title') || el.getAttribute('placeholder') || '');
  }
  function rowContext(el) {
    const td = el.closest('td,th');
    const tr = td && td.parentElement && td.parentElement.tagName === 'TR' ? td.parentElement : null;
    if (!tr) return {label: null, row: [], column: null, tr: null};
    const cells = ownCells(tr);
    const i = cells.indexOf(td);
    let label = null;
    for (let j = i - 1; j >= 0; j--) {
      const c = cells[j];
      if (!c.querySelector(CONTROL) && norm(c.innerText)) { label = norm(c.innerText); break; }
    }
    const row = cells.filter(c => c !== td).map(c => norm(c.innerText)).filter(Boolean);
    let column = null;
    const table = tr.closest('table');
    if (table && table.rows.length > 1 && table.rows[0] !== tr && isHeaderRow(table.rows[0])) {
      const hdr = table.rows[0].cells[td.cellIndex];
      if (hdr && !hdr.querySelector(CONTROL)) column = norm(hdr.innerText) || null;
    }
    return {label, row, column, tr};
  }
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (n.nodeType === 1) {
      const el = n;
      if (!el.matches(CONTROL) || el.type === 'hidden' || !visible(el)) continue;
      const ctx = rowContext(el);
      const role = roleOf(el);
      let value = null;
      if (el.tagName === 'SELECT') value = el.selectedIndex >= 0 ? norm(el.options[el.selectedIndex].text) : '';
      else if (role === 'textbox') value = (el.type === 'password') ? (el.value ? '••••' : '') : el.value;
      else if (role === 'checkbox' || role === 'radio') value = el.checked ? 'checked' : 'unchecked';
      out.push({
        kind: 'control', role, name: nameOf(el), value,
        label: (el.labels && el.labels.length) ? null : ctx.label,
        row: [], column: null,
        options: el.tagName === 'SELECT' ? Array.from(el.options).map(o => norm(o.text)) : null,
        disabled: !!el.disabled, key: cssPath(el),
      });
      continue;
    }
    const text = norm(n.data);
    if (!text) continue;
    const parent = n.parentElement;
    if (!parent || parent.closest('script,style,option,select,a,button,title,textarea')) continue;
    const block = parent.closest('td,th,li,p,h1,h2,h3,h4,h5,h6,pre,label,center,div');
    let el = parent, itemText = text;
    if (block && (block.tagName === 'TD' || block.tagName === 'TH') && !block.querySelector(CONTROL) && !block.querySelector('table')) {
      el = block; itemText = norm(block.innerText);
    }
    if (seen.has(el) || !visible(el)) continue;
    seen.add(el);
    const isCell = el.tagName === 'TD' || el.tagName === 'TH';
    const ctx = isCell ? rowContext(el) : {label: null, row: [], column: null};
    out.push({
      kind: 'text', role: isCell ? 'cell' : 'text', name: itemText, value: null,
      label: null, row: ctx.row, column: ctx.column, options: null, disabled: false, key: cssPath(el),
    });
  }
  return out;
}"""
)

# Redacted DOM snapshot: values next to PII labels and all input values are blanked in a clone.
SNAPSHOT_JS = (
    "(piiLabels) => {\n"
    + JS_COMMON
    + r"""
  const clone = document.documentElement.cloneNode(true);
  for (const tr of clone.querySelectorAll('tr')) {
    const cells = ownCells(tr);
    cells.forEach((c, i) => {
      if (piiLabels.includes(norm(c.textContent)) && cells[i + 1]) cells[i + 1].textContent = '«pii»';
    });
  }
  for (const inp of clone.querySelectorAll('input')) {
    if (!['submit','button','reset','image','hidden'].includes((inp.getAttribute('type')||'').toLowerCase())) inp.setAttribute('value', '«redacted»');
    if ((inp.getAttribute('type')||'').toLowerCase() === 'hidden') inp.setAttribute('value', '«redacted»');
  }
  for (const s of clone.querySelectorAll('script')) s.remove();
  return '<!doctype html>\n' + clone.outerHTML;
}"""
)

# Capture what a human does while they hold control of the session.
HUMAN_CAPTURE_JS = r"""
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  function describe(el) {
    const t = el.tagName, ty = (el.getAttribute && (el.getAttribute('type') || '')).toLowerCase();
    let name = el.getAttribute && el.getAttribute('aria-label');
    if (!name) name = (t === 'INPUT' && ['submit','button','reset'].includes(ty)) ? el.value : norm(el.innerText || '').slice(0, 80);
    const td = el.closest && el.closest('td');
    let label = null;
    if (td && td.previousElementSibling) label = norm(td.previousElementSibling.innerText).slice(0, 80);
    return {tag: t, type: ty, name, label, frame: window.name || null};
  }
  document.addEventListener('click', e => {
    const el = e.target.closest('a,button,input,select,td') || e.target;
    if (window.__cuaHuman) window.__cuaHuman(Object.assign({event: 'click'}, describe(el)));
  }, true);
  document.addEventListener('change', e => {
    const el = e.target;
    const d = describe(el);
    // Never capture typed values: record only that a value was entered and its length.
    d.value_length = (el.value || '').length;
    if (el.tagName === 'SELECT') d.selected = norm(el.options[el.selectedIndex] && el.options[el.selectedIndex].text);
    if (window.__cuaHuman) window.__cuaHuman(Object.assign({event: 'change'}, d));
  }, true);
})();
"""


def chromium_executable() -> str | None:
    exe = os.environ.get("CUA_CHROMIUM")
    if exe:
        return exe
    # Pre-installed browsers (e.g. CI images) when Playwright's bundled build is absent.
    for cand in ("/opt/pw-browsers/chromium",):
        if Path(cand).exists():
            return cand
    return None


LABEL_PREFIX = re.compile(r"^([A-Za-z][^:\d]{1,40}:)\s*\S")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class WebSurface:
    """One live browser session. Discovery, replay and the human operator all act on the
    same `Page` — that is what makes handoff a transfer of control rather than a restart."""

    def __init__(
        self,
        base_url: str,
        *,
        headed: bool = False,
        pii_labels: list[str] | None = None,
        url_guard: Callable[[str], tuple[bool, str]] | None = None,
        on_blocked: Callable[[str, str], None] | None = None,
        on_human_event: Callable[[dict], None] | None = None,
        on_dialog: Callable[[str, str], None] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headed = headed
        self.pii_labels = pii_labels or []
        self.url_guard = url_guard
        self.on_blocked = on_blocked
        self.on_human_event = on_human_event
        self.on_dialog = on_dialog
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._obs_keys: dict[int, tuple[str | None, str]] = {}
        self.dialogs: list[str] = []
        self.sensitive_values: set[str] = set()  # registered PII/secret values to mask in screenshots

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._pw = await async_playwright().start()
        try:
            await self._pw.selectors.register("cua-anchor", ANCHOR_ENGINE)
            await self._pw.selectors.register("cua-cell", CELL_ENGINE)
        except Exception as e:  # already registered in this process
            if "already registered" not in str(e):
                raise
        exe = chromium_executable()
        self.browser = await self._pw.chromium.launch(headless=not self.headed, executable_path=exe)
        self.context = await self.browser.new_context(viewport={"width": 1100, "height": 700})
        self.context.set_default_timeout(10_000)
        await self.context.route("**/*", self._guard)
        await self.context.expose_binding("__cuaHuman", self._human_binding)
        await self.context.add_init_script(HUMAN_CAPTURE_JS)
        self.page = await self.context.new_page()
        self.page.on("dialog", self._dialog)

    async def close(self) -> None:
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _guard(self, route, request) -> None:
        if self.url_guard is not None:
            ok, why = self.url_guard(request.url)
            if not ok:
                if self.on_blocked:
                    self.on_blocked(request.url, why)
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    async def _human_binding(self, source, payload: dict) -> None:
        if self.on_human_event:
            self.on_human_event(payload)

    async def _dialog(self, dialog) -> None:
        # Native dialogs are never auto-accepted: accepting a confirm() may commit a
        # transaction. Dismiss, record, and let checkpoints decide what happens next.
        msg = dialog.message
        self.dialogs.append(msg)
        if self.on_dialog:
            self.on_dialog(dialog.type, msg)
        await dialog.dismiss()

    # ------------------------------------------------------------------ frames
    def frame(self, name: str | None) -> Frame | None:
        assert self.page is not None
        if name is None:
            return self.page.main_frame
        for f in self.page.frames:
            if f.name == name:
                return f
        return None

    def url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    async def goto(self, path: str, frame: str | None = None) -> None:
        f = self.frame(frame)
        if f is None:
            raise RuntimeError(f"frame {frame!r} not present")
        await f.goto(self.url(path), wait_until="load")
        await self.settle()

    async def settle(self, timeout_ms: int = 5_000) -> None:
        """Wait for navigations to finish in every frame (legacy apps reload frames a lot)."""
        assert self.page is not None
        try:
            await self.page.wait_for_load_state("load", timeout=timeout_ms)
            for f in self.page.frames:
                await f.wait_for_load_state("load", timeout=timeout_ms)
        except Exception:
            pass

    # ------------------------------------------------------------------ perception
    async def observe(self) -> Observation:
        assert self.page is not None
        await self.settle()
        frames: list[FrameInfo] = []
        items: list[UIItem] = []
        self._obs_keys = {}
        ref = 1
        for f in self.page.frames:
            name = f.name or None
            try:
                title = await f.title()
                raw = await f.evaluate(ENUMERATE_JS)
            except Exception:
                continue  # frame navigating away; the next observation will catch it
            frames.append(FrameInfo(name=name, url=f.url, title=title))
            for r in raw:
                it = UIItem(ref=ref, frame=name, **{k: r[k] for k in (
                    "kind", "role", "name", "value", "label", "row", "column", "options", "disabled", "key")})
                it.sensitive = self._is_sensitive(it)
                items.append(it)
                self._obs_keys[ref] = (name, it.key)
                ref += 1
        dialogs, self.dialogs = self.dialogs, []
        return Observation(frames=frames, items=items, dialogs=dialogs, pii_labels=self.pii_labels)

    def _is_sensitive(self, it: UIItem) -> bool:
        ctx = [it.label or ""] + it.row
        return any(_norm(c) in self.pii_labels for c in ctx)

    async def frame_text(self, frame: str | None) -> str:
        frames = [self.frame(frame)] if frame is not None else (self.page.frames if self.page else [])
        parts = []
        for f in frames:
            if f is None:
                continue
            try:
                parts.append(await f.locator("body").inner_text(timeout=1_000))
            except Exception:
                pass
        return "\n".join(parts)

    # ------------------------------------------------------------------ locators
    def _locator(self, frame: Frame, strat: Any, params: dict[str, str]) -> Locator:
        if isinstance(strat, RoleLocator):
            return frame.get_by_role(strat.role, name=interpolate(strat.name, params), exact=strat.exact)
        if isinstance(strat, AnchoredLocator):
            body = json.dumps({"text": interpolate(strat.anchor_text, params), "role": strat.role})
            return frame.locator("cua-anchor=" + body).nth(strat.nth)
        if isinstance(strat, TableCellLocator):
            body = json.dumps({"row": interpolate(strat.row_key, params), "col": interpolate(strat.column, params)})
            return frame.locator("cua-cell=" + body)
        if isinstance(strat, TextLocator):
            return frame.get_by_text(interpolate(strat.text, params), exact=strat.exact)
        if isinstance(strat, CssLocator):
            return frame.locator("css=" + strat.selector)
        raise TypeError(f"unsupported locator {strat!r}")

    async def _count(self, frame: Frame, strat: Any, params: dict[str, str]) -> tuple[int, Locator]:
        loc = self._locator(frame, strat, params)
        if isinstance(strat, AnchoredLocator):
            # nth() always yields 0..1; judge ambiguity on the un-indexed set.
            base = frame.locator("cua-anchor=" + json.dumps(
                {"text": interpolate(strat.anchor_text, params), "role": strat.role}))
            n = await base.count()
            return (1 if n > strat.nth else 0), loc
        return await loc.count(), loc

    async def resolve(self, target: Target, params: dict[str, str], timeout_ms: int, *,
                      allow_structural: bool = True) -> Resolution:
        """Resolve a target to exactly one element, trying strategies in rank order.

        Structural (css) fallbacks are only consulted after a grace period, so a slow
        screen can't make a brittle locator win over the semantic one — and not at all
        when `allow_structural` is false (data extraction, irreversible actions), where
        acting on a positionally-similar element would be worse than stopping."""
        deadline = time.monotonic() + timeout_ms / 1000
        grace = time.monotonic() + min(1.5, timeout_ms / 2000)
        attempts: list[dict[str, Any]] = []
        while True:
            attempts = []
            frame = self.frame(target.frame)
            if frame is not None:
                for idx, strat in enumerate(target.strategies):
                    if isinstance(strat, CssLocator) and idx > 0 and (not allow_structural or time.monotonic() < grace):
                        if not allow_structural:
                            attempts.append({"strategy": "css", "skipped": "structural fallback not allowed for this step"})
                        continue
                    try:
                        n, loc = await self._count(frame, strat, params)
                    except Exception as e:  # frame detached mid-query
                        attempts.append({"strategy": strat.kind, "error": str(e)[:120]})
                        continue
                    attempts.append({"strategy": strat.kind, "matches": n})
                    if n == 1:
                        return Resolution(handle=loc, strategy_index=idx, strategy_kind=strat.kind)
            else:
                attempts.append({"frame": target.frame, "error": "frame not present"})
            if time.monotonic() >= deadline:
                ambiguous = any(a.get("matches", 0) > 1 for a in attempts)
                code = "TARGET_AMBIGUOUS" if ambiguous else "TARGET_NOT_FOUND"
                raise TargetError(code, target, f"no strategy matched exactly one element in {timeout_ms}ms", attempts)
            await asyncio.sleep(0.15)

    async def synthesize(self, item: UIItem, *, row_key: str | None = None) -> Target:
        """Build ranked locator candidates for an observed item and keep only those that
        uniquely identify *that same element* on the live page right now."""
        frame_name, key = self._obs_keys[item.ref]
        frame = self.frame(frame_name)
        assert frame is not None
        element = frame.locator("css=" + key)
        cands: list[tuple[Any, str]] = []
        if item.kind == "control":
            if item.name:
                cands.append((RoleLocator(role=item.role, name=item.name),
                              "accessible role+name; survives layout and styling changes"))
            if item.label:
                siblings = await frame.locator("cua-anchor=" + json.dumps({"text": item.label, "role": item.role})).all()
                for nth, s in enumerate(siblings):
                    if await s.evaluate("(a, b) => a === b", await element.element_handle()):
                        cands.append((AnchoredLocator(role=item.role, anchor_text=item.label, nth=nth),
                                      "unlabelled control anchored to its visible row label"))
                        break
            if item.role == "link" and item.name:
                cands.append((TextLocator(text=item.name), "visible link text"))
        else:
            if item.column:
                keys = [row_key] if row_key else [r for r in item.row if not re.fullmatch(r"[-$\d.,\s]+", r)]
                for k in keys:
                    cands.append((TableCellLocator(row_key=k, column=item.column),
                                  f"table cell by row key {k!r} and column header; independent of row order"))
            if item.row and not item.column:
                label = item.row[0]
                cands.append((AnchoredLocator(role="cell", anchor_text=label, nth=0),
                              "value cell to the right of its label"))
            if not re.search(r"\d", item.name) and len(item.name) < 80:
                cands.append((TextLocator(text=item.name), "static visible text"))
            elif (m := LABEL_PREFIX.match(item.name)):
                cands.append((TextLocator(text=m.group(1), exact=False),
                              "element whose text starts with the stable label before the value"))
        cands.append((CssLocator(selector=key), "structural DOM path; brittle, last resort"))

        handle = await element.element_handle()
        kept: list[tuple[Any, str]] = []
        for strat, why in cands:
            try:
                n, loc = await self._count(frame, strat, {})
                if n != 1:
                    continue
                same = await loc.evaluate("(a, b) => a === b", handle)
            except Exception:
                continue
            if same:
                kept.append((strat, why))
        if not kept:
            raise TargetError("TARGET_NOT_FOUND", Target(frame=frame_name, description=item.name or item.role,
                                                         strategies=[CssLocator(selector=key)]),
                              "no candidate locator uniquely identifies the element", [])
        desc = item.name or (f"{item.role} next to '{item.label}'" if item.label else item.role)
        if item.kind == "text" and item.column:
            desc = f"{item.column} cell"
        elif item.kind == "text" and re.search(r"\d", item.name):
            m = LABEL_PREFIX.match(item.name)  # never put screen data into a description
            desc = f"text after '{m.group(1)}'" if m else f"{item.role} (data)"
        return Target(
            frame=frame_name,
            description=desc,
            strategies=[s for s, _ in kept],
            rationale=f"primary: {kept[0][0].kind} ({kept[0][1]}); {len(kept) - 1} verified fallback(s)",
        )

    # ------------------------------------------------------------------ actions
    async def perform(self, step: Step, value: str | None, params: dict[str, str], *,
                      allow_structural: bool = True) -> Resolution | None:
        if isinstance(step, NavigateStep):
            await self.goto(interpolate(step.path, params), step.frame)
            return None
        target = getattr(step, "target", None)
        res = (await self.resolve(target, params, step.timeout_ms, allow_structural=allow_structural)
               if target is not None else None)
        loc: Locator | None = res.handle if res else None
        if isinstance(step, ClickStep):
            await loc.click(timeout=step.timeout_ms)
        elif isinstance(step, FillStep):
            await loc.fill(value or "", timeout=step.timeout_ms)
        elif isinstance(step, SelectStep):
            await loc.select_option(label=value, timeout=step.timeout_ms)
        elif isinstance(step, PressStep):
            if loc is not None:
                await loc.press(step.key, timeout=step.timeout_ms)
            else:
                await self.page.keyboard.press(step.key)
        elif isinstance(step, ExtractStep):
            return res
        await self.settle()
        return res

    async def read(self, target: Target, params: dict[str, str], timeout_ms: int) -> tuple[str, Resolution]:
        res = await self.resolve(target, params, timeout_ms, allow_structural=False)
        el = res.handle
        tag = await el.evaluate("e => e.tagName")
        if tag in ("INPUT", "TEXTAREA"):
            return await el.input_value(), res
        return _norm(await el.inner_text()), res

    async def check(self, cond: Condition, params: dict[str, str]) -> bool:
        if isinstance(cond, TextCondition):
            return interpolate(cond.text, params) in _norm(await self.frame_text(cond.frame))
        if isinstance(cond, UrlCondition):
            f = self.frame(cond.frame)
            if f is None:
                return False
            u = urlparse(f.url)
            path = u.path + (("?" + u.query) if u.query else "")
            return fnmatch.fnmatch(path, interpolate(cond.pattern, params))
        if isinstance(cond, ElementCondition):
            f = self.frame(cond.target.frame)
            if f is None:
                return False
            for s in cond.target.strategies:
                try:
                    n, _ = await self._count(f, s, params)
                except Exception:
                    continue
                if n == 1:
                    return True
            return False
        raise TypeError(cond)

    # ------------------------------------------------------------------ evidence
    async def screenshot(self, path: str, *, masked: bool = True) -> None:
        assert self.page is not None
        mask: list[Locator] = []
        if masked:
            for f in self.page.frames:
                mask.append(f.get_by_text(SSN))
                mask.append(f.get_by_text(PHONE))
                mask.append(f.locator("input[type=password]"))
                for label in self.pii_labels:
                    mask.append(f.locator("cua-anchor=" + json.dumps({"text": label, "role": "cell"})))
                for v in self.sensitive_values:
                    mask.append(f.get_by_text(v))
        await self.page.screenshot(path=path, mask=mask, mask_color="#222", full_page=False)

    async def screenshot_bytes(self) -> bytes:
        """Live, unmasked view for an authorised operator. Never persisted."""
        assert self.page is not None
        return await self.page.screenshot(type="png")

    async def dom_snapshot(self, directory: str) -> list[str]:
        assert self.page is not None
        out = []
        Path(directory).mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(self.page.frames):
            try:
                html = await f.evaluate(SNAPSHOT_JS, self.pii_labels)
            except Exception:
                continue
            p = Path(directory) / f"frame{i}-{f.name or 'top'}.html"
            p.write_text(html, encoding="utf-8")
            out.append(str(p))
        return out

    # ------------------------------------------------------------------ operator (human) input
    async def act_on_ref(self, ref: int, action: str, value: str | None = None) -> None:
        frame_name, key = self._obs_keys[ref]
        f = self.frame(frame_name)
        loc = f.locator("css=" + key)
        if action == "click":
            await loc.click()
        elif action == "fill":
            await loc.fill(value or "")
        elif action == "select":
            await loc.select_option(label=value)
        else:
            raise ValueError(action)
        await self.settle()

    async def click_xy(self, x: float, y: float) -> None:
        await self.page.mouse.click(x, y)
        await self.settle()

    async def type_text(self, text: str) -> None:
        await self.page.keyboard.type(text)
