"""What the kagent pilot of technical instructions asked for, inside the five tools (rule 7):
search results that carry their facts and neighbours, the best of each kind for a task, batches,
related over several hops, hidden predicates, and a finer semantic index; then, from the analysis
of its calls, search by a fact's value, a document per list element and why each result matched;
then, from its second round, kind as a list and a faster semantic search; then, from its 41 CUs, the
evidence of many subjects (`only`) and many names checked in one call."""

from datetime import datetime, timedelta, timezone

import pytest

from keel import Event, Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.ingest import JsonlAdapter
from keel.semantic import SPLIT, HashEmbedder, MemoryIndex, index_tenant

FLEET, EVENTS, TENANT = "cartridges/fleet", "examples/events/fleet.jsonl", "acme-logistics"
UTC = timezone.utc
STAMPS = {"recorded_at", "as_of", "closed_at", "watermark"}


def _strip(o):
    if isinstance(o, dict):
        return {k: _strip(v) for k, v in o.items() if k not in STAMPS}
    if isinstance(o, list):
        return [_strip(v) for v in o]
    return o


@pytest.fixture(params=["memory", "sqlite"])
def tools(request, tmp_path):
    c = load_cartridge(FLEET)
    s = MemoryStore() if request.param == "memory" else SqliteStore(tmp_path / "k.db")
    Ingestor(c, s).ingest(JsonlAdapter(EVENTS), strict=True)
    return Tools(c, s, TENANT)


def _unit_with_tires(store, tires=20):
    c = load_cartridge(FLEET)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    evs = [Event(tenant=TENANT, type="unit.registered", subject="unit:U1", occurred_at=base, source="b", payload={"id": "U1", "plate": "P", "model": "X"})]
    for n in range(tires):
        for k, depth in enumerate((8.0, 2.0 + n % 5)):  # an old reading, then the current one
            evs.append(Event(tenant=TENANT, type="tire.depth_measured", subject=f"tire:T{n}", occurred_at=base + timedelta(days=30 * k, hours=n), source="b", payload={"unit_id": "U1", "position": f"P{n}", "depth_mm": depth}))
    Ingestor(c, store).ingest(evs, strict=True)
    return Tools(c, store, TENANT), base


# ---- 1. search results carry their facts --------------------------------------------------


def test_search_results_carry_their_current_facts(tools):
    plain = tools.search("T-9001")
    assert "facts" not in plain[0]  # the default answer is unchanged
    concise = tools.search("T-9001", detail="concise")
    assert concise[0]["key"] == "tire:T-9001" and concise[0]["facts"]["depth_mm"] == 2.6
    full = tools.search("T-9001", detail="full")
    assert [f["predicate"] for f in full[0]["facts"]] == ["depth_mm"] and full[0]["facts"][0]["value"] == 2.6
    listed = tools.search(kind="tire", rule="tire_critical", detail="concise")
    assert listed[0]["facts"] == {"depth_mm": 2.6}
    with pytest.raises(ValueError):
        tools.search("T-9001", detail="everything")


def test_search_depth_brings_the_neighbours_and_their_facts(tools):
    [unit] = tools.search(kind="unit", depth=1, detail="concise")
    near = {n["subject"]: n for n in unit["neighbors"]}
    assert near["tire:T-9001"]["relation"] == "mounted_on" and near["tire:T-9001"]["direction"] == "in"
    assert near["tire:T-9001"]["facts"] == {"depth_mm": 2.6}
    [bare] = tools.search(kind="unit", depth=1)
    assert "facts" not in bare and all("facts" not in n for n in bare["neighbors"])


def test_enriched_search_reads_in_batches(tmp_path):
    """Facts and neighbours for a page of results: a few statements, whatever the page's size."""

    def statements(tires):
        store = SqliteStore(tmp_path / f"s{tires}.db")
        t, _ = _unit_with_tires(store, tires)
        n, orig = [0], store._rows
        store._rows = lambda sql, params=(): (n.__setitem__(0, n[0] + 1), orig(sql, params))[1]
        assert len(t.search(kind="tire", detail="concise", depth=1)) == tires
        return n[0]

    assert statements(10) == statements(30)


# ---- 2. batches ---------------------------------------------------------------------------


def test_context_and_search_take_a_list(tools):
    subjects = ["tire:T-9001", "unit:MXK-4821"]
    assert _strip(tools.context(subjects, depth=1)) == [_strip(tools.context(s, depth=1)) for s in subjects]
    texts = ["T-9001", "MXK"]
    assert tools.search(texts, detail="concise") == [tools.search(t, detail="concise") for t in texts]


