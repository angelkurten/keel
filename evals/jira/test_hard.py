"""The hard question slice (evals/jira/hard.py): two hops, counts as recorded, re-entries, time in a status, two instants.

    uv run --extra mcp pytest evals/jira/test_hard.py -q

Each oracle answer is checked by hand on a small log with a late arrival, a correction and a link that ends; then the
generated questions on that log are answered with Keel's primitives (hard.check) and must agree with the oracle.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evals.jira import hard
from evals.jira.questions import Oracle
from evals.jira.test_sql import created, ev

D = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
IP = {"status": "In Progress", "status_category": "indeterminate", "by": None}
OPEN = {"status": "Open", "status_category": "new", "by": None}

LOG = [
    created("P-1", "2020-01-01T00:00:00"),
    created("P-2", "2020-01-01T00:00:00"),
    created("P-3", "2020-01-01T00:00:00", parent="P-1"),
    created("P-4", "2020-01-01T00:00:00"),
    created("P-5", "2020-01-01T00:00:00", parent="P-4"),
    created("P-6", "2020-01-01T00:00:00"),
    # P-1: in progress Jan 10 (recorded only Jan 20), open Jan 15, in progress Feb 1, then on Feb 10 first recorded
    # as Closed and corrected to Resolved on Feb 12
    ev("jira:t:P-1:h1", "issue.status_changed", "P-1", "2020-01-10T00:00:00", IP, recorded="2020-01-20T00:00:00"),
    ev("jira:t:P-1:h2", "issue.status_changed", "P-1", "2020-01-15T00:00:00", OPEN),
    ev("jira:t:P-1:h3", "issue.status_changed", "P-1", "2020-02-01T00:00:00", IP),
    ev("jira:t:P-1:h4:wrong", "issue.status_changed", "P-1", "2020-02-10T00:00:00", {"status": "Closed", "status_category": "done", "by": None}),
    ev("jira:t:P-1:h4", "issue.status_changed", "P-1", "2020-02-10T00:00:00", {"status": "Resolved", "status_category": "done", "by": None},
       recorded="2020-02-12T00:00:00"),
    # P-2 in progress from Jan 5 on
    ev("jira:t:P-2:h1", "issue.status_changed", "P-2", "2020-01-05T00:00:00", IP),
    # P-4 blocks P-1; P-6 relates to P-4 from Jan 1 to Jan 20
    ev("jira:t:P-4:l1", "issue.linked", "P-4", "2020-01-01T00:00:00", {"relation": "blocks", "other": "P-1"}),
    ev("jira:t:P-6:l1", "issue.linked", "P-6", "2020-01-01T00:00:00", {"relation": "relates_to", "other": "P-4"}),
    ev("jira:t:P-6:l2", "issue.unlinked", "P-6", "2020-01-20T00:00:00", {"relation": "relates_to", "other": "P-4"}),
]
LOG.sort(key=lambda e: (e["recorded_at"], e["id"]))
O = Oracle(LOG)
SUB, BLOCKERS, PARENT, RELATED = ("subtask_of", "in"), ("blocks", "in"), ("subtask_of", "out"), ("relates_to", "any")


def test_two_hops_follow_both_relations_at_the_instant():
    assert hard.two_hop(O, "issue:P-1", BLOCKERS, SUB, D("2020-03-01")) == ["issue:P-5"]
    assert hard.two_hop(O, "issue:P-3", PARENT, BLOCKERS, D("2020-03-01")) == ["issue:P-4"]
    assert hard.two_hop(O, "issue:P-1", BLOCKERS, RELATED, D("2020-01-05")) == ["issue:P-6"]
    assert hard.two_hop(O, "issue:P-1", BLOCKERS, RELATED, D("2020-02-01")) == []  # the link ended Jan 20
    assert "issue:P-1" not in hard.two_hop(O, "issue:P-1", BLOCKERS, ("blocks", "out"), D("2020-03-01"))  # never the asked issue


def test_counts_as_recorded_see_late_arrivals_and_corrections_as_they_stood():
    assert hard.known_count(O, "In Progress", D("2020-01-12"), D("2020-01-15")) == 1  # P-2 only: P-1's change not recorded yet
    assert hard.known_count(O, "In Progress", D("2020-01-12"), D("2020-01-25")) == 2
    assert hard.known_count(O, "Closed", D("2020-02-11"), D("2020-02-11")) == 1  # the wrong record
    assert hard.known_count(O, "Closed", D("2020-02-11"), D("2020-02-13")) == 0


def test_entries_count_moves_into_a_status_never_the_creation():
    assert hard.entries(O, "issue:P-1", "In Progress", D("2020-01-01"), D("2020-03-01")) == 2
    assert hard.entries(O, "issue:P-1", "In Progress", D("2020-01-11"), D("2020-03-01")) == 1
    assert hard.entries(O, "issue:P-1", "Open", D("2019-12-01"), D("2020-03-01")) == 1  # created Open: not a move
    assert hard.entries(O, "issue:P-1", "Closed", D("2020-01-01"), D("2020-03-01")) == 0  # corrected away


def test_time_in_a_status_adds_every_spell_as_now_known():
    assert hard.time_in(O, "issue:P-1", "In Progress", D("2020-03-01")) == timedelta(days=14)  # Jan 10-15, Feb 1-10
    assert hard.time_in(O, "issue:P-1", "Open", D("2020-03-01")) == timedelta(days=26)  # Jan 1-10, Jan 15-Feb 1
    assert hard.time_in(O, "issue:P-1", "In Progress", D("2020-02-05")) == timedelta(days=9)  # a spell still open
    assert hard.time_in(O, "issue:P-1", "Closed", D("2020-03-01")) == timedelta(0)


def test_both_instants_keeps_issues_in_progress_at_each():
    assert hard.both(O, D("2020-01-12"), D("2020-02-05")) == ["issue:P-1", "issue:P-2"]
    assert hard.both(O, D("2020-01-12"), D("2020-01-16")) == ["issue:P-2"]


def test_generated_questions_have_gold_keel_reaches():
    qs = hard.generate(LOG, [], seed=1, counts={k: 3 for k in hard.TYPES})
    assert qs and all(q["type"] in hard.TYPES and q["answer_type"] in ("set", "count") for q in qs)
    assert len({q["id"] for q in qs}) == len(qs) and len({q["question"] for q in qs}) == len(qs)
    assert hard.check(LOG, qs) == 0


HERE = Path(__file__).parent / "hard"
MANIFEST = json.loads((HERE / "manifest.json").read_text())


def test_committed_questions_match_the_manifest():
    for t, info in MANIFEST["tenants"].items():
        assert hashlib.sha256((HERE / f"{t}.questions.json").read_bytes()).hexdigest() == info["questions_sha256"], t


@pytest.mark.parametrize("tenant", sorted(MANIFEST["tenants"]))
def test_questions_regenerate_from_the_data_and_keel_reaches_every_gold(tenant):
    data = Path(MANIFEST["tenants"][tenant]["data"]).expanduser()
    if not (data / f"{tenant}.bitemporal.jsonl").exists():
        pytest.skip("the tenant's data is not on disk")
    events = [json.loads(line) for line in (data / f"{tenant}.bitemporal.jsonl").open()]
    qs = hard.generate(events, json.loads((data / f"{tenant}.injections.json").read_text()), MANIFEST["seed"], MANIFEST["counts"])
    assert json.dumps(qs, ensure_ascii=False, indent=1) == (HERE / f"{tenant}.questions.json").read_text()
    assert hard.check(events, qs) == 0
