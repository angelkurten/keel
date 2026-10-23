"""The benchmark run: each question asked of an agent with one system's tools, graded by exact match.

    OFOX_API_KEY=$(cat ~/.ofox_key) uv run --extra mcp --with tiktoken python -m evals.jira.bench \
        --data ~/datasets/public-jira/export --systems keel,rag,api,closed --models openai/gpt-6-luna \
        [--tenants hyperledger] [--types known_at,rule] [--per-type 2] [--reps 1] [--workers 6] --out results.jsonl

Every system reads the same source, the tenant's log as written (bitemporal.jsonl: each event with when it
happened and when it was recorded, corrections included), in its best ordinary form:

- keel:   the five tools over the tenant's graph, through a real MCP session (as in evals/efficiency).
- rag:    one text record per event, both times in it; hybrid search (dense + BM25, fused by RRF), with an
          optional filter by issue key: a strong baseline, not a straw man.
- api:    a Jira-like product API: get_issue (current fields and links), get_changelog (the audit log, each
          entry with both times), search_issues over current fields.
- closed: no tools: what the model knows of these public trackers from training (a contamination control).
- graphiti, mem0: search over what their LLM extractor made of the log (evals/jira/memories.py; they ingest the
          subset the questions touch, and --subset gives every other system the same subset).
- tgms:   TGMS 0.8.0's own tools over the log loaded into its bi-temporal graph (evals/jira/tgms_system.py).
- tgms-pev: the same graph through TGMS's own agent layer (planner, plan verifier, executor with repairs) instead
          of a tool loop, scored on the engine's answer as TGMS's harness scores it.

Same model, same system prompt but for one paragraph about the tools, temperature 0. The agent ends with
`ANSWER: <json>`; grading is exact after normalising (case, None/"none"/"unassigned", sets of keys, yes/no),
so no judge model is needed. Results are appended as JSON lines, one run per line, and a rerun skips what is
already there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from threading import Lock
from typing import Any

import numpy as np

from evals.efficiency.run import Gateway, call, pause, tokens, tool_list
from keel import Ingestor, MemoryStore, Tools, load_cartridge, mcp_server
from keel.envelope import Event
from keel.mcp_server import build_server

HERE = Path(__file__).parent
EMBEDDING_MODEL, EMBEDDING_DIMS = "openai/text-embedding-3-small", 512
INSTANCES = {"hyperledger": ("Hyperledger", "INDY"), "mariadb": ("MariaDB", "MXS"), "spring": ("Spring", "SEC")}
SYSTEM = """You answer questions about the Jira project {project} of the {instance} tracker. Today is {today}. All times are UTC.
{tools}
Use your tools before answering and never invent facts. Read every date in the question carefully: some ask what was true at \
an earlier time, or what had been recorded by a given time. End your reply with one line, `ANSWER: <json>`, where <json> is \
{fmt} If the records cannot answer the question, end with `ANSWER: "unknown"`."""
FORMATS = {
    "value": 'the value as a JSON string (for example "In Progress" or "user-1a2b3c4d"), or null when the field is empty (unassigned, unresolved).',
    "set": 'a JSON list of issue keys, for example ["INDY-12", "INDY-40"]; [] when there are none.',
    "bool": "true or false.",
    "count": "a JSON integer.",
}
TOOLS_TEXT = {
    "keel": "The project's data is in Keel, a context graph, through five tools. Subjects are '<kind>:<id>', issues are 'issue:<KEY>'.",
    "rag": "The project's records (every change to every issue, with when it happened and when it was recorded) are searchable through search_records.",
    "api": "The tracker's API is available: get_issue, get_changelog and search_issues.",
    "closed": "You have no tools: answer from what you know.",
    "graphiti": "The project's history is in a temporal knowledge graph (Graphiti), searchable through search_memory: facts with when they became and stopped being true.",
    "mem0": "The project's history is in a memory store (Mem0), searchable through search_memory: memories with when the change happened and when it was recorded.",
    "tgms": "",  # filled from evals.jira.tgms_system on first use
}
MEMORY_TOOLS = [{"type": "function", "function": {
    "name": "search_memory", "description": "Search the project's memory for facts relevant to a query.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer", "description": "how many results (default 15, at most 50)"}},
                   "required": ["query"]}}}]
EXTRACTOR = "anthropic/claude-haiku-5.5"  # --extractor: the model Graphiti and Mem0 ingested with


# ---- the source, read once per tenant ---------------------------------------------------


SUBSET = False  # --subset: every system reads only the issues the questions touch, as Graphiti and Mem0 do
KEEL_MAX_LIST: int | None = None  # --keel-max-list: the deployment ceiling an operator serving agents would set (off by default)
KEEL_RULES = True  # --keel-no-rules: the ablation where the cartridge declares no rules and the agent applies the question's definition
KEEL_DETAIL: str | None = None  # --keel-detail full: the ablation where every context/history/search returns its full form


@cache
def log(data: Path, tenant: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (data / f"{tenant}.bitemporal.jsonl").open()]


def source(data: Path, tenant: str) -> list[dict[str, Any]]:
    if SUBSET:
        from evals.jira.memories import subset

        return subset(data, tenant)
    return log(data, tenant)


def when(s: str) -> str:
    return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M:%S UTC")


def record(e: dict[str, Any]) -> tuple[str, list[str]]:
    """An event as one line of text, and the issue keys it is about."""
    key, p = e["subject"].split(":", 1)[1], e["payload"]
    times = f"happened {when(e['occurred_at'])}, recorded {when(e['recorded_at'])}"
    if e["type"] == "issue.created":
        text = (f"{key} created: \"{p['summary']}\"; type {p['issue_type']}, status {p['status']}, priority {p['priority']}, "
                f"assignee {p['assignee'] or 'none'}, resolution {p['resolution'] or 'none'}, reporter {p['reporter']}"
                + (f", sub-task of {p['parent']}" if p["parent"] else ""))
        return f"[{times}] {text}", [key] + ([p["parent"]] if p["parent"] else [])
    if e["type"] in ("issue.linked", "issue.unlinked"):
        verb = "linked" if e["type"] == "issue.linked" else "unlinked"
        return f"[{times}] {key} {verb}: {key} {p['relation'].replace('_', ' ')} {p['other']}", [key, p["other"]]
    field = next(k for k in ("status", "assignee", "priority", "resolution", "issue_type") if k in p)
    return f"[{times}] {key} {field.replace('_', ' ')} changed to {p[field] if p[field] is not None else 'none'}", [key]


# ---- keel --------------------------------------------------------------------------------


@cache
def keel_cartridge() -> Any:
    c = load_cartridge(HERE)
    return c if KEEL_RULES else replace(c, rules={})


def keel_server(data: Path, tenant: str) -> Any:
    """A fresh graph per question: what one agent annotates never reaches another run."""
    store = MemoryStore()
    Ingestor(keel_cartridge(), store).ingest([Event(**e) for e in source(data, tenant)], strict=True)
    return build_server(Tools(keel_cartridge(), store, tenant=tenant, max_list=KEEL_MAX_LIST))


@cache
def keel_tools(data: Path, tenant: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t["inputSchema"]}}
            for t in tool_list(keel_server(data, tenant))]


# ---- rag ---------------------------------------------------------------------------------

WORD = re.compile(r"[a-z0-9]+(?:-[0-9]+)?")


class Rag:
    def __init__(self, data: Path, tenant: str, gw: Gateway) -> None:
        self.texts, self.keys = zip(*(record(e) for e in source(data, tenant)), strict=True)
        digest = hashlib.sha1("\n".join(self.texts).encode()).hexdigest()[:12]  # a changed log is embedded again
        path = data / f"{tenant}.rag-{digest}.npy"
        if path.exists():
            self.vectors = np.load(path)
        else:
            self.vectors = np.array(embed(gw, list(self.texts)), dtype=np.float32)
            np.save(path, self.vectors)
        self.vectors /= np.linalg.norm(self.vectors, axis=1, keepdims=True)
        self.docs = [Counter(WORD.findall(t.lower())) for t in self.texts]
        df = Counter(w for d in self.docs for w in d)
        self.idf = {w: math.log(1 + (len(self.docs) - n + 0.5) / (n + 0.5)) for w, n in df.items()}
        self.avg = sum(sum(d.values()) for d in self.docs) / len(self.docs)

    def bm25(self, query: str, pool: list[int]) -> list[int]:
        q = WORD.findall(query.lower())

        def score(i: int) -> float:
            d, n = self.docs[i], sum(self.docs[i].values())
            return sum(self.idf.get(w, 0) * d[w] * 2.2 / (d[w] + 1.2 * (0.25 + 0.75 * n / self.avg)) for w in q if w in d)

        return sorted(pool, key=lambda i: -score(i))

    def search(self, gw: Gateway, query: str, issue: str | None, k: int) -> list[str]:
        pool = [i for i, keys in enumerate(self.keys) if issue is None or issue.upper() in keys]
        if not pool:
            return []
        q = np.array(embed(gw, [query])[0], dtype=np.float32)
        dense = sorted(pool, key=lambda i: -float(self.vectors[i] @ q))
        rrf: dict[int, float] = defaultdict(float)
        for ranking in (dense[:100], self.bm25(query, pool)[:100]):
            for rank, i in enumerate(ranking):
                rrf[i] += 1 / (60 + rank)
        return [self.texts[i] for i in sorted(rrf, key=lambda i: -rrf[i])[:k]]


def embed(gw: Gateway, texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for i in range(0, len(texts), 512):
        for attempt in range(8):  # the gateway's per-minute limit is shared with the ingests: back off and retry
            r = gw.http.post(f"{gw.base}/embeddings", json={"model": EMBEDDING_MODEL, "input": texts[i : i + 512], "dimensions": EMBEDDING_DIMS},
                             headers=gw.auth, timeout=120)
            if r.status_code != 429 and r.status_code < 500:
                break
            time.sleep(pause(r.headers) if r.status_code == 429 else min(60, 5 * 2**attempt))
        r.raise_for_status()
        out += [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    return out


RAG_TOOLS = [{"type": "function", "function": {
    "name": "search_records", "description": "Search the project's records (one per change to an issue, with when it happened and when it was recorded). Hybrid semantic and keyword search.",
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string"}, "issue": {"type": "string", "description": "only records about this issue key, e.g. INDY-12"},
        "k": {"type": "integer", "description": "how many records (default 15, at most 50)"}}, "required": ["query"]}}}]


# ---- api ---------------------------------------------------------------------------------


class Api:
    """The tracker's own API over the same log: current state as the product holds it, and its audit log."""

    def __init__(self, data: Path, tenant: str) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.changelog: dict[str, list[dict[str, Any]]] = defaultdict(list)
        links: dict[tuple[str, str, str], bool] = {}
        latest: dict[tuple[str, str], tuple[str, str]] = {}  # (key, field) -> (occurred, recorded) of the value shown
        for e in sorted(source(data, tenant), key=lambda e: (e["recorded_at"], e["id"])):  # the product writes what it learns, in order
            key, p = e["subject"].split(":", 1)[1], e["payload"]
            entry = {"happened": when(e["occurred_at"]), "recorded": when(e["recorded_at"])}
            if e["type"] == "issue.created":
                self.issues[key] = {"key": key, "created": when(e["occurred_at"]), **{k: p[k] for k in ("summary", "issue_type", "status", "priority", "assignee", "resolution", "reporter", "parent")}}
                # the creation carries the initial values, as Jira's history shows them: without it, a value before the
                # first change cannot be read
                self.changelog[key].append({**entry, "field": "created", "initial": {k: p[k] for k in ("status", "issue_type", "priority", "assignee", "resolution")}})
            elif e["type"] in ("issue.linked", "issue.unlinked"):
                links[(key, p["relation"], p["other"])] = e["type"] == "issue.linked"
                self.changelog[key].append({**entry, "field": "link", "to" if e["type"] == "issue.linked" else "from": f"{p['relation'].replace('_', ' ')} {p['other']}"})
            else:
                field = next(k for k in ("status", "assignee", "priority", "resolution", "issue_type") if k in p)
                self.changelog[key].append({**entry, "field": field, "to": p[field]})
                stamp = (datetime.fromisoformat(e["occurred_at"]).isoformat(), datetime.fromisoformat(e["recorded_at"]).isoformat())
                if key in self.issues and stamp >= latest.get((key, field), ("", "")):  # a late change never hides a newer one
                    latest[(key, field)] = stamp
                    self.issues[key][field] = p[field]
        for (src, rel, dst), live in links.items():
            if live:
                self.issues.get(src, {}).setdefault("links", []).append(f"{rel.replace('_', ' ')} {dst}")
                if dst in self.issues:
                    inverse = {"blocks": "is blocked by", "depends_on": "is depended on by", "duplicates": "is duplicated by", "clones": "is cloned by"}
                    self.issues[dst].setdefault("links", []).append(f"{inverse.get(rel, rel.replace('_', ' '))} {src}")

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name == "get_issue":
            return self.issues.get(str(args.get("key", "")).upper()) or {"error": "no such issue"}
        if name == "get_changelog":
            key = str(args.get("key", "")).upper()
            if key not in self.issues:
                return {"error": "no such issue"}
            return [c for c in self.changelog[key] if not args.get("field") or c["field"] == args["field"]]
        if name == "search_issues":
            hits = [i for i in self.issues.values()
                    if all(str(i.get(f) or "").casefold() == str(args[f]).casefold() for f in ("status", "assignee", "priority", "issue_type") if args.get(f))
                    and (not args.get("text") or str(args["text"]).casefold() in i["summary"].casefold())]
            field = args.get("field")
            if field and (args.get("was") is not None or args.get("on")):  # JQL's `<field> WAS <value> ON <instant>`
                at = instant(args.get("on"))
                hits = [i for i in hits if i["created"] <= when(at.isoformat()) and norm(self.was(i["key"], field, at)) == norm(args.get("was"))]
            if field and (args.get("changed_from") or args.get("changed_until")):  # JQL's `<field> CHANGED DURING (<from>, <until>)`
                lo, hi = instant(args.get("changed_from") or "1970-01-01"), instant(args.get("changed_until") or "2100-01-01")
                hits = [i for i in hits if any(c["field"] == field and lo <= instant(c["happened"]) < hi for c in self.changelog[i["key"]])]
            return {"total": len(hits), "issues": [{k: i[k] for k in ("key", "summary", "status")} for i in hits[: int(args.get("limit") or 20)]]}
        return {"error": f"unknown tool {name}"}

    def was(self, key: str, field: str, at: datetime) -> Any:
        """A field's value at an instant, from the audit log as it stands now: the initial value, then the last change
        that happened by then (a correction, recorded later for the same instant, wins it)."""
        value, best = None, None
        for c in self.changelog[key]:
            stamp = (instant(c["happened"]), instant(c["recorded"]))
            if stamp[0] > at:
                continue
            if c["field"] == "created":
                v = c["initial"].get(field)
            elif c["field"] == field:
                v = c["to"]
            else:
                continue
            if best is None or stamp >= best:
                value, best = v, stamp
        return value


