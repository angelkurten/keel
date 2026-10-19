# Design note: the support showcase

Status: proposed on 2026-10-19. A public page at https://demo.angelkurten.com where a support agent answers the same question twice, once with Keel and once without, over the same data. Decided the same day: a B2B SaaS with subscriptions as the domain; RAG over the same data as the baseline (then RAG plus the product's API, see Fairness); guided scenarios plus a free question; generated data with planted stories; an expected answer and an LLM judge per scenario; `openai/gpt-6-sol` through OfoxAI on both sides; the Keel side is an account of hosted Keel.

## What it must show

Keel's claim is that agents inside a product need context with time, rules and scope, and that a pipeline per agent rebuilds it badly. The showcase makes four of those visible, each in at least one scenario:

1. **The customer's context in one call**: plan, open tickets, invoices, payments and incidents together, not stitched from search hits.
2. **Time**: what was true when something happened, and what was superseded since. Text retrieval mixes current and past.
3. **Rules**: SLA breached, refund eligible, account at risk are defined once and evaluated, never improvised by the model.
4. **Scope**: one customer's data never answers for another, and memory written for one stays with it.

And it must show what it costs: time and money per answer, side by side, because a context layer that answers better but slower or dearer is a trade-off a buyer has to see.

And it must be fair, or it convinces no one.

## Fairness rules

- **Same model, same base prompt, same help center.** Both agents run `openai/gpt-6-sol` with the same system prompt except the paragraph that describes their customer-data tools, and both get the same `search_help` tool over the product's help articles. Keel is not a document store; the help center is not what is compared.
- **Same source data.** One generator writes the SaaS's events. The Keel side ingests them into a hosted account (one tenant per customer company). The RAG side renders the same events, tickets and invoices as text chunks (one per record, with dates and the customer's name) and embeds them.
- **Each side gets its best ordinary setup.** Without Keel (decided 2026-10-19, after the first runs): the product's own API as tools (`get_account`, `list_subscription_changes`, `list_invoices`, `list_tickets`, `get_ticket` with its activity, `list_incidents`, `list_integrations`, `list_contacts`), the records' current state rebuilt from the same events, plus top-k similarity search over the events as text, filtered by customer, and a `save_note` tool (its equivalent of `annotate`): the strongest agent without Keel we could build. Business rules are not computed for it; it applies the help center's policies itself, as an agent on a product's API does. Keel: the five tools with semantic search on (the same embeddings), the cartridge's rules, nothing hand-tuned per scenario.
- **One scenario RAG should win or tie**: a how-to question answered from the help center. It shows both work where they should.
- **Everything is visible**: each side's tool calls with arguments and results, tokens, latency and cost per answer.

## The product: Relay

Relay is a fictional workflow-automation SaaS for teams. Plans: Starter (SLA 72 h), Pro (24 h), Enterprise (4 h, a named CSM). Billing monthly, prorated on plan changes; refunds within 14 days of a charge.

**Cartridge `relay`** (one tenant per customer company):

| Kind | What it is |
|---|---|
| `account` | the customer company (plan, seats, region, CSM) |
| `contact` | a person at the customer (role, language) |
| `subscription` | the plan over time (`plan`, `seats`, `status` as facts with valid time) |
| `invoice` | a charge (`amount`, `status`, `period`) |
| `ticket` | a support ticket (`priority`, `status`, `category`, opened and resolved times) |
| `incident` | a platform incident as it affected this customer (`severity`, `region`, start, end) |
| `integration` | a connected app (`status`: connected, failing, disconnected) |

Events (from the SaaS, deterministic): `account.created`, `contact.added`, `subscription.started`, `subscription.changed`, `subscription.cancelled`, `invoice.issued`, `invoice.paid`, `payment.failed`, `refund.issued`, `ticket.opened`, `ticket.status_changed`, `ticket.commented`, `incident.affected`, `incident.resolved`, `integration.status_changed`.

