"""Tenant extensions (keel/extensions.py): additive, declarative cartridge fragments one tenant layers on the base."""

import copy
from datetime import datetime, timezone

import pytest

from keel import Event, Ingestor, MemoryStore, Tools, load_cartridge
from keel.extensions import for_tenant, problems, put_extension

UTC = timezone.utc
CRM = "examples/crm"

EXT = {
    "extends": "crm@0.1.0",
    "version": 1,
    "entities": {"deal": {"description": "A deal in the CRM.", "facts": {"amount": "number", "stage": "string"}}},
    "relations": {"deal_for": {"from": "deal", "to": "customer", "one": True}},
    "events": {
        "deal.observed": {
            "subject": "deal",
            "owner": "source:hubspot",
            "payload": {"amount": "number", "stage": "string", "customer_id": "string | null"},
            "facts": ["amount", "stage"],
            "relate": {"deal_for": "customer:{customer_id}"},
        }
    },
    "rules": {"big_deal": {"applies_to": "deal", "definition": "A deal over 1000.", "when": {"predicate": "amount", "gt": 1000}}},
}


def _deal(i, tenant="org-1", **payload):
    return Event(id=f"d{i}", tenant=tenant, type="deal.observed", subject="deal:D1", occurred_at=datetime(2026, 10, i, tzinfo=UTC),
                 source="hubspot", payload={"amount": 5000, "stage": "open", "customer_id": "c1", **payload})


@pytest.fixture
def base():
    return load_cartridge(CRM)


def test_an_extension_reaches_only_its_tenant(base):
    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    ing = Ingestor(base, s)
    r = ing.ingest([_deal(1), _deal(2, tenant="org-2")])
    assert r.processed == 1 and r.unhandled == ["deal.observed"]  # org-2 has no such event
    t = Tools(base, s, "org-1")
    ctx = t.context("deal:D1")
    assert {f["predicate"]: f["value"] for f in ctx["facts"]} == {"amount": 5000, "stage": "open"}
    assert [r["dst"] for r in ctx["relations"] if r["rel"] == "deal_for"] == ["customer:c1"]
    assert "deal" in for_tenant(base, s, "org-1").entities and "deal" not in for_tenant(base, s, "org-2").entities
    with pytest.raises(ValueError):
        Tools(base, s, "org-2").context("deal:D1")  # an unknown kind there
    assert "deal" not in base.entities  # the base is never changed


def test_an_extension_rule_is_read_by_the_tools(base):
    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    Ingestor(base, s).ingest([_deal(1)], strict=True)
    assert "big_deal" in Tools(base, s, "org-1").context("deal:D1")["rules"]


def test_replay_keeps_the_extension(base):
    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    ing = Ingestor(base, s)
    log = [_deal(1), _deal(2, amount=7000)]
    ing.ingest(log, strict=True)
    before = [(f.predicate, f.value) for f in s.facts("org-1", "deal:D1")]
    ing.replay("org-1", events=log)
    assert [(f.predicate, f.value) for f in s.facts("org-1", "deal:D1")] == before


def _problems(base, change, previous=None):
    ext = copy.deepcopy(EXT)
    change(ext)
    return problems(base, ext, previous)


def test_validation(base):
    assert problems(base, EXT) == []
    assert any("collides" in p for p in _problems(base, lambda e: e["entities"].update(customer={"description": "x"})))
    assert any("collides" in p for p in _problems(base, lambda e: e["events"].update({"payment.received": e["events"]["deal.observed"]})))
    assert any("declarative" in p for p in _problems(base, lambda e: [e["events"]["deal.observed"].pop(k) for k in ("facts", "relate")]))
    assert any("forgets" in p for p in _problems(base, lambda e: e["events"]["deal.observed"].update(forgets="tenant")))
    assert any("extends" in p for p in _problems(base, lambda e: e.update(extends="fleet@1")))
    assert any("unknown key 'agents'" in p for p in _problems(base, lambda e: e.update(agents={})))
    assert any("version" in p for p in _problems(base, lambda e: e.pop("version")))
    # the merged cartridge is validated too: a relation to a kind nobody declares
    assert any("deal_for" in p for p in _problems(base, lambda e: e["relations"]["deal_for"].update(to="ghost")))


