"""The five tools. Every agent, in every cartridge, talks to the graph through these.

They are plain functions first, so any framework can call them directly;
`keel.mcp_server` wraps the same object for MCP.

    context(subject)                   what is true now about this subject
    history(subject, since, until)     what was true, and when we learned it
    search(text)                       entities and facts matching text
    annotate(scope, fact, evidence)    an agent or a human writes what it learned
    related(subject, relation)         neighbors by relation type
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import asdict
from datetime import datetime
from typing import Any

from keel.agents import resolve
from keel.cartridge import Cartridge
from keel.envelope import parse_iso, utcnow
from keel.rules import Calendar, evaluate
from keel.scopes import GLOBAL_TENANT, Scope
from keel.semantic import (
    accepts,
    as_text,
    embed_as,
    fuse,
    render,
    split_hash,
    subject_of,
    tokens,
    value_hash,
)
from keel.store.base import DuplicateKey, Entity, Fact, Relation, Store
from keel.store.memory import changed_after, filter_facts, search_in


def _ser(o: Any) -> Any:
    if isinstance(o, Entity):
        d = asdict(o)
        out = {k: _ser(v) for k, v in d.items() if k != "attrs"}
        return {**out, "key": o.key, "attrs": _ser(d["attrs"])}  # the '<kind>:<id>' every other call takes
    if isinstance(o, Fact):
        d = asdict(o)
        return {k: _ser(v) for k, v in d.items()}
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Scope):
        return o.value
    if isinstance(o, dict):
        return {k: _ser(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_ser(v) for v in o]
    return o


Instant = datetime | str | None  # every time argument: a datetime or ISO 8601 text, as the tools return it


def _t(v: Instant) -> datetime | None:
    """Accept what the tools hand out (ISO text: `watermark`, `as_of`, `recorded_at`) as readily as a datetime."""
    return parse_iso(v) if isinstance(v, str) else v


def _conflicts(facts: list[Fact]) -> dict[str, list[dict[str, Any]]]:
    """Predicates with more than one open value: authors disagree, and the reader should see it."""
    by: dict[str, list[Fact]] = {}
    for f in facts:
        by.setdefault(f.predicate, []).append(f)
    return {
        p: [{"value": f.value, "by": f.corrected_by, "source": f.source, "recorded_at": f.recorded_at} for f in fs]
        for p, fs in by.items()
        if len({repr(f.value) for f in fs}) > 1
    }


SNIPPET = 160  # characters of a matching value a search result quotes


def _same(a: Any, b: Any) -> bool:
    return a.casefold() == b.casefold() if isinstance(a, str) and isinstance(b, str) else a == b


def _holds(value: Any, want: Any) -> bool:
    """`where`: the value is `want`, or a list holding it; text compares without case."""
    return _same(value, want) or (isinstance(value, list) and any(_same(v, want) for v in value))


def _snippet(value: Any, needles: list[str]) -> str:
    """The part of a value around the first needle: of a list, the element that holds one."""
    if isinstance(value, list):
        value = next((v for v in value if any(n in as_text(v).lower() for n in needles)), value)
    s = as_text(value)
    if len(s) <= SNIPPET:
        return s
    at = [i for n in needles if (i := s.lower().find(n)) >= 0]
    start = max(0, min(at, default=0) - SNIPPET // 3)
    return ("…" if start else "") + s[start : start + SNIPPET] + ("…" if start + SNIPPET < len(s) else "")


def _compact_facts(facts: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    by_pred: dict[str, Any] = {}
    for f in facts:
        if f["predicate"] in out:  # a multi-valued predicate: every author's value
            prev = out[f["predicate"]]
            first = by_pred[f["predicate"]]
            out[f["predicate"]] = (prev if isinstance(prev, list) and prev and isinstance(prev[0], dict) and "by" in prev[0] else [{"value": prev, "by": first}]) + [{"value": f["value"], "by": f.get("corrected_by") or f["source"]}]
        else:
            out[f["predicate"]] = f["value"]
            by_pred[f["predicate"]] = f.get("corrected_by") or f["source"]
    return out


def _direction(rel: str, src: str, subject: str, symmetric: set[str]) -> str:
    """One answer everywhere: `both` for a symmetric relation, else relative to the subject."""
    return "both" if rel in symmetric else ("out" if src == subject else "in")


def _concise(ctx: dict[str, Any], symmetric: set[str] = frozenset()) -> dict[str, Any]:
    """The same answer in about a third of the tokens: what an LLM needs to act, ids kept."""
    rel = lambda r, s: {"relation": r["rel"], "direction": _direction(r["rel"], r["src"], s, symmetric), "subject": r["dst"] if r["src"] == s else r["src"]}
    out: dict[str, Any] = {
        "subject": ctx["subject"],
        "attrs": (ctx["entity"] or {}).get("attrs", {}),
        "facts": _compact_facts(ctx["facts"]),
        "relations": [rel(r, ctx["subject"]) for r in ctx["relations"]],
        "evaluation": ctx["evaluation"],
        "as_of": ctx["as_of"],
        "watermark": ctx.get("watermark"),
    }
    for k in ("as_known_at", "conflicts", "neighbors_truncated"):
        if k in ctx:
            out[k] = ctx[k]
    if "proposals" in ctx:
        out["proposals"] = [{"predicate": f["predicate"], "value": f["value"], "by": f.get("corrected_by") or f["source"], "evidence": f["evidence"]} for f in ctx["proposals"]]
    if "neighbors" in ctx:
        out["neighbors"] = [
            {"relation": n["relation"], "direction": n["direction"], "subject": n["subject"], "facts": _compact_facts(n["facts"]), "evaluation": n["evaluation"]}
            for n in ctx["neighbors"]
        ]
    return out


def _vias(cond: Any) -> set[str]:
    """The relations a rule walks (`via` anywhere in it)."""
    if isinstance(cond, dict):
        return ({cond["via"]} if "via" in cond else set()).union(*(_vias(v) for v in cond.values()))
    return set().union(*(_vias(v) for v in cond)) if isinstance(cond, list) else set()


def _kind_list(kind: str | list[str] | None) -> list[str] | None:
    """`kind` as a list: one kind, several, or None for any."""
    if kind is None:
        return None
    kinds = [kind] if isinstance(kind, str) else list(dict.fromkeys(kind))
    if not kinds or not all(isinstance(k, str) and k for k in kinds):
        raise ValueError("kind is a kind's name or a list of them")
    return kinds


def _kind(subject: str) -> str:
    return subject.split(":", 1)[0]


NEIGHBOR_CAP = 50  # `context(depth=1)` stops here and says so; ask `related` for the full list
SWEEP_CHUNK = 200  # `search(rule=)` reads the facts of this many candidates at once


class _Graph:
    """What rules may look at, read through the store at one instant. Rules are the product's
    semantics, so they see the whole tenant whatever the calling agent's read slice is (kinds and
    relations); memory scopes still apply: another agent's private memory, or another task's, is
    not the caller's to read, not even through a rule."""

    def __init__(
        self, store: Store, tenant: str, as_of: datetime, calendar: Calendar, symmetric: set[str],
        known_at: datetime | None = None, task: str | None = None,
        fetch: Callable[..., list[Fact]] | None = None,
        fetch_many: Callable[..., dict[str, list[Fact]]] | None = None,
    ) -> None:
        self.store, self.tenant, self.as_of, self.calendar, self.symmetric = store, tenant, as_of, calendar, symmetric
        self.known_at, self.task = known_at, task
        self.fetch = fetch or (lambda subject, task, **kw: store.facts(tenant, subject, **kw))
        self.fetch_many = fetch_many or (lambda subjects, task, **kw: {s: self.fetch(s, task, **kw) for s in subjects})
        self._facts: dict[str, list[Fact]] = {}
        self._proposed: dict[str, list[Fact]] = {}
        self._rels: dict[str, list[Relation]] = {}

    def prime_relations(self, subjects: Iterable[str]) -> None:
        """Read the relations of many subjects at once: a sweep of a rule that follows a relation."""
        missing = list(dict.fromkeys(s for s in subjects if s not in self._rels))
        many = getattr(self.store, "relations_of", None)
        if not missing or many is None:
            return
        for s, rels in many(self.tenant, missing, at=self.as_of).items():
            self._rels[s] = self._known(rels)

    def _known(self, rels: list[Relation]) -> list[Relation]:
        if self.known_at is not None:
            rels = [r for r in rels if r.recorded_at is None or r.recorded_at <= self.known_at]
        return rels

    def prime(self, subjects: Iterable[str]) -> None:
        """Read the facts of many subjects at once, ahead of `facts()`: a neighbourhood or a rule
        sweep costs a few statements instead of one or two per subject."""
        missing = list(dict.fromkeys(s for s in subjects if s not in self._facts))
        if not missing:
            return
        kw: dict[str, Any] = {"at": self.as_of}
        if self.known_at is not None:
            kw["known_at"] = self.known_at
        for s, facts in self.fetch_many(missing, self.task, **kw).items():
            self._facts[s] = [f for f in facts if f.status is None]
            self._proposed[s] = [f for f in facts if f.status == "proposed"]

    def _read(self, subject: str, proposed: bool = False, **kw: Any) -> list[Fact]:
        if self.known_at is not None:
            kw["known_at"] = self.known_at  # only passed when asked: a store from before 0.9 does not take it
        facts = self.fetch(subject, self.task, **kw)
        wanted = "proposed" if proposed else None  # accepted facts are the truth rules and agents read
        return [f for f in facts if f.status == wanted]

    def proposals(self, subject: str) -> list[Fact]:
        if subject in self._proposed:
            return self._proposed[subject]
        return [f for f in self._read(subject, at=self.as_of, proposed=True)]

    def facts(self, subject: str) -> list[Fact]:
        if subject not in self._facts:
            self._facts[subject] = self._read(subject, at=self.as_of)
        return self._facts[subject]

    def history(self, subject: str, predicate: str) -> list[Fact]:
        return [f for f in self._read(subject, until=self.as_of) if f.predicate == predicate]

    def relations(self, subject: str, rel: str | None = None) -> list[Relation]:
        if subject not in self._rels:
            self._rels[subject] = self._known(self.store.relations(self.tenant, subject, at=self.as_of))
        return [r for r in self._rels[subject] if rel is None or r.rel == rel]

    def neighbors(self, subject: str, rel: str, direction: str) -> list[str]:
        out: list[str] = []
        for r in self.relations(subject, rel):
            if rel not in self.symmetric and direction == "out" and r.src != subject:
                continue
            if rel not in self.symmetric and direction == "in" and r.dst != subject:
                continue
            other = r.dst if r.src == subject else r.src
            if other not in out:
                out.append(other)
        self.prime(out)  # a rule that follows a relation reads every neighbour's facts next
        return out


