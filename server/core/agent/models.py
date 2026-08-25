"""Framework-neutral contracts shared by the Chess Coach Agent boundaries.

These models deliberately contain no OpenAI Agents SDK types.  They are the
validated boundary between the runtime, domain tools, storage, and web layer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Generic, Literal, Mapping, TypeVar
from uuid import uuid4

import chess
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ReviewSide = Literal["white", "black"]
ToolPermission = Literal["read", "compute"]
AgentToolName = Literal[
    "get_review_context",
    "analyze_position",
    "analyze_move",
    "lookup_opening",
    "get_player_profile",
    "get_training_candidates",
    "create_training_draft",
]
ToolErrorCode = Literal[
    "invalid_fen",
    "illegal_move",
    "engine_timeout",
    "engine_unavailable",
    "game_not_found",
    "position_not_found",
    "profile_unavailable",
    "training_unavailable",
    "tool_budget_exceeded",
]
AGENT_TOOL_PERMISSIONS: Mapping[AgentToolName, ToolPermission] = MappingProxyType(
    {
        "get_review_context": "read",
        "analyze_position": "compute",
        "analyze_move": "compute",
        "lookup_opening": "read",
        "get_player_profile": "read",
        "get_training_candidates": "read",
        "create_training_draft": "compute",
    }
)
AgentActivity = Literal[
    "conversation",
    "game_review",
    "position_analysis",
    "opening_learning",
    "retry",
    "training",
    "training_planning",
]
LearningEvidenceType = Literal[
    "fact_motif",
    "fact_composite",
    "attempt_outcome",
    "puzzle_theme",
    "clock_outcome",
]


class ContractModel(BaseModel):
    """Base policy for persisted and externally exchanged Agent contracts."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _validate_fen(value: str) -> str:
    value = value.strip()
    try:
        board = chess.Board(value)
    except ValueError as exc:
        raise ValueError("fen must describe a valid chess position") from exc
    if not board.is_valid():
        raise ValueError("fen must describe a valid chess position")
    return value


def _validate_uci(value: str) -> str:
    value = value.strip().lower()
    try:
        move = chess.Move.from_uci(value)
    except ValueError as exc:
        raise ValueError("move must use UCI notation") from exc
    if not move or move.from_square == move.to_square:
        raise ValueError("move must use non-null UCI notation")
    return value


def _clean_unique_strings(values: list[str]) -> list[str]:
    cleaned = [value.strip() for value in values]
    if any(not value for value in cleaned):
        raise ValueError("list items must not be empty")
    if len(cleaned) != len(set(cleaned)):
        raise ValueError("list items must not contain duplicates")
    return cleaned


def _non_empty_optional(value: str | None, *, label: str = "identifier") -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _validate_utc_datetime(value: str) -> str:
    """Validate and canonicalize an ISO-8601 instant in UTC."""

    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be an ISO-8601 UTC datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must use UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def _legal_move(board: chess.Board, move: "MoveReference", *, label: str) -> chess.Move:
    parsed = chess.Move.from_uci(move.uci)
    if parsed not in board.legal_moves:
        raise ValueError(f"{label} must be legal in the supplied position")
    if board.san(parsed) != move.san:
        raise ValueError(f"{label} SAN must match its UCI move in the supplied position")
    return parsed


def _replay_line(
    fen: str,
    line_uci: list[str],
    line_san: list[str],
    *,
    label: str,
) -> chess.Board:
    board = chess.Board(fen)
    for index, (uci, san) in enumerate(zip(line_uci, line_san, strict=True), start=1):
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves:
            raise ValueError(f"{label} contains an illegal move at ply {index}")
        if board.san(move) != san:
            raise ValueError(f"{label} contains mismatched SAN at ply {index}")
        board.push(move)
    return board


def _validate_candidate_lines(fen: str, candidates: list["CandidateLine"]) -> None:
    for candidate in candidates:
        _replay_line(
            fen,
            candidate.line_uci,
            candidate.line_san,
            label=f"candidate rank {candidate.rank}",
        )


class PositionReference(ContractModel):
    """Stable pointer to an artifact position or a standalone FEN."""

    game_id: str | None = None
    review_side: ReviewSide | None = None
    critical_id: str | None = None
    ply: int | None = Field(default=None, ge=0)
    fen: str | None = None

    @field_validator("game_id", "critical_id")
    @classmethod
    def _non_empty_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _non_empty_optional(value, label="reference identifier")

    @field_validator("fen")
    @classmethod
    def _valid_optional_fen(cls, value: str | None) -> str | None:
        return None if value is None else _validate_fen(value)

    @model_validator(mode="after")
    def _has_position_identity(self) -> "PositionReference":
        if self.game_id is None and self.fen is None:
            raise ValueError("position reference requires game_id or fen")
        if self.review_side is not None and self.game_id is None:
            raise ValueError("review_side requires game_id")
        if self.critical_id is not None and self.game_id is None:
            raise ValueError("critical_id requires game_id")
        return self


