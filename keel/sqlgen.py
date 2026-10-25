"""A cartridge compiled to SQL: bitemporal tables, point-in-time macros, and one macro per rule.

Deterministic and model-free: the same cartridge always compiles to the same text. The core stays
domain-agnostic; every kind, predicate, relation and rule name in the output comes from the cartridge.

    con.execute(generate(cartridge))                       # DuckDB: tables (if missing) and macros
    con.execute("SELECT * FROM state_at('acme', '2026-10-01T00:00:00+00:00')")
    con.execute("SELECT subject FROM tire_critical_at('acme', '2026-10-01', k := '2026-10-02')")

The tables are the SQL stores' own (keel/store/sql.py): instants are ISO 8601 text in UTC, values JSON
text, close marks rows of `facts` with source `keel:close`. Every macro takes the tenant first and an
instant `t`; `k` (transaction time) answers as Keel knew it then, with Keel's own semantics
(keel/store/memory.py as_known_at): the facts recorded by k, each ended by the next fact of its slot
known by k, by a close learned by k, or by a close mark recorded by k.

    facts_known_at(tenant, t, k, alive)   every fact of the tenant true at t (all scopes and statuses)
    state_known_at(tenant, t, k, alive, as_author, as_task)
                                          what rules and agents read: accepted facts the caller may see
                                          (agent memory only its author's, task memory only its task's,
                                          the global partition's global facts)
    state_at(tenant, t)                   the same, as currently known
    relations_known_at(tenant, t, k), relations_at(tenant, t)
    <rule>_verdicts(tenant, t, k, ...)    subject, verdict (true, false or null) for its kind
    <rule>_at(tenant, t, k, ...)          the entities the rule holds for (what search(rule=) lists)

`alive` is the instant memory must outlive (expires_at); by default k, else now. The dialect is a class
(DuckDB here) so a Postgres one can follow: it owns types, instants, JSON access and macro syntax.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from keel.rules import DURATION_OPS, Calendar
from keel.scopes import GLOBAL_TENANT, Scope
from keel.store.memory import CLOSE

RESERVED = {"facts", "state", "relations", "entities", "keel"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYM = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "="}


class DuckDB:
    """The DuckDB dialect: instants are VARCHAR (ISO 8601, UTC), values VARCHAR holding JSON."""

    name = "duckdb"

    def schema(self) -> str:
        return """
CREATE SEQUENCE IF NOT EXISTS relations_seq;
CREATE SEQUENCE IF NOT EXISTS facts_seq;
CREATE TABLE IF NOT EXISTS entities (
  tenant VARCHAR NOT NULL, kind VARCHAR NOT NULL, id VARCHAR NOT NULL, ref VARCHAR, attrs VARCHAR NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant, kind, id));
CREATE TABLE IF NOT EXISTS relations (
  seq BIGINT DEFAULT nextval('relations_seq'), tenant VARCHAR NOT NULL, src VARCHAR NOT NULL, rel VARCHAR NOT NULL,
  dst VARCHAR NOT NULL, valid_from VARCHAR NOT NULL, valid_to VARCHAR, recorded_at VARCHAR, source VARCHAR,
  closed_at VARCHAR, changed_at VARCHAR);
CREATE TABLE IF NOT EXISTS facts (
  seq BIGINT DEFAULT nextval('facts_seq'), tenant VARCHAR NOT NULL, subject VARCHAR NOT NULL, predicate VARCHAR NOT NULL,
  value VARCHAR NOT NULL, valid_from VARCHAR NOT NULL, valid_to VARCHAR, recorded_at VARCHAR, source VARCHAR,
  scope VARCHAR NOT NULL, corrected_by VARCHAR, evidence VARCHAR, expires_at VARCHAR, task VARCHAR,
  idempotency_key VARCHAR, closed_at VARCHAR, status VARCHAR, trace VARCHAR, cartridge VARCHAR, changed_at VARCHAR);
