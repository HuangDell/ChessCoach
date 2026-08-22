"""Grounding policy, tool exposure, and deterministic Agent response validation."""
from __future__ import annotations

from collections.abc import Sequence
import json
import re

import chess

from server.core.agent.models import (
    AgentResponse,
    AgentToolName,
    ChessReference,
    ModelVisibleContext,
    SuggestedAction,
    ToolCallRecord,
)


POLICY_VERSION = 1
_MOVE_QUESTION = re.compile(
    r"(?:\b[a-h][1-8][a-h][1-8][qrbn]?\b|why\s+(?:is|was|not|can(?:not|'t))|what\s+if|instead|"
    r"为什么不能|为何不能|如果|改走)",
    re.IGNORECASE,
)
_POSITION_QUESTION = re.compile(
    r"(?:best\s+move|candidate|compare|evaluate|position|候选|比较|最佳着|评估|局面)",
    re.IGNORECASE,
)
_OPENING_QUESTION = re.compile(
    r"(?:\bopening\b|\beco\b|debut|开局|开局名称|开局计划)", re.IGNORECASE
)
_PROFILE_QUESTION = re.compile(
    r"(?:profile|weakness|strength|recurr|habit|personal|弱点|强项|反复|经常|个人)",
    re.IGNORECASE,
)
_PRIORITY_QUESTION = re.compile(
    r"(?:review\s+first|focus\s+first|prioriti[sz]e|where\s+should\s+i\s+start|"
    r"先复盘|先看哪|复盘哪里|重点局面|优先)",
    re.IGNORECASE,
)
_FOLLOW_UP_REFERENCE = re.compile(
    r"(?:\bhere\b|\bthere\b|that\s+(?:move|position|line)|this\s+(?:move|position)|"
    r"这里|这儿|那里|那儿|那一步|这个局面|这个变化|这里呢|那里呢)",
    re.IGNORECASE,
)


def is_review_priority_request(message: str) -> bool:
    return bool(_PRIORITY_QUESTION.search(message))


def is_follow_up_reference_request(message: str) -> bool:
    return bool(_FOLLOW_UP_REFERENCE.search(message))


def allowed_tools_for(message: str, context: ModelVisibleContext) -> list[AgentToolName]:
    """Expose only tools that can operate on the explicit current checkpoint."""

    allowed: list[AgentToolName] = []
    if context.position is not None and context.engine_facts is not None:
        allowed.append("get_review_context")
    if context.position is not None and _MOVE_QUESTION.search(message):
        allowed.append("analyze_move")
    if (
        context.position is not None
        and _POSITION_QUESTION.search(message)
        and context.engine_facts is None
    ):
        allowed.append("analyze_position")
    if context.position is not None and _OPENING_QUESTION.search(message):
        allowed.append("lookup_opening")
    if (
        context.task.personalization_enabled
        and (_PROFILE_QUESTION.search(message) or _PRIORITY_QUESTION.search(message))
    ):
        allowed.append("get_player_profile")
    return allowed