class MoveReference(ContractModel):
    uci: str
    san: str = Field(min_length=1)

    _valid_uci = field_validator("uci")(_validate_uci)

    @field_validator("san")
    @classmethod
    def _valid_san(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("san must not be empty")
        return value


class EngineScore(ContractModel):
    kind: Literal["cp", "mate"]
    value: int
    pov: Literal["white", "black", "side_to_move", "review_side"]


class EngineProvenance(ContractModel):
    engine_name: str = Field(min_length=1)
    engine_version: str = Field(min_length=1)
    depth: int = Field(gt=0)
    multipv: int = Field(gt=0)
    analysis_profile_id: str = Field(min_length=1)
    cache_key: str | None = None


class CandidateLine(ContractModel):
    rank: int = Field(ge=1)
    move: MoveReference
    score: EngineScore
    win_percent_for_review_side: float | None = Field(ge=0, le=100)
    line_uci: list[str] = Field(min_length=1)
    line_san: list[str] = Field(min_length=1)

    @field_validator("line_uci")
    @classmethod
    def _valid_line_uci(cls, values: list[str]) -> list[str]:
        return [_validate_uci(value) for value in values]

    @field_validator("line_san")
    @classmethod
    def _valid_line_san(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("SAN line items must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _matching_move_lines(self) -> "CandidateLine":
        if len(self.line_uci) != len(self.line_san):
            raise ValueError("line_uci and line_san must contain the same number of plies")
        if self.line_uci[0] != self.move.uci or self.line_san[0] != self.move.san:
            raise ValueError("candidate move must match the first UCI and SAN line entries")
        return self


class ChessReference(ContractModel):
    kind: Literal["position", "critical_position", "game", "puzzle", "skill"]
    game_id: str | None = None
    review_side: ReviewSide | None = None
    critical_id: str | None = None
    puzzle_id: str | None = None
    ply: int | None = Field(default=None, ge=0)
    fen: str | None = None
    skill_id: str | None = None

    @field_validator("game_id", "critical_id", "puzzle_id", "skill_id")
    @classmethod
    def _clean_reference_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("reference values must not be empty")
        return value

    @field_validator("fen")
    @classmethod
    def _valid_fen(cls, value: str | None) -> str | None:
        return None if value is None else _validate_fen(value)

    @model_validator(mode="after")
    def _matches_kind(self) -> "ChessReference":
        required = {
            "position": self.fen is not None or self.game_id is not None,
            "critical_position": (
                self.game_id is not None
                and self.review_side is not None
                and self.critical_id is not None
            ),
            "game": self.game_id is not None,
            "puzzle": self.puzzle_id is not None,
            "skill": self.skill_id is not None,
        }
        if not required[self.kind]:
            raise ValueError(f"{self.kind} reference is missing its identifying field")
        if self.review_side is not None and self.game_id is None:
            raise ValueError("review_side requires game_id")
        if self.critical_id is not None and self.game_id is None:
            raise ValueError("critical_id requires game_id")
        if self.puzzle_id is not None and self.kind != "puzzle":
            raise ValueError("puzzle_id is only valid for puzzle references")
        if self.skill_id is not None and self.kind != "skill":
            raise ValueError("skill_id is only valid for skill references")
        if self.kind == "puzzle" and self.game_id is not None:
            if self.review_side is None or self.critical_id is None:
                raise ValueError("game-backed puzzle references require review_side and critical_id")
        return self


class LearningMemoryItem(ContractModel):
    skill_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    status: Literal["unknown", "watch", "weakness", "strength"]
    confidence_level: Literal["insufficient", "emerging", "established"]
    window: Literal["recent", "lifetime"]
    evidence_count: int = Field(ge=1)
    window_games: int = Field(ge=0)
    examples: list[ChessReference] = Field(min_length=1, max_length=3)
    evidence_refs: list[str] = Field(min_length=1)

    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)


class MemoryQuery(ContractModel):
    """Trusted Core inputs for bounded deterministic learning-memory retrieval."""

    activity: AgentActivity
    current_facts: dict[str, Any] = Field(default_factory=dict)
    focus_skill_id: str | None = None
    focus_category: str | None = None
    window: Literal["recent", "lifetime"] = "recent"
    limit: int = Field(default=3, ge=1, le=5)

    @field_validator("focus_skill_id", "focus_category")
    @classmethod
    def _valid_optional_focus(cls, value: str | None) -> str | None:
        return _non_empty_optional(value, label="memory focus")


class PositionContext(ContractModel):
    fen: str
    recent_moves_uci: list[str] = Field(max_length=8)
    recent_moves_san: list[str] = Field(max_length=8)
    exploration_moves_uci: list[str] = Field(default_factory=list, max_length=16)
    exploration_moves_san: list[str] = Field(default_factory=list, max_length=16)
    selected_move_uci: str | None = None
    selected_move_san: str | None = None
    reference: PositionReference | None = None

    _valid_fen = field_validator("fen")(_validate_fen)

    @field_validator("recent_moves_uci")
    @classmethod
    def _valid_recent_uci(cls, values: list[str]) -> list[str]:
        return [_validate_uci(value) for value in values]

    @field_validator("recent_moves_san")
    @classmethod
    def _valid_recent_san(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("recent SAN moves must not be empty")
        return cleaned

    @field_validator("exploration_moves_uci")
    @classmethod
    def _valid_exploration_uci(cls, values: list[str]) -> list[str]:
        return [_validate_uci(value) for value in values]

    @field_validator("exploration_moves_san")
    @classmethod
    def _valid_exploration_san(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("exploration SAN moves must not be empty")
        return cleaned

    @field_validator("selected_move_uci")
    @classmethod
    def _valid_selected_uci(cls, value: str | None) -> str | None:
        return None if value is None else _validate_uci(value)

    @field_validator("selected_move_san")
    @classmethod
    def _valid_selected_san(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("selected_move_san must not be empty")
        return value

    @model_validator(mode="after")
    def _paired_move_notation(self) -> "PositionContext":
        if len(self.recent_moves_uci) != len(self.recent_moves_san):
            raise ValueError("recent UCI and SAN histories must contain the same number of plies")
        if len(self.exploration_moves_uci) != len(self.exploration_moves_san):
            raise ValueError("exploration requires paired UCI and SAN notation")
        if (self.selected_move_uci is None) != (self.selected_move_san is None):
            raise ValueError("selected move requires both UCI and SAN notation")
        if self.exploration_moves_uci:
            if self.reference is None or self.reference.fen is None:
                raise ValueError("exploration requires a canonical base position reference")
            reached = _replay_line(
                self.reference.fen,
                self.exploration_moves_uci,
                self.exploration_moves_san,
                label="exploration",
            )
            if reached.fen() != self.fen:
                raise ValueError("exploration moves must reach the supplied FEN")
        elif self.reference is not None and self.reference.fen not in (None, self.fen):
            raise ValueError("position context FEN must match its reference FEN")
        return self


class TaskContext(ContractModel):
    activity: AgentActivity = "conversation"
    user_goal: str | None = None
    review_side: ReviewSide | None = None
    personalization_enabled: bool = False


class GameContext(ContractModel):
    game_id: str = Field(min_length=1)
    review_side: ReviewSide | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    result: str | None = None


class ReviewContext(ContractModel):
    game_id: str = Field(min_length=1)
    review_side: ReviewSide
    critical_ids: list[str] = Field(default_factory=list)
    active_critical_id: str | None = None

    @field_validator("active_critical_id")
    @classmethod
    def _non_empty_active_critical_id(cls, value: str | None) -> str | None:
        return _non_empty_optional(value, label="active_critical_id")


class EngineFactsContext(ContractModel):
    reference: PositionReference
    played_move: MoveReference | None = None
    best_move: MoveReference | None = None
    classification: str | None = None
    criticality: str | None = None
    candidates: list[CandidateLine] = Field(default_factory=list, max_length=3)
    provenance: EngineProvenance | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)

    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)


class RelevantProfileContext(ContractModel):
    analyzed_games: int = Field(ge=0)
    weaknesses: list["SkillEstimate"] = Field(default_factory=list, max_length=3)
    strengths: list["SkillEstimate"] = Field(default_factory=list, max_length=3)
    training_success_rate: float | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _evidence_matches_profile_bucket(self) -> "RelevantProfileContext":
        if any(estimate.status != "weakness" for estimate in self.weaknesses):
            raise ValueError("weaknesses must contain only weakness estimates")
        if any(estimate.status != "strength" for estimate in self.strengths):
            raise ValueError("strengths must contain only strength estimates")
        if any(
            estimate.evidence_count < 1 or not estimate.examples
            for estimate in [*self.weaknesses, *self.strengths]
        ):
            raise ValueError("profile estimates require evidence and example references")
        return self


class AgentSessionState(ContractModel):
    schema_version: int = Field(default=1, ge=1)
    session_id: str = Field(min_length=1)
    active_game_id: str | None = None
    review_side: ReviewSide | None = None
    active_ply: int | None = Field(default=None, ge=0)
    active_critical_id: str | None = None
    activity: AgentActivity = "conversation"
    focus_ref: str | None = None
    position: PositionContext | None = None
    discussed_positions: list[PositionReference] = Field(default_factory=list)
    conversation_summary: str = ""
    conversation_summary_references: list[PositionReference] = Field(
        default_factory=list,
        max_length=5,
    )
    generation: int = Field(default=0, ge=0)
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)

    @field_validator("active_game_id", "active_critical_id", "focus_ref")
    @classmethod
    def _non_empty_optional_ids(cls, value: str | None) -> str | None:
        return _non_empty_optional(value, label="session identifier")

    @model_validator(mode="after")
    def _consistent_active_reference(self) -> "AgentSessionState":
        if self.review_side is not None and self.active_game_id is None:
            raise ValueError("review_side requires active_game_id")
        if self.active_critical_id is not None and self.active_game_id is None:
            raise ValueError("active_critical_id requires active_game_id")
        if self.active_ply is not None and self.active_game_id is None:
            raise ValueError("active_ply requires active_game_id")
        return self


class AgentSessionCreateRequest(ContractModel):
    game_id: str | None = None
    review_side: ReviewSide | None = None
    active_ply: int | None = Field(default=None, ge=0)
    active_critical_id: str | None = None

    @field_validator("game_id", "active_critical_id")
    @classmethod
    def _non_empty_optional_ids(cls, value: str | None) -> str | None:
        return _non_empty_optional(value, label="session identifier")

    @model_validator(mode="after")
    def _consistent_game_context(self) -> "AgentSessionCreateRequest":
        if self.review_side is not None and self.game_id is None:
            raise ValueError("review_side requires game_id")
        if self.active_critical_id is not None and self.game_id is None:
            raise ValueError("active_critical_id requires game_id")
        if self.active_ply is not None and self.game_id is None:
            raise ValueError("active_ply requires game_id")
        return self


class AgentSessionContextRequest(ContractModel):
    expected_generation: int = Field(ge=0)
    game_id: str | None = None
    review_side: ReviewSide | None = None
    active_ply: int | None = Field(default=None, ge=0)
    active_critical_id: str | None = None
    activity: AgentActivity | None = None
    focus_ref: str | None = None
    position: PositionContext | None = None

    @field_validator("game_id", "active_critical_id", "focus_ref")
    @classmethod
    def _non_empty_optional_ids(cls, value: str | None) -> str | None:
        # A context request is a patch. Cross-field ownership is validated only
        # after applying it to the persisted AgentSessionState.
        return _non_empty_optional(value, label="session patch identifier")


class AgentMessageRequest(ContractModel):
    message: str = Field(min_length=1)
    expected_generation: int = Field(ge=0)
    position_reference: PositionReference | None = None


class AgentSessionSummary(ContractModel):
    session_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    conversation_summary: str = ""


class ResolvedChessContext(ContractModel):
    session: AgentSessionState
    position: PositionContext | None
    game: GameContext | None
    review: ReviewContext | None
    profile: dict[str, Any] | None
    training: dict[str, Any] | None


class ReviewPriorityCandidate(ContractModel):
    """Deterministic, comparable features for one bounded review focus candidate."""

    reference: PositionReference
    classification: str = Field(min_length=1)
    category: str | None = None
    severity: float = Field(ge=0)
    criticality: float = Field(ge=0)
    fact_confidence: float = Field(ge=0, le=1)
    recurrence_evidence: int = Field(default=0, ge=0)
    training_available: bool = False
    user_goal_relevance: float = Field(default=0, ge=0, le=1)
    largest_error: bool = False
    evidence_refs: list[str] = Field(default_factory=list)

    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)


class ReviewPrioritizationContext(ContractModel):
    game_id: str = Field(min_length=1)
    review_side: ReviewSide
    candidates: list[ReviewPriorityCandidate] = Field(min_length=1, max_length=8)
    max_selection: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def _owned_bounded_candidates(self) -> "ReviewPrioritizationContext":
        identities: set[tuple[str | None, str | None, str | None]] = set()
        for candidate in self.candidates:
            reference = candidate.reference
            if reference.game_id != self.game_id or reference.review_side != self.review_side:
                raise ValueError("review priority candidate belongs to another review")
            identity = (reference.game_id, reference.review_side, reference.critical_id)
            if identity in identities:
                raise ValueError("review priority candidates must be unique")
            identities.add(identity)
        if not any(candidate.largest_error for candidate in self.candidates):
            raise ValueError("review priorities must retain the largest error")
        return self


class ModelVisibleContext(ContractModel):
    task: TaskContext
    position: PositionContext | None
    engine_facts: EngineFactsContext | None
    relevant_profile: RelevantProfileContext | None
    relevant_memory: list[LearningMemoryItem] = Field(max_length=5)
    conversation_summary: str
    discussed_positions: list[PositionReference] = Field(default_factory=list, max_length=5)
    opening_metadata: LookupOpeningResult | None = None
    review_priorities: ReviewPrioritizationContext | None = None
    allowed_evidence_refs: list[str]

    _valid_evidence_refs = field_validator("allowed_evidence_refs")(_clean_unique_strings)


class ActionTarget(ContractModel):
    """Closed structured-output target shared by grounded Agent actions."""

    game_id: str | None = None
    review_side: ReviewSide | None = None
    critical_id: str | None = None
    ply: int | None = Field(default=None, ge=0)
    fen: str | None = None
    move_uci: str | None = None
    position_references: list[PositionReference] = Field(default_factory=list, max_length=5)
    objective_skill_ids: list[str] = Field(default_factory=list, max_length=5)
    source: Literal["agent_training_draft"] | None = None

    @field_validator("game_id", "critical_id")
    @classmethod
    def _non_empty_optional_identifier(cls, value: str | None) -> str | None:
        return _non_empty_optional(value, label="action target identifier")

    @field_validator("fen")
    @classmethod
    def _valid_optional_fen(cls, value: str | None) -> str | None:
        return None if value is None else _validate_fen(value)

    @field_validator("move_uci")
    @classmethod
    def _valid_optional_uci(cls, value: str | None) -> str | None:
        return None if value is None else _validate_uci(value)

    _valid_objective_skill_ids = field_validator("objective_skill_ids")(
        _clean_unique_strings
    )


class SuggestedAction(ContractModel):
    kind: Literal[
        "open_position",
        "compare_move",
        "start_retry",
        "start_training",
        "review_weakness",
    ]
    label: str = Field(min_length=1)
    target: ActionTarget


class StartTrainingActionRequest(ContractModel):
    expected_generation: int = Field(ge=0)
    action: SuggestedAction

    @model_validator(mode="after")
    def _requires_start_training(self) -> "StartTrainingActionRequest":
        if self.action.kind != "start_training":
            raise ValueError("action must use kind=start_training")
        return self


class StartTrainingActionResult(ContractModel):
    position_references: list[PositionReference] = Field(min_length=1, max_length=5)
    objective_skill_ids: list[str] = Field(min_length=1, max_length=5)
    source: Literal["agent_training_draft"]

    _valid_objective_skill_ids = field_validator("objective_skill_ids")(
        _clean_unique_strings
    )


class AgentResponse(ContractModel):
    text: str = Field(min_length=1)
    references: list[ChessReference] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    suggested_actions: list[SuggestedAction] = Field(default_factory=list)

    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)


