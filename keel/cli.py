"""keel CLI: init, validate, schema, sdk, ingest, replay, expire, consolidate, export, index, context, serve."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from keel.cartridge import load_cartridge
from keel.envelope import Event
from keel.ingest import Ingestor, IngestReport, JsonlAdapter
from keel.store.memory import MemoryStore
from keel.store.sqlite import SqliteStore
from keel.tools import Tools

app = typer.Typer(help="Keel: a context layer for AI agents in vertical SaaS.", no_args_is_help=True)


def load_env_file(path: Path) -> list[str]:
    """KEY=VALUE lines into the environment, never over a variable already set (the real environment
    wins). Blank lines and # comments are skipped; `export ` and matching quotes are accepted; no
    interpolation. Returns the keys it set."""
    loaded = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not key.replace("_", "").isalnum():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()  # an inline comment after an unquoted value
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


@app.callback()
def _config(
    env_file: Path = typer.Option(Path(".env"), "--env-file", envvar="KEEL_ENV_FILE", help="Configuration as KEY=VALUE lines (KEEL_SECRET, KEEL_DB, KEEL_EMBEDDINGS_*...). Variables already set win."),
) -> None:
    """Keel: a context layer for AI agents in vertical SaaS."""
    if env_file.is_file():
        load_env_file(env_file)
    elif env_file != Path(".env"):  # the default is optional; a file you named is not
        raise typer.BadParameter(f"{env_file} does not exist", param_hint="--env-file")

DB = typer.Option(None, "--db", envvar="KEEL_DB", help="A durable graph: a SQLite file, or mysql://user:pass@host:3306/db (uv sync --extra mysql). Without it the graph lives in memory and ends with the command.")


def _store(db: str | None):
    if not db:
        return MemoryStore()
    if db.startswith(("mysql://", "mariadb://")):
        from keel.store.mysql import MysqlStore

        return MysqlStore.from_url(db)
    return SqliteStore(db)
TENANT = typer.Argument(..., envvar="KEEL_TENANT", help="The end-customer (KEEL_TENANT when omitted). A product with no end-customers sets one fixed value.", show_default=False)
AGENT = typer.Option(None, "--agent", envvar="KEEL_AGENT", help="The declared agent acting here. Required when the cartridge declares `agents:`; it bounds what is read and written.")


def _tools(c, store, tenant: str, agent: str | None, **semantic: Any) -> Tools:
    try:
        return Tools(c, store, tenant, agent, **semantic)
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
    typer.echo(json.dumps(load_cartridge(cartridge).schema(), indent=2, ensure_ascii=False))


@app.command()
def sdk(
    language: str = typer.Argument(..., help="typescript, python, go or php."),
    cartridge: Path = typer.Argument(...),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write here; stdout when absent."),
    check: bool = typer.Option(False, "--check", help="Exit 1 if --out exists and differs from what would be generated (for CI)."),
    package: str | None = typer.Option(None, "--package", help="Go: the package name (default keel<name>). PHP: the namespace (default Keel\\<Name>)."),
):
    """Generate the typed client for a cartridge: event payload types, emit constructors, subject helpers, constants."""
    from keel.sdk import generate

    text = generate(language, load_cartridge(cartridge), package=package)
    if out is None:
        typer.echo(text, nl=False)
        return
    if check:
        current = out.read_text(encoding="utf-8") if out.exists() else None
        if current != text:
            typer.echo(f"{out} is stale: regenerate with `keel sdk {language} {cartridge} -o {out}`")
            raise typer.Exit(code=1)
        typer.echo(f"{out} is up to date")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {out}")


def _run(cartridge: Path, events: Path | None, strict: bool = False, db: str | None = None):
    c = load_cartridge(cartridge)
    store = _store(db)
    ing = Ingestor(c, store)
    report = ing.ingest(JsonlAdapter(events), strict=strict, batch=500) if events else IngestReport()
    return c, store, ing, report


@app.command()
def ingest(cartridge: Path, events: Path, strict: bool = False, db: str | None = DB):
    """Ingest a .jsonl event stream into the graph (in memory, or the --db file) and report."""
    _, _, _, report = _run(cartridge, events, strict, db)
    typer.echo(f"processed={report.processed} duplicates={report.duplicates} unhandled={len(report.unhandled)} errors={len(report.errors)}")
    for eid, err in report.errors:
        typer.echo(f"  {eid}: {err}")


@app.command()
def replay(cartridge: Path, events: Path, tenant: str = TENANT, db: str | None = DB):
    """Rebuild one tenant's subgraph from the event log (recovery path). With --db, the file is wiped for that tenant first."""
    _, _, ing, _ = _run(cartridge, None, db=db)
    report = ing.replay(tenant, events=JsonlAdapter(events))
    typer.echo(f"replayed tenant={tenant} processed={report.processed}")


