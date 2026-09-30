import os
import socket
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("HC_USERNAME", "operator1")
os.environ.setdefault("HC_PASSWORD", "demo-pass-123")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def demo_apps():
    """Both tenants' demo apps on free ports; tenant configs are pointed at them via env."""
    from demo_app.server import make_server

    servers = {}
    for tenant in ("riverbend", "lakeside"):
        port = _free_port()
        srv = make_server("127.0.0.1", port, tenant)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ[f"CUA_BASE_URL_{tenant.upper()}"] = f"http://127.0.0.1:{port}"
        servers[tenant] = srv
    yield servers
    for srv in servers.values():
        srv.shutdown()


@pytest.fixture
def faults(demo_apps):
    def arm(spec: dict, tenant: str = "riverbend"):
        demo_apps[tenant].app_state.faults.set(spec)
    yield arm
    for srv in demo_apps.values():
        srv.app_state.faults.set({})


@pytest.fixture
def free_port():
    return _free_port()