class ToolPositionReference(ContractModel):
    game_id: str | None = None
    critical_id: str | None = None
    fen_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")

    @model_validator(mode="after")
    def _has_reference(self) -> "ToolPositionReference":
        if not any((self.game_id, self.critical_id, self.fen_fingerprint)):
            raise ValueError("tool position reference must contain a redacted identifier")
        return self


class ToolCallRecord(ContractModel):
    name: AgentToolName
    permission: ToolPermission
    status: Literal["ok", "error", "budget_exceeded"]
    duration_ms: int = Field(ge=0)
    cache_hit: bool = False
    engine_call_count: int = Field(default=0, ge=0)
    position_reference: ToolPositionReference | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    error_code: ToolErrorCode | None = None

    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)

    @model_validator(mode="after")
    def _consistent_tool_record(self) -> "ToolCallRecord":
        if self.permission != AGENT_TOOL_PERMISSIONS[self.name]:
            raise ValueError("tool permission does not match the registered V1 permission")
        if self.status == "ok" and self.error_code is not None:
            raise ValueError("successful tool calls cannot contain error_code")
        if self.status == "error" and self.error_code is None:
            raise ValueError("failed tool calls require error_code")
        if self.status == "budget_exceeded" and self.error_code != "tool_budget_exceeded":
            raise ValueError("budget_exceeded requires tool_budget_exceeded error_code")
        if self.error_code == "tool_budget_exceeded" and self.status != "budget_exceeded":
            raise ValueError("tool_budget_exceeded requires budget_exceeded status")
        return self


