"""A scripted operator that drives the console over HTTP, exactly as a person's browser
would. Used for the demo/evidence runs and tests; the console itself is the real seam.

A plan is a list of commands executed after claiming the next pending intervention:
    {"click": "Remind Me Later"}       click the element whose name matches
    {"fill": "Nickname:", "value": "x"} fill the field labelled ...
    {"release": "resume", "note": "..."} release control (resume|approve|deny|abort)
"""

from __future__ import annotations

import asyncio
import re

import aiohttp


def find_ref(screen: str, text: str) -> int | None:
    for line in screen.splitlines():
        m = re.match(r"\[(\d+)\] \S+ (.*)", line)
        if m and (f'"{text}"' in m.group(2) or f'label="{text}"' in m.group(2)):
            return int(m.group(1))
    return None


async def run_operator(console_url: str, plan: list[dict], *, operator: str = "op-jlee",
                       wait_s: float = 60, log=print) -> dict:
    async with aiohttp.ClientSession() as http:
        deadline = asyncio.get_running_loop().time() + wait_s
        iv = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with http.get(f"{console_url}/api/status") as r:
                    st = await r.json()
                iv = next((i for i in st["interventions"] if i["status"] == "pending"), None)
            except aiohttp.ClientError:
                pass
            if iv:
                break
            await asyncio.sleep(0.3)
        if not iv:
            raise TimeoutError("no intervention appeared")
        log(f"[operator {operator}] picked up {iv['id']} ({iv['kind']}): {iv['reason']}")
        async with http.post(f"{console_url}/api/interventions/{iv['id']}/claim", json={"operator": operator}) as r:
            token = (await r.json())["token"]
        hdr = {"X-Lease": token}
        for cmd in plan:
            if "release" in cmd:
                async with http.post(f"{console_url}/api/interventions/{iv['id']}/release", headers=hdr,
                                     json={"resolution": cmd["release"], "note": cmd.get("note")}) as r:
                    out = await r.json()
                log(f"[operator {operator}] released control: {cmd['release']}")
                return out
            async with http.get(f"{console_url}/api/interventions/{iv['id']}/screen", headers=hdr) as r:
                screen = (await r.json())["screen"]
            action = "click" if "click" in cmd else "fill" if "fill" in cmd else "select"
            ref = find_ref(screen, cmd[action])
            if ref is None:
                raise RuntimeError(f"operator could not find {cmd[action]!r} on screen")
            body = {"action": action, "ref": ref, "value": cmd.get("value"), "param": cmd.get("param"),
                    "note": cmd.get("note")}
            async with http.post(f"{console_url}/api/interventions/{iv['id']}/act", headers=hdr, json=body) as r:
                res = await r.json()
                if r.status != 200:
                    raise RuntimeError(res)
            log(f"[operator {operator}] {action} {cmd[action]!r}")
        return {}