@app.command()
def expire(
    tenant: str = TENANT,
    db: str = typer.Option(..., "--db", help="The durable graph to clean: a SQLite file or a mysql:// URL."),
    task: str | None = typer.Option(None, "--task", help="Close one task first: its memory expires now."),
    consolidate: Path | None = typer.Option(None, "--consolidate", help="Consolidate first, with this cartridge's `consolidate:` section, so nothing is dropped unsummarized."),
    summarizer: str | None = typer.Option(None, "--summarizer", help="With --consolidate: llm (KEEL_SUMMARIZER_* env vars), join, or pkg.module:factory."),
):
    """Drop task/agent memory past its expiry for one tenant. Reads already ignore expired facts; run this from cron to reclaim space."""
    store = _store(db)
    if consolidate:
        _consolidate(load_cartridge(consolidate), store, tenant, summarizer)
    if task:
        typer.echo(f"closed task={task} facts={store.close_task(tenant, task)}")
    n = store.expire(tenant)
    typer.echo(f"expired tenant={tenant} dropped={n}")


def _consolidate(c, store, tenant: str, summarizer: str | None) -> None:
    from keel.consolidate import consolidate_tenant, load_summarizer

    report = consolidate_tenant(c, store, tenant, summarizer=load_summarizer(summarizer) if summarizer else None)
    for name in report.written:
        typer.echo(f"consolidated {name}: written={report.written[name]} unchanged={report.unchanged[name]}")


@app.command(name="consolidate")
def consolidate_cmd(
    cartridge: Path,
    tenant: str = typer.Option(..., envvar="KEEL_TENANT", help="The end-customer to consolidate."),
    db: str = typer.Option(..., "--db", help="The durable graph: a SQLite file or a mysql:// URL."),
    summarizer: str | None = typer.Option(None, "--summarizer", help="For `reduce: summary`: llm (KEEL_SUMMARIZER_* env vars), join, or pkg.module:factory."),
):
    """Summarize episodic memory into durable facts, as the cartridge's `consolidate:` section declares. Idempotent; a job, never ingest."""
    _consolidate(load_cartridge(cartridge), _store(db), tenant, summarizer)


@app.command()
def export(
    tenant: str = TENANT,
    db: str = typer.Option(..., "--db", help="The durable graph to export from: a SQLite file or a mysql:// URL."),
    out: Path | None = typer.Option(None, "--out", help="Write here instead of stdout."),
):
    """Audit trail for one tenant as JSONL: every entity, relation and fact, closed ones included, with provenance, scope, evidence and who corrected what."""
    from dataclasses import asdict

    from keel.store.base import Entity, Fact
    from keel.tools import _ser

    lines = (json.dumps({"record": type(o).__name__.lower(), **(_ser(o) if isinstance(o, (Entity, Fact)) else _ser(asdict(o)))}, ensure_ascii=False) for o in _store(db).dump(tenant))  # entities carry their key, as everywhere
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


@app.command(name="index")
def index_cmd(
    cartridge: Path,
    tenant: str = typer.Option(..., envvar="KEEL_TENANT", help="The end-customer to index."),
    db: str = typer.Option(..., "--db", help="The durable graph: a SQLite file or a mysql:// URL."),
    index: str = typer.Option(..., "--index", envvar="KEEL_INDEX", help="memory, chroma:///path, chroma://host:port, or your adapter: pkg.module:factory?k=v"),
    embedder: str = typer.Option("openai", "--embedder", envvar="KEEL_EMBEDDER", help="openai (KEEL_EMBEDDINGS_* env vars), hash, or pkg.module:factory?k=v"),
    full: bool = typer.Option(False, "--full", help="Rebuild the tenant's index instead of updating it from the cursor."),
    cursor: Path | None = typer.Option(None, "--cursor", help="Where the watermark of the last run is kept (default .keel/index-<tenant>.cursor)."),
):
    """Bring the semantic index up to date for one tenant, outside ingest: only subjects changed since the last run."""
    from keel.envelope import parse_iso
    from keel.semantic import index_tenant, load_embedder, load_index

    c = load_cartridge(cartridge)
    path = cursor or Path(".keel") / f"index-{tenant}.cursor"
    since = None if full or not path.exists() else parse_iso(path.read_text(encoding="utf-8").strip())
    report = index_tenant(_store(db), tenant, load_embedder(embedder), load_index(index), since=since, policy=c.semantic)
    if report.cursor is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.cursor.isoformat(), encoding="utf-8")
    typer.echo(f"indexed tenant={tenant} subjects={report.indexed} removed={report.deleted} mode={'full' if since is None else 'incremental'}")


