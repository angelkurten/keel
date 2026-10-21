"""Memory scopes.

Shared memory is not a flat store. Each fact lives in a scope, from most
shared and governed to most private and short-lived. Nothing is promoted
to a wider scope by accident: promotion is an explicit `annotate` with
intent, and expiry is enforced here, not trusted to each agent.

    GLOBAL  the SaaS's own semantics and rules. Everyone reads; a privileged process writes.
    TENANT  one end-customer's fleet/clinic/portfolio. That customer's agents read; writes need evidence.
    TASK    one job in flight (audit of inspection #4821). Agents on that task read; expires when it closes.
    AGENT   one agent's working state. Never shared.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum

GLOBAL_TENANT = "__global__"  # the partition global-scope facts live in; every tenant reads it, no event writes it


def global_of(tenant: str) -> str:
    """The global partition a tenant reads. A tenant `account/t` (hosted: the server prefixes the
    account, docs/design/hosted.md) reads `account/__global__`; any other tenant reads `__global__`."""
    account, sep, _ = tenant.partition("/")
    return f"{account}/{GLOBAL_TENANT}" if sep else GLOBAL_TENANT


def is_global(tenant: str) -> bool:
    return tenant == GLOBAL_TENANT or tenant.endswith(f"/{GLOBAL_TENANT}")


class Scope(str, Enum):
    GLOBAL = "global"
    TENANT = "tenant"
    TASK = "task"
    AGENT = "agent"

    @property
    def rank(self) -> int:
        return {"global": 0, "tenant": 1, "task": 2, "agent": 3}[self.value]

    def wider_than(self, other: Scope) -> bool:
        return self.rank < other.rank


@dataclass(frozen=True)
class MemoryPolicy:
    """Per-scope lifetime and promotion rules. Comes from the cartridge."""

    ttl: dict[Scope, timedelta | None]
    requires_evidence: set[Scope]
    # what counts as evidence where it is required: "event" (an event id this tenant's log applied) or a
    # ref prefix ("photo:", "human:"). None: any non-empty string, as before
    evidence: tuple[str, ...] | None = None

    @classmethod
    def default(cls) -> MemoryPolicy:
        return cls(
            ttl={
                Scope.GLOBAL: None,
                Scope.TENANT: None,
                Scope.TASK: timedelta(days=14),
                Scope.AGENT: timedelta(days=1),
            },
            requires_evidence={Scope.GLOBAL, Scope.TENANT},
        )

    def check_write(self, scope: Scope, evidence: str | None, applied: Callable[[str], bool] = lambda _: False) -> None:
        """`applied` says whether an event id is in the writer's log."""
        if scope not in self.requires_evidence:
            return
        if not evidence:
            raise PermissionError(
                f"writing to scope '{scope.value}' requires evidence (an event id, a row ref, or a human correction)"
            )
        if self.evidence is None:
            return
        if ("event" in self.evidence and applied(evidence)) or any(evidence.startswith(p) for p in self.evidence if p != "event"):
            return
        raise PermissionError(
            f"evidence '{evidence}' for scope '{scope.value}' is not one this cartridge accepts: "
            + ", ".join("an applied event id" if p == "event" else f"a '{p}' ref" for p in self.evidence)
        )
