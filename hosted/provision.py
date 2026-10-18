"""What the control plane creates for an account: its own database and its own Keel container.

    AccountDatabases(admin_url).create("acme")   -> mysql://acme_x:pw@host:3306/keel_acme (grants on that database only)
    CoolifyProvisioner(...).create("acme", files, env) -> (app uuid, https://acme.<base domain>)

The account's container never holds a credential for anything but its own database: a cartridge carries
Python, and it must not reach another account's data or the control plane's.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Protocol
from urllib.parse import urlparse


def db_name(account: str) -> str:
    return "keel_" + account.replace("-", "_")


class Databases(Protocol):
    def create(self, account: str) -> str: ...  # returns the account's own --db URL

    def drop(self, account: str) -> None: ...


class AccountDatabases:
    """One MySQL database and one user per account, created with an admin connection."""

    def __init__(self, admin_url: str, host_for_accounts: str | None = None) -> None:
        self.admin_url = admin_url
        u = urlparse(admin_url)
        self.host, self.port = host_for_accounts or u.hostname or "127.0.0.1", u.port or 3306

    def _admin(self) -> Any:
        import pymysql

        u = urlparse(self.admin_url)
        return pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username, password=u.password or "", autocommit=True)

    def create(self, account: str) -> str:
        name, user, pw = db_name(account), f"{db_name(account)[:24]}_{secrets.token_hex(3)}", secrets.token_urlsafe(24)
        if not re.fullmatch(r"keel_[a-z0-9_]{1,40}", name):
            raise ValueError(f"unexpected database name {name}")
        conn = self._admin()
        try:
            cur = conn.cursor()
            cur.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
            cur.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (user, pw))
            cur.execute(f"GRANT ALL PRIVILEGES ON `{name}`.* TO %s@'%%'", (user,))
        finally:
            conn.close()
        return f"mysql://{user}:{pw}@{self.host}:{self.port}/{name}"

    def drop(self, account: str) -> None:
        conn = self._admin()
        try:
            conn.cursor().execute(f"DROP DATABASE IF EXISTS `{db_name(account)}`")
        finally:
            conn.close()


class Provisioner(Protocol):
    def create(self, account: str, files: dict[str, str], env: dict[str, str]) -> tuple[str, str]: ...  # (id, endpoint)


class CoolifyProvisioner:
    """A Keel container per account on Coolify (API v1): the official image, the cartridge mounted as files at
    /cartridge, the account's own database, a subdomain under `base_domain`, and a deploy."""

    def __init__(self, url: str, token: str, project_uuid: str, server_uuid: str, base_domain: str,
                 image_tag: str, environment: str = "production", http: Any = None) -> None:
        import httpx

        self.base_domain, self.image_tag, self.project, self.server, self.environment = base_domain, image_tag, project_uuid, server_uuid, environment
        self.http = http or httpx.Client(base_url=url.rstrip("/") + "/api/v1", headers={"Authorization": f"Bearer {token}"}, timeout=60)

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        r = self.http.request(method, path, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Coolify {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else None

    def create(self, account: str, files: dict[str, str], env: dict[str, str]) -> tuple[str, str]:
        endpoint = f"https://{account}.{self.base_domain}"
        app = self._call("POST", "/applications/dockerimage", json={
            "project_uuid": self.project, "server_uuid": self.server, "environment_name": self.environment,
            "docker_registry_image_name": "ghcr.io/angelkurten/keel", "docker_registry_image_tag": self.image_tag,
            "ports_exposes": "8000", "name": f"keel-{account}", "description": f"Keel for account {account} (hosted)",
            "domains": endpoint, "health_check_enabled": True, "health_check_path": "/health", "instant_deploy": False,
        })
        uuid = app["uuid"]
        for name, content in files.items():
            self._call("POST", f"/applications/{uuid}/storages", json={"type": "file", "mount_path": f"/cartridge/{name}", "content": content})
        for k, v in env.items():
            self._call("POST", f"/applications/{uuid}/envs", json={"key": k, "value": v, "is_literal": True})
        self._call("POST", "/deploy", params={"uuid": uuid})
        return uuid, endpoint
