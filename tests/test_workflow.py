"""The multi-agent workflow example runs end to end on every durable store and reaches the same outcome."""

import importlib.util
from pathlib import Path

import pytest

from keel import MemoryStore, SqliteStore


def _example():
    spec = importlib.util.spec_from_file_location("workflow_fleet_ops", Path("examples/workflow_fleet_ops.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("make", [MemoryStore, lambda: SqliteStore(":memory:")], ids=["memory", "sqlite"])
def test_fleet_ops_workflow(make):
    report = _example().run(make(), quiet=True)
    assert report["ranked"] == {"unit:MXK-4821": "high", "unit:TRK-1102": "high", "unit:VAN-0073": "high"}
    assert report["orders"] == ["tire:T-MXK-4821-P1"]  # the partial specialist missed the rear tire of VAN-0073
    assert report["flagged"] == ["document:POL-MXK-4821", "document:POL-VAN-0073"]
    assert report["rounds"] == 2  # the verifier sent the planner back once, for the grounded unit without an order
    assert set(report["promoted"]) == {"unit:MXK-4821", "unit:VAN-0073"} and all(";" in e or len(e) == 32 for e in report["promoted"].values())
    assert [c[1] for c in report["conflict"]] == ["annotate:tires-bot", "annotate:compliance-bot"]  # both kept, with their author
    assert report["refusals"] == ["agent 'compliance-bot' may not read kind 'ticket'"]
    assert report["triage_saw_30_days_ago"] == {"unit_grounded": False}  # before the last measurement
    assert report["export_records"] > 40
