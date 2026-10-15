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

FLEET, EVENTS, TENANT = "cartridges/fleet", "examples/events/fleet.jsonl", "acme-logistics"


def _indexes():
    params = ["memory"]
    try:
        import chromadb  # noqa: F401

        params.append("chroma")
    except ImportError:
        pass
    if os.environ.get("KEEL_VECTOR_INDEX"):
        params.append("external")
    return params


@pytest.fixture(params=_indexes())
def index(request):
    if request.param == "memory":
        return MemoryIndex()
    if request.param == "chroma":
        return load_index("chroma://")
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
    with pytest.raises(ValueError, match="not configured"):
        Tools(c, s, TENANT).search("x", mode="semantic")
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
    args = ["index", cart, "--tenant", TENANT, "--db", db, "--index", "chroma:///" + str(tmp_path / "vec") if "chroma" in _indexes() else "memory", "--embedder", "hash"]
    res = CliRunner().invoke(app, args)
    assert res.exit_code == 0 and "mode=full" in res.output, res.output
    assert (tmp_path / ".keel" / f"index-{TENANT}.cursor").exists()
    res = CliRunner().invoke(app, args)
    assert "mode=incremental" in res.output and "subjects=0" in res.output, res.output


@pytest.mark.skipif(not os.environ.get("KEEL_EMBEDDINGS_API_KEY"), reason="set KEEL_EMBEDDINGS_API_KEY (and _BASE_URL/_MODEL) for a live embedder")
def test_live_openai_compatible_embedder_finds_synonyms(fleet):
    c, s = fleet
    emb, idx = load_embedder("openai"), MemoryIndex()
    index_tenant(s, TENANT, emb, idx)
    t = Tools(c, s, TENANT, embedder=emb, index=idx)
    assert t.search("worn-out tyre", mode="semantic", limit=1)[0]["key"] == "tire:T-9001"  # no shared word with the document
    assert t.search("insurance about to lapse", mode="semantic", kind="document", limit=1)[0]["key"] == "document:POL-7731"
    assert datetime.now(timezone.utc)  # keeps the import used when skipped
