"""A/B in one process: `search(kind="issue", where=<typed want>, detail="count")` answered by the store's
`subjects_where`, then with the push-down capabilities hidden (Tools' Python sweep: what these wants got before
the store compared them in SQL), then by the store again. Same counts every time, or it fails.

    uv run --with duckdb==2.0.0 --with psutil python -m evals.scale.where_ab --store sqlite --db <SqliteStore file> --label pushdown:syn:1000000
    uv run --with duckdb==2.0.0 --with psutil python -m evals.scale.where_ab --store duckdb --db <new file> --load-from <SqliteStore file> --label ...
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from evals.scale.harness import RESULTS, _open, copy_rows, emit, machine
from keel import Tools, load_cartridge

AS_OF = datetime(2018, 6, 1, tzinfo=timezone.utc)
OPS: dict[str, dict[str, Any]] = {  # wants the store used to decline: null, a list (any of), as of and as known at
    "where_null_at": {"where": {"assignee": None}, "as_of": AS_OF},
    "where_any_of_at": {"where": {"status": ["New", "In Progress"]}, "as_of": AS_OF},
    "where_null_known_at": {"where": {"resolution": None}, "as_of": AS_OF, "as_known_at": AS_OF + timedelta(days=14)},
}


class Plain:
    """The store with its push-down capabilities hidden (as tests/test_pushdown.py)."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name in ("facts_between", "subjects_where", "rule_subjects"):
            raise AttributeError(name)
        return getattr(self._store, name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", choices=("sqlite", "duckdb", "postgres"), required=True)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--load-from", type=Path)
    ap.add_argument("--label", required=True)
    ap.add_argument("--tenant", default="syn0")
    ap.add_argument("--out", type=Path, default=RESULTS / "scale.jsonl")
    args = ap.parse_args()
    store = _open(args.store, args.db, fresh=args.load_from is not None)
    loaded = None
    if args.load_from is not None:
        t0 = time.perf_counter()
        copy_rows(args.load_from, store)
        loaded = round(time.perf_counter() - t0, 1)
    c = load_cartridge("evals/jira")
    m = machine()
    for op, kw in OPS.items():
        row: dict[str, Any] = {"kind": "where_ab", "label": args.label, "store": args.store, "op": op,
                               "where": kw["where"], "as_of": AS_OF.isoformat()}
        counts = []
        for way, s in (("pushdown", store), ("python", Plain(store)), ("pushdown2", store)):
            t0 = time.perf_counter()
            n = Tools(c, s, args.tenant).search(kind="issue", detail="count", **kw)
            row[f"{way}_s"] = round(time.perf_counter() - t0, 3)
            counts.append(n[0]["count"])
        assert len(set(counts)) == 1, (op, counts)
        row.update(count=counts[0], same_count=True, machine=m, loaded_s=loaded,
                   note="one process: search(kind, where=typed want, detail=count) by subjects_where, then capabilities hidden "
                        "(the Python sweep these wants took before), then by subjects_where again")
        emit(args.out, row)


if __name__ == "__main__":
    main()
