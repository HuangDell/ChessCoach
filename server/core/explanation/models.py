"""Stable request and response models for position explanations."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Explanation(BaseModel):
    """The only model-authored shape that may be persisted."""

    model_config = ConfigDict(extra="forbid")

    critical_id: str = Field(min_length=1)
    played_move: str = Field(min_length=1)
    why_it_looked_reasonable: str = Field(min_length=1)
    core_problem: str = Field(min_length=1)
    recommended_move: str = Field(min_length=1)
    why_recommended: list[str] = Field(min_length=1, max_length=5)
    played_line_summary: str = Field(min_length=1)
    best_line_summary: str = Field(min_length=1)
    primary_category: str | None
    secondary_categories: list[str] = Field(default_factory=list)
    transferable_principle: str = Field(min_length=1)
    next_time_checklist: list[str] = Field(min_length=1, max_length=6)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)

    @field_validator(
        "why_recommended", "secondary_categories", "next_time_checklist", "evidence_refs"
    )
    @classmethod
    def _non_empty_items(cls, values: list[str]) -> list[str]:
        cleaned = [str(value).strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("List fields cannot contain empty strings.")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("List fields cannot contain duplicate values.")
        return cleaned


class ExplanationRequest(BaseModel):
    """One bounded provider request; never contains a full PGN or unrelated history."""

    model_config = ConfigDict(extra="forbid")

    critical_id: str
    language: str
    prompt_version: int
    payload: dict[str, Any]
    expected: dict[str, Any]
    allowed_evidence_refs: list[str]
    system_prompt: str
    user_prompt: str
    input_hash: str = ""


class ProviderResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
