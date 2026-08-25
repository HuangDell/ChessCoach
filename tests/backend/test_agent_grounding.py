from __future__ import annotations

import unittest

import chess

from server.core.agent.models import (
    AgentResponse,
    ChessReference,
    EngineFactsContext,
    ModelVisibleContext,
    MoveReference,
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

        self.assertIn("Never emit either for a generic chess concept", model_input)
        self.assertIn("include no skill reference", model_input)

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


if __name__ == "__main__":
    unittest.main()