class AgentRunRequest(ContractModel):
    run_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    session_id: str = Field(min_length=1)
    expected_generation: int = Field(default=0, ge=0)
    message: str = Field(min_length=1)
    model_context: ModelVisibleContext
    allowed_tools: list[AgentToolName]
    max_turns: int = Field(gt=0)
    max_total_tool_calls: int = Field(default=6, ge=0)
    max_engine_tool_calls: int = Field(default=2, ge=0)
    timeout_seconds: int = Field(gt=0)

    _valid_allowed_tools = field_validator("allowed_tools")(_clean_unique_strings)


class AgentRunResult(ContractModel):
    response: AgentResponse
    tool_calls: list[ToolCallRecord]
    usage: dict[str, int | float] = Field(default_factory=dict)


class ToolError(ContractModel):
    code: ToolErrorCode
    message: str = Field(min_length=1)
    recoverable: bool


class AgentError(ContractModel):
    code: Literal[
        "agent_unavailable",
        "agent_timeout",
        "agent_authentication_failed",
        "agent_rate_limited",
        "agent_provider_error",
        "agent_endpoint_incompatible",
        "invalid_agent_response",
        "max_turns_exceeded",
    ]
    message: str = Field(min_length=1)
    recoverable: bool


class SessionError(ContractModel):
    code: Literal[
        "session_not_found",
        "stale_agent_context",
        "session_busy",
        "invalid_session_context",
        "training_action_unavailable",
    ]
    message: str = Field(min_length=1)
    recoverable: bool


