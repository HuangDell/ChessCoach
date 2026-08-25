from __future__ import annotations

import unittest
from typing import Any

from pydantic import ValidationError

from server.core.agent.fakes import FakeAgentRuntime, FakeAgentTools
from server.core.agent.models import (
    AgentError,
    AgentErrorResponse,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentResponse,
    AgentRunRequest,
    AgentRunResult,
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    AgentSessionState,
    AgentSessionSummary,
    AnalyzeMoveInput,
    AnalyzeMoveResult,
    AnalyzePositionInput,
    AnalyzePositionResult,
    CandidateLine,
    ChessReference,
    EngineProvenance,
    EngineScore,
    GetPlayerProfileInput,
    GetPlayerProfileResult,
    GetReviewContextInput,
    GetReviewContextResult,
    LearningMemoryItem,
    LearningObservation,
    MemoryQuery,
    ModelVisibleContext,
    MoveReference,
    PositionContext,
    PositionReference,
    RelevantProfileContext,
    SessionError,
    SkillDefinition,
    SkillEstimate,
    CompareMoveAction,
    TaskContext,
    ToolCallRecord,
    ToolError,
    ToolResult,
    TrainingDraft,
)
from server.core.agent.runtime import AgentRuntime


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
STALEMATE_FEN = "7k/5Q2/7K/8/8/8/8/8 b - - 0 1"


def provenance() -> EngineProvenance:
    return EngineProvenance(
        engine_name="Stockfish",
        engine_version="17",
        depth=18,
        multipv=3,
        analysis_profile_id="balanced-v1",
    )


def candidate() -> CandidateLine:
    return CandidateLine(
        rank=1,
        move=MoveReference(uci="e2e4", san="e4"),
        score=EngineScore(kind="cp", value=31, pov="review_side"),
        win_percent_for_review_side=54.2,
        line_uci=["e2e4", "e7e5"],
        line_san=["e4", "e5"],
    )


def d4_candidate() -> CandidateLine:
    return CandidateLine(
        rank=1,
        move=MoveReference(uci="d2d4", san="d4"),
        score=EngineScore(kind="cp", value=24, pov="review_side"),
        win_percent_for_review_side=53.0,
        line_uci=["d2d4", "d7d5"],
        line_san=["d4", "d5"],
    )


def analyze_move_result() -> AnalyzeMoveResult:
    return AnalyzeMoveResult(
        fen_before=START_FEN,
        legal=True,
        move=MoveReference(uci="e2e4", san="e4"),
        score=EngineScore(kind="cp", value=31, pov="white"),
        best_alternative=MoveReference(uci="d2d4", san="d4"),
        classification="good",
        continuation_uci=["e7e5", "g1f3"],
        continuation_san=["e5", "Nf3"],
        provenance=provenance(),
    )


def skill_estimate(status: str = "weakness") -> SkillEstimate:
    success = 2 if status == "strength" else 0
    failures = 0 if status == "strength" else 2
    return SkillEstimate(
        taxonomy_version=1,
        skill_id="calculation.candidate_moves",
        evidence_count=2,
        distinct_games=2,
        distinct_positions=2,
        success_count=success,
        partial_count=0,
        failure_count=failures,
        cumulative_loss=0 if status == "strength" else 28,
        recent_failure_count=failures,
        last_seen="2026-08-21T10:00:00Z",
        confidence_level="established",
        status=status,  # type: ignore[arg-type]
        examples=[
            ChessReference(
                kind="critical_position",
                game_id="game-1",
                review_side="white",
                critical_id="ply-1",
            )
        ],
    )


def visible_context() -> ModelVisibleContext:
    return ModelVisibleContext(
        task=TaskContext(activity="position_analysis", review_side="white"),
        position=PositionContext(
            fen=START_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
        ),
        engine_facts=None,
        relevant_profile=None,
        relevant_memory=[],
        conversation_summary="",
        allowed_evidence_refs=[],
    )


def run_result() -> AgentRunResult:
    return AgentRunResult(
        response=AgentResponse(text="e4 controls the center."),
        tool_calls=[],
    )


