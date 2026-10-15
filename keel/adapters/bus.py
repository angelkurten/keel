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
only after the ingestor has taken the event, so a crash mid-event re-delivers it. A message
that is not a valid envelope raises and stays unacknowledged, unless you pass `dead_letter`:
it receives (body, error), and the adapter acknowledges the message and moves on.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from typing import Any

from keel.envelope import Event

Ack = Callable[[], None]
DeadLetter = Callable[[bytes | str, Exception], None]


class BusAdapter:
    """The shared loop. A subclass fetches raw messages as (body, ack) pairs."""

    def __init__(self, dead_letter: DeadLetter | None = None) -> None:
        self.dead_letter = dead_letter

    def _fetch(self) -> list[tuple[bytes | str, Ack]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def __iter__(self) -> Iterator[Event]:
        """One pass: consume until the bus has nothing more right now, then stop."""
        while True:
            batch = self._fetch()
            if not batch:
                return
            for body, ack in batch:
                try:
                    event = Event.model_validate(json.loads(body))
                except (TypeError, ValueError) as exc:  # not JSON, not an object, not an envelope
                    if self.dead_letter is None:
                        raise
                    self.dead_letter(body, exc)
                    ack()
                    continue
                yield event
                ack()  # runs once the consumer asks for the next event: the handler already ran

    def follow(self, interval: float = 1.0) -> Iterator[Event]:
        """Consume forever. `ingestor.ingest(adapter.follow())` never returns."""
        while True:
            n = 0
            for ev in self:
                n += 1
                yield ev
            if n == 0:
                time.sleep(interval)


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

    def _fetch(self) -> list[tuple[bytes | str, Ack]]:
        msg = self.consumer.poll(self.poll_timeout)
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

    def _fetch(self) -> list[tuple[bytes | str, Ack]]:
        res = self.client.receive_message(
            QueueUrl=self.queue_url, MaxNumberOfMessages=self.batch_size, WaitTimeSeconds=self.wait_seconds
        )
        return [
            (m["Body"], lambda h=m["ReceiptHandle"]: self.client.delete_message(QueueUrl=self.queue_url, ReceiptHandle=h))
            for m in res.get("Messages", [])
        ]
