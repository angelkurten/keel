# Contributing to Keel

Keel is built in the open, part-time. Small, focused pull requests land fastest.

## Set up

```bash
uv sync --extra dev
uv run pytest
uv run keel validate cartridges/fleet --events examples/events/fleet.jsonl && uv run keel validate cartridges/crm --events examples/events/crm.jsonl && uv run keel validate cartridges/finance --events examples/events/finance.jsonl && uv run keel validate cartridges/clinics --events examples/events/clinics.jsonl
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

## Against a real Postgres

The table adapter's tests run on sqlite3. To run the same test on Postgres, start one and point `KEEL_PG_DSN` at it:

```bash
docker run -d --rm --name keel-pg -e POSTGRES_PASSWORD=keel -p 127.0.0.1:5435:5432 postgres:16-alpine
KEEL_PG_DSN=postgresql://postgres:keel@127.0.0.1:5435/postgres uv run --extra postgres pytest -k real_postgres
docker rm -f keel-pg
```

## Against MySQL

The store conformance suite runs on MySQL too (CI does it with a service container). Locally:

```bash
docker run -d --rm --name keel-mysql -e MYSQL_ROOT_PASSWORD=keel -e MYSQL_DATABASE=keel -p 127.0.0.1:3307:3306 mysql:8.4
KEEL_MYSQL_URL=mysql://root:keel@127.0.0.1:3307/keel uv run --extra mysql pytest -q tests/test_store.py
docker rm -f keel-mysql
```

## Your own store

The core talks to a `Store` protocol (`keel/store/base.py`); `MemoryStore` is the reference and `SqliteStore` the default durable one. A third store (Postgres, Graphiti, anything) is a separate package, never a core change. Run the conformance suite against it:

```bash
KEEL_STORE=your_pkg.module:factory uv run pytest tests/test_store.py
```

`factory` is a zero-argument callable or class that returns a fresh, empty store.

## Releasing

Bump `version` in `pyproject.toml` and `__version__` in `keel/__init__.py`, update the roadmap, then:

```bash
git tag -a vX.Y.Z -m "keel-context X.Y.Z" && git push origin vX.Y.Z
gh release create vX.Y.Z --title "X.Y.Z" --notes-file notes.md
```

The tag triggers `.github/workflows/release.yml`, which runs the tests, builds and publishes `keel-context` to PyPI through trusted publishing (one-time setup on PyPI: pending publisher for project `keel-context`, owner `angelkurten`, repository `keel`, workflow `release.yml`, environment `pypi`).

The SDK runtimes release separately, when they change:

```bash
git tag sdk/typescript/vX.Y.Z && git push origin sdk/typescript/vX.Y.Z   # npm, by .github/workflows/sdk-typescript-release.yml (trusted publishing); bump sdk/typescript/package.json first
git tag sdk/go/vX.Y.Z && git push origin sdk/go/vX.Y.Z                       # Go module in this repo
git subtree split --prefix=sdk/php -b php-split && git push git@github.com:angelkurten/keel-sdk-php.git php-split:main   # Composer
cd ../.. && git tag -f php-split-done && git branch -D php-split              # then tag vX.Y.Z in keel-sdk-php; Packagist follows it
```