class AgentSessionResponse(ContractModel):
    session: AgentSessionState


class AgentMessageResponse(ContractModel):
    session: AgentSessionSummary
    response: AgentResponse
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class AgentErrorResponse(ContractModel):
    error: AgentError | SessionError


DataT = TypeVar("DataT")


class ToolResult(ContractModel, Generic[DataT]):
    ok: bool
    data: DataT | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    error: ToolError | None = None

    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)

    @model_validator(mode="after")
    def _success_or_error(self) -> "ToolResult[DataT]":
        if self.ok and (self.data is None or self.error is not None):
            raise ValueError("successful tool result requires data and no error")
        if not self.ok and (self.data is not None or self.error is None):
            raise ValueError("failed tool result requires an error and no data")
        return self


class GetReviewContextInput(ContractModel):
    game_id: str = Field(min_length=1)
    review_side: ReviewSide
    critical_id: str = Field(min_length=1)


class GetReviewContextResult(ContractModel):
    reference: PositionReference
    position: PositionContext
    played_move: MoveReference
    best_move: MoveReference
    classification: str = Field(min_length=1)
    criticality: str = Field(min_length=1)
    candidates: list[CandidateLine] = Field(default_factory=list, max_length=3)
    facts: dict[str, Any] = Field(default_factory=dict)
    provenance: EngineProvenance | None = None

    @model_validator(mode="after")
    def _grounded_in_referenced_position(self) -> "GetReviewContextResult":
        reference = self.reference
        if not all(
            (
                reference.game_id,
                reference.review_side,
                reference.critical_id,
                reference.fen,
            )
        ):
            raise ValueError("review result reference requires game, side, critical_id, and FEN")
        if self.position.reference != reference or self.position.fen != reference.fen:
            raise ValueError("review position must exactly match its position reference")
        board = chess.Board(self.position.fen)
        _legal_move(board, self.played_move, label="played_move")
        _legal_move(board, self.best_move, label="best_move")
        _validate_candidate_lines(self.position.fen, self.candidates)
        return self


