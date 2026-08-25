"""Grounding policy, tool exposure, and deterministic Agent response validation."""
from __future__ import annotations

from collections.abc import Sequence
import json
import re

import chess

from server.core.agent.models import (
    AnalyzeMoveResult,
    AnalyzePositionResult,
    AgentResponse,
    AgentToolName,
    ChessReference,
    GetPlayerProfileResult,
    GetReviewContextResult,
    ModelVisibleContext,
    PositionReference,
    SuggestedAction,
    ToolCallRecord,
    TrainingDraft,
)


POLICY_VERSION = 2
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
_TRAINING_PLANNING_QUESTION = re.compile(
    r"(?:what\s+should\s+i\s+(?:train|practice)|what\s+to\s+(?:train|practice)|"
    r"train(?:ing)?\s+plan|practice\s+plan|next\s+(?:training|practice)|"
    r"(?:build|create|make|plan)\b[^?.!\n]{0,60}\b(?:training|practice)\s+"
    r"(?:plan|session|draft|set)|"
    r"练什么|训练什么|怎么练|训练计划|练习计划|接下来练|下一步练)",
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


def is_training_planning_request(message: str) -> bool:
    return bool(_TRAINING_PLANNING_QUESTION.search(message))


def allowed_tools_for(message: str, context: ModelVisibleContext) -> list[AgentToolName]:
    """Expose only tools that can operate on the explicit current checkpoint."""

    allowed: list[AgentToolName] = []
    planning = is_training_planning_request(message)
    if (context.position is not None and context.engine_facts is not None) or (
        planning and context.task.personalization_enabled
    ):
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
        and (_PROFILE_QUESTION.search(message) or _PRIORITY_QUESTION.search(message) or planning)
    ):
        allowed.append("get_player_profile")
    if context.task.personalization_enabled and planning:
        allowed.extend(["get_training_candidates", "create_training_draft"])
    return allowed


def build_model_input(context: ModelVisibleContext) -> str:
    """Serialize the sole model-visible chess context into the Agent instructions."""

    payload = context.model_dump(mode="json", exclude_none=True)
    return (
        "You are Chess Review Coach, a single chess teaching agent. Match the user's language. "
        "Lead with the conclusion, preserve required evidence and caveats, and omit repetition.\n\n"
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
        "recurring weakness or strength only from relevant_memory or after get_player_profile "
        "returns concrete evidence. "
        "When personalization_enabled is false, do not request or imply profile evidence.\n"
        "- A skill reference or personalization_claims always means retrieved evidence about this "
        "specific user. Never emit either for a generic chess concept, even when personalization "
        "is enabled. If no profile evidence is present in context or a successful profile tool "
        "result, leave personalization_claims empty and include no skill reference.\n"
        "- Mirror every material position, move, classification, score-POV, and personalization "
        "statement from the answer in the structured grounding fields. Include each exact position "
        "reference and evidence_ref actually used; never include an unused or invented one. Mark "
        "uncertainty true only when missing context, tool failure, or tool budget leaves the answer "
        "materially unresolved. Routine caveats, or a supported conclusion that profile evidence "
        "is insufficient to call a pattern recurring, use uncertainty false. When supplied facts "
        "directly answer the request, completion is full even if they contain no score or PV.\n"
        "- Outcome mapping is exact: no failure => full/none/no error; missing position => "
        "partial/missing_context/no error; handled illegal_move => full/tool_error_handled/"
        "illegal_move; budget exhaustion => partial/tool_budget_exhausted/tool_budget_exceeded; "
        "other tool failures => partial/recoverable_tool_failure/the returned error code. Every "
        "tool error must remain reflected in the final outcome even when a later fallback succeeds. "
        "After a recoverable failure, use any explicit fallback requested by the user.\n"
        "- A legality question about a concrete move always requires analyze_move, including when "
        "the move appears obviously illegal from the FEN; do not self-certify legality.\n"
        "- If the answer discusses the current board, include its exact position reference. Copy "
        "all evidence_refs actually used from Engine facts or successful tools. On a failed profile "
        "tool, do not emit profile claims or skill references. Canonical focus examples: fork => "
        "tactics.fork_detection; opponent forcing moves => "
        "calculation.opponent_forcing_moves.\n"
        "- If review_priorities is present, choose one to three entries only from that shortlist, "
        "retain its largest_error entry, and do not change any classification or invent a score.\n"
        "- For training planning, retrieve candidates before creating one draft. Draft positions "
        "must come from that retrieval, objectives must use their canonical skill_ids, and the "
        "start_training action must copy the successful draft with source agent_training_draft.\n"
        "- Suggested actions are limited to open_position, compare_move, start_retry, and a "
        "validated start_training draft.\n\n"
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
            item.skill_id == reference.skill_id for item in context.relevant_memory
        ) or any(
            reference == allowed for allowed in validated_tool_references
        )
    if any(
        reference == example
        for item in context.relevant_memory
        for example in item.examples
    ):
        return True
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


