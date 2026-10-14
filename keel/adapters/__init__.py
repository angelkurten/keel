"""Event adapters: anything that yields `Event`s.

Shipped: JsonlAdapter (keel.ingest) for development.
Planned: PostgresTableAdapter (table + polling), WebhookAdapter (FastAPI), KafkaAdapter, SQSAdapter.
Each adapter only produces envelopes; it never understands payloads.
"""
