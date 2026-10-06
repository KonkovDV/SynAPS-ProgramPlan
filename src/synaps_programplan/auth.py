"""Roles for the workbench: planner, OKR manager, observer.

Tokens come from ``SYNAPS_PROGRAMPLAN_TOKENS`` as ``token=role:user`` items
separated by commas. A token may be given as ``sha256:<hex>`` so the secret
itself never sits in the environment. Without the variable the workbench runs
in local mode (one planner, loopback only) - ``serve`` refuses any other host.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from enum import StrEnum

ENV = "SYNAPS_PROGRAMPLAN_TOKENS"
PROXY_SECRET_ENV = "SYNAPS_PROGRAMPLAN_PROXY_SECRET"


class Role(StrEnum):
    PLANNER = "planner"
    MANAGER = "manager"
    OBSERVER = "observer"


# What each role may do in the workbench.
PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OBSERVER: frozenset({"view", "check"}),
    Role.MANAGER: frozenset({"view", "check", "decide"}),
    Role.PLANNER: frozenset({"view", "check", "decide", "repair"}),
}


@dataclass(frozen=True)
class Principal:
    user: str
    role: Role

    def may(self, action: str) -> bool:
        return action in PERMISSIONS[self.role]


LOCAL = Principal(user="local", role=Role.PLANNER)


@dataclass(frozen=True)
class _Entry:
    digest: str
    principal: Principal


class TokenStore:
    def __init__(self, entries: list[_Entry]) -> None:
        self._entries = entries

    @property
    def enabled(self) -> bool:
        return bool(self._entries)

    @classmethod
    def parse(cls, spec: str | None) -> TokenStore:
        entries: list[_Entry] = []
        for item in (spec or "").split(","):
            item = item.strip()
            if not item:
                continue
            token, sep, rest = item.partition("=")
            role_text, sep2, user = rest.partition(":")
            if not sep or not sep2 or not token or not user:
                raise ValueError(f"{ENV}: expected token=role:user, got {item!r}")
            digest = token[7:] if token.startswith("sha256:") else _sha256(token)
            entries.append(
                _Entry(digest=digest.lower(), principal=Principal(user=user, role=Role(role_text)))
            )
        return cls(entries)

    @classmethod
    def from_env(cls) -> TokenStore:
        return cls.parse(os.environ.get(ENV))

    def authenticate(self, token: str | None) -> Principal | None:
        if not self.enabled:
            return LOCAL
        if not token:
            return None
        digest = _sha256(token)
        for entry in self._entries:
            if hmac.compare_digest(entry.digest, digest):
                return entry.principal
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def proxy_principal(secret_header: str | None, user: str | None, role: str | None) -> Principal | None:
    """Identity asserted by a reverse proxy that already did corporate sign-in.

    The proxy must send ``X-Synaps-Proxy-Secret`` equal to
    ``SYNAPS_PROGRAMPLAN_PROXY_SECRET`` together with ``X-Remote-User`` and
    ``X-Remote-Role``. A user header without that secret is ignored by the
    caller; a wrong secret is not a principal.
    """
    expected = os.environ.get(PROXY_SECRET_ENV)
    if not expected or not secret_header or not hmac.compare_digest(expected, secret_header):
        return None
    if not user or not role:
        return None
    try:
        parsed = Role(role)
    except ValueError:
        return None
    return Principal(user=user, role=parsed)
