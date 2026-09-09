"""Typed, parameter-matched fixture executor isolated from gold labels."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from server.core.agent.models import (
    AgentToolName,
    AnalyzeMoveInput,
    AnalyzeMoveResult,
    AnalyzePositionInput,
    AnalyzePositionResult,
    CreateTrainingDraftInput,
    GetPlayerProfileInput,
    GetPlayerProfileResult,
    GetReviewContextInput,
    GetReviewContextResult,
    GetTrainingCandidatesInput,
    GetTrainingCandidatesResult,
    LookupOpeningInput,
    LookupOpeningResult,
    ToolError,
    ToolResult,
    TrainingDraft,
)
from server.core.agent.tools import ToolExecution
from tests.evals.orchestration.contracts import ROOT, load_fixture_manifest


INPUT_TYPES: dict[AgentToolName, type[BaseModel]] = {
    "get_review_context": GetReviewContextInput,
    "analyze_position": AnalyzePositionInput,
    "analyze_move": AnalyzeMoveInput,
    "lookup_opening": LookupOpeningInput,
    "get_player_profile": GetPlayerProfileInput,
    "get_training_candidates": GetTrainingCandidatesInput,
    "create_training_draft": CreateTrainingDraftInput,
}
RESULT_TYPES: dict[AgentToolName, type[BaseModel]] = {
    "get_review_context": GetReviewContextResult,
    "analyze_position": AnalyzePositionResult,
    "analyze_move": AnalyzeMoveResult,
    "lookup_opening": LookupOpeningResult,
    "get_player_profile": GetPlayerProfileResult,
    "get_training_candidates": GetTrainingCandidatesResult,
    "create_training_draft": TrainingDraft,
}


class FixtureStorageConsistencyError(RuntimeError):
    pass


def load_fixture_catalog() -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = load_fixture_manifest()
    base_path = (ROOT / manifest.base_fixture_file).resolve()
    raw = base_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest.base_fixture_sha256:
        raise ValueError("the frozen baseline fixture hash changed")
    base = json.loads(raw)
    available = base["fixtures"]["tool_results"]
    fixtures = {name: available[name] for name in manifest.imported_fixture_ids}
    for name, fixture in manifest.custom_fixtures.items():
        if "alias_of" in fixture:
            fixture = {
                **available[fixture["alias_of"]],
                **{key: value for key, value in fixture.items() if key != "alias_of"},
            }
        fixtures[name] = fixture
    return fixtures, base["fixtures"]["positions"]


def _matches(actual: dict[str, Any], fixture: dict[str, Any]) -> bool:
    expected = fixture["request"]
    if any(actual.get(key) != value for key, value in expected.items()):
        return False
    for field, bounds in fixture.get("request_ranges", {}).items():
        value = actual.get(field)
        if not isinstance(value, int) or not bounds["min"] <= value <= bounds["max"]:
            return False
    return True


class FixtureExecutor:
    def __init__(self, fixture_ids: list[str]) -> None:
        catalog, _ = load_fixture_catalog()
        self.fixtures = {name: catalog[name] for name in fixture_ids}
        self.executions: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []
        self._successful_results: list[BaseModel] = []
        self._invalidated: set[str] = set()

    def invalidate(self, fixture_id: str) -> None:
        self._invalidated.add(fixture_id)

    def match(self, name: AgentToolName, payload: BaseModel) -> tuple[str, dict[str, Any]] | None:
        actual = payload.model_dump(mode="json", exclude_none=True)
        for fixture_id, fixture in self.fixtures.items():
            if fixture_id in self._invalidated or fixture["tool"] != name:
                continue
            if _matches(actual, fixture):
                return fixture_id, fixture
        return None

    def estimated_engine_calls(self, name: AgentToolName, payload: BaseModel) -> int:
        matched = self.match(name, payload)
        if matched is None:
            return 0
        fixture = matched[1]
        return int(fixture.get("engine_calls", int(bool(fixture.get("uses_engine")))))

    def successful_tool_results(self) -> list[BaseModel]:
        return list(self._successful_results)

    async def execute(self, name: AgentToolName, payload: BaseModel) -> ToolExecution[Any]:
        expected_type = INPUT_TYPES[name]
        if not isinstance(payload, expected_type):
            raise ValueError(f"{name} received the wrong input DTO")
        matched = self.match(name, payload)
        actual = payload.model_dump(mode="json", exclude_none=True)
        self.attempts.append({"name": name, "arguments": actual, "matched": bool(matched)})
        if matched is None:
            error = ToolError(
                code=(
                    "profile_unavailable"
                    if name == "get_player_profile"
                    else "training_unavailable"
                    if name in {"get_training_candidates", "create_training_draft"}
                    else "position_not_found"
                ),
                message="The fixture request did not match an available result.",
                recoverable=False,
            )
            return ToolExecution(name, ToolResult[Any](ok=False, error=error), False, 0)
        fixture_id, fixture = matched
        trigger = fixture.get("trigger")
        if trigger == "cancel":
            raise asyncio.CancelledError
        if trigger == "storage_consistency":
            raise FixtureStorageConsistencyError("fixture storage index disagrees with its artifact")
        result_type = RESULT_TYPES[name]
        result = ToolResult[result_type].model_validate(fixture["result"])
        cache_hit = bool(fixture.get("cache_hit", False))
        engine_calls = int(fixture.get("engine_calls", int(bool(fixture.get("uses_engine")))))
        self.executions.append(
            {
                "fixture_id": fixture_id,
                "name": name,
                "arguments": actual,
                "result": result.model_dump(mode="json", exclude_none=True),
                "cache_hit": cache_hit,
                "engine_calls": engine_calls,
            }
        )
        if result.ok and result.data is not None:
            self._successful_results.append(result.data)
        return ToolExecution(name, result, cache_hit, engine_calls)