class AnalyzePositionInput(ContractModel):
    fen: str
    purpose: Literal["compare_candidates", "find_best_move", "explain_position"]

    _valid_fen = field_validator("fen")(_validate_fen)


class AnalyzePositionResult(ContractModel):
    fen: str
    candidates: list[CandidateLine] = Field(max_length=3)
    provenance: EngineProvenance

    _valid_fen = field_validator("fen")(_validate_fen)

    @model_validator(mode="after")
    def _legal_candidate_lines(self) -> "AnalyzePositionResult":
        board = chess.Board(self.fen)
        terminal = board.is_game_over(claim_draw=True)
        if terminal and self.candidates:
            raise ValueError("terminal positions cannot contain candidate lines")
        if not terminal and not self.candidates:
            raise ValueError("non-terminal positions require at least one candidate line")
        _validate_candidate_lines(self.fen, self.candidates)
        return self


class AnalyzeMoveInput(ContractModel):
    fen_before: str
    move_uci: str

    _valid_fen = field_validator("fen_before")(_validate_fen)
    _valid_uci = field_validator("move_uci")(_validate_uci)


class AnalyzeMoveResult(ContractModel):
    fen_before: str
    legal: Literal[True]
    move: MoveReference
    score: EngineScore
    best_alternative: MoveReference | None = None
    classification: str = Field(min_length=1)
    continuation_uci: list[str] = Field(default_factory=list)
    continuation_san: list[str] = Field(default_factory=list)
    provenance: EngineProvenance

    _valid_fen = field_validator("fen_before")(_validate_fen)

    @field_validator("continuation_uci")
    @classmethod
    def _valid_continuation_uci(cls, values: list[str]) -> list[str]:
        return [_validate_uci(value) for value in values]

    @field_validator("continuation_san")
    @classmethod
    def _valid_continuation_san(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("continuation SAN moves must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _legal_move_and_continuation(self) -> "AnalyzeMoveResult":
        if len(self.continuation_uci) != len(self.continuation_san):
            raise ValueError("continuation requires paired UCI and SAN notation")
        board = chess.Board(self.fen_before)
        move = _legal_move(board, self.move, label="analyzed move")
        if self.best_alternative is not None:
            _legal_move(board, self.best_alternative, label="best alternative")
        board.push(move)
        _replay_line(
            board.fen(),
            self.continuation_uci,
            self.continuation_san,
            label="analyzed move continuation",
        )
        return self


class LookupOpeningInput(ContractModel):
    fen: str | None = None
    recent_moves_uci: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("fen")
    @classmethod
    def _valid_optional_fen(cls, value: str | None) -> str | None:
        return None if value is None else _validate_fen(value)

    @field_validator("recent_moves_uci")
    @classmethod
    def _valid_recent_moves(cls, values: list[str]) -> list[str]:
        return [_validate_uci(value) for value in values]

    @model_validator(mode="after")
    def _has_lookup_key(self) -> "LookupOpeningInput":
        if self.fen is None and not self.recent_moves_uci:
            raise ValueError("opening lookup requires fen or recent moves")
        return self


class LookupOpeningResult(ContractModel):
    eco: str | None = None
    name: str | None = None
    classification: Literal["recognized", "unrecognized"] = "unrecognized"
    metadata: dict[str, Any] = Field(default_factory=dict)


class GetPlayerProfileInput(ContractModel):
    focus_skill_ids: list[str] = Field(default_factory=list)
    focus_categories: list[str] = Field(default_factory=list)
    limit: int = Field(default=3, ge=1, le=5)

    _valid_skill_ids = field_validator("focus_skill_ids")(_clean_unique_strings)
    _valid_categories = field_validator("focus_categories")(_clean_unique_strings)


class GetPlayerProfileResult(ContractModel):
    analyzed_games: int = Field(ge=0)
    relevant_estimates: list["SkillEstimate"] = Field(default_factory=list, max_length=5)
    training_success_rate: float | None = Field(default=None, ge=0, le=100)
    recent: dict[str, int | float] = Field(default_factory=dict)
    lifetime: dict[str, int | float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _grounded_relevant_estimates(self) -> "GetPlayerProfileResult":
        for estimate in self.relevant_estimates:
            if estimate.evidence_count < 1 or not estimate.examples:
                raise ValueError("profile estimates require evidence and example references")
            if any(
                example.kind not in {"game", "critical_position", "position", "puzzle"}
                for example in estimate.examples
            ):
                raise ValueError(
                    "profile estimate examples must reference a game, critical position, "
                    "position, or puzzle"
                )
            if estimate.distinct_games > self.analyzed_games:
                raise ValueError("estimate distinct_games cannot exceed analyzed_games")
        return self


class TrainingCandidate(ContractModel):
    reference: PositionReference
    skill_ids: list[str] = Field(min_length=1)
    source_artifact: str = Field(default="analysis", min_length=1)
    category: str | None = None
    phase: str | None = None
    difficulty: str | None = None
    played_move: MoveReference | None = None
    best_move: MoveReference | None = None
    candidate_moves: list[MoveReference] = Field(default_factory=list, max_length=3)
    attempted: bool = False
    last_outcome: str | None = None
    last_attempted_at: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    estimate_confidence: Literal[
        "insufficient", "emerging", "established"
    ] | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    _valid_skill_ids = field_validator("skill_ids")(_clean_unique_strings)
    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)

    @model_validator(mode="before")
    @classmethod
    def _default_legacy_attempt_count(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "attempt_count" not in value:
            value = dict(value)
            value["attempt_count"] = 1 if value.get("attempted") else 0
        return value

    @model_validator(mode="after")
    def _consistent_attempt_metadata(self) -> "TrainingCandidate":
        if self.attempted != (self.attempt_count > 0):
            raise ValueError("attempted must match attempt_count")
        if self.attempt_count == 0 and (
            self.last_outcome is not None or self.last_attempted_at is not None
        ):
            raise ValueError("attempt metadata requires an attempt")
        if self.reference.fen is not None:
            board = chess.Board(self.reference.fen)
            for label, move in (
                ("played_move", self.played_move),
                ("best_move", self.best_move),
            ):
                if move is not None:
                    _legal_move(board, move, label=label)
            for index, move in enumerate(self.candidate_moves, start=1):
                _legal_move(board, move, label=f"candidate move {index}")
        return self


class GetTrainingCandidatesInput(ContractModel):
    skill_ids: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    window: Literal["recent", "lifetime"] = "recent"
    limit: int = Field(default=10, ge=1, le=10)
    exclude_recently_practiced: bool = True
    exclude_recently_solved: bool = True
    exclude_current_game: bool = False
    recent_practice_days: int = Field(default=7, ge=1, le=30)

    _valid_skill_ids = field_validator("skill_ids")(_clean_unique_strings)
    _valid_categories = field_validator("categories")(_clean_unique_strings)


class GetTrainingCandidatesResult(ContractModel):
    candidates: list[TrainingCandidate] = Field(default_factory=list)


class TrainingDraft(ContractModel):
    title: str = Field(min_length=1)
    objective_skill_ids: list[str] = Field(min_length=1, max_length=5)
    position_references: list[PositionReference] = Field(min_length=1, max_length=5)
    rationale: str = Field(min_length=1)
    recommended_count: int = Field(gt=0, le=5)
    source: Literal["agent_training_draft"] = "agent_training_draft"
    evidence_refs: list[str] = Field(default_factory=list)

    _valid_skill_ids = field_validator("objective_skill_ids")(_clean_unique_strings)
    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)

    @model_validator(mode="after")
    def _valid_recommended_count(self) -> "TrainingDraft":
        if self.recommended_count > len(self.position_references):
            raise ValueError("recommended_count cannot exceed available positions")
        return self


class CreateTrainingDraftInput(ContractModel):
    title: str = Field(min_length=1)
    objective_skill_ids: list[str] = Field(min_length=1, max_length=5)
    position_references: list[PositionReference] = Field(min_length=1, max_length=5)
    rationale: str = Field(min_length=1)
    recommended_count: int = Field(gt=0, le=5)
    source: Literal["agent_training_draft"] = "agent_training_draft"
    evidence_refs: list[str] = Field(default_factory=list)

    _valid_skill_ids = field_validator("objective_skill_ids")(_clean_unique_strings)
    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)

    @model_validator(mode="after")
    def _valid_recommended_count(self) -> "CreateTrainingDraftInput":
        if self.recommended_count > len(self.position_references):
            raise ValueError("recommended_count cannot exceed available positions")
        return self


class SkillDefinition(ContractModel):
    taxonomy_version: int = Field(ge=1)
    skill_id: str = Field(min_length=1)
    parent_id: str | None = None
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supported_evidence_types: list[LearningEvidenceType] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)

    _valid_evidence_types = field_validator("supported_evidence_types")(
        _clean_unique_strings
    )
    _valid_aliases = field_validator("aliases")(_clean_unique_strings)