@app.command()
def context(cartridge: Path, events: Path, subject: str, tenant: str | None = typer.Option(None, envvar="KEEL_TENANT", help="Default: KEEL_TENANT, else the first tenant in the log."), agent: str | None = AGENT):
    """Ingest, then print what the `context` tool returns for a subject."""
    c, store, ing, _ = _run(cartridge, events)
    tenant = tenant or next(iter({e.tenant for e in ing.log}), "")
    typer.echo(json.dumps(_tools(c, store, tenant, agent).context(subject), indent=2, ensure_ascii=False))


def _loopback(host: str) -> bool:
    import ipaddress

    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname: assume it is reachable


@app.command()
def serve(
    cartridge: Path,
    tenant: str | None = typer.Option(None, envvar="KEEL_TENANT", help="The end-customer this server is bound to. Required with stdio; with http it is the default when X-Keel-Tenant is absent and enables /mcp."),
    events: Path | None = None,
    db: str | None = DB,
    agent: str | None = AGENT,
    transport: str = typer.Option("stdio", help="stdio: MCP for one tenant. http: the /v1 JSON API for any tenant (X-Keel-Tenant), plus /mcp when --tenant is given."),
    host: str = typer.Option("127.0.0.1", help="http only."),
    port: int = typer.Option(8000, help="http only."),
    secret_env: str = typer.Option("KEEL_SECRET", help="http only: the env var holding the shared bearer secret. Unset = open, localhost only."),
    allow_open: bool = typer.Option(False, "--allow-open", help="http only: serve without a secret on a non-loopback host (behind your own auth proxy)."),
    index: str | None = typer.Option(None, "--index", envvar="KEEL_INDEX", help="Enable search(mode=semantic): the vector index spec, as in `keel index`."),
    embedder: str = typer.Option("openai", "--embedder", envvar="KEEL_EMBEDDER", help="With --index: the embedder spec."),
    reranker: str | None = typer.Option(None, "--reranker", help="With --index: http, llm (KEEL_RERANK_* env vars), or pkg.module:factory."),
):
    """Serve the five tools over MCP (stdio) or HTTP. --events fills an in-memory graph; --db serves a durable one."""
    c = load_cartridge(cartridge)
    store = _store(db)
    if events:
        Ingestor(c, store).ingest(JsonlAdapter(events))
    semantic: dict[str, Any] = {}
    if index:
        from keel.semantic import load_embedder, load_index, load_reranker

        semantic = {"embedder": load_embedder(embedder), "index": load_index(index), "reranker": load_reranker(reranker) if reranker else None}
    if transport == "stdio":
        from keel.mcp_server import build_server

        if not tenant:
            raise typer.BadParameter("--tenant is required with the stdio transport", param_hint="--tenant")
        build_server(_tools(c, store, tenant, agent, **semantic)).run()
        return
    if transport != "http":
        raise typer.BadParameter("stdio or http", param_hint="--transport")
    secret = os.environ.get(secret_env)
    if not secret and not _loopback(host) and not allow_open:
        raise typer.BadParameter(
            f"{secret_env} is unset and {host} is reachable from the network: set the secret, bind 127.0.0.1, "
            "or pass --allow-open if your own proxy authenticates", param_hint="--host",
        )
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("The HTTP transport needs the optional dependency: uv sync --extra http") from exc
    from keel.http import build_http_app

    if not secret:
        typer.echo(f"warning: {secret_env} is unset, the API is open on {host}", err=True)
    if tenant:
        _tools(c, store, tenant, agent)  # fail now, not on the first request, if the agent is not declared
    uvicorn.run(build_http_app(c, store, secret=secret, tenant=tenant, agent=agent, host=host, **semantic), host=host, port=port)


if __name__ == "__main__":
    app()
