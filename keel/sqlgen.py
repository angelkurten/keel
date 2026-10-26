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
(DuckDB, Postgres, SQLite): it owns types, instants, JSON access and macro syntax. SQLite has no table macros:
its dialect keeps them compiled in Python and inlines a query's calls (`SQLite.expand`).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from keel.rules import DURATION_OPS, Calendar
from keel.scopes import GLOBAL_TENANT, Scope
from keel.store.memory import CLOSE

RESERVED = {"facts", "state", "relations", "entities", "keel"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SYM = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "="}
# what the point-in-time functions return (a dialect that needs declared result columns reads these)
FACT_COLUMNS = ("seq", "tenant", "subject", "predicate", "value", "valid_from", "valid_to", "recorded_at", "source", "scope", "corrected_by",
                "evidence", "expires_at", "task", "idempotency_key", "closed_at", "status", "trace", "cartridge", "changed_at")
FACTS = ", ".join(f"{c} {'bigint' if c == 'seq' else 'text'}" for c in FACT_COLUMNS)
VISIBLE = FACTS + ", jt text, vs text, vn double precision"
RELATIONS = "src text, rel text, dst text, valid_from text, valid_to text, recorded_at text, source text"


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
CREATE OR REPLACE MACRO keel_utc(x) AS CASE
  WHEN regexp_matches(CAST(x AS VARCHAR), '(Z|[+-][0-9][0-9](:?[0-9][0-9])?)$') THEN CAST(x AS TIMESTAMPTZ) AT TIME ZONE 'UTC'
  ELSE CAST(x AS TIMESTAMP) END;
CREATE OR REPLACE MACRO keel_iso(x) AS CASE WHEN x IS NULL THEN NULL
  WHEN microsecond(keel_utc(x)) % 1000000 = 0 THEN strftime(keel_utc(x), '%Y-%m-%dT%H:%M:%S+00:00')
  ELSE strftime(keel_utc(x), '%Y-%m-%dT%H:%M:%S.%f+00:00') END;
CREATE OR REPLACE MACRO keel_now() AS keel_iso(get_current_timestamp());
CREATE OR REPLACE MACRO keel_global(tnt) AS
  CASE WHEN contains(tnt, '/') THEN split_part(tnt, '/', 1) || '/{GLOBAL_TENANT}' ELSE '{GLOBAL_TENANT}' END;
CREATE OR REPLACE MACRO keel_days(a, b) AS
  (date_diff('microsecond', CAST(a AS TIMESTAMPTZ), CAST(b AS TIMESTAMPTZ)) / 1000000.0) / 86400.0;
"""

    JT_STR, JT_NULL = "'VARCHAR'", "'NULL'"  # json_type's names for a string and a null

    def table_macro(self, name: str, params: str, body: str, returns: str = "") -> str:
        return f"CREATE OR REPLACE MACRO {name}({params}) AS TABLE\n{body};\n"

    def rule_verdicts(self, name: str, params: str, ctes: list[tuple[str, bool, str, list[str]]], select: str) -> str:
        """A rule's verdicts: its intermediate results as CTEs of one statement (DuckDB decorrelates the lookups)."""
        body = ",\n".join(f"{n} AS MATERIALIZED ({b})" if mat else f"{n} AS ({b})" for n, mat, b, _ in ctes)
        return self.table_macro(name, params, f"WITH {body}\n{select}", "subject text, verdict boolean")

    # the bodies that lean on DuckDB's star expressions (EXCLUDE, RENAME, UNION ALL BY NAME)
    def facts_star(self) -> str:
        return "*"

    def facts_known(self, valid_to: str, inner: str) -> str:
        return f"""SELECT * EXCLUDE (stored_to, next_from, mark_at),
  {valid_to} AS valid_to
FROM (SELECT f.* RENAME (valid_to AS stored_to), {inner}"""

    def facts_either(self) -> str:
        return "SELECT * FROM keel_facts_now(tnt) WHERE k IS NULL UNION ALL BY NAME SELECT * FROM keel_facts_known(tnt, k) WHERE k IS NOT NULL"

    def visible_facts(self) -> str:
        return "* EXCLUDE (jt, vs, vn)"

    def relations_known(self) -> str:
        return "SELECT * EXCLUDE (valid_to), CASE"

    def first_of(self, x: str, order: str, keep: str) -> str:
        """The value of x on the row with the largest `order` among those that `keep`."""
        return f"arg_max({x}, {order}) FILTER (WHERE {keep})"

    def date_diff_days(self, a: str, b: str) -> str:
        return f"date_diff('day', {a}, {b})"

    def idiv(self, a: str, b: str) -> str:
        return f"{a} // {b}"

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

    def date_lit(self, d: str) -> str:
        return f"DATE '{d}'"

    def kind_is(self, x: str, kind: str) -> str:
        """`x` (a subject key) is of `kind`: its part before the first ':' is the kind."""
        return f"split_part({x}, ':', 1) = {self.lit(kind)}"


