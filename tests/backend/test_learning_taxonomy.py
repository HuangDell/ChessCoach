from __future__ import annotations

import unittest

from server.core.agent.models import SkillDefinition
from server.core.learning.taxonomy import (
    ALIAS_INDEX,
    ATTEMPT_OUTCOME,
    CLOCK_OUTCOME,
    FACT_COMPOSITE,
    FACT_MOTIF,
    MIGRATION_REGISTRY,
    PUZZLE_THEME,
    SKILL_DEFINITIONS,
    TAXONOMY_VERSION,
    AmbiguousSkillAliasError,
    TaxonomyVersionError,
    build_alias_index,
    get_skill_definition,
    map_analysis_position,
    map_attempt_category,
    map_composite_evidence,
    map_fact_evidence,
    map_lichess_themes,
    migrate_skill_id,
    resolve_skill_id,
    supports_evidence_type,
)


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def skill_ids(items: object) -> list[str]:
    return [item.skill_id for item in items]  # type: ignore[union-attr]


def critical_analysis(*, result: str = "0-1", with_clock: bool = False) -> dict:
    analysis = {
        "review_side": "white",
        "result": result,
        "profile": {"critical": {"thresholds": [5.0, 10.0, 20.0]}},
        "headers": {"TimeControl": "600+5" if with_clock else "-"},
        "moves": [],
    }
    if with_clock:
        analysis["moves"] = [{"ply": 1, "clock_seconds": 570.0}]
    return analysis


class TaxonomyDefinitionTests(unittest.TestCase):
    def test_v1_contains_exactly_the_twelve_stable_skills(self) -> None:
        expected = {
            "tactics.fork_detection",
            "tactics.mating_threat_detection",
            "tactics.loose_piece_awareness",
            "calculation.opponent_forcing_moves",
            "calculation.exchange_sequence",
            "calculation.candidate_moves",
            "strategy.king_safety",
            "strategy.piece_activity",
            "opening.development",
            "endgame.conversion",
            "practical.blunder_check",
            "practical.time_management",
        }
        self.assertEqual(1, TAXONOMY_VERSION)
        self.assertEqual(expected, {item.skill_id for item in SKILL_DEFINITIONS})
        self.assertGreaterEqual(len(ALIAS_INDEX), len(SKILL_DEFINITIONS))
        self.assertIn(1, MIGRATION_REGISTRY)
        for definition in SKILL_DEFINITIONS:
            with self.subTest(skill_id=definition.skill_id):
                self.assertEqual(1, definition.taxonomy_version)
                self.assertIsNotNone(definition.parent_id)
                self.assertIs(definition, get_skill_definition(definition.skill_id))
                self.assertTrue(definition.supported_evidence_types)

    def test_alias_resolution_and_migration_fail_closed(self) -> None:
        aliases = {
            "fork": "tactics.fork_detection",
            "hangingPiece": "tactics.loose_piece_awareness",
            "missed_opponent_threat": "calculation.opponent_forcing_moves",
            "wrong-exchange-sequence": "calculation.exchange_sequence",
            "missed capture": "calculation.candidate_moves",
        }
        for alias, expected in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(expected, resolve_skill_id(alias))
                self.assertEqual(expected, migrate_skill_id(alias, 1))
        self.assertIsNone(resolve_skill_id("positional_understanding"))
        self.assertIsNone(migrate_skill_id("unknown", 1))
        with self.assertRaises(TaxonomyVersionError):
            resolve_skill_id("fork", taxonomy_version=2)
        with self.assertRaises(TaxonomyVersionError):
            migrate_skill_id("fork", from_version=2)

    def test_ambiguous_alias_registry_is_rejected(self) -> None:
        definitions = [
            SkillDefinition(
                taxonomy_version=1,
                skill_id=f"test.skill_{index}",
                parent_id="test",
                label=f"Skill {index}",
                description="Test definition.",
                supported_evidence_types=[FACT_MOTIF],
                aliases=[alias],
            )
            for index, alias in enumerate(("same-alias", "same_alias"), start=1)
        ]
        with self.assertRaises(AmbiguousSkillAliasError):
            build_alias_index(definitions)

    def test_supported_evidence_is_explicit(self) -> None:
        self.assertTrue(supports_evidence_type("tactics.fork_detection", PUZZLE_THEME))
        self.assertTrue(supports_evidence_type("strategy.king_safety", FACT_COMPOSITE))
        self.assertTrue(supports_evidence_type("practical.time_management", CLOCK_OUTCOME))
        self.assertFalse(supports_evidence_type("practical.time_management", FACT_MOTIF))
        self.assertFalse(supports_evidence_type("unknown", ATTEMPT_OUTCOME))


