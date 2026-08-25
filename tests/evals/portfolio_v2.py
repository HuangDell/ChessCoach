"""Versioned, deterministic scorer for the Phase 5 Agent portfolio."""
from __future__ import annotations

import math
import statistics
from typing import Any

from tests.evals.evaluator import score_dataset


SCORER_VERSION = 2


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def score_portfolio(
    portfolio: dict[str, Any],
    baseline_dataset: dict[str, Any],
    baseline_observed: dict[str, Any],
    observed: dict[str, Any],
    *,
    report_metadata: dict[str, Any],
) -> dict[str, Any]:
    if portfolio["dataset_id"] != "agent-portfolio-v2" or observed["dataset_id"] != portfolio["dataset_id"]:
        raise ValueError("Observed runs target a different portfolio")
    baseline_ids = [case["id"] for case in baseline_dataset["cases"]]
    if portfolio["base_case_ids"] != baseline_ids or len(baseline_ids) != 26:
        raise ValueError("Portfolio must preserve the ordered Phase 0 case IDs")
    cases = {case["id"]: case for case in portfolio["cases"]}
    runs = {run["case_id"]: run for run in observed["runs"]}
    if set(cases) != set(runs) or len(runs) != len(observed["runs"]):
        raise ValueError("Observed runs must contain every hardening case exactly once")

    baseline = score_dataset(baseline_dataset, baseline_observed)
    base_metrics = baseline["metrics"]
    results: list[dict[str, Any]] = []
    for case_id, case in cases.items():
        expected = case["expected"]
        run = runs[case_id]
        reference_checks = [
            run[field] == expected[field]
            for field in ("reference_valid", "action_valid")
            if expected[field] is not None
        ]
        results.append(
            {
                "grounded": run["grounded"],
                "correct_tools": run["correct_tool_selection"] is expected["correct_tool_selection"],
                "false_personalization": bool(run["false_personalization"]),
                "reference_action_correct": all(reference_checks) if reference_checks else None,
                "task_completed": run["completion"] == expected["completion"],
                "degradation_correct": all(
                    run[field] == expected[field]
                    for field in ("degradation", "error_code", "run_status")
                ),
                "move_claims": int(run["move_claims"]),
                "incorrect_move_claims": int(run["incorrect_move_claims"]),
                "tool_calls": len(run["tool_calls"]),
                "engine_calls": sum(int(call["engine_calls"]) for call in run["tool_calls"]),
                "unnecessary_engine_calls": sum(
                    int(call.get("unnecessary", False)) * int(call["engine_calls"])
                    for call in run["tool_calls"]
                ),
                "latency_ms": int(run["latency_ms"]),
            }
        )

    hard_grounded = [item["grounded"] for item in results if item["grounded"] is not None]
    hard_reference = [
        item["reference_action_correct"]
        for item in results
        if item["reference_action_correct"] is not None
    ]
    base_grounded = base_metrics["grounded_response_rate"]
    base_legal = base_metrics["illegal_move_claim_rate"]
    base_tools = base_metrics["correct_tool_selection_rate"]
    base_engine = base_metrics["unnecessary_engine_call_rate"]
    base_personalization = base_metrics["false_personalization_rate"]
    base_completion = base_metrics["task_completion_rate"]
    base_tool_calls = int(base_metrics["tool_calls_per_run"]["tool_calls"])
    base_engine_calls = int(base_engine["engine_calls"])
    total_runs = len(baseline_ids) + len(results)
    all_latencies = [run["latency_ms"] for run in baseline_observed["runs"]] + [
        item["latency_ms"] for item in results
    ]
    ordered_latencies = sorted(all_latencies)
    hard_move_claims = sum(item["move_claims"] for item in results)
    hard_incorrect = sum(item["incorrect_move_claims"] for item in results)
    hard_engine_calls = sum(item["engine_calls"] for item in results)
    hard_unnecessary = sum(item["unnecessary_engine_calls"] for item in results)
    hard_tools = sum(item["correct_tools"] for item in results)
    hard_false_personalization = sum(item["false_personalization"] for item in results)
    hard_completed = sum(item["task_completed"] for item in results)
    metrics = {
        "grounded_response_rate": {
            "value": _rate(base_grounded["passed"] + sum(hard_grounded), base_grounded["applicable"] + len(hard_grounded)),
            "passed": base_grounded["passed"] + sum(hard_grounded),
            "applicable": base_grounded["applicable"] + len(hard_grounded),
        },
        "illegal_move_claim_rate": {
            "value": _rate(base_legal["incorrect"] + hard_incorrect, base_legal["claims"] + hard_move_claims),
            "incorrect": base_legal["incorrect"] + hard_incorrect,
            "claims": base_legal["claims"] + hard_move_claims,
        },
        "correct_tool_selection_rate": {
            "value": _rate(base_tools["passed"] + hard_tools, base_tools["applicable"] + len(results)),
            "passed": base_tools["passed"] + hard_tools,
            "applicable": base_tools["applicable"] + len(results),
        },
        "unnecessary_engine_call_rate": {
            "value": _rate(base_engine["unnecessary"] + hard_unnecessary, base_engine_calls + hard_engine_calls),
            "unnecessary": base_engine["unnecessary"] + hard_unnecessary,
            "engine_calls": base_engine_calls + hard_engine_calls,
        },
        "false_personalization_rate": {
            "value": _rate(base_personalization["false"] + hard_false_personalization, base_personalization["applicable"] + len(results)),
            "false": base_personalization["false"] + hard_false_personalization,
            "applicable": base_personalization["applicable"] + len(results),
        },
        "valid_reference_action_rate": {"value": _rate(sum(hard_reference), len(hard_reference)), "passed": sum(hard_reference), "applicable": len(hard_reference)},
        "tool_calls_per_run": {"value": round((base_tool_calls + sum(item["tool_calls"] for item in results)) / total_runs, 6), "tool_calls": base_tool_calls + sum(item["tool_calls"] for item in results), "runs": total_runs},
        "engine_calls_per_run": {"value": round((base_engine_calls + hard_engine_calls) / total_runs, 6), "engine_calls": base_engine_calls + hard_engine_calls, "runs": total_runs},
        "latency_ms": {"p50": round(statistics.median(ordered_latencies)), "p95": ordered_latencies[max(0, math.ceil(0.95 * len(ordered_latencies)) - 1)], "max": max(ordered_latencies)},
        "task_completion_rate": {"value": _rate(base_completion["passed"] + hard_completed, base_completion["applicable"] + len(results)), "passed": base_completion["passed"] + hard_completed, "applicable": base_completion["applicable"] + len(results)},
        "degradation_correctness_rate": {"value": _rate(sum(item["degradation_correct"] for item in results), len(results)), "passed": sum(item["degradation_correct"] for item in results), "applicable": len(results)},
    }
    return {
        "schema_version": 2,
        "dataset_id": portfolio["dataset_id"],
        "base_dataset_id": baseline_dataset["dataset_id"],
        "observed_runs_id": observed["observed_runs_id"],
        "scorer_version": SCORER_VERSION,
        "source": observed["source"],
        "case_count": total_runs,
        "base_case_count": len(baseline_ids),
        "hardening_case_count": len(results),
        **report_metadata,
        "metrics": metrics,
    }
