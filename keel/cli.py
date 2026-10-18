"""keel CLI: init, validate, schema, sdk, ingest, replay, expire, consolidate, export, index, context, serve, keys, usage."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import typer

from keel.cartridge import load_cartridge
from keel.envelope import Event
from keel.ingest import BATCH, Ingestor, IngestReport, JsonlAdapter
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
ALL = typer.Option(False, "--all-tenants", help="Every tenant in the store, one after another (a tenant per end-customer means many).")


def _tenants(store, tenant: str | None, all_tenants: bool) -> list[str]:
    if all_tenants:
        return store.tenants()
    if not tenant:
        raise typer.BadParameter("name the tenant (or set KEEL_TENANT), or pass --all-tenants")
    return [tenant]


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
    ing = Ingestor(c, store, keep_log=db is None)  # an in-memory graph is already that size; a --db load is not
    report = ing.ingest(JsonlAdapter(events), strict=strict, batch=BATCH) if events else IngestReport()
    return c, store, ing, report


@app.command()
def ingest(cartridge: Path, events: Path, strict: bool = False, db: str | None = DB):
    """Ingest a .jsonl event stream into the graph (in memory, or the --db file) and report."""
    _, _, _, report = _run(cartridge, events, strict, db)
    typer.echo(f"processed={report.processed} duplicates={report.duplicates} unhandled={len(report.unhandled)} errors={len(report.errors)} skipped={report.skipped}")
    for t, counts in report.forgotten:
        typer.echo("  forgot " + json.dumps({"tenant": t, **counts}))
    for eid, err in report.errors:
        typer.echo(f"  {eid}: {err}")


def _queue(url: str):
    """sqs: an https://sqs.<region>.amazonaws.com/... queue URL, or any SQS-compatible one (ElasticMQ:
    http://elasticmq:9324/000000000000/keel); credentials and region from the usual AWS_* variables.
    ponytail: SQS only. Kafka has no per-message redelivery (a later commit skips a failed offset), so it
    needs a dead-letter topic of its own before it gets a command; KafkaAdapter works from Python."""
    from urllib.parse import urlparse

    from keel.adapters.bus import SqsAdapter

    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise typer.BadParameter(f"{url}: an SQS queue URL (http[s]://...)", param_hint="--queue")
    aws = u.hostname or ""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or (aws.split(".")[1] if aws.endswith(".amazonaws.com") else "us-east-1")
    endpoint = None if aws.endswith(".amazonaws.com") else f"{u.scheme}://{u.netloc}"  # an SQS-compatible server answers at the queue's host
    return SqsAdapter.connect(url, wait_seconds=20, region_name=region, endpoint_url=endpoint)


@app.command()
def consume(
    cartridge: Path,
    queue: str = typer.Option(..., "--queue", envvar="KEEL_QUEUE", help="An SQS queue URL, AWS or SQS-compatible like ElasticMQ (AWS_* variables for credentials and region)."),
    db: str = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph: a SQLite file or a mysql:// URL."),
    once: bool = typer.Option(False, "--once", help="Drain what is queued now and stop, instead of consuming forever."),
    batch: int = typer.Option(100, "--batch", min=1, help="Messages stored per transaction: one long poll, topped up with what is already queued."),
):
    """Consume envelopes from a queue into the graph, a batch per transaction. A message is acknowledged only once it is
    stored; when a batch fails it is retried one message at a time, and one that still fails (not an envelope, no handler,
    a handler error) stays unacknowledged, so the queue redelivers it and, after its maxReceiveCount, moves it to its
    dead-letter queue. If the store itself stops answering, the command exits (code 2) for its supervisor to restart it,
    instead of dead-lettering everything. Erasures (`forgets: tenant`) print one JSON line for your audit log."""
    from keel.adapters.bus import parse

    c, store = load_cartridge(cartridge), _store(db)
    ing, adapter = Ingestor(c, store), _queue(queue)
    done = errors = 0
    typer.echo(f"consuming {queue} into {c.name}", err=True)

    def store_up() -> bool:
        try:
            with store.transaction():  # the write connection, reconnected if the server dropped it
                store.entity("__keel__", "probe:probe")
            return True
        except Exception as exc:  # noqa: BLE001 (any driver's error means the same: nothing can be stored)
            typer.echo(f"store unavailable: {type(exc).__name__}: {exc}", err=True)
            return False

    def stored(events: list[Event], acks: list[Callable[[], None]]) -> None:
        nonlocal done
        report = ing.ingest(events, strict=True, batch=len(events))
        adapter.ack_all(acks)
        done += len(events)
        for t, counts in report.forgotten:
            erasure = next((e.id for e in events if e.tenant == t and e.type in ing.forgets), None)
            typer.echo(json.dumps({"forgot": t, "event": erasure, **counts}))

    while True:
        for msgs in adapter.batches(batch):
            good: list[tuple[Event, Callable[[], None]]] = []
            for body, ack in msgs:
                try:
                    good.append((parse(body), ack))
                except (TypeError, ValueError) as exc:  # unacknowledged: redelivered, then dead-lettered
                    errors += 1
                    typer.echo(f"failed message: {type(exc).__name__}: {exc}", err=True)
            try:
                stored([e for e, _ in good], [a for _, a in good])
            except Exception:  # noqa: BLE001 (find the message that fails: the rest is stored)
                for ev, ack in good:
                    try:
                        stored([ev], [ack])
                    except Exception as exc:  # (left for the queue to redeliver)
                        errors += 1
                        typer.echo(f"failed id={ev.id} type={ev.type} tenant={ev.tenant}: {type(exc).__name__}: {exc}", err=True)
                        if not store_up():
                            raise typer.Exit(2) from exc
        if once:
            typer.echo(f"consumed={done} failed={errors}")
            raise typer.Exit(1 if errors else 0)


@app.command()
def replay(cartridge: Path, events: Path, tenant: str = TENANT, db: str | None = DB):
    """Rebuild one tenant's subgraph from the event log (recovery path). With --db, the file is wiped for that tenant first."""
    _, _, ing, _ = _run(cartridge, None, db=db)
    report = ing.replay(tenant, events=JsonlAdapter(events))
    typer.echo(f"replayed tenant={tenant} processed={report.processed}")


