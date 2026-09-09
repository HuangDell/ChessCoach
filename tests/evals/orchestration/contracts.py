"""Versioned data contracts for the isolated orchestration evaluation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from server.core.agent.models import AgentToolName, ContractModel


ROOT = Path(__file__).resolve().parent


class SourceRecord(ContractModel):
    kind: Literal["synthetic", "public"]
    name: str = Field(min_length=1)
    license: str = Field(min_length=1)
    url: str | None = None


class BudgetRecord(ContractModel):
    max_turns: int = Field(gt=0)
    max_total_tool_calls: int = Field(ge=0)
    max_engine_tool_calls: int = Field(ge=0)
    timeout_seconds: int = Field(gt=0)


class CheckpointRecord(ContractModel):
    activity: str = Field(min_length=1)
    position_fixture: str | None = None
    personalization_enabled: bool
    has_engine_facts: bool
    evidence_refs: list[str] = Field(default_factory=list)
    allowed_tools: list[AgentToolName]
    missing_artifacts: list[str] = Field(default_factory=list)


class VariantRecord(ContractModel):
    variant_id: str = Field(pattern=r"^[A-Z][0-9]{2}-[a-z0-9-]+$")
    fixture_ids: list[str]
    script_id: str = Field(min_length=1)
    factor: str | None = None


class TaskRecord(ContractModel):
    task_id: str = Field(pattern=r"^[A-Z][0-9]{2}$")
    slice: Literal["N", "S", "A", "O", "R", "X"]
    title: str = Field(min_length=1)
    query: str = Field(min_length=1)
    split: Literal["dev"]
    source: SourceRecord
    source_game: str | None = None
    near_duplicate_group: str = Field(min_length=1)
    template_family: str = Field(min_length=1)
    checkpoint: CheckpointRecord
    budget: BudgetRecord
    variants: list[VariantRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def _matching_slice(self) -> "TaskRecord":
        if not self.task_id.startswith(self.slice):
            raise ValueError("task_id must match its slice")
        return self


class TaskSuite(ContractModel):
    schema_version: Literal[1]
    dataset_id: Literal["orchestration-dev-v1"]
    tasks: list[TaskRecord]


class FixtureManifest(ContractModel):
    schema_version: Literal[1]
    fixture_set_id: Literal["orchestration-fixtures-v1"]
    base_fixture_file: str
    base_fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_fixture_ids: list[str]
    custom_fixtures: dict[str, dict[str, Any]]


class GoldPredicate(ContractModel):
    type: Literal["equals", "current_fen", "legal_uci", "reference_subset", "max_count"]
    field: str = Field(min_length=1)
    value: Any | None = None


class GoldAction(ContractModel):
    kind: Literal["tool_call", "answer", "clarify", "partial", "abort"]
    name: AgentToolName | None = None
    predicates: list[GoldPredicate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tool_name_contract(self) -> "GoldAction":
        if (self.kind == "tool_call") != (self.name is not None):
            raise ValueError("only tool_call gold actions carry a tool name")
        return self


class GoldCase(ContractModel):
    variant_id: str = Field(min_length=1)
    acceptable_paths: list[list[GoldAction]] = Field(min_length=1)
    dependencies: list[tuple[AgentToolName, AgentToolName]] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    required_reference_ids: list[str] = Field(default_factory=list)
    required_response_claims: dict[str, Any] = Field(default_factory=dict)
    forbidden_response_claims: list[str] = Field(default_factory=list)
    allowed_final_actions: list[Literal["answer", "clarify", "partial", "abort"]]
    expected_protocol: Literal["complete", "clarified", "partial", "cancelled", "aborted"]


class GoldSuite(ContractModel):
    schema_version: Literal[1]
    scorer_version: Literal[1]
    dataset_id: Literal["orchestration-dev-v1"]
    cases: list[GoldCase]


class ScriptAction(ContractModel):
    kind: Literal[
        "tool_call",
        "answer",
        "clarify",
        "partial",
        "abort",
        "cancel",
        "generation_change",
        "invalidate_fixture",
    ]
    fixture_id: str | None = None
    name: AgentToolName | None = None
    arguments: dict[str, Any] | None = None
    generation: int | None = Field(default=None, ge=0)
    generation_after: int | None = Field(default=None, ge=0)
    response: dict[str, Any] = Field(default_factory=dict)


class ScriptRecord(ContractModel):
    candidate_decisions: list[list[AgentToolName]]
    allow_fallback: bool = True
    actions: list[ScriptAction]


class ScriptSuite(ContractModel):
    schema_version: Literal[1]
    scripts: dict[str, ScriptRecord]


def load_json(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def load_tasks() -> TaskSuite:
    return TaskSuite.model_validate(load_json("tasks.json"))


def load_gold() -> GoldSuite:
    return GoldSuite.model_validate(load_json("gold.json"))


def load_scripts() -> ScriptSuite:
    return ScriptSuite.model_validate(load_json("scripts.json"))


def load_fixture_manifest() -> FixtureManifest:
    return FixtureManifest.model_validate(load_json("fixtures.json"))