class ChessNotationContractTests(unittest.TestCase):
    def test_move_reference_requires_valid_uci_and_nonempty_san(self) -> None:
        move = MoveReference(uci="E2E4", san=" e4 ")
        self.assertEqual(move.uci, "e2e4")
        self.assertEqual(move.san, "e4")

        with self.assertRaises(ValidationError):
            MoveReference(uci="e4", san="e4")
        with self.assertRaises(ValidationError):
            MoveReference(uci="e2e4", san=" ")

    def test_engine_score_requires_explicit_supported_pov(self) -> None:
        for pov in ("white", "black", "side_to_move", "review_side"):
            score = EngineScore(kind="cp", value=10, pov=pov)  # type: ignore[arg-type]
            self.assertEqual(score.pov, pov)

        with self.assertRaises(ValidationError):
            EngineScore.model_validate({"kind": "cp", "value": 10})
        with self.assertRaises(ValidationError):
            EngineScore(kind="mate", value=2, pov="mover")  # type: ignore[arg-type]

    def test_candidate_line_requires_paired_notation_and_matching_first_move(self) -> None:
        self.assertEqual(candidate().score.pov, "review_side")

        with self.assertRaises(ValidationError):
            CandidateLine(
                rank=1,
                move=MoveReference(uci="e2e4", san="e4"),
                score=EngineScore(kind="cp", value=0, pov="white"),
                win_percent_for_review_side=None,
                line_uci=["d2d4"],
                line_san=["d4"],
            )
        with self.assertRaises(ValidationError):
            CandidateLine(
                rank=1,
                move=MoveReference(uci="e2e4", san="e4"),
                score=EngineScore(kind="cp", value=0, pov="white"),
                win_percent_for_review_side=None,
                line_uci=["e2e4", "e7e5"],
                line_san=["e4"],
            )

    def test_position_context_validates_fen_and_move_pairs(self) -> None:
        position = PositionContext(
            fen=START_FEN,
            recent_moves_uci=["e2e4"],
            recent_moves_san=["e4"],
            selected_move_uci="g1f3",
            selected_move_san="Nf3",
        )
        self.assertEqual(position.selected_move_san, "Nf3")

        with self.assertRaises(ValidationError):
            PositionContext(fen="not a fen", recent_moves_uci=[], recent_moves_san=[])
        with self.assertRaises(ValidationError):
            PositionContext(
                fen="8/8/8/8/8/8/8/8 w - - 0 1",
                recent_moves_uci=[],
                recent_moves_san=[],
            )
        with self.assertRaises(ValidationError):
            PositionContext(
                fen=START_FEN,
                recent_moves_uci=["e2e4"],
                recent_moves_san=[],
            )
        with self.assertRaises(ValidationError):
            PositionContext(
                fen=START_FEN,
                recent_moves_uci=[],
                recent_moves_san=[],
                selected_move_uci="e2e4",
            )

    def test_position_context_fen_matches_reference(self) -> None:
        reference = PositionReference(game_id="game-1", fen=START_FEN, ply=0)
        PositionContext(
            fen=START_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
            reference=reference,
        )

        with self.assertRaises(ValidationError):
            PositionContext(
                fen=START_FEN,
                recent_moves_uci=[],
                recent_moves_san=[],
                reference=PositionReference(
                    game_id="game-1",
                    fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1",
                ),
            )