class DirectEvidenceMappingTests(unittest.TestCase):
    def test_high_confidence_fact_mapping_table(self) -> None:
        cases = {
            "fork": "tactics.fork_detection",
            "allowed_mate": "tactics.mating_threat_detection",
            "missed_mate": "tactics.mating_threat_detection",
            "hanging_piece": "tactics.loose_piece_awareness",
            "missed_opponent_threat": "calculation.opponent_forcing_moves",
            "wrong_exchange_sequence": "calculation.exchange_sequence",
            "missed_capture": "calculation.candidate_moves",
        }
        for category, expected in cases.items():
            with self.subTest(category=category):
                mappings = map_fact_evidence(
                    {
                        "classification": "mistake",
                        "facts": {
                            "primary_category": category,
                            "motifs": [
                                {"name": category, "evidence_refs": [f"verified.{category}"]}
                            ],
                        },
                    }
                )
                self.assertEqual([expected], skill_ids(mappings))
                self.assertEqual(FACT_MOTIF, mappings[0].evidence_type)

    def test_blunder_check_requires_blunder_and_forcing_fact(self) -> None:
        positive = map_fact_evidence(
            {
                "classification": "blunder",
                "facts": {"primary_category": "hanging_piece"},
            }
        )
        self.assertIn("practical.blunder_check", skill_ids(positive))

        negatives = (
            {"classification": "mistake", "facts": {"primary_category": "hanging_piece"}},
            {"classification": "blunder", "facts": {"primary_category": "missed_capture"}},
            {"classification": "blunder", "metadata": {"category": "hanging_piece"}},
        )
        for position in negatives:
            with self.subTest(position=position):
                self.assertNotIn("practical.blunder_check", skill_ids(map_fact_evidence(position)))

    def test_low_confidence_or_unknown_motifs_do_not_map(self) -> None:
        cases = (
            {"facts": {"motifs": [{"name": "fork", "confidence": "low"}]}},
            {"facts": {"motifs": [{"name": "fork", "verified": False}]}},
            {"facts": {"primary_category": "positional_understanding"}},
            {"metadata": {"theme": "fork"}},
        )
        for position in cases:
            with self.subTest(position=position):
                self.assertEqual([], map_fact_evidence(position))

    def test_attempt_category_maps_only_supported_canonical_evidence(self) -> None:
        mapped = map_attempt_category("missed_capture")
        self.assertIsNotNone(mapped)
        self.assertEqual("calculation.candidate_moves", mapped.skill_id)  # type: ignore[union-attr]
        self.assertEqual(ATTEMPT_OUTCOME, mapped.evidence_type)  # type: ignore[union-attr]
        self.assertIsNone(map_attempt_category("time_management"))
        self.assertIsNone(map_attempt_category("style.creativity"))