# ---- 3. the context for a task: the best of each kind -------------------------------------


def test_per_kind_keeps_the_best_of_every_kind(tools):
    every = tools.search("MXK-4821")
    kinds = {h.get("kind") or h["subject"].split(":")[0] for h in every}
    one = tools.search("MXK-4821", per_kind=1)
    got = [h.get("kind") or h["subject"].split(":")[0] for h in one]
    assert sorted(got) == sorted(kinds) and len(got) == len(set(got))
    many, _ = _unit_with_tires(MemoryStore(), 6)
    assert len([h for h in many.search("tire:T") if h.get("kind") == "tire"]) == 6
    assert [h["kind"] for h in many.search("tire:T", per_kind=2)] == ["tire", "tire"]  # cut, not the whole ranking
    with pytest.raises(ValueError):
        tools.search("MXK", kind="tire", per_kind=1)  # one kind: that is `limit`
    with pytest.raises(ValueError):
        tools.search("MXK", per_kind=1, after="tire:T-9001")


def test_per_kind_in_semantic_mode_is_the_context_for_a_task(tools):
    emb, idx = HashEmbedder(), MemoryIndex()
    index_tenant(tools.store, TENANT, emb, idx)
    t = Tools(tools.cartridge, tools.store, TENANT, embedder=emb, index=idx)
    pack = t.search("tire tread unit MXK-4821", mode="semantic", per_kind=2, related_to="unit:MXK-4821", detail="concise")
    assert pack and all(h["key"] in {n["subject"] for n in t.related("unit:MXK-4821")} for h in pack)
    by_kind: dict[str, int] = {}
    for h in pack:
        by_kind[h["kind"]] = by_kind.get(h["kind"], 0) + 1
        assert "facts" in h
    assert max(by_kind.values()) <= 2 and len(by_kind) > 1


# ---- 4. related over several hops ---------------------------------------------------------


def test_related_walks_several_hops_through_the_relations_asked(tmp_path):
    t, _ = _unit_with_tires(SqliteStore(tmp_path / "r.db"), 5)
    one = t.related("tire:T0")
    assert one == [{"relation": "mounted_on", "direction": "out", "subject": "unit:U1", "since": one[0]["since"]}]  # depth 1 unchanged
    two = t.related("tire:T0", relation=["mounted_on"], depth=2)
    assert [(e["subject"], e["hops"], e["via"]) for e in two[:1]] == [("unit:U1", 1, "tire:T0")]
    assert sorted(e["subject"] for e in two if e["hops"] == 2) == [f"tire:T{n}" for n in range(1, 5)]
    assert all(e["via"] == "unit:U1" and e["direction"] == "in" for e in two if e["hops"] == 2)
    assert t.related("tire:T0", relation="assigned_to", depth=3) == []
    with pytest.raises(ValueError):
        t.related("tire:T0", depth=4)


# ---- 6. hide data in a query --------------------------------------------------------------


def test_exclude_hides_predicates_but_rules_still_see_the_truth(tools):
    ctx = tools.context("tire:T-9001", exclude=["depth_mm"])
    assert ctx["facts"] == [] and ctx["evaluation"]["tire_critical"] is True
    unit = tools.context("unit:MXK-4821", depth=1, exclude=["depth_mm"])
    assert all(f["predicate"] != "depth_mm" for n in unit["neighbors"] for f in n["facts"])
    assert any(h.get("predicate") == "depth_mm" for h in tools.search("2.6"))
    assert not any(h.get("predicate") == "depth_mm" for h in tools.search("2.6", exclude=["depth_mm"]))
    [tire] = tools.search("T-9001", detail="concise", exclude=["depth_mm"])
    assert tire["facts"] == {}


def test_search_as_of_evaluates_rules_then(tmp_path):
    t, base = _unit_with_tires(SqliteStore(tmp_path / "a.db"), 5)
    now_critical = {h["key"] for h in t.search(kind="tire", rule="tire_critical")}
    assert now_critical == {"tire:T0"}  # 2.0 mm now; the rest read 3.0 and up
    assert t.search(kind="tire", rule="tire_critical", as_of=base + timedelta(days=1)) == []  # all read 8.0 then
    [then] = t.search("T0", detail="concise", as_of=base + timedelta(days=1))
    assert then["facts"]["depth_mm"] == 8.0


