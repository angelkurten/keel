"""The standard event envelope.

Every event, from any source, arrives in this shape. The core never reads
`payload`; it only routes on `type` and keys on `tenant` and `subject`.
A cartridge's handlers are the only code that understands the payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from keel.scopes import is_global


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Any) -> datetime:
    """ISO 8601 date or datetime to an aware UTC datetime. Accepts a trailing Z on Python 3.10 too."""
    if isinstance(value, datetime):
        d = value
    else:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class Event(BaseModel):
    """A domain fact with time, from one of the SaaS's systems."""

    id: str = Field(default="", description="The source's own id when it has one; else a content hash, so replay yields the same ids.")
    tenant: str = Field(..., description="The end-customer this fact belongs to (the tenant of the tenant).")
    type: str = Field(..., description="Dotted event type from the cartridge catalog, e.g. 'inspection.created'.")
    subject: str = Field(..., description="Entity reference '<kind>:<id>' the fact is about, e.g. 'unit:MXK-4821'.")
    occurred_at: datetime = Field(..., description="When it happened in the world (valid time).")
    recorded_at: datetime = Field(default_factory=utcnow, description="When Keel learned it (transaction time).")
    source: str = Field(..., description="System of record, e.g. 'vec.inspections' or 'webhook:telematics'.")
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _content_hash_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("id"):
            fields = ("tenant", "type", "subject", "occurred_at", "source", "payload")
            key = json.dumps([data.get(k) for k in fields], sort_keys=True, default=str)
            data = {**data, "id": sha256(key.encode()).hexdigest()[:32]}
        return data

    @field_validator("tenant")
    @classmethod
    def _not_global(cls, v: str) -> str:
        if is_global(v):
            raise ValueError(f"tenant '{v}' is reserved for global-scope memory; events never write it")
        return v

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
