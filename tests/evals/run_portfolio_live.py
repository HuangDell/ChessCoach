"""Opt-in live portfolio execution through the production OpenAIAgentsRuntime adapter."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import time
from typing import Any

from server.core.agent.models import (
    AgentRunRequest,
    EngineFactsContext,
    ModelVisibleContext,
    MoveReference,
    PositionContext,
    PositionReference,
    TaskContext,
    ToolError,
    ToolResult,
)
from server.core.agent.policy import POLICY_VERSION
from server.core.agent.runtime import AgentRuntimeFailure
from server.core.agent.runtime_openai import (
    AGENTS_SDK_VERSION,
    OpenAIAgentsRuntime,
    SQLiteConversationSessionFactory,
)
from server.core.storage.agent_compatibility import AgentCompatibilityStore, endpoint_fingerprint
from server.core.storage.agent_runs import RESPONSE_SCHEMA_VERSION
from server.core.learning.taxonomy import resolve_skill_id
from tests.evals.evaluator import diagnose_dataset
from tests.evals.portfolio_v2 import score_portfolio


ROOT = Path(__file__).resolve().parent


def _load(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


class _FixtureTools:
    """Serve only the concrete tool results assigned to one portfolio case."""

    def __init__(self, case: dict[str, Any], fixtures: dict[str, Any]) -> None:
        self.case = case
        self.fixtures = fixtures
        self.executions: list[dict[str, Any]] = []
        self.attempted_names: list[str] = []
        self.attempt_summaries: list[dict[str, Any]] = []

    @staticmethod
    def _resolved_focus(
        actual: dict[str, Any], id_key: str, category_key: str
    ) -> set[str] | None:
        skill_ids = actual.get(id_key, [])
        resolved_ids = [resolve_skill_id(value) for value in skill_ids]
        if skill_ids and all(value is not None for value in resolved_ids):
            return {value for value in resolved_ids if value is not None}
        raw = [*skill_ids, *actual.get(category_key, [])]
        resolved = [resolve_skill_id(value) for value in raw]
        return None if any(value is None for value in resolved) else set(resolved)

    def _semantically_matches(
        self,
        name: str,
        actual: dict[str, Any],
        expected: dict[str, Any],
        fixture: dict[str, Any],
    ) -> bool:
        if name == "get_player_profile":
            expected_skills = set(expected.get("focus_skill_ids", []))
            if self._resolved_focus(
                actual, "focus_skill_ids", "focus_categories"
            ) != expected_skills:
                return False
            return not expected_skills or 1 <= int(actual.get("limit", 3)) <= 5
        if name == "get_training_candidates":
            expected_skills = set(expected.get("skill_ids", []))
            actual_skills = self._resolved_focus(actual, "skill_ids", "categories")
            prior_profile = any(
                execution["name"] == "get_player_profile"
                for execution in self.executions
            )
            if actual_skills != expected_skills and not (
                actual_skills == set() and prior_profile
            ):
                return False
            candidate_count = len(
                fixture["result"].get("data", {}).get("candidates", [])
            )
            if int(actual.get("limit", 10)) < candidate_count:
                return False
            return all(
                actual.get(key) == value
                for key, value in expected.items()
                if key not in {"skill_ids", "categories", "limit"}
            )
        if name == "create_training_draft":
            material = {
                "objective_skill_ids",
                "position_references",
                "recommended_count",
                "source",
            }
            return all(
                actual.get(key) == value
                for key, value in expected.items()
                if key in material
            )
        if name == "analyze_position":
            return actual.get("fen") == expected.get("fen")
        return all(actual.get(key) == value for key, value in expected.items())

    def _match(self, name: str, payload: Any) -> tuple[str, dict[str, Any]] | None:
        actual = payload.model_dump(mode="json", exclude_none=True)
        for fixture_id in self.case["input"].get("available_tool_results", []):
            fixture = self.fixtures[fixture_id]
            if fixture["tool"] != name:
                continue
            expected = fixture["request"]
            fixture_error = fixture["result"].get("error")
            if (
                name == "get_player_profile"
                and fixture_error is not None
                and fixture_error.get("code") == "profile_unavailable"
            ):
                return fixture_id, fixture
            if self._semantically_matches(name, actual, expected, fixture):
                return fixture_id, fixture
        return None

    def estimated_engine_calls(self, name: str, payload: Any) -> int:
        matched = self._match(name, payload)
        return int(bool(matched and matched[1]["uses_engine"]))

    async def execute(self, name: str, payload: Any) -> Any:
        self.attempted_names.append(name)
        actual = payload.model_dump(mode="json", exclude_none=True)
        matched = self._match(name, payload)
        summary: dict[str, Any] = {"name": name, "matched": matched is not None}
        if name in {"get_player_profile", "get_training_candidates"}:
            id_key = "focus_skill_ids" if name == "get_player_profile" else "skill_ids"
            category_key = (
                "focus_categories" if name == "get_player_profile" else "categories"
            )
            raw_focus = [*actual.get(id_key, []), *actual.get(category_key, [])]
            resolved = [resolve_skill_id(value) for value in raw_focus]
            summary.update(
                {
                    "canonical_skill_ids": sorted(
                        {value for value in resolved if value is not None}
                    ),
                    "unresolved_focus_count": sum(value is None for value in resolved),
                }
            )
        elif name == "create_training_draft":
            summary.update(
                {
                    "objective_skill_ids": sorted(actual["objective_skill_ids"]),
                    "position_count": len(actual["position_references"]),
                }
            )
        elif name == "analyze_position":
            summary["purpose"] = actual["purpose"]
        self.attempt_summaries.append(summary)
        if matched is None:
            error_code = (
                "profile_unavailable"
                if name == "get_player_profile"
                else "training_unavailable"
                if name in {"get_training_candidates", "create_training_draft"}
                else "position_not_found"
            )
            return SimpleNamespace(
                result=ToolResult[Any](
                    ok=False,
                    error=ToolError(
                        code=error_code,
                        message="The live eval tool request did not match an assigned fixture.",
                        recoverable=False,
                    ),
                ),
                cache_hit=False,
                engine_calls=0,
            )
        fixture_id, fixture = matched
        expected_calls = self.case["expected"]["tools"]["required_calls"]
        comparable = next(
            (
                call
                for call in expected_calls
                if call["name"] == name and call["result_fixture"] == fixture_id
            ),
            {
                "name": name,
                "arguments": payload.model_dump(mode="json", exclude_none=True),
                "result_fixture": fixture_id,
            },
        )
        self.executions.append({**comparable, "duration_ms": 1})
        return SimpleNamespace(
            result=ToolResult[Any].model_validate(fixture["result"]),
            cache_hit=bool(fixture.get("cache_hit", False)),
            engine_calls=int(bool(fixture["uses_engine"])),
        )


def _engine_facts(
    case: dict[str, Any],
    dataset: dict[str, Any],
    position_name: str | None,
) -> EngineFactsContext | None:
    if position_name is None:
        return None
    evidence = dataset["fixtures"]["evidence"]
    matched: list[tuple[str, dict[str, Any]]] = []
    for evidence_ref in case["input"].get("context_evidence_refs", []):
        fixture = evidence.get(evidence_ref)
        if (
            evidence_ref.startswith("review:")
            and fixture is not None
            and fixture.get("position_fixture") == position_name
        ):
            matched.append((evidence_ref, fixture["claims"]))
    if not matched:
        return None

    combined_claims: dict[str, Any] = {}
    for _, claims in matched:
        combined_claims.update(claims)
    reference = PositionReference.model_validate(
        dataset["fixtures"]["positions"][position_name]["reference"]
    )
    return EngineFactsContext(
        reference=reference,
        played_move=(
            MoveReference.model_validate(combined_claims["played_move"])
            if "played_move" in combined_claims
            else None
        ),
        best_move=(
            MoveReference.model_validate(combined_claims["best_move"])
            if "best_move" in combined_claims
            else None
        ),
        classification=combined_claims.get("classification"),
        facts=combined_claims,
        evidence_refs=[evidence_ref for evidence_ref, _ in matched],
    )


def _context(case: dict[str, Any], dataset: dict[str, Any]) -> ModelVisibleContext:
    position_name = case["input"].get("position_fixture")
    position = None
    if position_name:
        fixture = dataset["fixtures"]["positions"][position_name]
        position = PositionContext(
            fen=fixture["fen"],
            recent_moves_uci=[],
            recent_moves_san=[],
            reference=PositionReference.model_validate(fixture["reference"]),
        )
    return ModelVisibleContext(
        task=TaskContext(
            activity=case["input"]["activity"],
            user_goal=case["task"],
            review_side=(position.reference.review_side if position else None),
            personalization_enabled=bool(case["input"].get("profile_enabled")),
        ),
        position=position,
        engine_facts=_engine_facts(case, dataset, position_name),
        relevant_profile=None,
        relevant_memory=[],
        conversation_summary="",
        allowed_evidence_refs=case["input"].get("context_evidence_refs", []),
    )


def _position_names(response: Any, positions: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for reference in response.references:
        for name, fixture in positions.items():
            owned = fixture["reference"]
            if reference.fen == fixture["fen"] or (
                reference.game_id == owned.get("game_id")
                and reference.critical_id == owned.get("critical_id")
                and reference.game_id is not None
            ):
                if name not in found:
                    found.append(name)
    return found


def _position_name(reference: Any, positions: dict[str, Any]) -> str | None:
    for name, fixture in positions.items():
        owned = fixture["reference"]
        if reference.fen == fixture["fen"] or (
            reference.game_id == owned.get("game_id")
            and reference.critical_id == owned.get("critical_id")
            and reference.game_id is not None
        ):
            return name
    return None


def _observed_response(response: Any, positions: dict[str, Any]) -> dict[str, Any]:
    grounding = response.grounding
    move_claims = []
    for claim in grounding.move_claims:
        position_name = _position_name(claim.position, positions)
        if position_name is not None:
            move_claims.append(
                {
                    "position_fixture": position_name,
                    "move_uci": claim.move_uci,
                    "legal": claim.legal,
                }
            )
    personalization_claims = grounding.personalization_claims.model_dump(
        mode="json", exclude_none=True
    )
    return {
        "evidence_refs": response.evidence_refs,
        "position_fixtures": _position_names(response, positions),
        "acknowledges_uncertainty": grounding.acknowledges_uncertainty,
        "claims": grounding.claims.model_dump(mode="json", exclude_none=True),
        "claim_tags": [],
        "personalization_claims": personalization_claims,
        "personalization_tags": [
            "profile" for ref in response.references if ref.kind == "skill"
        ],
        "move_claims": move_claims,
        "completion": grounding.completion,
        "degradation": grounding.degradation,
        "error_code": grounding.error_code,
    }


def _runtime_error_response(error_code: str) -> dict[str, Any]:
    return {
        "evidence_refs": [],
        "position_fixtures": [],
        "acknowledges_uncertainty": True,
        "claims": {},
        "claim_tags": [],
        "personalization_claims": {},
        "personalization_tags": [],
        "move_claims": [],
        "completion": "error",
        "degradation": error_code,
        "error_code": error_code,
    }


async def _run_cases(
    runtime: OpenAIAgentsRuntime,
    sessions: SQLiteConversationSessionFactory,
    dataset: dict[str, Any],
    tool_instances: dict[str, _FixtureTools],
) -> tuple[dict[str, Any], dict[str, bool]]:
    runs: list[dict[str, Any]] = []
    structured_output = True
    structured_successes = 0
    sqlite_recent_items_successes = 0
    any_expected_function_tool = False
    function_tool_executed = False
    positions = dataset["fixtures"]["positions"]
    for index, case in enumerate(dataset["cases"]):
        session_id = f"live-eval-{index:03d}"
        tools = _FixtureTools(case, dataset["fixtures"]["tool_results"])
        tool_instances[session_id] = tools
        session = sessions.get_session(session_id)
        seeded_items = [
            {"role": item["role"], "content": item["content"]}
            for item in case["input"].get("conversation", [])
            if item.get("content")
        ]
        if seeded_items:
            await session.add_items(seeded_items)
        initial_item_count = len(await session.get_items(limit=12))
        request = AgentRunRequest(
            session_id=session_id,
            expected_generation=0,
            message=case["input"]["message"],
            model_context=_context(case, dataset),
            allowed_tools=case["expected"]["tools"]["allowed"],
            max_turns=4,
            max_total_tool_calls=case["expected"]["tools"]["max_total"],
            max_engine_tool_calls=case["expected"]["tools"]["max_engine"],
            timeout_seconds=120,
        )
        expected_function = bool(case["expected"]["tools"]["required_calls"])
        any_expected_function_tool = any_expected_function_tool or expected_function
        started = time.monotonic()
        try:
            if case["input"].get("agent_available") is False:
                runs.append(
                    {
                        "case_id": case["id"],
                        "tool_calls": [],
                        "tool_attempt_names": [],
                        "tool_attempt_summaries": [],
                        "response": _runtime_error_response("agent_unavailable"),
                        "latency_ms": max(1, round((time.monotonic() - started) * 1000)),
                    }
                )
                continue
            result = await runtime.run(request)
            response = result.response
            structured_successes += 1
            if len(await session.get_items(limit=12)) > initial_item_count:
                sqlite_recent_items_successes += 1
            function_tool_executed = function_tool_executed or bool(
                tools.attempted_names or tools.executions
            )
            runs.append(
                {
                    "case_id": case["id"],
                    "tool_calls": tools.executions,
                    "tool_attempt_names": [call.name for call in result.tool_calls],
                    "tool_attempt_summaries": list(tools.attempt_summaries),
                    "response": _observed_response(response, positions),
                    "latency_ms": max(1, round((time.monotonic() - started) * 1000)),
                }
            )
        except AgentRuntimeFailure as exc:
            structured_output = structured_output and exc.error.code != "invalid_agent_response"
            runs.append(
                {
                    "case_id": case["id"],
                    "tool_calls": tools.executions,
                    "tool_attempt_names": list(tools.attempted_names),
                    "tool_attempt_summaries": list(tools.attempt_summaries),
                    "response": _runtime_error_response(exc.error.code),
                    "latency_ms": max(1, round((time.monotonic() - started) * 1000)),
                }
            )
        finally:
            runtime.take_telemetry(request.run_id)
            await sessions.clear_session(session_id)
    return (
        {
            "schema_version": 1,
            "observed_runs_id": "agent-portfolio-v2-live-responses",
            "dataset_id": dataset["dataset_id"],
            "source": "live",
            "runs": runs,
        },
        {
            "responses_structured_output": structured_output and structured_successes > 0,
            "responses_function_tools": any_expected_function_tool and function_tool_executed,
            "responses_sqlite_recent_items": (
                structured_successes > 0
                and sqlite_recent_items_successes == structured_successes
            ),
        },
    )


def _all_quality_gates(report: dict[str, Any]) -> bool:
    metrics = report["metrics"]
    return bool(
        metrics["grounded_response_rate"]["value"] == 1
        and metrics["illegal_move_claim_rate"]["value"] == 0
        and metrics["correct_tool_selection_rate"]["value"] == 1
        and metrics["unnecessary_engine_call_rate"]["value"] == 0
        and metrics["false_personalization_rate"]["value"] == 0
        and metrics["valid_reference_action_rate"]["value"] == 1
        and metrics["task_completion_rate"]["value"] == 1
        and metrics["degradation_correctness_rate"]["value"] == 1
    )


async def _run(
    *, source: str, model: str, base_url: str, api_key: str, data_dir: str
) -> tuple[dict[str, Any], dict[str, bool]]:
    dataset = _load("agent_baseline_v1.json")
    portfolio = _load("agent_portfolio_v2.json")
    hardening_observed = _load("observed_fake_runs_v2.json")
    sessions = SQLiteConversationSessionFactory(data_dir)
    tool_instances: dict[str, _FixtureTools] = {}
    runtime = OpenAIAgentsRuntime(
        model=model,
        api_key=api_key,
        base_url=base_url or "https://api.openai.com/v1",
        endpoint_type="custom_responses" if source == "custom" else "openai_responses",
        domain_tools_factory=lambda request: tool_instances[request.session_id],
        session_provider=sessions.get_session,
    )
    try:
        baseline_observed, gates = await _run_cases(runtime, sessions, dataset, tool_instances)
        hardening_observed["source"] = source
        hardening_observed["observed_runs_id"] = (
            f"agent-portfolio-v2-{source}-with-deterministic-hardening"
        )
        report = score_portfolio(
            portfolio,
            dataset,
            baseline_observed,
            hardening_observed,
            report_metadata={
                "model": model,
                "endpoint_type": runtime.availability.endpoint_type,
                "sdk_version": runtime.sdk_version,
                "policy_version": POLICY_VERSION,
                "response_schema_version": RESPONSE_SCHEMA_VERSION,
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "live_model_case_count": len(dataset["cases"]),
                "deterministic_hardening_case_count": len(portfolio["cases"]),
            },
        )
        report["live_case_diagnostics"] = diagnose_dataset(dataset, baseline_observed)
        gates["portfolio_quality"] = _all_quality_gates(report)
        report["compatibility_gates"] = gates
        report["all_passed"] = all(gates.values())
        if source == "custom":
            report["endpoint_sha256"] = endpoint_fingerprint(base_url)
        return report, gates
    finally:
        await runtime.close()
        sessions.close()


def run_live_portfolio(
    *,
    source: str,
    model: str,
    base_url: str,
    api_key: str,
    data_dir: str,
    certificate_data_dir: str,
) -> dict[str, Any]:
    report, gates = asyncio.run(
        _run(
            source=source,
            model=model,
            base_url=base_url,
            api_key=api_key,
            data_dir=data_dir,
        )
    )
    if source == "custom" and all(gates.values()):
        if not certificate_data_dir:
            raise SystemExit("--certificate-data-dir is required to certify a passing custom eval")
        store = AgentCompatibilityStore(certificate_data_dir)
        store.save(
            store.certificate(
                base_url=base_url,
                model=model,
                sdk_version=AGENTS_SDK_VERSION,
                policy_version=POLICY_VERSION,
                response_schema_version=RESPONSE_SCHEMA_VERSION,
                gate_cases=gates,
            )
        )
    return report


__all__ = ["run_live_portfolio"]
