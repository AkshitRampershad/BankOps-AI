"""App profiles, tenant overlays, and policies.

Three layers, from most shared to most specific:

* AppProfile — one per *vendor product* (e.g. heritage_core). Knows how to sign in,
  which screens are known interstitials/errors/business outcomes, which labels mark PII.
  Written once, reused by every tenant and every capability on that product.
* Tenant — one per institution's *instance*. Base URL, version, secret bindings, policy,
  and an overlay (label aliases, per-step target overrides) for its configuration.
* Policy — allowlist and risk handling. Referenced by the tenant.

Capabilities are recorded against the product, not the tenant, so the same artifact can be
replayed on every tenant running a compatible version of that product.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .artifact import OutcomeRule, StateCheck, Step, Target

ROOT = Path(__file__).resolve().parents[2]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthFlow(Strict):
    steps: list[Step]
    expect: StateCheck


class AppProfile(Strict):
    product: str
    description: str = ""
    session_check: StateCheck = Field(description="Holds when an authenticated session is on screen.")
    auth: AuthFlow
    reauth: AuthFlow | None = Field(None, description="In-place re-authentication after session expiry.")
    outcomes: list[OutcomeRule] = Field(default_factory=list)
    pii_labels: list[str] = Field(default_factory=list, description="Labels whose adjacent values are PII.")


class Policy(Strict):
    name: str
    allowed_origins: list[str]
    allowed_paths: list[str]
    denied_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str]
    irreversible_controls: list[str] = Field(
        default_factory=list, description="Regexes on control names that commit to the system of record."
    )
    irreversible_handling: str = Field("require_approval", pattern="^(require_approval|block)$")
    max_steps: int = 30
    max_seconds: int = 300

    def url_allowed(self, url: str) -> tuple[bool, str]:
        u = urlparse(url)
        if u.scheme in ("about", "data", "blob"):
            return True, "non-network url"
        origin = f"{u.scheme}://{u.netloc}"
        if origin not in self.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        path = u.path or "/"
        for pat in self.denied_paths:
            if fnmatch.fnmatch(path, pat):
                return False, f"path {path} matches denied pattern {pat}"
        for pat in self.allowed_paths:
            if fnmatch.fnmatch(path, pat):
                return True, f"path matches {pat}"
        return False, f"path {path} not in allowlist"

    def is_irreversible_control(self, name: str) -> bool:
        return any(re.search(p, name or "", re.IGNORECASE) for p in self.irreversible_controls)


class Tenant(Strict):
    id: str
    product: str
    product_version: str
    base_url: str
    policy: str
    secrets: dict[str, str] = Field(default_factory=dict, description="secret ref -> environment variable")
    text_aliases: dict[str, str] = Field(
        default_factory=dict, description="Recorded label text -> this tenant's label text."
    )
    step_overrides: dict[str, dict[str, Target]] = Field(
        default_factory=dict, description="capability id -> step id -> replacement target"
    )


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def load_profile(product: str, root: Path = ROOT) -> AppProfile:
    return AppProfile.model_validate(_load_yaml(root / "profiles" / f"{product}.yaml"))


def load_tenant(tenant_id: str, root: Path = ROOT) -> Tenant:
    t = Tenant.model_validate(_load_yaml(root / "tenants" / f"{tenant_id}.yaml"))
    override = os.environ.get(f"CUA_BASE_URL_{tenant_id.upper()}")
    if override:
        t.base_url = override
    return t


def load_policy(name: str, tenant: Tenant | None = None, root: Path = ROOT) -> Policy:
    p = Policy.model_validate(_load_yaml(root / "policies" / f"{name}.yaml"))
    if tenant is not None:
        # The tenant's own origin is always the allowlisted origin for its policy.
        u = urlparse(tenant.base_url)
        p.allowed_origins = [f"{u.scheme}://{u.netloc}"]
    return p


class SecretProvider:
    """Resolves secret refs from the environment. Swap for a vault client in production.

    Values are registered with the redactor on first use so they can never be logged."""

    def __init__(self, tenant: Tenant, on_resolve=None) -> None:
        self.tenant = tenant
        self.on_resolve = on_resolve

    def get(self, ref: str) -> str:
        env = self.tenant.secrets.get(ref)
        if not env:
            raise KeyError(f"secret ref {ref!r} not bound for tenant {self.tenant.id}")
        val = os.environ.get(env)
        if val is None:
            raise KeyError(f"secret {ref!r}: environment variable {env} is not set")
        if self.on_resolve:
            self.on_resolve(val)
        return val


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))