class LearningObservation(ContractModel):
    schema_version: int = Field(default=1, ge=1)
    taxonomy_version: int = Field(default=1, ge=1)
    observation_id: str = Field(min_length=1)
    dedupe_key: str = Field(min_length=1)
    skill_id: str = Field(min_length=1)
    outcome: Literal["success", "failure", "partial"]
    source_type: Literal[
        "game_fact",
        "retry_attempt",
        "puzzle_attempt",
        "training_attempt",
    ]
    evidence_type: LearningEvidenceType
    game_id: str | None = None
    review_side: ReviewSide | None = None
    critical_id: str | None = None
    attempt_id: str | None = None
    puzzle_id: str | None = None
    severity: float | None = Field(default=None, ge=0)
    evidence_refs: list[str] = Field(min_length=1)
    occurred_at: str = Field(min_length=1)

    _valid_evidence_refs = field_validator("evidence_refs")(_clean_unique_strings)

    @field_validator(
        "observation_id",
        "dedupe_key",
        "skill_id",
        "game_id",
        "critical_id",
        "attempt_id",
        "puzzle_id",
    )
    @classmethod
    def _valid_observation_identifiers(cls, value: str | None) -> str | None:
        return _non_empty_optional(value, label="observation identifier")

    _valid_occurred_at = field_validator("occurred_at")(_validate_utc_datetime)

    @model_validator(mode="after")
    def _valid_source_ownership(self) -> "LearningObservation":
        source_evidence = {
            "game_fact": {"fact_motif", "fact_composite", "clock_outcome"},
            "retry_attempt": {"attempt_outcome"},
            "training_attempt": {"attempt_outcome"},
            "puzzle_attempt": {"attempt_outcome", "puzzle_theme"},
        }
        if self.evidence_type not in source_evidence[self.source_type]:
            raise ValueError("evidence_type is not supported by source_type")

        game_fields = (self.game_id, self.review_side, self.critical_id)
        has_complete_game_owner = all(value is not None for value in game_fields)
        has_partial_game_owner = any(value is not None for value in game_fields)
        if has_partial_game_owner and not has_complete_game_owner:
            raise ValueError("game ownership requires game_id, review_side, and critical_id")

        if self.source_type == "game_fact":
            if not has_complete_game_owner:
                raise ValueError("game facts require complete game ownership")
            if self.attempt_id is not None or self.puzzle_id is not None:
                raise ValueError("game facts cannot claim attempt or puzzle ownership")
        elif self.source_type in {"retry_attempt", "training_attempt"}:
            if self.attempt_id is None or not has_complete_game_owner:
                raise ValueError("game training attempts require attempt and game ownership")
            if self.puzzle_id is not None:
                raise ValueError("game training attempts cannot claim puzzle ownership")
        else:
            if self.attempt_id is None or self.puzzle_id is None:
                raise ValueError("puzzle attempts require attempt_id and puzzle_id")
            if has_complete_game_owner:
                raise ValueError("puzzle attempts cannot also claim game ownership")
        return self


