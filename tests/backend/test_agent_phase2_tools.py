from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

import chess
from pydantic import ValidationError

from server.core.agent.models import (
    AgentResponse,
    ChessReference,
    GetPlayerProfileInput,
    LookupOpeningInput,
    ModelVisibleContext,
    PositionContext,
    SuggestedAction,
    TaskContext,
)
from server.core.agent.policy import AgentResponseValidationError, validate_agent_response
from server.core.agent.prioritization import build_review_prioritization
from server.core.agent.service import _successful_profile_references
from server.core.agent.tools import AgentTools

from tests.backend.fixtures import analysis_artifact


class RecordingOpeningClassifier:
    def __init__(
        self,
        result: tuple[str | None, str | None] | Exception,
    ) -> None:
        self.result = result
        self.calls: list[list[str]] = []

    def __call__(self, fens: list[str]) -> tuple[str | None, str | None]:
        self.calls.append(list(fens))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RecordingProfileLoader:
    def __init__(self, result: dict | Exception) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def profile_fixture() -> dict:
    missed_examples = [
        {
            "game_id": "game-1",
            "reviewed_side": "white",
            "critical_id": "ply-11",
            "ply": 11,
            "date": "2026-08-20",
        },
        {
            "game_id": "game-2",
            "reviewed_side": "black",
            "critical_id": "ply-20",
            "ply": 20,
            "date": "2026-08-21",
        },
    ]
    recent = {
        "games": 4,
        "avg_accuracy": 72.5,
        "categories": [
            {
                "category": "hanging_piece",
                "count": 1,
                "game_count": 1,
                "cumulative_win_loss": 8.0,
                "examples": [
                    {
                        "game_id": "game-3",
                        "reviewed_side": "white",
                        "critical_id": "ply-9",
                        "ply": 9,
                    }
                ],
            },
            {
                "category": "missed_capture",
                "count": 3,
                "game_count": 2,
                "cumulative_win_loss": 33.0,
                "examples": missed_examples,
            },
        ],
        "weaknesses": [{"category": "missed_capture"}],
        "training": {"total": 4, "solved": 3, "solve_rate": 75.0},
    }
    return {
        "games_analyzed": 4,
        "recent": recent,
        "lifetime": {
            "games": 4,
            "avg_accuracy": 71.0,
            "training": {"total": 5, "solved": 3, "solve_rate": 60.0},
        },
    }


class OpeningToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_uses_only_injected_local_boundary(self) -> None:
        classifier = RecordingOpeningClassifier(("B20", "Sicilian Defense"))
        tools = AgentTools(opening_classifier=classifier)
        request = LookupOpeningInput(recent_moves_uci=["e2e4", "c7c5", "g1f3"])

        execution = await tools.execute("lookup_opening", request)

        self.assertTrue(execution.result.ok)
        self.assertEqual(0, execution.engine_calls)
        self.assertTrue(execution.cache_hit)
        self.assertEqual(1, len(classifier.calls))
        self.assertEqual(3, len(classifier.calls[0]))
        board = chess.Board()
        for uci in request.recent_moves_uci:
            board.push_uci(uci)
        self.assertEqual(board.fen(), classifier.calls[0][-1])
        data = execution.result.data
        assert data is not None
        self.assertEqual("B20", data.eco)
        self.assertEqual("Sicilian Defense", data.name)
        self.assertEqual("recognized", data.classification)
        self.assertEqual("local_eco", data.metadata["source"])

    async def test_illegal_recent_sequence_and_book_failure_degrade_to_typed_errors(self) -> None:
        classifier = RecordingOpeningClassifier((None, None))
        tools = AgentTools(opening_classifier=classifier)

        illegal = await tools.lookup_opening(
            LookupOpeningInput(recent_moves_uci=["e2e4", "e2e3"])
        )

        self.assertFalse(illegal.ok)
        self.assertEqual("illegal_move", illegal.error.code)
        self.assertEqual([], classifier.calls)

        failing = RecordingOpeningClassifier(OSError("missing local book"))
        failed = await AgentTools(opening_classifier=failing).lookup_opening(
            LookupOpeningInput(fen=chess.STARTING_FEN)
        )
        self.assertFalse(failed.ok)
        self.assertEqual("position_not_found", failed.error.code)
        self.assertTrue(failed.error.recoverable)

    async def test_unknown_local_opening_is_a_success_without_invented_theory(self) -> None:
        classifier = RecordingOpeningClassifier((None, None))
        result = await AgentTools(opening_classifier=classifier).lookup_opening(
            LookupOpeningInput(fen=chess.STARTING_FEN)
        )

        self.assertTrue(result.ok)
        self.assertEqual([], result.evidence_refs)
        self.assertEqual("unrecognized", result.data.classification)
        self.assertIsNone(result.data.eco)
        self.assertIsNone(result.data.name)

    async def test_current_fen_uses_validated_full_history_beyond_recent_move_window(self) -> None:
        board = chess.Board()
        fens = [board.fen()]
        for uci in (
            "e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6",
            "b5a4", "g8f6", "e1g1", "f8e7", "f1e1", "b7b5",
        ):
            board.push_uci(uci)
            fens.append(board.fen())

        result = await AgentTools(opening_history_fens=fens).lookup_opening(
            LookupOpeningInput(fen=board.fen())
        )

        self.assertTrue(result.ok)
        self.assertEqual("recognized", result.data.classification)
        self.assertEqual("C84", result.data.eco)
        self.assertEqual("Ruy Lopez: Closed", result.data.name)
        self.assertEqual("validated_game_history", result.data.metadata["lookup"])


