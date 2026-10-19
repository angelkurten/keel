"""HTTP transport: the five tools as a JSON API for many tenants, the ingest endpoint, and MCP
over streamable HTTP for one tenant. Optional extra: `uv sync --extra http`.

    keel serve examples/fleet --transport http --db keel.db                 # /v1/* and /mcp for any tenant (X-Keel-Tenant)
    keel serve examples/fleet --transport http --db keel.db --tenant acme   # /mcp bound to acme

    POST /v1/events    one envelope or a list        -> ingest report
    GET  /v1/events/recent                           -> the tenant's last 200 events received here, with their outcome
    POST /v1/events/check  a list of envelopes (JSON) -> what the catalog would refuse in each, nothing ingested
    GET  /v1/sdk/{language}                          -> the typed client generated from this cartridge (keel sdk)
    POST /v1/context   {subject (or a list), as_of?, depth?, exclude?, only?}  -> the context object (or a list)
    POST /v1/history   {subject, since?, until?, predicate?, scope?}
    POST /v1/search    {text (or a list), kind? (or a list), rule?, mode?, detail?, depth?, per_kind?, exclude?, as_of?, where?}
    POST /v1/annotate  {subject, predicate, value, scope?, evidence?, by?}
    POST /v1/related   {subject, relation? (or a list), as_of?, depth?}
    GET  /openapi.json, /health
    POST /mcp          MCP over streamable HTTP: bound to `tenant` when given, else per request from the key and headers
    GET  /ui           with ui=True (`keel serve --ui`): a read-only browser over the same API

Every request carries `Authorization: Bearer <secret>` (unset secret and no API keys = open:
localhost only), `X-Keel-Tenant`, and `X-Keel-Agent` when the cartridge declares agents. The
product authenticates its users and sets the two headers; Keel takes them as given (SPEC §6.1) and
builds a `Tools` per request, so nothing is shared across tenants. A refusal is a 403 with
its reason; bad input is a 400; a malformed envelope is a 422.

API keys (`keel keys create`, keel/keys.py) serve several SaaS products from one deployment: the
bearer is a key of one account, and the tenant becomes `account/<X-Keel-Tenant>` (envelopes posted to
/v1/events likewise), so a key never reaches another account. A key narrowed to a tenant or an agent
fixes that header; only a global key may annotate(scope="global"). The shared secret stays the
operator's credential and reaches every tenant unprefixed.
"""

# no `from __future__ import annotations` here: FastAPI resolves the request models defined inside
# build_http_app from real annotations, not strings.
import hashlib
import hmac
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from keel.accounts import Quotas
from keel.cartridge import Cartridge
from keel.envelope import Event, parse_iso, utcnow
from keel.ingest import Ingestor
from keel.keys import check_tenant, has_keys, resolve
from keel.semantic import Unavailable
from keel.store.base import Store
from keel.tools import Tools
from keel.usage import Meter

OPEN_PATHS = {"/health", "/openapi.json", "/docs", "/redoc", "/ui"}  # /ui holds no data; its calls carry the secret