class ChessToolResultContractTests(unittest.TestCase):
    def test_analyze_position_replays_candidate_pv(self) -> None:
        result = AnalyzePositionResult(
            fen=START_FEN,
            candidates=[candidate(), d4_candidate()],
            provenance=provenance(),
        )
        self.assertEqual(2, len(result.candidates))

        wrong_san = candidate().model_copy(
            update={"line_san": ["e4", "e6"]}
        )
        with self.assertRaisesRegex(ValidationError, "mismatched SAN"):
            AnalyzePositionResult(
                fen=START_FEN,
                candidates=[wrong_san],
                provenance=provenance(),
            )

        wrong_turn = candidate().model_copy(
            update={"line_uci": ["e2e4", "g1f3"], "line_san": ["e4", "Nf3"]}
        )
        with self.assertRaisesRegex(ValidationError, "illegal move"):
            AnalyzePositionResult(
                fen=START_FEN,
                candidates=[wrong_turn],
                provenance=provenance(),
            )

    def test_analyze_position_allows_zero_candidates_only_when_terminal(self) -> None:
        terminal = AnalyzePositionResult(
            fen=STALEMATE_FEN,
            candidates=[],
            provenance=provenance(),
        )
        self.assertEqual([], terminal.candidates)

        with self.assertRaisesRegex(ValidationError, "non-terminal"):
            AnalyzePositionResult(
                fen=START_FEN,
                candidates=[],
                provenance=provenance(),
            )
        with self.assertRaisesRegex(ValidationError, "terminal positions"):
            AnalyzePositionResult(
                fen=STALEMATE_FEN,
                candidates=[candidate()],
                provenance=provenance(),
            )

    def test_analyze_move_requires_explicit_legal_and_replays_after_move(self) -> None:
        self.assertTrue(analyze_move_result().legal)

        payload = analyze_move_result().model_dump(mode="python")
        payload.pop("legal")
        with self.assertRaises(ValidationError):
            AnalyzeMoveResult.model_validate(payload)

        payload = analyze_move_result().model_dump(mode="python")
        payload["legal"] = False
        with self.assertRaises(ValidationError):
            AnalyzeMoveResult.model_validate(payload)

        payload = analyze_move_result().model_dump(mode="python")
        payload["move"] = {"uci": "e2e5", "san": "e5"}
        with self.assertRaisesRegex(ValidationError, "must be legal"):
            AnalyzeMoveResult.model_validate(payload)

        payload = analyze_move_result().model_dump(mode="python")
        payload["move"] = {"uci": "e2e4", "san": "e3"}
        with self.assertRaisesRegex(ValidationError, "SAN must match"):
            AnalyzeMoveResult.model_validate(payload)

        payload = analyze_move_result().model_dump(mode="python")
        payload["continuation_uci"] = ["d2d4"]
        payload["continuation_san"] = ["d4"]
        with self.assertRaisesRegex(ValidationError, "illegal move"):
            AnalyzeMoveResult.model_validate(payload)

    def test_review_result_matches_reference_and_legal_moves(self) -> None:
        reference = PositionReference(
            game_id="game-1",
            review_side="white",
            critical_id="ply-1",
            ply=1,
            fen=START_FEN,
        )
        position = PositionContext(
            fen=START_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
            reference=reference,
        )
        result = GetReviewContextResult(
            reference=reference,
            position=position,
            played_move=MoveReference(uci="e2e4", san="e4"),
            best_move=MoveReference(uci="d2d4", san="d4"),
            classification="mistake",
            criticality="critical",
            candidates=[d4_candidate()],
            provenance=provenance(),
        )
        self.assertEqual(reference, result.position.reference)

        payload = result.model_dump(mode="python")
        payload["position"]["reference"]["game_id"] = "other-game"
        with self.assertRaisesRegex(ValidationError, "exactly match"):
            GetReviewContextResult.model_validate(payload)

        payload = result.model_dump(mode="python")
        payload["played_move"] = {"uci": "e2e5", "san": "e5"}
        with self.assertRaisesRegex(ValidationError, "must be legal"):
            GetReviewContextResult.model_validate(payload)