CREATE TABLE IF NOT EXISTS ingested (tenant VARCHAR NOT NULL, id VARCHAR NOT NULL, PRIMARY KEY (tenant, id));
CREATE TABLE IF NOT EXISTS forgotten (tenant VARCHAR NOT NULL PRIMARY KEY, "at" VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
  id VARCHAR NOT NULL PRIMARY KEY, hash VARCHAR NOT NULL UNIQUE, account VARCHAR NOT NULL, tenant VARCHAR, agent VARCHAR,
  "glob" INTEGER NOT NULL DEFAULT 0, created_at VARCHAR NOT NULL, revoked_at VARCHAR);
CREATE TABLE IF NOT EXISTS metering (
  tenant VARCHAR NOT NULL, day VARCHAR NOT NULL, item VARCHAR NOT NULL, n BIGINT NOT NULL, PRIMARY KEY (tenant, day, item));
CREATE TABLE IF NOT EXISTS cutoffs (tenant VARCHAR NOT NULL PRIMARY KEY, cut VARCHAR NOT NULL, "at" VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (id VARCHAR NOT NULL PRIMARY KEY, rps INTEGER, events_month BIGINT, store VARCHAR, retention VARCHAR)
"""

    def prelude(self) -> str:
        """Session settings and the helpers every macro calls."""
        return f"""
SET TimeZone = 'UTC';
CREATE OR REPLACE MACRO keel_iso(x) AS CASE WHEN x IS NULL THEN NULL
  WHEN microsecond(CAST(x AS TIMESTAMPTZ)) % 1000000 = 0 THEN strftime(CAST(x AS TIMESTAMPTZ), '%Y-%m-%dT%H:%M:%S+00:00')
  ELSE strftime(CAST(x AS TIMESTAMPTZ), '%Y-%m-%dT%H:%M:%S.%f+00:00') END;
CREATE OR REPLACE MACRO keel_now() AS keel_iso(get_current_timestamp());
CREATE OR REPLACE MACRO keel_global(tnt) AS
  CASE WHEN contains(tnt, '/') THEN split_part(tnt, '/', 1) || '/{GLOBAL_TENANT}' ELSE '{GLOBAL_TENANT}' END;
CREATE OR REPLACE MACRO keel_days(a, b) AS
  (date_diff('microsecond', CAST(a AS TIMESTAMPTZ), CAST(b AS TIMESTAMPTZ)) / 1000000.0) / 86400.0;
"""

    def table_macro(self, name: str, params: str, body: str) -> str:
        return f"CREATE OR REPLACE MACRO {name}({params}) AS TABLE\n{body};\n"

    def lit(self, v: Any) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            return repr(float(v))
        return "'" + str(v).replace("'", "''") + "'"

    def json_type(self, v: str) -> str:
        return f"json_type(CAST({v} AS JSON))"

    def json_str(self, v: str) -> str:
        return f"json_extract_string({v}, '$')"

    def json_num(self, v: str, jt: str) -> str:
        """A JSON number or boolean as a DOUBLE (Python compares booleans as 0 and 1), else NULL."""
        return (f"CASE WHEN {jt} IN ('UBIGINT', 'BIGINT', 'HUGEINT', 'UHUGEINT', 'DOUBLE') THEN CAST({v} AS DOUBLE) "
                f"WHEN {jt} = 'BOOLEAN' THEN CASE WHEN {v} = 'true' THEN 1.0 ELSE 0.0 END END")

    def json_eq(self, a: str, b: str) -> str:
        """Two JSON texts hold the same value (close marks: `[value]` against a fact's value)."""
        return f"CAST(json_extract({a}, '$[0]') AS VARCHAR) = CAST(json({b}) AS VARCHAR)"

    def epoch(self, x: str) -> str:
        return f"epoch(TRY_CAST({x} AS TIMESTAMPTZ))"

    def day(self, x: str) -> str:
        """The UTC calendar day of an instant (instants are stored in UTC)."""
        return f"CAST(substr({x}, 1, 10) AS DATE)"

    def weekday(self, d: str) -> str:
        return f"(isodow({d}) - 1)"  # Monday 0, as Python's weekday()

    def lower(self, x: str) -> str:
        return f"lower({x})"


DIALECTS = {"duckdb": DuckDB}


def rule_macro(name: str) -> str:
    """The SQL name a rule compiles to (`<name>_at`, `<name>_verdicts`)."""
    if not _IDENT.match(name) or name.lower() in RESERVED or name.lower().startswith("keel_"):
        raise NotImplementedError(f"rule '{name}': its name is not a plain SQL identifier free of the core's names")
    return name


def generate(cartridge: Any, dialect: str = "duckdb") -> str:
    """The whole script: tables (created if missing), helpers, point-in-time macros, one macro pair per rule."""
    d = DIALECTS[dialect]()
    return d.schema() + ";\n" + macros(cartridge, d)


def macros(cartridge: Any, d: Any | None = None) -> str:
    """Helpers, point-in-time macros and rule macros, without the tables (for a store that owns them)."""
    d = d or DuckDB()
    multi = sorted(p for p, spec in (cartridge.predicates or {}).items() if isinstance(spec, dict) and spec.get("multi"))
    symmetric = {r for r, spec in (cartridge.relations or {}).items() if isinstance(spec, dict) and spec.get("symmetric")}
    out = [d.prelude(), _state(d, multi)]
    for name, spec in cartridge.rules.items():
        if spec.get("when"):
            out.append(_Rule(d, cartridge.calendar, symmetric).macros(name, spec))
    return "\n".join(out)


# ---- point in time --------------------------------------------------------------------------------------


def _state(d: Any, multi: list[str]) -> str:
    per_source = "(f.status IS NOT NULL" + (f" OR f.predicate IN ({', '.join(d.lit(p) for p in multi)})" if multi else "") + ")"
    slot = (f"f.tenant, f.subject, f.predicate, f.status, f.scope, CASE WHEN f.scope = '{Scope.TASK.value}' THEN f.task END, "
            f"CASE WHEN f.scope = '{Scope.AGENT.value}' THEN f.source END, CASE WHEN {per_source} THEN f.source END, "
            f"CASE WHEN {per_source} THEN f.corrected_by END")
    succession = "f.valid_from, coalesce(f.recorded_at, ''), coalesce(f.evidence, ''), f.value"
    close = d.lit(CLOSE)
    marks = (f"SELECT min(c.valid_from) FROM facts c WHERE c.source = {close} AND c.tenant = f.tenant AND c.subject = f.subject "
             f"AND c.predicate = f.predicate AND c.status IS NOT DISTINCT FROM f.status AND c.scope = f.scope "
             f"AND (c.recorded_at IS NULL OR c.recorded_at <= keel_iso(k)) AND f.valid_from < c.valid_from "
             f"AND (c.value = 'null' OR {d.json_eq('c.value', 'f.value')})")
    alive = "(expires_at IS NULL OR expires_at > coalesce(keel_iso(alive), keel_iso(k), keel_now()))"
    at = "valid_from <= keel_iso(t) AND (valid_to IS NULL OR valid_to > keel_iso(t))"
    visible = (f"status IS NULL AND (tenant = tnt OR (tenant = keel_global(tnt) AND scope = '{Scope.GLOBAL.value}')) "
               f"AND (scope <> '{Scope.AGENT.value}' OR (as_author IS NOT NULL AND source = 'annotate:' || as_author)) "
               f"AND (scope <> '{Scope.TASK.value}' OR task IS NULL OR task = as_task)")
    jt = d.json_type("value")
    return "".join([
        # every value fact of the tenant and its global partition, ended as known at k (k NULL: as stored)
        d.table_macro("keel_facts_now", "tnt", f"SELECT * FROM facts WHERE tenant IN (tnt, keel_global(tnt)) AND source IS DISTINCT FROM {close}"),
        d.table_macro("keel_facts_known", "tnt, k", f"""SELECT * EXCLUDE (stored_to, next_from, mark_at),
  least(next_from, CASE WHEN stored_to IS NOT NULL AND (closed_at IS NULL OR closed_at <= keel_iso(k)) THEN stored_to END, mark_at) AS valid_to
FROM (SELECT f.* RENAME (valid_to AS stored_to), lead(f.valid_from) OVER (PARTITION BY {slot} ORDER BY {succession}) AS next_from,
             ({marks}) AS mark_at
      FROM facts f
      WHERE f.tenant IN (tnt, keel_global(tnt)) AND f.source IS DISTINCT FROM {close}
        AND (f.recorded_at IS NULL OR f.recorded_at <= keel_iso(k)))"""),
        d.table_macro("keel_facts", "tnt, k := NULL",
                      "SELECT * FROM keel_facts_now(tnt) WHERE k IS NULL UNION ALL BY NAME SELECT * FROM keel_facts_known(tnt, k) WHERE k IS NOT NULL"),
        d.table_macro("facts_known_at", "tnt, t, k := NULL, alive := NULL",
                      f"SELECT * FROM keel_facts(tnt, k := k) WHERE tenant = tnt AND {at} AND {alive}"),
        # what rules (and agents) read: accepted facts the caller may see, with their JSON decoded once
        d.table_macro("keel_visible", "tnt, k := NULL, alive := NULL, as_author := NULL, as_task := NULL",
                      f"SELECT *, {jt} AS jt, CASE WHEN {jt} = 'VARCHAR' THEN {d.json_str('value')} END AS vs, {d.json_num('value', jt)} AS vn "
                      f"FROM keel_facts(tnt, k := k) WHERE {visible} AND {alive}"),
        d.table_macro("state_known_at", "tnt, t, k := NULL, alive := NULL, as_author := NULL, as_task := NULL",
                      f"SELECT * EXCLUDE (jt, vs, vn) FROM keel_visible(tnt, k := k, alive := alive, as_author := as_author, as_task := as_task) WHERE {at}"),
        d.table_macro("state_at", "tnt, t", "SELECT * FROM state_known_at(tnt, t)"),
        d.table_macro("relations_known_at", "tnt, t, k := NULL", """SELECT src, rel, dst, valid_from, valid_to, recorded_at, source FROM (
  SELECT * EXCLUDE (valid_to), CASE WHEN k IS NOT NULL AND closed_at IS NOT NULL AND closed_at > keel_iso(k) THEN NULL ELSE valid_to END AS valid_to
  FROM relations WHERE tenant = tnt AND (k IS NULL OR recorded_at IS NULL OR recorded_at <= keel_iso(k)))
WHERE valid_from <= keel_iso(t) AND (valid_to IS NULL OR valid_to > keel_iso(t))"""),
        d.table_macro("relations_at", "tnt, t", "SELECT * FROM relations_known_at(tnt, t)"),
    ])


# ---- rules ------------------------------------------------------------------------------------------------


class _Rule:
    """One rule's `when` as a SQL expression over the subject, with keel/rules.py's semantics: Kleene logic
    (SQL's own three values), a leaf null when its predicate has no current fact or a comparison would raise,
    comparisons checked in order (the first that fails or raises decides), `via` over neighbours."""

    T = "keel_iso(t)"

    def __init__(self, d: Any, calendar: Calendar, symmetric: set[str]) -> None:
        self.d, self.calendar, self.symmetric = d, calendar, symmetric
        self.ctes: list[str] = []
        self.predicates: set[str] = set()
        self.n = 0

    def macros(self, name: str, spec: dict[str, Any]) -> str:
        d, m = self.d, rule_macro(name)
        when = spec["when"]
        expr = self.cond({"all": when} if isinstance(when, list) else when, "s.subject")
        kind = spec.get("applies_to")
        preds = ", ".join(d.lit(p) for p in sorted(self.predicates)) or "NULL"
        params = "tnt, t, k := NULL, alive := NULL, as_author := NULL, as_task := NULL"
        of_kind = f"kind = {d.lit(kind)}" if kind else "TRUE"
        subject_kind = f"split_part(subject, ':', 1) = {d.lit(kind)}" if kind else "TRUE"
        ctes = ",\n".join([
            (f"vis AS MATERIALIZED (SELECT * FROM keel_visible(tnt, k := k, alive := alive, as_author := as_author, as_task := as_task) "
            f"WHERE predicate IN ({preds}) AND valid_from <= {self.T})"),
            (f"cur AS MATERIALIZED (SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY subject, predicate "
            f"ORDER BY valid_from DESC, (tenant <> tnt) DESC, seq DESC) AS rn FROM vis WHERE valid_to IS NULL OR valid_to > {self.T}) WHERE rn = 1)"),
            "nbr AS MATERIALIZED (SELECT * FROM relations_known_at(tnt, t, k := k))",
            *self.ctes,
        ])
        # every subject Keel knows of that kind: an entity, a fact's subject, a relation's end
        body = (f"WITH {ctes}\nSELECT s.subject, {expr} AS verdict FROM (\n"
                f"  SELECT kind || ':' || id AS subject FROM entities WHERE tenant = tnt AND {of_kind}\n"
                f"  UNION SELECT subject FROM facts WHERE tenant = tnt AND {subject_kind}\n"
                f"  UNION SELECT src FROM relations WHERE tenant = tnt AND {subject_kind.replace('subject', 'src')}\n"
                f"  UNION SELECT dst FROM relations WHERE tenant = tnt AND {subject_kind.replace('subject', 'dst')}) s")
        at = f"SELECT v.subject FROM {m}_verdicts(tnt, t, k := k, alive := alive, as_author := as_author, as_task := as_task) v " \
             f"JOIN entities e ON e.tenant = tnt AND e.kind || ':' || e.id = v.subject WHERE v.verdict"
        return d.table_macro(f"{m}_verdicts", params, body) + d.table_macro(f"{m}_at", params, at)

    def alias(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}{self.n}"

    # conditions -> boolean expressions (true, false or NULL) of `subject`, a SQL expression
    def cond(self, c: Any, subject: str) -> str:
        if not isinstance(c, dict):
            raise NotImplementedError(f"condition {c!r}: a condition is a mapping")
        if "via" in c:
            return self.via(c, subject)
        if "all" in c:
            return "(" + " AND ".join(self.cond(x, subject) for x in c["all"]) + ")" if c["all"] else "TRUE"
        if "any" in c:
            return "(" + " OR ".join(self.cond(x, subject) for x in c["any"]) + ")" if c["any"] else "FALSE"
        if "not" in c:
            return f"(NOT {self.cond(c['not'], subject)})"
        return self.leaf(c, subject)

    def via(self, c: dict[str, Any], subject: str) -> str:
        every = "all" in c
        rel, direction = c["via"], c.get("direction", "any")
        lit = self.d.lit(rel)
        r, n, v = self.alias("r"), self.alias("n"), self.alias("v")
        out = f"SELECT {r}.dst FROM nbr {r} WHERE {r}.rel = {lit} AND {r}.src = {subject}"
        inn = f"SELECT {r}.src FROM nbr {r} WHERE {r}.rel = {lit} AND {r}.dst = {subject}"
        if rel in self.symmetric or direction == "any":
            others = f"{out} UNION {inn}"
        elif direction == "out":
            others = f"SELECT DISTINCT * FROM ({out})"
        elif direction == "in":
            others = f"SELECT DISTINCT * FROM ({inn})"
        else:
            raise NotImplementedError(f"via direction '{direction}'")
        x = self.cond(c["all"] if every else c["any"], f"{n}.o")
        if every:
            agg = "CASE WHEN count(*) = 0 THEN TRUE WHEN count(*) FILTER (WHERE NOT x) > 0 THEN FALSE WHEN count(*) FILTER (WHERE x IS NULL) > 0 THEN NULL ELSE TRUE END"
        else:
            agg = "CASE WHEN count(*) = 0 THEN FALSE WHEN count(*) FILTER (WHERE x) > 0 THEN TRUE WHEN count(*) FILTER (WHERE x IS NULL) > 0 THEN NULL ELSE FALSE END"
        return f"(SELECT {agg} FROM (SELECT {x} AS x FROM ({others}) {n}(o)) {v})"

    def leaf(self, c: dict[str, Any], subject: str) -> str:
        if "predicate" not in c:
            raise NotImplementedError(f"condition {c!r}: a leaf needs a predicate")
        unknown = set(c) - {"predicate", "ci"} - set(_SYM) - {"ne", "in", "within_days"} - DURATION_OPS
        if unknown:
            raise NotImplementedError(f"operator(s) {', '.join(sorted(unknown))}")
        p, ci = c["predicate"], bool(c.get("ci"))
        self.predicates.add(p)
        a = self.alias("c")
        checks = [self.comparison(op, arg, a, ci) for op, arg in c.items() if op in _SYM or op in ("ne", "in", "within_days")]
        held = None
        durations = []
        for op in sorted(DURATION_OPS & set(c)):
            spec = c[op]
            if not isinstance(spec, dict) or not spec or set(spec) - set(_SYM):
                raise NotImplementedError(f"`{op}` takes a mapping of lt, lte, gt, gte, eq")
            since = f"{a}.valid_from"
            if op.startswith("held_for"):
                held = held or self.held(p, c, ci)
                since = f"coalesce({held}.since, {a}.valid_from)"
            days = self.business_days(since, self.T) if op.endswith("business_days") else f"keel_days({since}, {self.T})"
            test = " AND ".join(f"{days} {_SYM[k]} {self.d.lit(v)}" for k, v in spec.items())
            durations.append(f"(CASE WHEN {held}.err THEN NULL ELSE ({test}) END)" if op.startswith("held_for") else f"({test})")
        tail = " AND ".join(durations) or "TRUE"
        whens = " ".join(f"WHEN ({x}) IS NULL THEN NULL WHEN NOT ({x}) THEN FALSE" for x in checks)
        expr = f"CASE {whens} ELSE {tail} END" if checks else tail
        join = f" LEFT JOIN {held} ON {held}.subject = {a}.subject" if held else ""
        return f"(SELECT {expr} FROM cur {a}{join} WHERE {a}.subject = {subject} AND {a}.predicate = {self.d.lit(p)})"

    def comparison(self, op: str, arg: Any, a: str, ci: bool) -> str:
        """One comparison of the fact `a`'s value: NULL exactly where Python's would raise."""
        if op == "eq":
            return self.eq(arg, a, ci)
        if op == "ne":
            return f"(NOT {self.eq(arg, a, ci)})"
        if op == "in":
            if not isinstance(arg, (list, tuple)):
                raise NotImplementedError(f"`in` with a {type(arg).__name__} (only a list compiles)")
            return "(" + " OR ".join(self.eq(x, a, ci) for x in arg) + ")" if arg else "FALSE"
        if op == "within_days":
            if isinstance(arg, bool) or not isinstance(arg, (int, float)):
                return "NULL"
            value = self.d.lower(f"{a}.vs") if ci else f"{a}.vs"
            return f"(CASE WHEN {a}.jt = 'VARCHAR' THEN {self.d.epoch(value)} <= {self.d.epoch(self.T)} + {float(arg) * 86400!r} END)"
        sym = _SYM[op]
        if isinstance(arg, (bool, int, float)):
            return f"({a}.vn {sym} {self.d.lit(arg)})"
        if isinstance(arg, str):
            if ci:
                return f"({self.d.lower(f'{a}.vs')} {sym} {self.d.lit(arg.casefold())})"
            return f"({a}.vs {sym} {self.d.lit(arg)})"
        if isinstance(arg, (list, tuple, dict)):
            raise NotImplementedError(f"`{op}` with a {type(arg).__name__}")
        return "NULL"  # None, a date: Python cannot order a JSON value against it

    def eq(self, arg: Any, a: str, ci: bool) -> str:
        """Python's ==, never NULL."""
        if arg is None:
            return f"({a}.jt = 'NULL')"
        if isinstance(arg, (bool, int, float)):
            return f"coalesce({a}.vn = {self.d.lit(arg)}, FALSE)"
        if isinstance(arg, str):
            if ci:
                return f"coalesce({self.d.lower(f'{a}.vs')} = {self.d.lit(arg.casefold())}, FALSE)"
            return f"coalesce({a}.vs = {self.d.lit(arg)}, FALSE)"
        if isinstance(arg, (date, datetime)):
            return "FALSE"  # a JSON value is never a date object
        raise NotImplementedError(f"`eq` with a {type(arg).__name__}")

    def held(self, p: str, c: dict[str, Any], ci: bool) -> str:
        """A CTE: per subject, where the run of consecutive facts meeting the comparisons began (keel/rules.py
        _held_since: walk back while each earlier fact ends where the later one starts and also matches)."""
        h = self.alias("held")
        checks = [self.comparison(op, arg, "h", ci) for op, arg in c.items() if op in _SYM or op in ("ne", "in", "within_days")]
        m = ("CASE " + " ".join(f"WHEN ({x}) IS NULL THEN NULL WHEN NOT ({x}) THEN FALSE" for x in checks) + " ELSE TRUE END") if checks else "TRUE"
        nxt = "coalesce(lead(h.valid_from) OVER w, cc.valid_from)"
        self.ctes.append(f"""{h} AS (SELECT subject,
    arg_max(status, rn) FILTER (WHERE status <> 'ok') = 'e' AS err,
    coalesce(arg_max(nxt, rn) FILTER (WHERE status <> 'ok'), min(valid_from)) AS since
  FROM (SELECT h.subject, h.valid_from, row_number() OVER w AS rn, {nxt} AS nxt,
               CASE WHEN h.valid_to IS DISTINCT FROM {nxt} THEN 'b' WHEN ({m}) IS NULL THEN 'e' WHEN NOT ({m}) THEN 'b' ELSE 'ok' END AS status
        FROM vis h JOIN cur cc ON cc.subject = h.subject AND cc.predicate = h.predicate
        WHERE h.predicate = {self.d.lit(p)} AND h.valid_from < cc.valid_from
        WINDOW w AS (PARTITION BY h.subject ORDER BY h.valid_from, h.seq))
  GROUP BY subject)""")
        return h

    def business_days(self, start: str, end: str) -> str:
        """Calendar.business_days in arithmetic: whole days from start's day to end's day, per weekday not in
        the weekend, minus the holidays in range that fall on a working day."""
        d = self.d
        a, b = d.day(start), d.day(end)
        n = f"greatest(date_diff('day', {a}, {b}), 0)"
        per_day = [f"({n} // 7 + CASE WHEN (({w} - {d.weekday(a)}) % 7 + 7) % 7 < {n} % 7 THEN 1 ELSE 0 END)"
                   for w in range(7) if w not in self.calendar.weekend]
        holidays = [f"CASE WHEN {a} <= DATE '{h.isoformat()}' AND DATE '{h.isoformat()}' < {b} THEN 1 ELSE 0 END"
                    for h in sorted(self.calendar.holidays) if h.weekday() not in self.calendar.weekend]
        return "(" + (" + ".join(per_day) or "0") + (" - " + " - ".join(holidays) if holidays else "") + ")"
