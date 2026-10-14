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

from dataclasses import dataclass
from datetime import timedelta
from enum import Enum


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

    def check_write(self, scope: Scope, evidence: str | None) -> None:
        if scope in self.requires_evidence and not evidence:
            raise PermissionError(
                f"writing to scope '{scope.value}' requires evidence (an event id, a row ref, or a human correction)"
            )
