"""Operator console: a deliberately minimal HTTP surface for taking over a live session.

It is a mock of a real co-browsing console, but the mechanism is real: the operator
claims the intervention (gets the lease), drives the *same* Playwright page the
automation was using (live screenshot + click-by-coordinates / click-by-element / typing),
and releases control with a resolution. Every command is lease-checked and recorded.

Endpoints (JSON unless noted):
  GET  /                                  HTML console
  GET  /api/status                        control state + open interventions
  POST /api/interventions/{id}/claim      {"operator": "..."} -> {"token": ...}
  GET  /api/interventions/{id}/screen     element list of the live screen   (X-Lease)
  GET  /api/live.png                      live screenshot (unmasked, never stored) (?token=)
  POST /api/interventions/{id}/act        {"action": click|fill|select|click_xy|type, ...} (X-Lease)
  POST /api/interventions/{id}/release    {"resolution": resume|approve|deny|abort, "note": ...} (X-Lease)
  GET  /evidence/{path}                   masked evidence screenshots
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import web

from .handoff import ControlError
from .runtime import Session

HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Operator Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font:14px system-ui,sans-serif;margin:16px;background:#f6f6f4;color:#222}
.card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px;margin:10px 0}
button{margin:2px;padding:4px 10px} img{max-width:100%;border:1px solid #ccc;cursor:crosshair}
pre{white-space:pre-wrap;font-size:12px;max-height:260px;overflow:auto;background:#fafafa;padding:6px}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;background:#eee}
</style></head><body>
<h2>Operator Console <span id=state class=pill></span></h2>
<div id=list></div>
<div id=live class=card style="display:none">
 <b>Live session</b> — you hold control. Click the image to click the page.
 <div><img id=shot></div>
 <div><input id=txt placeholder="text to type"> <button onclick="act({action:'type',text:txt.value})">Type</button></div>
 <div><button onclick="rel('resume')">Return control (resume)</button><button onclick="rel('approve')">Approve</button>
 <button onclick="rel('deny')">Deny</button><button onclick="rel('abort')">Abort run</button>
 <input id=note placeholder="note for the log" size=40></div>
 <pre id=screen></pre>
</div>
<script>
let token=null, cur=null;
async function j(u,o){const r=await fetch(u,o);const t=await r.json();if(!r.ok)alert(t.error||r.status);return t}
async function refresh(){
 const s=await j('/api/status'); document.getElementById('state').textContent=s.state+' · '+s.holder;
 const l=document.getElementById('list'); l.innerHTML='';
 for(const iv of s.interventions){const d=document.createElement('div');d.className='card';
  d.innerHTML=`<b>${iv.kind.toUpperCase()}</b> ${iv.id} — ${iv.reason}<br>subject: ${iv.subject}<br>step ${iv.step_id}: ${iv.step_intent||''}<br>status: ${iv.status}`+
  (iv.screenshot?`<br><img src="/evidence/${iv.screenshot}" style="max-width:420px">`:'')+`<pre>${iv.screen_text.replace(/</g,'&lt;')}</pre>`+
  (iv.status==='pending'?`<button onclick="claim('${iv.id}')">Take control</button>`:'');l.appendChild(d)}
 if(token){document.getElementById('shot').src='/api/live.png?token='+token+'&t='+Date.now();
  const sc=await j(`/api/interventions/${cur}/screen`,{headers:{'X-Lease':token}});document.getElementById('screen').textContent=sc.screen}
}
async function claim(id){const r=await j(`/api/interventions/${id}/claim`,{method:'POST',body:JSON.stringify({operator:prompt('operator id','op-1')||'op-1'})});
 if(r.token){token=r.token;cur=id;document.getElementById('live').style.display='block';refresh()}}
async function act(body){await j(`/api/interventions/${cur}/act`,{method:'POST',headers:{'X-Lease':token},body:JSON.stringify(body)});refresh()}
async function rel(res){await j(`/api/interventions/${cur}/release`,{method:'POST',headers:{'X-Lease':token},body:JSON.stringify({resolution:res,note:note.value})});
 token=null;cur=null;document.getElementById('live').style.display='none';refresh()}
document.getElementById('shot').onclick=e=>{const r=e.target.getBoundingClientRect();const k=e.target.naturalWidth/r.width;
 act({action:'click_xy',x:(e.clientX-r.left)*k,y:(e.clientY-r.top)*k})};
setInterval(refresh,2000);refresh();
</script></body></html>"""


