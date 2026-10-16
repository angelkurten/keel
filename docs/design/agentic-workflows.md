# Design note: what agentic workflows demand from Keel

Status: researched and exercised on 2026-10-14; items 1 to 6 and 11 shipped in 0.9.0 and 7 to 10 in 0.10.0, both on 2026-10-15 (the flow in `examples/fleet/workflow_ops.py` uses them); 12 stays a documented convention. Two inputs: a survey of the 2025-2026 state of the art (frameworks, memory systems, context engineering, protocols, observability, approvals) and `examples/fleet/workflow_ops.py`, a deliberately complex flow on Keel (five agents with different registry slices, two in parallel threads, an evaluator-optimizer loop, a human promoting a plan to tenant memory with evidence, an audit) that runs in the test suite on both stores.

## What the flow showed

| What happened | What it means |
| --- | --- |
| Triage had to enumerate units with `search("unit:")` and filter by kind | There is no way to list subjects by kind or by rule state; every orchestrator-workers pattern needs a work list |
| Two specialists wrote `recommendation` on the same unit; history kept both with their author, the open fact is the last writer's | Concurrent annotations have no declared policy: last-writer-wins by accident, contradictions are not surfaced |
| The dispatcher approving through the planner's console was refused (`planner-bot may not write scope 'tenant'`) | People are principals too; the registry must declare them, and the audit must show who approved |
| `context(as_of=30 days ago)` answered what was true then, not what the triage saw at the time | Transaction time (`recorded_at`) is stored but not exposed; post-mortems and eval replays need "as known at" |
| A second run the same day would read the first run's task memory | Task scope has no task id and no lifecycle beyond TTL |
| Rerunning a step after a failure would annotate twice | Durable-execution frameworks replay steps; writes need idempotency keys |
| Parallel threads on one `SqliteStore`, the human promotion with evidence, the registry refusal, `depth=1`, the export: all worked as designed | The core model holds; the gaps are in enumeration, identity of runs, time axes and write policies |

## What the state of the art says (sources in the research summary at the end)

- **Workflows persist state per step and resume from it.** LangGraph checkpoints per super-step with time travel; Microsoft Agent Framework checkpoints at the end of each superstep including pending human requests; Temporal, DBOS and Restate journal every LLM and tool call and never re-run a completed step; LangGraph warns that side effects before an `interrupt` must be idempotent. ADK changes state only by appending events with an `invocation_id`.
- **Memory systems are bitemporal and scoped, and they never delete.** Graphiti keeps four timestamps per fact and invalidates contradicted edges with provenance to the episode; Mem0 scopes by `user_id`, `agent_id`, `app_id`, `run_id` and marks conflicting relations invalid; Letta documents which shared-memory operations are safe under concurrency (append safe, replace is compare-and-swap, rewrite is last-writer-wins and an anti-pattern).
- **Context is a budget.** Anthropic's guidance: return names over ids, offer concise and detailed formats (concise is about a third of the tokens), cap tool results (Claude Code: 25k tokens), avoid list-everything tools but give filtered search; sub-agents return distilled summaries and pass references, not payloads; deterministic serialization keeps prompt caches warm (Manus, MCP 2026-07-28).
- **Protocols moved to explicit handles, subscriptions and tasks.** MCP 2026-07-28 removed sessions in favour of server-minted handles passed as tool arguments, added `subscriptions/listen` for change notifications and `ttlMs`/`cacheScope` on results, and carries `traceparent` in `_meta`; A2A groups work by `contextId`/`taskId` with terminal states; agent identity products (Entra, Okta) give each agent a credential, a human sponsor and per-agent audit logs.
- **Observability joins traces to data.** OpenTelemetry GenAI conventions name `execute_tool` spans with `gen_ai.tool.call.id`; XTDB's decision-audit pattern pins both valid time and system time to the decision timestamp; the EU AI Act Art. 12 requires automatic event logging over the system's lifetime for high-risk systems.
- **Approval is a durable pause with an id.** OpenAI's `RunState` interruptions, Microsoft `request_info` ids in checkpoints, A2A `INPUT_REQUIRED`, MCP multi-round-trip requests; findings are promoted only after verification (Anthropic's long-running harness).

