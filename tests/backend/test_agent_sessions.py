from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from server.core.agent.models import (
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    PositionContext,
    PositionReference,
)
from server.core.agent.sessions import (
    ChessSessionCheckpointStore,
    GenerationGuardedSession,
    InMemoryConversationSessionFactory,
    InvalidSessionContextError,
    SessionBusyError,
    SessionMessageGate,
    SessionMutationCoordinator,
    SessionNotFoundError,
    SessionStoreError,
    StaleAgentContextError,
)


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
SESSION_ID = "1" * 32


class CheckpointStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.store = ChessSessionCheckpointStore(
            self._temp.name,
            clock=lambda: "2026-08-22T00:00:00+00:00",
            id_factory=lambda: SESSION_ID,
        )

    def test_create_get_and_path_safe_backend_id(self) -> None:
        created = self.store.create(
            AgentSessionCreateRequest(
                game_id="game-1",
                review_side="white",
                active_ply=4,
                active_critical_id="ply-5",
            )
        )

        self.assertEqual(created.session_id, SESSION_ID)
        self.assertEqual(created.generation, 0)
        self.assertEqual(created.schema_version, 1)
        self.assertEqual(self.store.get(SESSION_ID), created)
        self.assertTrue((self.store.sessions_dir / f"{SESSION_ID}.json").is_file())
        with self.assertRaises(SessionNotFoundError):
            self.store.get("../settings")
        self.assertFalse((Path(self._temp.name) / "settings.json").exists())

    def test_context_patch_uses_fields_set_and_generation_cas(self) -> None:
        self.store.create(AgentSessionCreateRequest(game_id="game-1", review_side="white"))
        position = PositionContext(
            fen=START_FEN,
            recent_moves_uci=[],
            recent_moves_san=[],
            reference=PositionReference(fen=START_FEN),
        )
        updated = self.store.update_context(
            SESSION_ID,
            AgentSessionContextRequest(
                expected_generation=0,
                active_ply=0,
                activity="position_analysis",
                focus_ref="move:e2e4",
                position=position,
            ),
        )

        self.assertEqual(updated.active_game_id, "game-1")
        self.assertEqual(updated.review_side, "white")
        self.assertEqual(updated.active_ply, 0)
        self.assertEqual(updated.position, position)
        self.assertEqual(updated.generation, 1)
        with self.assertRaises(StaleAgentContextError):
            self.store.update_context(
                SESSION_ID,
                AgentSessionContextRequest(expected_generation=0, active_ply=2),
            )
        self.assertEqual(self.store.get(SESSION_ID), updated)

    def test_switching_or_clearing_game_drops_owned_context(self) -> None:
        self.store.create(
            AgentSessionCreateRequest(
                game_id="game-1",
                review_side="black",
                active_ply=8,
                active_critical_id="ply-9",
            )
        )
        switched = self.store.update_context(
            SESSION_ID,
            AgentSessionContextRequest(expected_generation=0, game_id="game-2"),
        )
        self.assertEqual(switched.active_game_id, "game-2")
        self.assertIsNone(switched.review_side)
        self.assertIsNone(switched.active_ply)
        self.assertIsNone(switched.active_critical_id)

        cleared = self.store.update_context(
            SESSION_ID,
            AgentSessionContextRequest(expected_generation=1, game_id=None),
        )
        self.assertIsNone(cleared.active_game_id)

    def test_invalid_patch_and_corrupt_checkpoint_fail_closed(self) -> None:
        self.store.create(AgentSessionCreateRequest(game_id="game-1"))
        with self.assertRaises(InvalidSessionContextError):
            self.store.update_context(
                SESSION_ID,
                AgentSessionContextRequest(expected_generation=0, activity=None),
            )

        path = self.store.sessions_dir / f"{SESSION_ID}.json"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(SessionStoreError):
            self.store.get(SESSION_ID)

    def test_replace_failure_preserves_previous_checkpoint(self) -> None:
        original = self.store.create(AgentSessionCreateRequest(game_id="game-1"))
        path = self.store.sessions_dir / f"{SESSION_ID}.json"
        before = path.read_text(encoding="utf-8")
        with mock.patch("server.core.agent.sessions.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(SessionStoreError):
                self.store.update_context(
                    SESSION_ID,
                    AgentSessionContextRequest(expected_generation=0, active_ply=1),
                )
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(self.store.get(SESSION_ID), original)
        self.assertEqual(list(self.store.sessions_dir.glob("*.tmp")), [])

    def test_delete_removes_only_the_validated_checkpoint(self) -> None:
        created = self.store.create(AgentSessionCreateRequest())
        self.assertEqual(self.store.delete(SESSION_ID), created)
        with self.assertRaises(SessionNotFoundError):
            self.store.get(SESSION_ID)


class AgentSessionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.store = ChessSessionCheckpointStore(
            self._temp.name,
            id_factory=lambda: SESSION_ID,
        )
        self.store.create(AgentSessionCreateRequest())
        self.coordinator = SessionMutationCoordinator()

    async def test_compare_and_set_allows_only_one_concurrent_update(self) -> None:
        async def update(ply: int):
            return await self.coordinator.update_context(
                self.store,
                SESSION_ID,
                AgentSessionContextRequest(expected_generation=0, game_id="game-1", active_ply=ply),
            )

        results = await asyncio.gather(update(1), update(2), return_exceptions=True)
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        self.assertEqual(sum(isinstance(item, StaleAgentContextError) for item in results), 1)
        self.assertEqual(self.store.get(SESSION_ID).generation, 1)

    async def test_message_gate_fails_busy_without_waiting_and_releases(self) -> None:
        gate = SessionMessageGate()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first() -> None:
            async with gate.hold(SESSION_ID):
                entered.set()
                await release.wait()

        task = asyncio.create_task(first())
        await entered.wait()
        with self.assertRaises(SessionBusyError):
            async with gate.hold(SESSION_ID):
                self.fail("busy session must not be entered")
        release.set()
        await task
        async with gate.hold(SESSION_ID):
            pass

    async def test_staged_add_is_bounded_and_commits_once(self) -> None:
        factory = InMemoryConversationSessionFactory()
        backing = factory.get_session(SESSION_ID)
        await backing.add_items([{"index": index} for index in range(20)])
        guarded = GenerationGuardedSession(
            backing,
            self.store,
            self.coordinator,
            expected_generation=0,
        )

        self.assertEqual(
            [item["index"] for item in await guarded.get_items()],
            list(range(8, 20)),
        )
        await guarded.add_items([{"role": "user", "content": "why?"}])
        self.assertEqual(len(await backing.get_items()), 20)
        self.assertEqual(len(guarded.staged_items), 1)
        await guarded.commit()
        self.assertEqual(len(await backing.get_items()), 21)
        with self.assertRaises(RuntimeError):
            await guarded.add_items([{"late": True}])

    async def test_stale_cancel_and_discard_never_touch_backing(self) -> None:
        factory = InMemoryConversationSessionFactory()
        backing = factory.get_session(SESSION_ID)
        guarded = GenerationGuardedSession(
            backing,
            self.store,
            self.coordinator,
            expected_generation=0,
        )
        await guarded.add_items([{"role": "assistant", "content": "candidate"}])
        await self.coordinator.update_context(
            self.store,
            SESSION_ID,
            AgentSessionContextRequest(expected_generation=0, activity="game_review"),
        )
        with self.assertRaises(StaleAgentContextError):
            await guarded.commit()
        self.assertEqual(await backing.get_items(), [])

        cancelled = GenerationGuardedSession(
            backing,
            self.store,
            self.coordinator,
            expected_generation=1,
        )
        await cancelled.add_items([{"content": "discard me"}])
        cancelled.discard()
        self.assertEqual(await backing.get_items(), [])

    async def test_staged_pop_and_clear_are_virtual_until_commit(self) -> None:
        factory = InMemoryConversationSessionFactory()
        backing = factory.get_session(SESSION_ID)
        await backing.add_items([{"id": 1}, {"id": 2}])

        popped = GenerationGuardedSession(
            backing,
            self.store,
            self.coordinator,
            expected_generation=0,
        )
        self.assertEqual(await popped.pop_item(), {"id": 2})
        await popped.add_items([{"id": 3}])
        self.assertEqual(await backing.get_items(), [{"id": 1}, {"id": 2}])
        await popped.commit()
        self.assertEqual(await backing.get_items(), [{"id": 1}, {"id": 3}])

        cleared = GenerationGuardedSession(
            backing,
            self.store,
            self.coordinator,
            expected_generation=0,
        )
        await cleared.clear_session()
        await cleared.add_items([{"id": 4}])
        self.assertEqual(await backing.get_items(), [{"id": 1}, {"id": 3}])
        await cleared.commit()
        self.assertEqual(await backing.get_items(), [{"id": 4}])

    async def test_cancellation_during_backing_add_rolls_back_completed_write(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class SlowBacking:
            session_id = SESSION_ID
            session_settings = None

            def __init__(self) -> None:
                self.items: list[object] = []

            async def get_items(self, limit=None):
                return list(self.items)

            async def add_items(self, items):
                entered.set()
                await release.wait()
                self.items.extend(items)

            async def pop_item(self):
                return self.items.pop() if self.items else None

            async def clear_session(self):
                self.items.clear()

        backing = SlowBacking()
        guarded = GenerationGuardedSession(
            backing,
            self.store,
            self.coordinator,
            expected_generation=0,
        )
        await guarded.add_items([{"role": "assistant", "content": "cancelled"}])
        commit = asyncio.create_task(guarded.commit())
        await entered.wait()
        commit.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await commit
        self.assertEqual([], backing.items)


if __name__ == "__main__":
    unittest.main()
