"""HTTP transport: the five tools as a JSON API for many tenants, the ingest endpoint, and MCP
over streamable HTTP for one tenant. Optional extra: `uv sync --extra http`.

    keel serve cartridges/fleet --transport http --db keel.db                 # /v1/* for any tenant
    keel serve cartridges/fleet --transport http --db keel.db --tenant acme   # plus /mcp bound to acme

    POST /v1/events    one envelope or a list        -> ingest report
    POST /v1/context   {subject, as_of?, depth?}     -> the context object
    POST /v1/history   {subject, since?, until?, predicate?, scope?}
    POST /v1/search    {text}
    POST /v1/annotate  {subject, predicate, value, scope?, evidence?, by?}
    POST /v1/related   {subject, relation?, as_of?}
    GET  /openapi.json, /health

Every request carries `Authorization: Bearer <secret>` (unset secret = open: localhost only),
`X-Keel-Tenant`, and `X-Keel-Agent` when the cartridge declares agents. The product
authenticates its users and sets the two headers; Keel takes them as given (SPEC §6.1) and
builds a `Tools` per request, so nothing is shared across tenants. A refusal is a 403 with
its reason; bad input is a 400; a malformed envelope is a 422.
"""

# no `from __future__ import annotations` here: FastAPI resolves the request models defined inside
# build_http_app from real annotations, not strings.
import hmac
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from keel.cartridge import Cartridge
from keel.envelope import Event, parse_iso
from keel.ingest import Ingestor
from keel.store.base import Store
from keel.tools import Tools

OPEN_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}


class _Bearer:
    """ASGI middleware: one shared secret for every path but the open ones. Raw ASGI so that MCP's
    streaming responses pass through untouched."""

    def __init__(self, app: Any, secret: str | None) -> None:
        self.app, self.secret = app, secret

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and self.secret and scope["path"] not in OPEN_PATHS:
            given = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not hmac.compare_digest(given, f"Bearer {self.secret}"):
                from starlette.responses import JSONResponse

                await JSONResponse({"detail": "bad or missing bearer token"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app(
    cartridge: Cartridge,
    store: Store,
    secret: str | None = None,
    tenant: str | None = None,
    agent: str | None = None,
    host: str = "127.0.0.1",
) -> Any:
    """An ASGI app. With `tenant`, `/mcp` is also served, bound to that tenant (and `agent`)."""
    try:
        from fastapi import FastAPI, HTTPException, Request
        from pydantic import BaseModel
    except ImportError as exc:
        raise SystemExit("The HTTP transport needs the optional dependency: uv sync --extra http") from exc

    lifespan = None
    mcp_app = None
    if tenant is not None:
        from keel.mcp_server import build_server

        server = build_server(Tools(cartridge, store, tenant, agent))
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
            async with server.session_manager.run():
                yield

    app = FastAPI(title=f"keel:{cartridge.name}", version="1", lifespan=lifespan)
    ingestor = Ingestor(cartridge, store)

    def tools_for(request: Request) -> Tools:
        t = request.headers.get("x-keel-tenant") or tenant
        if not t:
            raise HTTPException(400, "X-Keel-Tenant header required")
        try:
            return Tools(cartridge, store, t, request.headers.get("x-keel-agent") or agent)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    def run(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    class ContextIn(BaseModel):
        subject: str
        as_of: str | None = None
        depth: int = 0

    class HistoryIn(BaseModel):
        subject: str
        since: str | None = None
        until: str | None = None
        predicate: str | None = None
        scope: str | None = None

    class SearchIn(BaseModel):
        text: str

    class AnnotateIn(BaseModel):
        subject: str
        predicate: str
        value: Any
        scope: str = "task"
        evidence: str | None = None
        by: str | None = None

    class RelatedIn(BaseModel):
        subject: str
        relation: str | None = None
        as_of: str | None = None

    def dt(s: str | None):
        return parse_iso(s) if s else None

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "cartridge": cartridge.name, "mcp": mcp_app is not None}

    @app.post("/v1/events")
    def events(body: list[Event] | Event, request: Request) -> dict[str, Any]:
        batch = body if isinstance(body, list) else [body]
        t = request.headers.get("x-keel-tenant")
        if t and any(e.tenant != t for e in batch):
            raise HTTPException(400, f"every envelope must belong to tenant '{t}'")
        report = ingestor.ingest(batch)
        return {"processed": report.processed, "duplicates": report.duplicates, "unhandled": report.unhandled, "errors": report.errors}

    @app.post("/v1/context")
    def context(body: ContextIn, request: Request) -> dict[str, Any]:
        return run(lambda: tools_for(request).context(body.subject, dt(body.as_of), body.depth))

    @app.post("/v1/history")
    def history(body: HistoryIn, request: Request) -> list[dict[str, Any]]:
        return run(lambda: tools_for(request).history(body.subject, dt(body.since), dt(body.until), body.predicate, body.scope))

    @app.post("/v1/search")
    def search(body: SearchIn, request: Request) -> list[dict[str, Any]]:
        return run(lambda: tools_for(request).search(body.text))

    @app.post("/v1/annotate")
    def annotate(body: AnnotateIn, request: Request) -> dict[str, Any]:
        return run(lambda: tools_for(request).annotate(body.subject, body.predicate, body.value, body.scope, body.evidence, body.by))

    @app.post("/v1/related")
    def related(body: RelatedIn, request: Request) -> list[dict[str, Any]]:
        return run(lambda: tools_for(request).related(body.subject, body.relation, dt(body.as_of)))

    if mcp_app is not None:
        app.mount("/", mcp_app)
    return _Bearer(app, secret)
