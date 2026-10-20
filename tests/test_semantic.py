"""Semantic search: the two adapter contracts (conformance, runnable against your own adapters), the
indexer, and search(mode="semantic") with tenant, registry and rule filters.

    KEEL_VECTOR_INDEX=my_pkg.vectors:make KEEL_EMBEDDER=my_pkg.emb:make uv run pytest tests/test_semantic.py
"""

import os
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from keel import Ingestor, MemoryStore, SqliteStore, Tools, load_cartridge
from keel.agents import parse_agents
from keel.cli import app
from keel.ingest import JsonlAdapter
from keel.semantic import HashEmbedder, MemoryIndex, index_tenant, load_embedder, load_index, render

FLEET, EVENTS, TENANT = "examples/fleet", "examples/fleet/events.jsonl", "acme-logistics"


def _indexes():
    params = ["memory"]
    try:
        import chromadb  # noqa: F401

        params.append("chroma")
    except ImportError:
        pass
    if os.environ.get("KEEL_WEAVIATE_URL"):  # a running server: KEEL_WEAVIATE_URL=weaviate://localhost:8080
        params.append("weaviate")
    if os.environ.get("KEEL_VECTOR_INDEX"):
        params.append("external")
    return params


@pytest.fixture(params=_indexes())
def index(request):
    if request.param == "memory":
        return MemoryIndex()
    if request.param == "chroma":
        return load_index("chroma://")
    if request.param == "weaviate":
        url = os.environ["KEEL_WEAVIATE_URL"]
        return load_index(url + ("&" if "?" in url else "?") + "collection=KeelTest")
    return load_index(os.environ["KEEL_VECTOR_INDEX"])


@pytest.fixture
def embedder():
    return load_embedder(os.environ.get("KEEL_EMBEDDER", "hash"))


# ---- VectorIndex conformance -------------------------------------------------------------


def test_index_contract(index):
    index.clear("t-a")
    index.clear("t-b")
    assert index.query("t-a", [1.0, 0.0, 0.0], 5) == []  # empty tenant
    index.upsert("t-a", [("k:1", [1.0, 0.0, 0.0], "one"), ("k:2", [0.0, 1.0, 0.0], "two"), ("k:3", [0.7, 0.7, 0.0], "three")])
    index.upsert("t-b", [("k:9", [1.0, 0.0, 0.0], "other tenant")])
    hits = index.query("t-a", [0.9, 0.1, 0.0], 2)
    assert [k for k, _ in hits] == ["k:1", "k:3"] and hits[0][1] > hits[1][1]  # best first, k respected
    assert all(k != "k:9" for k, _ in index.query("t-a", [1.0, 0.0, 0.0], 10))  # never another tenant
    index.upsert("t-a", [("k:2", [1.0, 0.0, 0.0], "two, moved")])  # upsert replaces
    assert index.query("t-a", [1.0, 0.0, 0.0], 1)[0][1] == pytest.approx(1.0, abs=1e-3)
    index.delete("t-a", ["k:1", "k:2"])
    assert [k for k, _ in index.query("t-a", [1.0, 0.0, 0.0], 10)] == ["k:3"]
    index.clear("t-a")
    assert index.query("t-a", [1.0, 0.0, 0.0], 10) == [] and index.query("t-b", [1.0, 0.0, 0.0], 1)[0][0] == "k:9"


def test_embedder_contract(embedder):
    texts = ["tire tread depth critical", "insurance policy expiry", "tire tread depth critical"]
    vectors = embedder.embed(texts)
    assert len(vectors) == 3 and len({len(v) for v in vectors}) == 1 and len(vectors[0]) > 8
    assert vectors[0] == pytest.approx(vectors[2])  # same text, same vector, order kept


# ---- indexer and search ---------------------------------------------------------------------


@pytest.fixture
def fleet(tmp_path):
    c = load_cartridge(FLEET)
    s = SqliteStore(tmp_path / "keel.db")
    Ingestor(c, s).ingest(JsonlAdapter(EVENTS), strict=True)
    return c, s