def build_model_input(context: ModelVisibleContext) -> str:
    """Serialize the sole model-visible chess context into the Agent instructions."""

    payload = context.model_dump(mode="json", exclude_none=True)
    return (
        "You are Chess Review Coach, a single chess teaching agent. Match the user's language and "
        "be concise and concrete.\n\n"
        "GROUNDING RULES (mandatory):\n"
        "- The current FEN below is authoritative. Never reconstruct or change it from prose.\n"
        "- Only supplied Engine/Facts or a successful registered tool may support claims that a "
        "move is legal, best, winning, losing, a mistake, or a forced tactic.\n"
        "- Never alter an authoritative classification. If evidence is absent or a reference such "
        "as 'there' is ambiguous, ask for the exact position or answer conservatively.\n"
        "- Prefer existing review facts. Use analyze_move only for an uncovered what-if move and "
        "analyze_position only for a general candidate comparison. Conceptual questions need no "
        "Engine call. The backend, not the user or model, controls depth and budgets.\n"
        "- The conversation summary is continuity-only, never chess truth. Re-read the current "
        "checkpoint, Engine facts, or tools for scores, legality, lines, classification, and FEN.\n"
        "- Cite only evidence_refs present in this context or successful tool results. Claim a "
        "recurring weakness or strength only after get_player_profile returns concrete evidence. "
        "When personalization_enabled is false, do not request or imply profile evidence.\n"
        "- If review_priorities is present, choose one to three entries only from that shortlist, "
        "retain its largest_error entry, and do not change any classification or invent a score.\n"
        "- Suggested actions are limited to open_position, compare_move, and start_retry and must "
        "target the current position or a validated review-priority candidate.\n\n"
        "MODEL_VISIBLE_CONTEXT_JSON:\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


class AgentResponseValidationError(ValueError):
    pass


def _matches_reference(
    reference: ChessReference,
    context: ModelVisibleContext,
    validated_tool_references: Sequence[ChessReference],
) -> bool:
    position = context.position
    facts = context.engine_facts
    game_id = None
    review_side = context.task.review_side
    critical_id = None
    if position and position.reference:
        game_id = position.reference.game_id
        critical_id = position.reference.critical_id
    if facts is not None:
        game_id = facts.reference.game_id
        review_side = facts.reference.review_side
        critical_id = facts.reference.critical_id

    if reference.kind == "skill":
        estimates = []
        if context.relevant_profile is not None:
            estimates = [
                *context.relevant_profile.weaknesses,
                *context.relevant_profile.strengths,
            ]
        return any(item.skill_id == reference.skill_id for item in estimates) or any(
            reference == allowed for allowed in validated_tool_references
        )
    if any(reference == allowed for allowed in validated_tool_references):
        return True
    priorities = context.review_priorities
    if priorities is not None:
        for candidate in priorities.candidates:
            owned = candidate.reference
            if (
                reference.game_id in (None, owned.game_id)
                and reference.review_side in (None, owned.review_side)
                and reference.critical_id in (None, owned.critical_id)
                and reference.ply in (None, owned.ply)
                and reference.fen in (None, owned.fen)
                and reference.game_id is not None
            ):
                return True
        return False
    if reference.game_id is not None and reference.game_id != game_id:
        return False
    if reference.review_side is not None and reference.review_side != review_side:
        return False
    if reference.critical_id is not None and reference.critical_id != critical_id:
        return False
    if reference.fen is not None and (position is None or reference.fen != position.fen):
        return False
    if reference.ply is not None:
        owned_ply = position.reference.ply if position and position.reference else None
        if reference.ply != owned_ply:
            return False
    return reference.game_id is not None or reference.fen is not None


def _target_subset(target: dict[str, object], allowed: set[str]) -> bool:
    return bool(target) and set(target).issubset(allowed)


def _validate_action(action: SuggestedAction, context: ModelVisibleContext) -> None:
    if action.kind not in {"open_position", "compare_move", "start_retry"}:
        raise AgentResponseValidationError("Suggested action is not available in Phase 1.")
    position = context.position
    facts = context.engine_facts
    target = action.target.model_dump(mode="python", exclude_none=True)
    if action.kind == "compare_move":
        if position is None or not _target_subset(target, {"fen", "move_uci"}):
            raise AgentResponseValidationError("compare_move has an invalid target.")
        if target.get("fen") not in (None, position.fen):
            raise AgentResponseValidationError("compare_move targets a different FEN.")
        try:
            move = chess.Move.from_uci(str(target["move_uci"]).lower())
        except (KeyError, ValueError) as exc:
            raise AgentResponseValidationError("compare_move requires a legal UCI move.") from exc
        if move not in chess.Board(position.fen).legal_moves:
            raise AgentResponseValidationError("compare_move requires a legal UCI move.")
        return

    priorities = context.review_priorities
    if action.kind in {"open_position", "start_retry"} and priorities is not None:
        for candidate in priorities.candidates:
            expected = candidate.reference.model_dump(mode="python", exclude_none=True)
            if target and all(expected.get(key) == value for key, value in target.items()):
                if action.kind == "open_position" and not all(
                    target.get(key) for key in ("game_id", "review_side", "critical_id")
                ):
                    continue
                if action.kind == "start_retry" and not all(
                    target.get(key) for key in ("game_id", "review_side", "critical_id")
                ):
                    continue
                return
        raise AgentResponseValidationError(
            f"{action.kind} targets a position outside the review shortlist."
        )

    if facts is None:
        if action.kind == "start_retry":
            raise AgentResponseValidationError("start_retry requires an active critical position.")
        if position is None or not _target_subset(target, {"fen", "ply"}):
            raise AgentResponseValidationError("open_position has an invalid target.")
        if target.get("fen") != position.fen:
            raise AgentResponseValidationError("open_position targets a different FEN.")
        return

    allowed = {"game_id", "review_side", "critical_id", "ply", "fen"}
    if not _target_subset(target, allowed):
        raise AgentResponseValidationError(f"{action.kind} has an invalid target.")
    expected = facts.reference.model_dump(mode="python", exclude_none=True)
    for key, value in target.items():
        if expected.get(key) != value:
            raise AgentResponseValidationError(f"{action.kind} targets a different position.")
    if action.kind == "start_retry" and not all(
        target.get(key) for key in ("game_id", "review_side", "critical_id")
    ):
        raise AgentResponseValidationError("start_retry requires a critical position target.")


def validate_agent_response(
    response: AgentResponse,
    context: ModelVisibleContext,
    tool_calls: list[ToolCallRecord],
    *,
    validated_tool_references: Sequence[ChessReference] = (),
) -> AgentResponse:
    allowed_evidence = set(context.allowed_evidence_refs)
    for call in tool_calls:
        if call.status == "ok":
            allowed_evidence.update(call.evidence_refs)
    if not set(response.evidence_refs).issubset(allowed_evidence):
        raise AgentResponseValidationError("Agent response cites evidence outside this run.")
    if any(
        not _matches_reference(reference, context, validated_tool_references)
        for reference in response.references
    ):
        raise AgentResponseValidationError("Agent response references an unowned chess position.")
    for action in response.suggested_actions:
        _validate_action(action, context)
    priorities = context.review_priorities
    if priorities is not None:
        shortlist = {
            (
                candidate.reference.game_id,
                candidate.reference.review_side,
                candidate.reference.critical_id,
            )
            for candidate in priorities.candidates
        }
        selected = {
            (reference.game_id, reference.review_side, reference.critical_id)
            for reference in response.references
            if (
                reference.game_id,
                reference.review_side,
                reference.critical_id,
            ) in shortlist
        }
        selected.update(
            (
                action.target.game_id,
                action.target.review_side,
                action.target.critical_id,
            )
            for action in response.suggested_actions
            if (
                action.target.game_id,
                action.target.review_side,
                action.target.critical_id,
            ) in shortlist
        )
        if len(selected) > priorities.max_selection:
            raise AgentResponseValidationError("Agent selected too many review priorities.")
        largest = {
            (
                candidate.reference.game_id,
                candidate.reference.review_side,
                candidate.reference.critical_id,
            )
            for candidate in priorities.candidates
            if candidate.largest_error
        }
        if not selected.intersection(largest):
            raise AgentResponseValidationError("Agent omitted the deterministic largest error.")
    return response
