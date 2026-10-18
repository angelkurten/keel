# Your first pilot in a day

A pilot answers one question: does your second agent connect in under a day, because there is no second pipeline to write? This page is the path, with a time budget for each step. Write down how long each one really took; that number is what the pilot measures.

You need: a product with domain events (rows in Postgres, webhooks, a Kafka topic or an SQS queue), one agent you already run or plan to, and Docker or Python 3.10+.

## 1. Pick one slice (30 min)

Choose one tenant and the three to five event types an agent actually needs: for a fleet, units, tires and inspections; for a clinic, patients, appointments and results. Leave everything else for later. Keel holds pointers to your rows, not copies, so the slice is small.

## 2. Write the cartridge (2 h)

```bash
uv tool install keel-context
keel init cartridges/mine        # cartridge.yaml + handlers.py
```

Declare the entities, relations and events, then one handler per event, about ten lines each. Put what your team means by "critical", "overdue" or "at risk" in `rules`, so every agent gets the same verdict. [Your first cartridge](first-cartridge.md) walks through it in 20 minutes; `examples/` has four finished ones.

Check it against a sample of real events before going further:

```bash
keel validate cartridges/mine --events sample.jsonl
```

## 3. Send your events (1–2 h)

Pick the path you already have; [Connect your events](connect.md) covers each one:

- **A Postgres outbox table**: your product inserts envelopes into it, and Keel polls it in order with a cursor, so a restart resumes.
- **Webhooks**: `POST /v1/events` with the envelope.
- **Kafka or SQS**: `keel consume --queue <url> --db <graph>`, which acknowledges only after the graph has stored the batch.
- **A file**, to start: `keel ingest cartridges/mine events.jsonl --db keel.db`.

Load your history first (`keel ingest` does a million events in under two minutes on MySQL), then leave the live stream running.

## 4. Serve the tools (30 min)

```bash
docker run -p 8000:8000 -e KEEL_SECRET=$(openssl rand -hex 32) \
  -v ./cartridges/mine:/cartridge:ro -v keel-data:/data ghcr.io/angelkurten/keel
```

Or run it next to your agent over stdio: `keel serve cartridges/mine --tenant <t> --db keel.db`. If several products or teams share one deployment, give each its own key (`keel keys create <account>`); the server confines each key to its account's tenants.

## 5. Connect the first agent (1 h)

Over MCP, any client: Claude Code (`claude mcp add keel -- keel serve ...`), Codex, LangGraph (`examples/fleet/agent_langgraph.py`), or the HTTP API and SDKs for TypeScript, Python, Go and PHP. Ask it something it used to answer from its own pipeline, and compare.

## 6. Connect the second agent (the measured part)

Start the clock. Connect a second agent, with a different job, to the same server: no new extraction, no new rules, no new memory store. If the cartridge declares `agents:`, give it its own id, the scopes it may write and the kinds it may see. Stop the clock when it answers from the shared graph and what the first agent wrote is visible to it.

## What you get from us

During a pilot we help you through these steps and fix what blocks you in the core, while keeping it generic: what your domain needs goes in your cartridge, never under `keel/`. The first design customer's requests have shipped as releases, most of them within days.

## Start one

- Open a [pilot request](https://github.com/angelkurten/keel/issues/new?template=pilot_request.yml) on GitHub,
- book 30 minutes: [calendar.app.google/3oKUSqXTQPYmjya56](https://calendar.app.google/3oKUSqXTQPYmjya56),
- or write to angel@angelkurten.com.
