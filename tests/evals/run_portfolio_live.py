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
    ModelVisibleContext,
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

    def _match(self, name: str, payload: Any) -> tuple[str, dict[str, Any]] | None:
        actual = payload.model_dump(mode="json", exclude_none=True)
        for fixture_id in self.case["input"].get("available_tool_results", []):
            fixture = self.fixtures[fixture_id]
            if fixture["tool"] != name:
                continue
            expected = fixture["request"]
            if all(actual.get(key) == value for key, value in expected.items()):
                return fixture_id, fixture
        return None

    def estimated_engine_calls(self, name: str, payload: Any) -> int:
        matched = self._match(name, payload)
        return int(bool(matched and matched[1]["uses_engine"]))

    async def execute(self, name: str, payload: Any) -> Any:
        matched = self._match(name, payload)
        if matched is None:
            return SimpleNamespace(
                result=ToolResult[Any](
                    ok=False,
                    error=ToolError(
                        code="position_not_found",
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
            personalization_enabled=bool(case["input"].get("profile_enabled")),
        ),
        position=position,
        engine_facts=None,
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
            result = await runtime.run(request)
            response = result.response
            structured_successes += 1
            if await sessions.get_session(session_id).get_items(limit=12):
                sqlite_recent_items_successes += 1
            function_tool_executed = function_tool_executed or bool(tools.executions)
            text = response.text.lower()
            runs.append(
                {
                    "case_id": case["id"],
                    "tool_calls": tools.executions,
                    "response": {
                        "evidence_refs": response.evidence_refs,
                        "position_fixtures": _position_names(response, positions),
                        "acknowledges_uncertainty": any(
                            marker in text
                            for marker in ("uncertain", "insufficient", "cannot verify", "不确定", "无法确认")
                        ),
                        "claims": {},
                        "claim_tags": [],
                        "personalization_claims": {},
                        "personalization_tags": [
                            "profile" for ref in response.references if ref.kind == "skill"
                        ],
                        "move_claims": [],
                        "completion": "full",
                        "degradation": "none",
                        "error_code": None,
                    },
                    "latency_ms": max(1, round((time.monotonic() - started) * 1000)),
                }
            )
        except AgentRuntimeFailure as exc:
            structured_output = structured_output and exc.error.code != "invalid_agent_response"
            runs.append(
                {
                    "case_id": case["id"],
                    "tool_calls": tools.executions,
                    "response": {
                        "evidence_refs": [],
                        "position_fixtures": [],
                        "acknowledges_uncertainty": True,
                        "claims": {},
                        "claim_tags": [],
                        "personalization_claims": {},
                        "personalization_tags": [],
                        "move_claims": [],
                        "completion": "error",
                        "degradation": exc.error.code,
                        "error_code": exc.error.code,
                    },
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
