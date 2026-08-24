from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from server.core.agent.models import (
    ChessReference,
    GetPlayerProfileInput,
    LearningMemoryItem,
    MemoryQuery,
    SkillEstimate,
)
from server import config
from server.core import history, puzzles
from server.core.agent.tools import AgentTools
from server.core.learning import memory


def estimate(
    skill_id: str,
    *,
    evidence: int,
    failures: int = 0,
    successes: int = 0,
    partials: int = 0,
    games: int = 2,
    positions: int = 2,
    confidence: str = "emerging",
    status: str = "weakness",
    loss: float = 20.0,
    recent_failures: int | None = None,
    puzzle: bool = False,
) -> SkillEstimate:
    examples = []
    if evidence:
        if puzzle:
            examples = [ChessReference(kind="puzzle", puzzle_id=f"puzzle-{skill_id}")]
        else:
            examples = [
                ChessReference(
                    kind="critical_position",
                    game_id=f"game-{skill_id}",
                    review_side="white",
                    critical_id="ply-7",
                )
            ]
    return SkillEstimate(
        taxonomy_version=1,
        skill_id=skill_id,
        evidence_count=evidence,
        distinct_games=games if evidence else 0,
        distinct_positions=positions if evidence else 0,
        success_count=successes,
        partial_count=partials,
        failure_count=failures,
        cumulative_loss=loss if failures else 0.0,
        recent_failure_count=(failures if recent_failures is None else recent_failures),
        confidence_level=confidence,
        status=status,
        examples=examples,
    )


class RecordingEstimateLoader:
    def __init__(self, estimates: list[SkillEstimate] | Exception) -> None:
        self.estimates = estimates
        self.calls: list[str] = []

    def __call__(self, window: str) -> list[SkillEstimate]:
        self.calls.append(window)
        if isinstance(self.estimates, Exception):
            raise self.estimates
        return list(self.estimates)


class LearningMemoryRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="learning-memory-")
        self.old_personalize = config.PERSONALIZE_HISTORY

    def tearDown(self) -> None:
        config.PERSONALIZE_HISTORY = self.old_personalize
        self.temporary.cleanup()

    def test_exact_focus_is_bounded_and_zero_evidence_is_filtered(self) -> None:
        loader = RecordingEstimateLoader(
            [
                estimate(
                    "tactics.fork_detection",
                    evidence=2,
                    failures=2,
                ),
                estimate(
                    "calculation.candidate_moves",
                    evidence=0,
                    games=0,
                    positions=0,
                    status="unknown",
                    confidence="insufficient",
                ),
            ]
        )
        items = memory.retrieve_memory(
            MemoryQuery(
                activity="game_review",
                focus_category="fork",
                window="lifetime",
                limit=5,
            ),
            data_dir=self.temporary.name,
            personalization_enabled=True,
            estimate_loader=loader,
        )
        self.assertEqual(["tactics.fork_detection"], [item.skill_id for item in items])
        self.assertEqual(["lifetime"], loader.calls)
        self.assertEqual("weakness", items[0].status)
        self.assertIn("2 observations across 2 positions", items[0].summary)
        self.assertEqual(1, len(items[0].evidence_refs))

    def test_current_verified_fact_relevance_precedes_unrelated_confidence(self) -> None:
        fork = estimate(
            "tactics.fork_detection",
            evidence=2,
            failures=2,
            confidence="emerging",
        )
        established = estimate(
            "opening.development",
            evidence=3,
            successes=3,
            failures=0,
            confidence="established",
            status="strength",
        )
        items = memory.retrieve_memory(
            MemoryQuery(
                activity="game_review",
                current_facts={"facts": {"primary_category": "fork"}},
                limit=2,
            ),
            personalization_enabled=True,
            estimate_loader=RecordingEstimateLoader([established, fork]),
        )
        self.assertEqual("tactics.fork_detection", items[0].skill_id)

    def test_invalid_or_conflicting_focus_fails_closed_without_loading(self) -> None:
        loader = RecordingEstimateLoader([])
        unknown = memory.retrieve_memory(
            MemoryQuery(activity="training", focus_category="not-a-skill"),
            personalization_enabled=True,
            estimate_loader=loader,
        )
        conflicting = memory.retrieve_memory(
            MemoryQuery(
                activity="training",
                focus_skill_id="tactics.fork_detection",
                focus_category="missed_capture",
            ),
            personalization_enabled=True,
            estimate_loader=loader,
        )
        self.assertEqual([], unknown)
        self.assertEqual([], conflicting)
        self.assertEqual([], loader.calls)

    def test_disabled_and_unhealthy_personalization_never_read_estimates(self) -> None:
        loader = RecordingEstimateLoader(AssertionError("must not read"))
        self.assertEqual(
            [],
            memory.retrieve_memory(
                MemoryQuery(activity="conversation"),
                personalization_enabled=False,
                estimate_loader=loader,
            ),
        )
        with patch("server.core.learning.memory.is_available", return_value=False):
            self.assertEqual(
                [],
                memory.retrieve_memory(
                    MemoryQuery(activity="conversation"),
                    personalization_enabled=True,
                    estimate_loader=loader,
                ),
            )
        self.assertEqual([], loader.calls)

    def test_puzzle_only_memory_allows_zero_window_games(self) -> None:
        puzzle = estimate(
            "tactics.mating_threat_detection",
            evidence=3,
            failures=2,
            partials=1,
            games=0,
            positions=3,
            confidence="established",
            puzzle=True,
        )
        item = memory.retrieve_memory(
            MemoryQuery(activity="training", limit=1),
            personalization_enabled=True,
            estimate_loader=RecordingEstimateLoader([puzzle]),
        )[0]
        self.assertEqual(0, item.window_games)
        self.assertEqual("puzzle", item.examples[0].kind)
        self.assertEqual(item.summary, memory.retrieve_memory(
            MemoryQuery(activity="training", limit=1),
            personalization_enabled=True,
            estimate_loader=RecordingEstimateLoader([puzzle]),
        )[0].summary)

    def test_disabled_legacy_profile_short_circuits_before_estimate_storage(self) -> None:
        config.PERSONALIZE_HISTORY = False
        with patch(
            "server.core.history.EstimateStore",
            side_effect=AssertionError("must not read"),
        ):
            rows = history._canonical_weakness_rows(
                {"games": 2, "categories": []},
                data_dir=self.temporary.name,
                window="recent",
            )
        self.assertEqual([], rows)

    def test_puzzle_only_weakness_does_not_form_legacy_game_training_row(self) -> None:
        config.PERSONALIZE_HISTORY = True
        puzzle_estimate = estimate(
            "tactics.fork_detection",
            evidence=3,
            failures=3,
            games=0,
            positions=3,
            confidence="established",
            puzzle=True,
        )
        store = unittest.mock.Mock()
        store.ensure_current.return_value = [puzzle_estimate]
        with patch("server.core.history.EstimateStore", return_value=store):
            rows = history._canonical_weakness_rows(
                {"games": 2, "categories": []},
                data_dir=self.temporary.name,
                window="recent",
            )
        self.assertEqual([], rows)

    def test_puzzle_bias_uses_canonical_memory_not_legacy_state(self) -> None:
        config.PERSONALIZE_HISTORY = True
        item = LearningMemoryItem(
            skill_id="tactics.fork_detection",
            summary="Forks are a verified weakness.",
            status="weakness",
            confidence_level="established",
            window="recent",
            evidence_count=3,
            window_games=0,
            examples=[ChessReference(kind="puzzle", puzzle_id="puzzle-fork")],
            evidence_refs=["learning:tactics.fork_detection:example"],
        )
        with patch(
            "server.core.puzzles.learning_memory.retrieve_memory",
            return_value=[item],
        ) as retrieve:
            themes = puzzles.weakness_themes(
                {"by_theme": {"hangingPiece": {"seen": 99, "solved": 0}}}
            )
        self.assertEqual(["fork"], themes)
        retrieve.assert_called_once()

    def test_disabled_puzzle_bias_short_circuits_before_memory_retrieval(self) -> None:
        config.PERSONALIZE_HISTORY = False
        with patch(
            "server.core.puzzles.learning_memory.retrieve_memory",
            side_effect=AssertionError("must not read"),
        ):
            self.assertEqual([], puzzles.weakness_themes({"by_theme": {}}))


class CanonicalAgentProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_returns_canonical_estimate_and_legacy_stats_without_repromotion(self) -> None:
        canonical = estimate(
            "calculation.candidate_moves",
            evidence=3,
            failures=3,
            games=2,
            positions=2,
            confidence="established",
        )
        loader = RecordingEstimateLoader([canonical])
        tools = AgentTools(
            personalization_enabled=True,
            estimate_loader=loader,
            profile_loader=lambda: {
                "games_analyzed": 4,
                "recent": {
                    "games": 4,
                    "avg_accuracy": 72.5,
                    "training": {"total": 4, "solved": 3, "solve_rate": 75.0},
                    "weaknesses": [{"category": "fork"}],
                },
                "lifetime": {"games": 8, "avg_accuracy": 70.0},
            },
        )
        result = await tools.get_player_profile(
            GetPlayerProfileInput(focus_categories=["missed_capture"], limit=5)
        )
        self.assertTrue(result.ok)
        assert result.data is not None
        self.assertEqual([canonical], result.data.relevant_estimates)
        self.assertEqual(4, result.data.analyzed_games)
        self.assertEqual(75.0, result.data.training_success_rate)
        self.assertEqual(3, result.data.relevant_estimates[0].evidence_count)
        self.assertTrue(all(ref.startswith("learning:") for ref in result.evidence_refs))

    async def test_profile_health_failure_is_typed_before_any_profile_read(self) -> None:
        reads = 0

        def profile_loader() -> dict:
            nonlocal reads
            reads += 1
            raise AssertionError("must not read")

        tools = AgentTools(
            personalization_enabled=True,
            profile_loader=profile_loader,
            estimate_loader=RecordingEstimateLoader(AssertionError("must not read")),
        )
        with patch("server.core.learning.memory.is_available", return_value=False):
            result = await tools.get_player_profile(GetPlayerProfileInput())
        self.assertFalse(result.ok)
        self.assertEqual("profile_unavailable", result.error.code)
        self.assertTrue(result.error.recoverable)
        self.assertEqual(0, reads)


if __name__ == "__main__":
    unittest.main()