class Postgres(DuckDB):
    """The Postgres dialect (12+; 16+ for subqueries without an alias): instants are text (ISO 8601, UTC) and
    values text holding JSON, as SQLite stores them, every text column `COLLATE "C"` (code-point order, as Python).
    Table macros become set-returning SQL functions (LANGUAGE sql, STABLE: the planner inlines them), named
    arguments keep `name := value`, which Postgres accepts beside `name => value`."""

    name = "postgres"
    JT_STR, JT_NULL = "'string'", "'null'"  # jsonb_typeof's names

    def schema(self) -> str:
        t = 'TEXT COLLATE "C"'
        return f"""
CREATE TABLE IF NOT EXISTS entities (
  seq BIGSERIAL PRIMARY KEY, tenant {t} NOT NULL, kind {t} NOT NULL, id {t} NOT NULL, ref {t}, attrs {t} NOT NULL DEFAULT '{{}}',
  UNIQUE (tenant, kind, id));
CREATE TABLE IF NOT EXISTS relations (
  seq BIGSERIAL PRIMARY KEY, tenant {t} NOT NULL, src {t} NOT NULL, rel {t} NOT NULL, dst {t} NOT NULL,
  valid_from {t} NOT NULL, valid_to {t}, recorded_at {t}, source {t}, closed_at {t}, changed_at {t});
CREATE INDEX IF NOT EXISTS relations_src ON relations (tenant, src);
CREATE INDEX IF NOT EXISTS relations_dst ON relations (tenant, dst);
CREATE TABLE IF NOT EXISTS facts (
  seq BIGSERIAL PRIMARY KEY, tenant {t} NOT NULL, subject {t} NOT NULL, predicate {t} NOT NULL,
  value {t} NOT NULL, valid_from {t} NOT NULL, valid_to {t}, recorded_at {t}, source {t},
  scope {t} NOT NULL, corrected_by {t}, evidence {t}, expires_at {t},
  task {t}, idempotency_key {t}, closed_at {t}, status {t}, trace {t}, cartridge {t}, changed_at {t});
CREATE INDEX IF NOT EXISTS facts_subject ON facts (tenant, subject);
CREATE INDEX IF NOT EXISTS facts_recorded ON facts (tenant, recorded_at);
CREATE TABLE IF NOT EXISTS ingested (tenant {t} NOT NULL, id {t} NOT NULL, PRIMARY KEY (tenant, id));
CREATE TABLE IF NOT EXISTS forgotten (tenant {t} NOT NULL PRIMARY KEY, at {t} NOT NULL);
CREATE TABLE IF NOT EXISTS api_keys (
  id {t} NOT NULL PRIMARY KEY, hash {t} NOT NULL UNIQUE, account {t} NOT NULL, tenant {t}, agent {t},
  glob INTEGER NOT NULL DEFAULT 0, created_at {t} NOT NULL, revoked_at {t});
CREATE TABLE IF NOT EXISTS metering (
  tenant {t} NOT NULL, day {t} NOT NULL, item {t} NOT NULL, n BIGINT NOT NULL, PRIMARY KEY (tenant, day, item));
CREATE TABLE IF NOT EXISTS cutoffs (tenant {t} NOT NULL PRIMARY KEY, cut {t} NOT NULL, at {t} NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (id {t} NOT NULL PRIMARY KEY, rps INTEGER, events_month BIGINT, store {t}, retention {t})
"""

    def prelude(self) -> str:
        utc = "(CASE WHEN x ~ '(Z|[+-][0-9][0-9](:?[0-9][0-9])?)$' THEN CAST(x AS timestamptz) AT TIME ZONE 'UTC' ELSE CAST(x AS timestamp) END)"
        iso = f"to_char({utc}, 'YYYY-MM-DD\"T\"HH24:MI:SS{{}}') || '+00:00'"
        return f"""
SET TimeZone = 'UTC';
CREATE OR REPLACE FUNCTION keel_iso(x text) RETURNS text LANGUAGE sql IMMUTABLE AS $keel$
  SELECT CASE WHEN x IS NULL THEN NULL WHEN to_char({utc}, 'US') = '000000' THEN {iso.format('')}
  ELSE {iso.format('.US')} END $keel$;
CREATE OR REPLACE FUNCTION keel_now() RETURNS text LANGUAGE sql STABLE AS $keel$ SELECT keel_iso(CAST(now() AS text)) $keel$;
CREATE OR REPLACE FUNCTION keel_global(tnt text) RETURNS text LANGUAGE sql IMMUTABLE AS $keel$
  SELECT CASE WHEN strpos(tnt, '/') > 0 THEN split_part(tnt, '/', 1) || '/{GLOBAL_TENANT}' ELSE '{GLOBAL_TENANT}' END $keel$;
CREATE OR REPLACE FUNCTION keel_days(a text, b text) RETURNS double precision LANGUAGE sql IMMUTABLE AS $keel$
  SELECT CAST(EXTRACT(epoch FROM (CAST(b AS timestamptz) - CAST(a AS timestamptz))) AS double precision) / 86400.0 $keel$;
CREATE OR REPLACE FUNCTION keel_try_ts(x text) RETURNS timestamptz LANGUAGE plpgsql IMMUTABLE AS $keel$
BEGIN
  IF x IS NULL OR x !~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}' THEN RETURN NULL; END IF;  -- an ISO date first, as parse_iso wants
  RETURN CAST(x AS timestamptz);
EXCEPTION WHEN others THEN RETURN NULL;
END $keel$;
"""

    def table_macro(self, name: str, params: str, body: str, returns: str = "") -> str:
        args = [p.split(" := ")[0] for p in params.split(", ")]
        decl = ", ".join(f"{p.split(' := ')[0]} text" + (" DEFAULT NULL" if " := " in p else "") for p in params.split(", "))
        # DROP first: CREATE OR REPLACE cannot change a function's result columns
        return (f"DROP FUNCTION IF EXISTS {name}({', '.join('text' for _ in args)});\n"
                f"CREATE FUNCTION {name}({decl}) RETURNS TABLE({returns}) LANGUAGE sql STABLE AS $keel$\n{body}\n$keel$;\n")

    def rule_verdicts(self, name: str, params: str, ctes: list[tuple[str, bool, str, list[str]]], select: str) -> str:
        """A rule's verdicts as a plpgsql function: Postgres runs each lookup of a CTE (a correlated subquery per
        subject) as a scan of it, quadratic in the tenant, so every intermediate result becomes a temp table with
        an index on what the lookups go by, then the verdicts are one query over them."""
        args = [p.split(" := ")[0] for p in params.split(", ")]
        decl = ", ".join(f"{p.split(' := ')[0]} text" + (" DEFAULT NULL" if " := " in p else "") for p in params.split(", "))
        steps = []
        for n, _, b, keys in ctes:
            steps.append(f"  DROP TABLE IF EXISTS pg_temp.{n};\n  CREATE TEMP TABLE {n} AS {b};")
            # the temp columns carry the default collation (the functions return plain text) while a subject from the
            # tables compares in "C": an index under each, or the planner cannot probe it from both kinds of lookup
            for cols in keys:
                first, _, rest = cols.partition(",")
                steps += [f"  CREATE INDEX ON {n} ({cols});", f'  CREATE INDEX ON {n} ({first} COLLATE "C"{"," + rest if rest else ""});']
            if keys:
                steps.append(f"  ANALYZE {n};")
        return (f"DROP FUNCTION IF EXISTS {name}({', '.join('text' for _ in args)});\n"
                f"CREATE FUNCTION {name}({decl}) RETURNS TABLE(subject text, verdict boolean) LANGUAGE plpgsql VOLATILE AS $keel$\n"
                f"#variable_conflict use_column\nBEGIN\n  PERFORM set_config('client_min_messages', 'warning', true);\n"
                + "\n".join(steps) + f"\n  RETURN QUERY {select};\nEND $keel$;\n")

    def facts_star(self) -> str:
        return ", ".join(FACT_COLUMNS)

    def facts_known(self, valid_to: str, inner: str) -> str:
        cols = ", ".join(f"{valid_to} AS valid_to" if c == "valid_to" else c for c in FACT_COLUMNS)
        stored = ", ".join("f.valid_to AS stored_to" if c == "valid_to" else f"f.{c}" for c in FACT_COLUMNS)
        return f"SELECT {cols}\nFROM (SELECT {stored}, {inner} known"

    def facts_either(self) -> str:
        return "SELECT * FROM keel_facts_now(tnt) WHERE k IS NULL UNION ALL SELECT * FROM keel_facts_known(tnt, k) WHERE k IS NOT NULL"

    def visible_facts(self) -> str:
        return ", ".join(FACT_COLUMNS)

    def relations_known(self) -> str:
        return "SELECT src, rel, dst, valid_from, recorded_at, source, CASE"

    def first_of(self, x: str, order: str, keep: str) -> str:
        return f"(array_agg({x} ORDER BY {order} DESC) FILTER (WHERE {keep}))[1]"

    def date_diff_days(self, a: str, b: str) -> str:
        return f"({b} - {a})"

    def idiv(self, a: str, b: str) -> str:
        return f"div({a}, {b})"

    def json_type(self, v: str) -> str:
        return f"jsonb_typeof(CAST({v} AS jsonb))"

    def json_str(self, v: str) -> str:
        return f"((CAST({v} AS jsonb) #>> '{{}}') COLLATE \"C\")"

    def json_num(self, v: str, jt: str) -> str:
        return (f"CASE WHEN {jt} = 'number' THEN CAST(CAST({v} AS jsonb) #>> '{{}}' AS double precision) "
                f"WHEN {jt} = 'boolean' THEN CASE WHEN {v} = 'true' THEN 1.0 ELSE 0.0 END END")

    def json_eq(self, a: str, b: str) -> str:
        return f"(CAST({a} AS jsonb) -> 0) = CAST({b} AS jsonb)"

    def epoch(self, x: str) -> str:
        return f"EXTRACT(epoch FROM keel_try_ts({x}))"

    def weekday(self, d: str) -> str:
        return f"(CAST(EXTRACT(isodow FROM {d}) AS integer) - 1)"


