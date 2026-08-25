from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from tests.evals.portfolio_v2 import SCORER_VERSION, score_portfolio
from tests.evals.run_portfolio import deterministic_report


ROOT = Path(__file__).parents[1] / "evals"


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


class AgentPortfolioV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.portfolio = _load("agent_portfolio_v2.json")
        cls.baseline = _load("agent_baseline_v1.json")
        cls.baseline_observed = _load("observed_fake_runs_v1.json")
        cls.observed = _load("observed_fake_runs_v2.json")

    def test_v1_case_ids_are_immutable_and_hardening_coverage_is_complete(self) -> None:
        self.assertEqual(
            [case["id"] for case in self.baseline["cases"]],
            self.portfolio["base_case_ids"],
        )
        self.assertEqual(26, len(self.portfolio["base_case_ids"]))
        self.assertEqual(
            {
                "conversation_summary_boundary",
                "reference_action_validation",
                "recent_improvement",
                "training_diversity",
                "training_stale_source",
                "storage_failure",
                "stale_cancel",
                "malformed_output",
                "custom_incompatibility",
            },
            {case["category"] for case in self.portfolio["cases"]},
        )

    def test_deterministic_report_is_complete_redacted_and_reproducible(self) -> None:
        generated_at = "2026-08-25T00:00:00Z"
        first = deterministic_report(generated_at=generated_at)
        second = deterministic_report(generated_at=generated_at)
        self.assertEqual(first, second)
        self.assertEqual(SCORER_VERSION, first["scorer_version"])
        self.assertEqual(37, first["case_count"])
        self.assertEqual(set(self.portfolio["metrics"]), set(first["metrics"]))
        serialized = json.dumps(first, sort_keys=True).lower()
        for forbidden in ("api_key", "credential", "base_url", "prompt", "reasoning", "traceback", " fen"):
            self.assertNotIn(forbidden, serialized)
        committed = _load("deterministic_report_v2.json")
        self.assertEqual(
            committed,
            deterministic_report(generated_at=committed["generated_at"]),
        )

    def test_scorer_detects_reference_degradation_and_efficiency_regressions(self) -> None:
        changed = deepcopy(self.observed)
        runs = {run["case_id"]: run for run in changed["runs"]}
        runs["reference-action-valid-028"]["reference_valid"] = False
        runs["stale-run-034"]["run_status"] = "success"
        runs["summary-boundary-027"]["tool_calls"].append(
            {"name": "analyze_position", "engine_calls": 1, "unnecessary": True}
        )
        report = score_portfolio(
            self.portfolio,
            self.baseline,
            self.baseline_observed,
            changed,
            report_metadata={"generated_at": "2026-08-25T00:00:00Z"},
        )
        self.assertLess(report["metrics"]["valid_reference_action_rate"]["value"], 1)
        self.assertLess(report["metrics"]["degradation_correctness_rate"]["value"], 1)
        self.assertGreater(report["metrics"]["unnecessary_engine_call_rate"]["value"], 0)


if __name__ == "__main__":
    unittest.main()