class OperatorConsole:
    def __init__(self, session: Session, port: int) -> None:
        self.s = session
        self.port = port
        self.runner: web.AppRunner | None = None

    async def start(self) -> str:
        app = web.Application()
        app.add_routes([
            web.get("/", self.index),
            web.get("/api/status", self.status),
            web.post("/api/interventions/{iid}/claim", self.claim),
            web.get("/api/interventions/{iid}/screen", self.screen),
            web.get("/api/live.png", self.live),
            web.post("/api/interventions/{iid}/act", self.act),
            web.post("/api/interventions/{iid}/release", self.release),
            web.get("/evidence/{path:.+}", self.evidence),
        ])
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        url = f"http://127.0.0.1:{self.port}"
        self.s.controller.console_url = url
        self.s.evidence.event("operator_console_started", url=url)
        return url

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    @staticmethod
    def _err(e: Exception, status: int = 409) -> web.Response:
        return web.json_response({"error": str(e)}, status=status)

    async def index(self, _req) -> web.Response:
        return web.Response(text=HTML, content_type="text/html")

    async def status(self, _req) -> web.Response:
        c = self.s.controller
        return web.json_response({**c.status(), "interventions": [iv.public() for iv in c.interventions.values()]})

    async def claim(self, req) -> web.Response:
        body = await req.json() if req.can_read_body else {}
        try:
            token = self.s.controller.claim(req.match_info["iid"], body.get("operator", "operator"))
        except ControlError as e:
            return self._err(e)
        return web.json_response({"token": token})

    async def screen(self, req) -> web.Response:
        try:
            self.s.controller.check_token(req.headers.get("X-Lease"))
        except ControlError as e:
            return self._err(e, 403)
        obs = await self.s.surface.observe()
        # The operator is an authorised user of the app, but this rendering may be logged
        # by the console client, so it is redacted like everything else.
        return web.json_response({"screen": obs.render(self.s.redactor.text)})

    async def live(self, req) -> web.Response:
        try:
            self.s.controller.check_token(req.query.get("token"))
        except ControlError as e:
            return self._err(e, 403)
        return web.Response(body=await self.s.surface.screenshot_bytes(), content_type="image/png")

    async def act(self, req) -> web.Response:
        c, surf = self.s.controller, self.s.surface
        try:
            c.check_token(req.headers.get("X-Lease"))
        except ControlError as e:
            return self._err(e, 403)
        a = await req.json()
        action = a.get("action")
        record: dict = {"source": "operator", "action": action, "note": a.get("note")}
        try:
            if action in ("click", "fill", "select"):
                ref = int(a["ref"])
                item = (await surf.observe()).item(ref)  # refs are stable while the screen is unchanged
                if item is None:
                    return self._err(ValueError(f"no element [{ref}]"), 400)
                try:
                    record["target"] = (await surf.synthesize(item)).model_dump(mode="json")
                except Exception as e:  # still let the human act; just note it can't be replayed
                    record["target_error"] = str(e)[:120]
                record["element"] = f"{item.role} {item.name or item.label or ''}".strip()
                if action != "click":
                    record["param"] = a.get("param")
                    record["value_length"] = len(a.get("value", ""))
                    if not a.get("param") and not self.s.redactor.contains_sensitive(a.get("value", "")):
                        record["literal"] = a.get("value", "")
                await surf.act_on_ref(ref, action, a.get("value"))
            elif action == "click_xy":
                record.update(x=round(a["x"]), y=round(a["y"]))
                await surf.click_xy(float(a["x"]), float(a["y"]))
            elif action == "type":
                record["value_length"] = len(a.get("text", ""))
                await surf.type_text(a.get("text", ""))
            else:
                return self._err(ValueError(f"unknown action {action}"), 400)
        except Exception as e:
            return self._err(e, 400)
        c.record_human_action(record)
        return web.json_response({"ok": True})

    async def release(self, req) -> web.Response:
        body = await req.json()
        try:
            iv = self.s.controller.release(req.match_info["iid"], req.headers.get("X-Lease"),
                                           body.get("resolution", "resume"), body.get("note"))
        except (ControlError, ValueError) as e:
            return self._err(e)
        return web.json_response({"ok": True, "intervention": iv.public()})

    async def evidence(self, req) -> web.Response:
        root = self.s.evidence.dir.resolve()
        p = (root / req.match_info["path"]).resolve()
        if not p.is_relative_to(root) or not p.exists() or p.suffix != ".png":
            return web.Response(status=404)
        return web.FileResponse(p)
