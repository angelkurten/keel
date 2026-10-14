"""Event adapters: anything that yields `Event`s.

Shipped: JsonlAdapter (keel.ingest) for development, PostgresTableAdapter
(table + polling with a cursor), webhook `build_app` (FastAPI, push),
KafkaAdapter and SqsAdapter (keel.adapters.bus, at-least-once).
Planned: Postgres CDC.
Each adapter only produces envelopes; it never understands payloads.
"""