def test_render_keeps_tenant_knowledge_out_of_private_memory(fleet):
    c, s = fleet
    t = Tools(c, s, TENANT)
    t.annotate("tire:T-9001", "scratch", "private agent thought", scope="agent")
    t.annotate("tire:T-9001", "condition", "sidewall bulge reported by the driver", scope="tenant", evidence="photo:1")
    t.annotate("tire:T-9001", "idea", "a proposal", scope="tenant", evidence="e", status="proposed")
    doc = render(s, TENANT, "tire:T-9001")
    assert doc.startswith("tire:T-9001\nposition: P3") and "depth_mm: 2.6" in doc and "sidewall bulge" in doc
    assert "private agent thought" not in doc and "a proposal" not in doc
    assert render(s, TENANT, "tire:NOPE") is None


def test_index_then_search_by_meaning_with_filters(fleet, index):
    c, s = fleet
    emb = HashEmbedder()
    report = index_tenant(s, TENANT, emb, index)
    assert report.indexed >= 7 and report.cursor is not None
    t = Tools(c, s, TENANT, embedder=emb, index=index)
    hits = t.search("tread depth", mode="semantic", limit=3)
    assert hits[0]["key"] == "tire:T-9001" and 0 < hits[0]["score"] <= 1 and "attrs" in hits[0]
    assert [h["key"] for h in t.search("tread depth", mode="semantic", kind="unit")] != []  # kind filter applies
    assert all(h["kind"] == "unit" for h in t.search("tread depth", mode="semantic", kind="unit"))
    assert [h["key"] for h in t.search("tread depth", mode="semantic", rule="tire_critical")] == ["tire:T-9001"]
    c.agents = parse_agents({"compliance-bot": {"writes": ["task"], "reads": {"kinds": ["document", "driver", "unit"], "relations": ["covers_unit"]}}})
    slice_ = Tools(c, s, TENANT, agent="compliance-bot", embedder=emb, index=index)
    assert all(h["kind"] in ("document", "driver", "unit") for h in slice_.search("tread depth", mode="semantic"))  # the registry applies
    other = Tools(load_cartridge(FLEET), s, "someone-else", embedder=emb, index=index)
    assert other.search("tread depth", mode="semantic") == []  # tenant isolation end to end


def test_incremental_indexing_follows_the_watermark(fleet, index):
    import time

    c, s = fleet
    emb = HashEmbedder()
    first = index_tenant(s, TENANT, emb, index)
    time.sleep(0.01)
    Tools(c, s, TENANT).annotate("unit:MXK-4821", "condition", "brake noise on the left side", scope="tenant", evidence="ticket:9")
    second = index_tenant(s, TENANT, emb, index, since=first.cursor)
    assert second.indexed == 1 and second.cursor > first.cursor  # only the subject that changed
    t = Tools(c, s, TENANT, embedder=emb, index=index)
    assert t.search("brake noise", mode="semantic", limit=1)[0]["key"] == "unit:MXK-4821"
    assert index_tenant(s, TENANT, emb, index, since=second.cursor).indexed == 0


def test_semantic_search_needs_configuration_and_text():
    c, s = load_cartridge(FLEET), MemoryStore()
    assert Tools(c, s, TENANT).search("x", mode="semantic") == []
    Ingestor(c, s).ingest(JsonlAdapter(EVENTS))
    note, *hits = Tools(c, s, TENANT).search("crack damage on the truck", mode="semantic")  # no embedder: by words, and it says so
    assert "not configured" in note["note"] and hits[0]["key"] == "inspection:INS-4821-10"
    with pytest.raises(ValueError, match="needs text"):
        Tools(c, s, TENANT, embedder=HashEmbedder(), index=MemoryIndex()).search("", mode="semantic")
    with pytest.raises(ValueError):
        Tools(c, s, TENANT).search("x", mode="fuzzy")


