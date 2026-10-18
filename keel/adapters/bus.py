"""Bus adapters: Kafka and SQS. Your SaaS publishes envelopes as JSON messages; Keel consumes them.

Optional extras for the clients: `uv sync --extra kafka` (confluent-kafka) or `--extra sqs`
(boto3). Each adapter needs two calls from its client, so the tests run on fakes and any
other client can be wrapped the same way.

    from keel.adapters.bus import KafkaAdapter, SqsAdapter

    adapter = KafkaAdapter.connect("broker:9092", topic="keel-events", group_id="keel")
    adapter = SqsAdapter.connect("https://sqs.us-east-1.amazonaws.com/123456789012/keel-events")
    ingestor.ingest(adapter)            # drain what is there now, then stop
    ingestor.ingest(adapter.follow())   # consume forever

Delivery is at-least-once: a message is acknowledged (offset committed, SQS message deleted)
only after it is stored. Driven by `Ingestor.ingest`, that is after each commit (`ack()`), so a
batch that rolls back is redelivered whole; iterated by hand, once you ask for the next event,
or when you call `ack()` yourself. A message
that is not a valid envelope raises and stays unacknowledged, unless you pass `dead_letter`:
it receives (body, error), and the adapter acknowledges the message and moves on.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

from keel.adapters import Following
from keel.envelope import Event

Ack = Callable[[], None]
DeadLetter = Callable[[bytes | str, Exception], None]


def parse(body: bytes | str) -> Event:
    """A message body as an envelope; TypeError or ValueError when it is not one."""
    return Event.model_validate(json.loads(body))


class BusAdapter:
    """The shared loop. A subclass fetches raw messages as (body, ack) pairs."""

    manual_ack = False  # Ingestor.ingest sets it: messages are then acknowledged only on ack()

    def __init__(self, dead_letter: DeadLetter | None = None) -> None:
        self.dead_letter = dead_letter
        self._pending: list[Ack] = []

    def ack(self) -> None:
        """Everything taken so far is stored: acknowledge it."""
        pending, self._pending = self._pending, []
        self.ack_all(pending)

    def ack_all(self, acks: list[Ack]) -> None:
        """Acknowledge these messages (a bus with a batch call overrides it)."""
        for ack in acks:
            ack()

    def nack(self) -> None:
        """What was taken since the last ack() was not stored: leave it unacknowledged, so the bus redelivers
        it (SQS after its visibility timeout, and to the queue's dead-letter queue after maxReceiveCount)."""
        self._pending = []

    def _fetch(self, wait: bool = True) -> list[tuple[bytes | str, Ack]]:  # pragma: no cover - abstract
        """Raw messages; `wait=False` returns at once with what is already there."""
        raise NotImplementedError

    def batches(self, size: int = 100) -> Iterator[list[tuple[bytes | str, Ack]]]:
        """One pass in batches of raw (body, ack) messages, for a caller that stores each batch in one
        transaction and acknowledges each message itself: one waiting fetch, topped up with what is already
        there until about `size` (SQS returns at most 10 per call). Stops when the bus has nothing."""
        while batch := self._fetch():
            while len(batch) < size and (more := self._fetch(wait=False)):
                batch += more
            yield batch

    def __iter__(self) -> Iterator[Event]:
        """One pass: consume until the bus has nothing more right now, then stop."""
        while True:
            batch = self._fetch()
            if not batch:
                return
            for body, ack in batch:
                try:
                    event = parse(body)
                except (TypeError, ValueError) as exc:  # not JSON, not an object, not an envelope
                    if self.dead_letter is None:
                        raise
                    self.dead_letter(body, exc)
                    self._pending.append(ack)
                    if not self.manual_ack:
                        self.ack()
                    continue
                self._pending.append(ack)  # before the yield: ack() right after a commit covers it
                yield event
                if not self.manual_ack:
                    self.ack()  # runs once the consumer asks for the next event: the handler already ran

    def follow(self, interval: float = 1.0) -> Following:
        """Consume forever. `ingestor.ingest(adapter.follow())` never returns."""

        def forever() -> Iterator[Event]:
            while True:
                n = 0
                for ev in self:
                    n += 1
                    yield ev
                if n == 0:
                    time.sleep(interval)

        return Following(self, forever())


class KafkaAdapter(BusAdapter):
    """A consumer with `poll(timeout) -> message | None` and `commit(message=, asynchronous=)`,
    the confluent-kafka shape; `message.value()` is the JSON envelope. Auto-commit must be off."""

    def __init__(self, consumer: Any, poll_timeout: float = 1.0, dead_letter: DeadLetter | None = None) -> None:
        super().__init__(dead_letter)
        self.consumer = consumer
        self.poll_timeout = poll_timeout

    @classmethod
    def connect(
        cls, bootstrap_servers: str, topic: str, group_id: str = "keel", *, poll_timeout: float = 1.0,
        dead_letter: DeadLetter | None = None, **config: Any,
    ) -> KafkaAdapter:
        """`config` is passed to the confluent-kafka Consumer (librdkafka keys, e.g. `**{"security.protocol": "SASL_SSL"}`)."""
        try:
            from confluent_kafka import Consumer
        except ImportError as exc:
            raise SystemExit("KafkaAdapter needs the optional dependency: uv sync --extra kafka") from exc
        consumer = Consumer(
            {"bootstrap.servers": bootstrap_servers, "group.id": group_id, "enable.auto.commit": False, "auto.offset.reset": "earliest", **config}
        )
        consumer.subscribe([topic])
        return cls(consumer, poll_timeout=poll_timeout, dead_letter=dead_letter)

    def _fetch(self, wait: bool = True) -> list[tuple[bytes | str, Ack]]:
        msg = self.consumer.poll(self.poll_timeout if wait else 0)
        if msg is None:
            return []
        if msg.error():
            raise RuntimeError(f"kafka: {msg.error()}")
        return [(msg.value(), lambda m=msg: self.consumer.commit(message=m, asynchronous=False))]


class SqsAdapter(BusAdapter):
    """A client with `receive_message(**kw)` and `delete_message(**kw)`, the boto3 shape. The message
    body is the JSON envelope; if SNS fans out into the queue, enable raw message delivery."""

    def __init__(
        self, client: Any, queue_url: str, batch_size: int = 10, wait_seconds: int = 10, dead_letter: DeadLetter | None = None
    ) -> None:
        super().__init__(dead_letter)
        self.client = client
        self.queue_url = queue_url
        self.batch_size = batch_size
        self.wait_seconds = wait_seconds

    @classmethod
    def connect(
        cls, queue_url: str, *, batch_size: int = 10, wait_seconds: int = 10, dead_letter: DeadLetter | None = None,
        **boto3_kwargs: Any,
    ) -> SqsAdapter:
        """`boto3_kwargs` go to `boto3.client("sqs", ...)` (region_name, endpoint_url, credentials)."""
        try:
            import boto3
        except ImportError as exc:
            raise SystemExit("SqsAdapter needs the optional dependency: uv sync --extra sqs") from exc
        return cls(boto3.client("sqs", **boto3_kwargs), queue_url, batch_size=batch_size, wait_seconds=wait_seconds, dead_letter=dead_letter)

    def _fetch(self, wait: bool = True) -> list[tuple[bytes | str, Ack]]:
        res = self.client.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=self.batch_size, WaitTimeSeconds=self.wait_seconds if wait else 0
        )
        return [
            (m["Body"], partial(self.client.delete_message, QueueUrl=self.queue_url, ReceiptHandle=m["ReceiptHandle"]))
            for m in res.get("Messages", [])
        ]

    def ack_all(self, acks: list[Ack]) -> None:
        """Ten deletions per call. One that fails is redelivered later and skipped as a duplicate."""
        handles = [a.keywords["ReceiptHandle"] for a in acks]  # type: ignore[attr-defined]
        for i in range(0, len(handles), 10):
            entries = [{"Id": str(j), "ReceiptHandle": h} for j, h in enumerate(handles[i : i + 10])]
            self.client.delete_message_batch(QueueUrl=self.queue_url, Entries=entries)