def test_versions_are_monotone(base):
    later = lambda change: _problems(base, lambda e: (e.update(version=2), change(e)), previous=EXT)
    assert later(lambda e: None) == []
    assert any("version" in p for p in _problems(base, lambda e: None, previous=EXT))  # not increasing
    assert any("'deal.observed'" in p for p in later(lambda e: e["events"].clear()))  # an event cannot go
    assert any("'deal.observed'" in p for p in later(lambda e: e["events"]["deal.observed"].update(facts=["amount"])))  # nor change
    assert any("'deal_for'" in p for p in later(lambda e: e["relations"]["deal_for"].update(one=False)))
    assert any("'deal'" in p for p in later(lambda e: e["entities"].clear()))  # retire it instead
    assert later(lambda e: e["rules"]["big_deal"].update(definition="Over 1000, open.")) == []  # reads only: may change


def test_a_retired_kind_is_hidden_but_still_ingested(base):
    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    put_extension(base, s, "org-1", {**EXT, "version": 2, "retired": ["deal"]})
    r = Ingestor(base, s).ingest([_deal(1)], strict=True)
    assert r.processed == 1 and s.facts("org-1", "deal:D1")  # history goes on, so a replay rebuilds it
    c = for_tenant(base, s, "org-1")
    assert "deal" not in c.entities and "deal_for" not in c.relations and "big_deal" not in c.rules
    with pytest.raises(ValueError):
        Tools(base, s, "org-1").context("deal:D1")
    assert any("'ghost'" in p for p in problems(base, {**EXT, "retired": ["ghost"]}))


def test_put_extension_refuses_what_is_invalid(base):
    s = MemoryStore()
    with pytest.raises(ValueError, match="collides"):
        put_extension(base, s, "org-1", {**EXT, "entities": {"customer": {}}})
    assert s.extension("org-1") is None
    put_extension(base, s, "org-1", EXT)
    with pytest.raises(ValueError, match="version"):
        put_extension(base, s, "org-1", EXT)  # the same version again


def test_limits(base):
    many = {f"k{i}": {"description": "x"} for i in range(51)}
    assert any("at most 50" in p for p in problems(base, {**EXT, "entities": {**EXT["entities"], **many}}))


def test_a_stored_agent_may_read_an_extension_kind(tmp_path):
    import shutil

    from keel.agents import put_agent

    shutil.copytree(CRM, tmp_path / "crm")
    y = tmp_path / "crm" / "cartridge.yaml"
    y.write_text(y.read_text() + "\nregistry: store\nagents:\n  reader:\n    description: Reads.\n    writes: []\n")
    base = load_cartridge(tmp_path / "crm")
    s = MemoryStore()
    with pytest.raises(ValueError, match="undeclared kind 'deal'"):
        put_agent(base, s, "org-1", "deals", {"extends": "reader", "reads": {"kinds": ["deal"]}})
    put_extension(base, s, "org-1", EXT)
    put_agent(base, s, "org-1", "deals", {"extends": "reader", "reads": {"kinds": ["deal"]}})
    Ingestor(base, s).ingest([_deal(1)], strict=True)
    assert Tools(base, s, "org-1", "deals").context("deal:D1")


def test_a_bulk_load_on_sqlite_applies_the_extension(base, tmp_path):
    from keel import SqliteStore

    s = SqliteStore(str(tmp_path / "k.db"))
    put_extension(base, s, "org-1", EXT)
    r = Ingestor(base, s).ingest([_deal(1), _deal(2, customer_id="c2"), _deal(3, stage="won")], strict=True, batch=10)
    assert r.processed == 3
    assert {f.predicate: f.value for f in s.facts("org-1", "deal:D1")} == {"amount": 5000, "stage": "won"}
    at = lambda d: [x.dst for x in s.relations("org-1", "deal:D1", "deal_for", at=datetime(2026, 10, d, 12, tzinfo=UTC))]
    assert (at(1), at(2), at(3)) == (["customer:c1"], ["customer:c2"], ["customer:c1"])  # `one`: each closes the one before