@app.command()
def expire(
    tenant: str | None = typer.Argument(None, envvar="KEEL_TENANT", help="The end-customer (KEEL_TENANT when omitted).", show_default=False),
    db: str = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph to clean: a SQLite file or a mysql:// URL."),
    task: str | None = typer.Option(None, "--task", help="Close one task first: its memory expires now."),
    consolidate: Path | None = typer.Option(None, "--consolidate", help="Consolidate first, with this cartridge's `consolidate:` section, so nothing is dropped unsummarized."),
    summarizer: str | None = typer.Option(None, "--summarizer", help="With --consolidate: llm (KEEL_SUMMARIZER_* env vars), join, or pkg.module:factory."),
    all_tenants: bool = ALL,
):
    """Drop task/agent memory past its expiry for one tenant (or all). Reads already ignore expired facts; run this from cron to reclaim space."""
    store = _store(db)
    if task and all_tenants:
        raise typer.BadParameter("--task closes one tenant's task: name the tenant")
    c = load_cartridge(consolidate) if consolidate else None
    for t in _tenants(store, tenant, all_tenants):
        if c:
            _consolidate(c, store, t, summarizer)
        if task:
            typer.echo(f"closed task={task} facts={store.close_task(t, task)}")
        typer.echo(f"expired tenant={t} dropped={store.expire(t)}")


def _consolidate(c, store, tenant: str, summarizer: str | None) -> None:
    from keel.consolidate import consolidate_tenant, load_summarizer

    report = consolidate_tenant(c, store, tenant, summarizer=load_summarizer(summarizer) if summarizer else None)
    for name in report.written:
        typer.echo(f"consolidated {name}: written={report.written[name]} unchanged={report.unchanged[name]} tenant={tenant}")


