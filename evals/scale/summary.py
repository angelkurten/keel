"""results/scale.jsonl -> results/summary.md: the tables of the scalability evaluation.

    uv run python -m evals.scale.harness summary
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.scale.harness import OPS, TENANT_OPS


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.open() if line.strip()]


def _last(rows: list[dict[str, Any]], **match: Any) -> dict[str, Any] | None:
    hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    return hits[-1] if hits else None


def _f(x: Any, d: int = 1) -> str:
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:,.{d}f}"
    if isinstance(x, int):
        return f"{x:,}"
    return str(x)


def _ms(v: dict[str, Any] | None, key: str) -> str:
    if not v or not v.get("n"):
        return "–"
    x = v.get(key)
    return "–" if x is None else (f"{x:.3f}" if x < 1 else f"{x:,.1f}" if x < 1000 else f"{x:,.0f}")


def _table(head: list[str], body: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(" --- " for _ in head) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(out)


def _labels(rows: list[dict[str, Any]], prefix: str) -> list[str]:
    seen = dict.fromkeys(r["label"] for r in rows if str(r.get("label", "")).startswith(prefix) and r.get("kind") in ("one", "duck"))
    return list(seen)


def write(results: Path, md: Path) -> str:
    rows = _rows(results)
    parts = ["# Keel scalability evaluation: results", ""]
    m = next((r["machine"] for r in rows if "machine" in r), None)
    if m:
        parts += [(f"Machine: {m['cpu']}, {m['cores']} cores, {m['ram_gb']} GB RAM, {m['os']}; Python {m['python']}, SQLite {m['sqlite']}, "
                  f"DuckDB {m.get('duckdb')}; Keel {m.get('keel_version')} at {m['keel_commit'][:12]}{' (keel/ dirty)' if m['keel_dirty'] else ''}."), ""]
    # ingest, replay, size
    body = []
    for prefix in ("real:", "syn:", "tenants:", "per_subject:"):
        for label in _labels(rows, prefix):
            for store in ("memory", "sqlite"):
                ing = _last(rows, kind="one", label=label, store=store, phase="ingest")
                rep = _last(rows, kind="one", label=label, store=store, phase="replay")
                stop = _last(rows, kind="one", label=label, store=store, phase="timeout") or _last(rows, kind="one", label=label, store=store, phase="not_run") \
                    or _last(rows, kind="one", label=label, store=store, phase="failed")
                if not ing and not stop:
                    continue
                note = ""
                if stop and (not rep or stop.get("at", "") > rep.get("at", "")):
                    note = stop.get("reason") or f"{stop['phase']} after {_f(stop.get('wall_s'), 0)} s"
                ing = ing or {}
                body.append([label, store, _f(ing.get("events")), _f(ing.get("ingest_s")), _f(ing.get("events_per_s"), 0),
                             _f(rep.get("replay_s")) if rep else "–", _f(rep.get("replay_events_per_s"), 0) if rep else "–",
                             _f(ing.get("bytes_per_event_disk"), 0), _f(ing.get("bytes_per_event_rss"), 0),
                             _f(ing.get("peak_rss_bytes", 0) / 2**30, 2) if ing else "–", note])
    parts += ["## Ingest throughput, replay time, store size", "",
              ("Ingest: the log read from .jsonl and applied in batches of 5,000 (`keel ingest`'s path), parsing included. Replay: "
              "`Ingestor.replay` of the measured tenant over the built graph (wipe, then rebuild). Disk: the SQLite file after a WAL "
              "checkpoint. RSS: resident memory after ingest minus before (the whole process: store, interpreter caches)."), "",
              _table(["log", "store", "events", "ingest s", "events/s", "replay s", "replay ev/s", "disk B/event", "RSS B/event", "peak RSS GiB", "note"], body), ""]
    # latency
    parts += ["## Per-tool latency (ms): p50 / p95 / p99", "",
              ("In-process Tools calls (context, history, search, related) and store primitives (facts_*), interleaved in a seeded order. "
              "Subject ops: up to 200 calls each; tenant-wide ops: up to 10 (3 at 1e7), each op stopped once its calls used 60 s. "
              "DuckDB: the equivalent read on the bitemporal table (– where there is none)."), ""]
    for label in _labels(rows, "real:") + _labels(rows, "syn:") + _labels(rows, "tenants:") + _labels(rows, "per_subject:"):
        q = {s: _last(rows, kind="one", label=label, store=s, phase="queries") for s in ("memory", "sqlite")}
        d = _last(rows, kind="duck", label=label)
        if not any(q.values()) and not d:
            continue
        lat = {s: (r or {}).get("latency", {}) for s, r in q.items()}
        lat["duckdb"] = (d or {}).get("latency", {})
        body = []
        for op in OPS:
            cells = []
            for s in ("memory", "sqlite", "duckdb"):
                v = lat[s].get(op)
                cells.append(" / ".join(_ms(v, k) for k in ("p50_ms", "p95_ms", "p99_ms")) + (f" (n={v['n']})" if v and v.get("n") and v["n"] < 10 else ""))
            body.append([op, *cells])
        mix = [f"{s}: {_f(q[s].get('mix_mean_ms'), 2)} ms" for s in ("memory", "sqlite") if q[s]]
        parts += [f"### {label}", "", _table(["op", "MemoryStore", "SqliteStore", "DuckDB"], body), "",
                  f"Weighted benchmark mix, mean: {', '.join(mix)}." if mix else "", ""]
    # tenant-wide reads pushed down to the store, beside the Python path they replace
    names = {"memory": "MemoryStore", "sqlite": "SqliteStore", "duckdb": "DuckdbStore", "postgres": "PostgresStore"}
    tables = []
    for label in _labels(rows, "pushdown:"):
        base = label.removeprefix("pushdown:")
        cols = [(f"{names[s]}, Python", _last(rows, kind="one", label=base, store=s, phase="queries")) for s in ("memory", "sqlite")]
        cols += [(f"{names[s]}, push-down", _last(rows, kind="one", label=label, store=s, phase="queries")) for s in ("sqlite", "duckdb", "postgres")]
        cols = [(name, r["latency"]) for name, r in cols if r]
        body = [[op, *(" / ".join(_ms(lat.get(op), k) for k in ("p50_ms", "p95_ms"))
                       + (f" (n={lat[op]['n']})" if lat.get(op) and lat[op].get("n", 10) < 10 else "") for _, lat in cols)] for op in TENANT_OPS]
        tables += [f"### {base}", "", _table(["op", *(name for name, _ in cols)], body), ""]
    if tables:
        loads = [f"{r['label']} {names.get(r['store'], r['store'])}: {_f(r.get('load_s'), 1)} s" for r in rows
                 if str(r.get("label", "")).startswith("pushdown:") and r.get("phase") == "load" and r.get("load_s") is not None]
        parts += ["## Tenant-wide reads pushed down to the store (ms): p50 / p95", "",
                  ("The same seeded tenant-wide ops, answered by the store's optional capabilities (facts_between, subjects_where, "
                   "rule_subjects) instead of Tools' Python sweep; tests/test_pushdown.py and evals/jira/test_pushdown.py check the "
                   "outputs are identical. SqliteStore: plain indexed SQL, rules still in Python. DuckdbStore and PostgresStore hold "
                   "the rows Keel's SqliteStore ingest wrote, copied as stored (`one --load-from`)."
                   + (" Copied in: " + "; ".join(loads) + "." if loads else "")), "", *tables]
    # duckdb load
    body = []
    for label in _labels(rows, "real:") + _labels(rows, "syn:"):
        d = _last(rows, kind="duck", label=label)
        sq = _last(rows, kind="one", label=label, store="sqlite", phase="ingest")
        if d:
            body.append([label, _f(d.get("events")), _f(d["load_s"], 2), _f(d["rows"]), _f(d["db_bytes"] / max(d.get("events") or 1, 1), 0),
                         _f(sq["ingest_s"], 1) if sq else "–", _f(sq["bytes_per_event_disk"], 0) if sq else "–"])
    if body:
        parts += ["## DuckDB baseline: load and size", "",
                  ("DuckDB reads the .jsonl, derives valid_to/closed_at with one window function and indexes (tenant, subject); facts only, "
                  "no links, no rules, no dedupe, no incremental maintenance."), "",
                  _table(["log", "events", "DuckDB load s", "rows", "DuckDB B/event", "SqliteStore ingest s", "SqliteStore B/event"], body), ""]
    # determinism
    det = [r for r in rows if r.get("kind") == "determinism"]
    if det:
        body = []
        for r in det:
            for k, h in r["hashes"].items():
                body.append([r["label"], k, _f(h["n_facts"]), _f(h["n_relations"]), h["facts"][:12], h["relations"][:12], _f(h.get("relation_rows_differing")),
                             _f(h["ingest_s"], 1)])
        parts += ["## Determinism: the log in written order and shuffled", "",
                  "Seeded shuffle of the whole log; graph hashed without the ingest clock. `same` per part across every build: "
                  + "; ".join(f"{r['label']}: {r['same']}, replay rebuilds the same facts: {r['replay_same_facts']}" for r in det), "",
                  _table(["log", "build", "facts", "relations", "facts hash", "relations hash", "relation rows differing", "ingest s"], body), ""]
    http = _last(rows, kind="http")
    if http:
        body = [[op, *(" / ".join(_ms(http[side].get(op), k) for k in ("p50_ms", "p99_ms")) for side in ("in_process", "http"))] for op in http["http"]]
        parts += [f"## Transport: in-process vs `keel serve` over HTTP ({http['label']}, SqliteStore), p50 / p99 ms", "", _table(["op", "in-process", "HTTP"], body), ""]
    stops = [r for r in rows if r.get("phase") in ("timeout", "not_run", "failed")]
    if stops:
        parts += ["## Where it stopped", ""] + [f"- {r['label']} {r.get('store', '')}: {r['phase']} {r.get('reason', '')} {_f(r.get('wall_s'), 0)} s" for r in stops] + [""]
    text = "\n".join(parts)
    md.write_text(text)
    return text