def test_http_extension_is_managed_by_the_operator_or_an_extend_key(base):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app
    from keel.keys import create_key

    s = MemoryStore()
    plain, _ = create_key(s, "acme")
    ext_key, _ = create_key(s, "acme", tenant="org-1", extend=True)
    with pytest.raises(ValueError, match="extend"):
        create_key(s, "acme", agent="bot", extend=True)  # an agent never widens its own tenant's schema
    c = TestClient(build_http_app(base, s, secret="s3cret"))
    op = {"Authorization": "Bearer s3cret"}
    assert c.get("/v1/tenants/org-9/extension", headers=op).status_code == 404  # none yet
    r = c.put("/v1/tenants/org-9/extension", json=EXT, headers=op)
    assert r.status_code == 200 and r.json()["version"] == 1
    assert c.get("/v1/tenants/org-9/extension", headers=op).json() == EXT
    assert c.put("/v1/tenants/org-9/extension", json=EXT, headers=op).status_code == 422  # the same version again
    assert c.put("/v1/tenants/org-1/extension", json=EXT, headers={"Authorization": f"Bearer {plain}"}).status_code == 403
    key = {"Authorization": f"Bearer {ext_key}"}
    assert c.put("/v1/tenants/org-2/extension", json=EXT, headers=key).status_code == 403  # narrowed to org-1
    assert c.put("/v1/tenants/org-1/extension", json=EXT, headers=key).status_code == 200
    assert s.extension("acme/org-1")["version"] == 1  # under the key's account, like every keyed tenant
    ev = {**_deal(1).model_dump(mode="json"), "tenant": "org-1"}
    assert c.post("/v1/events", json=ev, headers=key).status_code == 200
    got = c.post("/v1/context", json={"subject": "deal:D1"}, headers=key).json()
    assert got["facts"] == {"amount": 5000, "stage": "open"} and got["evaluation"] == {"big_deal": True}


def test_a_base_upgrade_that_collides_keeps_the_base_and_validate_reports_it(base, tmp_path):
    import shutil

    from typer.testing import CliRunner

    from keel import SqliteStore
    from keel.cli import app

    db = str(tmp_path / "k.db")
    put_extension(base, SqliteStore(db), "org-1", EXT)
    shutil.copytree(CRM, tmp_path / "crm")
    y = tmp_path / "crm" / "cartridge.yaml"
    y.write_text(y.read_text().replace("entities:\n", "entities:\n  deal:\n    description: The base's own deal.\n", 1))
    upgraded = load_cartridge(tmp_path / "crm")
    c = for_tenant(upgraded, SqliteStore(db), "org-1")
    assert c.entities["deal"] == {"description": "The base's own deal."}  # an extension never alters the base
    r = CliRunner().invoke(app, ["validate", str(tmp_path / "crm"), "--db", db])
    assert r.exit_code == 1 and "tenant 'org-1': extension: entity 'deal' collides with the base cartridge" in r.output


def test_cli_extension_set_get_validate(base, tmp_path):
    import json

    import yaml
    from typer.testing import CliRunner

    from keel.cli import app

    db, f = str(tmp_path / "k.db"), tmp_path / "ext.yaml"
    f.write_text(yaml.safe_dump(EXT))
    run = lambda *a: CliRunner().invoke(app, list(a))
    assert run("extension", "validate", CRM, str(f)).exit_code == 0
    assert run("extension", "set", CRM, str(f), "--tenant", "org-1", "--db", db).exit_code == 0
    got = run("extension", "get", "--tenant", "org-1", "--db", db)
    assert json.loads(got.output) == EXT
    r = run("extension", "set", CRM, str(f), "--tenant", "org-1", "--db", db)
    assert r.exit_code != 0 and "version" in r.output  # the same version again
    events = tmp_path / "ev.jsonl"
    events.write_text(_deal(1).model_dump_json() + "\n")
    assert run("validate", CRM, "--events", str(events)).exit_code == 1  # deal.observed is not the base's
    r = run("validate", CRM, "--events", str(events), "--extension", str(f))
    assert r.exit_code == 0, r.output


def test_python_client_puts_and_reads_the_extension(base):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.client import Client, KeelError
    from keel.http import build_http_app

    s = MemoryStore()
    keel = Client("http://testserver", "org-1", secret="s3cret", http_client=TestClient(build_http_app(base, s, secret="s3cret")))
    assert keel.put_extension(EXT)["version"] == 1
    assert keel.extension() == EXT
    with pytest.raises(KeelError) as err:
        keel.put_extension(EXT)
    assert err.value.status == 422