def instant(s: Any) -> datetime:
    """An instant as agents write them: ISO 8601, or "2018-06-12 14:00 UTC" as the questions do; UTC when unzoned."""
    text = str(s).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    at = datetime.fromisoformat(text)
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


API_TOOLS = [{"type": "function", "function": f} for f in (
    {"name": "get_issue", "description": "An issue's current fields (status, assignee, priority, resolution, type, summary) and its current links.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}},
    {"name": "get_changelog", "description": "An issue's audit log, oldest write first: its creation with the initial values, then each change with its field, its new value (to), when it happened and when it was recorded.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "field": {"type": "string", "description": "status, assignee, priority, resolution, issue_type or link"}}, "required": ["key"]}},
    {"name": "search_issues", "description": ("Issues by current status, assignee, priority, type and words in the summary; returns the total and the "
                                               "first `limit` (default 20). History filters, as JQL's WAS and CHANGED: `field` with `was` and `on` keeps "
                                               "issues whose field had that value at that instant (was=null: empty); `field` with `changed_from` and "
                                               "`changed_until` keeps issues whose field changed in that window."),
     "parameters": {"type": "object", "properties": {"text": {"type": "string"}, "status": {"type": "string"}, "assignee": {"type": "string"},
                                                     "priority": {"type": "string"}, "issue_type": {"type": "string"}, "limit": {"type": "integer"},
                                                     "field": {"type": "string", "description": "status, assignee, priority, resolution or issue_type"},
                                                     "was": {"type": ["string", "null"]}, "on": {"type": "string", "description": "an instant, ISO 8601"},
                                                     "changed_from": {"type": "string"}, "changed_until": {"type": "string"}}}},
)]


# ---- one question --------------------------------------------------------------------------


def ask(gw: Gateway, data: Path, system: str, model: str, q: dict[str, Any], rags: dict[str, Rag], apis: dict[str, Api], max_steps: int = 15) -> dict[str, Any]:
    tenant = q["tenant"]
    if system == "tgms-pev":
        return ask_pev(gw, data, model, q)
    instance, project = INSTANCES[tenant]
    fmt = FORMATS["value" if q["answer_type"] == "unknown" else q["answer_type"]]  # an abstention looks like any value question
    tools_text = TOOLS_TEXT[system]
    server = keel_server(data, tenant) if system == "keel" else None
    if server is not None:
        tools_text += "\n\n" + mcp_server.instructions(keel_cartridge())
    prompt = SYSTEM.format(project=project, instance=instance, today=datetime.now().strftime("%Y-%m-%d"), tools=tools_text, fmt=fmt)
    if system == "tgms":
        from evals.jira import tgms_system

        tools_text = tgms_system.TOOLS_TEXT
        prompt = SYSTEM.format(project=project, instance=instance, today=datetime.now().strftime("%Y-%m-%d"), tools=tools_text, fmt=fmt)
    tools = {"keel": keel_tools(data, tenant) if server else None, "rag": RAG_TOOLS, "api": API_TOOLS, "closed": None,
             "graphiti": MEMORY_TOOLS, "mem0": MEMORY_TOOLS, "tgms": None}[system]
    if system == "tgms":
        tools = tgms_system.tools()
    messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}, {"role": "user", "content": q["question"]}]
    used, cost, calls, errors, reply, start = {"input": 0, "output": 0}, 0.0, [], 0, "", time.monotonic()
    for _ in range(max_steps):
        msg, u, c = gw.chat(model, messages, tools)
        used = {k: used[k] + u[k] for k in used}
        cost += c
        turn = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            turn["tool_calls"] = [{"id": t["id"], "type": "function", "function": {"name": t["function"]["name"], "arguments": t["function"].get("arguments") or "{}"}}
                                  for t in msg["tool_calls"]]
        messages.append(turn)
        if not msg.get("tool_calls"):
            reply = turn["content"]
            break
        for t in turn["tool_calls"]:
            name = t["function"]["name"]
            try:
                args = json.loads(t["function"]["arguments"])
            except ValueError:
                args = {}
            from evals.jira.memories import backoff, delay, transient

            for wait in backoff():  # a rate limit or gateway outage under a search is waited out, never shown to the agent
                error = False
                try:
                    if system == "keel":
                        if KEEL_DETAIL and name in ("context", "history", "search"):
                            args = args | {"detail": KEEL_DETAIL}
                        text, _, error, _ = call(server, name, args)
                    elif system == "rag":
                        text = json.dumps(rags[tenant].search(gw, str(args.get("query", "")), args.get("issue"), min(int(args.get("k") or 15), 50)))
                    elif system == "tgms":
                        text = tgms_system.call(data, tenant, name, args)
                    elif system in ("graphiti", "mem0"):
                        from evals.jira import memories

                        k = min(int(args.get("k") or 15), 50)
                        hits = (memories.graphiti_search(tenant, EXTRACTOR, str(args.get("query", "")), k) if system == "graphiti"
                                else memories.mem0_search(data, tenant, EXTRACTOR, str(args.get("query", "")), k))
                        text = json.dumps(hits, ensure_ascii=False)
                    else:
                        text = json.dumps(apis[tenant].call(name, args), ensure_ascii=False)
                except Exception as exc:  # noqa: BLE001 - a bad argument is the agent's to read, as a real tool would say it
                    if wait and transient(exc):
                        time.sleep(delay(exc, wait))
                        continue
                    text, error = json.dumps({"error": f"{type(exc).__name__}: {exc}"}), True
                break
            errors += bool(error)
            calls.append({"tool": name, "args": args, "tokens": tokens(text), "error": bool(error)})
            messages.append({"role": "tool", "tool_call_id": t["id"], "content": text})
    else:
        reply = "(stopped after the step limit)"
    answer, parsed = extract(reply)
    return {"id": q["id"], "tenant": tenant, "type": q["type"], "system": system, "model": model, "gold": q["answer"],
            "answer": answer, "parsed": parsed, "correct": parsed and grade(q, answer), "reply": reply[-2000:], "calls": calls,
            **({"keel_max_list": KEEL_MAX_LIST} if system == "keel" and KEEL_MAX_LIST else {}),
            **({"keel_rules": False} if system == "keel" and not KEEL_RULES else {}),
            **({"keel_detail": KEEL_DETAIL} if system == "keel" and KEEL_DETAIL else {}), "route": gw.route(model),
            "tool_errors": errors, "turns": sum(m["role"] == "assistant" for m in messages), "tokens": used,
            "cost_usd": round(cost, 6), "ms": round((time.monotonic() - start) * 1000)}