# ---- 5. a finer semantic index ------------------------------------------------------------


def test_split_documents_rank_their_subject_and_follow_value_changes(tmp_path):
    store = SqliteStore(tmp_path / "split.db")
    t0, base = _unit_with_tires(store, 3)
    store.open_fact(TENANT, "unit:U1", "field", "Ventas netas por region", valid_from=base, source="b")
    store.open_fact(TENANT, "unit:U1", "note", "a long text that dilutes everything else in the document", valid_from=base, source="b")
    policy = {"split": {"unit": ["field"]}}
    emb, idx = HashEmbedder(), MemoryIndex()
    first = index_tenant(store, TENANT, emb, idx, policy=policy)
    keys = set(idx._data[TENANT])
    split_keys = {k for k in keys if SPLIT in k}
    assert len(split_keys) == 1 and first.indexed == len({k for k in keys if SPLIT not in k})
    t = Tools(t0.cartridge, store, TENANT, embedder=emb, index=idx)
    [hit] = t.search("ventas netas por region", mode="semantic", kind="unit", limit=1)
    assert hit["key"] == "unit:U1" and hit["matched"] == "field"
    assert len({h["key"] for h in t.search("ventas netas", mode="semantic", limit=10)}) == len(t.search("ventas netas", mode="semantic", limit=10))
    store.open_fact(TENANT, "unit:U1", "field", "Margen bruto", valid_from=base + timedelta(days=1), source="b")
    index_tenant(store, TENANT, emb, idx, since=first.cursor, policy=policy)
    now = {k for k in idx._data[TENANT] if SPLIT in k}
    assert len(now) == 1 and now != split_keys  # the old value's document is gone


def test_the_cartridge_checks_split():
    c = load_cartridge(FLEET)
    c.semantic = {"split": {"tire": ["depth_mm"]}}
    assert not [p for p in c.validate() if "split" in p]
    c.semantic = {"split": {"spaceship": ["x"]}}
    assert any("split kind 'spaceship'" in p for p in c.validate())
    c.semantic = {"split": ["tire"]}
    assert any("split maps a kind" in p for p in c.validate())


# ---- 6. from the analysis of the pilot's calls ---------------------------------------------


def _pages(store):
    t, base = _unit_with_tires(store, 3)
    store.open_fact(TENANT, "tire:T0", "page", "Detalhe Ociosidade", valid_from=base, source="b")
    store.open_fact(TENANT, "tire:T1", "page", "Resumo", valid_from=base, source="b")
    store.open_fact(TENANT, "unit:U1", "field", ["Ventas netas por region", "Margen bruto", "Tipo de ticket"], valid_from=base, source="b")
    return t, base


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_where_finds_an_entity_by_the_value_of_a_fact(kind, tmp_path):
    store = MemoryStore() if kind == "memory" else SqliteStore(tmp_path / "w.db")
    t, base = _pages(store)
    assert [h["key"] for h in t.search(kind="tire", where={"page": "detalhe ociosidade"})] == ["tire:T0"]  # without case
    assert [h["key"] for h in t.search(kind="unit", where={"field": "Margen bruto"})] == ["unit:U1"]  # an element of a list
    assert t.search(kind="tire", where={"page": "Detalhe"}) == []  # the value, not a part of it
    assert {h.get("subject", h.get("key")) for h in t.search("Resumo", where={"page": "Resumo"})} == {"tire:T1"}
    assert [h["key"] for h in t.search(kind="tire", where={"page": "Resumo", "position": "P1"})] == ["tire:T1"]  # an attribute or a fact
    store.open_fact(TENANT, "tire:T0", "page", "Overview", valid_from=base + timedelta(days=1), source="b")
    assert t.search(kind="tire", where={"page": "Detalhe Ociosidade"}) == []  # current facts only
    assert [h["key"] for h in t.search(kind="tire", where={"page": "Detalhe Ociosidade"}, as_of=base + timedelta(hours=12))] == ["tire:T0"]
    with pytest.raises(ValueError, match="exclude hides"):
        t.search(kind="tire", where={"page": "Resumo"}, exclude=["page"])
    with pytest.raises(ValueError):
        t.search(kind="tire", where={})


