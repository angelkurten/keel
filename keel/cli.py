"""keel CLI: init, validate, schema, ingest, replay, expire, export, context, serve."""

from __future__ import annotations

import json
from pathlib import Path

import typer
import yaml

from keel import __version__
from keel.cartridge import load_cartridge
from keel.envelope import Event
from keel.ingest import Ingestor, IngestReport, JsonlAdapter
from keel.store.memory import MemoryStore
from keel.store.sqlite import SqliteStore
from keel.tools import Tools

app = typer.Typer(help="Keel: a context layer for AI agents in vertical SaaS.", no_args_is_help=True)

DB = typer.Option(None, "--db", help="SQLite file for a durable graph. Without it the graph lives in memory and ends with the command.")
AGENT = typer.Option(None, "--agent", help="The declared agent acting here. Required when the cartridge declares `agents:`; it bounds what is read and written.")


def _tools(c, store, tenant: str, agent: str | None) -> Tools:
    try:
        return Tools(c, store, tenant, agent)
    except PermissionError as exc:
        raise typer.BadParameter(str(exc), param_hint="--agent") from exc

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
def validate(
    cartridge: Path,
    events: Path | None = typer.Option(None, help="Also check a .jsonl stream: unknown types, wrong subject kinds, payload shape."),
):
    """Check that the catalog, the ontology and the handlers agree; with --events, that a stream fits the catalog."""
    c = load_cartridge(cartridge)
    problems = c.validate()
    checked = 0
    if events:
        for n, line in enumerate(events.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.startswith("#"):
                continue
            try:
                ev = Event.model_validate(json.loads(line))
            except ValueError as exc:  # json and pydantic errors both subclass ValueError
                errs = getattr(exc, "errors", None)  # pydantic: first error, with its field
                detail = f"{'.'.join(map(str, errs()[0]['loc']))}: {errs()[0]['msg']}" if errs else str(exc)
                problems.append(f"line {n}: bad envelope: {detail}")
                continue
            checked += 1
            problems += [f"line {n}: {p}" for p in c.check_event(ev)]
    if problems:
        for p in problems:
            typer.echo(f"  - {p}")
        raise typer.Exit(code=1)
    typer.echo(f"{c.name}: {len(c.entities)} entity kinds, {len(c.relations)} relations, {len(c.events)} events, all handled")
    if events:
        typer.echo(f"{events}: {checked} events match the catalog")


@app.command()
def schema(cartridge: Path):
    """The cartridge as JSON Schema: envelope, events with payload schemas, kinds, relations, rules, agents, memory. The input of every SDK template."""
    from dataclasses import asdict

    from keel.schema import payload_schema

    c = load_cartridge(cartridge)
    doc = {
        "cartridge": c.name,
        "version": yaml.safe_load((cartridge / "cartridge.yaml").read_text(encoding="utf-8")).get("version"),
        "keel": __version__,
        "envelope": Event.model_json_schema(),
        "entities": c.entities,
        "relations": c.relations,
        "events": {t: {**{k: v for k, v in s.items() if k != "payload"}, "payload": payload_schema(s.get("payload"))} for t, s in c.events.items()},
        "rules": c.rules,
        "agents": {a.id: {**asdict(a), "writes": sorted(a.writes), "kinds": sorted(a.kinds) if a.kinds is not None else None, "relations": sorted(a.relations) if a.relations is not None else None} for a in c.agents.values()},
        "memory": {"ttl": {s.value: (None if d is None else d.total_seconds()) for s, d in c.policy.ttl.items()}, "requires_evidence": sorted(s.value for s in c.policy.requires_evidence)},
    }
    typer.echo(json.dumps(doc, indent=2, ensure_ascii=False))


def _run(cartridge: Path, events: Path | None, strict: bool = False, db: Path | None = None):
    c = load_cartridge(cartridge)
    store = SqliteStore(db) if db else MemoryStore()
    ing = Ingestor(c, store)
    report = ing.ingest(JsonlAdapter(events), strict=strict) if events else IngestReport()
    return c, store, ing, report


@app.command()
def ingest(cartridge: Path, events: Path, strict: bool = False, db: Path | None = DB):
    """Ingest a .jsonl event stream into the graph (in memory, or the --db file) and report."""
    _, _, _, report = _run(cartridge, events, strict, db)
    typer.echo(f"processed={report.processed} unhandled={len(report.unhandled)} errors={len(report.errors)}")
    for eid, err in report.errors:
        typer.echo(f"  {eid}: {err}")


@app.command()
def replay(cartridge: Path, events: Path, tenant: str, db: Path | None = DB):
    """Rebuild one tenant's subgraph from the event log (recovery path). With --db, the file is wiped for that tenant first."""
    _, _, ing, _ = _run(cartridge, None, db=db)
    report = ing.replay(tenant, events=JsonlAdapter(events))
    typer.echo(f"replayed tenant={tenant} processed={report.processed}")


@app.command()
def expire(tenant: str, db: Path = typer.Option(..., "--db", help="The durable graph to clean.")):
    """Drop task/agent memory past its expiry for one tenant. Reads already ignore expired facts; run this from cron to reclaim space."""
    n = SqliteStore(db).expire(tenant)
    typer.echo(f"expired tenant={tenant} dropped={n}")


@app.command()
def export(
    tenant: str,
    db: Path = typer.Option(..., "--db", help="The durable graph to export from."),
    out: Path | None = typer.Option(None, "--out", help="Write here instead of stdout."),
):
    """Audit trail for one tenant as JSONL: every entity, relation and fact, closed ones included, with provenance, scope, evidence and who corrected what."""
    from dataclasses import asdict

    from keel.tools import _ser

    lines = (json.dumps({"record": type(o).__name__.lower(), **_ser(asdict(o))}, ensure_ascii=False) for o in SqliteStore(db).dump(tenant))
    if out:
        n = 0
        with out.open("w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
                n += 1
        typer.echo(f"exported tenant={tenant} records={n} to {out}")
    else:
        for line in lines:
            typer.echo(line)


@app.command()
def context(cartridge: Path, events: Path, subject: str, tenant: str | None = None, agent: str | None = AGENT):
    """Ingest, then print what the `context` tool returns for a subject."""
    c, store, ing, _ = _run(cartridge, events)
    tenant = tenant or next(iter({e.tenant for e in ing.log}), "")
    typer.echo(json.dumps(_tools(c, store, tenant, agent).context(subject), indent=2, ensure_ascii=False))


@app.command()
def serve(
    cartridge: Path,
    tenant: str = typer.Option(..., help="The end-customer this server is bound to."),
    events: Path | None = None,
    db: Path | None = DB,
    agent: str | None = AGENT,
):
    """Serve the five tools over MCP (stdio). Requires `uv sync --extra mcp`. --events fills an in-memory graph; --db serves a durable one."""
    from keel.mcp_server import build_server

    c = load_cartridge(cartridge)
    store = SqliteStore(db) if db else MemoryStore()
    if events:
        Ingestor(c, store).ingest(JsonlAdapter(events))
    build_server(_tools(c, store, tenant, agent)).run()


if __name__ == "__main__":
    app()
