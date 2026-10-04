"""Role-checking TLS client pieces (#1161): an adapter that requires a reserved
role name in the peer certificate, and the lock file that records a seen role."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter, HTTPAdapter

from durable_io import write_durable  # type: ignore[import-not-found]  # sibling — leaf module

log = logging.getLogger("llm-systems-manager.tls_roles")


class RoleAdapter(HTTPAdapter):
    """HTTPS adapter that requires one reserved role name in the peer certificate."""

    def __init__(self, role_name: str, quiet: bool = False, **kwargs):
        self._role_name = role_name
        self._quiet = quiet
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_hostname"] = self._role_name
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs["assert_hostname"] = self._role_name
        return super().proxy_manager_for(proxy, **kwargs)

    def send(self, request, **kwargs):
        try:
            return super().send(request, **kwargs)
        except Exception as e:
            if not self._quiet and is_name_mismatch(e):
                log.error("refused %s: its certificate does not carry the required role %s",
                          urlsplit(request.url).netloc, self._role_name)
            raise


class PlainRefused(BaseAdapter):
    """Refuses every request; mounted on http:// where a certificate role is required."""

    def send(self, request, **kwargs):
        raise requests.exceptions.ConnectionError(
            f"refused {urlsplit(request.url).netloc}: plain HTTP where a certificate role is required",
            request=request)

    def close(self):
        pass


def role_session(role_name: str, quiet: bool = False) -> requests.Session:
    """A fresh session that requires `role_name` over HTTPS and refuses plain HTTP; `quiet` skips the refusal log."""
    s = requests.Session()
    s.mount("https://", RoleAdapter(role_name, quiet=quiet))
    s.mount("http://", PlainRefused())
    return s


def is_name_mismatch(exc: BaseException) -> bool:
    """True when the failure is the peer certificate lacking the required name."""
    seen = set()
    cur: "BaseException | None" = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ == "CertificateError":
            return True
        inner = [getattr(cur, "reason", None), *getattr(cur, "args", ())]
        if any(isinstance(x, BaseException) and id(x) not in seen and is_name_mismatch(x) for x in inner):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def load_locks(path: Path, roles: tuple) -> dict:
    """{role: locked_at}; empty when the file is absent, every role when it is unreadable."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return {r: str(data[r]) for r in roles if data.get(r)}
    except Exception as e:
        log.error("role lock file %s is unreadable (%s) — every role stays required", path.name, e)
        return {r: "unreadable" for r in roles}


def lock_role(path: Path, role: str, roles: tuple) -> None:
    """Records `role` as locked; an existing entry is left as it is."""
    if role not in roles:
        raise ValueError(f"unknown role {role!r}")
    path = Path(path)
    locks = load_locks(path, roles)
    if locks.get(role):
        return
    locks[role] = datetime.now(timezone.utc).isoformat()
    write_durable(path, json.dumps(locks, indent=2))
