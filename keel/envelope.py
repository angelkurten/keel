"""The standard event envelope.

Every event, from any source, arrives in this shape. The core never reads
`payload`; it only routes on `type` and keys on `tenant` and `subject`.
A cartridge's handlers are the only code that understands the payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Event(BaseModel):
    """A domain fact with time, from one of the SaaS's systems."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    tenant: str = Field(..., description="The end-customer this fact belongs to (the tenant of the tenant).")
    type: str = Field(..., description="Dotted event type from the cartridge catalog, e.g. 'inspection.created'.")
    subject: str = Field(..., description="Entity reference '<kind>:<id>' the fact is about, e.g. 'unit:MXK-4821'.")
    occurred_at: datetime = Field(..., description="When it happened in the world (valid time).")
    recorded_at: datetime = Field(default_factory=utcnow, description="When Keel learned it (transaction time).")
    source: str = Field(..., description="System of record, e.g. 'vec.inspections' or 'webhook:telematics'.")
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("subject")
    @classmethod
    def _subject_shape(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("subject must look like '<kind>:<id>'")
        return v

    @field_validator("occurred_at", "recorded_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @property
    def subject_kind(self) -> str:
        return self.subject.split(":", 1)[0]

    @property
    def subject_id(self) -> str:
        return self.subject.split(":", 1)[1]
