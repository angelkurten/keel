"""Event adapters: anything that yields `Event`s.

Shipped: JsonlAdapter (keel.ingest) for development, PostgresTableAdapter
(table + polling with a cursor), webhook `build_app` (FastAPI, push).
Planned: KafkaAdapter, SQSAdapter, Postgres CDC.
Each adapter only produces envelopes; it never understands payloads.
"""
