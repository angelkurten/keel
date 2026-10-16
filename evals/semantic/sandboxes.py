"""Sandboxes for the semantic-search evals: small, deterministic tenants with labeled queries.

Each sandbox builds a store (through the real store primitives, no ingest needed) and returns a
list of queries with the subjects that should come back. Query families, chosen to probe known
weak spots of dense retrieval:

    paraphrase   no word shared with the document ("worn-out tyre" for depth 2.6 mm)
    exact        names and words that appear in the document
    id           identifiers and codes ("CUS-1974", "T-9001")
    crosslang    Spanish query, English document, or the reverse
    relation     answerable only through a related entity ("cards of client BAT")
    negative     nothing relevant exists: the right answer is an empty list
    rule         semantic query plus a rule filter
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from keel import MemoryStore, load_cartridge
from keel.cartridge import Cartridge
from keel.store.base import Store

NOW = datetime.now(timezone.utc)


@dataclass
class Query:
    text: str
    relevant: set[str]  # subject keys; empty for a negative query
    family: str
    kind: str | None = None
    rule: str | None = None


@dataclass
class Sandbox:
    name: str
    cartridge: Cartridge
    store: Store
    tenant: str
    queries: list[Query] = field(default_factory=list)
    policy: dict = field(default_factory=dict)  # the cartridge `semantic:` section to evaluate with


def fleet() -> Sandbox:
    """A fleet with 40 units and 160 tires; distractors share vocabulary with the targets."""
    c, s, t = load_cartridge("examples/fleet"), MemoryStore(), "acme"
    rnd = random.Random(7)
    models = ["Freightliner Cascadia", "Mercedes Actros", "Volvo FH16", "Kenworth T680", "Sprinter 515"]
    conditions = {
        "T-0101": "sidewall bulge after hitting a curb",
        "T-0202": "tread worn down to the wear bars",
        "T-0303": "slow leak, loses pressure overnight",
        "T-0404": "uneven wear on the inner shoulder, alignment suspected",
        "T-0504": "nail in the tread, patched",
    }
    for u in range(40):
        uid = f"U-{u:03d}"
        s.upsert_entity(t, "unit", uid, plate=f"MX{rnd.randint(1000, 9999)}", model=models[u % 5])
        for p in range(4):
            tid = f"T-{u:02d}{p + 1:02d}"
            s.upsert_entity(t, "tire", tid, position=f"P{p + 1}")
            s.link(t, f"tire:{tid}", "mounted_on", f"unit:{uid}", NOW - timedelta(days=90))
            depth = 2.4 if tid in ("T-0202", "T-1703") else round(rnd.uniform(4.5, 9.0), 1)
            s.open_fact(t, f"tire:{tid}", "depth_mm", depth, valid_from=NOW - timedelta(days=3))
            note = conditions.get(tid) or rnd.choice(["no issues found", "routine check, ok", "pressure adjusted to spec", "rotated at service"])
            s.open_fact(t, f"tire:{tid}", "condition", note, valid_from=NOW - timedelta(days=3))
            s.open_fact(t, f"tire:{tid}", "comment", f"inspection photo https://cdn.example/{tid}.jpg ref#{rnd.randint(10**6, 10**7)}", valid_from=NOW - timedelta(days=3))
    s.upsert_entity(t, "document", "POL-77", category="insurance_policy")
    s.open_fact(t, "document:POL-77", "expires_on", (NOW + timedelta(days=6)).date().isoformat(), valid_from=NOW - timedelta(days=200))
    s.link(t, "document:POL-77", "covers_unit", "unit:U-012", NOW - timedelta(days=200))
    q = [
        Query("tyre with a bubble on the side wall", {"tire:T-0101"}, "paraphrase"),
        Query("tire that keeps going flat", {"tire:T-0303"}, "paraphrase"),
        Query("bald tire, no tread left", {"tire:T-0202"}, "paraphrase"),
        Query("wheels out of alignment eating the tire edge", {"tire:T-0404"}, "paraphrase"),
        Query("sidewall bulge", {"tire:T-0101"}, "exact"),
        Query("nail in the tread", {"tire:T-0504"}, "exact"),
        Query("T-0303", {"tire:T-0303"}, "id"),
        Query("U-012", {"unit:U-012"}, "id"),
        Query("llanta con una burbuja en el costado", {"tire:T-0101"}, "crosslang"),
        Query("neumático que pierde aire", {"tire:T-0303"}, "crosslang"),
        Query("tires on unit U-012", {f"tire:T-12{p:02d}" for p in range(1, 5)}, "relation", kind="tire"),
        Query("engine overheating and coolant leak", set(), "negative"),
        Query("passenger seat upholstery torn", set(), "negative"),
        Query("tire with low tread", {"tire:T-0202", "tire:T-1703"}, "rule", rule="tire_critical"),
    ]
    return Sandbox("fleet", c, s, t, q, policy={"exclude": ["comment"], "relations": ["mounted_on"]})


def cards() -> Sandbox:
    """Jira-like support cards in Spanish (kagent-style): clients, noisy comments, renamed keys."""
    c = load_cartridge("examples/fleet")  # reuse the engine; kinds below are declared on the fly
    c.entities.update({"card": {}, "cliente": {}})
    c.relations.update({"de_cliente": {"from": "card", "to": "cliente"}})
    c.rules["en_pausa"] = {"applies_to": "card", "when": {"predicate": "estado", "in": ["on hold", "pend. autorización td"], "ci": True}}
    s, t = MemoryStore(), "vecfleet-bi"
    rnd = random.Random(11)
    clients = {"BAT": "British American Tobacco", "HEIN": "Heineken México", "CEM": "Cementos del Norte", "LOG": "Logística Andina"}
    for cid, name in clients.items():
        s.upsert_entity(t, "cliente", cid, nombre=name)
    targets = {
        "CUS-1974": ("El tablero de costos no muestra el consumo de combustible por unidad", "BAT", "On Hold"),
        "CUS-1981": ("Agregar filtro por centro de costo en el reporte de mantenimiento", "HEIN", "In Progress"),
        "CUS-1990": ("Los kilómetros recorridos salen duplicados cuando hay dos GPS", "CEM", "To Do"),
        "CUS-2001": ("Exportar a Excel el listado de vencimientos de pólizas", "LOG", "Pend. Autorización TD"),
        "BI-857": ("Medida DAX de rendimiento km/l devuelve en blanco para camiones nuevos", "BAT", "Backlog"),
    }
    fillers = ["Ajuste de colores del dashboard", "Cambiar logo en la portada del reporte", "Revisar permisos de usuario en el workspace",
               "Actualizar credenciales del gateway", "Corregir typo en el título del gráfico", "Mover visual de la página 2 a la 3"]
    keys = list(targets)
    for i in range(120):
        keys.append(f"CUS-{1500 + i}")
    for key in keys:
        summary, client, status = targets.get(key) or (rnd.choice(fillers), rnd.choice(list(clients)), rnd.choice(["Done", "To Do", "Backlog", "Finalizada"]))
        s.upsert_entity(t, "card", key, tipo="CUS" if key.startswith("CUS") else "BI")
        s.open_fact(t, f"card:{key}", "resumen", summary, valid_from=NOW - timedelta(days=30))
        s.open_fact(t, f"card:{key}", "estado", status, valid_from=NOW - timedelta(days=5))
        s.open_fact(t, f"card:{key}", "comentario", f"ver https://vecfleet.atlassian.net/browse/{key} commit {rnd.randint(16**7, 16**8):x} build #{rnd.randint(100, 999)}", valid_from=NOW - timedelta(days=2))
        s.link(t, f"card:{key}", "de_cliente", f"cliente:{client}", NOW - timedelta(days=30))
    q = [
        Query("el reporte no trae cuánto gasta de diésel cada camión", {"card:CUS-1974"}, "paraphrase"),
        Query("los km aparecen dos veces si el vehículo tiene doble rastreador", {"card:CUS-1990"}, "paraphrase"),
        Query("bajar a hoja de cálculo las fechas en que vencen los seguros", {"card:CUS-2001"}, "paraphrase"),
        Query("rendimiento km/l en blanco", {"card:BI-857"}, "exact"),
        Query("filtro por centro de costo", {"card:CUS-1981"}, "exact"),
        Query("CUS-1990", {"card:CUS-1990"}, "id"),
        Query("BI-857", {"card:BI-857"}, "id"),
        Query("fuel consumption per vehicle missing from the cost dashboard", {"card:CUS-1974"}, "crosslang"),
        Query("cards of British American Tobacco", {"card:CUS-1974", "card:BI-857"} | {f"card:{k}" for k in keys if k not in targets and s.relations(t, f"card:{k}", rel="de_cliente")[0].dst == "cliente:BAT"}, "relation", kind="card"),
        Query("integración con SAP para facturas electrónicas", set(), "negative"),
        Query("app móvil para choferes con checklist", set(), "negative"),
        Query("tarjetas detenidas esperando autorización o al cliente", {"card:CUS-1974", "card:CUS-2001"}, "rule", rule="en_pausa"),
    ]
    return Sandbox("cards", c, s, t, q, policy={"exclude": ["comentario"], "relations": ["de_cliente"]})


def finance() -> Sandbox:
    """A household's debts and dues (Toche-style), Spanish, numbers and creditors."""
    c, s, t = load_cartridge("examples/finance"), MemoryStore(), "hogar"
    debts = {
        "visa": ("Banco de Bogotá", "Tarjeta de crédito Visa", 3870000, 29.22),
        "libranza": ("Bancolombia", "Crédito de libranza", 12500000, 14.5),
        "vehiculo": ("Davivienda", "Crédito de vehículo", 28000000, 17.9),
        "icetex": ("ICETEX", "Crédito educativo posgrado", 9400000, 11.2),
    }
    s.upsert_entity(t, "household", t)
    for did, (creditor, product, balance, rate) in debts.items():
        s.upsert_entity(t, "debt", did, creditor=creditor, product=product)
        s.open_fact(t, f"debt:{did}", "balance_cop", balance, valid_from=NOW - timedelta(days=10))
        s.open_fact(t, f"debt:{did}", "rate_ea_pct", rate, valid_from=NOW - timedelta(days=10))
        s.link(t, f"debt:{did}", "owed_by", f"household:{t}", NOW - timedelta(days=300))
    for i, (concept, due) in enumerate([("Cuota tarjeta Visa", 5), ("Arriendo apartamento", 1), ("Plan de celular", 12), ("Seguro del carro", 20), ("Colegio de los niños", 3)]):
        s.upsert_entity(t, "due", f"D-{i}")
        s.open_fact(t, f"due:D-{i}", "status", "pending", valid_from=NOW - timedelta(days=20))
        s.open_fact(t, f"due:D-{i}", "concept", concept, valid_from=NOW - timedelta(days=20))
        s.open_fact(t, f"due:D-{i}", "due_on", (NOW + timedelta(days=due)).date().isoformat(), valid_from=NOW - timedelta(days=20))
    q = [
        Query("la deuda del carro", {"debt:vehiculo"}, "paraphrase"),
        Query("préstamo de estudios", {"debt:icetex"}, "paraphrase"),
        Query("lo que pago de alquiler", {"due:D-1"}, "paraphrase"),
        Query("Bancolombia", {"debt:libranza"}, "exact"),
        Query("student loan", {"debt:icetex"}, "crosslang"),
        Query("car insurance payment", {"due:D-3"}, "crosslang"),
        Query("hipoteca de la casa", set(), "negative"),
        Query("deudas caras", {"debt:visa"}, "rule", rule="debt_expensive"),
    ]
    return Sandbox("finance", c, s, t, q, policy={"relations": []})


SANDBOXES = {"fleet": fleet, "cards": cards, "finance": finance}
