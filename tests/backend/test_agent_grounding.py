from __future__ import annotations

import unittest

import chess

from server.core.agent.models import (
    AgentResponse,
    ChessReference,
    EngineFactsContext,
    ModelVisibleContext,
    MoveReference,
    OpenPositionAction,
    PositionContext,
    PositionReference,
    TaskContext,
    ToolCallRecord,
)
from server.core.agent.policy import (
    AgentResponseValidationError,
    build_model_input,
    validate_agent_response,
)


EVIDENCE_REF = "review:test:ply-1"


def _reference() -> PositionReference:
    return PositionReference(
        game_id="game-test",
        review_side="white",
        critical_id="ply-1",
        ply=0,
        fen=chess.STARTING_FEN,
    )


def _context(*, with_facts: bool = False) -> ModelVisibleContext:
    reference = _reference()
    return ModelVisibleContext(
        task=TaskContext(activity="game_review", review_side="white"),
        position=PositionContext(
            fen=chess.STARTING_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
            reference=reference,
        ),
        engine_facts=(
            EngineFactsContext(
                reference=reference,
                best_move=MoveReference(uci="e2e4", san="e4"),
                classification="mistake",
                facts={"score_pov": "white"},
                evidence_refs=[EVIDENCE_REF],
            )
            if with_facts
            else None
        ),
        relevant_profile=None,
        relevant_memory=[],
        conversation_summary="",
        allowed_evidence_refs=[EVIDENCE_REF] if with_facts else [],
    )


def _chess_reference() -> ChessReference:
    return ChessReference(kind="critical_position", **_reference().model_dump())


class AgentGroundingContractTests(unittest.TestCase):
    def test_prompt_separates_generic_skills_from_personal_evidence(self) -> None:
        context = _context().model_copy(
            update={
                "task": TaskContext(
                    activity="conversation",
                    personalization_enabled=True,
                ),
                "position": None,
            }
        )

        model_input = build_model_input(context)

        self.assertIn("Never emit it for a generic chess concept", model_input)
        self.assertIn("training objective", model_input)
        self.assertIn("never prove a user weakness/status/distinct_games", model_input)

    def test_open_position_accepts_the_owned_checkpoint_identity_without_facts(self) -> None:
        reference = _reference()
        response = AgentResponse(
            text="Open the current position.",
            suggested_actions=[
                OpenPositionAction(
                    kind="open_position",
                    label="Open position",
                    target=reference.model_dump(mode="python", exclude_none=True),
                )
            ],
        )

        self.assertEqual(response, validate_agent_response(response, _context(), []))

    def test_open_position_accepts_an_exact_reference_from_a_successful_tool(self) -> None:
        historical = ChessReference(
            kind="critical_position",
            game_id="historical-game",
            review_side="white",
            critical_id="ply-12",
            ply=12,
            fen=chess.STARTING_FEN,
        )
        response = AgentResponse(
            text="Open the retrieved example.",
            suggested_actions=[
                OpenPositionAction(
                    kind="open_position",
                    label="Open example",
                    target={
                        key: value
                        for key, value in historical.model_dump(
                            mode="python", exclude_none=True
                        ).items()
                        if key in {"game_id", "review_side", "critical_id", "ply", "fen"}
                    },
                )
            ],
        )

        self.assertEqual(
            response,
            validate_agent_response(
                response,
                _context(),
                [],
                validated_tool_references=[historical],
            ),
        )

    def test_move_claim_legality_is_derived_from_exact_fen(self) -> None:
        response = AgentResponse(
            text="e4 is illegal.",
            grounding={
                "claims": {"move_uci": "e2e4", "legal": False},
                "move_claims": [
                    {
                        "position": {"fen": chess.STARTING_FEN},
                        "move_uci": "e2e4",
                        "legal": False,
                    }
                ],
            },
        )
        with self.assertRaisesRegex(AgentResponseValidationError, "legality does not match"):
            validate_agent_response(response, _context(), [])

        direct_claim = AgentResponse(
            text="e4 is legal.",
            grounding={"claims": {"move_uci": "e2e4", "legal": True}},
        )
        self.assertEqual(
            direct_claim,
            validate_agent_response(direct_claim, _context(), []),
        )

    def test_tool_owned_position_matches_across_reference_kinds(self) -> None:
        board = chess.Board()
        board.push_uci("e2e4")
        owned = ChessReference(
            kind="critical_position",
            game_id="historical-game",
            review_side="white",
            critical_id="ply-12",
            ply=12,
            fen=board.fen(),
        )
        response = AgentResponse(
            text="This is the retrieved position.",
            references=[{"kind": "position", "fen": board.fen()}],
        )

        self.assertEqual(
            response,
            validate_agent_response(
                response,
                _context(),
                [],
                validated_tool_references=[owned],
            ),
        )

    def test_engine_claims_must_match_authoritative_facts(self) -> None:
        context = _context(with_facts=True)
        response = AgentResponse(
            text="This was a mistake; e4 was best. Scores are from White's point of view.",
            references=[_chess_reference()],
            evidence_refs=[EVIDENCE_REF],
            grounding={
                "claims": {
                    "classification": "mistake",
                    "best_move_uci": "e2e4",
                    "score_pov": "white",
                }
            },
        )
        self.assertEqual(response, validate_agent_response(response, context, []))

        changed = response.model_copy(deep=True)
        changed.grounding.claims.classification = "blunder"
        with self.assertRaisesRegex(
            AgentResponseValidationError, "classification is not present"
        ):
            validate_agent_response(changed, context, [])

    def test_side_to_move_and_tool_degradation_are_checked_against_run_state(self) -> None:
        context = _context()
        wrong_side = AgentResponse(
            text="Black to move.",
            references=[_chess_reference()],
            grounding={"claims": {"side_to_move": "black"}},
        )
        with self.assertRaisesRegex(AgentResponseValidationError, "current FEN"):
            validate_agent_response(wrong_side, context, [])

        illegal_call = ToolCallRecord(
            name="analyze_move",
            permission="compute",
            status="error",
            duration_ms=1,
            error_code="illegal_move",
        )
        unannotated = AgentResponse(text="That move is illegal.")
        with self.assertRaisesRegex(AgentResponseValidationError, "degradation"):
            validate_agent_response(unannotated, context, [illegal_call])

        timeout_call = ToolCallRecord(
            name="analyze_position",
            permission="compute",
            status="error",
            duration_ms=1,
            error_code="engine_timeout",
        )
        missing_uncertainty = AgentResponse(
            text="The fresh analysis timed out.",
            grounding={
                "completion": "partial",
                "degradation": "recoverable_tool_failure",
                "error_code": "engine_timeout",
                "acknowledges_uncertainty": False,
            },
        )
        with self.assertRaisesRegex(AgentResponseValidationError, "uncertainty"):
            validate_agent_response(missing_uncertainty, context, [timeout_call])


if __name__ == "__main__":
    unittest.main()
