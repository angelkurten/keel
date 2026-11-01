import typer.main

from keel.cli import app


def test_every_db_option_names_postgres():
    """open_store takes postgresql:// (PostgresStore); the --db help of every command said SQLite or mysql:// only."""
    group = typer.main.get_command(app)
    helps = {name: p.help for name, cmd in group.commands.items() for p in cmd.params if "--db" in getattr(p, "opts", [])}
    assert helps
    assert all("postgresql://" in h for h in helps.values()), [n for n, h in helps.items() if "postgresql://" not in h]