class EnvelopeContractTests(unittest.TestCase):
    def test_tool_result_enforces_success_error_exclusivity(self) -> None:
        success = ToolResult[MoveReference](
            ok=True,
            data=MoveReference(uci="e2e4", san="e4"),
            evidence_refs=["analysis:game-1:ply-1"],
        )
        self.assertIsNone(success.error)

        failure = ToolResult[MoveReference](
            ok=False,
            error=ToolError(
                code="illegal_move",
                message="The move is not legal in this position.",
                recoverable=False,
            ),
        )
        self.assertIsNone(failure.data)

        invalid_payloads = [
            {"ok": True},
            {
                "ok": True,
                "data": {"uci": "e2e4", "san": "e4"},
                "error": {
                    "code": "engine_timeout",
                    "message": "timeout",
                    "recoverable": True,
                },
            },
            {"ok": False},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ToolResult[MoveReference].model_validate(payload)

    def test_api_error_envelope_supports_agent_and_session_errors(self) -> None:
        unavailable = AgentErrorResponse(
            error=AgentError(
                code="agent_unavailable",
                message="The Agent model API is not configured.",
                recoverable=True,
            )
        )
        self.assertEqual(unavailable.error.code, "agent_unavailable")

        stale = AgentErrorResponse(
            error=SessionError(
                code="stale_agent_context",
                message="The board context changed during this request.",
                recoverable=True,
            )
        )
        self.assertEqual(stale.error.code, "stale_agent_context")

    def test_contracts_reject_unknown_fields(self) -> None:
        with self.assertRaises(ValidationError):
            MoveReference.model_validate({"uci": "e2e4", "san": "e4", "raw": "secret"})

    def test_tool_call_record_enforces_registry_and_status_contract(self) -> None:
        ok = ToolCallRecord(
            name="get_review_context",
            permission="read",
            status="ok",
            duration_ms=1,
        )
        self.assertIsNone(ok.error_code)

        with self.assertRaisesRegex(ValidationError, "permission"):
            ToolCallRecord(
                name="analyze_move",
                permission="read",
                status="ok",
                duration_ms=1,
            )
        with self.assertRaises(ValidationError):
            ToolCallRecord.model_validate(
                {
                    "name": "delete_game",
                    "permission": "compute",
                    "status": "ok",
                    "duration_ms": 1,
                }
            )
        with self.assertRaisesRegex(ValidationError, "require error_code"):
            ToolCallRecord(
                name="analyze_move",
                permission="compute",
                status="error",
                duration_ms=1,
            )
        with self.assertRaisesRegex(ValidationError, "cannot contain error_code"):
            ToolCallRecord(
                name="analyze_move",
                permission="compute",
                status="ok",
                duration_ms=1,
                error_code="engine_timeout",
            )
        with self.assertRaisesRegex(ValidationError, "budget_exceeded requires"):
            ToolCallRecord(
                name="analyze_move",
                permission="compute",
                status="budget_exceeded",
                duration_ms=1,
                error_code="engine_timeout",
            )

        budget = ToolCallRecord(
            name="analyze_move",
            permission="compute",
            status="budget_exceeded",
            duration_ms=1,
            error_code="tool_budget_exceeded",
        )
        self.assertEqual("tool_budget_exceeded", budget.error_code)

    def test_agent_run_request_allows_only_v1_tool_names(self) -> None:
        with self.assertRaises(ValidationError):
            AgentRunRequest(
                session_id="session-1",
                message="Delete the game",
                model_context=visible_context(),
                allowed_tools=["delete_game"],  # type: ignore[list-item]
                max_turns=4,
                timeout_seconds=120,
            )


class SessionAndApiContractTests(unittest.TestCase):
    def test_session_state_defaults_and_context_ownership(self) -> None:
        state = AgentSessionState(
            session_id="session-1",
            active_game_id="game-1",
            review_side="white",
            active_ply=12,
            created_at="2026-08-21T10:00:00Z",
            updated_at="2026-08-21T10:00:00Z",
        )
        self.assertEqual(state.schema_version, 1)
        self.assertEqual(state.generation, 0)
        self.assertEqual(state.activity, "conversation")

        with self.assertRaises(ValidationError):
            AgentSessionState(
                session_id="session-2",
                review_side="black",
                created_at="2026-08-21T10:00:00Z",
                updated_at="2026-08-21T10:00:00Z",
            )

    def test_session_api_requests_freeze_generation_contract(self) -> None:
        AgentSessionCreateRequest(
            game_id="game-1",
            review_side="white",
            active_ply=33,
            active_critical_id="ply-33",
        )
        context = AgentSessionContextRequest(
            expected_generation=4,
            game_id="game-1",
            review_side="white",
            active_ply=34,
            position=PositionContext(
                fen=START_FEN,
                recent_moves_uci=[],
                recent_moves_san=[],
            ),
        )
        message = AgentMessageRequest(message="If I play Nc6?", expected_generation=4)
        self.assertEqual(context.expected_generation, message.expected_generation)

        with self.assertRaises(ValidationError):
            AgentMessageRequest(message="question", expected_generation=-1)

    def test_session_ids_are_nonempty_and_active_ply_requires_game(self) -> None:
        base = {
            "session_id": "session-1",
            "created_at": "2026-08-21T10:00:00Z",
            "updated_at": "2026-08-21T10:00:00Z",
        }
        for field in ("active_game_id", "active_critical_id", "focus_ref"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AgentSessionState(**base, **{field: " "})

        with self.assertRaisesRegex(ValidationError, "active_ply requires"):
            AgentSessionState(**base, active_ply=3)
        with self.assertRaisesRegex(ValidationError, "active_ply requires"):
            AgentSessionCreateRequest(active_ply=3)

        patch = AgentSessionContextRequest(expected_generation=2, active_ply=3)
        self.assertEqual(3, patch.active_ply)
        for field in ("game_id", "active_critical_id", "focus_ref"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AgentSessionContextRequest(
                    expected_generation=2,
                    **{field: " "},
                )

    def test_message_response_is_structured(self) -> None:
        response = AgentMessageResponse(
            session=AgentSessionSummary(session_id="session-1", generation=5),
            response=AgentResponse(
                text="This loses time.",
                references=[ChessReference(kind="position", fen=START_FEN)],
                evidence_refs=["analysis:game-1:ply-33"],
                suggested_actions=[
                    CompareMoveAction(
                        kind="compare_move",
                        label="Compare Nc6",
                        target={"move_uci": "b8c6"},
                    )
                ],
            ),
            tool_calls=[
                ToolCallRecord(
                    name="analyze_move",
                    permission="compute",
                    status="ok",
                    duration_ms=412,
                )
            ],
        )
        self.assertEqual(response.session.generation, 5)

    def test_agent_response_schema_has_no_open_object_targets(self) -> None:
        schema = AgentResponse.model_json_schema()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                self.assertIsNot(value.get("additionalProperties"), True)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(schema)

    def test_suggested_action_schema_rejects_cross_kind_target_fields(self) -> None:
        with self.assertRaises(ValidationError):
            AgentResponse.model_validate(
                {
                    "text": "Compare e4.",
                    "suggested_actions": [
                        {
                            "kind": "compare_move",
                            "label": "Compare e4",
                            "target": {"game_id": "game-1", "move_uci": "e2e4"},
                        }
                    ],
                }
            )

        schema = AgentResponse.model_json_schema()
        action_items = schema["properties"]["suggested_actions"]["items"]
        self.assertEqual(4, len(action_items["anyOf"]))
        self.assertEqual(
            {"fen", "move_uci"},
            set(schema["$defs"]["CompareMoveActionTarget"]["properties"]),
        )
        self.assertEqual(
            {"position_references", "objective_skill_ids", "source"},
            set(schema["$defs"]["TrainingActionTarget"]["properties"]),
        )

    def test_agent_reference_schema_is_narrowed_by_kind(self) -> None:
        schema = AgentResponse.model_json_schema()
        reference_items = schema["properties"]["references"]["items"]

        self.assertEqual(5, len(reference_items["anyOf"]))
        self.assertEqual(
            {"kind", "game_id", "review_side", "critical_id", "ply", "fen"},
            set(schema["$defs"]["AgentCriticalPositionReference"]["properties"]),
        )
        self.assertEqual(
            {"kind", "skill_id"},
            set(schema["$defs"]["AgentSkillReference"]["properties"]),
        )
        with self.assertRaises(ValidationError):
            AgentResponse.model_validate(
                {
                    "text": "Invalid reference.",
                    "references": [
                        {
                            "kind": "critical_position",
                            "game_id": "game-1",
                            "review_side": "white",
                        }
                    ],
                }
            )


class DocumentedDtoContractTests(unittest.TestCase):
    def test_learning_and_training_dtos_are_instantiable(self) -> None:
        ref = PositionReference(game_id="game-1", review_side="white", critical_id="ply-33")
        chess_ref = ChessReference(
            kind="critical_position",
            game_id="game-1",
            review_side="white",
            critical_id="ply-33",
        )
        SkillDefinition(
            taxonomy_version=1,
            skill_id="calculation.candidate_moves",
            parent_id="calculation",
            label="Candidate moves",
            description="Scan forcing candidate moves.",
            supported_evidence_types=["fact_motif"],
        )
        LearningObservation(
            observation_id="observation-1",
            dedupe_key="game_fact:game-1:white:ply-33:calculation.candidate_moves:failure",
            skill_id="calculation.candidate_moves",
            outcome="failure",
            source_type="game_fact",
            evidence_type="fact_motif",
            game_id="game-1",
            review_side="white",
            critical_id="ply-33",
            severity=14.0,
            evidence_refs=["analysis:game-1:ply-33"],
            occurred_at="2026-08-21T10:00:00Z",
        )
        estimate = SkillEstimate(
            taxonomy_version=1,
            skill_id="calculation.candidate_moves",
            evidence_count=2,
            distinct_games=2,
            distinct_positions=2,
            success_count=0,
            partial_count=0,
            failure_count=2,
            cumulative_loss=28.0,
            recent_failure_count=2,
            last_seen="2026-08-21T10:00:00Z",
            confidence_level="emerging",
            status="weakness",
            examples=[chess_ref],
        )
        legacy_estimate = SkillEstimate.model_validate(
            {
                key: value
                for key, value in estimate.model_dump(mode="python").items()
                if key != "distinct_positions"
            }
        )
        self.assertEqual(legacy_estimate.distinct_games, legacy_estimate.distinct_positions)
        memory = LearningMemoryItem(
            skill_id=estimate.skill_id,
            summary="Two recent failures.",
            status="weakness",
            confidence_level="emerging",
            window="recent",
            evidence_count=2,
            window_games=2,
            examples=[chess_ref],
            evidence_refs=["estimate:calculation.candidate_moves"],
        )
        draft = TrainingDraft(
            title="Candidate move scan",
            objective_skill_ids=[memory.skill_id],
            position_references=[ref],
            rationale="Practice checks, captures, and threats.",
            recommended_count=1,
        )
        self.assertEqual(draft.recommended_count, 1)

        with self.assertRaises(ValidationError):
            SkillEstimate(
                taxonomy_version=1,
                skill_id="calculation.candidate_moves",
                evidence_count=2,
                distinct_games=1,
                distinct_positions=1,
                success_count=0,
                partial_count=0,
                failure_count=1,
                cumulative_loss=1,
                recent_failure_count=1,
                confidence_level="emerging",
                status="watch",
                examples=[],
            )

    def test_profile_context_contains_typed_status_appropriate_evidence(self) -> None:
        weakness = skill_estimate("weakness")
        strength = skill_estimate("strength")
        profile = RelevantProfileContext(
            analyzed_games=4,
            weaknesses=[weakness],
            strengths=[strength],
            training_success_rate=75,
        )
        self.assertEqual(1, weakness.schema_version)
        self.assertEqual("strength", profile.strengths[0].status)

        with self.assertRaisesRegex(ValidationError, "weakness estimates"):
            RelevantProfileContext(
                analyzed_games=4,
                weaknesses=[strength],
            )
        with self.assertRaisesRegex(ValidationError, "strength estimates"):
            RelevantProfileContext(
                analyzed_games=4,
                strengths=[weakness],
            )
        no_examples = weakness.model_copy(update={"examples": []})
        with self.assertRaisesRegex(ValidationError, "require evidence"):
            RelevantProfileContext(
                analyzed_games=4,
                weaknesses=[no_examples],
            )

    def test_player_profile_result_requires_verified_estimate_evidence(self) -> None:
        estimate = skill_estimate("weakness")
        result = GetPlayerProfileResult(
            analyzed_games=2,
            relevant_estimates=[estimate],
        )
        self.assertEqual("game-1", result.relevant_estimates[0].examples[0].game_id)

        without_evidence = estimate.model_copy(
            update={
                "evidence_count": 0,
                "distinct_games": 0,
                "distinct_positions": 0,
                "success_count": 0,
                "failure_count": 0,
                "recent_failure_count": 0,
            }
        )
        with self.assertRaisesRegex(ValidationError, "require evidence"):
            GetPlayerProfileResult(
                analyzed_games=2,
                relevant_estimates=[without_evidence],
            )

        without_examples = estimate.model_copy(update={"examples": []})
        with self.assertRaisesRegex(ValidationError, "example references"):
            GetPlayerProfileResult(
                analyzed_games=2,
                relevant_estimates=[without_examples],
            )

        skill_only = estimate.model_copy(
            update={
                "examples": [
                    ChessReference(
                        kind="skill",
                        skill_id="calculation.candidate_moves",
                    )
                ]
            }
        )
        with self.assertRaisesRegex(ValidationError, "game, critical position"):
            GetPlayerProfileResult(
                analyzed_games=2,
                relevant_estimates=[skill_only],
            )

        with self.assertRaisesRegex(ValidationError, "cannot exceed analyzed_games"):
            GetPlayerProfileResult(
                analyzed_games=1,
                relevant_estimates=[estimate],
            )

    def test_learning_observation_requires_source_ownership_and_utc(self) -> None:
        base: dict[str, Any] = {
            "observation_id": "observation-1",
            "dedupe_key": "game_fact:game-1:white:ply-3:skill:failure",
            "skill_id": "tactics.fork_detection",
            "outcome": "failure",
            "source_type": "game_fact",
            "evidence_type": "fact_motif",
            "game_id": "game-1",
            "review_side": "white",
            "critical_id": "ply-3",
            "evidence_refs": ["analysis:game-1:ply-3"],
            "occurred_at": "2026-08-21T10:00:00+00:00",
        }
        observation = LearningObservation.model_validate(base)
        self.assertEqual("2026-08-21T10:00:00Z", observation.occurred_at)

        invalid_updates = (
            {"review_side": None},
            {"attempt_id": "attempt-1"},
            {"evidence_type": "attempt_outcome"},
            {"occurred_at": "2026-08-21T18:00:00+08:00"},
            {"occurred_at": "2026-08-21T10:00:00"},
        )
        for update in invalid_updates:
            with self.subTest(update=update), self.assertRaises(ValidationError):
                LearningObservation.model_validate({**base, **update})

        puzzle = LearningObservation.model_validate(
            {
                **base,
                "dedupe_key": "attempt:attempt-1:tactics.fork_detection:success",
                "source_type": "puzzle_attempt",
                "evidence_type": "puzzle_theme",
                "game_id": None,
                "review_side": None,
                "critical_id": None,
                "attempt_id": "attempt-1",
                "puzzle_id": "lichess-123",
                "outcome": "success",
            }
        )
        self.assertEqual("lichess-123", puzzle.puzzle_id)

    def test_memory_and_profile_limits_support_bounded_phase3_retrieval(self) -> None:
        self.assertEqual(3, GetPlayerProfileInput().limit)
        self.assertEqual(5, GetPlayerProfileInput(limit=5).limit)
        with self.assertRaises(ValidationError):
            GetPlayerProfileInput(limit=6)

        query = MemoryQuery(
            activity="game_review",
            current_facts={"primary_category": "fork"},
            focus_skill_id="tactics.fork_detection",
            window="lifetime",
            limit=5,
        )
        self.assertEqual("lifetime", query.window)
        with self.assertRaises(ValidationError):
            MemoryQuery(activity="training", limit=6)

        puzzle = ChessReference(kind="puzzle", puzzle_id="lichess-123", fen=START_FEN)
        estimate = skill_estimate().model_copy(
            update={"distinct_games": 0, "examples": [puzzle]}
        )
        GetPlayerProfileResult(analyzed_games=0, relevant_estimates=[estimate])
        memory = LearningMemoryItem(
            skill_id=estimate.skill_id,
            summary="Verified recent evidence.",
            status="watch",
            confidence_level="emerging",
            window="recent",
            evidence_count=2,
            window_games=0,
            examples=[puzzle],
            evidence_refs=["estimate:tactics.fork_detection"],
        )
        self.assertEqual(0, memory.window_games)

        with self.assertRaises(ValidationError):
            ChessReference(kind="puzzle")
        with self.assertRaises(ValidationError):
            ChessReference(kind="game", game_id="game-1", puzzle_id="puzzle-1")


class FakeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_runtime_is_scriptable_and_records_requests(self) -> None:
        expected = run_result()
        runtime = FakeAgentRuntime([expected, TimeoutError("provider timeout")])
        self.assertIsInstance(runtime, AgentRuntime)
        request = AgentRunRequest(
            session_id="session-1",
            message="What should I play?",
            model_context=visible_context(),
            allowed_tools=["analyze_position"],
            max_turns=4,
            timeout_seconds=120,
        )

        actual = await runtime.run(request)
        self.assertEqual(actual, expected)
        self.assertEqual(runtime.requests, [request])
        with self.assertRaises(TimeoutError):
            await runtime.run(request)

    async def test_fake_tools_are_fifo_and_enforce_engine_budget(self) -> None:
        result = ToolResult[AnalyzePositionResult](
            ok=True,
            data=AnalyzePositionResult(
                fen=START_FEN,
                candidates=[candidate()],
                provenance=provenance(),
            ),
            evidence_refs=["engine-cache:position-1"],
        )
        tools = FakeAgentTools(max_total_calls=2, max_engine_calls=1)
        tools.queue("analyze_position", result)
        request = AnalyzePositionInput(fen=START_FEN, purpose="compare_candidates")

        actual = await tools.analyze_position(request)
        self.assertEqual(actual.data, result.data)
        self.assertEqual(tools.total_calls, 1)
        self.assertEqual(tools.engine_calls, 1)
        self.assertEqual(tools.calls[0].request, request)

        tools.queue("analyze_position", result)
        exceeded = await tools.analyze_position(request)
        self.assertFalse(exceeded.ok)
        self.assertEqual("tool_budget_exceeded", exceeded.error.code)
        self.assertTrue(exceeded.error.recoverable)
        self.assertEqual(2, tools.total_calls)

    async def test_fake_tools_revalidate_concrete_result_type(self) -> None:
        tools = FakeAgentTools()
        generic = ToolResult[Any](
            ok=True,
            data={
                "fen": START_FEN,
                "candidates": [],
                "provenance": provenance().model_dump(mode="python"),
            },
        )
        tools.queue("analyze_position", generic)

        with self.assertRaises(ValidationError):
            await tools.analyze_position(
                AnalyzePositionInput(fen=START_FEN, purpose="compare_candidates")
            )

    async def test_fake_tools_reject_request_result_mismatches(self) -> None:
        tools = FakeAgentTools()
        result = ToolResult[AnalyzePositionResult](
            ok=True,
            data=AnalyzePositionResult(
                fen=AFTER_E4_FEN,
                candidates=[
                    CandidateLine(
                        rank=1,
                        move=MoveReference(uci="e7e5", san="e5"),
                        score=EngineScore(kind="cp", value=20, pov="white"),
                        win_percent_for_review_side=52,
                        line_uci=["e7e5"],
                        line_san=["e5"],
                    )
                ],
                provenance=provenance(),
            ),
        )
        tools.queue("analyze_position", result)
        with self.assertRaisesRegex(AssertionError, "FEN does not match"):
            await tools.analyze_position(
                AnalyzePositionInput(fen=START_FEN, purpose="compare_candidates")
            )

        move_tools = FakeAgentTools()
        move_tools.queue(
            "analyze_move",
            ToolResult[AnalyzeMoveResult](ok=True, data=analyze_move_result()),
        )
        with self.assertRaisesRegex(AssertionError, "does not match"):
            await move_tools.analyze_move(
                AnalyzeMoveInput(fen_before=START_FEN, move_uci="d2d4")
            )

    async def test_fake_review_tool_matches_request_reference(self) -> None:
        reference = PositionReference(
            game_id="game-1",
            review_side="white",
            critical_id="ply-1",
            ply=1,
            fen=START_FEN,
        )
        review = GetReviewContextResult(
            reference=reference,
            position=PositionContext(
                fen=START_FEN,
                recent_moves_uci=[],
                recent_moves_san=[],
                reference=reference,
            ),
            played_move=MoveReference(uci="e2e4", san="e4"),
            best_move=MoveReference(uci="d2d4", san="d4"),
            classification="mistake",
            criticality="critical",
            candidates=[d4_candidate()],
        )
        tools = FakeAgentTools()
        tools.queue(
            "get_review_context",
            ToolResult[GetReviewContextResult](ok=True, data=review),
        )
        with self.assertRaisesRegex(AssertionError, "does not match"):
            await tools.get_review_context(
                GetReviewContextInput(
                    game_id="other-game",
                    review_side="white",
                    critical_id="ply-1",
                )
            )


if __name__ == "__main__":
    unittest.main()