def test_text_over_a_kind_reads_the_facts_and_each_result_says_what_matched(tmp_path):
    store = SqliteStore(tmp_path / "m.db")
    t, base = _pages(store)
    [hit] = t.search("ociosidade", kind="tire")
    assert hit["key"] == "tire:T0" and hit["matched"] == "page" and hit["snippet"] == "Detalhe Ociosidade"
    assert t.search("ociosidade", kind="tire", exclude=["page"]) == []  # a hidden fact matches nothing
    [unit] = t.search("margen", kind="unit")
    assert unit["matched"] == "field" and unit["snippet"] == "Margen bruto"  # the element, not the whole list
    assert t.search("U1", kind="unit")[0]["matched"] == "key" and "snippet" not in t.search("U1", kind="unit")[0]
    store.open_fact(TENANT, "tire:T2", "note", "x" * 400 + " needle " + "y" * 400, valid_from=base, source="b")
    [long] = t.search("needle", kind="tire")
    assert "needle" in long["snippet"] and long["snippet"].startswith("…") and long["snippet"].endswith("…") and len(long["snippet"]) <= 162
    assert "matched" not in t.search(kind="tire")[0]  # a listing without text matched nothing


def test_split_indexes_each_element_of_a_list_and_quotes_the_one_that_ranked(tmp_path):
    store = SqliteStore(tmp_path / "s.db")
    t0, base = _pages(store)
    policy = {"split": {"unit": ["field"]}}
    emb, idx = HashEmbedder(), MemoryIndex()
    first = index_tenant(store, TENANT, emb, idx, policy=policy)
    assert len([k for k in idx._data[TENANT] if SPLIT in k]) == 3  # one document per element
    t = Tools(t0.cartridge, store, TENANT, embedder=emb, index=idx)
    [hit] = t.search("margen bruto", mode="semantic", kind="unit", limit=1)
    assert hit["key"] == "unit:U1" and hit["matched"] == "field" and hit["snippet"] == "Margen bruto"
    [page] = t.search("detalhe ociosidade", mode="semantic", kind="tire", limit=1)  # not split: the field sharing most words
    assert page["key"] == "tire:T0" and page["matched"] == "page" and page["snippet"] == "Detalhe Ociosidade"
    [hidden] = t.search("margen bruto", mode="semantic", kind="unit", limit=1, exclude=["field"])
    assert "matched" not in hidden and "snippet" not in hidden
    assert [h["key"] for h in t.search("detalhe", mode="semantic", kind="tire", where={"page": "Resumo"})] == ["tire:T1"]
    store.open_fact(TENANT, "unit:U1", "field", ["Ventas netas por region"], valid_from=base + timedelta(days=1), source="b")
    index_tenant(store, TENANT, emb, idx, since=first.cursor, policy=policy)
    assert len([k for k in idx._data[TENANT] if SPLIT in k]) == 1  # the dropped elements' documents are gone


# ---- 7. the second round: kind as a list, a faster semantic search -------------------------


def test_kind_takes_a_list(tmp_path):
    store = SqliteStore(tmp_path / "k.db")
    t, _ = _pages(store)
    keys = lambda hits: [h["key"] for h in hits]
    assert keys(t.search(kind=["unit", "tire"])) == ["tire:T0", "tire:T1", "tire:T2", "unit:U1"]  # still by key
    assert keys(t.search(kind=["unit", "tire"], limit=2)) == ["tire:T0", "tire:T1"]
    assert keys(t.search(kind=["unit", "tire"], after="tire:T1")) == ["tire:T2", "unit:U1"]
    assert keys(t.search("Resumo", kind=["tire", "unit"])) == ["tire:T1"] and keys(t.search("margen", kind=["tire", "unit"])) == ["unit:U1"]
    assert keys(t.search("", kind=["tire", "unit"], per_kind=1)) == ["tire:T0", "unit:U1"]  # the first of each
    for bad in ({"kind": []}, {"kind": ["tire", "unit"], "after": "T1"}, {"kind": "tire", "per_kind": 1, "text": "T"}):
        with pytest.raises(ValueError):
            t.search(**bad)
    emb, idx = HashEmbedder(), MemoryIndex()
    index_tenant(store, TENANT, emb, idx)
    s = Tools(t.cartridge, store, TENANT, embedder=emb, index=idx)
    assert {h["kind"] for h in s.search("margen ociosidade resumo", mode="semantic", kind=["unit"])} == {"unit"}
    assert {h["kind"] for h in s.search("margen ociosidade resumo", mode="semantic", kind=["unit", "tire"])} == {"unit", "tire"}


