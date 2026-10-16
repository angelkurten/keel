## What

## Why

## Checklist

- [ ] `uv run pytest` and `uv run ruff check .` pass
- [ ] `examples/fleet` and `examples/crm` still validate without edits
- [ ] Core change: a test in `tests/` covers it. Example cartridge change: events in `examples/<name>/events.jsonl` and assertions in its `test_<name>.py`.
- [ ] Nothing under `keel/` names an entity kind, relation, predicate, rule or event type
- [ ] If `examples/fleet/agent_minimal.py` output changed, the reason is in this description