def ask_pev(gw: Gateway, data: Path, model: str, q: dict[str, Any]) -> dict[str, Any]:
    """TGMS through its planner, plan verifier and executor (tgms_system.ask_pev) instead of a tool loop."""
    from evals.jira import tgms_system

    used, cost, start = {"input": 0, "output": 0}, [0.0], time.monotonic()

    def chat(messages: list[dict[str, Any]]) -> str:
        msg, u, c = gw.chat(model, messages, None)
        used.update({k: used[k] + u[k] for k in used})
        cost[0] += c
        text = msg.get("content") or ""
        # TGMS's planner parses the reply as bare JSON, as its guided decoding guarantees on the models it was built
        # with; an API model may say a sentence first, so the plan object is cut out of the reply (nothing else changes)
        a, b = text.find("{"), text.rfind("}")
        return text[a : b + 1] if 0 <= a < b else text

    answer, info = tgms_system.ask_pev(chat, data, model, q)
    return {"id": q["id"], "tenant": q["tenant"], "type": q["type"], "system": "tgms-pev", "model": model, "gold": q["answer"],
            "answer": answer, "parsed": answer is not None, "correct": answer is not None and grade(q, answer), "reply": json.dumps(info),
            "calls": [{"tool": op} for op in info["plan_ops"] or []], "route": gw.route(model), "tool_errors": int(info["error"] is not None),
            "turns": info["llm_calls"], "tokens": used, "cost_usd": round(cost[0], 6), "ms": round((time.monotonic() - start) * 1000)}


