"""Pure deterministic scoring for the Phase 0 Agent baseline dataset."""
from __future__ import annotations

from collections import Counter
import math
import statistics
from typing import Any

import chess


SCORER_VERSION = 1
_MISSING = object()


def _lookup(document: dict[str, Any], path: str) -> Any:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def matches(document: dict[str, Any], matcher: dict[str, Any]) -> bool:
    value = _lookup(document, matcher["path"])
    operation = matcher["op"]
    if operation == "exists":
        return value is not _MISSING
    if operation == "not_exists":
        return value is _MISSING
    if operation == "equals":
        return value is not _MISSING and value == matcher["value"]
    if operation == "contains":
        return value is not _MISSING and matcher["value"] in value
    if operation == "not_contains":
        return value is _MISSING or matcher["value"] not in value
    raise ValueError(f"Unknown matcher operation: {operation}")


def _call_key(call: dict[str, Any]) -> str:
    import json

    comparable = {
        "name": call["name"],
        "arguments": call["arguments"],
        "result_fixture": call["result_fixture"],
    }
    return json.dumps(comparable, sort_keys=True, separators=(",", ":"))


def _tool_selection(case: dict[str, Any], run: dict[str, Any], fixtures: dict[str, Any]) -> tuple[bool, int, int]:
    expected = case["expected"]["tools"]
    calls = run["tool_calls"]
    required = Counter(_call_key(call) for call in expected["required_calls"])
    observed = Counter(_call_key(call) for call in calls)
    names = [call["name"] for call in calls]
    engine_calls = sum(int(fixtures[call["result_fixture"]]["uses_engine"]) for call in calls)
    correct = (
        all(observed[key] >= count for key, count in required.items())
        and all(name in expected["allowed"] for name in names)
        and not set(names).intersection(expected["forbidden"])
        and len(calls) <= expected["max_total"]
        and engine_calls <= expected["max_engine"]
    )
    remaining_required = required.copy()
    unnecessary_engine = 0
    for call in calls:
        key = _call_key(call)
        if fixtures[call["result_fixture"]]["uses_engine"]:
            if remaining_required[key] > 0:
                remaining_required[key] -= 1
            else:
                unnecessary_engine += 1
    return correct, engine_calls, unnecessary_engine


def _grounded(case: dict[str, Any], response: dict[str, Any]) -> bool | None:
    if case["expected"]["outcome"]["completion"] == "error":
        return None
    expected = case["expected"]["grounding"]
    return (
        set(expected["evidence"]).issubset(response["evidence_refs"])
        and set(expected["positions"]).issubset(response["position_fixtures"])
        and response["acknowledges_uncertainty"] == expected["uncertainty"]
        and all(matches(response, matcher) for matcher in expected["required"])
        and all(matches(response, matcher) for matcher in expected["forbidden"])
    )


def _personalization_ok(case: dict[str, Any], response: dict[str, Any]) -> bool:
    expected = case["expected"]["personalization"]
    if not expected["allowed"] and (
        response["personalization_claims"] or response["personalization_tags"]
    ):
        return False
    if expected["minimum_evidence_count"] is not None:
        if len(response["evidence_refs"]) < expected["minimum_evidence_count"]:
            return False
    return all(matches(response, matcher) for matcher in expected["required"] + expected["forbidden"])


def _task_completed(case: dict[str, Any], response: dict[str, Any]) -> bool:
    expected = case["expected"]["outcome"]
    return all(response[field] == expected[field] for field in ("completion", "degradation", "error_code"))


def _incorrect_move_claims(response: dict[str, Any], positions: dict[str, Any]) -> int:
    incorrect = 0
    for claim in response["move_claims"]:
        board = chess.Board(positions[claim["position_fixture"]]["fen"])
        actual = chess.Move.from_uci(claim["move_uci"]) in board.legal_moves
        incorrect += int(actual != claim["legal"])
    return incorrect


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def score_dataset(dataset: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    if observed["dataset_id"] != dataset["dataset_id"]:
        raise ValueError("Observed runs target a different dataset")
    cases = {case["id"]: case for case in dataset["cases"]}
    runs = {run["case_id"]: run for run in observed["runs"]}
    if set(cases) != set(runs) or len(runs) != len(observed["runs"]):
        raise ValueError("Observed runs must contain every dataset case exactly once")

    tool_fixtures = dataset["fixtures"]["tool_results"]
    positions = dataset["fixtures"]["positions"]
    results: list[dict[str, Any]] = []
    for case_id in cases:
        case = cases[case_id]
        run = runs[case_id]
        response = run["response"]
        correct_tools, engine_calls, unnecessary_engine = _tool_selection(
            case, run, tool_fixtures
        )
        grounded = _grounded(case, response)
        personalization_ok = _personalization_ok(case, response)
        incorrect_moves = _incorrect_move_claims(response, positions)
        results.append(
            {
                "case_id": case_id,
                "grounded": grounded,
                "correct_tool_selection": correct_tools,
                "false_personalization": not personalization_ok,
                "task_completed": _task_completed(case, response),
                "move_claims": len(response["move_claims"]),
                "incorrect_move_claims": incorrect_moves,
                "tool_calls": len(run["tool_calls"]),
                "engine_calls": engine_calls,
                "unnecessary_engine_calls": unnecessary_engine,
                "latency_ms": run["latency_ms"],
            }
        )

    grounded_results = [item["grounded"] for item in results if item["grounded"] is not None]
    move_claims = sum(item["move_claims"] for item in results)
    incorrect_moves = sum(item["incorrect_move_claims"] for item in results)
    engine_calls = sum(item["engine_calls"] for item in results)
    unnecessary_engine = sum(item["unnecessary_engine_calls"] for item in results)
    tool_calls = sum(item["tool_calls"] for item in results)
    latencies = sorted(item["latency_ms"] for item in results)
    p95_index = max(0, math.ceil(0.95 * len(latencies)) - 1)
    metrics = {
        "grounded_response_rate": {"value": _rate(sum(grounded_results), len(grounded_results)), "passed": sum(grounded_results), "applicable": len(grounded_results)},
        "illegal_move_claim_rate": {"value": _rate(incorrect_moves, move_claims), "incorrect": incorrect_moves, "claims": move_claims},
        "correct_tool_selection_rate": {"value": _rate(sum(item["correct_tool_selection"] for item in results), len(results)), "passed": sum(item["correct_tool_selection"] for item in results), "applicable": len(results)},
        "unnecessary_engine_call_rate": {"value": _rate(unnecessary_engine, engine_calls), "unnecessary": unnecessary_engine, "engine_calls": engine_calls},
        "false_personalization_rate": {"value": _rate(sum(item["false_personalization"] for item in results), len(results)), "false": sum(item["false_personalization"] for item in results), "applicable": len(results)},
        "tool_calls_per_run": {"value": round(tool_calls / len(results), 6), "tool_calls": tool_calls, "runs": len(results)},
        "latency_ms": {"mean": round(statistics.fmean(latencies), 3), "median": round(statistics.median(latencies), 3), "p95": latencies[p95_index], "max": max(latencies)},
        "task_completion_rate": {"value": _rate(sum(item["task_completed"] for item in results), len(results)), "passed": sum(item["task_completed"] for item in results), "applicable": len(results)},
    }
    return {
        "schema_version": 1,
        "dataset_id": dataset["dataset_id"],
        "observed_runs_id": observed["observed_runs_id"],
        "scorer_version": SCORER_VERSION,
        "source": observed["source"],
        "case_count": len(results),
        "metrics": metrics,
    }
