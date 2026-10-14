## What

## Why

## Checklist

- [ ] `uv run pytest` and `uv run ruff check .` pass
- [ ] `cartridges/fleet` and `cartridges/crm` still validate without edits
- [ ] Core change: a test in `tests/` covers it. Cartridge change: events in `examples/events/` and assertions.
- [ ] Nothing under `keel/` names an entity kind, relation, predicate, rule or event type
- [ ] If `examples/agent_minimal.py` output changed, the reason is in this description
