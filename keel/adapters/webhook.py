"""Webhook adapter: HTTP in, envelope validated, ingested. Optional extra: `uv sync --extra webhook`.

    from keel.adapters.webhook import build_app
    app = build_app(ingestor, token="shared-secret")   # then: uvicorn yourmodule:app

POST /events with one envelope or a list of them and `Authorization: Bearer <token>`.
Unlike the pull adapters, this one pushes into the ingestor: the sender drives the loop.
"""

from __future__ import annotations

import hmac
from typing import Any

from keel.envelope import Event
from keel.ingest import Ingestor


def build_app(ingestor: Ingestor, token: str | None = None, strict: bool = False):
    """`token=None` accepts any caller; use it only on localhost."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
    except ImportError as exc:
        raise SystemExit("Webhook adapter needs the optional dependency: uv sync --extra webhook") from exc

    app = FastAPI(title=f"keel:{ingestor.cartridge.name}")

    def auth(authorization: str | None = Header(default=None)) -> None:
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "bad or missing bearer token")

    @app.post("/events", dependencies=[Depends(auth)])
    def events(body: list[Event] | Event) -> dict[str, Any]:
        batch = body if isinstance(body, list) else [body]
        report = ingestor.ingest(batch, strict=strict, batch=len(batch))
        return {"processed": report.processed, "duplicates": report.duplicates, "unhandled": report.unhandled, "errors": report.errors,
            "forgotten": [{"tenant": t, **n} for t, n in report.forgotten], "skipped": report.skipped}

    return app