# ---- grading --------------------------------------------------------------------------------

NONE = {"none", "null", "unassigned", "unresolved", "no one", "nobody", ""}


def extract(reply: str) -> tuple[Any, bool]:
    m = list(re.finditer(r"ANSWER:\s*(.+)", reply))
    if not m:
        return None, False
    raw = m[-1].group(1).strip().strip("`").strip()
    try:
        return json.loads(raw), True
    except ValueError:
        return raw.strip('"'), True


def norm(v: Any) -> Any:
    if v is None or (isinstance(v, str) and v.strip().casefold() in NONE):
        return None
    return v.strip().casefold() if isinstance(v, str) else v


def grade(q: dict[str, Any], answer: Any) -> bool:
    gold, kind = q["answer"], q["answer_type"]
    if kind == "unknown":  # abstaining, in any wording that does not invent a value
        return norm(answer) in (None, "unknown")
    if kind == "set":
        # a key as Keel writes it ("issue:MXS-12") names the same issue: the answer is right in either form
        return isinstance(answer, list) and {str(a).upper().strip().removeprefix("ISSUE:") for a in answer} == {g.upper() for g in gold}
    if kind == "count":
        try:
            return int(str(answer).strip()) == gold
        except ValueError:
            return False
    if kind == "bool":
        b = {"true": True, "yes": True, "false": False, "no": False}.get(str(answer).strip().casefold()) if not isinstance(answer, bool) else answer
        return b is gold
    return norm(answer) == norm(gold)


