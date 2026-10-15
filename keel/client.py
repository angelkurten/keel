"""HTTP client for the five tools: the Python runtime behind `keel sdk python`, stdlib only.

    from keel.client import Client
    keel = Client("http://keel.internal:8000", tenant="acme-logistics", secret=os.environ["KEEL_SECRET"])
    keel.context("tire:T-9001")["evaluation"]

Talks to `keel serve --transport http`. A refusal (scope policy, agent registry) raises
`KeelError(status, detail)`; so does any other non-2xx answer.

One keep-alive connection per client (and thread), reused across calls: no TCP and TLS handshake
per call. It sends `User-Agent: keel-context/<version>` (some proxies, Cloudflare among them,
block urllib's); `headers=` adds or overrides any header.

`http_client=` hands the requests to your own client instead, anything with httpx's
`post(url, content=, headers=)`: HTTP/2 (`httpx.Client(http2=True)`, `pip install httpx[h2]`),
a proxy, retries, mTLS. Its timeout and connection pool are then its own.
"""

from __future__ import annotations

import http.client
import json
import threading
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from keel import __version__
from keel.envelope import Event

_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest, BrokenPipeError, ConnectionResetError)


class KeelError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"keel {status}: {detail}")
        self.status, self.detail = status, detail


def _iso(d: datetime | str | None) -> str | None:
    return d.isoformat() if isinstance(d, datetime) else d


class Client:
    def __init__(
        self, base_url: str, tenant: str, secret: str | None = None, agent: str | None = None, timeout: float = 30.0,
        traceparent: str | None = None, headers: dict[str, str] | None = None, http_client: Any = None,
    ) -> None:
        self.base_url, self.tenant, self.secret, self.agent, self.timeout = base_url.rstrip("/"), tenant, secret, agent, timeout
        self.traceparent = traceparent  # sent as the W3C header; Keel stores it with every write
        self.headers = dict(headers or {})  # added to every request, over the defaults
        self.http_client = http_client
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError(f"base_url must be http(s)://host[:port][/prefix], got {base_url!r}")
        self._scheme, self._netloc, self._prefix = url.scheme, url.netloc, url.path
        self._local = threading.local()  # http.client connections are not thread-safe: one per thread

    def _connection(self) -> http.client.HTTPConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            cls = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
            conn = self._local.conn = cls(self._netloc, timeout=self.timeout)
        return conn

    def close(self) -> None:
        """Close this thread's connection; the next call opens a new one."""
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            conn.close()

    def _post(self, path: str, body: Any) -> Any:
        headers = {"Content-Type": "application/json", "User-Agent": f"keel-context/{__version__}", "X-Keel-Tenant": self.tenant}
        if self.agent:
            headers["X-Keel-Agent"] = self.agent
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        if self.traceparent:
            headers["traceparent"] = self.traceparent
        headers.update(self.headers)
        data = json.dumps(body).encode()
        if self.http_client is not None:
            res = self.http_client.post(self.base_url + path, content=data, headers=headers)
            return self._answer(res.status_code, res.content, res.reason_phrase)
        for attempt in (1, 2):
            conn = self._connection()
            reused = conn.sock is not None
            try:
                conn.request("POST", self._prefix + path, body=data, headers=headers)
                res = conn.getresponse()
                raw = res.read()
                break
            except _STALE:
                self.close()
                if not reused or attempt == 2:
                    raise
                # ponytail: the server dropped an idle keep-alive connection before reading the
                # request; retrying once on a fresh one is what urllib3 does too.
            except Exception:
                self.close()  # a half-read response leaves the connection unusable
                raise
        if res.getheader("Connection", "").lower() == "close":
            self.close()
        return self._answer(res.status, raw, res.reason)

    @staticmethod
    def _answer(status: int, raw: bytes, reason: str) -> Any:
        if 200 <= status < 300:
            return json.loads(raw)
        try:
            detail = json.loads(raw).get("detail", raw.decode())
        except (ValueError, AttributeError):
            detail = raw.decode() or reason
        raise KeelError(status, detail if isinstance(detail, str) else json.dumps(detail))

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
