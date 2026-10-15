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

The cursor is a text file holding the last `seq` stored, so a restart continues where
it stopped. Delivery is at-least-once. Driven by `Ingestor.ingest`, the cursor moves only
after each commit (`ack()`), so a batch that rolls back is read again whole; iterated by
hand, it moves once you ask for the next event, or call `ack()` yourself after storing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from keel.adapters import Following
from keel.envelope import Event

Connect = Callable[[], Any]  # returns a DB-API 2.0 connection


class PostgresTableAdapter:
    manual_ack = False  # Ingestor.ingest sets it: the cursor then moves only on ack()

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
        self._taken: int | None = None
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

    def ack(self) -> None:
        """Everything taken so far is stored: move the cursor past it."""
        if self._taken is not None:
            self._save(self._taken)

    def _pass(self, after: int) -> Iterator[tuple[int, Event]]:
        """Every row after `after`, in order, then stop."""
        conn = self.connect()
        try:
            while True:
                cur = conn.cursor()
                cur.execute(self.sql, (after, self.batch_size))
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                for row in rows:
                    rec = dict(zip(cols, row))
                    after = rec.pop(self.cursor_column)
                    if isinstance(rec.get("payload"), str):
                        rec["payload"] = json.loads(rec["payload"])
                    yield after, Event.model_validate(rec)
                if len(rows) < self.batch_size:
                    return
        finally:
            conn.close()

    def _take(self, pairs: Iterator[tuple[int, Event]]) -> Iterator[Event]:
        for seq, event in pairs:
            self._taken = seq  # before the yield: ack() right after a commit covers this event
            yield event
            if not self.manual_ack:
                self._save(seq)  # runs once the consumer asks for the next event

    def __iter__(self) -> Iterator[Event]:
        """One pass: every row after the cursor, in order, then stop."""
        return self._take(self._pass(self.position))

    def follow(self, interval: float = 5.0) -> Following:
        """Poll forever. `ingestor.ingest(adapter.follow())` never returns."""

        def passes() -> Iterator[tuple[int, Event]]:
            after = self.position
            while True:
                n = 0
                for seq, event in self._pass(after):  # from what was taken, not what was acked
                    n, after = n + 1, seq
                    yield seq, event
                if n == 0:
                    time.sleep(interval)

        return Following(self, self._take(passes()))
