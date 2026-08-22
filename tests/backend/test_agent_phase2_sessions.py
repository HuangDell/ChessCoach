from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import chess

from server import config
from server.core.agent.context import ChessContextBuilder
from server.core.agent.models import (
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    AgentSessionState,
    PositionContext,
    PositionReference,
)
from server.core.agent.sessions import (
    ChessSessionCheckpointStore,
    GenerationGuardedSession,
    InMemoryConversationSessionFactory,
    SessionMutationCoordinator,
    SessionStoreError,
    StaleAgentContextError,
)
from server.core.agent.summary import ConversationSummaryBuilder
from tests.backend.fixtures import CRITICAL_ID, GAME_ID, TACTICAL_FEN, store_analysis_fixture


SESSION_ID = "2" * 32
START_FEN = chess.Board().fen()


def _state(**changes: object) -> AgentSessionState:
    values: dict[str, object] = {
        "session_id": SESSION_ID,
        "created_at": "2026-08-22T00:00:00Z",
        "updated_at": "2026-08-22T00:00:00Z",
    }
    values.update(changes)
    return AgentSessionState.model_validate(values)


class Phase2ConversationSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="agent-phase2-session-")
        self.addCleanup(self._temporary.cleanup)
        self._old_data_dir = config.DATA_DIR
        config.DATA_DIR = self._temporary.name
        self.addAsyncCleanup(self._restore_data_dir)
        store_analysis_fixture(self._temporary.name)
        self.store = ChessSessionCheckpointStore(
            self._temporary.name,
            id_factory=lambda: SESSION_ID,
            clock=lambda: "2026-08-22T01:00:00Z",
        )
        self.store.create(
            AgentSessionCreateRequest(
                game_id=GAME_ID,
                review_side="white",
                active_ply=0,
                active_critical_id=CRITICAL_ID,
            )
        )
        self.builder = ChessContextBuilder()

    async def _restore_data_dir(self) -> None:
        config.DATA_DIR = self._old_data_dir

    def _validate_reference(self, reference: PositionReference) -> PositionReference:
        return self.builder.canonicalize_reference(
            reference,
            session=self.store.get(SESSION_ID),
        )

    async def test_recent_history_is_hard_limited_to_twelve_items(self) -> None:
        factory = InMemoryConversationSessionFactory()
        backing = factory.get_session(SESSION_ID)
        await backing.add_items([{"index": index} for index in range(20)])
        guarded = GenerationGuardedSession(
            backing,
            self.store,
            SessionMutationCoordinator(),
            expected_generation=0,
        )

        self.assertEqual(list(range(8, 20)), [item["index"] for item in await guarded.get_items()])
        self.assertEqual(list(range(15, 20)), [item["index"] for item in await guarded.get_items(5)])
        self.assertEqual(list(range(8, 20)), [item["index"] for item in await guarded.get_items(100)])

    async def test_summary_extracts_readable_text_from_sdk_structured_output(self) -> None:
        summary = ConversationSummaryBuilder().summarize(
            [
                {"role": "user", "content": "How should I calculate here?"},
                {
                    "role": "assistant",
                    "content": [{
                        "type": "output_text",
                        "text": (
                            '{"text":"Check forcing replies before choosing a move.",'
                            '"references":[{"fen":"' + START_FEN + '"}]}'
                        ),
                    }],
                },
            ]
        )

        self.assertIn("Check forcing replies before choosing a move.", summary)
        self.assertNotIn('"references"', summary)

    async def test_summary_update_is_guarded_atomic_and_filters_invalid_references(self) -> None:
        valid = PositionReference(
            game_id=GAME_ID,
            review_side="white",
            critical_id=CRITICAL_ID,
        )
        expired = PositionReference(game_id="f" * 20, review_side="white", ply=0)

        updated = await SessionMutationCoordinator().update_conversation_summary(
            self.store,
            SESSION_ID,
            expected_generation=0,
            summary="  Focus on checking forcing replies.  ",
            references=[expired, valid, valid],
            reference_validator=self._validate_reference,
        )

        self.assertEqual(0, updated.generation)
        self.assertEqual("Focus on checking forcing replies.", updated.conversation_summary)
        self.assertEqual([], updated.discussed_positions)
        self.assertEqual(1, len(updated.conversation_summary_references))
        canonical = updated.conversation_summary_references[0]
        self.assertEqual(CRITICAL_ID, canonical.critical_id)
        self.assertEqual(1, canonical.ply)
        self.assertEqual(TACTICAL_FEN, canonical.fen)

        with self.assertRaises(StaleAgentContextError):
            self.store.update_conversation_summary(
                SESSION_ID,
                expected_generation=1,
                summary="must not replace",
                references=[],
                reference_validator=self._validate_reference,
            )
        self.assertEqual("Focus on checking forcing replies.", self.store.get(SESSION_ID).conversation_summary)

    async def test_failed_atomic_replace_keeps_previous_summary_and_references(self) -> None:
        valid = PositionReference(
            game_id=GAME_ID,
            review_side="white",
            critical_id=CRITICAL_ID,
        )
        before = self.store.update_conversation_summary(
            SESSION_ID,
            expected_generation=0,
            summary="Previous summary",
            references=[valid],
            reference_validator=self._validate_reference,
        )
        checkpoint_path = self.store.sessions_dir / f"{SESSION_ID}.json"
        original_content = checkpoint_path.read_text(encoding="utf-8")

        with mock.patch("server.core.agent.sessions.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(SessionStoreError):
                self.store.update_conversation_summary(
                    SESSION_ID,
                    expected_generation=0,
                    summary="New summary",
                    references=[],
                    reference_validator=self._validate_reference,
                )

        self.assertEqual(original_content, checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(before, self.store.get(SESSION_ID))
        self.assertEqual([], list(Path(self.store.sessions_dir).glob("*.tmp")))

    async def test_context_change_wins_against_concurrent_summary_update(self) -> None:
        coordinator = SessionMutationCoordinator()

        async def change_context() -> object:
            return await coordinator.update_context(
                self.store,
                SESSION_ID,
                AgentSessionContextRequest(expected_generation=0, activity="game_review"),
            )

        async def update_summary() -> object:
            await asyncio.sleep(0)
            return await coordinator.update_conversation_summary(
                self.store,
                SESSION_ID,
                expected_generation=0,
                summary="stale summary",
                references=[],
                reference_validator=self._validate_reference,
            )

        results = await asyncio.gather(change_context(), update_summary(), return_exceptions=True)

        self.assertEqual(1, sum(isinstance(item, StaleAgentContextError) for item in results))
        self.assertEqual(1, self.store.get(SESSION_ID).generation)
        self.assertEqual("", self.store.get(SESSION_ID).conversation_summary)

    async def test_identical_context_sync_is_a_generation_noop(self) -> None:
        before = self.store.get(SESSION_ID)

        repeated = self.store.update_context(
            SESSION_ID,
            AgentSessionContextRequest(
                expected_generation=0,
                game_id=GAME_ID,
                review_side="white",
                active_ply=0,
                active_critical_id=CRITICAL_ID,
            ),
            validator=lambda state: self.builder.resolve(state),
        )

        self.assertEqual(before, repeated)
        self.assertEqual(0, repeated.generation)

    async def test_record_discussed_positions_appends_deduplicates_and_is_guarded(self) -> None:
        coordinator = SessionMutationCoordinator()
        standalone = PositionReference(fen=START_FEN)
        critical = PositionReference(
            game_id=GAME_ID,
            review_side="white",
            critical_id=CRITICAL_ID,
        )
        first = await coordinator.record_discussed_positions(
            self.store,
            SESSION_ID,
            expected_generation=0,
            references=[standalone, critical],
            reference_validator=self._validate_reference,
        )
        self.assertEqual([START_FEN, TACTICAL_FEN], [ref.fen for ref in first.discussed_positions])

        promoted = self.store.record_discussed_positions(
            SESSION_ID,
            expected_generation=0,
            references=[standalone],
            reference_validator=self._validate_reference,
        )
        self.assertEqual([TACTICAL_FEN, START_FEN], [ref.fen for ref in promoted.discussed_positions])
        self.assertEqual(0, promoted.generation)
        with self.assertRaises(StaleAgentContextError):
            self.store.record_discussed_positions(
                SESSION_ID,
                expected_generation=1,
                references=[critical],
                reference_validator=self._validate_reference,
            )


class Phase2FollowUpResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="agent-phase2-reference-")
        self.addCleanup(self._temporary.cleanup)
        self._old_data_dir = config.DATA_DIR
        config.DATA_DIR = self._temporary.name
        self.addCleanup(self._restore_data_dir)
        store_analysis_fixture(self._temporary.name)
        self.builder = ChessContextBuilder()
        self.current = _state(
            active_game_id=GAME_ID,
            review_side="white",
            active_ply=0,
            active_critical_id=CRITICAL_ID,
        )
        self.saved_critical = PositionReference(
            game_id=GAME_ID,
            review_side="white",
            critical_id=CRITICAL_ID,
        )

    def _restore_data_dir(self) -> None:
        config.DATA_DIR = self._old_data_dir

    def test_explicit_reference_precedes_current_checkpoint(self) -> None:
        explicit = PositionReference(fen=START_FEN)

        result = self.builder.resolve_follow_up_reference(
            self.current,
            explicit_references=[explicit],
        )

        self.assertEqual("resolved", result.status)
        self.assertEqual("explicit", result.source)
        self.assertEqual(START_FEN, result.reference.fen)
        self.assertIsNone(result.reference.game_id)

    def test_current_checkpoint_precedes_discussed_recent_and_summary(self) -> None:
        old = PositionReference(fen=START_FEN)
        state = self.current.model_copy(update={"discussed_positions": [old]})

        result = self.builder.resolve_follow_up_reference(
            state,
            recent_turn_references=[old],
            summary_references=[old],
        )

        self.assertEqual("checkpoint", result.source)
        self.assertEqual(CRITICAL_ID, result.reference.critical_id)

    def test_discussed_then_recent_turn_then_summary_fallback_order(self) -> None:
        no_current = _state(discussed_positions=[self.saved_critical])
        discussed = self.builder.resolve_follow_up_reference(
            no_current,
            recent_turn_references=[PositionReference(fen=START_FEN)],
        )
        self.assertEqual("discussed", discussed.source)
        self.assertEqual(CRITICAL_ID, discussed.reference.critical_id)

        recent = self.builder.resolve_follow_up_reference(
            _state(),
            recent_turn_references=[PositionReference(fen=START_FEN)],
            summary_references=[self.saved_critical],
        )
        self.assertEqual("recent_turn", recent.source)
        self.assertEqual(START_FEN, recent.reference.fen)

        summary = self.builder.resolve_follow_up_reference(
            _state(conversation_summary_references=[self.saved_critical]),
        )
        self.assertEqual("summary", summary.source)
        self.assertEqual(CRITICAL_ID, summary.reference.critical_id)

    def test_equal_priority_ambiguity_is_explicit_and_does_not_guess(self) -> None:
        result = self.builder.resolve_follow_up_reference(
            _state(),
            recent_turn_references=[
                PositionReference(fen=START_FEN),
                PositionReference(fen=TACTICAL_FEN),
            ],
        )

        self.assertEqual("ambiguous", result.status)
        self.assertEqual("recent_turn", result.source)
        self.assertIsNone(result.reference)
        self.assertEqual(2, len(result.candidates))
        self.assertIn("clarification", result.message)

    def test_expired_explicit_reference_does_not_fall_back_to_checkpoint(self) -> None:
        expired = PositionReference(game_id="f" * 20, review_side="white", ply=0)

        result = self.builder.resolve_follow_up_reference(
            self.current,
            explicit_references=[expired],
        )

        self.assertEqual("unresolved", result.status)
        self.assertEqual("explicit", result.source)
        self.assertIsNone(result.reference)

    def test_game_owned_exploration_is_trusted_only_from_validated_checkpoint_or_discussion(self) -> None:
        board = chess.Board(TACTICAL_FEN)
        move = chess.Move.from_uci("d1d3")
        san = board.san(move)
        board.push(move)
        explored_fen = board.fen()
        exploration = PositionContext(
            fen=explored_fen,
            recent_moves_uci=[],
            recent_moves_san=[],
            exploration_moves_uci=[move.uci()],
            exploration_moves_san=[san],
            reference=PositionReference(
                game_id=GAME_ID,
                review_side="white",
                critical_id=CRITICAL_ID,
                fen=TACTICAL_FEN,
            ),
        )
        current = self.current.model_copy(update={"position": exploration})

        resolved = self.builder.resolve_follow_up_reference(current)

        self.assertEqual("checkpoint", resolved.source)
        explored_reference = resolved.reference
        self.assertEqual(explored_fen, explored_reference.fen)
        self.assertEqual(GAME_ID, explored_reference.game_id)
        self.assertIsNone(explored_reference.ply)
        self.assertIsNone(explored_reference.critical_id)

        discussed = _state(
            active_game_id=GAME_ID,
            review_side="white",
            discussed_positions=[explored_reference],
        )
        restored = self.builder.resolve_follow_up_reference(discussed)
        self.assertEqual("discussed", restored.source)
        self.assertEqual(explored_fen, restored.reference.fen)

        summary_only = _state(
            active_game_id=GAME_ID,
            review_side="white",
            conversation_summary_references=[explored_reference],
        )
        rejected = self.builder.resolve_follow_up_reference(summary_only)
        self.assertEqual("unresolved", rejected.status)
        self.assertIsNone(rejected.reference)

    def test_summary_is_visible_but_never_used_to_reconstruct_position_truth(self) -> None:
        state = _state(
            conversation_summary="The old chat called a move winning at +9.",
        )
        bundle = self.builder.resolve(state)

        async def exercise() -> object:
            return await self.builder.build_model_context(bundle, "What now?")

        context = asyncio.run(exercise())
        self.assertEqual(state.conversation_summary, context.conversation_summary)
        self.assertIsNone(context.position)
        self.assertIsNone(context.engine_facts)


if __name__ == "__main__":
    unittest.main()
