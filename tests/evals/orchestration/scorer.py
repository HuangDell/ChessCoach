"""Deterministic scorer that consumes traces and independent gold labels."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import chess

from tests.evals.orchestration.contracts import GoldAction, GoldCase, GoldPredicate


SCORER_VERSION = 1


def _value(document: dict[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _reference_id(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def predicate_matches(
    predicate: GoldPredicate,
    action: dict[str, Any],
    trace: dict[str, Any],
) -> bool:
    arguments = action.get("arguments", {})
    value = _value(arguments, predicate.field)
    if predicate.type == "equals":
        return value == predicate.value
    if predicate.type == "current_fen":
        return value == trace.get("checkpoint_fen")
    if predicate.type == "legal_uci":
        fen = trace.get("checkpoint_fen")
        if not isinstance(fen, str) or not isinstance(value, str):
            return False
        try:
            return chess.Move.from_uci(value) in chess.Board(fen).legal_moves
        except ValueError:
            return False
    if predicate.type == "max_count":
        return isinstance(value, list) and isinstance(predicate.value, int) and len(value) <= predicate.value
    if predicate.type == "reference_subset":
        if not isinstance(value, list):
            return False
        allowed = set(action.get("available_reference_ids_before", []))
        return {_reference_id(item) for item in value}.issubset(allowed)
    return False


def _action_matches(
    expected: GoldAction,
    observed: dict[str, Any],
    trace: dict[str, Any],
) -> bool:
    if expected.kind != observed.get("kind"):
        return False
    if expected.name != observed.get("name"):
        return False
    return all(predicate_matches(item, observed, trace) for item in expected.predicates)


def _required_claim_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return isinstance(actual, list) and set(expected).issubset(actual)
    return actual == expected


def _path_matches(
    path: list[GoldAction], actions: list[dict[str, Any]], trace: dict[str, Any]
) -> tuple[bool, int | None]:
    for index in range(max(len(path), len(actions))):
        if index >= len(path) or index >= len(actions):
            return False, index
        if not _action_matches(path[index], actions[index], trace):
            return False, index
    return True, None


def _valid_next_tools(gold: GoldCase, prefix: list[str]) -> set[str]:
    valid: set[str] = set()
    for path in gold.acceptable_paths:
        tool_path = [item.name for item in path if item.kind == "tool_call"]
        if tool_path[: len(prefix)] != prefix or len(tool_path) <= len(prefix):
            continue
        next_name = tool_path[len(prefix)]
        if next_name is not None:
            valid.add(next_name)
    return valid


def _candidate_scores(trace: dict[str, Any], gold: GoldCase) -> dict[str, Any]:
    snapshots = {item["decision_id"]: item for item in trace.get("snapshots", [])}
    prefix: list[str] = []
    recall_sum = 0.0
    recall_states = 0
    precision_sum = 0.0
    precision_states = 0
    any_valid = 0
    applicable = 0
    empty_candidate_states = 0
    policy_blocked_states = 0
    first_divergence: dict[str, Any] | None = None
    for event in trace.get("events", []):
        if event["kind"] == "candidates_submitted":
            snapshot = snapshots[event["decision_id"]]
            candidates = set(snapshot["candidate_tools"])
            valid = _valid_next_tools(gold, prefix)
            if valid:
                if not snapshot["authorized_tools"]:
                    policy_blocked_states += 1
                    continue
                overlap = candidates & valid
                recall_sum += len(overlap) / len(valid)
                recall_states += 1
                applicable += 1
                any_valid += int(bool(overlap))
                if not overlap and first_divergence is None:
                    first_divergence = {
                        "kind": "candidate",
                        "decision_id": event["decision_id"],
                        "expected": sorted(valid),
                        "observed": sorted(candidates),
                    }
            if candidates:
                precision_sum += len(candidates & valid) / len(candidates)
                precision_states += 1
            else:
                empty_candidate_states += 1
        elif event["kind"] in {"call_attempted", "call_intercepted"}:
            prefix.append(event["tool"])
    return {
        "recall_at_k": recall_sum / recall_states if recall_states else None,
        "precision_at_k": precision_sum / precision_states if precision_states else None,
        "any_valid_at_k": any_valid / applicable if applicable else None,
        "applicable_states": applicable,
        "empty_candidate_states": empty_candidate_states,
        "policy_blocked_states": policy_blocked_states,
        "first_divergence": first_divergence,
    }


def score_trace(trace: dict[str, Any], gold: GoldCase) -> dict[str, Any]:
    actions = trace["actions"]
    path_results = [_path_matches(path, actions, trace) for path in gold.acceptable_paths]
    path_ok = any(result[0] for result in path_results)
    first_path_divergence = min(
        (index for ok, index in path_results if not ok and index is not None),
        default=None,
    )
    names = [item["name"] for item in actions if item["kind"] == "tool_call"]
    dependencies_ok = all(
        before in names and after in names and names.index(before) < names.index(after)
        for before, after in gold.dependencies
    )
    final_action = trace.get("final_action")
    final_ok = final_action in gold.allowed_final_actions
    state = trace["state"]
    artifacts_ok = set(gold.required_artifacts).issubset(state["completed_artifacts"])
    response = trace.get("response", {})
    response_evidence = set(response.get("evidence_refs", []))
    available_evidence = set(trace.get("initial_evidence_refs", [])) | set(
        state["evidence_refs"]
    )
    evidence_ok = set(gold.required_evidence).issubset(
        response_evidence
    ) and response_evidence.issubset(available_evidence)
    observed_reference_ids = {
        reference
        for observation in state["observations"]
        for reference in observation["reference_ids"]
    }
    references_ok = set(gold.required_reference_ids).issubset(observed_reference_ids)
    response_claims_ok = all(
        _required_claim_matches(_value(response, field), expected)
        for field, expected in gold.required_response_claims.items()
    )
    response_tags = set(response.get("fact_tags", []))
    response_claims_ok = response_claims_ok and all(
        tag not in response_tags for tag in gold.forbidden_response_claims
    )
    parameter_ok = any(
        len(path) == len(actions)
        and all(
            item.kind != "tool_call"
            or all(predicate_matches(predicate, observed, trace) for predicate in item.predicates)
            for item, observed in zip(path, actions, strict=True)
        )
        for path in gold.acceptable_paths
    )
    protocol_status = (
        "cancelled"
        if state["terminal_status"] == "cancelled"
        else "aborted"
        if state["terminal_status"] == "aborted" or final_action == "abort"
        else "clarified"
        if final_action == "clarify"
        else "partial"
        if final_action == "partial"
        else "complete"
    )
    protocol_ok = protocol_status == gold.expected_protocol and final_ok
    candidate = _candidate_scores(trace, gold)
    complete = all(
        (
            path_ok,
            dependencies_ok,
            parameter_ok,
            artifacts_ok,
            evidence_ok,
            references_ok,
            response_claims_ok,
            protocol_ok,
        )
    )
    first_divergence = candidate["first_divergence"]
    if first_divergence is None and not complete:
        first_divergence = {
            "kind": "action" if first_path_divergence is not None else "outcome",
            "action_index": first_path_divergence,
        }
    attempts = state["attempts"]
    return {
        "task_id": trace["task_id"],
        "variant_id": trace["variant_id"],
        "candidate": candidate,
        "next_action_correct": path_ok,
        "parameter_correct": parameter_ok,
        "dependencies_satisfied": dependencies_ok,
        "artifacts_satisfied": artifacts_ok,
        "evidence_satisfied": evidence_ok,
        "references_satisfied": references_ok,
        "response_claims_satisfied": response_claims_ok,
        "protocol_correct": protocol_ok,
        "complete_success": complete,
        "invalid_calls": sum(item["status"] == "intercepted" for item in attempts),
        "redundant_calls": sum(bool(item["redundant"]) for item in attempts),
        "logical_tool_calls": len(attempts),
        "engine_calls": sum(item["engine_calls"] for item in attempts),
        "first_divergence": first_divergence,
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    recall = [item["candidate"]["recall_at_k"] for item in results if item["candidate"]["recall_at_k"] is not None]
    precision = [item["candidate"]["precision_at_k"] for item in results if item["candidate"]["precision_at_k"] is not None]
    any_valid = [item["candidate"]["any_valid_at_k"] for item in results if item["candidate"]["any_valid_at_k"] is not None]
    count = len(results)
    rate = lambda key: round(sum(bool(item[key]) for item in results) / count, 6) if count else 0.0
    mean = lambda values: round(sum(values) / len(values), 6) if values else None
    return {
        "schema_version": 1,
        "scorer_version": SCORER_VERSION,
        "case_count": count,
        "metrics": {
            "recall_at_k": mean(recall),
            "precision_at_k": mean(precision),
            "any_valid_at_k": mean(any_valid),
            "next_action_accuracy": rate("next_action_correct"),
            "parameter_accuracy": rate("parameter_correct"),
            "dependency_satisfaction_rate": rate("dependencies_satisfied"),
            "artifact_satisfaction_rate": rate("artifacts_satisfied"),
            "evidence_satisfaction_rate": rate("evidence_satisfied"),
            "reference_constraint_rate": rate("references_satisfied"),
            "response_claim_accuracy": rate("response_claims_satisfied"),
            "complete_success_rate": rate("complete_success"),
            "protocol_correctness_rate": rate("protocol_correct"),
            "invalid_calls": sum(item["invalid_calls"] for item in results),
            "redundant_calls": sum(item["redundant_calls"] for item in results),
            "logical_tool_calls": sum(item["logical_tool_calls"] for item in results),
            "engine_calls": sum(item["engine_calls"] for item in results),
            "empty_candidate_states": sum(item["candidate"]["empty_candidate_states"] for item in results),
            "policy_blocked_states": sum(item["candidate"]["policy_blocked_states"] for item in results),
        },
        "results": results,
    }
