"""keel CLI: init, validate, ingest, replay, context, serve."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from keel.cartridge import load_cartridge
from keel.ingest import Ingestor, JsonlAdapter
from keel.store.memory import MemoryStore
from keel.tools import Tools

app = typer.Typer(help="Keel: a context layer for AI agents in vertical SaaS.", no_args_is_help=True)

CARTRIDGE_TEMPLATE = """name: {name}

entities:
  account:
    description: The thing your product is about. Rename it.

relations: {{}}

events:
  account.created:
    subject: account
    owner: core
    payload:
      id: string
      name: string

rules: {{}}

memory:
  ttl:
    task: 14d
    agent: 1d
  requires_evidence: [global, tenant]
"""

HANDLERS_TEMPLATE = '''"""Handlers for the {name} cartridge. One per event type, a few lines each."""

from keel.handlers import HandlerRegistry

registry = HandlerRegistry()


@registry.on("account.created")
def account_created(event, store):
    p = event.payload
    store.upsert_entity(event.tenant, "account", p["id"], ref=event.source, name=p.get("name"))
'''


@app.command()
def init(path: Path = typer.Argument(..., help="Folder for the new cartridge, e.g. cartridges/clinics")):
    """Scaffold a new cartridge."""
    path.mkdir(parents=True, exist_ok=False)
    (path / "cartridge.yaml").write_text(CARTRIDGE_TEMPLATE.format(name=path.name), encoding="utf-8")
    (path / "handlers.py").write_text(HANDLERS_TEMPLATE.format(name=path.name), encoding="utf-8")
    typer.echo(f"created {path}/cartridge.yaml and {path}/handlers.py")


@app.command()
def validate(cartridge: Path):
    """Check that the catalog, the ontology and the handlers agree."""
    c = load_cartridge(cartridge)
    problems = c.validate()
    if problems:
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(code=1)
    typer.echo(f"{c.name}: {len(c.entities)} entity kinds, {len(c.relations)} relations, {len(c.events)} events, all handled")


def _run(cartridge: Path, events: Path, strict: bool = False):
    c = load_cartridge(cartridge)
    store = MemoryStore()
    ing = Ingestor(c, store)
    report = ing.ingest(JsonlAdapter(events), strict=strict)
    return c, store, ing, report


@app.command()
def ingest(cartridge: Path, events: Path, strict: bool = False):
    """Ingest a .jsonl event stream into an in-memory graph and report."""
    _, _, _, report = _run(cartridge, events, strict)
    typer.echo(f"processed={report.processed} unhandled={len(report.unhandled)} errors={len(report.errors)}")
    for eid, err in report.errors:
        typer.echo(f"  {eid}: {err}")


@app.command()
def replay(cartridge: Path, events: Path, tenant: str):
    """Rebuild one tenant's subgraph from the log (recovery path)."""
    _, _, ing, _ = _run(cartridge, events)
    report = ing.replay(tenant)
    typer.echo(f"replayed tenant={tenant} processed={report.processed}")


@app.command()
def context(cartridge: Path, events: Path, subject: str, tenant: str | None = None):
    """Ingest, then print what the `context` tool returns for a subject."""
    c, store, ing, _ = _run(cartridge, events)
    tenant = tenant or next(iter({e.tenant for e in ing.log}), "")
    typer.echo(json.dumps(Tools(c, store, tenant).context(subject), indent=2, ensure_ascii=False))


@app.command()
def serve(
    cartridge: Path,
    tenant: str = typer.Option(..., help="The end-customer this server is bound to."),
    events: Path | None = None,
):
    """Serve the five tools over MCP (stdio). Requires `uv sync --extra mcp`."""
    from keel.mcp_server import build_server

    c = load_cartridge(cartridge)
    store = MemoryStore()
    if events:
        Ingestor(c, store).ingest(JsonlAdapter(events))
    build_server(Tools(c, store, tenant)).run()


if __name__ == "__main__":
    app()