# ---- the run ------------------------------------------------------------------------------


def main() -> None:
    global SUBSET, EXTRACTOR, KEEL_MAX_LIST, KEEL_RULES, KEEL_DETAIL
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--systems", default="keel,rag,api,closed")
    ap.add_argument("--models", default="openai/gpt-6-luna,anthropic/claude-haiku-5.5")
    ap.add_argument("--tenants", default=",".join(INSTANCES))
    ap.add_argument("--types", help="only these question types")
    ap.add_argument("--per-type", type=int, help="only the first N questions of each type per tenant (a pilot)")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--subset", action="store_true", help="every system reads only the issues the questions touch")
    ap.add_argument("--extractor", default=EXTRACTOR, help="the model Graphiti and Mem0 ingested with")
    ap.add_argument("--keel-max-list", type=int, default=200,
                    help="Keel's listing ceiling (keel serve --max-list), the full run's setting (plan B); 0: none, the ablation")
    ap.add_argument("--keel-no-rules", action="store_true", help="the cartridge declares no rules: the agent applies the definition itself")
    ap.add_argument("--keel-detail", choices=["full"], help="force every context/history/search to this detail (the brief/full ablation)")
    ap.add_argument("--ids", help="only these question ids (comma-separated)")
    ap.add_argument("--max-usd", type=float, help="stop starting runs once this invocation has spent this much")
    a = ap.parse_args()
    SUBSET, EXTRACTOR, KEEL_MAX_LIST = a.subset, a.extractor, a.keel_max_list
    KEEL_RULES, KEEL_DETAIL = not a.keel_no_rules, a.keel_detail
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data = a.data.expanduser()
    gw = Gateway()
    questions = []
    for tenant in a.tenants.split(","):
        qs = json.loads((data / f"{tenant}.questions.json").read_text())
        if a.types:
            qs = [q for q in qs if q["type"] in a.types.split(",")]
        if a.ids:
            qs = [q for q in qs if q["id"] in a.ids.split(",")]
        if a.per_type:
            qs = [q for q in qs if int(q["id"].rsplit("-", 1)[1]) <= a.per_type]
        questions += qs
    systems, models = a.systems.split(","), a.models.split(",")
    rags = {t: Rag(data, t, gw) for t in {q["tenant"] for q in questions}} if "rag" in systems else {}
    apis = {t: Api(data, t) for t in {q["tenant"] for q in questions}} if "api" in systems else {}
    done = set()
    if a.out.exists():
        done = {(r["id"], r["system"], r["model"], r.get("rep", 0)) for r in map(json.loads, a.out.open())}
    jobs = [(q, s, m, r) for r in range(a.reps) for m in models for s in systems for q in questions if (q["id"], s, m, r) not in done]
    print(f"{len(jobs)} runs ({len(done)} already in {a.out})")
    lock = Lock()
    spent = [0.0]
    vers = versions()
    print(f"keel {vers['commit'][:10]}{' (uncommitted changes)' if vers['dirty'] else ''}")

    def run(job: tuple[dict[str, Any], str, str, int]) -> None:
        q, s, m, r = job
        if a.max_usd is not None and spent[0] >= a.max_usd:
            return  # over budget: the rest is left for a rerun to pick up
        from evals.jira.memories import OUTAGES, backoff, delay, transient

        for wait in backoff():  # a gateway outage (429, or its upstream account down for a while) waits it out
            try:
                res = ask(gw, data, s, m, q, rags, apis) | {"rep": r}
                break
            except Exception as exc:  # noqa: BLE001 - a run that ends in an error is a wrong answer, recorded with its error
                if wait and transient(exc):
                    time.sleep(delay(exc, wait))
                    continue
                print(f"  {q['id']} {s} {m}: {type(exc).__name__}: {str(exc)[:300]}")
                if any(o in str(exc) for o in OUTAGES) or transient(exc):
                    return  # the gateway or the network was down, not the system: left out, so a rerun asks it again
                res = {"id": q["id"], "tenant": q["tenant"], "type": q["type"], "system": s, "model": m, "gold": q["answer"], "answer": None,
                       "parsed": False, "correct": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}", "reply": "", "calls": [],
                       "tool_errors": 0, "turns": 0, "tokens": {"input": 0, "output": 0}, "cost_usd": 0.0, "ms": 0, "rep": r}
                break
        res["versions"] = vers
        with lock, a.out.open("a") as fh:
            fh.write(json.dumps(res, ensure_ascii=False, default=str) + "\n")
            spent[0] += res["cost_usd"]
            if a.max_usd is not None and spent[0] >= a.max_usd:
                print(f"  budget reached: {spent[0]:.2f} USD of {a.max_usd:.2f}; the remaining runs are left for a rerun")

    with ThreadPoolExecutor(a.workers) as pool:
        list(pool.map(run, jobs))
    summarize(a.out)