def _position_claim_reference(reference: PositionReference) -> ChessReference:
    values = reference.model_dump(mode="python", exclude_none=True)
    return ChessReference(
        kind=("critical_position" if values.get("critical_id") else "position"),
        **values,
    )


def _validate_grounding_outcome(
    response: AgentResponse,
    tool_calls: Sequence[ToolCallRecord],
) -> None:
    grounding = response.grounding
    errors = [call.error_code for call in tool_calls if call.error_code is not None]
    if "tool_budget_exceeded" in errors:
        expected = ("partial", "tool_budget_exhausted", "tool_budget_exceeded")
    elif errors:
        error_code = errors[0]
        expected = (
            ("full", "tool_error_handled", error_code)
            if all(item == "illegal_move" for item in errors)
            else ("partial", "recoverable_tool_failure", error_code)
        )
    else:
        expected = (
            ("partial", "missing_context", None)
            if grounding.degradation == "missing_context"
            else ("full", "none", None)
        )
    actual = (grounding.completion, grounding.degradation, grounding.error_code)
    if actual != expected:
        raise AgentResponseValidationError(
            "Agent response degradation does not match this run's tool results."
        )


def _validate_grounding_claims(
    response: AgentResponse,
    context: ModelVisibleContext,
    *,
    validated_tool_references: Sequence[ChessReference],
    successful_tool_results: Sequence[object],
) -> None:
    grounding = response.grounding
    claims = grounding.claims
    position = context.position
    for move_claim in grounding.move_claims:
        owned = _position_claim_reference(move_claim.position)
        if not _matches_reference(owned, context, validated_tool_references):
            raise AgentResponseValidationError(
                "Agent move claim references an unowned chess position."
            )
        board_for_claim = chess.Board(move_claim.position.fen or "")
        actual_legal = chess.Move.from_uci(move_claim.move_uci) in board_for_claim.legal_moves
        if move_claim.legal != actual_legal:
            raise AgentResponseValidationError(
                "Agent move claim legality does not match its supplied FEN."
            )

    if claims.move_uci is not None and claims.legal is not None:
        if not any(
            item.move_uci == claims.move_uci and item.legal == claims.legal
            for item in grounding.move_claims
        ):
            raise AgentResponseValidationError(
                "Agent move legality claim requires a matching move_claim."
            )

    board = chess.Board(position.fen) if position is not None else None
    if claims.side_to_move is not None:
        if board is None:
            raise AgentResponseValidationError("side_to_move requires a current position.")
        actual_side = "white" if board.turn == chess.WHITE else "black"
        if claims.side_to_move != actual_side:
            raise AgentResponseValidationError("side_to_move does not match the current FEN.")
    if claims.move_san is not None:
        if board is None:
            raise AgentResponseValidationError("move_san requires a current position.")
        move = chess.Move.from_uci(claims.move_uci or "")
        if move not in board.legal_moves or board.san(move) != claims.move_san:
            raise AgentResponseValidationError("move_san does not match the current FEN.")

    classifications: set[str] = set()
    best_moves: set[str] = set()
    score_povs: set[str] = set()
    facts = context.engine_facts
    if facts is not None:
        if facts.classification:
            classifications.add(facts.classification)
        if facts.best_move is not None:
            best_moves.add(facts.best_move.uci)
        score_povs.update(candidate.score.pov for candidate in facts.candidates)
        score_pov = facts.facts.get("score_pov")
        if score_pov in {"white", "black"}:
            score_povs.add(score_pov)
    estimates = list(context.relevant_memory)
    if context.relevant_profile is not None:
        estimates.extend(
            [*context.relevant_profile.weaknesses, *context.relevant_profile.strengths]
        )
    for result in successful_tool_results:
        if isinstance(result, GetReviewContextResult):
            classifications.add(result.classification)
            best_moves.add(result.best_move.uci)
            score_povs.update(candidate.score.pov for candidate in result.candidates)
        elif isinstance(result, AnalyzeMoveResult):
            classifications.add(result.classification)
            score_povs.add(result.score.pov)
            if result.best_alternative is not None:
                best_moves.add(result.best_alternative.uci)
        elif isinstance(result, AnalyzePositionResult):
            if result.candidates:
                best_moves.add(result.candidates[0].move.uci)
            score_povs.update(candidate.score.pov for candidate in result.candidates)
        elif isinstance(result, GetPlayerProfileResult):
            estimates.extend(result.relevant_estimates)

    evidence_claims = (
        claims.classification,
        claims.best_move_uci,
        claims.score_pov,
    )
    if any(value is not None for value in evidence_claims) and not response.evidence_refs:
        raise AgentResponseValidationError("Engine-backed claims require evidence_refs.")
    if claims.classification is not None and claims.classification not in classifications:
        raise AgentResponseValidationError(
            "Agent classification is not present in authoritative facts."
        )
    if claims.best_move_uci is not None and claims.best_move_uci not in best_moves:
        raise AgentResponseValidationError(
            "Agent best move is not present in authoritative facts."
        )
    if claims.score_pov is not None and claims.score_pov not in score_povs:
        raise AgentResponseValidationError(
            "Agent score POV is not present in authoritative facts."
        )

    personal = grounding.personalization_claims
    personal_values = (personal.skill_id, personal.status, personal.distinct_games)
    if any(value is not None for value in personal_values):
        if not context.task.personalization_enabled or not response.evidence_refs:
            raise AgentResponseValidationError(
                "Personalization claims require enabled, retrieved evidence."
            )
        matching = [
            estimate
            for estimate in estimates
            if personal.skill_id in (None, estimate.skill_id)
            and personal.status in (None, estimate.status)
            and personal.distinct_games in (None, estimate.distinct_games)
        ]
        if not matching:
            raise AgentResponseValidationError(
                "Personalization claims do not match retrieved learning evidence."
            )