class CompositeEvidenceMappingTests(unittest.TestCase):
    def test_strategy_requires_artifact_threshold_and_exact_delta(self) -> None:
        position = {
            "win_loss": 5.0,
            "facts": {
                "deltas": {
                    "king_safety": {"best_minus_played": 1},
                    "activity": {"best_minus_played": 5},
                }
            },
        }
        mappings = map_composite_evidence(position, analysis=critical_analysis())
        self.assertEqual(
            ["strategy.king_safety", "strategy.piece_activity"],
            skill_ids(mappings),
        )

        low_loss = {**position, "win_loss": 4.9}
        self.assertEqual([], map_composite_evidence(low_loss, analysis=critical_analysis()))
        self.assertEqual([], map_composite_evidence(position, analysis={}))

    def test_opening_development_requires_best_improvement_and_played_non_improvement(self) -> None:
        base_facts = {
            "snapshots": {
                "before": {
                    "fen": START_FEN,
                    "turn": "white",
                    "phase": {"name": "opening"},
                }
            },
            "move_effects": {
                "played": {
                    "moved_piece": {"piece": "pawn"},
                    "from": "e2",
                    "is_castle": False,
                },
                "best": {
                    "moved_piece": {"piece": "knight"},
                    "from": "g1",
                    "is_castle": False,
                },
            },
        }
        position = {"side": "white", "fen_before": START_FEN, "facts": base_facts}
        self.assertEqual(
            ["opening.development"],
            skill_ids(map_composite_evidence(position)),
        )

        already_improved = {
            **position,
            "facts": {
                **base_facts,
                "move_effects": {
                    **base_facts["move_effects"],
                    "played": {
                        "moved_piece": {"piece": "bishop"},
                        "from": "f1",
                        "is_castle": False,
                    },
                },
            },
        }
        self.assertEqual([], map_composite_evidence(already_improved))

    def test_endgame_conversion_requires_transition_and_non_win_result(self) -> None:
        position = {
            "signals": ["missed_win"],
            "facts": {"snapshots": {"before": {"phase": {"name": "endgame"}}}},
        }
        self.assertEqual(
            ["endgame.conversion"],
            skill_ids(map_composite_evidence(position, analysis=critical_analysis(result="0-1"))),
        )
        self.assertEqual(
            ["endgame.conversion"],
            skill_ids(
                map_composite_evidence(position, analysis=critical_analysis(result="1/2-1/2"))
            ),
        )
        self.assertEqual(
            [],
            map_composite_evidence(position, analysis=critical_analysis(result="1-0")),
        )
        no_transition = {**position, "signals": ["equal_to_losing"]}
        self.assertEqual(
            [],
            map_composite_evidence(no_transition, analysis=critical_analysis(result="0-1")),
        )

    def test_endgame_conversion_prefers_authoritative_analysis_result(self) -> None:
        position = {
            "signals": ["missed_win"],
            "facts": {"snapshots": {"before": {"phase": {"name": "endgame"}}}},
        }
        cases = (
            (
                "analysis win ignores stale history loss",
                critical_analysis(result="1-0"),
                {"player_result": "loss"},
                [],
            ),
            (
                "analysis loss ignores contradictory history win",
                critical_analysis(result="0-1"),
                {"player_result": "win"},
                ["endgame.conversion"],
            ),
            (
                "analysis header ignores contradictory history win",
                {
                    **critical_analysis(result="0-1"),
                    "result": None,
                    "headers": {"Result": "0-1", "TimeControl": "-"},
                },
                {"player_result": "win"},
                ["endgame.conversion"],
            ),
            (
                "history is a fallback only when analysis result is absent",
                {**critical_analysis(result="0-1"), "result": None},
                {"player_result": "loss"},
                ["endgame.conversion"],
            ),
        )
        for name, analysis, history_record, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    expected,
                    skill_ids(
                        map_composite_evidence(
                            position,
                            analysis=analysis,
                            history_record=history_record,
                        )
                    ),
                )

        invalid_or_ambiguous = (
            ({**critical_analysis(), "result": "unknown"}, {"player_result": "loss"}),
            (
                {
                    **critical_analysis(result="1-0"),
                    "headers": {"Result": "0-1", "TimeControl": "-"},
                },
                {"player_result": "loss"},
            ),
            ({**critical_analysis(), "review_side": ""}, {"player_result": "loss"}),
        )
        for analysis, history_record in invalid_or_ambiguous:
            with self.subTest(analysis=analysis):
                self.assertEqual(
                    [],
                    map_composite_evidence(
                        position,
                        analysis=analysis,
                        history_record=history_record,
                    ),
                )

    def test_time_management_requires_real_clock_and_verified_failure(self) -> None:
        position = {"ply": 1, "classification": "mistake", "facts": {}}
        mappings = map_composite_evidence(
            position,
            analysis=critical_analysis(with_clock=True),
        )
        self.assertEqual(["practical.time_management"], skill_ids(mappings))
        self.assertEqual(CLOCK_OUTCOME, mappings[0].evidence_type)

        cases = (
            (position, critical_analysis(with_clock=False)),
            ({**position, "classification": "best"}, critical_analysis(with_clock=True)),
            (position, {**critical_analysis(with_clock=True), "moves": []}),
        )
        for candidate, analysis in cases:
            with self.subTest(candidate=candidate, analysis=analysis):
                self.assertEqual([], map_composite_evidence(candidate, analysis=analysis))

    def test_combined_mapping_is_deterministic_and_deduplicated(self) -> None:
        position = {
            "classification": "blunder",
            "win_loss": 10,
            "facts": {
                "primary_category": "fork",
                "secondary_categories": ["fork"],
                "deltas": {"activity": {"best_minus_played": 5}},
            },
        }
        first = map_analysis_position(critical_analysis(), position)
        second = map_analysis_position(critical_analysis(), position)
        self.assertEqual(first, second)
        self.assertEqual(len(first), len({(item.skill_id, item.evidence_type) for item in first}))


class LichessThemeMappingTests(unittest.TestCase):
    def test_only_explicit_theme_whitelist_maps(self) -> None:
        mappings = map_lichess_themes(
            ["fork", "hangingPiece", "mateIn3", "smotheredMate", "mateIn3"]
        )
        self.assertEqual(
            [
                "tactics.fork_detection",
                "tactics.loose_piece_awareness",
                "tactics.mating_threat_detection",
            ],
            skill_ids(mappings),
        )
        self.assertTrue(all(item.evidence_type == PUZZLE_THEME for item in mappings))

    def test_metadata_and_near_match_themes_do_not_map(self) -> None:
        themes = [
            "opening",
            "middlegame",
            "endgame",
            "master",
            "short",
            "mate",
            "mateIn6",
            "Fork",
            "hanging_piece",
            "doubleAttack",
        ]
        self.assertEqual([], map_lichess_themes(themes))


if __name__ == "__main__":
    unittest.main()