_EITHER = "-- keel_facts: as stored, or as known at k"


class SQLite(DuckDB):
    """The SQLite dialect (3.39+), for SqliteStore's own tables. SQLite has no table macros, so a compiled cartridge
    keeps each macro's parameters and body, and `expand` inlines the calls of a query, its arguments as literals,
    into one statement: a rule's intermediate results become materialized CTEs. The helpers are Python functions
    registered on the connection (`register`), the same ones Keel's own code runs (parse_iso, global_of, casefold).

        compiled = SQLite.compile(cartridge)
        conn.execute(compiled.expand("SELECT subject FROM tire_critical_at('acme', '2026-10-01T00:00:00+00:00')"))
    """

    name = "sqlite"
    JT_STR, JT_NULL = "'text'", "'null'"  # json_type's names
    MIN_VERSION = (3, 39, 0)  # IS [NOT] DISTINCT FROM; also window functions, FILTER, MATERIALIZED, ->

    def __init__(self) -> None:
        self.defs: dict[str, tuple[list[tuple[str, str | None]], str]] = {}
        self.skipped: dict[str, str] = {}  # rule -> why it did not compile (Tools evaluates it in Python)

    @classmethod
    def compile(cls, cartridge: Any) -> SQLite:
        """The point-in-time macros and every rule that compiles; one that does not is kept in `skipped`."""
        d = cls()
        multi = sorted(p for p, spec in (cartridge.predicates or {}).items() if isinstance(spec, dict) and spec.get("multi"))
        symmetric = {r for r, spec in (cartridge.relations or {}).items() if isinstance(spec, dict) and spec.get("symmetric")}
        _state(d, multi)
        for name, spec in cartridge.rules.items():
            if spec.get("when"):
                defs = dict(d.defs)
                try:
                    _Rule(d, cartridge.calendar, symmetric).macros(name, spec)
                except NotImplementedError as exc:
                    d.defs, d.skipped[name] = defs, str(exc)
        return d

    def schema(self) -> str:
        raise NotImplementedError("SqliteStore owns its tables (keel/store/sqlite.py)")

    def prelude(self) -> str:
        return ""

    def table_macro(self, name: str, params: str, body: str, returns: str = "") -> str:
        args = [(p.split(" := ")[0], p.split(" := ")[1] if " := " in p else None) for p in params.split(", ")]
        self.defs[name] = (args, body)
        return ""

    def rule_verdicts(self, name: str, params: str, ctes: list[tuple[str, bool, str, list[str]]], select: str) -> str:
        """Every intermediate result materialized: a lookup by subject then probes it (SQLite indexes it on the fly)."""
        body = ",\n".join(f"{n} AS MATERIALIZED ({b})" for n, _, b, _ in ctes)
        return self.table_macro(name, params, f"WITH {body}\n{select}")

    # ---- expansion ----

    def expand(self, sql: str, env: dict[str, str] | None = None) -> str:
        """`sql` with every call of a compiled macro inlined as a subquery and every name in `env` (a macro's
        parameters) replaced by its literal. String literals are copied untouched."""
        env = env or {}
        out, i, n = [], 0, len(sql)
        while i < n:
            c = sql[i]
            if c == "'":
                j = _end_of_string(sql, i)
                out.append(sql[i:j])
                i = j
                continue
            m = _WORD.match(sql, i) if c.isalpha() or c == "_" else None
            if m is None:
                out.append(c)
                i += 1
                continue
            word, j = m.group(0), m.end()
            if i and (sql[i - 1] == "." or sql[i - 1].isdigit()):  # a column of an alias (c1.t), a number's exponent
                out.append(word)
                i = j
                continue
            k = j
            while k < n and sql[k].isspace():
                k += 1
            if word in self.defs and k < n and sql[k] == "(":
                close = _close_paren(sql, k)
                out.append("(" + self._call(word, _split_args(sql[k + 1 : close]), env) + ")")
                i = close + 1
                continue
            out.append(env.get(word, word))
            i = j
        return "".join(out)

    def _call(self, name: str, args: list[str], env: dict[str, str]) -> str:
        params, body = self.defs[name]
        values: dict[str, str] = {}
        for pos, a in enumerate(args):
            key, sep, v = a.partition(":=")
            if sep:
                values[key.strip()] = self.expand(v.strip(), env)
            else:
                values[params[pos][0]] = self.expand(a.strip(), env)
        for p, default in params:
            values.setdefault(p, default or "NULL")
        if body == _EITHER:  # k is a literal here: pick the branch, never run both
            known = values["k"] != "NULL"
            return self.expand("SELECT * FROM keel_facts_known(tnt, k)" if known else "SELECT * FROM keel_facts_now(tnt)", values)
        return self.expand(body, values)

    # ---- the bodies (column lists, as Postgres) ----

    facts_star = Postgres.facts_star
    facts_known = Postgres.facts_known
    visible_facts = Postgres.visible_facts
    relations_known = Postgres.relations_known

    def facts_either(self) -> str:
        return _EITHER

    def first_of(self, x: str, order: str, keep: str) -> str:
        return f"keel_arg_max({x}, {order}) FILTER (WHERE {keep})"

    def date_diff_days(self, a: str, b: str) -> str:
        return f"CAST(julianday({b}) - julianday({a}) AS INTEGER)"

    def idiv(self, a: str, b: str) -> str:
        return f"({a} / {b})"

    def json_type(self, v: str) -> str:
        return f"json_type({v})"

    def json_str(self, v: str) -> str:
        return f"json_extract({v}, '$')"

    def json_num(self, v: str, jt: str) -> str:
        return (f"CASE WHEN {jt} IN ('integer', 'real') THEN CAST(json_extract({v}, '$') AS REAL) "
                f"WHEN {jt} IN ('true', 'false') THEN CASE WHEN {v} = 'true' THEN 1.0 ELSE 0.0 END END")

    def json_eq(self, a: str, b: str) -> str:
        return f"(({a}) -> '$[0]') = json({b})"

    def epoch(self, x: str) -> str:
        return f"keel_epoch({x})"

    def day(self, x: str) -> str:
        return f"substr({x}, 1, 10)"  # 'YYYY-MM-DD': text order is date order, julianday and strftime read it

    def weekday(self, d: str) -> str:
        return f"((CAST(strftime('%w', {d}) AS INTEGER) + 6) % 7)"

    def lower(self, x: str) -> str:
        return f"keel_casefold({x})"  # what keel/rules.py compares (SQLite's lower folds ASCII only)

    def date_lit(self, d: str) -> str:
        return f"'{d}'"

    def kind_is(self, x: str, kind: str) -> str:
        # the keys from 'kind:' up to 'kind;' (';' follows ':'): a range of the (tenant, subject) index
        return f"(({x} >= {self.lit(kind + ':')} AND {x} < {self.lit(kind + ';')}) OR {x} = {self.lit(kind)})"

    @staticmethod
    def register(conn: Any) -> None:
        """The helpers the expanded SQL calls, as SQLite functions on `conn` (a sqlite3 connection)."""
        from keel.envelope import parse_iso, utcnow
        from keel.scopes import global_of

        def iso(x: Any) -> str | None:
            return None if x is None else parse_iso(x).astimezone(timezone.utc).isoformat()

        def epoch(x: Any) -> float | None:
            try:
                return None if x is None else parse_iso(x).timestamp()
            except (TypeError, ValueError):
                return None

        def days(a: Any, b: Any) -> float | None:
            return None if a is None or b is None else (parse_iso(b) - parse_iso(a)).total_seconds() / 86400

        def least(*xs: Any) -> Any:
            return min((x for x in xs if x is not None), default=None)

        def greatest(*xs: Any) -> Any:
            return max((x for x in xs if x is not None), default=None)

        class ArgMax:
            def __init__(self) -> None:
                self.x, self.order = None, None

            def step(self, x: Any, order: Any) -> None:
                if order is not None and (self.order is None or order > self.order):
                    self.x, self.order = x, order

            def finalize(self) -> Any:
                return self.x

        for name, n, fn in (("keel_iso", 1, iso), ("keel_epoch", 1, epoch), ("keel_days", 2, days), ("least", -1, least),
                            ("greatest", -1, greatest), ("keel_global", 1, lambda t: None if t is None else global_of(t)),
                            ("keel_casefold", 1, lambda x: None if x is None else str(x).casefold())):
            conn.create_function(name, n, fn, deterministic=True)
        conn.create_function("keel_now", 0, lambda: utcnow().isoformat())
        conn.create_aggregate("keel_arg_max", 2, ArgMax)


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _end_of_string(sql: str, i: int) -> int:
    """The index just past the string literal opening at sql[i] ('' is a quote inside it)."""
    j = i + 1
    while True:
        j = sql.index("'", j)
        if j + 1 < len(sql) and sql[j + 1] == "'":
            j += 2
            continue
        return j + 1


