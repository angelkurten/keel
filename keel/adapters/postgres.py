"""Table + polling adapter: your SaaS writes envelopes to an outbox table, Keel polls it.

Optional extra for the driver: `uv sync --extra postgres` (psycopg). Any DB-API 2.0
connection works; the tests run it on sqlite3.

Expected table (names are configurable):

    CREATE TABLE keel_events (
        seq          bigserial PRIMARY KEY,   -- the cursor: assigned at insert, read in order
        id           text NOT NULL,
        tenant       text NOT NULL,
        type         text NOT NULL,
        subject      text NOT NULL,
        occurred_at  timestamptz NOT NULL,
        recorded_at  timestamptz NOT NULL DEFAULT now(),
        source       text NOT NULL,
        payload      jsonb NOT NULL DEFAULT '{}'
    );

Usage:

    import psycopg
    from keel.adapters.postgres import PostgresTableAdapter

    adapter = PostgresTableAdapter(lambda: psycopg.connect(DSN), cursor_path=".keel/cursor")
    ingestor.ingest(adapter)                   # one pass over everything after the cursor
    ingestor.ingest(adapter.follow(interval=5))  # or poll forever

The cursor is a text file holding the last `seq` the consumer took, so a restart
continues where it stopped. Delivery is at-least-once: the cursor advances after
the consumer has taken an event, so a crash mid-event re-delivers it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from keel.envelope import Event

Connect = Callable[[], Any]  # returns a DB-API 2.0 connection


class PostgresTableAdapter:
    def __init__(
        self,
        connect: Connect,
        table: str = "keel_events",
        cursor_path: str | Path = ".keel/cursor",
        cursor_column: str = "seq",
        batch_size: int = 500,
        placeholder: str = "%s",  # psycopg uses %s; sqlite3 uses ?
    ) -> None:
        self.connect = connect
        self.cursor_path = Path(cursor_path)
        self.cursor_column = cursor_column
        self.batch_size = batch_size
        # ponytail: a serial cursor can skip a row that commits late under concurrent
        # writers; use one writer per table, or a recorded_at cursor with a lag, if that bites.
        self.sql = (
            f"SELECT * FROM {table} WHERE {cursor_column} > {placeholder} "
            f"ORDER BY {cursor_column} LIMIT {placeholder}"
        )

    @property
    def position(self) -> int:
        try:
            return int(self.cursor_path.read_text())
        except FileNotFoundError:
            return 0

    def _save(self, seq: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(seq))

    def __iter__(self) -> Iterator[Event]:
        """One pass: every row after the cursor, in order, then stop."""
        conn = self.connect()
        try:
            while True:
                cur = conn.cursor()
                cur.execute(self.sql, (self.position, self.batch_size))
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                for row in rows:
                    rec = dict(zip(cols, row))
                    seq = rec.pop(self.cursor_column)
                    if isinstance(rec.get("payload"), str):
                        rec["payload"] = json.loads(rec["payload"])
                    yield Event.model_validate(rec)
                    self._save(seq)  # runs once the consumer asks for the next event
                if len(rows) < self.batch_size:
                    return
        finally:
            conn.close()

    def follow(self, interval: float = 5.0) -> Iterator[Event]:
        """Poll forever. `ingestor.ingest(adapter.follow())` never returns."""
        while True:
            n = 0
            for ev in self:
                n += 1
                yield ev
            if n == 0:
                time.sleep(interval)