class PlayerProfileToolTests(unittest.IsolatedAsyncioTestCase):
    def test_recurrence_helper_is_profile_gated_and_degrades_to_empty(self) -> None:
        disabled_loader = RecordingProfileLoader(profile_fixture())
        disabled = AgentTools(
            profile_loader=disabled_loader,
            personalization_enabled=False,
        ).review_recurrence_evidence()
        self.assertEqual({}, disabled)
        self.assertEqual(0, disabled_loader.calls)

        enabled_loader = RecordingProfileLoader(profile_fixture())
        enabled = AgentTools(
            profile_loader=enabled_loader,
            personalization_enabled=True,
        ).review_recurrence_evidence()
        self.assertEqual(1, enabled_loader.calls)
        self.assertEqual(3, enabled["missed_capture"]["count"])
        self.assertEqual(2, len(enabled["missed_capture"]["evidence_refs"]))
        self.assertNotIn("hanging_piece", enabled)

        failed_loader = RecordingProfileLoader(RuntimeError("unavailable"))
        failed = AgentTools(
            profile_loader=failed_loader,
            personalization_enabled=True,
        ).review_recurrence_evidence()
        self.assertEqual({}, failed)
        self.assertEqual(1, failed_loader.calls)

    async def test_default_loader_uses_local_identity_not_legacy_review_session(self) -> None:
        with patch(
            "server.core.agent.tools.history.my_player_id", return_value="local-player"
        ) as identity, patch(
            "server.core.agent.tools.history.get_profile", return_value=profile_fixture()
        ) as get_profile, patch(
            "server.core.agent.tools.config.DATA_DIR", "/tmp/agent-profile-test"
        ):
            result = await AgentTools(personalization_enabled=True).get_player_profile(
                GetPlayerProfileInput(focus_categories=["missed_capture"], limit=1)
            )

        self.assertTrue(result.ok)
        identity.assert_called_once_with("/tmp/agent-profile-test")
        get_profile.assert_called_once_with("local-player", "/tmp/agent-profile-test")

    async def test_disabled_personalization_never_reads_profile(self) -> None:
        loader = RecordingProfileLoader(profile_fixture())
        tools = AgentTools(profile_loader=loader, personalization_enabled=False)

        result = await tools.get_player_profile(GetPlayerProfileInput(limit=3))

        self.assertEqual(0, loader.calls)
        self.assertFalse(result.ok)
        self.assertEqual("profile_unavailable", result.error.code)
        self.assertFalse(result.error.recoverable)

    async def test_profile_is_focused_bounded_and_game_backed(self) -> None:
        loader = RecordingProfileLoader(profile_fixture())
        tools = AgentTools(profile_loader=loader, personalization_enabled=True)

        result = await tools.get_player_profile(
            GetPlayerProfileInput(focus_categories=["missed_capture"], limit=1)
        )

        self.assertTrue(result.ok)
        self.assertEqual(1, loader.calls)
        data = result.data
        assert data is not None
        self.assertEqual(4, data.analyzed_games)
        self.assertEqual(1, len(data.relevant_estimates))
        estimate = data.relevant_estimates[0]
        self.assertEqual("missed_capture", estimate.skill_id)
        self.assertEqual("weakness", estimate.status)
        self.assertEqual(3, estimate.evidence_count)
        self.assertEqual(2, estimate.distinct_games)
        self.assertEqual({"game-1", "game-2"}, {item.game_id for item in estimate.examples})
        self.assertEqual(75.0, data.training_success_rate)
        self.assertEqual(4, data.recent["training_attempts"])
        self.assertEqual(60.0, data.lifetime["training_solve_rate"])
        self.assertEqual(2, len(result.evidence_refs))
        self.assertTrue(all(item.startswith("profile:game-") for item in result.evidence_refs))

        allowed = _successful_profile_references(tools)
        historical = estimate.examples[1]
        context = ModelVisibleContext(
            task=TaskContext(activity="game_review", review_side="white"),
            position=PositionContext(
                fen=chess.STARTING_FEN,
                recent_moves_uci=[],
                recent_moves_san=[],
                reference={"game_id": "current-game", "ply": 0, "fen": chess.STARTING_FEN},
            ),
            engine_facts=None,
            relevant_profile=None,
            relevant_memory=[],
            conversation_summary="",
            allowed_evidence_refs=[],
        )
        response = AgentResponse(text="This is a typical example.", references=[historical])
        with self.assertRaises(AgentResponseValidationError):
            validate_agent_response(response, context, [])
        self.assertEqual(
            response,
            validate_agent_response(
                response,
                context,
                [],
                validated_tool_references=allowed,
            ),
        )

    async def test_single_game_category_stays_watch_and_loader_error_is_typed(self) -> None:
        result = await AgentTools(
            profile_loader=RecordingProfileLoader(profile_fixture()),
            personalization_enabled=True,
        ).get_player_profile(
            GetPlayerProfileInput(focus_categories=["hanging_piece"], limit=3)
        )
        self.assertTrue(result.ok)
        self.assertEqual("watch", result.data.relevant_estimates[0].status)
        self.assertEqual("insufficient", result.data.relevant_estimates[0].confidence_level)

        failed = await AgentTools(
            profile_loader=RecordingProfileLoader(OSError("broken profile")),
            personalization_enabled=True,
        ).get_player_profile(GetPlayerProfileInput())
        self.assertFalse(failed.ok)
        self.assertEqual("profile_unavailable", failed.error.code)
        self.assertTrue(failed.error.recoverable)

    def test_profile_limit_cannot_exceed_three(self) -> None:
        with self.assertRaises(ValidationError):
            GetPlayerProfileInput(limit=4)