@app.command(name="consolidate")
def consolidate_cmd(
    cartridge: Path,
    tenant: str | None = typer.Option(None, envvar="KEEL_TENANT", help="The end-customer to consolidate."),
    db: str = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph: a SQLite file or a mysql:// URL."),
    summarizer: str | None = typer.Option(None, "--summarizer", help="For `reduce: summary`: llm (KEEL_SUMMARIZER_* env vars), join, or pkg.module:factory."),
    all_tenants: bool = ALL,
):
    """Summarize episodic memory into durable facts, as the cartridge's `consolidate:` section declares. Idempotent; a job, never ingest."""
    c, store = load_cartridge(cartridge), _store(db)
    for t in _tenants(store, tenant, all_tenants):
        _consolidate(c, store, t, summarizer)


@app.command()
def export(
    tenant: str = TENANT,
    db: str = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph to export from: a SQLite file or a mysql:// URL."),
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
    tenant: str | None = typer.Option(None, envvar="KEEL_TENANT", help="The end-customer to index."),
    db: str = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph: a SQLite file or a mysql:// URL."),
    index: str = typer.Option(..., "--index", envvar="KEEL_INDEX", help="memory, chroma:///path, chroma://host:port, weaviate://host:port, or your adapter: pkg.module:factory?k=v"),
    embedder: str = typer.Option("openai", "--embedder", envvar="KEEL_EMBEDDER", help="openai (KEEL_EMBEDDINGS_* env vars), hash, or pkg.module:factory?k=v"),
    full: bool = typer.Option(False, "--full", help="Rebuild the tenant's index instead of updating it from the cursor."),
    cursor: Path | None = typer.Option(None, "--cursor", envvar="KEEL_INDEX_CURSOR", help="Where the watermark of the last run is kept (one tenant). Default: next to a chroma:///path index, else .keel/index-<tenant>.cursor. Keep it on a volume."),
    cursor_dir: Path | None = typer.Option(None, "--cursor-dir", envvar="KEEL_INDEX_CURSOR_DIR", help="A folder for every tenant's cursor (index-<tenant>.cursor), instead of the default place."),
    workers: int = typer.Option(4, "--workers", envvar="KEEL_INDEX_WORKERS", help="Embedding batches requested at once."),
    all_tenants: bool = typer.Option(False, "--all-tenants", help="Every tenant in the store, one after another; also deletes the documents of tenants forgotten since their last run."),
):
    """Bring the semantic index up to date for one tenant (or all), outside ingest: only subjects changed since the last run.
    One run per tenant at a time: a second one finds the lock next to the cursor and skips."""
    from keel.semantic import load_embedder, load_index

    if cursor and all_tenants:
        raise typer.BadParameter("--cursor is one tenant's file; with --all-tenants use --cursor-dir")
    c, store, vectors, emb = load_cartridge(cartridge), _store(db), load_index(index), load_embedder(embedder)
    place = lambda t: cursor or (cursor_dir / f"index-{t}.cursor" if cursor_dir else _index_cursor(index, t))
    for t in _tenants(store, tenant, all_tenants):
        _index_one(c, store, vectors, emb, t, place(t), full, workers)
    if all_tenants:  # a tenant erased by an event: its documents go, once (its cursor marks it as not yet swept)
        for t in sorted(store.forgotten_tenants()):
            if place(t).exists():
                _index_one(c, store, vectors, emb, t, place(t), full, workers)


