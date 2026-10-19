"""Configuration from a .env file: the CLI reads it, the real environment wins."""

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from keel.cli import app, load_env_file


def _unset(monkeypatch, *keys):
    """Absent now and absent again after the test, even if the loader sets them meanwhile."""
    for k in keys:
        monkeypatch.setenv(k, "")  # registers the original state, so teardown restores it
        monkeypatch.delenv(k)


def test_env_file_parsing_and_precedence(tmp_path, monkeypatch):
    _unset(monkeypatch, "A", "B", "C", "D", "E", "SET")
    monkeypatch.setenv("SET", "from the environment")
    f = tmp_path / ".env"
    f.write_text('# comment\n\nA=1\nexport B="two words"\nC=\'#not a comment\'\nD=x # inline\nSET=from the file\nnot a line\nE=\n', encoding="utf-8")
    assert sorted(load_env_file(f)) == ["A", "B", "C", "D", "E"]
    assert (os.environ["A"], os.environ["B"], os.environ["C"], os.environ["D"], os.environ["E"]) == ("1", "two words", "#not a comment", "x", "")
    assert os.environ["SET"] == "from the environment"


def test_cli_reads_db_and_more_from_an_env_file(tmp_path, monkeypatch):
    _unset(monkeypatch, "KEEL_DB")
    monkeypatch.delenv("KEEL_ENV_FILE")  # the suite points it at an empty file; here the default .env is the point
    monkeypatch.chdir(tmp_path)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    (tmp_path / ".env").write_text(f"KEEL_DB={tmp_path / 'from-env.db'}\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["ingest", f"{root}/examples/fleet", f"{root}/examples/fleet/events.jsonl"])
    assert res.exit_code == 0 and (tmp_path / "from-env.db").exists(), res.output
    res = CliRunner().invoke(app, ["--env-file", "missing.env", "validate", f"{root}/examples/fleet"], env={"COLUMNS": "300"})
    assert res.exit_code != 0 and "does not exist" in res.output


def test_keel_tenant_is_the_default_tenant(tmp_path, monkeypatch):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = str(tmp_path / "k.db")
    res = CliRunner().invoke(app, ["ingest", f"{root}/examples/fleet", f"{root}/examples/fleet/events.jsonl", "--db", db])
    assert res.exit_code == 0, res.output
    monkeypatch.setenv("KEEL_TENANT", "acme-logistics")
    res = CliRunner().invoke(app, ["export", "--db", db])
    assert res.exit_code == 0 and '"tenant": "acme-logistics"' in res.output, res.output
    res = CliRunner().invoke(app, ["export", "nobody", "--db", db])  # an explicit tenant wins
    assert res.exit_code == 0 and res.output.strip() == "", res.output


def test_keel_db_reaches_every_command_with_db(tmp_path, monkeypatch):
    """A container is configured by its environment: no command with --db may require the flag."""
    monkeypatch.chdir(tmp_path)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cart = f"{root}/examples/fleet"
    monkeypatch.setenv("KEEL_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("KEEL_TENANT", "acme-logistics")
    assert CliRunner().invoke(app, ["ingest", cart, f"{root}/examples/fleet/events.jsonl"]).exit_code == 0
    for args in (["export"], ["expire"], ["consolidate", cart], ["index", cart, "--index", "memory", "--embedder", "hash"]):
        res = CliRunner().invoke(app, args)
        assert res.exit_code == 0 and "Missing option" not in res.output, (args, res.output)


def test_keys_cli(tmp_path):
    db = str(tmp_path / "k.db")
    res = CliRunner().invoke(app, ["keys", "create", "acme", "--db", db, "--tenant", "c1"])
    made = json.loads(res.output)
    assert res.exit_code == 0 and made["key"].startswith(made["id"]) and made["tenant"] == "c1"
    listed = CliRunner().invoke(app, ["keys", "list", "--db", db]).output
    assert made["id"] in listed and made["key"] not in listed  # never printed again
    assert CliRunner().invoke(app, ["keys", "create", "Bad/Name", "--db", db]).exit_code != 0
    assert CliRunner().invoke(app, ["keys", "revoke", made["id"], "--db", db]).exit_code == 0
    assert CliRunner().invoke(app, ["keys", "revoke", made["id"], "--db", db]).exit_code != 0


def test_usage_cli(tmp_path):
    from keel.store.sqlite import SqliteStore

    db = str(tmp_path / "u.db")
    SqliteStore(db).meter([("acme/c1", "2027-01-05", "events", 7), ("solo", "2027-02-01", "events", 1)])
    out = CliRunner().invoke(app, ["usage", "--db", db, "--month", "2027-01"])
    assert out.exit_code == 0 and [json.loads(x) for x in out.output.splitlines()] == [
        {"account": "acme", "tenant": "acme/c1", "month": "2027-01", "events": 7, "calls": {}}]
    assert CliRunner().invoke(app, ["usage", "--db", db, "--month", "2027-13"]).exit_code != 0


def test_forget_before_cli(tmp_path):
    root = Path(__file__).resolve().parent.parent
    db = str(tmp_path / "r.db")
    assert CliRunner().invoke(app, ["ingest", f"{root}/examples/fleet", f"{root}/examples/fleet/events.jsonl", "--db", db]).exit_code == 0
    assert CliRunner().invoke(app, ["forget", "acme-logistics", "--db", db, "--before", "2026-10-05"]).exit_code != 0  # no --yes
    assert CliRunner().invoke(app, ["forget", "--db", db, "--yes"]).exit_code != 0  # names no tenant
    out = CliRunner().invoke(app, ["forget", "--all-tenants", "--db", db, "--before", "2026-10-05", "--yes"])
    assert out.exit_code == 0 and json.loads(out.output) == {"tenant": "acme-logistics", "before": "2026-10-05T00:00:00+00:00", "facts": 1, "annotations": 0, "relations": 0}


def test_accounts_cli(tmp_path):
    db = str(tmp_path / "a.db")
    out = CliRunner().invoke(app, ["accounts", "set", "acme", "--db", db, "--rps", "5", "--retention", "90d"])
    assert out.exit_code == 0 and json.loads(out.output) == {"account": "acme", "rps": 5, "events_month": None, "store": None, "retention": "90d"}
    assert CliRunner().invoke(app, ["accounts", "set", "acme", "--db", db, "--retention", "forever"]).exit_code != 0
    assert '"acme"' in CliRunner().invoke(app, ["accounts", "list", "--db", db]).output


def test_forget_policy_uses_each_accounts_retention(tmp_path):
    from keel.accounts import set_account
    from keel.store.sqlite import SqliteStore

    root = Path(__file__).resolve().parent.parent
    shared, own = str(tmp_path / "s.db"), str(tmp_path / "own.db")
    lines = (root / "examples/fleet/events.jsonl").read_text(encoding="utf-8").splitlines()
    for path, tenant in ((shared, "acme/c1"), (own, "big/c1"), (shared, "solo")):
        f = tmp_path / f"{tenant.replace('/', '_')}.jsonl"
        f.write_text("\n".join(x.replace('"acme-logistics"', f'"{tenant}"') for x in lines if x.strip() and not x.startswith("#")), encoding="utf-8")
        assert CliRunner().invoke(app, ["ingest", f"{root}/examples/fleet", str(f), "--db", path]).exit_code == 0
    s = SqliteStore(shared)
    set_account(s, "acme", retention="1d")
    set_account(s, "big", retention="1d", store=own)
    out = CliRunner().invoke(app, ["forget", "--policy", "--db", shared, "--yes"])
    rows = [json.loads(x) for x in out.output.splitlines()]
    assert out.exit_code == 0 and sorted((r["account"], r["tenant"], r["facts"]) for r in rows) == [("acme", "acme/c1", 2), ("big", "big/c1", 2)]


def test_serve_index_every_indexes_in_a_thread_of_the_server(tmp_path, monkeypatch):
    """keel serve --index-every: the process that searches the index is the one that writes it."""
    import time

    from keel import Ingestor, Tools, load_cartridge
    from keel.cli import _index_in_background
    from keel.ingest import JsonlAdapter
    from keel.semantic import HashEmbedder, MemoryIndex
    from keel.store.sqlite import SqliteStore

    monkeypatch.setenv("KEEL_INDEX_CURSOR_DIR", str(tmp_path))
    c, store = load_cartridge("examples/fleet"), SqliteStore(tmp_path / "g.db")
    Ingestor(c, store).ingest(JsonlAdapter("examples/fleet/events.jsonl"), strict=True)
    vectors, emb, status = MemoryIndex(), HashEmbedder(), {}
    _index_in_background(c, store, vectors, emb, "memory", 3600, status)
    for _ in range(100):
        if status.get("ok"):
            break
        time.sleep(0.05)
    assert status["ok"] and status["tenants"] == 1 and (tmp_path / "index-acme-logistics.cursor").exists()
    hits = Tools(c, store, "acme-logistics", embedder=emb, index=vectors).search("T-9001", mode="semantic")
    assert hits and hits[0]["key"] == "tire:T-9001"


def test_version_and_init_example(tmp_path):
    from keel import __version__

    assert CliRunner().invoke(app, ["--version"]).output.strip() == f"keel {__version__}"
    dest = tmp_path / "fleet"
    assert CliRunner().invoke(app, ["init", str(dest), "--example", "fleet"]).exit_code == 0
    assert sorted(p.name for p in dest.iterdir()) == ["cartridge.yaml", "events.jsonl", "handlers.py"]
    res = CliRunner().invoke(app, ["ingest", str(dest), str(dest / "events.jsonl")])
    assert res.exit_code == 0, res.output
    assert CliRunner().invoke(app, ["init", str(tmp_path / "x"), "--example", "nope"]).exit_code != 0
