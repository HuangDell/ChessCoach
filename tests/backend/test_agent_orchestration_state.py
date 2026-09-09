from __future__ import annotations

import asyncio
import unittest

from server.core.agent.execution_state import (
    ExecutionEvent,
    ResourceSnapshot,
    ToolObservation,
    project,
)
from server.core.agent.routing import (
    FixedCandidateProvider,
    OrchestrationController,
    SequencedCandidateProvider,
)
from tests.evals.orchestration.contracts import load_scripts
from tests.evals.orchestration.fixtures import FixtureExecutor, INPUT_TYPES, load_fixture_catalog
from tests.evals.orchestration.runner import (
    _payload,
    _request,
    _task_variant,
    run_replay_variant,
)


def _initialized() -> ExecutionEvent:
    return ExecutionEvent(
        event_id="event-1",
        sequence=1,
        kind="initialized",
        run_id="run-1",
        session_id="session-1",
        generation=0,
        user_goal="Analyze this position.",
        position_fen="8/8/8/8/8/8/8/K6k w - - 0 1",
        resources=ResourceSnapshot(total_calls_remaining=3, engine_calls_remaining=1),
    )


class ExecutionStateProjectionTests(unittest.TestCase):
    def test_projection_is_pure_idempotent_and_candidate_events_do_not_advance_revision(self) -> None:
        state = project(None, _initialized())
        initial_payload = state.model_dump(mode="json")
        candidate = ExecutionEvent(
            event_id="event-2",
            sequence=2,
            kind="candidates_submitted",
            decision_id="decision-1",
        )

        projected = project(state, candidate)
        replayed = project(projected, candidate)

        self.assertEqual(initial_payload, state.model_dump(mode="json"))
        self.assertEqual(0, projected.revision)
        self.assertEqual(projected, replayed)
        with self.assertRaisesRegex(ValueError, "increasing sequence"):
            project(projected, candidate.model_copy(update={"event_id": "different"}))

    def test_old_generation_result_is_diagnostic_only(self) -> None:
        state = project(None, _initialized())
        state = project(
            state,
            ExecutionEvent(
                event_id="event-2",
                sequence=2,
                kind="call_attempted",
                call_id="call-1",
                tool="analyze_position",
                arguments_fingerprint="a" * 64,
                generation=0,
            ),
        )
        state = project(
            state,
            ExecutionEvent(
                event_id="event-3",
                sequence=3,
                kind="generation_changed",
                generation=1,
            ),
        )
        before_result = state.model_dump(mode="json")
        state = project(
            state,
            ExecutionEvent(
                event_id="event-4",
                sequence=4,
                kind="result_validated",
                call_id="call-1",
                generation=0,
                result_ref="result-1",
                observation=ToolObservation(
                    tool="analyze_position",
                    artifact_kind="position_analysis",
                    result_ref="result-1",
                    evidence_refs=["engine:late"],
                ),
            ),
        )

        self.assertEqual("stale", state.attempts[0].status)
        self.assertEqual([], state.evidence_refs)
        self.assertEqual([], state.completed_artifacts)
        self.assertEqual([], state.observations)
        self.assertEqual([], before_result["evidence_refs"])


class OrchestrationControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_callbacks_compute_one_immutable_snapshot(self) -> None:
        class SlowProvider:
            version = "slow-v1"

            def __init__(self) -> None:
                self.calls = 0

            async def candidates(self, view, authorized):
                del view, authorized
                self.calls += 1
                await asyncio.sleep(0)
                return ["get_player_profile"]

        task, variant = _task_variant("S01-main")
        provider = SlowProvider()
        controller = OrchestrationController(
            _request(task, variant), provider, allow_fallback=False
        )

        enabled = await asyncio.gather(
            *(controller.is_enabled("get_player_profile") for _ in range(20))
        )

        self.assertTrue(all(enabled))
        self.assertEqual(1, provider.calls)
        self.assertEqual(1, controller.snapshots.computation_count)
        self.assertEqual(["decision-1"], controller.state.decision_ids)

    async def test_empty_candidate_fallback_is_used_at_most_once(self) -> None:
        task, variant = _task_variant("S01-main")
        controller = OrchestrationController(
            _request(task, variant),
            SequencedCandidateProvider([[], []]),
            allow_fallback=True,
        )

        self.assertTrue(await controller.is_enabled("get_player_profile"))
        controller.generation_changed(1)
        self.assertFalse(await controller.is_enabled("get_player_profile"))

        fallback_snapshots = [
            item for item in controller.snapshots.snapshots if item.fallback_of is not None
        ]
        self.assertEqual(1, len(fallback_snapshots))
        self.assertEqual([], controller.active_snapshot.candidate_tools)
        self.assertTrue(
            set(fallback_snapshots[0].candidate_tools)
            <= set(fallback_snapshots[0].authorized_tools)
        )

    async def test_authorization_obeys_personalization_and_verified_prerequisites(self) -> None:
        task, variant = _task_variant("S01-main")
        request = _request(task, variant)
        disabled_context = request.model_context.model_copy(
            update={
                "task": request.model_context.task.model_copy(
                    update={"personalization_enabled": False}
                )
            }
        )
        disabled = OrchestrationController(
            request.model_copy(update={"model_context": disabled_context}),
            FixedCandidateProvider(request.allowed_tools),
            allow_fallback=False,
        )
        self.assertFalse(await disabled.is_enabled("get_player_profile"))
        self.assertEqual([], disabled.active_snapshot.authorized_tools)

        task, variant = _task_variant("S04-main")
        script = load_scripts().scripts[variant.script_id]
        controller = OrchestrationController(
            _request(task, variant),
            SequencedCandidateProvider(script.candidate_decisions),
            allow_fallback=False,
        )
        executor = FixtureExecutor(variant.fixture_ids)
        action = script.actions[0]
        name, payload, _ = _payload(action)
        self.assertTrue(await controller.is_enabled(name))
        call_id, error = await controller.before_call(
            name, payload, total_used=1, engine_used=0
        )
        self.assertIsNone(error)
        execution = await executor.execute(name, payload)
        controller.after_call(
            call_id,
            name,
            payload,
            execution.result,
            cache_hit=execution.cache_hit,
            engine_calls=execution.engine_calls,
            total_used=1,
            engine_used=0,
        )
        self.assertFalse(await controller.is_enabled(name))
        self.assertNotIn(name, controller.active_snapshot.authorized_tools)

    async def test_replay_tracks_cache_engine_redundancy_cancellation_and_storage_error(self) -> None:
        cache_hit = await run_replay_variant("R02-hit")
        engine_miss = await run_replay_variant("R02-miss")
        budget_limited = await run_replay_variant("R04-main")
        cancelled = await run_replay_variant("X02-main")
        storage = await run_replay_variant("X04-main")

        self.assertTrue(cache_hit["state"]["attempts"][0]["cache_hit"])
        self.assertEqual(0, cache_hit["actual_engine_calls"])
        self.assertFalse(engine_miss["state"]["attempts"][0]["cache_hit"])
        self.assertEqual(1, engine_miss["actual_engine_calls"])
        self.assertEqual(
            "tool_budget_exceeded",
            budget_limited["state"]["attempts"][1]["error_category"],
        )
        self.assertEqual("aborted", budget_limited["state"]["terminal_status"])
        self.assertEqual("cancelled", cancelled["state"]["terminal_status"])
        self.assertEqual([], cancelled["state"]["observations"])
        self.assertEqual("storage_consistency", storage["state"]["attempts"][0]["error_category"])
        self.assertEqual([], storage["state"]["completed_artifacts"])

        task, variant = _task_variant("R04-main")
        request = _request(task, variant).model_copy(
            update={"max_total_tool_calls": 3, "max_engine_tool_calls": 3}
        )
        controller = OrchestrationController(
            request,
            FixedCandidateProvider(["analyze_position"]),
            allow_fallback=False,
        )
        executor = FixtureExecutor(variant.fixture_ids)
        action = load_scripts().scripts[variant.script_id].actions[0]
        name, payload, _ = _payload(action)
        for used in (1, 2):
            self.assertTrue(await controller.is_enabled(name))
            call_id, error = await controller.before_call(
                name, payload, total_used=used, engine_used=used
            )
            self.assertIsNone(error)
            execution = await executor.execute(name, payload)
            controller.after_call(
                call_id,
                name,
                payload,
                execution.result,
                cache_hit=execution.cache_hit,
                engine_calls=execution.engine_calls,
                total_used=used,
                engine_used=used,
            )
        self.assertTrue(controller.state.attempts[1].redundant)

        task, variant = _task_variant("R04-main")
        request = _request(task, variant).model_copy(
            update={"max_total_tool_calls": 3, "max_engine_tool_calls": 3}
        )
        controller = OrchestrationController(
            request,
            FixedCandidateProvider(["analyze_position"]),
            allow_fallback=False,
        )
        executor = FixtureExecutor(variant.fixture_ids)
        payload = INPUT_TYPES["analyze_position"].model_validate(
            load_fixture_catalog()[0]["analysis_initial"]["request"]
        )
        for count in (1, 2):
            await controller.is_enabled("analyze_position")
            call_id, error = await controller.before_call(
                "analyze_position", payload, total_used=count, engine_used=count - 1
            )
            self.assertIsNone(error)
            execution = await executor.execute("analyze_position", payload)
            controller.after_call(
                call_id,
                "analyze_position",
                payload,
                execution.result,
                cache_hit=execution.cache_hit,
                engine_calls=execution.engine_calls,
                total_used=count,
                engine_used=count,
            )
        self.assertTrue(controller.state.attempts[1].redundant)

    async def test_fixture_invalidation_cannot_become_a_success(self) -> None:
        catalog, _ = load_fixture_catalog()
        fixture = catalog["training_draft_stale"]
        executor = FixtureExecutor(["training_draft_stale"])
        executor.invalidate("training_draft_stale")
        payload = INPUT_TYPES[fixture["tool"]].model_validate(fixture["request"])

        result = await executor.execute(fixture["tool"], payload)

        self.assertFalse(result.result.ok)
        self.assertEqual([], executor.successful_tool_results())
