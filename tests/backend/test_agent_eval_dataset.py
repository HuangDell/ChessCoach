from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
import unittest

import chess

from server.core.agent.models import (
    AnalyzeMoveInput,
    AnalyzeMoveResult,
    AnalyzePositionInput,
    AnalyzePositionResult,
    CreateTrainingDraftInput,
    GetPlayerProfileInput,
    GetPlayerProfileResult,
    GetReviewContextInput,
    GetReviewContextResult,
    GetTrainingCandidatesInput,
    GetTrainingCandidatesResult,
    LookupOpeningInput,
    LookupOpeningResult,
    ToolResult,
    TrainingDraft,
)


DATASET_PATH = Path(__file__).parents[1] / "evals" / "agent_baseline_v1.json"
EXPECTED_CATEGORIES = {
    "position_grounding",
    "move_legality_claims",
    "cached_review_vs_engine",
    "concept_no_engine",
    "what_if_analyze_move",
    "follow_up_reference_resolution",
    "tool_budget_compliance",
    "false_recurring_weakness",
    "profile_disabled",
    "training_draft_grounding",
    "failure_degradation",
}
EXPECTED_METRICS = {
    "grounded_response_rate",
    "illegal_move_claim_rate",
    "correct_tool_selection_rate",
    "unnecessary_engine_call_rate",
    "false_personalization_rate",
    "tool_calls_per_run",
    "latency_ms",
    "task_completion_rate",
}
TOOL_CONTRACTS = {
    "get_review_context": (GetReviewContextInput, GetReviewContextResult),
    "analyze_position": (AnalyzePositionInput, AnalyzePositionResult),
    "analyze_move": (AnalyzeMoveInput, AnalyzeMoveResult),
    "lookup_opening": (LookupOpeningInput, LookupOpeningResult),
    "get_player_profile": (GetPlayerProfileInput, GetPlayerProfileResult),
    "get_training_candidates": (GetTrainingCandidatesInput, GetTrainingCandidatesResult),
    "create_training_draft": (CreateTrainingDraftInput, TrainingDraft),
}
MATCHER_OPERATORS = {"equals", "contains", "not_contains", "exists", "not_exists"}
REFERENCE_FIELDS = ("game_id", "review_side", "critical_id", "ply", "fen")


def load_dataset() -> dict:
    with DATASET_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def comparable_reference(reference: dict) -> dict:
    return {field: reference.get(field) for field in REFERENCE_FIELDS}


class AgentEvalDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset()
        cls.fixtures = cls.dataset["fixtures"]

    def test_dataset_identity_size_categories_and_metrics(self) -> None:
        self.assertEqual(self.dataset["schema_version"], 1)
        self.assertEqual(self.dataset["dataset_id"], "agent-baseline-v1")
        self.assertEqual(set(self.dataset["metrics"]), EXPECTED_METRICS)
        self.assertEqual(set(self.dataset["metric_definitions"]), EXPECTED_METRICS)
        self.assertGreaterEqual(len(self.dataset["cases"]), 20)
        self.assertLessEqual(len(self.dataset["cases"]), 30)
        counts = Counter(case["category"] for case in self.dataset["cases"])
        self.assertEqual(set(counts), EXPECTED_CATEGORIES)

    def test_case_contract_and_executable_matchers(self) -> None:
        ids = [case["id"] for case in self.dataset["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        for case in self.dataset["cases"]:
            with self.subTest(case=case["id"]):
                self.assertRegex(case["id"], re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+-\d{3}$"))
                self.assertTrue(case["task"])
                self.assertTrue(case["input"]["message"])
                self.assertIn("position_fixture", case["input"])
                self.assertIsInstance(case["input"]["profile_enabled"], bool)
                self.assertIn(case["expected"]["outcome"]["completion"], {"full", "partial", "error"})
                for section in ("grounding", "personalization"):
                    expected = case["expected"][section]
                    for group in ("required", "forbidden"):
                        self.assertIsInstance(expected[group], list)
                        for matcher in expected[group]:
                            self.assertEqual(set(matcher), {"path", "op", "value"} if matcher["op"] not in {"exists", "not_exists"} else {"path", "op"})
                            self.assertIn(matcher["op"], MATCHER_OPERATORS)
                            self.assertTrue(matcher["path"])

    def test_game_timelines_and_critical_fen_before_semantics(self) -> None:
        games = self.fixtures["games"]
        positions = self.fixtures["positions"]
        for game_name, game in games.items():
            with self.subTest(game=game_name):
                self.assertEqual(len(game["moves_uci"]), len(game["moves_san"]))
                board = chess.Board(game["initial_fen"])
                for uci, san in zip(game["moves_uci"], game["moves_san"], strict=True):
                    move = chess.Move.from_uci(uci)
                    self.assertIn(move, board.legal_moves)
                    self.assertEqual(board.san(move), san)
                    board.push(move)

        for name, position in positions.items():
            with self.subTest(position=name):
                board = chess.Board(position["fen"])
                self.assertTrue(board.is_valid())
                reference = position["reference"]
                self.assertEqual(reference["fen"], position["fen"])
                if position["game_fixture"] is None:
                    self.assertIsNone(reference["critical_id"])
                    continue
                game = games[position["game_fixture"]]
                before_ply = position["before_ply"]
                replay = chess.Board(game["initial_fen"])
                for uci in game["moves_uci"][: before_ply - 1]:
                    replay.push_uci(uci)
                self.assertEqual(replay.fen(), position["fen"])
                self.assertEqual(reference["game_id"], game["game_id"])
                self.assertEqual(reference["ply"], before_ply)
                self.assertEqual(reference["critical_id"], f"ply-{before_ply}")
                self.assertEqual(replay.turn, chess.WHITE if before_ply % 2 else chess.BLACK)
                self.assertEqual(replay.fullmove_number, (before_ply + 1) // 2)
                self.assertNotEqual(replay.fullmove_number, 1)

    def test_every_fake_tool_request_and_result_uses_phase0_pydantic_contract(self) -> None:
        for fixture_name, fixture in self.fixtures["tool_results"].items():
            with self.subTest(tool_result=fixture_name):
                input_type, output_type = TOOL_CONTRACTS[fixture["tool"]]
                request = input_type.model_validate(fixture["request"])
                result = ToolResult[output_type].model_validate(fixture["result"])
                self.assertEqual(result.ok, fixture["result"]["error"] is None)
                if result.ok:
                    self.assertIsNotNone(result.data)
                else:
                    self.assertTrue(result.error.message)
                if fixture["tool"] == "get_player_profile" and result.ok:
                    references = [
                        comparable_reference(position["reference"])
                        for position in self.fixtures["positions"].values()
                    ]
                    for estimate in fixture["result"]["data"]["relevant_estimates"]:
                        self.assertTrue(estimate["examples"])
                        for example in estimate["examples"]:
                            self.assertIn(comparable_reference(example), references)
                if fixture["tool"] == "get_review_context" and result.ok:
                    self.assertIn(
                        comparable_reference(fixture["result"]["data"]["reference"]),
                        [comparable_reference(item["reference"]) for item in self.fixtures["positions"].values()],
                    )
                if fixture["tool"] == "get_training_candidates" and result.ok:
                    for candidate in fixture["result"]["data"]["candidates"]:
                        self.assertIn(
                            comparable_reference(candidate["reference"]),
                            [comparable_reference(item["reference"]) for item in self.fixtures["positions"].values()],
                        )
                if fixture["tool"] == "create_training_draft" and result.ok:
                    for reference in fixture["result"]["data"]["position_references"]:
                        self.assertIn(
                            comparable_reference(reference),
                            [comparable_reference(item["reference"]) for item in self.fixtures["positions"].values()],
                        )
                position_name = fixture.get("position_fixture")
                if position_name:
                    position = self.fixtures["positions"][position_name]
                    request_fen = getattr(request, "fen", None) or getattr(request, "fen_before", None)
                    if request_fen:
                        self.assertEqual(request_fen, position["fen"])

    def test_evidence_moves_and_tool_move_results_replay_legally(self) -> None:
        positions = self.fixtures["positions"]
        for evidence_ref, item in self.fixtures["evidence"].items():
            board = chess.Board(positions[item["position_fixture"]]["fen"])
            claims = item["claims"]
            moves = [claims[key] for key in ("move", "played_move", "best_move") if key in claims]
            moves.extend(claims.get("candidate_moves", []))
            for move_ref in moves:
                with self.subTest(evidence=evidence_ref, move=move_ref["uci"]):
                    move = chess.Move.from_uci(move_ref["uci"])
                    self.assertIn(move, board.legal_moves)
                    self.assertEqual(board.san(move), move_ref["san"])

        for fixture_name, fixture in self.fixtures["tool_results"].items():
            if fixture["tool"] != "analyze_move":
                continue
            board = chess.Board(fixture["request"]["fen_before"])
            move = chess.Move.from_uci(fixture["request"]["move_uci"])
            legal = move in board.legal_moves
            with self.subTest(tool_result=fixture_name):
                if fixture["result"]["ok"]:
                    self.assertTrue(legal)
                    self.assertTrue(fixture["result"]["data"]["legal"])
                elif fixture["result"]["error"]["code"] == "illegal_move":
                    self.assertFalse(legal)

    def test_case_fixture_ownership_and_tool_budgets(self) -> None:
        defaults = self.dataset["defaults"]
        evidence = self.fixtures["evidence"]
        positions = self.fixtures["positions"]
        results = self.fixtures["tool_results"]
        for case in self.dataset["cases"]:
            with self.subTest(case=case["id"]):
                input_data = case["input"]
                if input_data["position_fixture"] is not None:
                    self.assertIn(input_data["position_fixture"], positions)
                self.assertTrue(set(input_data["context_evidence_refs"]).issubset(evidence))
                available = set(input_data["available_tool_results"])
                self.assertTrue(available.issubset(results))
                tools = case["expected"]["tools"]
                self.assertTrue(set(tools["allowed"]).isdisjoint(tools["forbidden"]))
                self.assertLessEqual(tools["max_total"], defaults["max_total_tool_calls"])
                self.assertLessEqual(tools["max_engine"], defaults["max_engine_tool_calls"])
                self.assertLessEqual(len(tools["required_calls"]), tools["max_total"])
                owned_evidence = set(input_data["context_evidence_refs"])
                engine_calls = 0
                for call in tools["required_calls"]:
                    self.assertIn(call["name"], tools["allowed"])
                    self.assertIn(call["result_fixture"], available)
                    fixture = results[call["result_fixture"]]
                    self.assertEqual(fixture["tool"], call["name"])
                    arguments = call["arguments"]
                    if "position_fixture" in arguments:
                        self.assertEqual(fixture.get("position_fixture"), arguments["position_fixture"])
                    if "move_uci" in arguments:
                        self.assertEqual(fixture["request"].get("move_uci"), arguments["move_uci"])
                    owned_evidence.update(fixture["result"]["evidence_refs"])
                    engine_calls += int(fixture["uses_engine"])
                self.assertLessEqual(engine_calls, tools["max_engine"])
                grounding = case["expected"]["grounding"]
                self.assertTrue(set(grounding["evidence"]).issubset(owned_evidence))
                self.assertTrue(set(grounding["positions"]).issubset(positions))
                if not input_data["profile_enabled"]:
                    self.assertNotIn("get_player_profile", tools["allowed"])


if __name__ == "__main__":
    unittest.main()
