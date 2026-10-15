"""Configuration from a .env file: the CLI reads it, the real environment wins."""

import os

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
    res = CliRunner().invoke(app, ["ingest", f"{root}/cartridges/fleet", f"{root}/examples/events/fleet.jsonl"])
    assert res.exit_code == 0 and (tmp_path / "from-env.db").exists(), res.output
    res = CliRunner().invoke(app, ["--env-file", "missing.env", "validate", f"{root}/cartridges/fleet"], env={"COLUMNS": "300"})
    assert res.exit_code != 0 and "does not exist" in res.output


def test_keel_tenant_is_the_default_tenant(tmp_path, monkeypatch):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = str(tmp_path / "k.db")
    res = CliRunner().invoke(app, ["ingest", f"{root}/cartridges/fleet", f"{root}/examples/events/fleet.jsonl", "--db", db])
    assert res.exit_code == 0, res.output
    monkeypatch.setenv("KEEL_TENANT", "acme-logistics")
    res = CliRunner().invoke(app, ["export", "--db", db])
    assert res.exit_code == 0 and '"tenant": "acme-logistics"' in res.output, res.output
    res = CliRunner().invoke(app, ["export", "nobody", "--db", db])  # an explicit tenant wins
    assert res.exit_code == 0 and res.output.strip() == "", res.output