class SkillEstimate(ContractModel):
    schema_version: int = Field(default=1, ge=1)
    taxonomy_version: int = Field(ge=1)
    skill_id: str = Field(min_length=1)
    evidence_count: int = Field(ge=0)
    distinct_games: int = Field(ge=0)
    distinct_positions: int = Field(ge=0)
    success_count: int = Field(ge=0)
    partial_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    cumulative_loss: float = Field(ge=0)
    recent_failure_count: int = Field(ge=0)
    last_seen: str | None = None
    confidence_level: Literal["insufficient", "emerging", "established"]
    status: Literal["unknown", "watch", "weakness", "strength"]
    examples: list[ChessReference] = Field(max_length=3)

    @model_validator(mode="before")
    @classmethod
    def _backfill_distinct_positions(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "distinct_positions" not in value:
            value = dict(value)
            value["distinct_positions"] = value.get("distinct_games", 0)
        return value

    @model_validator(mode="after")
    def _consistent_counts(self) -> "SkillEstimate":
        outcomes = self.success_count + self.partial_count + self.failure_count
        if outcomes != self.evidence_count:
            raise ValueError("outcome counts must add up to evidence_count")
        if self.distinct_games > self.evidence_count:
            raise ValueError("distinct_games cannot exceed evidence_count")
        if self.distinct_positions > self.evidence_count:
            raise ValueError("distinct_positions cannot exceed evidence_count")
        if self.distinct_games > self.distinct_positions:
            raise ValueError("distinct_games cannot exceed distinct_positions")
        if self.recent_failure_count > self.failure_count:
            raise ValueError("recent failures cannot exceed all failures")
        return self


RelevantProfileContext.model_rebuild()
ModelVisibleContext.model_rebuild()
GetPlayerProfileResult.model_rebuild()