def _index_one(c, store, vectors, emb, tenant: str, path: Path, full: bool, workers: int) -> None:
    from keel.envelope import parse_iso
    from keel.semantic import index_tenant

    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive(path.with_name(path.name + ".lock")) as mine:
        if not mine:  # two runs over one index corrupt it (Chroma's compaction): the one running covers this
            typer.echo(f"skipped tenant={tenant}: another keel index holds {path}.lock")
            return
        if store.forgotten(tenant) is not None:
            vectors.clear(tenant)
            path.unlink(missing_ok=True)
            typer.echo(f"cleared tenant={tenant}: forgotten")
            return
        if not full and not path.exists():
            typer.echo(f"no cursor at {path}: indexing the whole tenant. Keep it on a volume (--cursor, KEEL_INDEX_CURSOR) or every deploy rebuilds the index.", err=True)
        since = None if full or not path.exists() else parse_iso(path.read_text(encoding="utf-8").strip())
        report = index_tenant(store, tenant, emb, vectors, since=since, policy=c.semantic, workers=workers)
        if report.cursor is not None:
            path.write_text(report.cursor.isoformat(), encoding="utf-8")
    typer.echo(f"indexed tenant={tenant} subjects={report.indexed} removed={report.deleted} mode={'full' if since is None else 'incremental'}")


@app.command()
def forget(
    tenant: str = typer.Argument(..., help="The end-customer to erase. Never read from KEEL_TENANT: name it.", show_default=False),
    db: str = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph: a SQLite file or a mysql:// URL."),
    index: str | None = typer.Option(None, "--index", envvar="KEEL_INDEX", help="Also delete its documents from this semantic index."),
    yes: bool = typer.Option(False, "--yes", help="Required to erase: it cannot be undone."),
):
    """Erase one tenant for good: entities, relations, facts of every scope, annotations, applied event ids and, with --index,
    its semantic documents. A tombstone stays, so later events for it are skipped and writes refused. Prints what it deleted, as JSON.
    A tenant erased by an event (`forgets: tenant`) loses its semantic documents on the next `keel index --all-tenants`."""
    store = _store(db)
    if not yes:
        raise typer.BadParameter("erasing a tenant cannot be undone: add --yes")
    counts = store.forget(tenant)
    if index:
        from keel.semantic import load_index

        load_index(index).clear(tenant)
    typer.echo(json.dumps({"tenant": tenant, "forgotten_at": store.forgotten(tenant).isoformat(), **counts, "index": bool(index)}))


@app.command()
def usage(
    db: str = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph: a SQLite file or a mysql:// URL."),
    month: str | None = typer.Option(None, "--month", help="Only this month, YYYY-MM. Default: every month metered."),
    account: str | None = typer.Option(None, "--account", help="Only this account's tenants (acme/...)."),
):
    """What each tenant used, one JSON line per account, tenant and month: `events` applied for the first time (the
    billing unit; duplicates and replays are not counted) and `calls` per tool through `keel serve --transport http`."""
    import re

    from keel.usage import report

    if month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise typer.BadParameter("YYYY-MM", param_hint="--month")
    for row in report(_store(db), month, account):
        typer.echo(json.dumps(row))


keys_app = typer.Typer(help="API keys: one account per SaaS on a shared deployment (docs/design/hosted.md).", no_args_is_help=True)
app.add_typer(keys_app, name="keys")
KEYS_DB = typer.Option(..., "--db", envvar="KEEL_DB", help="The durable graph the server reads: a SQLite file or a mysql:// URL.")


def _key_row(k: Any) -> dict[str, Any]:
    return {"id": k.id, "account": k.account, "tenant": k.tenant, "agent": k.agent, "global": k.glob,
            "created_at": k.created_at.isoformat(), "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None}


@keys_app.command("create")
def keys_create(
    account: str = typer.Argument(..., help="The account: lowercase letters, digits and hyphens, at most 32. Created with its first key.", show_default=False),
    db: str = KEYS_DB,
    tenant: str | None = typer.Option(None, "--tenant", help="Only this tenant of the account."),
    agent: str | None = typer.Option(None, "--agent", help="Calls are made as this agent (the cartridge's registry applies)."),
    glob: bool = typer.Option(False, "--global", help="The account's privileged key: the only one that may annotate(scope=\"global\")."),
):
    """Create a key. Prints it once, as JSON with the record; only its hash is stored. The first key makes the server
    require a key or the secret on every call."""
    from keel.keys import create_key

    try:
        full, key = create_key(_store(db), account, tenant, agent, glob)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({"key": full, **_key_row(key)}))


