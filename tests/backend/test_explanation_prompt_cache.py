from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from server import config
from server.core.agent.models import ChessReference, LearningMemoryItem
from server.core.explanation.builder import build_request
from tests.backend.fixtures import analysis_artifact


class ExplanationPromptCacheTests(unittest.TestCase):
    def test_position_variables_are_confined_to_terminal_context_in_both_languages(self) -> None:
        analysis_one = analysis_artifact()
        analysis_two = deepcopy(analysis_one)
        first = analysis_one["critical_positions"][0]
        second = analysis_two["critical_positions"][0]
        second["critical_id"] = "dynamic-critical-marker"
        second["played_move"]["uci"] = "dynamic-played-uci-marker"
        second["played_move"]["san"] = "PlayedMarkerSAN"
        second["candidates"][0]["move"]["uci"] = "dynamic-best-uci-marker"
        second["candidates"][0]["move"]["san"] = "BestMarkerSAN"
        second["classification"] = "dynamic-classification-marker"
        second["played_line"]["san"] = ["PlayedLineMarker", "ReplyMarker"]
        second["best_line"]["san"] = ["BestLineMarker", "AnswerMarker"]
        second["facts"]["primary_category"] = "dynamic-category-marker"
        second["facts"]["secondary_categories"] = ["dynamic-secondary-marker"]
        memory = LearningMemoryItem(
            skill_id="calculation.candidate_moves",
            summary="dynamic-memory-marker",
            status="weakness",
            confidence_level="established",
            window="recent",
            evidence_count=2,
            window_games=2,
            examples=[
                ChessReference(
                    kind="critical_position",
                    game_id="dynamic-memory-game",
                    review_side="white",
                    critical_id="dynamic-memory-position",
                )
            ],
            evidence_refs=["learning:dynamic-evidence-marker"],
        )

        old_language = config.EXPLANATION_LANGUAGE
        old_personalize = config.PERSONALIZE_HISTORY
        try:
            config.PERSONALIZE_HISTORY = True
            for language in ("en", "zh-CN"):
                with self.subTest(language=language), patch(
                    "server.core.explanation.builder.learning_memory.is_available",
                    return_value=True,
                ), patch(
                    "server.core.explanation.builder.learning_memory.retrieve_memory",
                    side_effect=[[], [memory]],
                ):
                    config.EXPLANATION_LANGUAGE = language
                    request_one = build_request(analysis_one, first)
                    request_two = build_request(analysis_two, second)

                marker = "position_context:\n"
                prefix_one, context_one = request_one.user_prompt.split(marker, 1)
                prefix_two, context_two = request_two.user_prompt.split(marker, 1)
                self.assertEqual(3, request_one.prompt_version)
                self.assertEqual(request_one.system_prompt, request_two.system_prompt)
                self.assertEqual(prefix_one, prefix_two)
                for dynamic in (
                    "dynamic-critical-marker",
                    "dynamic-played-uci-marker",
                    "dynamic-best-uci-marker",
                    "PlayedMarkerSAN",
                    "BestMarkerSAN",
                    "dynamic-classification-marker",
                    "dynamic-category-marker",
                    "dynamic-secondary-marker",
                    "dynamic-memory-marker",
                    "learning:dynamic-evidence-marker",
                ):
                    self.assertNotIn(dynamic, request_two.system_prompt)
                    self.assertNotIn(dynamic, prefix_two)
                    self.assertIn(dynamic, context_two)
                parsed = json.loads(context_two)
                self.assertEqual(
                    {"allowed_evidence_refs", "engine", "expected"}, set(parsed)
                )
                self.assertEqual(request_two.expected, parsed["expected"])
                self.assertEqual(request_two.payload, parsed["engine"])
        finally:
            config.EXPLANATION_LANGUAGE = old_language
            config.PERSONALIZE_HISTORY = old_personalize


if __name__ == "__main__":
    unittest.main()
