"""A developer's own .env must not change what the suite sees: the CLI reads an empty one here."""

import importlib
import os
import tempfile

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
