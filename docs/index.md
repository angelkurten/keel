# Keel

**Stop rebuilding the pipeline for every agent.**

Keel is a context layer for the AI agents inside a product: a vertical SaaS, a consumer app, an internal tool. Domain events from your product go in. A bitemporal context graph per tenant comes out, whatever your unit is (an end-customer, a household, a team). Every agent you build reads and writes it through the same five MCP tools.

You describe your vertical once, in a *cartridge*: ontology, event catalog, semantic rules, memory policies. Keel's core knows nothing about your domain. Your second agent connects in a day instead of weeks, because there is no second pipeline to write.

!!! note "Status"
    0.27.1, alpha. The five tools over MCP and an HTTP JSON API (`/v1`), Postgres, webhook, Kafka and SQS adapters, semantic search, consolidation, an agent registry and SDKs for TypeScript, Python, Go and PHP, on the in-memory store, SQLite or MySQL (`--db`). See the [roadmap](roadmap.md).

![Terminal demo: validate the stream against the cartridge, ingest, ask for a tire's context with the rule's verdict, run an agent with no LLM](demo.gif)

!!! tip "Pilot it with us"
    Run Keel on your product's events with your agents, with our help: [your first pilot in a day](pilot.md). Open a [pilot request](https://github.com/angelkurten/keel/issues/new?template=pilot_request.yml), book 30 minutes at [calendar.app.google/3oKUSqXTQPYmjya56](https://calendar.app.google/3oKUSqXTQPYmjya56), or write to angel@angelkurten.com.

## The problem

Every new agent in a product repeats the same work: extract the customer's data, normalize it, decide what "critical" means, keep its own memory, expose its own tools to the model. Four agents, four copies of the customer's context, four definitions of "critical", four memories that never talk.

Existing memory layers ingest text and documents and model memory per user or per agent. None of them start from **domain events**, and none model the **tenant** your product already has: the end-customer of a B2B SaaS, the household of a consumer app, the team of an internal tool.

## How Keel is different

1. **Cartridges.** A vertical is declared in YAML plus a few ten-line handlers. The core stays generic. Write a cartridge for fleets, clinics, logistics, or your own product.
2. **Events in, not documents.** Keel ingests structured facts with time: Postgres tables, webhooks, Kafka, SQS. Ingestion is deterministic and cheap. The graph is exact because it is born from events, not inferred from text.
3. **Every fact carries its provenance.** Source, `valid_from`, `valid_to`, `recorded_at`, and who corrected it. Audit-ready by construction.
4. **One graph per tenant.** One subgraph per end-customer, household or team, with its own permissions and expiry.

## Architecture

```mermaid
flowchart LR
    subgraph saas ["Your product"]
        pg[("Postgres outbox")]
        wh["Webhook"]
        jl[".jsonl file"]
    end
    subgraph core ["Keel core (domain-agnostic)"]
        env["Event envelope<br/>tenant · type · subject · time"]
        ing["Ingestor<br/>dispatch by event.type"]
        store[("Bitemporal graph<br/>one subgraph per tenant")]
        tools["Five tools<br/>context · history · search · annotate · related"]
        env --> ing --> store --> tools
    end
    subgraph cart ["Cartridge (your vertical)"]
        yaml["cartridge.yaml<br/>ontology · events · rules · memory policy"]
        hand["handlers.py<br/>payload → store primitives"]
    end
    pg & wh & jl --> env
    hand -.-> ing
    yaml -.-> tools
    tools <--> a1["Agent A"] & a2["Agent B"] & a3["Agent N"]
```

Events are the truth and the graph is derived, so one tenant's subgraph can be rebuilt by replaying its log. Agents never see a pipeline: they see five tools.

## Five tools, any agent

```text
context(subject, as_of?, depth?, as_known_at?, task?, detail?, exclude?, only?)
  -> what is true now, at as_of, or as known at a past moment; rules and their verdicts;
     depth=1 adds the neighbors; detail="concise" for about a third of the tokens; only=[...] just those predicates
history(subject?, since?, until?, predicate?, scope?, as_known_at?, task?, changed_since?, limit?)
  -> what was true and when we learned it; without a subject, what changed since a watermark
search(text?, kind?, rule?, limit?, after?, mode?, min_score?, related_to?, via?, detail?, depth?, per_kind?, exclude?, as_of?, where?)
  -> text or semantic search, or every entity of a kind / where a rule fires, paged;
     related_to (and via) keeps only one subject's neighbors; detail="concise" puts each result's facts in it;
     per_kind=n the best of every kind: the context for a task in one call
     where={name: value} by an attribute's or a fact's value; each result says what matched (matched, snippet);
     kind may be a list of kinds
annotate(subject, predicate, value, scope?, evidence?, by?, task?, key?, status?)
  -> an agent (or a person) writes what it learned; key makes retries safe; status="proposed" waits for review
related(subject, relation?, as_of?, depth?)
  -> neighbors by relation type, up to three hops
```

`search(mode="semantic")` finds subjects by meaning: ids in the query first, then a dense and a lexical leg fused, then an optional reranker. Where vectors live and which model makes them are adapters you choose: in memory, Chroma, any OpenAI-compatible embeddings endpoint, or your own vector database and model ([how](connect.md#semantic-search), [example](https://github.com/angelkurten/keel/blob/master/examples/custom_adapters.py)).

Expose them over MCP with `keel serve` and point any agent framework at them. The names and signatures are part of the public standard; see the [specification](SPEC.md).

## Quick start

```bash
git clone https://github.com/angelkurten/keel && cd keel
uv sync --extra dev
uv run keel validate examples/fleet
uv run keel ingest examples/fleet examples/fleet/events.jsonl
uv run keel context examples/fleet examples/fleet/events.jsonl unit:MXK-4821
```

Then:

- [Your first cartridge in 20 minutes](first-cartridge.md): declare your own vertical.
- [Connect your events](connect.md): a Postgres outbox table, a webhook, Kafka, SQS or a `.jsonl` file; the MCP server and the HTTP JSON API; semantic search.
- [SDKs](sdks.md): a typed client generated from your cartridge (TypeScript, Python, Go, PHP), the way protoc does.
- [Specification](SPEC.md): the normative contract for the envelope, the graph model, cartridges, tools and scopes.

## Design rules the core enforces

- **Events are the truth.** An append-only log. The graph is derived and can be replayed.
- **Pointers, not copies.** The graph holds references to your rows, the semantics, and earned memory. Never a copy of your tables.
- **Explicit time.** Facts close. They are not overwritten.
- **Scopes, not a flat store.** `global` > `tenant` > `task` > `agent`. Nothing is promoted to a wider scope by accident. Agent memory is read only by the agent that wrote it; task memory tied to a task id only by calls that pass that task (`context(task=)`, `history(task=)`); global memory lives in a reserved partition every tenant reads and no event writes.
- **Five tools, and only five.** New capability goes into the semantics of the existing five.

## License

Apache 2.0. The event envelope, the cartridge format and the five tools are the standard. They will always be free.