def _close_paren(sql: str, i: int) -> int:
    """The index of the ')' closing the '(' at sql[i]."""
    depth, j = 0, i
    while True:
        c = sql[j]
        if c == "'":
            j = _end_of_string(sql, j)
            continue
        depth += c == "("
        depth -= c == ")"
        if depth == 0:
            return j
        j += 1


def _split_args(s: str) -> list[str]:
    """A call's arguments: split on the commas outside parentheses and string literals."""
    if not s.strip():
        return []
    out, depth, start, j = [], 0, 0, 0
    while j < len(s):
        c = s[j]
        if c == "'":
            j = _end_of_string(s, j)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(s[start:j])
            start = j + 1
        j += 1
    return out + [s[start:]]


DIALECTS = {"duckdb": DuckDB, "postgres": Postgres}


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
        d.table_macro("keel_facts_now", "tnt", f"SELECT {d.facts_star()} FROM facts WHERE tenant IN (tnt, keel_global(tnt)) AND source IS DISTINCT FROM {close}", FACTS),
        d.table_macro("keel_facts_known", "tnt, k", d.facts_known(
            "least(next_from, CASE WHEN stored_to IS NOT NULL AND (closed_at IS NULL OR closed_at <= keel_iso(k)) THEN stored_to END, mark_at)",
            f"""lead(f.valid_from) OVER (PARTITION BY {slot} ORDER BY {succession}) AS next_from,
             ({marks}) AS mark_at
      FROM facts f
      WHERE f.tenant IN (tnt, keel_global(tnt)) AND f.source IS DISTINCT FROM {close}
        AND (f.recorded_at IS NULL OR f.recorded_at <= keel_iso(k)))"""), FACTS),
        d.table_macro("keel_facts", "tnt, k := NULL", d.facts_either(), FACTS),
        d.table_macro("facts_known_at", "tnt, t, k := NULL, alive := NULL",
                      f"SELECT * FROM keel_facts(tnt, k := k) WHERE tenant = tnt AND {at} AND {alive}", FACTS),
        # what rules (and agents) read: accepted facts the caller may see, with their JSON decoded once
        d.table_macro("keel_visible", "tnt, k := NULL, alive := NULL, as_author := NULL, as_task := NULL",
                      f"SELECT *, {jt} AS jt, CASE WHEN {jt} = {d.JT_STR} THEN {d.json_str('value')} END AS vs, {d.json_num('value', jt)} AS vn "
                      f"FROM keel_facts(tnt, k := k) WHERE {visible} AND {alive}", VISIBLE),
        d.table_macro("state_known_at", "tnt, t, k := NULL, alive := NULL, as_author := NULL, as_task := NULL",
                      f"SELECT {d.visible_facts()} FROM keel_visible(tnt, k := k, alive := alive, as_author := as_author, as_task := as_task) WHERE {at}", FACTS),
        d.table_macro("state_at", "tnt, t", "SELECT * FROM state_known_at(tnt, t)", FACTS),
        d.table_macro("relations_known_at", "tnt, t, k := NULL", f"""SELECT src, rel, dst, valid_from, valid_to, recorded_at, source FROM (
  {d.relations_known()} WHEN k IS NOT NULL AND closed_at IS NOT NULL AND closed_at > keel_iso(k) THEN NULL ELSE valid_to END AS valid_to
  FROM relations WHERE tenant = tnt AND NOT (valid_to IS NOT NULL AND valid_to = valid_from)
    AND (k IS NULL OR recorded_at IS NULL OR recorded_at <= keel_iso(k)))
WHERE valid_from <= keel_iso(t) AND (valid_to IS NULL OR valid_to > keel_iso(t))""", RELATIONS),
        d.table_macro("relations_at", "tnt, t", "SELECT * FROM relations_known_at(tnt, t)", RELATIONS),
    ])


