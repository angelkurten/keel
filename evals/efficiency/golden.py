"""The golden set for tool efficiency (reports/Eficiencia de herramientas en Keel.md).

PROBES are fixed tool calls, measured for free on every change: tokens of what an MCP client hands the model, content
blocks, whether two identical calls return identical bytes, and `must`: strings the answer depends on, so a lighter
shape cannot drop them unnoticed. QUESTIONS are asked to real agents over the same MCP tools and graded against
`expected`: accuracy, tool calls, tokens and cost."""

from __future__ import annotations

from datetime import datetime, timezone

from showcase.scenarios import SCENARIOS

TODAY = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)  # every example is read on the showcase's day
EXAMPLES = {"fleet": "examples/fleet", "relay": "examples/relay"}

# (id, example, tenant, tool, args, must)
PROBES: list[tuple[str, str, str, str, dict, list[str]]] = [
    ("fleet.context", "fleet", "acme-logistics", "context", {"subject": "unit:MXK-4821"}, ["MXK-4821", "D-118"]),
    ("fleet.context.depth1", "fleet", "acme-logistics", "context", {"subject": "unit:MXK-4821", "depth": 1}, ["2.6", "unit_grounded"]),
    ("fleet.history.tire", "fleet", "acme-logistics", "history", {"subject": "tire:T-9001"}, ["5.2", "2.6", "2026-10-03"]),
    ("fleet.related", "fleet", "acme-logistics", "related", {"subject": "unit:MXK-4821"}, ["tire:T-9001"]),
    ("fleet.search.kind", "fleet", "acme-logistics", "search", {"kind": "tire"}, ["T-9001"]),
    ("fleet.annotate", "fleet", "acme-logistics", "annotate",
     {"subject": "tire:T-9001", "predicate": "assessment", "value": "critical: 2.6 mm < 3 mm", "scope": "task", "by": "agent:eval"}, ["assessment"]),
    ("relay.context.acme", "relay", "acme", "context", {"subject": "account:acme"}, ["enterprise"]),
    ("relay.context.wayne.depth1", "relay", "wayne", "context", {"subject": "account:wayne", "depth": 1}, ["INC-7"]),
    ("relay.search.sla", "relay", "stark", "search", {"rule": "sla_breached"}, ["T-3112"]),
    ("relay.search.text", "relay", "initech", "search", {"text": "Salesforce"}, ["T-2201"]),
    ("relay.search.kind", "relay", "stark", "search", {"kind": "ticket"}, ["T-3112"]),
    ("relay.history.ticket", "relay", "initech", "history", {"subject": "ticket:T-2201"}, ["resolved", "2026-10-06", "2026-10-14"]),
    ("relay.history.plan", "relay", "globex", "history", {"subject": "account:globex", "predicate": "plan"}, ["pro", "enterprise", "2026-10-10"]),
    ("relay.related.stark", "relay", "stark", "related", {"subject": "account:stark"}, ["ticket:T-3112"]),
    ("relay.related.wayne.depth2", "relay", "wayne", "related", {"subject": "account:wayne", "depth": 2}, ["incident:INC-7"]),
    ("relay.unknown.kind", "relay", "stark", "search", {"kind": "tickets"}, []),
    # the lean options, next to the defaults above
    ("fleet.context.tire.brief", "fleet", "acme-logistics", "context", {"subject": "tire:T-9001", "detail": "brief"}, ["2.6", "2026-10-03"]),
    ("relay.context.acme.brief", "relay", "acme", "context", {"subject": "account:acme", "detail": "brief"}, ["enterprise"]),
    ("fleet.history.tire.concise", "fleet", "acme-logistics", "history", {"subject": "tire:T-9001", "detail": "concise"}, ["5.2", "2.6", "2026-10-03"]),
    ("relay.history.ticket.concise", "relay", "initech", "history", {"subject": "ticket:T-2201", "detail": "concise"}, ["resolved", "2026-10-06", "2026-10-14"]),
    ("relay.search.text.brief", "relay", "initech", "search", {"text": "Salesforce", "detail": "brief"}, ["T-2201"]),
    ("fleet.annotate.ack", "fleet", "acme-logistics", "annotate",
     {"subject": "tire:T-9001", "predicate": "assessment", "value": "critical", "scope": "task", "by": "agent:eval", "detail": "ack"}, ["assessment"]),
    # R11, the text render, next to its JSON twin above
    ("fleet.context.depth1.text", "fleet", "acme-logistics", "context", {"subject": "unit:MXK-4821", "depth": 1, "detail": "text"}, ["2.6", "unit_grounded"]),
    ("relay.context.wayne.depth1.text", "relay", "wayne", "context", {"subject": "account:wayne", "depth": 1, "detail": "text"}, ["INC-7"]),
    ("relay.history.ticket.text", "relay", "initech", "history", {"subject": "ticket:T-2201", "detail": "text"}, ["resolved", "2026-10-06", "2026-10-14"]),
]

# The showcase's single-question scenarios (how-to needs the help center, memory is multi-step), and fleet questions.
QUESTIONS: list[dict] = [
    {"id": s["id"], "example": "relay", "tenant": s["customer"], "question": s["question"], "expected": s["expected"]}
    for s in SCENARIOS if "question" in s and s["id"] != "how-to"
] + [
    {"id": "fleet-dispatch", "example": "fleet", "tenant": "acme-logistics",
     "question": "Can unit MXK-4821 be dispatched today? Why?",
     "expected": "No: it is grounded because tire T-9001 (position P3) measures 2.6 mm since Oct 3, below the 3 mm critical "
                 "threshold. The Oct 10 inspection INS-4821-10 also found a major crack, and corrective ticket TK-3608 is approved "
                 "at workshop WS-02."},
    {"id": "fleet-tire-then", "example": "fleet", "tenant": "acme-logistics",
     "question": "What was the tread depth of tire T-9001 on September 20, and since when is it critical?",
     "expected": "5.2 mm on September 20 (measured Sep 10); it has been critical since October 3, when it measured 2.6 mm (< 3 mm)."},
    {"id": "fleet-docs", "example": "fleet", "tenant": "acme-logistics",
     "question": "Is any document of unit MXK-4821 or its driver about to expire?",
     "expected": "Yes: insurance policy POL-7731 covering MXK-4821 expires on Oct 20, 2026 (within 15 days, critical). The driver "
                 "D-118's license LIC-D118 expires on Mar 15, 2027, which is fine."},
]