## Proposals, ranked, all inside the five tools

| # | Capability | Pattern that needs it | Size |
| --- | --- | --- | --- |
| 1 | **Transaction time**: `context(as_known_at=)` and `history(as_known_at=)` answer "what did the agent see"; needs the close's own `recorded_at` on each fact | post-mortems, eval replay, Art. 12 | S |
| 2 | **Changes since a cursor**: `history(since=, subject=None)` returns the tenant's facts recorded after a watermark, annotations included; `context` carries the current watermark | incremental runs, background memory formation, blackboard polling, MCP subscriptions | S |
| 3 | **Task ids on task memory**: `annotate(task=)`, `history(task=)`, `keel expire --task` closes a task | handoffs, sub-agents on one job, trace grouping (`run_id`, `contextId`, `invocation_id`) | M |
| 4 | **Idempotency key on `annotate`**: same key, same subject and predicate returns the existing fact | durable-execution replay, interrupt re-runs | S |
| 5 | **Enumerate**: `search(kind=, rule=, limit=)` lists subjects of a kind, or those where a rule fires now, with a cursor | routing, orchestrator-workers, blackboards | M |
| 6 | **People as principals**: `kind: human` in the registry, approvals signed by a declared person | human-in-the-loop with audit | S |
| 7 | **Detail levels and budgets**: `context(detail=concise)` returns `{predicate: value}` and neighbour summaries; `limit` on `history` and `search`; truncation notices name the narrower filter | context rot, sub-agent summaries | M |
| 8 | **Conflict policy per predicate**: `predicates: { recommendation: { multi: true } }` keeps one open fact per author instead of last-writer-wins; `context` lists contradictions across authors | concurrent agents on one subject | M |
| 9 | **Proposed vs accepted**: an annotation can carry `status: proposed`; promotion is an `annotate` with evidence by an authorized principal; `context` shows proposals apart from accepted facts | evaluator-optimizer, approvals | M |
| 10 | **Trace propagation**: accept `traceparent` and `tool_call_id` on HTTP and MCP calls, store them with each write (and with refusals) so a Langfuse or LangSmith trace joins the audit export | observability, "what did the agent see" together with 1 | M |
| 11 | **Freshness in results**: the tenant watermark and ingest lag in every `context`, in a stable position; deterministic key order everywhere | agents deciding whether to re-query, prompt caches | S |
| 12 | **Progress notes**: a documented convention, `predicate: note` at task scope with TTL, for agents that must survive a context reset | long-running agents | docs |

Items 1 to 6 and 11 are one release (0.9.0): three new columns on facts (`task`, `idempotency_key`, `closed_at`) with an additive migration in the SQL stores, and extensions to the five tools that keep every existing call valid. Items 7 to 10 are the one after.

## Research summary

Frameworks: Anthropic "Building effective agents" and "Effective context engineering"; OpenAI Agents SDK handoffs, sessions, human-in-the-loop; LangGraph persistence, interrupts, memory; Google ADK sessions, events, memory; Microsoft Agent Framework workflows; AG2 swarm; CrewAI memory; Temporal OpenAI Agents integration; DBOS architecture; Restate agents; Inngest AgentKit; "Safe to Resume?" (arXiv 2608.29381). Memory: Zep/Graphiti (arXiv 2501.13956), Mem0 (arXiv 2504.19413), Letta shared memory, LangMem, Anthropic memory tool and context editing, OpenAI conversation state. Context: Anthropic "Writing tools for agents", "Advanced tool use", multi-agent research system, long-running harness; Manus context engineering; Chroma "Context Rot". Protocols: MCP changelogs 2025-06-18, 2025-11-25, 2026-07-28; A2A 1.0; Entra agent identity; Okta agent access; Claude Agent SDK permissions; EU AI Act Art. 12. Coordination: blackboard papers (arXiv 2510.01285, 2507.01701); Stripe idempotent requests. Observability: OpenTelemetry GenAI semantic conventions; Langfuse data model; LangSmith threads; XTDB temporal orchestration.
