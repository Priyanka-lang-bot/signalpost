"""Typed, validated data structures for facts, profiles and agent output."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class FactStatus(str, Enum):
    VERIFIED = "verified"  # value read from an authoritative record whose identity was checked
    UNVERIFIED = "unverified"  # evidence check failed; never published as fact
    CONFLICT = "conflict"  # sources disagree; both values kept, none asserted


class Fact(BaseModel):
    field: str
    value: Any
    source: str  # stable source id, e.g. "brreg:enhetsregisteret"
    source_url: str  # canonical URL of the record that states this value
    retrieved_via: str  # URL actually fetched (may be a search/batch URL)
    json_path: list[str | int]  # where the value sits inside the fetched payload
    retrieved_at: str
    source_modified_at: str | None = None
    verification: str = "authoritative_registry_field"
    status: FactStatus = FactStatus.VERIFIED

    @field_validator("source_url", "retrieved_via")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("source URLs must be absolute https URLs")
        return v


class RelatedEntity(BaseModel):
    """Another legal entity referenced by this record. Never merged into this profile."""

    relation: Literal["parent_unit", "role_holder_entity", "auditor", "accountant", "other"]
    orgnr: str
    name: str | None = None
    source_url: str


class CompanyProfile(BaseModel):
    orgnr: str
    entity_kind: Literal["enhet", "underenhet"]
    registry_state: Literal["registered", "deleted"]
    facts: list[Fact] = Field(default_factory=list)
    related_entities: list[RelatedEntity] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)

    def fact(self, name: str) -> Fact | None:
        return next((f for f in self.facts if f.field == name), None)

    def value(self, name: str) -> Any:
        f = self.fact(name)
        return f.value if f else None


class Change(BaseModel):
    field: str
    old_value: Any
    new_value: Any
    kind: Literal["added", "changed", "removed"]
    source_url: str
    retrieved_at: str
    source_modified_at: str | None = None


Outcome = Literal[
    "ok",
    "invalid_input",
    "not_found",
    "removed_from_registry",
    "identity_mismatch",
    "source_unavailable",
    "budget_exceeded",
]


class AgentResult(BaseModel):
    input: str
    orgnr: str | None = None
    outcome: Outcome
    profile: CompanyProfile | None = None
    changes: list[Change] = Field(default_factory=list)
    explanation: str = ""
    explanation_method: str = "deterministic_template"  # no LLM; text is built only from verified facts
    warnings: list[str] = Field(default_factory=list)
    checked_at: str = ""
    run: dict[str, Any] = Field(default_factory=dict)
