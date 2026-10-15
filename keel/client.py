"""HTTP client for the five tools: the Python runtime behind `keel sdk python`, stdlib only.

    from keel.client import Client
    keel = Client("http://keel.internal:8000", tenant="acme-logistics", secret=os.environ["KEEL_SECRET"])
    keel.context("tire:T-9001")["evaluation"]

Talks to `keel serve --transport http`. A refusal (scope policy, agent registry) raises
`KeelError(status, detail)`; so does any other non-2xx answer.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from keel.envelope import Event


class KeelError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"keel {status}: {detail}")
        self.status, self.detail = status, detail


def _iso(d: datetime | str | None) -> str | None:
    return d.isoformat() if isinstance(d, datetime) else d


class Client:
    def __init__(self, base_url: str, tenant: str, secret: str | None = None, agent: str | None = None, timeout: float = 30.0, traceparent: str | None = None) -> None:
        self.base_url, self.tenant, self.secret, self.agent, self.timeout = base_url.rstrip("/"), tenant, secret, agent, timeout
        self.traceparent = traceparent  # sent as the W3C header; Keel stores it with every write

    def _post(self, path: str, body: Any) -> Any:
        headers = {"Content-Type": "application/json", "X-Keel-Tenant": self.tenant}
        if self.agent:
            headers["X-Keel-Agent"] = self.agent
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if self.traceparent:
            headers["traceparent"] = self.traceparent
        req = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw).get("detail", raw.decode())
            except ValueError:
                detail = raw.decode() or exc.reason
            raise KeelError(exc.code, detail if isinstance(detail, str) else json.dumps(detail)) from exc

    def context(
        self, subject: str, as_of: datetime | str | None = None, depth: int = 0,
        as_known_at: datetime | str | None = None, task: str | None = None, detail: str = "full",
    ) -> dict[str, Any]:
        """What is true about a subject now, or at `as_of`. `depth=1` adds each neighbour; `as_known_at`
        answers what Keel knew then; `task` keeps one task's memory. `watermark` is the changes cursor."""
        return self._post("/v1/context", {"subject": subject, "as_of": _iso(as_of), "depth": depth, "as_known_at": _iso(as_known_at), "task": task, "detail": detail})

    def history(
        self, subject: str | None = None, since: datetime | str | None = None, until: datetime | str | None = None,
        predicate: str | None = None, scope: str | None = None, as_known_at: datetime | str | None = None,
        task: str | None = None, changed_since: datetime | str | None = None, limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Facts over time. Without a subject, `changed_since` (a watermark) lists the tenant's changes."""
        return self._post("/v1/history", {
            "subject": subject, "since": _iso(since), "until": _iso(until), "predicate": predicate, "scope": scope,
            "as_known_at": _iso(as_known_at), "task": task, "changed_since": _iso(changed_since), "limit": limit,
        })

    def search(self, text: str = "", kind: str | None = None, rule: str | None = None, limit: int | None = None, after: str | None = None, mode: str = "text", min_score: float | None = None, related_to: str | None = None, via: str | None = None) -> list[dict[str, Any]]:
        """Text search; with `kind` or `rule`, the entities of a kind or where a rule fires, paged by `limit`/`after`.
        `mode="semantic"`: subjects ranked by meaning, each with a `score` (the server needs --index)."""
        return self._post("/v1/search", {"text": text, "kind": kind, "rule": rule, "limit": limit, "after": after, "mode": mode, "min_score": min_score, "related_to": related_to, "via": via})

    def annotate(
        self, subject: str, predicate: str, value: Any, scope: str = "task", evidence: str | None = None,
        by: str | None = None, task: str | None = None, key: str | None = None, status: str | None = None,
    ) -> dict[str, Any]:
        """Write memory. `task` ties it to a job; `key` makes a retried write return the first one."""
        return self._post("/v1/annotate", {"subject": subject, "predicate": predicate, "value": value, "scope": scope, "evidence": evidence, "by": by, "task": task, "key": key, "status": status})

    def related(self, subject: str, relation: str | None = None, as_of: datetime | str | None = None) -> list[dict[str, Any]]:
        return self._post("/v1/related", {"subject": subject, "relation": relation, "as_of": _iso(as_of)})

    def events(self, batch: Event | dict[str, Any] | list[Event | dict[str, Any]]) -> dict[str, Any]:
        items = batch if isinstance(batch, list) else [batch]
        return self._post("/v1/events", [e.model_dump(mode="json") if isinstance(e, Event) else e for e in items])
