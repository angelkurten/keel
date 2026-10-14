# Contributing to Keel

Keel is built in the open, part-time. Small, focused pull requests land fastest.

## Set up

```bash
uv sync --extra dev
uv run pytest
uv run keel validate cartridges/fleet --events examples/events/fleet.jsonl && uv run keel validate cartridges/crm --events examples/events/crm.jsonl && uv run keel validate cartridges/finance --events examples/events/finance.jsonl
```

## What we are looking for

- **Cartridges.** The best contribution. A new vertical (clinics, logistics, property management, ...) in `cartridges/<name>/` with `cartridge.yaml`, `handlers.py`, a sample stream in `examples/events/<name>.jsonl` and a test in `tests/`. It must pass `keel validate` and require zero changes under `keel/`. If it does need a core change, open an issue first: that is a bug in the core, and we want to fix it generically.
- **Adapters.** Sources that yield `Event`s (see `keel/adapters/`). An adapter never reads a payload.
- **Bug reports with a failing test.** The fastest way to get something fixed.

## Rules the review enforces

`CLAUDE.md` is the spec and `docs/SPEC.md` is the normative contract. The short version:

1. No domain names under `keel/`: no entity kind, relation, predicate, rule or event type.
2. Pointers, not copies. Never copy a row or a table into the graph.
3. Facts close. Handlers never mutate or delete them.
4. Five tools. New capability goes into the semantics of the existing five.
5. Deterministic ingest: no network, no LLM, no randomness.
6. Every core change comes with a test. Every cartridge change comes with events and assertions.

## Process

- Open an issue before a large change. For a cartridge or a small fix, a pull request is fine.
- Run `uv run ruff check .` and `uv run pytest` before pushing.
- Commit messages and all user-facing text in English.
- If `examples/agent_minimal.py` output changes, say why in the pull request.

By contributing you agree that your contributions are licensed under the Apache License 2.0.