def test_your_own_adapters_load_from_a_spec(fleet):
    idx = load_index("keel.semantic:MemoryIndex")
    emb = load_embedder("keel.semantic:HashEmbedder?dim=64")
    assert isinstance(idx, MemoryIndex) and len(emb.embed(["x"])[0]) == 64  # params are strings; your factory converts


def test_cli_index_full_then_incremental(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "keel.db")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cart, events = os.path.join(root, FLEET), os.path.join(root, EVENTS)
    assert CliRunner().invoke(app, ["ingest", cart, events, "--db", db]).exit_code == 0
    chroma = "chroma" in _indexes()
    args = ["index", cart, "--tenant", TENANT, "--db", db, "--index", "chroma:///" + str(tmp_path / "vec") if chroma else "memory", "--embedder", "hash"]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0 and "mode=full" in res.output and "no cursor at" in res.output, res.output
    # the cursor lives next to a persistent index (its volume), else under .keel
    assert (tmp_path / "vec" / f"keel-index-{TENANT}.cursor" if chroma else tmp_path / ".keel" / f"index-{TENANT}.cursor").exists()
    res = CliRunner().invoke(app, args)
    assert "mode=incremental" in res.output and "subjects=0" in res.output and "no cursor" not in res.output, res.output


def test_cli_index_skips_while_another_run_holds_the_lock(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "keel.db")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cart, events = os.path.join(root, FLEET), os.path.join(root, EVENTS)
    assert CliRunner().invoke(app, ["ingest", cart, events, "--db", db]).exit_code == 0
    cursor = tmp_path / "vol" / "idx.cursor"
    cursor.parent.mkdir()
    args = ["index", cart, "--tenant", TENANT, "--db", db, "--index", "memory", "--embedder", "hash", "--cursor", str(cursor)]
    with open(str(cursor) + ".lock", "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another `keel index` is running
        res = CliRunner().invoke(app, args)
        assert res.exit_code == 0 and "skipped" in res.output and not cursor.exists(), res.output
    res = CliRunner().invoke(app, args)
    assert "mode=full" in res.output and cursor.exists(), res.output


def test_batches_embed_in_parallel_and_land_in_order(fleet):
    """An embeddings API is a round trip per batch: several at once, the same index as one by one."""
    import threading
    import time

    _, s = fleet
    live, peak, lock = [0], [0], threading.Lock()

    class Slow(HashEmbedder):
        def embed(self, texts, role="document"):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            with lock:
                live[0] -= 1
            return super().embed(texts, role)

    one, many = MemoryIndex(), MemoryIndex()
    index_tenant(s, TENANT, HashEmbedder(), one, batch=1)
    index_tenant(s, TENANT, Slow(), many, batch=1, workers=4)
    assert peak[0] > 1
    assert many._data == one._data


def test_the_embedder_retries_a_rate_limit():
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from keel.semantic import OpenAIEmbedder

    calls = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(1)
            if len(calls) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            out = _json.dumps({"data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(body["input"])]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        emb = OpenAIEmbedder(base_url=f"http://127.0.0.1:{srv.server_port}", api_key="test")
        assert emb.embed(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]] and len(calls) == 2
    finally:
        srv.shutdown()


@pytest.mark.skipif(not os.environ.get("KEEL_EMBEDDINGS_API_KEY"), reason="set KEEL_EMBEDDINGS_API_KEY (and _BASE_URL/_MODEL) for a live embedder")
def test_live_openai_compatible_embedder_finds_synonyms(fleet):
    c, s = fleet
    emb, idx = load_embedder("openai"), MemoryIndex()
    index_tenant(s, TENANT, emb, idx)
    t = Tools(c, s, TENANT, embedder=emb, index=idx)
    assert t.search("worn-out tyre", mode="semantic", limit=1)[0]["key"] == "tire:T-9001"  # no shared word with the document
    assert t.search("insurance about to lapse", mode="semantic", kind="document", limit=1)[0]["key"] == "document:POL-7731"
    assert datetime.now(timezone.utc)  # keeps the import used when skipped


# ---- 0.11.1: the cartridge chooses what is indexed; relations; filters never cut the page ----


def test_cartridge_policy_chooses_kinds_predicates_and_relations(fleet):
    c, s = fleet
    t = Tools(c, s, TENANT)
    t.annotate("tire:T-9001", "comment", "see https://jira.example/T-9001 rev 42f9", scope="tenant", evidence="jira:1")
    t.annotate("tire:T-9001", "condition", "sidewall bulge", scope="tenant", evidence="photo:1")
    policy = {"kinds": ["tire", "unit"], "exclude": ["comment"], "relations": ["mounted_on"]}
    doc = render(s, TENANT, "tire:T-9001", policy)
    assert "sidewall bulge" in doc and "jira.example" not in doc  # excluded predicate
    assert "mounted_on: unit:MXK-4821 (MXK-4821)" in doc  # the other side, with one label (default: plate before model)
    assert "mounted_on: unit:MXK-4821 (Freightliner Cascadia)" in render(s, TENANT, "tire:T-9001", {**policy, "labels": ["model"]})
    assert render(s, TENANT, "document:POL-7731", policy) is None  # not an indexed kind
    only = render(s, TENANT, "tire:T-9001", {"predicates": ["condition"], "attrs": False})
    assert only == "tire:T-9001\ncondition: sidewall bulge"
    c.semantic = {"kinds": ["spaceship"], "relations": ["flies"], "predicates": ["a"], "exclude": ["b"], "colour": 1}
    msgs = c.validate()
    for expected in ("kind 'spaceship'", "relation 'flies'", "not both", "unknown key 'colour'"):
        assert any(expected in m for m in msgs), msgs


def test_a_new_link_reindexes_both_ends(fleet, index):
    import time

    c, s = fleet
    emb, policy = HashEmbedder(), {"relations": ["in_workshop"]}
    first = index_tenant(s, TENANT, emb, index, policy=policy)
    time.sleep(0.01)
    s.upsert_entity(TENANT, "workshop", "W-99", name="Taller Norte")
    s.link(TENANT, "unit:MXK-4821", "in_workshop", "workshop:W-99", datetime.now(timezone.utc))  # no fact changed
    second = index_tenant(s, TENANT, emb, index, since=first.cursor, policy=policy)
    assert second.indexed == 2  # the unit and the workshop
    t = Tools(c, s, TENANT, embedder=emb, index=index)
    assert t.search("Taller Norte", mode="semantic", kind="unit", limit=1)[0]["key"] == "unit:MXK-4821"


def test_rule_filter_keeps_looking_past_the_first_candidates(index):
    c = load_cartridge(FLEET)
    s = MemoryStore()
    now = datetime.now(timezone.utc)
    for i in range(120):  # 120 healthy tires that match the query better than the critical one
        s.upsert_entity(TENANT, "tire", f"H-{i:03d}", position="P1")
        s.open_fact(TENANT, f"tire:H-{i:03d}", "condition", f"tread depth fine at check {i} bay {i % 7}", valid_from=now)  # distinct docs: identical vectors degrade an approximate index
        s.open_fact(TENANT, f"tire:H-{i:03d}", "depth_mm", 7.0, valid_from=now)
    s.upsert_entity(TENANT, "tire", "Z-1", position="P2")
    s.open_fact(TENANT, "tire:Z-1", "condition", "worn", valid_from=now)
    s.open_fact(TENANT, "tire:Z-1", "depth_mm", 1.5, valid_from=now)
    emb = HashEmbedder()
    index_tenant(s, TENANT, emb, index)
    t = Tools(c, s, TENANT, embedder=emb, index=index)
    hits = t.search("tread depth", mode="semantic", rule="tire_critical", limit=5)
    assert [h["key"] for h in hits] == ["tire:Z-1"]  # found far down the ranking, not cut at a fixed window



# ---- 0.12: key shortcut, hybrid, min_score, in-index filters, roles, rerankers ----------------


def _cards_box():
    from evals.semantic.sandboxes import cards

    return cards()


def test_ids_in_the_query_come_first(index):
    box = _cards_box()
    emb = HashEmbedder()
    index_tenant(box.store, box.tenant, emb, index, policy=box.policy)
    t = Tools(box.cartridge, box.store, box.tenant, embedder=emb, index=index)
    hits = t.search("what happened with CUS-1990?", mode="semantic", limit=3)
    assert hits[0]["key"] == "card:CUS-1990" and hits[0]["match"] == "key" and hits[0]["score"] == 1.0
    assert t.search("bi-857", mode="semantic", limit=1)[0]["key"] == "card:BI-857"  # case-insensitive
    assert t.search("card:CUS-1974 status", mode="semantic", limit=1)[0]["key"] == "card:CUS-1974"  # a full key too


def test_hybrid_lexical_leg_and_its_weight(index):
    box = _cards_box()
    emb = HashEmbedder()
    index_tenant(box.store, box.tenant, emb, index, policy=box.policy)
    box.cartridge.semantic = {"lexical_weight": 0.3}
    hybrid = Tools(box.cartridge, box.store, box.tenant, embedder=emb, index=index).search("British American Tobacco", mode="semantic", kind="card", limit=10)
    bat = {k for k in (e.key for e in box.store.entities(box.tenant) if e.kind == "card") if box.store.relations(box.tenant, k, rel="de_cliente")[0].dst == "cliente:BAT"}
    assert sum(h["key"] in bat for h in hybrid) >= 8  # the client's name is an exact word: the lexical leg carries it
    assert all("similarity" in h for h in hybrid)
    box.cartridge.semantic = {"lexical_weight": 0}
    dense_only = Tools(box.cartridge, box.store, box.tenant, embedder=emb, index=index).search("British American Tobacco", mode="semantic", kind="card", limit=10)
    assert len(dense_only) == 10


def test_min_score_returns_nothing_below_the_floor(fleet, index):
    c, s = fleet
    emb = HashEmbedder()
    index_tenant(s, TENANT, emb, index)
    t = Tools(c, s, TENANT, embedder=emb, index=index)
    assert t.search("zzz qqq xyzzy", mode="semantic", min_score=0.5) == []  # nothing relevant: silence
    assert t.search("tread depth", mode="semantic", min_score=0.05)  # relevant results pass
    c.semantic = {"min_score": 0.99}
    assert Tools(c, s, TENANT, embedder=emb, index=index).search("tread depth", mode="semantic") == []  # cartridge default
    assert t.search("T-9001", mode="semantic", min_score=0.99)[0]["key"] == "tire:T-9001"  # an exact id ignores the floor


def test_kind_filters_go_inside_the_index_when_it_can():
    calls = []

    class Spy(MemoryIndex):
        def query(self, tenant, vector, k, where=None):
            calls.append(where)
            return super().query(tenant, vector, k, where=where)

    c, s = load_cartridge(FLEET), MemoryStore()
    Ingestor(c, s).ingest(JsonlAdapter(EVENTS), strict=True)
    idx, emb = Spy(), HashEmbedder()
    index_tenant(s, TENANT, emb, idx)
    hits = Tools(c, s, TENANT, embedder=emb, index=idx).search("tread", mode="semantic", kind="document")
    assert calls[-1] == {"kind": ["document"]} and all(h["kind"] == "document" for h in hits)


def test_embedders_get_the_role_when_they_take_it():
    seen = []

    class Asym:
        def embed(self, texts, role="document"):
            seen.append(role)
            return HashEmbedder().embed(texts)

    class Plain:
        def embed(self, texts):
            return HashEmbedder().embed(texts)

    c, s = load_cartridge(FLEET), MemoryStore()
    Ingestor(c, s).ingest(JsonlAdapter(EVENTS), strict=True)
    for emb in (Asym(), Plain()):  # an adapter without `role` keeps working
        idx = MemoryIndex()
        index_tenant(s, TENANT, emb, idx)
        assert Tools(c, s, TENANT, embedder=emb, index=idx).search("tread", mode="semantic")
    assert seen == ["document", "query"]
    from keel.semantic import OpenAIEmbedder

    e5 = OpenAIEmbedder(query_prefix="query: ", document_prefix="passage: ")
    assert e5.prefix == {"query": "query: ", "document": "passage: "}


def test_rerankers_reorder_the_pool(fleet, index):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from keel.semantic import HTTPReranker, OverlapReranker

    c, s = fleet
    emb = HashEmbedder()
    index_tenant(s, TENANT, emb, index)
    t = Tools(c, s, TENANT, embedder=emb, index=index, reranker=OverlapReranker())
    hits = t.search("fuel load liters odometer", mode="semantic", limit=3)
    assert hits[0]["key"] == "unit:MXK-4821"  # the unit's document holds the fuel load

    class Fake(BaseHTTPRequestHandler):  # the Cohere / Jina / Voyage /rerank shape
        def do_POST(self):
            body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Bearer k" and body["top_n"] == len(body["documents"])
            results = [{"index": i, "relevance_score": 1.0 if "document" in d.split("\n")[0] else 0.1} for i, d in enumerate(body["documents"])]
            raw = _json.dumps({"results": results}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rr = HTTPReranker(f"http://127.0.0.1:{srv.server_port}/v1", "rerank-x", api_key="k")
        hits = Tools(c, s, TENANT, embedder=emb, index=index, reranker=rr).search("tread depth", mode="semantic", limit=2)
        assert hits[0]["kind"] == "document" and hits[0]["score"] == 1.0  # the reranker's order won
    finally:
        srv.shutdown()


def test_global_memory_is_indexed_for_every_tenant_that_knows_the_subject(fleet):
    """0.15.0 moved global memory to its own partition; the index must still read it."""
    c, s = fleet
    emb, idx = HashEmbedder(), MemoryIndex()
    first = index_tenant(s, TENANT, emb, idx)
    Tools(c, s, TENANT).annotate("tire:T-9001", "recall", "manufacturer recall on this batch", scope="global", evidence="bulletin-7")
    assert "manufacturer recall" in render(s, TENANT, "tire:T-9001")
    again = index_tenant(s, TENANT, emb, idx, since=first.cursor)  # incremental: the global change is seen
    assert again.indexed == 1 and again.cursor > first.cursor
    assert index_tenant(s, "nobody-knows-it", emb, MemoryIndex(), since=first.cursor).indexed == 0


def test_custom_adapters_example_runs_and_honours_the_index_contract():
    """docs/serve.md points at examples/custom_adapters.py; it must keep working."""
    import runpy

    mod = runpy.run_path("examples/custom_adapters.py", run_name="__main__")
    test_index_contract(mod["make_index"]())


def test_split_documents_work_on_every_index(fleet, index):
    """semantic.split keys carry a separator: every index must store and return them, and a hit maps to its subject."""
    from keel.semantic import SPLIT

    c, s = fleet
    s.open_fact(TENANT, "unit:MXK-4821", "field", "Ventas netas por region", valid_from=datetime(2026, 9, 1, tzinfo=timezone.utc), source="b")
    emb = HashEmbedder()
    index_tenant(s, TENANT, emb, index, policy={"split": {"unit": ["field"]}})
    t = Tools(c, s, TENANT, embedder=emb, index=index)
    [hit] = t.search("ventas netas por region", mode="semantic", kind="unit", limit=1)
    assert hit["key"] == "unit:MXK-4821" and hit["matched"] == "field" and SPLIT not in hit["key"]
