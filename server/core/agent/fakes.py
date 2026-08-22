"""Deterministic Agent and tool fakes for backend contract tests."""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from server.core.agent.models import (
    AgentRunRequest,
    AgentRunResult,
    AgentToolName,
    AGENT_TOOL_PERMISSIONS,
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


class FakeAgentRuntime:
    """Queue-backed runtime that records validated requests in call order."""

    def __init__(self, results: Iterable[AgentRunResult | Exception] = ()) -> None:
        self.requests: list[AgentRunRequest] = []
        self._results = deque(results)

    def queue(self, result: AgentRunResult | Exception) -> None:
        self._results.append(result)

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("FakeAgentRuntime has no queued result")
        result = self._results.popleft()
        if isinstance(result, Exception):
            raise result
        return AgentRunResult.model_validate(result.model_dump(mode="python"))


@dataclass(frozen=True)
class FakeToolCall:
    name: AgentToolName
    request: Any


ResultT = TypeVar("ResultT", bound=BaseModel)


class FakeAgentTools:
    """Typed domain-tool fake with independent FIFO responses per tool."""

    ENGINE_TOOLS = frozenset({"analyze_position", "analyze_move"})

    def __init__(
        self,
        *,
        max_total_calls: int | None = None,
        max_engine_calls: int | None = None,
    ) -> None:
        self.calls: list[FakeToolCall] = []
        self._results: dict[str, deque[ToolResult[Any] | Exception]] = defaultdict(deque)
        self.max_total_calls = max_total_calls
        self.max_engine_calls = max_engine_calls

    @property
    def total_calls(self) -> int:
        return len(self.calls)

    @property
    def engine_calls(self) -> int:
        return sum(call.name in self.ENGINE_TOOLS for call in self.calls)

    def queue(self, name: AgentToolName, result: ToolResult[Any] | Exception) -> None:
        if name not in AGENT_TOOL_PERMISSIONS:
            raise ValueError(f"Unknown Agent tool: {name}")
        self._results[name].append(result)

    def _budget_error(self, name: AgentToolName) -> ToolResult[Any] | None:
        total_exceeded = (
            self.max_total_calls is not None and self.total_calls > self.max_total_calls
        )
        engine_exceeded = (
            name in self.ENGINE_TOOLS
            and self.max_engine_calls is not None
            and self.engine_calls > self.max_engine_calls
        )
        if not (total_exceeded or engine_exceeded):
            return None
        detail = "Engine tool call" if engine_exceeded else "Total tool call"
        return ToolResult[Any](
            ok=False,
            error=ToolError(
                code="tool_budget_exceeded",
                message=f"{detail} budget exceeded for this Agent run.",
                recoverable=True,
            ),
        )

    @staticmethod
    def _validate_correspondence(
        name: AgentToolName,
        request: BaseModel,
        data: BaseModel,
    ) -> None:
        if name == "get_review_context":
            typed_request = cast(GetReviewContextInput, request)
            typed_data = cast(GetReviewContextResult, data)
            reference = typed_data.reference
            if (
                reference.game_id != typed_request.game_id
                or reference.review_side != typed_request.review_side
                or reference.critical_id != typed_request.critical_id
            ):
                raise AssertionError("get_review_context result does not match its request")
        elif name == "analyze_position":
            typed_request = cast(AnalyzePositionInput, request)
            typed_data = cast(AnalyzePositionResult, data)
            if typed_data.fen != typed_request.fen:
                raise AssertionError("analyze_position result FEN does not match its request")
        elif name == "analyze_move":
            typed_request = cast(AnalyzeMoveInput, request)
            typed_data = cast(AnalyzeMoveResult, data)
            if (
                typed_data.fen_before != typed_request.fen_before
                or typed_data.move.uci != typed_request.move_uci
            ):
                raise AssertionError("analyze_move result does not match its request")
        elif name == "create_training_draft":
            typed_request = cast(CreateTrainingDraftInput, request)
            typed_data = cast(TrainingDraft, data)
            expected = TrainingDraft.model_validate(typed_request.model_dump(mode="python"))
            if typed_data != expected:
                raise AssertionError("create_training_draft result does not match its request")

    async def _call(
        self,
        name: AgentToolName,
        request: BaseModel,
        result_model: type[ResultT],
    ) -> ToolResult[ResultT]:
        self.calls.append(FakeToolCall(name=name, request=request))
        budget_error = self._budget_error(name)
        result_type = ToolResult[result_model]  # type: ignore[valid-type]
        if budget_error is not None:
            return cast(
                ToolResult[ResultT],
                result_type.model_validate(budget_error.model_dump(mode="python")),
            )
        if not self._results[name]:
            raise AssertionError(f"FakeAgentTools has no queued result for {name}")
        result = self._results[name].popleft()
        if isinstance(result, Exception):
            raise result
        validated = result_type.model_validate(result.model_dump(mode="python"))
        if validated.ok:
            self._validate_correspondence(name, request, validated.data)
        return cast(ToolResult[ResultT], validated)

    async def get_review_context(
        self, request: GetReviewContextInput
    ) -> ToolResult[GetReviewContextResult]:
        return await self._call("get_review_context", request, GetReviewContextResult)

    async def analyze_position(
        self, request: AnalyzePositionInput
    ) -> ToolResult[AnalyzePositionResult]:
        return await self._call("analyze_position", request, AnalyzePositionResult)

    async def analyze_move(
        self, request: AnalyzeMoveInput
    ) -> ToolResult[AnalyzeMoveResult]:
        return await self._call("analyze_move", request, AnalyzeMoveResult)

    async def lookup_opening(
        self, request: LookupOpeningInput
    ) -> ToolResult[LookupOpeningResult]:
        return await self._call("lookup_opening", request, LookupOpeningResult)

    async def get_player_profile(
        self, request: GetPlayerProfileInput
    ) -> ToolResult[GetPlayerProfileResult]:
        return await self._call("get_player_profile", request, GetPlayerProfileResult)

    async def get_training_candidates(
        self, request: GetTrainingCandidatesInput
    ) -> ToolResult[GetTrainingCandidatesResult]:
        return await self._call(
            "get_training_candidates", request, GetTrainingCandidatesResult
        )

    async def create_training_draft(
        self, request: CreateTrainingDraftInput
    ) -> ToolResult[TrainingDraft]:
        return await self._call("create_training_draft", request, TrainingDraft)