def versions() -> dict[str, Any]:
    """What a row was measured with: the repo's commit (Keel, harness and questions), whether keel/ or evals/ had
    uncommitted changes (results aside), and the versions of the packages the systems run on."""
    from importlib.metadata import PackageNotFoundError, version

    root = Path(__file__).resolve().parents[2]
    git = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True, text=True).stdout.strip()
    out: dict[str, Any] = {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "keel", "evals", ":!evals/jira/results"))}
    for pkg in ("graphiti-core", "mem0ai", "tgms", "anthropic", "openai"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            pass
    return out


def per_question(runs: list[dict[str, Any]]) -> dict[str, float]:
    """Accuracy per question, its repetitions averaged: the reps of one question are not independent draws."""
    by: dict[str, list[bool]] = defaultdict(list)
    for r in runs:
        by[r["id"]].append(bool(r["correct"]))
    return {q: sum(v) / len(v) for q, v in by.items()}


def bootstrap(values: list[float], b: int = 10_000, seed: int = 0) -> tuple[float, float, float, float]:
    """Mean, 95% percentile interval and two-sided p (of the mean being 0, for paired differences) by resampling
    questions with replacement; seeded, so a summary is reproducible."""
    v = np.asarray(values, dtype=float)
    means = v[np.random.default_rng(seed).integers(0, len(v), (b, len(v)))].mean(axis=1)
    p = min(1.0, 2 * min((means <= 0).mean(), (means >= 0).mean()))
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(v.mean()), float(lo), float(hi), float(p)


def summarize(*paths: Path) -> None:
    runs = [json.loads(line) for path in paths for line in path.open()]
    table: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        table[(r["model"], r["system"])].append(r)
    print(f"{'model':32} {'system':7} {'n':>4} {'q':>4} {'acc':>5} {'95% CI':>13} {'parsed':>6} {'calls':>5} {'in tok':>7} {'usd':>7}")
    for (m, s), rs in sorted(table.items()):
        n, acc = len(rs), per_question(rs)
        _, lo, hi, _ = bootstrap(list(acc.values()))
        print(f"{m:32} {s:7} {n:4} {len(acc):4} {sum(acc.values()) / len(acc):5.2f} [{lo:4.2f}, {hi:4.2f}] "
              f"{sum(r['parsed'] for r in rs) / n:6.2f} {sum(len(r['calls']) for r in rs) / n:5.1f} "
              f"{sum(r['tokens']['input'] for r in rs) / n:7.0f} {sum(r['cost_usd'] for r in rs):7.3f}")
    print("paired, Keel minus each system, same agent, questions both answered:")
    for (m, s), rs in sorted(table.items()):
        if s == "keel" or (m, "keel") not in table:
            continue
        k, o = per_question(table[(m, "keel")]), per_question(rs)
        both = sorted(k.keys() & o.keys())
        if both:
            d, lo, hi, p = bootstrap([k[q] - o[q] for q in both])
            print(f"{m:32} {s:7} {len(both):4} {d:+5.2f} [{lo:+4.2f}, {hi:+4.2f}] p={p:.3f}")
    by_type: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for r in runs:
        by_type[(r["system"], r["type"])].append(bool(r["correct"]))
    types = sorted({t for _, t in by_type})
    print("accuracy by type: " + " ".join(f"{t:>9}" for t in types))
    for s in sorted({s for s, _ in by_type}):
        print(f"{s:17} " + " ".join(f"{sum(by_type[(s, t)]) / len(by_type[(s, t)]):9.2f}" if by_type[(s, t)] else f"{'-':>9}" for t in types))


if __name__ == "__main__":
    if sys.argv[1:2] == ["summarize"]:  # summarize results.jsonl [more.jsonl ...]: several runs pooled
        summarize(*map(Path, sys.argv[2:]))
    else:
        main()