def priority_artifact(count: int = 10) -> dict:
    artifact = analysis_artifact()
    base = artifact["critical_positions"][0]
    positions = []
    for index in range(count):
        position = copy.deepcopy(base)
        position["critical_id"] = f"ply-{index + 1}"
        position["ply"] = index + 1
        position["priority"] = index + 1
        position["critical_score"] = float(count - index)
        position["win_loss"] = float(index + 1)
        position["classification"] = "blunder" if index == count - 1 else "mistake"
        position["criticality"] = "only_move" if index % 2 else "critical"
        category = "fork" if index == 0 else f"category_{index}"
        position["facts"]["primary_category"] = category
        position["facts"]["classification_evidence"] = ["motifs.test"]
        positions.append(position)
    artifact["critical_positions"] = positions
    return artifact


class ReviewPrioritizationTests(unittest.TestCase):
    def test_shortlist_is_bounded_owned_and_never_hides_largest_error(self) -> None:
        artifact = priority_artifact()
        training_key = f"{artifact['game_id']}:white:ply-1"

        first = build_review_prioritization(
            artifact,
            recurrence_evidence={
                "fork": {"count": 4, "evidence_refs": ["profile:fork:evidence"]}
            },
            focus_categories=["fork"],
            training_references={training_key},
            limit=3,
        )
        second = build_review_prioritization(
            artifact,
            recurrence_evidence={
                "fork": {"count": 4, "evidence_refs": ["profile:fork:evidence"]}
            },
            focus_categories=["fork"],
            training_references={training_key},
            limit=3,
        )

        self.assertEqual(first, second)
        self.assertEqual(3, len(first.candidates))
        self.assertEqual("ply-10", first.candidates[0].reference.critical_id)
        self.assertTrue(first.candidates[0].largest_error)
        self.assertEqual(10.0, first.candidates[0].severity)
        self.assertEqual("blunder", first.candidates[0].classification)
        self.assertEqual("ply-1", first.candidates[1].reference.critical_id)
        self.assertEqual(1.0, first.candidates[1].user_goal_relevance)
        self.assertEqual(4, first.candidates[1].recurrence_evidence)
        self.assertTrue(first.candidates[1].training_available)
        self.assertIn("profile:fork:evidence", first.candidates[1].evidence_refs)
        source_ids = {item["critical_id"] for item in artifact["critical_positions"]}
        self.assertTrue(
            {item.reference.critical_id for item in first.candidates}.issubset(source_ids)
        )

    def test_default_shortlist_caps_at_eight_and_compares_fact_completeness(self) -> None:
        artifact = priority_artifact()
        artifact["critical_positions"][4]["facts"] = {}

        result = build_review_prioritization(artifact)

        self.assertEqual(8, len(result.candidates))
        self.assertTrue(any(item.largest_error for item in result.candidates))
        complete = next(item for item in result.candidates if item.reference.critical_id == "ply-8")
        incomplete = next(
            (
                item
                for item in result.candidates
                if item.reference.critical_id == "ply-5"
            ),
            None,
        )
        self.assertGreater(complete.fact_confidence, 0.0)
        if incomplete is not None:
            self.assertEqual(0.0, incomplete.fact_confidence)

    def test_clean_short_game_does_not_invent_candidates_to_reach_three(self) -> None:
        artifact = priority_artifact(count=2)

        result = build_review_prioritization(artifact)

        self.assertEqual(2, len(result.candidates))
        self.assertEqual(
            {"ply-1", "ply-2"},
            {item.reference.critical_id for item in result.candidates},
        )

    def test_response_cannot_select_a_current_position_outside_shortlist(self) -> None:
        artifact = priority_artifact()
        priorities = build_review_prioritization(artifact, limit=3)
        shortlist_ids = {
            candidate.reference.critical_id for candidate in priorities.candidates
        }
        outside_raw = next(
            item
            for item in artifact["critical_positions"]
            if item["critical_id"] not in shortlist_ids
        )
        outside = ChessReference(
            kind="critical_position",
            game_id=artifact["game_id"],
            review_side="white",
            critical_id=outside_raw["critical_id"],
            ply=outside_raw["ply"],
            fen=outside_raw["fen_before"],
        )
        largest_position = next(
            candidate.reference for candidate in priorities.candidates if candidate.largest_error
        )
        largest = ChessReference(
            kind="critical_position",
            **largest_position.model_dump(mode="python"),
        )
        outside_raw_reference = {
            "game_id": outside.game_id,
            "review_side": outside.review_side,
            "critical_id": outside.critical_id,
            "ply": outside.ply,
            "fen": outside.fen,
        }
        context = ModelVisibleContext(
            task=TaskContext(activity="game_review", review_side="white"),
            position=PositionContext(
                fen=outside.fen,
                recent_moves_uci=[],
                recent_moves_san=[],
                reference=outside_raw_reference,
            ),
            engine_facts=None,
            relevant_profile=None,
            relevant_memory=[],
            conversation_summary="",
            review_priorities=priorities,
            allowed_evidence_refs=[],
        )
        with self.assertRaises(AgentResponseValidationError):
            validate_agent_response(
                AgentResponse(text="Review these.", references=[largest, outside]),
                context,
                [],
            )
        with self.assertRaises(AgentResponseValidationError):
            validate_agent_response(
                AgentResponse(
                    text="Open the omitted position.",
                    references=[largest],
                    suggested_actions=[
                        SuggestedAction(
                            kind="open_position",
                            label="Open",
                            target={
                                "fen": outside_raw_reference["fen"],
                                "ply": outside_raw_reference["ply"],
                            },
                        )
                    ],
                ),
                context,
                [],
            )

    def test_priority_response_can_cite_exact_profile_tool_history(self) -> None:
        priorities = build_review_prioritization(priority_artifact(), limit=3)
        largest_position = next(
            candidate.reference for candidate in priorities.candidates if candidate.largest_error
        )
        largest = ChessReference(
            kind="critical_position",
            **largest_position.model_dump(mode="python"),
        )
        historical = ChessReference(
            kind="critical_position",
            game_id="historical-game",
            review_side="white",
            critical_id="historical-critical",
            ply=7,
            fen=chess.STARTING_FEN,
        )
        context = ModelVisibleContext(
            task=TaskContext(activity="game_review", review_side="white"),
            position=None,
            engine_facts=None,
            relevant_profile=None,
            relevant_memory=[],
            conversation_summary="",
            review_priorities=priorities,
            allowed_evidence_refs=[],
        )
        response = AgentResponse(
            text="Review the largest error; this older example shows the recurring category.",
            references=[largest, historical],
        )

        self.assertEqual(
            response,
            validate_agent_response(
                response,
                context,
                [],
                validated_tool_references=[historical],
            ),
        )


if __name__ == "__main__":
    unittest.main()
