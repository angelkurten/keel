"""A developer's own .env must not change what the suite sees: the CLI reads an empty one here."""

import importlib
import os
import tempfile
from typing import Any
from urllib.parse import urlparse, urlunparse

import pytest

from keel import MemoryStore, SqliteStore

fd, path = tempfile.mkstemp(prefix="keel-test-", suffix=".env")
os.close(fd)
os.environ["KEEL_ENV_FILE"] = path
for _k in ("KEEL_TENANT", "KEEL_AGENT", "KEEL_DB", "KEEL_INDEX", "KEEL_EMBEDDER"):
    os.environ.pop(_k, None)  # nor do the variables of a developer's shell


EXTERNAL = os.environ.get("KEEL_STORE")  # "pkg.module:factory": a third store runs the conformance suite and the battery
MYSQL = os.environ.get("KEEL_MYSQL_URL")  # mysql://root:keel@127.0.0.1:3306/keel, see CONTRIBUTING
POSTGRES = os.environ.get("KEEL_POSTGRES_URL")  # postgresql://postgres:keel@127.0.0.1:5499/keel (uv sync --extra postgres)
TABLES = ("entities", "relations", "facts", "ingested", "forgotten", "api_keys", "metering", "cutoffs", "accounts")


@pytest.fixture(params=["memory", "sqlite"] + (["mysql"] if MYSQL else []) + (["postgres"] if POSTGRES else []) + (["external"] if EXTERNAL else []))
def store(request, tmp_path):
    """Every store the suite can reach: a test that takes `store` runs on each."""
    if request.param == "memory":
        return MemoryStore()
    if request.param == "sqlite":
        return SqliteStore(tmp_path / "keel.db")
    if request.param == "mysql":
        from keel.store.mysql import MysqlStore

        s = MysqlStore.from_url(MYSQL)
        for table in TABLES:
            s._exec(f"DELETE FROM {table}")  # a clean database per test
        return s
    if request.param == "postgres":
        return postgres_store()
    module, _, factory = EXTERNAL.partition(":")
    return getattr(importlib.import_module(module), factory)()


def postgres_store():
    """A PostgresStore on KEEL_POSTGRES_URL with every table emptied: a clean database per test."""
    from keel.store.postgres import PostgresStore

    s = PostgresStore.from_url(POSTGRES)
    s._exec("TRUNCATE " + ", ".join(TABLES))
    return s


def as_user(user: str, password: str, database: str | None = None) -> str:
    u = urlparse(POSTGRES)
    return urlunparse(u._replace(netloc=f"{user}:{password}@{u.hostname}:{u.port}", path=f"/{database}" if database else u.path))


def admin(sql: str, database: str | None = None) -> None:
    import psycopg

    with psycopg.connect(as_user(urlparse(POSTGRES).username, urlparse(POSTGRES).password, database) if database else POSTGRES,
                         autocommit=True) as conn:
        conn.execute(sql)


def postgres_app_store(user: str = "keel_app", createrole: bool = True) -> Any:
    """A store whose user has only LOGIN (CREATEROLE) and owns its schema: the deployment CI checks."""
    import psycopg

    from keel.store.postgres import PostgresStore

    admin(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{user}') THEN "
          f"CREATE ROLE {user} LOGIN {'CREATEROLE' if createrole else ''} PASSWORD 'apppw'; END IF; END $$")
    try:  # a database of its own (an empty public schema), the user owning only its schema in it
        admin(f"CREATE DATABASE {user}_db")
    except psycopg.errors.DuplicateDatabase:
        pass
    admin(f"CREATE SCHEMA IF NOT EXISTS {user} AUTHORIZATION {user}", f"{user}_db")
    s = PostgresStore.from_url(as_user(user, "apppw", f"{user}_db"))
    s._exec("TRUNCATE " + ", ".join(TABLES))
    return s
