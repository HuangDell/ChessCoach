from __future__ import annotations

import copy
import tempfile
import unittest

import chess

from server import config
from server.core.agent.context import ChessContextBuilder, ChessContextError
from server.core.agent.models import (
    AgentSessionState,
    AnalyzePositionResult,
    CandidateLine,
    EngineProvenance,
    EngineScore,
    MoveReference,
    PositionContext,
    PositionReference,
    ToolResult,
)
from server.core.agent.tools import ActiveReviewArtifact, AgentTools
from server.core.importers.pgn import ImportedGame
from server.core.storage import games
from tests.backend.fixtures import CRITICAL_ID, GAME_ID, TACTICAL_FEN, analysis_artifact


START_FEN = chess.Board().fen()


def session_state(**changes: object) -> AgentSessionState:
    values: dict[str, object] = {
        "session_id": "a" * 32,
        "active_game_id": GAME_ID,
        "review_side": "white",
        "active_ply": 0,
        "active_critical_id": CRITICAL_ID,
        "created_at": "2026-08-22T10:00:00Z",
        "updated_at": "2026-08-22T10:00:00Z",
    }
    values.update(changes)
    return AgentSessionState.model_validate(values)


class AgentContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="agent-context-")
        self._old_data_dir = config.DATA_DIR
        config.DATA_DIR = self._temporary.name
        self.artifact = analysis_artifact()
        games.store_analysis(GAME_ID, "white", self.artifact)
        self.builder = ChessContextBuilder()

    def tearDown(self) -> None:
        config.DATA_DIR = self._old_data_dir
        self._temporary.cleanup()

    def test_critical_context_uses_canonical_fen_ply_and_owned_reference(self) -> None:
        supplied = PositionContext(
            fen=TACTICAL_FEN,
            recent_moves_uci=["e2e4"],
            recent_moves_san=["e4"],
            reference=PositionReference(game_id=GAME_ID, ply=0, fen=TACTICAL_FEN),
        )

        bundle = self.builder.resolve(session_state(position=supplied))

        position = bundle.context.position
        assert position is not None
        self.assertEqual(chess.Board(TACTICAL_FEN).fen(), position.fen)
        self.assertEqual([], position.recent_moves_uci)
        self.assertEqual([], position.recent_moves_san)
        self.assertEqual(GAME_ID, position.reference.game_id)
        self.assertEqual("white", position.reference.review_side)
        self.assertEqual(CRITICAL_ID, position.reference.critical_id)
        self.assertEqual(1, position.reference.ply)
        self.assertEqual(position.fen, position.reference.fen)
        self.assertEqual(GAME_ID, bundle.analysis["game_id"])
        self.assertEqual(CRITICAL_ID, bundle.critical["critical_id"])

    def test_selected_move_is_legally_verified_and_merged_into_canonical_context(self) -> None:
        supplied = PositionContext(
            fen=TACTICAL_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
            selected_move_uci="h1h2",
            selected_move_san="Rh2",
            reference=PositionReference(fen=TACTICAL_FEN),
        )

        position = self.builder.resolve(session_state(position=supplied)).context.position

        assert position is not None
        self.assertEqual("h1h2", position.selected_move_uci)
        self.assertEqual("Rh2", position.selected_move_san)
        self.assertEqual(GAME_ID, position.reference.game_id)

        invalid = supplied.model_copy(update={"selected_move_san": "Qh2"})
        with self.assertRaisesRegex(ChessContextError, "mismatched SAN"):
            self.builder.resolve(session_state(position=invalid))

    def test_game_ply_is_replayed_from_artifact_and_recent_moves_are_bounded(self) -> None:
        board = chess.Board()
        ucis = [
            "e2e4",
            "e7e5",
            "g1f3",
            "b8c6",
            "f1b5",
            "a7a6",
            "b5a4",
            "g8f6",
            "e1g1",
            "f8e7",
        ]
        moves: list[dict[str, object]] = []
        sans: list[str] = []
        for ply, uci in enumerate(ucis, start=1):
            move = chess.Move.from_uci(uci)
            fen_before = board.fen()
            san = board.san(move)
            sans.append(san)
            board.push(move)
            moves.append(
                {
                    "ply": ply,
                    "fen_before": fen_before,
                    "fen_after": board.fen(),
                    "played_move": {"uci": uci, "san": san},
                }
            )
        artifact = analysis_artifact()
        artifact["moves"] = moves
        artifact["critical_positions"] = []
        games.store_analysis(GAME_ID, "white", artifact)

        bundle = self.builder.resolve(
            session_state(active_ply=len(moves), active_critical_id=None, position=None)
        )

        position = bundle.context.position
        assert position is not None
        self.assertEqual(board.fen(), position.fen)
        self.assertEqual(ucis[-8:], position.recent_moves_uci)
        self.assertEqual(sans[-8:], position.recent_moves_san)
        self.assertEqual(10, position.reference.ply)
        self.assertEqual(board.fen(), position.reference.fen)

    def test_imported_game_without_analysis_still_builds_canonical_position(self) -> None:
        game_id = "1" * 20
        board = chess.Board()
        move = chess.Move.from_uci("e2e4")
        fen_before = board.fen()
        san = board.san(move)
        board.push(move)
        games.store_game(
            ImportedGame(
                game_id=game_id,
                source_type="pgn_text",
                source_url=None,
                pgn="[Result \"*\"]\n\n1. e4 *\n",
                original_pgn="[Result \"*\"]\n\n1. e4 *\n",
                headers={"Result": "*"},
                ply_count=1,
                review_side="white",
                moves=[
                    {
                        "ply": 1,
                        "fen_before": fen_before,
                        "fen_after": board.fen(),
                        "uci": move.uci(),
                        "san": san,
                    }
                ],
            )
        )

        bundle = self.builder.resolve(
            session_state(
                active_game_id=game_id,
                active_ply=1,
                active_critical_id=None,
                position=None,
            )
        )

        self.assertIsNone(bundle.analysis)
        self.assertIsNone(bundle.context.review)
        self.assertEqual(board.fen(), bundle.context.position.fen)
        self.assertEqual(["e2e4"], bundle.context.position.recent_moves_uci)

    def test_conflicting_supplied_position_and_critical_ply_are_rejected(self) -> None:
        with self.assertRaisesRegex(ChessContextError, "does not match") as mismatch:
            self.builder.resolve(
                session_state(
                    position=PositionContext(
                        fen=START_FEN,
                        recent_moves_uci=[],
                        recent_moves_san=[],
                        reference=PositionReference(game_id=GAME_ID, ply=0, fen=START_FEN),
                    )
                )
            )
        self.assertEqual("invalid_session_context", mismatch.exception.code)

        with self.assertRaisesRegex(ChessContextError, "Active ply") as wrong_ply:
            self.builder.resolve(session_state(active_ply=1))
        self.assertEqual("invalid_session_context", wrong_ply.exception.code)

    def test_corrupt_mainline_move_is_rejected_before_it_reaches_model_context(self) -> None:
        corrupted = copy.deepcopy(self.artifact)
        corrupted["moves"][0]["played_move"]["san"] = "Qxd3"
        games.store_analysis(GAME_ID, "white", corrupted)

        with self.assertRaisesRegex(ChessContextError, "illegal or mismatched") as raised:
            self.builder.resolve(session_state())

        self.assertEqual("invalid_session_context", raised.exception.code)

    async def test_model_context_contains_only_bounded_grounded_review_facts(self) -> None:
        bundle = self.builder.resolve(session_state())
        tools = AgentTools(
            active_review=ActiveReviewArtifact.from_analysis(self.artifact, CRITICAL_ID),
            line_plies=1,
        )

        context = await self.builder.build_model_context(
            bundle,
            "Why was Rh2 a blunder?",
            review_loader=tools.get_review_context,
        )

        self.assertEqual("Why was Rh2 a blunder?", context.task.user_goal)
        self.assertEqual("white", context.task.review_side)
        self.assertIsNone(context.relevant_profile)
        self.assertEqual([], context.relevant_memory)
        self.assertEqual("", context.conversation_summary)
        self.assertEqual(
            [f"review:{GAME_ID}:white:{CRITICAL_ID}"],
            context.allowed_evidence_refs,
        )
        facts = context.engine_facts
        assert facts is not None
        self.assertEqual(3, len(facts.candidates))
        self.assertTrue(all(len(candidate.line_uci) == 1 for candidate in facts.candidates))
        self.assertEqual(context.allowed_evidence_refs, facts.evidence_refs)
        self.assertEqual(CRITICAL_ID, facts.reference.critical_id)
        self.assertEqual(1, facts.reference.ply)
        self.assertLessEqual(len(facts.facts.get("motifs", [])), 5)
        self.assertLessEqual(len(facts.facts.get("classification_evidence", [])), 8)
        self.assertNotIn("candidates", facts.facts)

    async def test_free_analysis_reuses_verified_live_candidates(self) -> None:
        analysis_ref = "a" * 64
        position = PositionContext(
            fen=START_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
            live_analysis_ref=analysis_ref,
            reference=PositionReference(fen=START_FEN),
        )
        state = session_state(
            active_game_id=None,
            review_side=None,
            active_ply=None,
            active_critical_id=None,
            activity="position_analysis",
            position=position,
        )
        candidate = CandidateLine(
            rank=1,
            move=MoveReference(uci="e2e4", san="e4"),
            score=EngineScore(kind="cp", value=25, pov="white"),
            win_percent_for_review_side=None,
            line_uci=["e2e4", "e7e5"],
            line_san=["e4", "e5"],
        )
        result = AnalyzePositionResult(
            fen=START_FEN,
            candidates=[candidate],
            provenance=EngineProvenance(
                engine_name="Stockfish",
                engine_version="Stockfish fixture",
                depth=22,
                multipv=3,
                analysis_profile_id="live",
                cache_key=analysis_ref,
            ),
        )

        context = await self.builder.build_model_context(
            self.builder.resolve(state),
            "What should I do?",
            live_analysis_loader=lambda fen, ref: ToolResult(
                ok=fen == START_FEN and ref == analysis_ref,
                data=result,
                evidence_refs=["engine-position:fixture"],
            ),
        )

        assert context.engine_facts is not None
        self.assertIsNone(context.position.live_analysis_ref)
        self.assertEqual("e2e4", context.engine_facts.best_move.uci)
        self.assertEqual("live_best_moves", context.engine_facts.facts["source"])
        self.assertEqual(["engine-position:fixture"], context.allowed_evidence_refs)


if __name__ == "__main__":
    unittest.main()