class Tools:
    def __init__(
        self, cartridge: Cartridge, store: Store, tenant: str, agent: str | None = None, trace: str | None = None,
        embedder: Any = None, index: Any = None, reranker: Any = None,
    ) -> None:
        self.cartridge = cartridge
        self.store = store
        if tenant == GLOBAL_TENANT:
            raise ValueError(f"tenant '{GLOBAL_TENANT}' is reserved for global-scope memory")
        self.tenant = tenant  # tools are always scoped to one end-customer
        self.agent_id = agent  # who is calling; the host decides, Keel records
        self.agent = resolve(cartridge.agents, agent)  # the spec to enforce; None when the cartridge declares no agents
        # the author this caller writes as, and the only one whose agent-scope memory it reads
        self.author = f"human:{agent}" if self.agent and self.agent.kind == "human" else agent
        self.symmetric = {rel for rel, spec in cartridge.relations.items() if isinstance(spec, dict) and spec.get("symmetric")}
        self.multi = {p for p, spec in cartridge.predicates.items() if isinstance(spec, dict) and spec.get("multi")}
        self.trace = trace  # set per request by the transport (traceparent, tool call id); stored with every write
        self.embedder, self.index, self.reranker = embedder, index, reranker  # keel.semantic adapters

    # ---- registry enforcement: the agent never polices itself -------------------------------

    def _readable(self, subject: str) -> None:
        if self.agent and not self.agent.sees_kind(_kind(subject)):
            raise PermissionError(f"agent '{self.agent.id}' may not read kind '{_kind(subject)}'")

    def _visible(self, r: Relation, subject: str) -> bool:
        if self.agent is None:
            return True
        other = r.dst if r.src == subject else r.src
        return self.agent.sees_relation(r.rel) and self.agent.sees_kind(_kind(other))

    def _graph(self, now: datetime, known_at: datetime | None = None, task: str | None = None) -> _Graph:
        return _Graph(self.store, self.tenant, now, self.cartridge.calendar, self.symmetric, known_at, task, self._fetch, self._fetch_many)

    # ---- memory scopes on every read: enforced here, not trusted to the caller ---------------

    def _sees(self, f: Fact, task: str | None) -> bool:
        """agent: only its author (a caller bound to no agent is nobody's agent and sees none).
        task: a fact tied to a task only to reads of that task; one tied to none, to the tenant."""
        if f.scope == Scope.AGENT:
            return self.author is not None and f.source == f"annotate:{self.author}"
        if f.scope == Scope.TASK and f.task is not None:
            return f.task == task
        return True

    def _fetch(self, subject: str, task: str | None = None, **kw: Any) -> list[Fact]:
        """A subject's facts in this tenant plus the global partition's, as this caller may see them."""
        # ponytail: one extra indexed lookup per subject read; a has-global flag if it ever shows in profiles
        glob = [f for f in self.store.facts(GLOBAL_TENANT, subject, **kw) if f.scope == Scope.GLOBAL]
        facts = self.store.facts(self.tenant, subject, **kw)
        if glob:
            facts = sorted(facts + glob, key=lambda f: f.valid_from)
        return [f for f in facts if self._sees(f, task)]

    def _fetch_many(self, subjects: list[str], task: str | None = None, **kw: Any) -> dict[str, list[Fact]]:
        """`_fetch` for many subjects: two statements per chunk on a store with `facts_of`, else one
        `_fetch` per subject."""
        many = getattr(self.store, "facts_of", None)
        if many is None:
            return {s: self._fetch(s, task, **kw) for s in subjects}
        mine, glob = many(self.tenant, subjects, **kw), many(GLOBAL_TENANT, subjects, **kw)
        out = {}
        for s in subjects:
            g = [f for f in glob.get(s, []) if f.scope == Scope.GLOBAL]
            facts = sorted(mine.get(s, []) + g, key=lambda f: f.valid_from) if g else mine.get(s, [])
            out[s] = [f for f in facts if self._sees(f, task)]
        return out

    def _entities(self, keys: list[str]) -> dict[str, Entity | None]:
        many = getattr(self.store, "entities_of", None)
        return many(self.tenant, keys) if many else {k: self.store.entity(self.tenant, k) for k in keys}

    def _watermark(self) -> datetime | None:
        wm = getattr(self.store, "watermark", None)
        marks = [m for m in (wm(self.tenant), wm(GLOBAL_TENANT)) if m is not None] if wm else []
        return max(marks) if marks else None

    def _rules_for(self, subject: str) -> dict[str, Any]:
        kind = _kind(subject)
        return {k: v for k, v in self.cartridge.rules.items() if v.get("applies_to") in (None, kind)}

    def _evaluation(self, subject: str, graph: _Graph) -> dict[str, bool | None]:
        return {k: evaluate(v, subject, graph.as_of, graph) for k, v in self._rules_for(subject).items() if v.get("when")}

    def _dedupe(self, rels: list[Relation], subject: str) -> list[Relation]:
        """A symmetric relation recorded from both ends is one edge."""
        seen: set[tuple[str, frozenset[str]]] = set()
        out = []
        for r in rels:
            key = (r.rel, frozenset({r.src, r.dst}))
            if r.rel in self.symmetric:
                if key in seen:
                    continue
                seen.add(key)
            out.append(r)
        return out

    def context(
        self,
        subject: str | list[str],
        as_of: Instant = None,
        depth: int = 0,
        as_known_at: Instant = None,
        task: str | None = None,
        detail: str = "full",
        exclude: list[str] | None = None,
        only: list[str] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Truth about a subject now (or at `as_of`): entity, open facts and relations, the rules
        that apply to its kind, and the evaluation of every rule that carries a `when`. With
        `depth=1`, also each neighbour's entity, open facts and evaluation (up to NEIGHBOR_CAP).
        `as_known_at` answers what Keel knew then (transaction time; `as_of` defaults to it): what
        an agent saw when it decided. `task` is the task the caller works on: task-scope memory tied to
        a task is read only by that task (memory tied to none is the tenant's), agent-scope memory only by
        its author. Global-scope memory is every tenant's. `watermark`
        is the tenant's latest transaction time, the cursor for `history(changed_since=)`. `proposals`
        lists pending proposals; `conflicts` lists predicates with more than one open value (multi-valued
        predicates written by several authors). `detail="concise"` returns about a third of the tokens:
        facts as {predicate: value}, relations as (relation, direction, subject), no rule definitions.
        A list of subjects returns their answers in order, in one call. `exclude` hides those predicates
        from the facts, proposals and neighbours returned (an evaluation where the agent must not see the
        answer); rules still evaluate on the whole truth. `only` returns just those predicates (the
        evidence a reviewer needs from many subjects, without the rest of their facts); with a list of
        subjects, their facts and relations are read in a few statements, not one per subject."""
        if detail not in ("full", "concise"):
            raise ValueError("detail is 'full' or 'concise'")
        if only is not None and (not isinstance(only, list) or not only or not all(isinstance(p, str) for p in only)):
            raise ValueError("only is a non-empty list of predicates")
        subjects = subject if isinstance(subject, list) else [subject]
        for s in subjects:
            self._readable(s)
        as_of, as_known_at = _t(as_of), _t(as_known_at)
        now = as_of or as_known_at or utcnow()
        graph = self._graph(now, as_known_at, task)
        if len(subjects) > 1:
            graph.prime(subjects)
            graph.prime_relations(subjects)
        hide, keep = set(exclude or ()), set(only) if only is not None else None
        shown = lambda fs: [f for f in fs if f.predicate not in hide and (keep is None or f.predicate in keep)]
        entities, watermark = self._entities(subjects), self._watermark()
        answers = [self._context(s, graph, now, as_known_at, depth, detail, shown, entities[s], watermark) for s in subjects]
        return answers if isinstance(subject, list) else answers[0]

    def _context(
        self, subject: str, graph: _Graph, now: datetime, as_known_at: datetime | None, depth: int, detail: str,
        shown: Callable[[list[Fact]], list[Fact]], entity: Entity | None, watermark: datetime | None,
    ) -> dict[str, Any]:
        rels = self._dedupe([r for r in graph.relations(subject) if self._visible(r, subject)], subject)
        out: dict[str, Any] = {
            "subject": subject,
            "entity": entity,
            "facts": shown(graph.facts(subject)),
            "relations": [asdict(r) for r in rels],
            "rules": self._rules_for(subject),
            "evaluation": self._evaluation(subject, graph),
            "as_of": now,
            "watermark": watermark,
        }
        if as_known_at is not None:
            out["as_known_at"] = as_known_at
        proposals = shown(graph.proposals(subject))
        if proposals:
            out["proposals"] = proposals
        conflicts = _conflicts(out["facts"])
        if conflicts:
            out["conflicts"] = conflicts
        if depth >= 1:
            neighbors = []
            others = [r.dst if r.src == subject else r.src for r in rels[:NEIGHBOR_CAP]]
            graph.prime(others)
            entities = self._entities(others)
            for r, other in zip(rels[:NEIGHBOR_CAP], others):
                neighbors.append(
                    {
                        "relation": r.rel,
                        "direction": _direction(r.rel, r.src, subject, self.symmetric),
                        "subject": other,
                        "entity": entities[other],
                        "facts": shown(graph.facts(other)),
                        "evaluation": self._evaluation(other, graph),
                    }
                )
            out["neighbors"] = neighbors
            if len(rels) > NEIGHBOR_CAP:
                out["neighbors_truncated"] = True
        return _concise(_ser(out), self.symmetric) if detail == "concise" else _ser(out)

    def history(
        self,
        subject: str | None = None,
        since: Instant = None,
        until: Instant = None,
        predicate: str | None = None,
        scope: str | None = None,
        as_known_at: Instant = None,
        task: str | None = None,
        changed_since: Instant = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Facts over time: valid_from/valid_to (world), recorded_at and closed_at (when we learned
        them). Filters: one predicate, one memory scope, one task (only what that task wrote; without it,
        task-scope memory tied to a task is not returned). Another agent's agent-scope memory is never
        returned. `as_known_at`: as Keel knew them
        then. `changed_since` (a watermark from `context`): only facts recorded or closed after it;
        with no `subject` it spans the whole tenant, oldest change first: an agent's "since my last run"."""
        since, until, as_known_at, changed_since = _t(since), _t(until), _t(as_known_at), _t(changed_since)
        if subject is None:
            if changed_since is None:
                raise ValueError("history needs a subject, or changed_since for the whole tenant")
            facts = changed_after(self.store.changes(self.tenant, changed_since) + [f for f in self.store.changes(GLOBAL_TENANT, changed_since) if f.scope == Scope.GLOBAL], changed_since)
            facts = [f for f in facts if self._sees(f, task)]
            if self.agent:
                facts = [f for f in facts if self.agent.sees_kind(_kind(f.subject))]
            if as_known_at is not None:
                facts = filter_facts(facts, known_at=as_known_at)
        else:
            self._readable(subject)
            kw: dict[str, Any] = {"since": since, "until": until}
            if as_known_at is not None:
                kw["known_at"] = as_known_at
            facts = self._fetch(subject, task, **kw)
            if changed_since is not None:
                facts = changed_after(facts, changed_since)
        if predicate:
            facts = [f for f in facts if f.predicate == predicate]
        if scope:
            facts = [f for f in facts if f.scope == Scope(scope)]
        if task:  # only what that task wrote
            facts = [f for f in facts if f.task == task]
        if limit:  # the most recent `limit`, still oldest first; ask for limit + 1 to know whether more exist
            facts = facts[-limit:]
        return _ser(facts)

    def search(
        self,
        text: str | list[str] = "",
        kind: str | list[str] | None = None,
        rule: str | None = None,
        limit: int | None = None,
        after: str | None = None,
        mode: str = "text",
        min_score: float | None = None,
        related_to: str | None = None,
        via: str | None = None,
        detail: str = "entity",
        depth: int = 0,
        per_kind: int | None = None,
        exclude: list[str] | None = None,
        as_of: Instant = None,
        where: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Entities and facts matching text. `mode="semantic"` ranks subjects by meaning through the
        deployment's embedder and vector index (keel.semantic): ids in the query first, then dense and
        lexical legs fused, then the optional reranker; tenant, the agent's read slice, `kind` and `rule`
        apply. Each entity carries `score` and `similarity` (the cosine; `min_score` drops results below it).
        `related_to` (a subject key) keeps only that subject's neighbours, through relation `via` when given:
        "cards of this client where this rule holds" is one call, in every mode. `after` is always a key
        (an id alone is accepted when the kind is known) and applies to the kind/rule listing. With `kind` or `rule` it enumerates instead: the entities
        of that kind (a rule's `applies_to` when only `rule` is given) whose key or attributes match
        `text`, where the rule fires now when `rule` is given, ordered by key; page with `limit` and
        `after` (the last key you got). The work list of a sweep, routing or orchestrator-workers.

        What each result carries:
        `detail="concise"` adds the entity's current facts as {predicate: value}, `"full"` as facts, so
        a result needs no `context` call to be understood; `depth=1` adds its neighbours (relation,
        direction, subject, and their facts when `detail` asks for them). `per_kind=n` returns the best n
        of every kind instead of one ranking: with `related_to` for the scope and `mode="semantic"`, the
        context a task needs in one call. `exclude` hides predicates from what is returned, and drops fact
        hits on them; `as_of` evaluates rules, neighbours and facts at that instant. A list of texts
        returns one result list per text, in order.

        `where={name: value}` keeps the entities whose attribute or current fact `name` is `value` (or
        a list holding it; text compares without case), in every mode: a visual by its exact title is
        `search(kind="visual", where={"title": "..."})`. With `kind`, `text` matches the key, the
        attributes and the current facts' values. Each ranked result says why it is there: `matched`
        (the attribute, predicate, or "key") and `snippet`, the part of the value that matched.

        `kind` may be a list: the listing covers those kinds (still ordered by key), a semantic ranking
        only them, and `per_kind` takes the best n of each of them (the first n by key in text mode)."""
        graph = self._graph(_t(as_of) or utcnow())  # shared by every text of a list: each subject's facts are read once
        if isinstance(text, list):
            return [self._search_one(t, kind, rule, limit, after, mode, min_score, related_to, via, detail, depth, per_kind, exclude, where, graph) for t in text]
        return self._search_one(text, kind, rule, limit, after, mode, min_score, related_to, via, detail, depth, per_kind, exclude, where, graph)

    def _search_one(
        self, text: str, kind: str | list[str] | None, rule: str | None, limit: int | None, after: str | None, mode: str,
        min_score: float | None, related_to: str | None, via: str | None, detail: str, depth: int, per_kind: int | None,
        exclude: list[str] | None, where: dict[str, Any] | None, graph: _Graph,
    ) -> list[Any]:
        if detail not in ("entity", "concise", "full"):
            raise ValueError("search detail is 'entity', 'concise' or 'full'")
        if depth not in (0, 1):
            raise ValueError("search depth is 0 or 1")
        hide = set(exclude or ())
        if where is not None and (not isinstance(where, dict) or not where):
            raise ValueError("where maps an attribute or predicate to the value it must have, e.g. {\"title\": \"Overview\"}")
        if hidden := sorted(set(where or ()) & hide):
            raise ValueError(f"where filters on {hidden}, which exclude hides")
        kinds = _kind_list(kind)
        if per_kind is not None:
            if per_kind < 1:
                raise ValueError("per_kind is at least 1")
            if after is not None:
                raise ValueError("per_kind takes the best of each kind; page one kind with `kind`, `limit` and `after`")
            if mode != "semantic" and kinds and len(kinds) == 1:
                raise ValueError("per_kind with one kind is `limit`: search(text, kind=k, limit=n)")
            if mode == "semantic" or kinds:  # the index filters by kind: one ranking (or listing) per kind
                if not kinds:
                    one = (self.cartridge.rules.get(rule) or {}).get("applies_to") if rule else None
                    kinds = [one] if one else [k for k in self.cartridge.entities if not self.agent or self.agent.sees_kind(k)]
                vector = embed_as(self.embedder, [text], "query")[0] if mode == "semantic" and self.embedder and text else None  # once, not per kind
                hits = [h for k in kinds for h in self._search(text, k, rule, per_kind, None, mode, min_score, related_to, via, graph, where, hide, vector)]
            elif rule is not None:
                raise ValueError("per_kind groups a text or semantic search by kind; with `rule` you list one kind (use `limit`)")
            else:
                taken: dict[str, int] = {}
                hits = []
                for h in self._search(text, None, None, None, None, mode, min_score, related_to, via, graph, where, hide):
                    k = h.get("kind") or _kind(h["subject"])
                    if taken.get(k, 0) < per_kind:
                        taken[k] = taken.get(k, 0) + 1
                        hits.append(h)
        else:
            hits = self._search(text, kinds, rule, limit, after, mode, min_score, related_to, via, graph, where, hide)
        hits = [h for h in hits if h.get("predicate") not in hide]  # a fact hit on a hidden predicate would show it
        self._explain(hits, text, mode, hide, graph)
        return self._enrich(hits, detail, depth, hide, graph)

    def _explain(self, hits: list[dict[str, Any]], text: str, mode: str, hide: set[str], graph: _Graph) -> None:
        """`matched` and `snippet` on each ranked entity: why it is a result, without a `context` call."""
        entities = [h for h in hits if "predicate" not in h]
        split = {h["key"]: h.pop("_split") for h in entities if "_split" in h}
        if not text:
            return
        entities = [h for h in entities if h.get("match") != "key"]  # an id in the query says it already
        graph.prime(h["key"] for h in entities)
        words = set(tokens(text))
        for h in entities:
            facts = [(f.predicate, f.value) for f in graph.facts(h["key"]) if f.predicate not in hide]
            fields = list((h.get("attrs") or {}).items()) + facts
            found: tuple[str, Any, list[str]] | None = None
            if h.get("matched") in hide:
                h.pop("matched")  # semantic.split ranked it by a hidden fact: say nothing about it
            elif h["key"] in split:  # the split document that ranked it: that value, or that element of a list
                found = next(((p, v, []) for p, value in facts if p == h["matched"]
                              for v in ([value, *value] if isinstance(value, list) else [value]) if value_hash(v) == split[h["key"]]), None)
            elif mode == "text":
                if text.lower() in h["key"].lower():
                    h["matched"] = "key"
                else:
                    found = next(((p, v, [text.lower()]) for p, v in fields if text.lower() in str(v).lower()), None)
            else:  # ranked by meaning: the field that shares most words with the query
                shared = [(len(common), p, v, sorted(common)) for p, v in fields if (common := words & set(tokens(as_text(v))))]
                if shared:
                    _, p, v, needles = max(shared, key=lambda x: x[0])
                    found = (p, v, needles)
            if found:
                h["matched"], h["snippet"] = found[0], _snippet(found[1], found[2])

    def _enrich(self, hits: list[dict[str, Any]], detail: str, depth: int, hide: set[str], graph: _Graph) -> list[dict[str, Any]]:
        """Facts and neighbours on each entity result, read in batches (a few statements per page)."""
        if detail == "entity" and not depth:
            return hits
        entities = [h for h in hits if "predicate" not in h]
        graph.prime(h["key"] for h in entities)

        def facts(key: str) -> Any:
            fs = _ser([f for f in graph.facts(key) if f.predicate not in hide])
            return fs if detail == "full" else _compact_facts(fs)

        if depth:
            graph.prime_relations(h["key"] for h in entities)
            near = {h["key"]: self._dedupe([r for r in graph.relations(h["key"]) if self._visible(r, h["key"])], h["key"]) for h in entities}
            if detail != "entity":
                graph.prime(r.dst if r.src == k else r.src for k, rels in near.items() for r in rels[:NEIGHBOR_CAP])
        for h in entities:
            if detail != "entity":
                h["facts"] = facts(h["key"])
            if depth:
                rels = near[h["key"]]
                h["neighbors"] = []
                for r in rels[:NEIGHBOR_CAP]:
                    other = r.dst if r.src == h["key"] else r.src
                    n = {"relation": r.rel, "direction": _direction(r.rel, r.src, h["key"], self.symmetric), "subject": other}
                    if detail != "entity":
                        n["facts"] = facts(other)
                    h["neighbors"].append(n)
                if len(rels) > NEIGHBOR_CAP:
                    h["neighbors_truncated"] = True
        return hits

    def _search(
        self,
        text: str = "",
        kind: str | list[str] | None = None,
        rule: str | None = None,
        limit: int | None = None,
        after: str | None = None,
        mode: str = "text",
        min_score: float | None = None,
        related_to: str | None = None,
        via: str | None = None,
        graph: _Graph | None = None,
        where: dict[str, Any] | None = None,
        hide: set[str] | None = None,
        vector: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """The search itself, before `search` adds facts and neighbours. `graph` reads at the instant asked."""
        graph = graph or self._graph(utcnow())
        hide = hide or set()
        if mode not in ("text", "semantic"):
            raise ValueError("mode is 'text' or 'semantic'")
        if via and not related_to:
            raise ValueError("`via` needs `related_to`")
        neighbours = self._neighbours(related_to, via, graph) if related_to else None
        if after is not None and (mode == "semantic" or (kind is None and rule is None)):
            raise ValueError("`after` pages the kind/rule listing; semantic and text search rank, page them with `limit`")
        if mode == "semantic":
            return self._semantic(text, kind, rule, limit or 10, min_score, neighbours, graph, where, vector)
        if kind is None and rule is None:
            hits = self.store.search(self.tenant, text) + [h for h in self.store.search(GLOBAL_TENANT, text) if isinstance(h, Fact) and h.scope == Scope.GLOBAL]
            hits = [h for h in hits if isinstance(h, Entity) or self._sees(h, None)]
            if self.agent:
                hits = [h for h in hits if self.agent.sees_kind(h.kind if isinstance(h, Entity) else _kind(h.subject))]
            if neighbours is not None:
                hits = [h for h in hits if (h.key if isinstance(h, Entity) else h.subject) in neighbours]
            if where:
                subject = lambda h: h.key if isinstance(h, Entity) else h.subject
                graph.prime(subject(h) for h in hits)
                ents = {subject(h): (h if isinstance(h, Entity) else self.store.entity(self.tenant, subject(h))) for h in hits}
                hits = [h for h in hits if self._where(graph, subject(h), ents[subject(h)], where)]
            return _ser(hits[:limit] if limit else hits)
        spec = None
        if rule is not None:
            spec = self.cartridge.rules.get(rule)
            if spec is None or not spec.get("when"):
                raise ValueError(f"rule '{rule}' does not exist or has no `when` to evaluate")
            kind = kind or spec.get("applies_to")
            if kind is None:
                raise ValueError(f"rule '{rule}' applies to every kind: pass `kind` too")
        kinds = _kind_list(kind) or []
        for k in kinds:
            if self.agent and not self.agent.sees_kind(k):
                raise PermissionError(f"agent '{self.agent.id}' may not read kind '{k}'")
        found: list[Entity] = []
        if after is not None and ":" not in after:
            if len(kinds) > 1:
                raise ValueError("with several kinds, `after` is the last key received, kind included")
            after = f"{kinds[0]}:{after}"  # an id alone: the listing has one kind, so the key is unambiguous
        elif after is not None and _kind(after) not in kinds:
            raise ValueError(f"`after` is a key of kind {' or '.join(map(repr, kinds))} (the last key received), got '{after}'")
        if neighbours is not None:  # walk the neighbours, not the whole kind
            entities = sorted((e for e in self._entities(sorted(k for k in neighbours if _kind(k) in kinds)).values() if e), key=lambda e: e.key)
        elif len(kinds) == 1:
            entities = self.store.entities(self.tenant, kinds[0])
        else:
            entities = sorted((e for k in kinds for e in self.store.entities(self.tenant, k)), key=lambda e: e.key)
        candidates = [e for e in entities if after is None or e.key > after]
        t = text.lower()
        # ponytail: text or where over a kind reads every candidate's facts, a chunk at a time; an index on
        # fact values (or the semantic index) if a kind grows past what a sweep reads in time
        for i in range(0, len(candidates), SWEEP_CHUNK):
            chunk = candidates[i : i + SWEEP_CHUNK]
            if spec or text or where:
                graph.prime(e.key for e in chunk)  # one read per chunk, not per entity
                if vias := _vias(spec):  # the chunk's relations, then the facts at their other ends, in a few reads
                    graph.prime_relations(e.key for e in chunk)
                    graph.prime(r.dst if r.src == e.key else r.src for e in chunk for r in graph.relations(e.key) if r.rel in vias)
            for e in chunk:
                if text and not search_in([e], [], text) and not any(f.predicate not in hide and t in str(f.value).lower() for f in graph.facts(e.key)):
                    continue
                if where and not self._where(graph, e.key, e, where):
                    continue
                if spec and evaluate(spec, e.key, graph.as_of, graph) is not True:
                    continue
                found.append(e)
                if limit and len(found) >= limit:
                    return _ser(found)
        return _ser(found)

    def _where(self, graph: _Graph, key: str, ent: Entity | None, where: dict[str, Any]) -> bool:
        """Every `where` entry holds as an attribute or as a current fact of the subject."""
        attrs = ent.attrs if ent else {}
        return all((name in attrs and _holds(attrs[name], want)) or any(f.predicate == name and _holds(f.value, want) for f in graph.facts(key))
                   for name, want in where.items())

    def _neighbours(self, subject: str, via: str | None, graph: _Graph) -> set[str]:
        """The subjects linked to `subject` at the graph's instant (through `via` when given), as the caller may see them."""
        self._readable(subject)
        if via is not None and via not in self.cartridge.relations:
            raise ValueError(f"relation '{via}' is not declared")
        return {r.dst if r.src == subject else r.src for r in graph.relations(subject, via) if self._visible(r, subject)}

    def _exact_keys(self, text: str, kinds: list[str] | None) -> list[str]:
        """Subjects whose key or id appears verbatim in the query: ids are what dense vectors blur."""
        found: list[str] = []
        for tok in re.findall(r"[\w:.-]+", text):
            tok = tok.strip(".:-")
            if not any(ch.isdigit() for ch in tok):
                continue
            candidates = [tok] if ":" in tok else [f"{k}:{v}" for k in (kinds or self.cartridge.entities) for v in dict.fromkeys((tok, tok.upper()))]
            for key in candidates:
                if key not in found and self.store.entity(self.tenant, key) is not None:
                    found.append(key)
        return found

    def _semantic(
        self, text: str, kind: str | None, rule: str | None, limit: int, min_score: float | None = None, neighbours: set[str] | None = None,
        graph: _Graph | None = None, where: dict[str, Any] | None = None, vector: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        if self.embedder is None or self.index is None:
            raise ValueError("semantic search is not configured: pass an embedder and a vector index (keel serve --embedder --index)")
        if not text:
            raise ValueError("semantic search needs text")
        policy = self.cartridge.semantic
        spec = self.cartridge.rules.get(rule) if rule else None
        if rule and (spec is None or not spec.get("when")):
            raise ValueError(f"rule '{rule}' does not exist or has no `when` to evaluate")
        asked = _kind_list(kind) or _kind_list((spec or {}).get("applies_to"))
        kinds = asked or (sorted(self.agent.kinds) if self.agent and self.agent.kinds is not None else None)
        if neighbours is not None and not neighbours:
            return []  # related_to a subject with no neighbours: nothing to rank
        scope: dict[str, Any] = {"kind": kinds} if kinds else {}  # the index's own filter
        if neighbours is not None:
            scope["subject"] = sorted(neighbours)  # related_to inside the index, when it can; admit() checks it either way
        floor = min_score if min_score is not None else policy.get("min_score")
        w_lex = float(policy.get("lexical_weight", 0.3))
        lexical = getattr(self.index, "text_query", None) if w_lex > 0 else None
        graph = graph or self._graph(utcnow())

        def admit(key: str) -> Entity | None:
            kk = _kind(key)
            if (asked and kk not in asked) or (self.agent and not self.agent.sees_kind(kk)) or (neighbours is not None and key not in neighbours):
                return None
            ent = self.store.entity(self.tenant, key)
            if ent is None or (spec and evaluate(spec, key, graph.as_of, graph) is not True):
                return None  # gone since indexing, or the rule does not fire
            if where and not self._where(graph, key, ent, where):
                return None
            return ent

        def ask(fn: Any, *args: Any) -> list[tuple[str, float]]:
            return fn(self.tenant, *args, where=scope) if scope and accepts(fn, "where") else fn(self.tenant, *args)

        out: list[dict[str, Any]] = []
        for key in self._exact_keys(text, kinds):
            if (ent := admit(key)) is not None:
                out.append({**_ser(ent), "score": 1.0, "similarity": None, "match": "key"})
        taken = {o["key"] for o in out}
        pool_size = max(limit, 30) if self.reranker else limit
        vector = vector if vector is not None else embed_as(self.embedder, [text], "query")[0]
        k = max(limit * 5 if (asked or spec or self.agent or neighbours is not None) else limit, 20)
        matched: dict[str, tuple[str, str | None]] = {}
        while True:  # filters drop candidates: widen until the pool is full or both legs run out
            dense = ask(self.index.query, vector, k)
            lex = ask(lexical, text, k) if lexical else []
            similarity: dict[str, float] = {}
            for doc, sim in dense:  # a subject's best document, its own or a split one (semantic.split)
                similarity[subject_of(doc)[0]] = max(sim, similarity.get(subject_of(doc)[0], -1.0))
            pool: list[tuple[Entity, float]] = []
            pooled: set[str] = set()
            for doc, score in fuse(dense, lex, 1 - w_lex):
                key, predicate = subject_of(doc)
                if key in taken or key in pooled:
                    continue  # ranked best first: a subject keeps its best document
                if floor is not None and similarity.get(key, -1.0) < float(floor):
                    continue  # below the cosine floor (or found only lexically, so its cosine is lower still)
                if (ent := admit(key)) is not None:
                    pool.append((ent, score))
                    pooled.add(key)
                    if predicate:
                        matched[key] = (predicate, split_hash(doc))
                    if len(pool) >= pool_size:
                        break
            exhausted = len(dense) < k and (not lexical or len(lex) < k)
            if len(pool) >= pool_size or exhausted:
                break
            k *= 4
        if self.reranker and pool:
            docs = [(e.key, render(self.store, self.tenant, e.key, policy) or e.key) for e, _ in pool]
            order = {key: sc for key, sc in self.reranker.rerank(text, docs)}
            pool = sorted(pool, key=lambda es: -order.get(es[0].key, -1.0))
            ranked = [(e, order.get(e.key, 0.0)) for e, _ in pool]
        else:
            ranked = pool
        for ent, score in ranked:
            if len(out) >= limit:
                break
            hit = {**_ser(ent), "score": round(score, 6), "similarity": None if ent.key not in similarity else round(similarity[ent.key], 6)}
            if ent.key in matched:  # the split predicate whose document ranked it; `search` finds the value
                hit["matched"], hit["_split"] = matched[ent.key]
            out.append(hit)
        return out

    def annotate(
        self,
        subject: str,
        predicate: str,
        value: Any,
        scope: str = "task",
        evidence: str | None = None,
        by: str | None = None,
        task: str | None = None,
        key: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Write earned memory. The registry says who may write which scope; the scope policy says
        whether evidence is required and how long it lives. Both are enforced here. `task` ties the
        fact to one job (`history(task=)`, `keel expire --task`). `key` makes the write idempotent:
        the same key again returns the fact it wrote; the same key with other arguments is an error.
        `status="proposed"` writes a proposal: it does not touch accepted facts, waits in `context`'s
        `proposals`, and needs no write right on `scope` (only some write right), so an agent can propose
        a tenant fact a person then accepts. An accepted write (no status) or `status="rejected"`
        decides: it closes the predicate's open proposals and needs the right on `scope`."""
        if status not in (None, "proposed", "rejected"):
            raise ValueError("status is None (accepted), 'proposed' or 'rejected'")
        sc = Scope(scope)
        self._readable(subject)
        if self.agent and status == "proposed" and not self.agent.writes:
            raise PermissionError(f"agent '{self.agent.id}' may not write anything, so it may not propose")
        if self.agent and status != "proposed" and not self.agent.can_write(sc):
            raise PermissionError(f"agent '{self.agent.id}' may not write scope '{sc.value}'")
        self.cartridge.policy.check_write(sc, evidence)
        part = GLOBAL_TENANT if sc == Scope.GLOBAL else self.tenant  # every tenant reads global memory

        def replayed(f: Fact) -> dict[str, Any]:
            if (f.predicate, f.value, f.task, f.status) != (predicate, value, task, status):
                raise ValueError(f"idempotency key '{key}' was already used on {subject} with other arguments")
            return _ser(f)

        if key is not None:
            for f in self.store.facts(part, subject):
                if f.idempotency_key == key:
                    return replayed(f)
        ttl = self.cartridge.policy.ttl.get(sc)
        now = utcnow()
        author = self.author or by or "agent"
        try:
            f = self.store.open_fact(
                part,
                subject,
                predicate,
                value,
                valid_from=now,
                source=f"annotate:{author}",
                scope=sc,
                corrected_by=by or (author if self.agent_id else None),
                evidence=evidence,
                expires_at=(now + ttl) if ttl else None,
                task=task,
                idempotency_key=key,
                status=status,
                trace=self.trace,
                per_source=status is not None or predicate in self.multi,
            )
        except DuplicateKey as dup:  # a concurrent call with the same key wrote first
            return replayed(dup.fact)
        every_scope = {"scope": None} if accepts(self.store.close_fact, "scope") else {}  # a decision settles proposals in any scope
        if status != "proposed":  # a decision: the pending proposals on this predicate are settled
            self.store.close_fact(part, subject, predicate, valid_to=now, status="proposed", **every_scope)
        if status == "rejected":  # the rejection itself is recorded, closed at once: history only
            self.store.close_fact(part, subject, predicate, valid_to=now, status="rejected", **every_scope)
            f.valid_to = now
        return _ser(f)

    def related(self, subject: str, relation: str | list[str] | None = None, as_of: Instant = None, depth: int = 1) -> list[dict[str, Any]]:
        """Neighbours by relation type (one, or a list), now or at `as_of`. A symmetric relation shows once,
        direction `both`. `depth` 2 or 3 walks further through the same relations, each subject once,
        nearest first; every entry then says how many `hops` away it is and which subject it was reached
        `via` (its direction is relative to that subject)."""
        if depth not in (1, 2, 3):
            raise ValueError("related depth is 1, 2 or 3")
        self._readable(subject)
        allowed = None if relation is None else {relation} if isinstance(relation, str) else set(relation)
        graph = self._graph(_t(as_of) or utcnow())
        out: list[dict[str, Any]] = []
        seen, frontier = {subject}, [subject]
        for hop in range(1, depth + 1):
            graph.prime_relations(frontier)
            reached: list[str] = []
            for s in frontier:
                rels = self._dedupe([r for r in graph.relations(s) if (allowed is None or r.rel in allowed) and self._visible(r, s)], s)
                for r in rels:
                    other = r.dst if r.src == s else r.src
                    if hop > 1 and other in seen:
                        continue
                    entry = {"relation": r.rel, "direction": _direction(r.rel, r.src, s, self.symmetric), "subject": other, "since": r.valid_from.isoformat()}
                    if depth > 1:
                        entry.update(hops=hop, via=s)
                        seen.add(other)
                    out.append(entry)
                    reached.append(other)
            frontier = list(dict.fromkeys(reached))
        return out

    # for frameworks that want a schema list
    def spec(self) -> list[dict[str, Any]]:
        return [
            {"name": "context", "args": ["subject", "as_of?", "depth?", "as_known_at?", "task?", "detail?", "exclude?", "only?"], "doc": self.context.__doc__},
            {"name": "history", "args": ["subject?", "since?", "until?", "predicate?", "scope?", "as_known_at?", "task?", "changed_since?", "limit?"], "doc": self.history.__doc__},
            {"name": "search", "args": ["text?", "kind?", "rule?", "limit?", "after?", "mode?", "min_score?", "related_to?", "via?", "detail?", "depth?", "per_kind?", "exclude?", "as_of?", "where?"], "doc": self.search.__doc__},
            {"name": "annotate", "args": ["subject", "predicate", "value", "scope?", "evidence?", "by?", "task?", "key?", "status?"], "doc": self.annotate.__doc__},
            {"name": "related", "args": ["subject", "relation?", "as_of?", "depth?"], "doc": self.related.__doc__},
        ]