@keys_app.command("list")
def keys_list(
    account: str | None = typer.Argument(None, help="Only this account's keys.", show_default=False),
    db: str = KEYS_DB,
):
    """List keys as JSON lines, revoked ones included. Never prints a key, only its id."""
    for k in _store(db).api_keys(account):
        typer.echo(json.dumps(_key_row(k)))


@keys_app.command("revoke")
def keys_revoke(key_id: str = typer.Argument(..., help="The key's id (kk_...).", show_default=False), db: str = KEYS_DB):
    """Revoke a key. Running servers stop accepting it within a few seconds."""
    from keel.keys import revoke_key

    if not revoke_key(_store(db), key_id):
        raise typer.BadParameter(f"no live key '{key_id}'")
    typer.echo(json.dumps({"id": key_id, "revoked": True}))


def _index_cursor(index: str, tenant: str) -> Path:
    """Next to a persistent Chroma (its volume), else under .keel in the working directory."""
    from urllib.parse import urlparse

    u = urlparse(index)
    if u.scheme == "chroma" and u.path not in ("", "/"):
        return Path(u.path) / f"keel-index-{tenant}.cursor"
    return Path(".keel") / f"index-{tenant}.cursor"


@contextmanager
def _exclusive(lock: Path) -> Iterator[bool]:
    """An advisory lock held for the block, without waiting: yields False when another process holds it.
    ponytail: POSIX flock; where fcntl is missing (Windows) every run proceeds."""
    try:
        import fcntl
    except ImportError:
        yield True
        return
    with lock.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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
    ui: bool = typer.Option(False, "--ui", help="http only: serve a read-only graph browser at /ui (it asks for the secret and tenant)."),
    http2: bool = typer.Option(False, "--http2", help="http only: serve HTTP/2 too, on Hypercorn (uv sync --extra http2): over TLS with --certfile, or cleartext h2c."),
    certfile: Path | None = typer.Option(None, "--certfile", envvar="KEEL_CERTFILE", help="http only: TLS certificate (PEM). With --keyfile, serve https."),
    keyfile: Path | None = typer.Option(None, "--keyfile", envvar="KEEL_KEYFILE", help="http only: TLS private key (PEM)."),
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
    from keel.keys import has_keys

    if not secret and not _loopback(host) and not allow_open and not has_keys(store):  # keys close the server too
        raise typer.BadParameter(
            f"{secret_env} is unset, no API key exists and {host} is reachable from the network: set the secret, create a key, bind 127.0.0.1, "
            "or pass --allow-open if your own proxy authenticates", param_hint="--host",
        )
    if bool(certfile) != bool(keyfile):
        raise typer.BadParameter("--certfile and --keyfile go together", param_hint="--certfile")
    try:
        if http2:
            import hypercorn  # noqa: F401
        else:
            import uvicorn
    except ImportError as exc:
        raise SystemExit(f"The HTTP transport needs the optional dependency: uv sync --extra {'http2' if http2 else 'http'}") from exc
    from keel.http import build_http_app

    if not secret:
        typer.echo(f"warning: {secret_env} is unset, the API is open on {host}", err=True)
    if tenant:
        _tools(c, store, tenant, agent)  # fail now, not on the first request, if the agent is not declared
    api = build_http_app(c, store, secret=secret, tenant=tenant, agent=agent, host=host, ui=ui, **semantic)
    if not http2:
        uvicorn.run(api, host=host, port=port, ssl_certfile=certfile, ssl_keyfile=keyfile)
        return
    import asyncio

    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config

    config = Config()  # HTTP/1.1 and HTTP/2: h2 by ALPN over TLS, h2c (prior knowledge or Upgrade) without
    config.bind = [f"[{host}]:{port}" if ":" in host else f"{host}:{port}"]
    config.certfile, config.keyfile = (str(certfile), str(keyfile)) if certfile else (None, None)
    config.accesslog = "-"
    asyncio.run(hypercorn_serve(api, config))


if __name__ == "__main__":
    app()
