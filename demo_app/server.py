"""Heritage Core — a deliberately "legacy" member-services app used as the proxy target.

It imitates the shape of a 2000s-era core-banking back office screen on purpose:

* a <frameset> (banner / nav / main) — automation must work across frames;
* table-based layout, <font> tags, no <label for>, no ids, no test ids;
* POST-back screens whose URL does not change between inquiry and profile,
  so checkpoints cannot rely on URLs alone;
* business outcomes rendered as red text ("NO RECORD FOUND", "ACCESS DENIED");
* injectable runtime faults (system notice interstitial, session expiry,
  slow responses, application errors) so replay error handling can be
  exercised deterministically.

Two tenants run the same "vendor product" with different branding and labels
(`--tenant riverbend` is the base, `--tenant lakeside` is a variant) — a stand-in
for many institutions running one configured vendor app.

All data is fictional. Stdlib only, so the target has no dependency on the
automation stack.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEMO_USER = "operator1"
# Fake credential for a fake app. Read from env so it is never hard-coded into artifacts.
DEMO_PASSWORD = os.environ.get("HC_PASSWORD", "demo-pass-123")

MEMBERS = {
    "100234": {
        "name": "JANE Q SAMPLE",
        "ssn": "123-45-6789",
        "dob": "1984-03-17",
        "phone": "(555) 201-4432",
        "address": "12 ELM ST, SPRINGFIELD",
        "accounts": [
            ("S0001", "SHARE SAVINGS", "1,234.56", "1,209.56"),
            ("S0009", "HOLIDAY CLUB", "310.00", "310.00"),
            ("D0010", "SHARE DRAFT CHECKING", "2,087.13", "2,087.13"),
        ],
    },
    "100377": {
        "name": "ROBERT EXAMPLE",
        "ssn": "987-65-4321",
        "dob": "1969-11-02",
        "phone": "(555) 390-1188",
        "address": "400 OAK AVE APT 3, SHELBYVILLE",
        "accounts": [
            ("S0001", "SHARE SAVINGS", "58,002.10", "58,002.10"),
            ("L0140", "AUTO LOAN", "-9,410.77", "0.00"),
        ],
    },
    "100555": {"restricted": True},
}

TENANTS = {
    "riverbend": {
        "institution": "RIVERBEND COMMUNITY CREDIT UNION",
        "version": "4.2.7",
        "member_label": "Member Number:",
        "inquire": "Inquire",
        "balance_col": "Balance",
        "profile_title": "MEMBER PROFILE",
    },
    # Same vendor product, different configuration — labels and branding differ.
    "lakeside": {
        "institution": "LAKESIDE FEDERAL CREDIT UNION",
        "version": "4.3.1",
        "member_label": "Account No.:",
        "inquire": "Search",
        "balance_col": "Current Bal",
        "profile_title": "MEMBER PROFILE",
    },
}


@dataclass
class Faults:
    """Faults are consumed by the next matching page request under /hc/."""

    notice_once: bool = False
    unknown_dialog_once: bool = False  # an interstitial no profile knows about
    session_expire_once: bool = False
    error_once: bool = False
    error_always: bool = False
    slow_seconds: float = 0.0
    slow_count: int = 0
    path: str | None = None  # only fire on requests whose path contains this (None = any)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, spec: dict) -> None:
        with self.lock:
            self.notice_once = bool(spec.get("notice_once", False))
            self.unknown_dialog_once = bool(spec.get("unknown_dialog_once", False))
            self.session_expire_once = bool(spec.get("session_expire_once", False))
            self.error_once = bool(spec.get("error_once", False))
            self.error_always = bool(spec.get("error_always", False))
            self.slow_seconds = float(spec.get("slow_seconds", 0.0))
            self.slow_count = int(spec.get("slow_count", 1 if self.slow_seconds else 0))
            self.path = spec.get("path")

    def snapshot(self) -> dict:
        return {
            "notice_once": self.notice_once,
            "unknown_dialog_once": self.unknown_dialog_once,
            "session_expire_once": self.session_expire_once,
            "error_once": self.error_once,
            "error_always": self.error_always,
            "slow_seconds": self.slow_seconds,
            "slow_count": self.slow_count,
            "path": self.path,
        }


class AppState:
    def __init__(self, tenant: str) -> None:
        self.tenant = TENANTS[tenant]
        self.tenant_key = tenant
        self.sessions: set[str] = set()
        self.faults = Faults()
        self.created: list[dict] = []
        # Where to return after the notice interstitial, per session.
        self.pending_return: dict[str, str] = {}


def page(title: str, body: str, *, bg: str = "#d4d0c8") -> bytes:
    return (
        "<html><head><title>" + html.escape(title) + "</title>"
        '<meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>'
        f'<body bgcolor="{bg}" style="font-family: Tahoma, Arial; font-size: 12px">'
        + body
        + "</body></html>"
    ).encode()


def screen_header(state: AppState, code: str, title: str) -> str:
    t = state.tenant
    return (
        '<table width="100%" cellpadding="2" cellspacing="0" bgcolor="#000080"><tr>'
        f'<td><font color="white"><b>{code}</b></font></td>'
        f'<td align="center"><font color="white" size="4"><b>{title}</b></font></td>'
        f'<td align="right"><font color="white">HC {t["version"]}</font></td></tr></table><br>'
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "HeritageCore/4.2"
    state: AppState  # injected

    # --- plumbing -----------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # quiet
        if os.environ.get("HC_VERBOSE"):
            super().log_message(fmt, *args)

    def _session(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = cookies.SimpleCookie(raw)
        sid = c.get("HCSESSION")
        if sid and sid.value in self.state.sessions:
            return sid.value
        return None

    def _send(self, status: int, body: bytes, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, headers: dict | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n).decode() if n else ""
        return {k: v[0] for k, v in parse_qs(data, keep_blank_values=True).items()}

    # --- routing ------------------------------------------------------------------
    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        path = url.path
        query = {k: v[0] for k, v in parse_qs(url.query).items()}

        if path == "/__admin/faults":
            return self._admin_faults(method)
        if path == "/__admin/created":
            return self._send(200, json.dumps(self.state.created).encode(), {"Content-Type": "application/json"})
        if path == "/login":
            return self._login(method)

        sid = self._session()
        if path == "/":
            if not sid:
                return self._redirect("/login")
            return self._frameset()
        if not sid:
            if path.startswith("/hc/"):
                return self._send(200, self._expired_page())
            return self._redirect("/login")
        if path == "/banner":
            return self._banner()
        if path == "/nav":
            return self._nav()
        if path == "/signoff":
            self.state.sessions.discard(sid)
            return self._redirect("/login")
        if path.startswith("/hc/"):
            return self._hc(method, path, query, sid)
        self._send(404, page("Not found", "<b>404</b>"))

    # --- admin (fault injection; operator-only, never on the automation allowlist) --
    def _admin_faults(self, method: str) -> None:
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            spec = json.loads(self.rfile.read(n) or b"{}")
            self.state.faults.set(spec)
        body = json.dumps(self.state.faults.snapshot()).encode()
        self._send(200, body, {"Content-Type": "application/json"})

    # --- login / frames -----------------------------------------------------------
    def _login_form(self, action: str, message: str = "") -> str:
        msg = f'<font color="red"><b>{html.escape(message)}</b></font><br><br>' if message else ""
        return (
            f'<form method="POST" action="{action}">'
            '<table border="0" cellpadding="3">'
            "<tr><td>User ID:</td><td><input type=text name=u size=12></td></tr>"
            "<tr><td>Password:</td><td><input type=password name=p size=12></td></tr>"
            '<tr><td></td><td><input type=submit value="Sign On"></td></tr>'
            f"</table></form>{msg}"
        )

    def _login(self, method: str) -> None:
        t = self.state.tenant
        if method == "POST":
            f = self._form()
            if f.get("u") == DEMO_USER and f.get("p") == DEMO_PASSWORD:
                sid = secrets.token_hex(16)
                self.state.sessions.add(sid)
                return self._redirect("/", {"Set-Cookie": f"HCSESSION={sid}; Path=/; HttpOnly"})
            body = self._login_page(t, "INVALID USER ID OR PASSWORD")
            return self._send(200, body)
        self._send(200, self._login_page(t))

    def _login_page(self, t: dict, message: str = "") -> bytes:
        return page(
            "Heritage Core Sign On",
            f'<center><br><br><font size="5"><b>HERITAGE CORE</b></font><br>{t["institution"]}<br><br>'
            + self._login_form("/login", message)
            + "</center>",
        )

    def _expired_page(self, message: str = "") -> bytes:
        return page(
            "Session Expired",
            "<center><br><font color=red size=4><b>SESSION EXPIRED - PLEASE SIGN ON AGAIN</b></font><br><br>"
            + self._login_form("/hc/RELOGIN", message)
            + "</center>",
        )

    def _frameset(self) -> None:
        body = (
            "<html><head><title>Heritage Core</title></head>"
            '<frameset rows="48,*" border="1">'
            '<frame name="banner" src="/banner" scrolling="no">'
            '<frameset cols="170,*">'
            '<frame name="nav" src="/nav">'
            '<frame name="main" src="/hc/HOME">'
            "</frameset></frameset></html>"
        ).encode()
        self._send(200, body)

    def _banner(self) -> None:
        t = self.state.tenant
        self._send(
            200,
            page(
                "banner",
                f'<table width="100%"><tr><td><font size=4 color="#800000"><b>HERITAGE CORE</b></font> '
                f'&nbsp; {t["institution"]}</td><td align=right>User: {DEMO_USER}</td></tr></table>',
                bg="#c0c0c0",
            ),
        )

    def _nav(self) -> None:
        links = [
            ("Member Inquiry", "/hc/MBR0100"),
            ("Transaction History", "/hc/TRN0100"),
            ("Reports", "/hc/RPT0100"),
        ]
        rows = "".join(
            f'<tr><td>&#9656; <a href="{href}" target="main">{text}</a></td></tr>' for text, href in links
        )
        rows += '<tr><td><br><a href="/signoff" target="_top">Sign Off</a></td></tr>'
        self._send(200, page("nav", f'<table cellpadding="3">{rows}</table>', bg="#e8e8e8"))

    # --- the application screens ---------------------------------------------------
    def _hc(self, method: str, path: str, query: dict, sid: str) -> None:
        f = self.state.faults
        with f.lock:
            delay = 0.0
            targeted = not (f.path and f.path not in path)
            if targeted and f.slow_count > 0 and f.slow_seconds > 0:
                delay = f.slow_seconds
                f.slow_count -= 1
            fault = None
            if targeted and path not in ("/hc/ACK", "/hc/RELOGIN"):
                if f.error_always or f.error_once:
                    fault = "error"
                    f.error_once = False
                elif f.session_expire_once:
                    fault = "expire"
                    f.session_expire_once = False
                elif f.notice_once and method == "GET":
                    fault = "notice"
                    f.notice_once = False
                elif f.unknown_dialog_once and method == "POST":
                    fault = "training"
                    f.unknown_dialog_once = False
        if delay:
            time.sleep(delay)
        if fault == "error":
            return self._send(
                500,
                page(
                    "Application Error",
                    screen_header(self.state, "SYS9999", "APPLICATION ERROR")
                    + "<font color=red><b>HC-E999 UNEXPECTED APPLICATION ERROR. CONTACT SUPPORT.</b></font>"
                    "<br><pre>at HC.MBR.Dispatch(line 4411)</pre>",
                ),
            )
        if fault == "expire":
            self.state.sessions.discard(sid)
            return self._send(200, self._expired_page())
        if fault == "training":
            self.state.pending_return[sid] = "/hc/MBR0100"
            return self._send(
                200,
                page(
                    "Reminder",
                    screen_header(self.state, "TRN9000", "COMPLIANCE REMINDER")
                    + "<table border=1 cellpadding=8 bgcolor=#ffe0e0><tr><td>"
                    "<b>ANNUAL BSA/AML TRAINING IS DUE IN 3 DAYS.</b><br><br>"
                    '<form method=GET action="/hc/ACK" style="display:inline"><input type=submit value="Remind Me Later"></form> '
                    '<form method=GET action="/hc/ACK" style="display:inline"><input type=submit value="Start Training"></form>'
                    "</td></tr></table>",
                ),
            )
        if fault == "notice":
            self.state.pending_return[sid] = path + ("?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else "")
            return self._send(
                200,
                page(
                    "System Notice",
                    screen_header(self.state, "SYS0001", "SYSTEM NOTICE")
                    + "<table border=1 cellpadding=8 bgcolor=#ffffe0><tr><td>"
                    "<b>END-OF-DAY PROCESSING IS SCHEDULED FOR 22:00.</b><br>"
                    "Balances shown after 21:45 may not reflect pending items.<br><br>"
                    '<form method=GET action="/hc/ACK"><input type=submit value="Acknowledge"></form>'
                    "</td></tr></table>",
                ),
            )

        if path == "/hc/HOME":
            return self._send(
                200,
                page(
                    "Home",
                    screen_header(self.state, "HOM0001", "MAIN MENU")
                    + "Select a function from the menu on the left.",
                ),
            )
        if path == "/hc/ACK":
            back = self.state.pending_return.pop(sid, "/hc/HOME")
            return self._redirect(back)
        if path == "/hc/MBR0100":
            if method == "POST":
                return self._member_lookup(self._form().get("f_mbrno", "").strip())
            return self._send(200, self._inquiry_page())
        if path == "/hc/SUB0100":
            return self._sub_account(method, query)
        if path == "/hc/SUB0200":
            return self._sub_submit(method)
        if path in ("/hc/TRN0100", "/hc/RPT0100"):
            return self._send(
                200, page("n/a", screen_header(self.state, path[-7:], "NOT LICENSED") + "Function not licensed.")
            )
        self._send(404, page("Not found", screen_header(self.state, "SYS0404", "NOT FOUND")))

    def _inquiry_page(self, message: str = "", value: str = "") -> bytes:
        t = self.state.tenant
        msg = f'<br><font color="red"><b>{html.escape(message)}</b></font>' if message else ""
        return page(
            "Member Inquiry",
            screen_header(self.state, "MBR0100", "MEMBER INQUIRY")
            + '<form method="POST" action="/hc/MBR0100"><table border="0" cellpadding="3">'
            f'<tr><td align="right">{t["member_label"]}</td>'
            f'<td><input type="text" name="f_mbrno" size="10" maxlength="10" value="{html.escape(value)}"></td>'
            f'<td><input type="submit" value="{t["inquire"]}"></td></tr>'
            "</table></form>" + msg,
        )

    def _member_lookup(self, mbr: str) -> None:
        if not (mbr.isdigit() and len(mbr) == 6):
            return self._send(200, self._inquiry_page("MBR-101 INVALID MEMBER NUMBER FORMAT (6 DIGITS REQUIRED)", mbr))
        m = MEMBERS.get(mbr)
        if m is None:
            return self._send(200, self._inquiry_page(f"*** NO RECORD FOUND FOR MEMBER {mbr} ***", mbr))
        if m.get("restricted"):
            return self._send(
                200, self._inquiry_page("SEC-403 ACCESS DENIED - RESTRICTED ACCOUNT. SUPERVISOR OVERRIDE REQUIRED.", mbr)
            )
        return self._send(200, self._profile_page(mbr, m))

    def _profile_page(self, mbr: str, m: dict) -> bytes:
        t = self.state.tenant
        info = [
            ("Name", m["name"]),
            ("Member No.", mbr),
            ("SSN", m["ssn"]),
            ("Date of Birth", m["dob"]),
            ("Phone", m["phone"]),
            ("Address", m["address"]),
        ]
        info_rows = "".join(f"<tr><td><b>{k}:</b></td><td>{html.escape(v)}</td></tr>" for k, v in info)
        acct_rows = "".join(
            f"<tr><td>{s}</td><td>{d}</td><td align=right>{b}</td><td align=right>{a}</td></tr>"
            for s, d, b, a in m["accounts"]
        )
        return page(
            "Member Profile",
            screen_header(self.state, "MBR0200", t["profile_title"])
            + f'<table border="0" cellpadding="2">{info_rows}</table><br>'
            + '<table border="1" cellpadding="3" cellspacing="0" width="80%">'
            + f'<tr bgcolor="#a0a0c0"><td><b>Suffix</b></td><td><b>Description</b></td>'
            + f'<td><b>{t["balance_col"]}</b></td><td><b>Available</b></td></tr>'
            + acct_rows
            + "</table><br>"
            + f'<form method="GET" action="/hc/SUB0100" style="display:inline"><input type="hidden" name="m" value="{mbr}">'
            + '<input type="submit" value="Open Sub-Account"></form> '
            + '<form method="GET" action="/hc/MBR0100" style="display:inline"><input type="submit" value="New Inquiry"></form>',
        )

    def _sub_account(self, method: str, query: dict) -> None:
        mbr = query.get("m", "")
        if mbr not in MEMBERS or MEMBERS[mbr].get("restricted"):
            return self._send(200, self._inquiry_page(f"*** NO RECORD FOUND FOR MEMBER {mbr} ***"))
        if method == "POST":
            f = self._form()
            err = None
            amt = f.get("amt", "").replace(",", "").replace("$", "")
            try:
                amount = float(amt)
                if amount < 5 or amount > 10000:
                    err = "SUB-220 INITIAL DEPOSIT MUST BE BETWEEN 5.00 AND 10,000.00"
            except ValueError:
                err = "SUB-221 INITIAL DEPOSIT AMOUNT INVALID"
            if f.get("typ") not in ("HC", "VC", "XS"):
                err = "SUB-210 SELECT A SHARE TYPE"
            if err:
                return self._send(200, self._sub_form(mbr, err))
            desc = {"HC": "HOLIDAY CLUB", "VC": "VACATION CLUB", "XS": "EXTRA SAVINGS"}[f["typ"]]
            return self._send(
                200,
                page(
                    "Review",
                    screen_header(self.state, "SUB0150", "REVIEW NEW SUB-ACCOUNT")
                    + "<table border=1 cellpadding=4 cellspacing=0>"
                    f"<tr><td>Member No.</td><td>{mbr}</td></tr>"
                    f"<tr><td>Share Type</td><td>{desc}</td></tr>"
                    f"<tr><td>Nickname</td><td>{html.escape(f.get('nick', ''))}</td></tr>"
                    f"<tr><td>Initial Deposit</td><td>{amount:,.2f}</td></tr></table><br>"
                    "<i>Submitting will open the account and post the deposit. This cannot be undone.</i><br><br>"
                    '<form method="POST" action="/hc/SUB0200">'
                    f'<input type="hidden" name="m" value="{mbr}"><input type="hidden" name="typ" value="{f["typ"]}">'
                    f'<input type="hidden" name="amt" value="{amount:.2f}">'
                    '<input type="submit" value="Submit"></form> '
                    f'<form method="GET" action="/hc/MBR0100" style="display:inline"><input type="submit" value="Cancel"></form>',
                ),
            )
        return self._send(200, self._sub_form(mbr))

    def _sub_form(self, mbr: str, err: str = "") -> bytes:
        msg = f'<br><font color="red"><b>{html.escape(err)}</b></font>' if err else ""
        return page(
            "New Sub-Account",
            screen_header(self.state, "SUB0100", "OPEN SUB-ACCOUNT")
            + f'<form method="POST" action="/hc/SUB0100?m={mbr}"><table cellpadding=3>'
            f"<tr><td>Member No.:</td><td>{mbr}</td></tr>"
            '<tr><td>Share Type:</td><td><select name="typ"><option value="">-- select --</option>'
            '<option value="HC">HOLIDAY CLUB</option><option value="VC">VACATION CLUB</option>'
            '<option value="XS">EXTRA SAVINGS</option></select></td></tr>'
            '<tr><td>Nickname:</td><td><input type="text" name="nick" size="20"></td></tr>'
            '<tr><td>Initial Deposit:</td><td><input type="text" name="amt" size="10"></td></tr>'
            '<tr><td></td><td><input type="submit" value="Continue"></td></tr></table></form>' + msg,
        )

    def _sub_submit(self, method: str) -> None:
        if method != "POST":
            return self._redirect("/hc/MBR0100")
        f = self._form()
        conf = f"SA{len(self.state.created) + 70001}"
        self.state.created.append({"member": f.get("m"), "type": f.get("typ"), "amount": f.get("amt"), "conf": conf})
        self._send(
            200,
            page(
                "Created",
                screen_header(self.state, "SUB0300", "SUB-ACCOUNT CREATED")
                + f"<b>Confirmation #: {conf}</b>",
            ),
        )


def make_server(host: str, port: int, tenant: str) -> ThreadingHTTPServer:
    state = AppState(tenant)
    handler = type("BoundHandler", (Handler,), {"state": state})

    # Re-login from inside the frame after a session expiry returns to the inquiry screen,
    # like many real cores do (the original navigation context is lost).
    orig_route = handler._route

    def _route(self, method):  # type: ignore[no-redef]
        if urlparse(self.path).path == "/hc/RELOGIN" and method == "POST":
            f = self._form()
            if f.get("u") == DEMO_USER and f.get("p") == DEMO_PASSWORD:
                sid = secrets.token_hex(16)
                self.state.sessions.add(sid)
                return self._redirect("/hc/MBR0100", {"Set-Cookie": f"HCSESSION={sid}; Path=/; HttpOnly"})
            return self._send(200, self._expired_page("INVALID USER ID OR PASSWORD"))
        return orig_route(self, method)

    handler._route = _route
    server = ThreadingHTTPServer((host, port), handler)
    server.app_state = state  # type: ignore[attr-defined]
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--tenant", default="riverbend", choices=sorted(TENANTS))
    args = ap.parse_args()
    srv = make_server(args.host, args.port, args.tenant)
    print(f"Heritage Core ({args.tenant}) on http://{args.host}:{args.port}/  user={DEMO_USER}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
