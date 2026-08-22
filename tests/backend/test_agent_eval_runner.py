from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests.evals.evaluator import matches, score_dataset


EVAL_DIR = Path(__file__).parents[1] / "evals"


def load(name: str) -> dict:
    with (EVAL_DIR / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class AgentEvalRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load("agent_baseline_v1.json")
        cls.observed = load("observed_fake_runs_v1.json")
        cls.report = load("baseline_report.json")

    def test_observed_runs_are_fixed_complete_and_reference_known_fixtures(self) -> None:
        case_ids = [case["id"] for case in self.dataset["cases"]]
        run_ids = [run["case_id"] for run in self.observed["runs"]]
        self.assertEqual(run_ids, case_ids)
        self.assertEqual(len(run_ids), len(set(run_ids)))
        tool_results = self.dataset["fixtures"]["tool_results"]
        positions = self.dataset["fixtures"]["positions"]
        for run in self.observed["runs"]:
            with self.subTest(case=run["case_id"]):
                self.assertGreaterEqual(
                    run["latency_ms"],
                    sum(call["duration_ms"] for call in run["tool_calls"]),
                )
                for call in run["tool_calls"]:
                    self.assertIn(call["result_fixture"], tool_results)
                    self.assertEqual(call["name"], tool_results[call["result_fixture"]]["tool"])
                for position in run["response"]["position_fixtures"]:
                    self.assertIn(position, positions)
                for claim in run["response"]["move_claims"]:
                    self.assertIn(claim["position_fixture"], positions)

    def test_baseline_report_is_exactly_reproducible(self) -> None:
        first = score_dataset(self.dataset, self.observed)
        second = score_dataset(deepcopy(self.dataset), deepcopy(self.observed))
        self.assertEqual(first, second)
        self.assertEqual(first, self.report)
        self.assertEqual(set(first["metrics"]), set(self.dataset["metrics"]))

    def test_matcher_dsl_has_executable_positive_and_negative_semantics(self) -> None:
        document = {"claims": {"legal": False}, "claim_tags": ["cached"]}
        self.assertTrue(matches(document, {"path": "claims.legal", "op": "equals", "value": False}))
        self.assertTrue(matches(document, {"path": "claim_tags", "op": "contains", "value": "cached"}))
        self.assertTrue(matches(document, {"path": "claim_tags", "op": "not_contains", "value": "engine"}))
        self.assertTrue(matches(document, {"path": "claims.legal", "op": "exists"}))
        self.assertTrue(matches(document, {"path": "claims.score", "op": "not_exists"}))
        self.assertFalse(matches(document, {"path": "claims.legal", "op": "equals", "value": True}))

    def test_metric_scores_react_to_observed_regressions(self) -> None:
        changed = deepcopy(self.observed)
        runs = {run["case_id"]: run for run in changed["runs"]}

        runs["position-side-from-fen-002"]["response"]["claims"]["side_to_move"] = "white"
        runs["legality-cached-e4-005"]["response"]["move_claims"][0]["legal"] = False
        runs["concept-isolated-pawn-010"]["tool_calls"].append(
            {
                "name": "analyze_move",
                "arguments": {"position_fixture": "qg_before_ply_7", "move_uci": "c4d5"},
                "result_fixture": "qg_cxd5_engine",
                "duration_ms": 1,
            }
        )
        runs["profile-disabled-prioritize-review-021"]["response"][
            "personalization_tags"
        ].append("historical_pattern")
        runs["concept-fork-011"]["response"]["completion"] = "partial"

        metrics = score_dataset(self.dataset, changed)["metrics"]
        self.assertLess(metrics["grounded_response_rate"]["value"], 1.0)
        self.assertGreater(metrics["illegal_move_claim_rate"]["value"], 0.0)
        self.assertLess(metrics["correct_tool_selection_rate"]["value"], 1.0)
        self.assertGreater(metrics["unnecessary_engine_call_rate"]["value"], 0.0)
        self.assertGreater(metrics["false_personalization_rate"]["value"], 0.0)
        self.assertLess(metrics["task_completion_rate"]["value"], 1.0)

    def test_missing_or_duplicate_observed_cases_fail_closed(self) -> None:
        missing = deepcopy(self.observed)
        missing["runs"].pop()
        with self.assertRaisesRegex(ValueError, "every dataset case exactly once"):
            score_dataset(self.dataset, missing)

        duplicate = deepcopy(self.observed)
        duplicate["runs"].append(deepcopy(duplicate["runs"][0]))
        with self.assertRaisesRegex(ValueError, "every dataset case exactly once"):
            score_dataset(self.dataset, duplicate)


if __name__ == "__main__":
    unittest.main()