class _Auth:
    """ASGI middleware for every path but the open ones: the shared secret (the operator), or an API key
    (one account), whose record goes to the endpoints in `scope["state"]["key"]`. With neither a secret
    nor any key the server is open, as before keys existed. Raw ASGI so that MCP's streaming responses
    pass through untouched. `/mcp` is bound to one tenant: a key reaches it only if that tenant is its own."""

    def __init__(self, app: Any, secret: str | None, store: Store, mcp_tenant: str | None, mcp_agent: str | None, quotas: Any = None,
                 account: str | None = None) -> None:
        self.app, self.secret, self.store, self.quotas, self.account = app, secret, store, quotas, account
        self.mcp_tenant, self.mcp_agent = mcp_tenant, mcp_agent
        self._keyed: tuple[float, bool] = (-1e9, False)

    def keyed(self) -> bool:
        """Whether any key exists, re-read every few seconds: the first `keel keys create` closes an open server."""
        if time.monotonic() - self._keyed[0] > 5:
            self._keyed = (time.monotonic(), has_keys(self.store))
        return self._keyed[1]

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] not in OPEN_PATHS:
            from starlette.responses import JSONResponse

            given = dict(scope["headers"]).get(b"authorization", b"").decode()
            token = given.removeprefix("Bearer ") if given.startswith("Bearer ") else ""
            key = None
            if self.secret and hmac.compare_digest(given, f"Bearer {self.secret}"):
                pass  # the operator
            elif token.startswith("kk_") and (key := resolve(self.store, token)) is not None:
                if self.account is not None and key.account != self.account:  # one container per account (hosted)
                    await JSONResponse({"detail": f"this server serves account '{self.account}' only"}, status_code=403)(scope, receive, send)
                    return
                if scope["path"].startswith("/mcp") and self.mcp_tenant is not None and not self._binds(key):
                    await JSONResponse({"detail": "this key does not reach the tenant /mcp is bound to"}, status_code=403)(scope, receive, send)
                    return
                if self.quotas and (wait := self.quotas.request(key.account)) is not None:
                    await JSONResponse({"detail": f"account '{key.account}' is over its requests per second"}, status_code=429,
                                       headers={"Retry-After": str(max(1, round(wait)))})(scope, receive, send)
                    return
                scope.setdefault("state", {})["key"] = key
            elif self.secret or self.keyed():
                await JSONResponse({"detail": "bad or missing bearer token"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _binds(self, key: Any) -> bool:
        account, _, tenant = (self.mcp_tenant or "").partition("/")
        return (account == key.account and bool(tenant) and key.tenant in (None, tenant)
                and key.agent in (None, self.mcp_agent))


def build_http_app(
    cartridge: Cartridge,
    store: Store,
    secret: str | None = None,
    tenant: str | None = None,
    agent: str | None = None,
    host: str = "127.0.0.1",
    embedder: Any = None,
    index: Any = None,
    reranker: Any = None,
    ui: bool = False,
    account: str | None = None,
) -> Any:
    """An ASGI app. With `tenant`, `/mcp` is also served, bound to that tenant (and `agent`). With `account`
    (hosted: one container per account), only that account's keys are accepted and its graph lives in the
    account's own database (`keel accounts set --store`); keys, limits and usage stay in `store`."""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from pydantic import BaseModel
    except ImportError as exc:
        raise SystemExit("The HTTP transport needs the optional dependency: uv sync --extra http") from exc

    meter = Meter(store)  # tool calls per tenant and day (keel/usage.py), flushed every few seconds and at shutdown
    quotas = Quotas(store)  # per account (keel/accounts.py), for requests made with an API key
    server = None
    mcp_app = None
    from importlib.util import find_spec

    if tenant is not None or find_spec("mcp") is not None:  # the http extra brings mcp; a bare FastAPI install serves /v1 only
        from keel.mcp_server import build_server

        def per_request(request: Any) -> Tools:
            """Without a fixed tenant, /mcp builds its tools per call from the request, exactly like /v1."""
            if request is None:
                raise PermissionError("no HTTP request to take the tenant from")
            try:
                return tools_for(request)
            except HTTPException as exc:
                raise PermissionError(exc.detail) from exc

        bound = Tools(cartridge, store, tenant, agent, embedder=embedder, index=index, reranker=reranker, meter=meter) if tenant is not None else per_request
        server = build_server(bound, name=f"keel:{cartridge.name}")
        kw: dict[str, Any] = {"streamable_http_path": "/mcp", "json_response": True, "host": host}
        if host not in ("127.0.0.1", "localhost"):
            try:  # behind a proxy the Host header is the public one; the shared secret is the guard
                from mcp.server.transport_security import TransportSecuritySettings

                kw["transport_security"] = TransportSecuritySettings(enable_dns_rebinding_protection=False)
            except ImportError:  # pragma: no cover
                pass
        mcp_app = server.streamable_http_app(**kw)

    @asynccontextmanager
    async def lifespan(_app: Any):
        try:
            if server is None:
                yield
            else:
                async with server.session_manager.run():
                    yield
        finally:
            meter.flush()

    app = FastAPI(title=f"keel:{cartridge.name}", version="1", lifespan=lifespan)
    ingestor = Ingestor(cartridge, store)
    own: dict[str, tuple[Store, Ingestor]] = {}  # an account's own database (keel accounts set --store), opened once
    own_lock = threading.Lock()

    def graph_for(key: Any) -> tuple[Store, Ingestor]:
        """The store and ingestor a request works on: its account's own database, or the shared one. Usage is
        always counted in the shared store, where `keel usage` reads it."""
        name = key.account if key is not None else account  # a server bound to one account works on its graph
        acct = quotas.account(name) if name else None
        if acct is None or not acct.store:
            return store, ingestor
        with own_lock:
            if acct.store not in own:
                from keel.store import open_store

                s = open_store(acct.store)
                own[acct.store] = (s, Ingestor(cartridge, s, meter=getattr(store, "meter", None)))
            return own[acct.store]

    def key_of(request: Request) -> Any:
        return request.scope.get("state", {}).get("key")

    def scoped(key: Any, t: str | None) -> str:
        """The tenant a key's request is about: its own tenant, or the header's, under its account."""
        t = t or key.tenant
        if not t:
            raise HTTPException(400, "X-Keel-Tenant header required")
        if key.tenant is not None and t != key.tenant:
            raise HTTPException(403, f"this key reaches only tenant '{key.tenant}'")
        try:
            return f"{key.account}/{check_tenant(t)}"
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    def tools_for(request: Request) -> Tools:
        key, who = key_of(request), request.headers.get("x-keel-agent")
        if key is not None:
            t = scoped(key, request.headers.get("x-keel-tenant"))
            if key.agent is not None and who not in (None, key.agent):
                raise HTTPException(403, f"this key calls as agent '{key.agent}'")
            who = key.agent or who
        else:
            t = request.headers.get("x-keel-tenant") or tenant
            who = who or agent
            if not t:
                raise HTTPException(400, "X-Keel-Tenant header required")
        parts = [request.headers.get("traceparent")] + ([f"tool_call_id={request.headers['x-keel-tool-call']}"] if request.headers.get("x-keel-tool-call") else [])
        trace = "; ".join(p for p in parts if p) or None
        try:
            return Tools(cartridge, graph_for(key)[0], t, who, trace=trace, embedder=embedder, index=index, reranker=reranker, meter=meter)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    def run(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Unavailable as exc:  # the embeddings provider, after its retries: the caller may retry
            raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc

    class ContextIn(BaseModel):
        subject: str | list[str]
        as_of: str | None = None
        depth: int = 0
        as_known_at: str | None = None
        task: str | None = None
        detail: str = "full"
        exclude: list[str] | None = None
        only: list[str] | None = None

    class HistoryIn(BaseModel):
        subject: str | None = None
        since: str | None = None
        until: str | None = None
        predicate: str | None = None
        scope: str | None = None
        as_known_at: str | None = None
        task: str | None = None
        changed_since: str | None = None
        limit: int | None = None

    class SearchIn(BaseModel):
        text: str | list[str] = ""
        kind: str | list[str] | None = None
        rule: str | None = None
        limit: int | None = None
        after: str | None = None
        mode: str = "text"
        min_score: float | None = None
        related_to: str | None = None
        via: str | None = None
        detail: str = "entity"
        depth: int = 0
        per_kind: int | None = None
        exclude: list[str] | None = None
        as_of: str | None = None
        where: dict[str, Any] | None = None

    class AnnotateIn(BaseModel):
        subject: str
        predicate: str
        value: Any
        scope: str = "task"
        evidence: str | None = None
        by: str | None = None
        task: str | None = None
        key: str | None = None
        status: str | None = None

    class RelatedIn(BaseModel):
        subject: str
        relation: str | list[str] | None = None
        as_of: str | None = None
        depth: int = 1

    def dt(s: str | None):
        return parse_iso(s) if s else None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "cartridge": cartridge.name, "mcp": mcp_app is not None}

    recent: dict[str, deque[dict[str, Any]]] = {}  # tenant -> its last events received here; memory only, gone on restart

    def remember(batch: list[Event], report: Any) -> None:
        at = utcnow().isoformat()
        for i, e in enumerate(batch):
            why = report.not_applied.get(i)
            status = "applied" if why is None else why.split(":")[0]
            recent.setdefault(e.tenant, deque(maxlen=200)).appendleft(
                {"id": e.id, "type": e.type, "subject": e.subject, "occurred_at": e.occurred_at.isoformat(), "received_at": at,
                 "source": e.source, "status": status, "reason": why if why and why != status else None})

    def tenant_of(request: Request) -> str:
        key = key_of(request)
        if key is not None:
            return scoped(key, request.headers.get("x-keel-tenant"))
        t = request.headers.get("x-keel-tenant") or tenant
        if not t:
            raise HTTPException(400, "X-Keel-Tenant header required")
        return t

    @app.get("/v1/events/recent")
    def events_recent(request: Request) -> list[dict[str, Any]]:
        return list(recent.get(tenant_of(request), ()))

    @app.post("/v1/events/check")
    def events_check(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from pydantic import ValidationError

        out = []
        for i, raw in enumerate(body):
            try:
                problems = cartridge.check_event(Event.model_validate(raw))
            except ValidationError as exc:
                problems = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in exc.errors()]
            if problems:
                out.append({"index": i, "id": raw.get("id"), "type": raw.get("type"), "problems": problems})
        return out

    @app.get("/v1/sdk/{language}")
    def sdk(language: str) -> dict[str, Any]:
        from keel.sdk import TEMPLATES, generate

        if language not in TEMPLATES:
            raise HTTPException(400, f"no SDK for '{language}': {', '.join(TEMPLATES)}")
        code = generate(language, cartridge)
        name = cartridge.name
        filename = {"typescript": f"keel-{name}.ts", "python": f"keel_{name}.py", "go": f"keel_{name}.go", "php": f"{name.title()}.php"}[language]  # as docs/sdks.md names them
        return {"language": language, "cartridge": name, "filename": filename, "code": code, "sha256": hashlib.sha256(code.encode()).hexdigest()}

    @app.post("/v1/events")
    def events(body: list[Event] | Event, request: Request) -> dict[str, Any]:
        batch = body if isinstance(body, list) else [body]
        t = request.headers.get("x-keel-tenant")
        if t and any(e.tenant != t for e in batch):
            raise HTTPException(400, f"every envelope must belong to tenant '{t}'")
        key = key_of(request)
        if key is not None:  # the account's own tenants, prefixed like every read
            batch = [e.model_copy(update={"tenant": scoped(key, e.tenant)}) for e in batch]
            if (wait := quotas.events(key.account, len(batch))) is not None:
                raise HTTPException(429, f"account '{key.account}' has used its events for this month", headers={"Retry-After": str(int(wait))})
        report = graph_for(key)[1].ingest(batch, batch=len(batch))
        remember(batch, report)
        return {"processed": report.processed, "duplicates": report.duplicates, "unhandled": report.unhandled, "errors": report.errors,
                "forgotten": [{"tenant": t.removeprefix(f"{key.account}/") if key else t, **n} for t, n in report.forgotten], "skipped": report.skipped,
                "retained": [{"tenant": t.removeprefix(f"{key.account}/") if key else t, "before": b, **n} for t, b, n in report.retained]}

    @app.post("/v1/context")
    def context(body: ContextIn, request: Request) -> Any:
        return run(lambda: tools_for(request).context(body.subject, dt(body.as_of), body.depth, dt(body.as_known_at), body.task, body.detail, body.exclude, body.only))

    @app.post("/v1/history")
    def history(body: HistoryIn, request: Request) -> list[dict[str, Any]]:
        return run(lambda: tools_for(request).history(body.subject, dt(body.since), dt(body.until), body.predicate, body.scope, dt(body.as_known_at), body.task, dt(body.changed_since), body.limit))

    @app.post("/v1/search")
    def search(body: SearchIn, request: Request) -> list[Any]:
        return run(lambda: tools_for(request).search(
            body.text, body.kind, body.rule, body.limit, body.after, body.mode, body.min_score, body.related_to, body.via,
            body.detail, body.depth, body.per_kind, body.exclude, dt(body.as_of), body.where,
        ))

    @app.post("/v1/annotate")
    def annotate(body: AnnotateIn, request: Request) -> dict[str, Any]:
        if body.scope == "global" and (key := key_of(request)) is not None and not key.glob:
            raise HTTPException(403, "only the account's global key may write global memory (keel keys create --global)")
        return run(lambda: tools_for(request).annotate(body.subject, body.predicate, body.value, body.scope, body.evidence, body.by, body.task, body.key, body.status))

    @app.post("/v1/related")
    def related(body: RelatedIn, request: Request) -> list[dict[str, Any]]:
        return run(lambda: tools_for(request).related(body.subject, body.relation, dt(body.as_of), body.depth))

    if ui:
        from pathlib import Path

        from fastapi.responses import HTMLResponse

        page = (Path(__file__).parent / "ui.html").read_text(encoding="utf-8")

        @app.get("/ui", include_in_schema=False)
        def ui_page() -> HTMLResponse:
            return HTMLResponse(page)

    if mcp_app is not None:
        app.mount("/", mcp_app)
    return _Auth(app, secret, store, tenant, agent, quotas, account)