def _target_subset(target: dict[str, object], allowed: set[str]) -> bool:
    return bool(target) and set(target).issubset(allowed)


def _validate_action(
    action: SuggestedAction,
    context: ModelVisibleContext,
    successful_training_drafts: Sequence[TrainingDraft],
) -> None:
    if action.kind == "start_training":
        target = action.target.model_dump(
            mode="python", exclude_none=True, exclude_defaults=True
        )
        if set(target) != {"position_references", "objective_skill_ids", "source"}:
            raise AgentResponseValidationError("start_training has an invalid target.")
        if target["source"] != "agent_training_draft":
            raise AgentResponseValidationError("start_training has an invalid source.")
        positions = action.target.position_references
        objectives = set(action.target.objective_skill_ids)
        if not positions or not objectives:
            raise AgentResponseValidationError("start_training requires positions and objectives.")
        for draft in successful_training_drafts:
            if len(positions) > draft.recommended_count:
                continue
            draft_positions = {
                reference.model_dump_json(exclude_none=True)
                for reference in draft.position_references
            }
            if all(
                reference.model_dump_json(exclude_none=True) in draft_positions
                for reference in positions
            ) and objectives.issubset(draft.objective_skill_ids):
                return
        raise AgentResponseValidationError(
            "start_training does not match a successful draft from this run."
        )
    if action.kind not in {"open_position", "compare_move", "start_retry"}:
        raise AgentResponseValidationError("Suggested action is not available in Phase 1.")
    position = context.position
    facts = context.engine_facts
    target = action.target.model_dump(mode="python", exclude_none=True, exclude_defaults=True)
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
    successful_training_drafts: Sequence[TrainingDraft] = (),
    successful_tool_results: Sequence[object] = (),
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
    _validate_grounding_outcome(response, tool_calls)
    _validate_grounding_claims(
        response,
        context,
        validated_tool_references=validated_tool_references,
        successful_tool_results=successful_tool_results,
    )
    for action in response.suggested_actions:
        _validate_action(action, context, successful_training_drafts)
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