Rules: `sla_breached` (a ticket open longer than its plan's SLA), `refund_eligible` (an invoice paid less than 14 days ago with no refund), `account_at_risk` (a failed payment in 30 days and an open high-priority ticket), `integration_failing`.

## Data

A generator (`showcase/generate.py`, seeded, so it is reproducible) writes about 50 customers and six months of events, and plants the stories the scenarios need in seven of them. Some names are deliberately close (Hooli and Hooli Labs) and some records deliberately superseded (a ticket reopened and resolved again, a plan changed twice), because that is where retrieval goes wrong in real products. The same output feeds both sides; a test checks the planted facts hold in both.

## Scenarios

Each has a question, the expected answer the page shows, and the strength it exercises. "Today" is fixed at 2026-10-15 so answers do not drift.

| # | Question | Expected | Strength |
|---|---|---|---|
| 1 | Acme says they were overcharged in September. What happened? | Pro → Enterprise on Sep 12; the Sep invoice is prorated, not a double charge | context |
| 2 | Which plan did Globex have when they reported the outage on Oct 3? | Pro (upgraded to Enterprise on Oct 10) | time |
| 3 | Is Initech's sync problem still open? | No: reopened Oct 6, resolved Oct 14 | time (superseded) |
| 4 | Can we refund Umbrella's last charge? | No: paid Sep 20, outside the 14 days | rules |
| 5 | Does Stark have any ticket past its SLA? | Yes: T-3112, high, open 31 h on Enterprise (4 h) | rules |
| 6 | Show Hooli's last two payments. | Hooli's, not Hooli Labs' | scope |
| 7 | Is Wayne's 502 error related to an incident? | Yes: INC-7 in eu-west affected them Oct 2 09:10–11:40 | context + time |
| 8 | How do I set up SSO with Okta? | From the help center; both should answer | honesty |

**Two agents, one memory** (replaces the first memory pair, 2026-10-19): a support agent records that Lucía Gómez, who receives Acme's invoices, wants them in Spanish; a billing agent, in a new conversation, prepares Acme's November invoice (who receives it, in which language); then Globex's, where nothing about Spanish must appear. With Keel both agents read the same graph: the support agent's `annotate` (scoped to the run, so a public page never keeps it) comes back in the billing agent's `context`. Without Keel each agent keeps its own notes, as two agents built separately do; sharing them is infrastructure the baseline would have to add.

**What it took to build**: a section with each side's files and their real line counts (blank lines and comments left out), and what each brings: Keel's cartridge declares rules that are evaluated, keeps every fact's history, shares memory between agents with scope and evidence, and enforces tenant isolation in the tools; the baseline's API, event-to-text pipeline and index give current state, the activity the API happens to log, per-agent notes, and isolation by a filter in code. The line counts are close; the difference is what the lines buy.

## The page

- Header: one line on what is compared and the fairness rules, linked to this note.
- Scenario list on the left; on the right, the question, the expected answer, and two columns (with Keel / without Keel), each with the answer, the judge's verdict (correct, partial, wrong, with its reason), the tool calls (expandable), tokens, latency and cost.
- Guided scenarios show a cached run (date shown) with a "run it live again" button. A free question box runs both sides live, without a judge.
- **Metrics** on every answer, side by side: time, cost, tokens and tool calls, as numbers. Wall time covers the whole answer (every model and tool call, end to end), cost comes from the tokens each model call reports at the gateway's prices. The summary band gives the correct answers per side, the median time and the total cost and tokens over the guided runs. No relative percentages (decided 2026-10-19): the absolute numbers are shown and the reader compares.
- A footer with links: the cartridge, the generator, the data, the code of both agents, hosted Keel sign-up.

## Architecture

- **App** (`showcase/`, a FastAPI service and a small page) on Coolify at `demo.angelkurten.com`: runs both agent loops (reusing `hosted/agent.py`'s loop), the RAG index, the judge, the cache and the limits.
- **Keel side**: a hosted account `showcase` with the `relay` cartridge, its data ingested once; the app calls it with a key narrowed to read tools (no `annotate` except for the memory pair, which resets after each run).
- **RAG side**: embeddings with `openai/text-embedding-3-small` through OfoxAI (it serves it), computed once and kept in a file in the image; no vector database to operate.
- **Limits**: the free question is capped per IP per day and globally per day; guided runs live are capped the same way. The cache is a JSON file per scenario, refreshed by an operator command.

## Open

- Whether the judge is the same model or another, to avoid it favouring its own style.
- How often to refresh the cached runs, and whether to show the history of past runs.
