"""TGMS (arXiv 2607.10265, the closest prior system) as a benchmark system: the tenant's log loaded into its
bi-temporal property graph, and its own fifteen tools for the agent.

    pinned: tgms==0.8.0 (PyPI, 2026-09-15, Apache-2.0); run the bench with `--with tgms==0.8.0`.

Loaded as its write path does (each op what `Store.assert_node` / `assert_edge` build, written in batches through
`Store._write` instead of one batch per op, for speed): each issue a node whose props hold its state, version by
version over the interval each held; each field value also an edge issue -has_<field>-> <field>:<value> over that
interval, so its snapshot and history operators reach values; each link an edge over its interval. Adapted from a
probe in another session (scratchpad tgms/probe.py), checked here.

What it cannot be given: TGMS stamps transaction time with its own clock at write time (a hybrid logical clock), as
Keel did before 2026-10-20, so the log's recorded_at cannot become TGMS's transaction time and "what had been
recorded by K" in the log's dates is not answerable natively. The graph holds the log's final belief (corrections
applied); such questions are asked anyway and reported as not supported natively.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any

FIELDS = {"issue.status_changed": "status", "issue.resolution_changed": "resolution", "issue.assigned": "assignee",
          "issue.type_changed": "issue_type", "issue.priority_changed": "priority"}
TOOLS_TEXT = (
    "The project's history is in TGMS, a bi-temporal graph: issues are nodes 'issue:<KEY>' (label Issue, props key, status, priority, "
    "assignee, resolution, issue_type, reporter, summary), the project is node 'project:<KEY>', edges in_project, subtask_of and Jira link "
    "types (blocks, relates_to, ...); each field value is also an edge over the interval it held: has_status -> 'status:<value>', "
    "has_priority -> 'priority:<value>', has_assignee -> 'user:<id>', has_resolution -> 'resolution:<value>', has_issue_type -> "
    "'issue_type:<value>'. Valid time is when it happened in Jira, transaction time when it was recorded; times are int64 epoch "
    "microseconds UTC."
)


def us(s: str) -> int:
    return int(datetime.fromisoformat(s).timestamp() * 1_000_000)


def edge(uid: str, field: str, value: Any, vt_s: int, vt_e: int) -> dict[str, Any]:
    kind = "user" if field == "assignee" else field
    return {"op": "assert_edge", "src": uid, "dst": f"{kind}:{value}", "rel_type": f"has_{field}", "vt_s": vt_s, "vt_e": vt_e,
            "src_label": "Issue", "dst_label": kind.capitalize()}


def load(events: list[dict[str, Any]], path: Path) -> Any:
    import tgms
    from tgms.core.model import OPEN_END

    if path.exists():
        shutil.rmtree(path)
    store = tgms.open(path)
    project = next(e["payload"]["project"] for e in events if e["type"] == "issue.created")
    changes: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    links: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    ops: list[dict[str, Any]] = [{"op": "assert_node", "uid": f"project:{project}", "label": "Project", "props": {"key": project, "name": project}, "vt_s": 0}]
    # the final belief: a correction's right value replaces the wrong one recorded for the same instant
    for e in sorted(events, key=lambda e: (e["occurred_at"], e["recorded_at"], e["id"])):
        uid, p, vt = e["subject"], e["payload"], us(e["occurred_at"])
        if e["type"] == "issue.created":
            changes[uid].append((vt, {"key": uid[6:], "name": uid[6:], **{k: p[k] for k in
                                ("summary", "issue_type", "status", "priority", "assignee", "resolution", "reporter")}}))
            ops.append({"op": "assert_edge", "src": uid, "dst": f"project:{p['project']}", "rel_type": "in_project", "vt_s": vt,
                        "src_label": "Issue", "dst_label": "Project"})
            if p["parent"]:
                ops.append({"op": "assert_edge", "src": uid, "dst": f"issue:{p['parent']}", "rel_type": "subtask_of", "vt_s": vt,
                            "src_label": "Issue", "dst_label": "Issue"})
        elif e["type"] in FIELDS:
            changes[uid].append((vt, {FIELDS[e["type"]]: p[FIELDS[e["type"]]]}))
        else:  # the adapter writes one linked/unlinked per change of the open count, alternating per pair
            links[(uid, f"issue:{p['other']}", p["relation"])].append(vt)
    for (src, dst, rel), ts in links.items():
        ts.sort()
        for i in range(0, len(ts), 2):
            ops.append({"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel, "vt_s": ts[i],
                        "vt_e": ts[i + 1] if i + 1 < len(ts) else OPEN_END, "src_label": "Issue", "dst_label": "Issue"})
    for uid, cs in changes.items():
        state: dict[str, Any] = {}
        held: dict[str, tuple[int, Any]] = {}  # field -> (since, value)
        for i, (vt, part) in enumerate(cs):
            state = {**state, **part}
            end = cs[i + 1][0] if i + 1 < len(cs) else OPEN_END
            if "key" in state and end > vt:
                ops.append({"op": "assert_node", "uid": uid, "label": "Issue", "props": dict(state), "vt_s": vt, "vt_e": end})
            for f in FIELDS.values():
                if f in part and (f not in held or held[f][1] != part[f]):
                    if f in held and held[f][1] is not None and vt > held[f][0]:
                        ops.append(edge(uid, f, held[f][1], held[f][0], vt))
                    held[f] = (vt, part[f])
        for f, (since, v) in held.items():
            if v is not None:
                ops.append(edge(uid, f, v, since, OPEN_END))
    nodes = {op["uid"] for op in ops if op["op"] == "assert_node"}
    for d, label in sorted({(op["dst"], op["dst_label"]) for op in ops if op["op"] == "assert_edge"}):
        if d not in nodes:  # a value, a user, or an issue outside the tenant: a node of its own, so snapshots reach it
            nodes.add(d)
            ops.append({"op": "assert_node", "uid": d, "label": label, "props": {"name": d.split(":", 1)[1]}, "vt_s": 0})
    for op in ops:
        op.setdefault("props", {})
        op.update(source="ingest", provenance_ref=None)
    for i in range(0, len(ops), 5000):  # ponytail: batches of what assert_* write one at a time
        store._write(ops[i : i + 5000])
    return store


@cache
def router(data: Path, tenant: str) -> Any:
    """One store and router per tenant, built once: TGMS's tools only read, so runs cannot leak into each other."""
    from tgms.tools.server import ToolRouter

    from evals.jira.bench import source

    store = load(source(data, tenant), data / "tgms" / tenant)
    return ToolRouter(store.adapter, tt_source=store)


@cache
def tools() -> list[dict[str, Any]]:
    from tgms.tools.schemas import openai_tools

    return openai_tools()


def call(data: Path, tenant: str, name: str, args: dict[str, Any]) -> str:
    return json.dumps(router(data, tenant).call(name, args), ensure_ascii=False, default=str)