@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_two_tenants_may_use_one_name_differently(base, tmp_path, kind):
    """Each extension's names are its own: the store's compiled rules are shared, so a tenant with an extension is
    answered in Python (same answers), never by another tenant's definition of the same name."""
    if kind == "duckdb":
        pytest.importorskip("duckdb")
        from keel.store.duckdb import DuckdbStore as S
    else:
        from keel import SqliteStore as S
    s = S(str(tmp_path / "k.db"))
    strict = {**EXT, "rules": {"big_deal": {**EXT["rules"]["big_deal"], "when": {"predicate": "amount", "gt": 10000}}}}
    put_extension(base, s, "org-1", EXT)
    put_extension(base, s, "org-2", strict)
    Ingestor(base, s).ingest([_deal(1), _deal(1, tenant="org-2")], strict=True)
    for _ in range(2):  # alternating, so neither tenant's definition sticks
        assert [x["key"] for x in Tools(base, s, "org-1").search(kind="deal", rule="big_deal")] == ["deal:D1"]
        assert Tools(base, s, "org-2").search(kind="deal", rule="big_deal") == []


def test_a_tenant_with_an_extension_never_installs_its_definitions_in_the_shared_store(base, tmp_path, monkeypatch):
    from keel import SqliteStore

    s = SqliteStore(str(tmp_path / "k.db"))
    put_extension(base, s, "org-1", EXT)
    Ingestor(base, s).ingest([_deal(1)], strict=True)
    calls = []
    for name in ("rule_subjects", "subjects_where"):
        monkeypatch.setattr(s, name, lambda *a, _n=name, **k: calls.append(_n))
    t = Tools(base, s, "org-1")
    assert [x["key"] for x in t.search(kind="deal", rule="big_deal")] == ["deal:D1"]
    assert [x["key"] for x in t.search(kind="deal", where={"stage": "open"})] == ["deal:D1"]
    assert calls == []  # answered in Python: concurrent tenants could otherwise replace each other's compiled names
    Tools(base, s, "org-2").search(kind="customer", where={"name": "x"})
    assert calls == ["subjects_where"]  # a tenant on the base alone keeps the push-down


@pytest.mark.parametrize("kind", ["sqlite", "duckdb"])
def test_query_reads_the_extension_kinds_of_its_tenant_only(base, tmp_path, kind):
    if kind == "duckdb":
        pytest.importorskip("duckdb")
        from keel.store.duckdb import DuckdbStore as S
    else:
        from keel import SqliteStore as S
    s = S(str(tmp_path / "k.db"))
    put_extension(base, s, "org-1", EXT)
    Ingestor(base, s).ingest([_deal(1)], strict=True)
    q = lambda t: Tools(base, s, t, query=True).query(f"SELECT subject, amount, deal_for FROM deal_state_at('{t}', keel_now())")
    assert q("org-1") == [{"subject": "deal:D1", "amount": 5000, "deal_for": "c1"}]
    with pytest.raises(ValueError, match="deal_state_at"):
        q("org-2")  # not a name there
    assert q("org-1")  # and back


# ---- from the review: each of these failed before its fix ----


def test_replay_after_retiring_a_kind_rebuilds_the_same_graph(base):
    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    ing = Ingestor(base, s)
    log = [_deal(1), _deal(2, customer_id="c2")]
    ing.ingest(log, strict=True)
    links = lambda: sorted((r.dst, r.valid_from.day, r.valid_to.day if r.valid_to else None) for r in s.dump("org-1") if getattr(r, "rel", None) == "deal_for")
    before = links()
    put_extension(base, s, "org-1", {**EXT, "version": 2, "retired": ["deal"]})
    ing.replay("org-1", events=log)
    assert links() == before == [("customer:c1", 1, 2), ("customer:c2", 2, None)]


def test_an_event_that_comes_before_its_extension_applies_when_redelivered(base):
    s = MemoryStore()
    ing = Ingestor(base, s)
    r = ing.ingest([_deal(1)])
    assert r.unhandled == ["deal.observed"] and not s.seen("org-1", "d1")  # refused before it is remembered
    put_extension(base, s, "org-1", EXT)
    assert ing.ingest([_deal(1)], strict=True).processed == 1
    assert s.facts("org-1", "deal:D1")