def test_semantic_per_kind_embeds_once_and_scopes_related_to_inside_the_index(tmp_path):
    store = SqliteStore(tmp_path / "p.db")
    t, _ = _pages(store)
    queries, asked = [], []

    class Embedder(HashEmbedder):
        def embed(self, texts, role="document"):
            queries.extend(texts if role == "query" else [])
            return super().embed(texts, role)

    class Index(MemoryIndex):
        def query(self, tenant, vector, k, where=None):
            asked.append(where)
            return super().query(tenant, vector, k, where)

    emb, idx = Embedder(), Index()
    index_tenant(store, TENANT, emb, idx)
    s = Tools(t.cartridge, store, TENANT, embedder=emb, index=idx)
    pack = s.search("tire depth", mode="semantic", per_kind=2, related_to="unit:U1")
    assert queries == ["tire depth"]  # one embedding for every kind
    assert len(pack) == 2 and {h["key"] for h in pack} < {"tire:T0", "tire:T1", "tire:T2"} and all(w["subject"] == ["tire:T0", "tire:T1", "tire:T2"] for w in asked)
    store.upsert_entity(TENANT, "unit", "U2")
    asked.clear()
    assert s.search("tire depth", mode="semantic", related_to="unit:U2") == [] and asked == []  # no neighbours: no query


def test_the_lexical_leg_is_built_once_until_the_index_changes():
    idx = MemoryIndex()
    idx.upsert(TENANT, [("tire:T1", [1.0], "worn tread"), ("tire:T2", [1.0], "slow leak"), ("unit:U1\x1fp\x1fh", [1.0], "leak report")])
    first = idx.text_query(TENANT, "leak", 5)
    built = idx._bm25._c[(TENANT, None)][2]
    assert first[0][0] == "tire:T2" and [k for k, _ in idx.text_query(TENANT, "tread", 5)] == ["tire:T1"] and idx._bm25._c[(TENANT, None)][2] is built
    assert [k for k, _ in idx.text_query(TENANT, "leak", 5, where={"subject": ["unit:U1"]})] == ["unit:U1\x1fp\x1fh"]  # a split document is its subject's
    idx.upsert(TENANT, [("tire:T3", [1.0], "leak again")])
    assert "tire:T3" in [k for k, _ in idx.text_query(TENANT, "leak", 5)]  # a write drops the cache


# ---- 8. from the 41 CUs: the evidence a reviewer reads, many names at once -----------------


def _counting(store):
    n, orig = [0], store._rows
    store._rows = lambda sql, params=(): (n.__setitem__(0, n[0] + 1), orig(sql, params))[1]
    return n


def test_context_only_returns_just_those_predicates_for_many_subjects(tmp_path):
    def statements(tires):
        store = SqliteStore(tmp_path / f"o{tires}.db")
        t, _ = _pages(store) if tires == 3 else _unit_with_tires(store, tires)
        n = _counting(store)
        got = t.context([f"tire:T{i}" for i in range(tires)], only=["page"], detail="concise")
        return n[0], got

    few, got = statements(3)
    assert [c["facts"] for c in got] == [{"page": "Detalhe Ociosidade"}, {"page": "Resumo"}, {}]  # depth_mm left out
    assert all("tire_critical" in c["evaluation"] for c in got)  # rules still see the whole truth
    assert statements(30)[0] == few  # a few statements, whatever the number of subjects
    store = SqliteStore(tmp_path / "x.db")
    t, _ = _pages(store)
    one = t.context("tire:T0", only=["page", "depth_mm"], exclude=["depth_mm"])
    assert [f["predicate"] for f in one["facts"]] == ["page"]  # exclude still hides
    with pytest.raises(ValueError):
        t.context("tire:T0", only=[])


def test_many_names_in_one_search_read_each_subject_once(tmp_path):
    def statements(names):
        store = SqliteStore(tmp_path / f"n{len(names)}.db")
        t, _ = _pages(store)
        n = _counting(store)
        got = t.search(names, kind=["tire", "unit"], related_to="unit:U1")
        return n[0], got

    few, got = statements(["Resumo", "ociosidade", "Margen bruto", "no such name"])
    assert [[(h["key"], h["matched"]) for h in hits] for hits in got] == [[("tire:T1", "page")], [("tire:T0", "page")], [], []]
    more, _ = statements(["Resumo", "ociosidade", "Margen bruto", "no such name"] * 5)
    assert more - few < 5 * 4  # the facts are read once; each extra name costs at most its neighbour lookup