# ---- rules ------------------------------------------------------------------------------------------------


class _Rule:
    """One rule's `when` as a SQL expression over the subject, with keel/rules.py's semantics: Kleene logic
    (SQL's own three values), a leaf null when its predicate has no current fact or a comparison would raise,
    comparisons checked in order (the first that fails or raises decides), `via` over neighbours."""

    T = "keel_iso(t)"

    def __init__(self, d: Any, calendar: Calendar, symmetric: set[str]) -> None:
        self.d, self.calendar, self.symmetric = d, calendar, symmetric
        self.ctes: list[tuple[str, bool, str, list[str]]] = []  # (name, materialized, body, the columns a lookup goes by)
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
        subject_kind = d.kind_is("subject", kind) if kind else "TRUE"
        ctes = [
            ("vis", True, (f"SELECT * FROM keel_visible(tnt, k := k, alive := alive, as_author := as_author, as_task := as_task) "
                           f"WHERE predicate IN ({preds}) AND valid_from <= {self.T}"), []),  # joined, never probed
            ("cur", True, (f"SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY subject, predicate "
                           f"ORDER BY valid_from DESC, (tenant <> tnt) DESC, seq DESC) AS rn FROM vis WHERE valid_to IS NULL OR valid_to > {self.T}) WHERE rn = 1"),
             ["subject, predicate"]),
            ("nbr", True, "SELECT * FROM relations_known_at(tnt, t, k := k)", ["src, rel", "dst, rel"]),
            *self.ctes,
        ]
        # every subject Keel knows of that kind: an entity, a fact's subject, a relation's end
        select = (f"SELECT s.subject, {expr} AS verdict FROM (\n"
                  f"  SELECT kind || ':' || id AS subject FROM entities WHERE tenant = tnt AND {of_kind}\n"
                  f"  UNION SELECT subject FROM facts WHERE tenant = tnt AND {subject_kind}\n"
                  f"  UNION SELECT src FROM relations WHERE tenant = tnt AND {d.kind_is('src', kind) if kind else 'TRUE'}\n"
                  f"  UNION SELECT dst FROM relations WHERE tenant = tnt AND {d.kind_is('dst', kind) if kind else 'TRUE'}) s")
        at = f"SELECT v.subject FROM {m}_verdicts(tnt, t, k := k, alive := alive, as_author := as_author, as_task := as_task) v " \
             f"JOIN entities e ON e.tenant = tnt AND e.kind || ':' || e.id = v.subject WHERE v.verdict"
        return d.rule_verdicts(f"{m}_verdicts", params, ctes, select) + d.table_macro(f"{m}_at", params, at, "subject text")

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
        out = f"SELECT {r}.dst AS o FROM nbr {r} WHERE {r}.rel = {lit} AND {r}.src = {subject}"
        inn = f"SELECT {r}.src AS o FROM nbr {r} WHERE {r}.rel = {lit} AND {r}.dst = {subject}"
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
        return f"(SELECT {agg} FROM (SELECT {x} AS x FROM ({others}) {n}) {v})"

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
            return f"(CASE WHEN {a}.jt = {self.d.JT_STR} THEN {self.d.epoch(value)} <= {self.d.epoch(self.T)} + {float(arg) * 86400!r} END)"
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
            return f"({a}.jt = {self.d.JT_NULL})"
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
        self.ctes.append((h, False, f"""SELECT subject,
    {self.d.first_of("status", "rn", "status <> 'ok'")} = 'e' AS err,
    coalesce({self.d.first_of("nxt", "rn", "status <> 'ok'")}, min(valid_from)) AS since
  FROM (SELECT h.subject, h.valid_from, row_number() OVER w AS rn, {nxt} AS nxt,
               CASE WHEN h.valid_to IS DISTINCT FROM {nxt} THEN 'b' WHEN ({m}) IS NULL THEN 'e' WHEN NOT ({m}) THEN 'b' ELSE 'ok' END AS status
        FROM vis h JOIN cur cc ON cc.subject = h.subject AND cc.predicate = h.predicate
        WHERE h.predicate = {self.d.lit(p)} AND h.valid_from < cc.valid_from
        WINDOW w AS (PARTITION BY h.subject ORDER BY h.valid_from, h.seq))
  GROUP BY subject""", ["subject"]))
        return h

    def business_days(self, start: str, end: str) -> str:
        """Calendar.business_days in arithmetic: whole days from start's day to end's day, per weekday not in
        the weekend, minus the holidays in range that fall on a working day."""
        d = self.d
        a, b = d.day(start), d.day(end)
        n = f"greatest({d.date_diff_days(a, b)}, 0)"
        per_day = [f"({d.idiv(n, '7')} + CASE WHEN (({w} - {d.weekday(a)}) % 7 + 7) % 7 < {n} % 7 THEN 1 ELSE 0 END)"
                   for w in range(7) if w not in self.calendar.weekend]
        holidays = [f"CASE WHEN {a} <= {d.date_lit(h.isoformat())} AND {d.date_lit(h.isoformat())} < {b} THEN 1 ELSE 0 END"
                    for h in sorted(self.calendar.holidays) if h.weekday() not in self.calendar.weekend]
        return "(" + (" + ".join(per_day) or "0") + (" - " + " - ".join(holidays) if holidays else "") + ")"