def test_an_extension_cannot_change_a_base_predicate_or_write_on_base_kinds(base):
    assert any("predicate 'status'" in p for p in problems(base, {**EXT, "predicates": {"status": {"multi": True}}}))
    assert problems(base, {**EXT, "predicates": {"stage": {"multi": True}}}) == []  # its own kind's fact
    onto = copy.deepcopy(EXT)
    onto["events"]["appt.seen"] = {"subject": "appointment", "owner": "x", "payload": {"status": "string"}, "facts": ["status"]}
    assert any("'appt.seen'" in p and "subject" in p for p in problems(base, onto))
    rel = copy.deepcopy(EXT)
    rel["events"]["deal.observed"]["relate"] = {"booked_by": "customer:{customer_id}"}
    assert any("'booked_by'" in p for p in problems(base, rel))


def test_a_retired_kind_is_hidden_from_every_read(base):
    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    Ingestor(base, s).ingest([_deal(1)], strict=True)
    put_extension(base, s, "org-1", {**EXT, "version": 2, "retired": ["deal"]})
    t = Tools(base, s, "org-1")
    assert all(r["rel"] != "deal_for" for r in t.context("customer:c1")["relations"])
    assert t.related("customer:c1") == []
    assert not [h for h in t.search("5000") if "deal:" in str(h)]
    assert not [f for f in t.history(since="2026-01-01T00:00:00Z") if f["subject"].startswith("deal:")]


def test_tools_built_once_see_a_new_version(base):
    s = MemoryStore()
    t = Tools(base, s, "org-1")
    put_extension(base, s, "org-1", EXT)
    Ingestor(base, s).ingest([_deal(1)], strict=True)
    assert t.current().context("deal:D1")["facts"]
    assert t.current() is t.current()  # rebuilt only when the version changed


def test_a_concurrent_version_is_refused(base):
    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    live = s.extension("org-1")
    s.put_extension("org-1", {**EXT, "version": 2}, datetime(2026, 11, 1, tzinfo=UTC), previous=live)
    with pytest.raises(ValueError, match="changed"):
        s.put_extension("org-1", {**EXT, "version": 2, "rules": {}}, datetime(2026, 11, 1, tzinfo=UTC), previous=live)


def test_the_operator_sets_an_account_tenant_over_http(base):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from keel.http import build_http_app

    s = MemoryStore()
    c = TestClient(build_http_app(base, s, secret="s3cret"))
    op = {"Authorization": "Bearer s3cret"}
    assert c.put("/v1/tenants/acme%2Fb/extension", json=EXT, headers=op).status_code == 200
    assert s.extension("acme/b")["version"] == 1
    assert c.put("/v1/tenants/__global__/extension", json=EXT, headers=op).status_code == 400


def test_a_colliding_base_upgrade_does_not_load_the_extension(base, tmp_path, caplog):
    import shutil

    s = MemoryStore()
    put_extension(base, s, "org-1", EXT)
    shutil.copytree(CRM, tmp_path / "crm")
    y = tmp_path / "crm" / "cartridge.yaml"
    y.write_text(y.read_text().replace("entities:\n", "entities:\n  deal:\n    description: The base's own deal.\n", 1))
    upgraded = load_cartridge(tmp_path / "crm")
    with caplog.at_level("WARNING"):
        c = for_tenant(upgraded, s, "org-1")
    assert c is upgraded and "deal_for" not in c.relations  # none of it, not a mix
    assert "org-1" in caplog.text and "collides" in caplog.text


def test_the_query_sandbox_follows_a_new_version(base, tmp_path):
    from keel import SqliteStore

    s = SqliteStore(str(tmp_path / "k.db"))
    put_extension(base, s, "org-1", EXT)
    Ingestor(base, s).ingest([_deal(1)], strict=True)
    sql = "SELECT subject FROM deal_state_at('org-1', keel_now())"
    assert Tools(base, s, "org-1", query=True).query(sql)
    put_extension(base, s, "org-1", {**EXT, "version": 2, "retired": ["deal"]})
    with pytest.raises(ValueError, match="deal_state_at"):
        Tools(base, s, "org-1", query=True).query(sql)
