"""Offline runner for execution-aware orchestration P0."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from server.core.agent.models import (
    AgentResponse,
    AgentResponseGrounding,
    AgentRunRequest,
    EngineFactsContext,
    ModelVisibleContext,
    PositionContext,
    TaskContext,
    ToolError,
    ToolResult,
)
from server.core.agent.routing import OrchestrationController, SequencedCandidateProvider
from server.core.agent.runtime import AgentRuntimeFailure
from server.core.agent.runtime_openai import OpenAIAgentsRuntime
from server.core.agent.sessions import InMemoryConversationSessionFactory
from tests.evals.orchestration.contracts import (
    ROOT,
    GoldCase,
    ScriptAction,
    TaskRecord,
    VariantRecord,
    load_gold,
    load_scripts,
    load_tasks,
)
from tests.evals.orchestration.fixtures import (
    INPUT_TYPES,
    FixtureExecutor,
    FixtureStorageConsistencyError,
    load_fixture_catalog,
)
from tests.evals.orchestration.scorer import SCORER_VERSION, aggregate, score_trace


TRACE_VERSION = 1
STATE_VERSION = 1
EXPERIMENT_ID = "execution-aware-orchestration-p0"
REPOSITORY_ROOT = ROOT.parents[2]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_variant(variant_id: str) -> tuple[TaskRecord, VariantRecord]:
    for task in load_tasks().tasks:
        for variant in task.variants:
            if variant.variant_id == variant_id:
                return task, variant
    raise ValueError(f"unknown orchestration variant: {variant_id}")


def _context(task: TaskRecord) -> ModelVisibleContext:
    _, positions = load_fixture_catalog()
    position = None
    if task.checkpoint.position_fixture is not None:
        fixture = positions[task.checkpoint.position_fixture]
        position = PositionContext(
            fen=fixture["fen"],
            recent_moves_uci=[],
            recent_moves_san=[],
            reference=fixture.get("reference"),
        )
    facts = None
    if task.checkpoint.has_engine_facts and position is not None and position.reference is not None:
        facts = EngineFactsContext(
            reference=position.reference,
            facts={"fixture": "prevalidated"},
            evidence_refs=task.checkpoint.evidence_refs,
        )
    return ModelVisibleContext(
        task=TaskContext(
            activity=task.checkpoint.activity,
            user_goal=task.query,
            personalization_enabled=task.checkpoint.personalization_enabled,
        ),
        position=position,
        engine_facts=facts,
        relevant_profile=None,
        relevant_memory=[],
        conversation_summary="",
        allowed_evidence_refs=task.checkpoint.evidence_refs,
    )


def _request(task: TaskRecord, variant: VariantRecord) -> AgentRunRequest:
    budget = task.budget
    return AgentRunRequest(
        run_id=f"p0-{variant.variant_id}",
        session_id=f"p0-session-{variant.variant_id}",
        expected_generation=0,
        message=task.query,
        model_context=_context(task),
        allowed_tools=task.checkpoint.allowed_tools,
        max_turns=budget.max_turns,
        max_total_tool_calls=budget.max_total_tool_calls,
        max_engine_tool_calls=budget.max_engine_tool_calls,
        timeout_seconds=budget.timeout_seconds,
    )


def _payload(action: ScriptAction) -> tuple[str, Any, str | None]:
    catalog, _ = load_fixture_catalog()
    if action.fixture_id is not None:
        fixture = catalog[action.fixture_id]
        name = fixture["tool"]
        return name, INPUT_TYPES[name].model_validate(fixture["request"]), action.fixture_id
    if action.name is None or action.arguments is None:
        raise ValueError("tool script action requires fixture_id or name plus arguments")
    return action.name, INPUT_TYPES[action.name].model_validate(action.arguments), None


def _reference_ids(controller: OrchestrationController) -> list[str]:
    assert controller.state is not None
    return [
        reference
        for observation in controller.state.observations
        for reference in observation.reference_ids
    ]


async def run_replay_variant(variant_id: str) -> dict[str, Any]:
    task, variant = _task_variant(variant_id)
    script = load_scripts().scripts[variant.script_id]
    request = _request(task, variant)
    provider = SequencedCandidateProvider(script.candidate_decisions)
    controller = OrchestrationController(
        request,
        provider,
        allow_fallback=script.allow_fallback,
        missing_artifacts=task.checkpoint.missing_artifacts,
    )
    executor = FixtureExecutor(variant.fixture_ids)
    total_used = 0
    engine_used = 0
    actions: list[dict[str, Any]] = []
    final_action: str | None = None
    response: dict[str, Any] = {}

    for action in script.actions:
        if action.kind in {"answer", "clarify", "partial", "abort"}:
            final_action = action.kind
            response = dict(action.response)
            if action.kind == "abort":
                controller.terminate("budget", reason="scripted_abort")
            else:
                controller.terminate("accepted")
            actions.append({"kind": action.kind})
            break
        if action.kind == "generation_change":
            controller.generation_changed(action.generation or 0)
            continue
        if action.kind == "invalidate_fixture":
            if action.fixture_id is None:
                raise ValueError("invalidate_fixture requires fixture_id")
            executor.invalidate(action.fixture_id)
            continue
        if action.kind == "cancel":
            controller.terminate("cancelled")
            final_action = "abort"
            break
        if action.kind != "tool_call":
            raise ValueError(f"unsupported replay action: {action.kind}")

        if request.allowed_tools:
            await asyncio.gather(
                *(controller.is_enabled(name) for name in request.allowed_tools)
            )
        name, payload, fixture_id = _payload(action)
        available_before = _reference_ids(controller)
        if total_used >= request.max_total_tool_calls:
            call_id, _ = await controller.before_call(
                name, payload, total_used=total_used, engine_used=engine_used
            )
            error = ToolError(
                code="tool_budget_exceeded",
                message="The replay tool budget is exhausted.",
                recoverable=True,
            )
            controller.after_call(
                call_id,
                name,
                payload,
                ToolResult[Any](ok=False, error=error),
                cache_hit=False,
                engine_calls=0,
                total_used=total_used,
                engine_used=engine_used,
            )
            controller.terminate("budget", reason="total_tool_calls")
            actions.append(
                {
                    "kind": "tool_call",
                    "name": name,
                    "arguments": payload.model_dump(mode="json", exclude_none=True),
                    "fixture_id": fixture_id,
                    "result": {"ok": False, "error": {"code": "tool_budget_exceeded"}},
                    "available_reference_ids_before": available_before,
                }
            )
            continue

        total_used += 1
        call_id, routing_error = await controller.before_call(
            name, payload, total_used=total_used, engine_used=engine_used
        )
        if routing_error is not None:
            actions.append(
                {
                    "kind": "tool_call",
                    "name": name,
                    "arguments": payload.model_dump(mode="json", exclude_none=True),
                    "fixture_id": fixture_id,
                    "result": ToolResult[Any](ok=False, error=routing_error).model_dump(
                        mode="json", exclude_none=True
                    ),
                    "available_reference_ids_before": available_before,
                }
            )
            continue

        reserved_engine = executor.estimated_engine_calls(name, payload)
        if engine_used + reserved_engine > request.max_engine_tool_calls:
            error = ToolError(
                code="tool_budget_exceeded",
                message="The replay Engine budget is exhausted.",
                recoverable=True,
            )
            controller.after_call(
                call_id,
                name,
                payload,
                ToolResult[Any](ok=False, error=error),
                cache_hit=False,
                engine_calls=0,
                total_used=total_used,
                engine_used=engine_used,
            )
            controller.terminate("budget", reason="engine_tool_calls")
            result_payload = ToolResult[Any](ok=False, error=error).model_dump(
                mode="json", exclude_none=True
            )
        else:
            try:
                execution = await executor.execute(name, payload)
                engine_used += execution.engine_calls
                if action.generation_after is not None:
                    controller.generation_changed(action.generation_after)
                controller.after_call(
                    call_id,
                    name,
                    payload,
                    execution.result,
                    cache_hit=execution.cache_hit,
                    engine_calls=execution.engine_calls,
                    total_used=total_used,
                    engine_used=engine_used,
                )
                result_payload = execution.result.model_dump(mode="json", exclude_none=True)
            except asyncio.CancelledError:
                controller.terminate("cancelled")
                result_payload = {"ok": False, "error": {"code": "cancelled"}}
                final_action = "abort"
            except FixtureStorageConsistencyError:
                error = ToolError(
                    code="position_not_found",
                    message="The fixture storage is inconsistent.",
                    recoverable=False,
                )
                controller.after_call(
                    call_id,
                    name,
                    payload,
                    ToolResult[Any](ok=False, error=error),
                    cache_hit=False,
                    engine_calls=0,
                    total_used=total_used,
                    engine_used=engine_used,
                    error_category="storage_consistency",
                )
                controller.terminate("rejected", reason="storage_consistency")
                result_payload = ToolResult[Any](ok=False, error=error).model_dump(
                    mode="json", exclude_none=True
                )
        actions.append(
            {
                "kind": "tool_call",
                "name": name,
                "arguments": payload.model_dump(mode="json", exclude_none=True),
                "fixture_id": fixture_id,
                "result": result_payload,
                "available_reference_ids_before": available_before,
            }
        )
        if final_action == "abort":
            break

    if final_action is None:
        final = next(
            (
                item
                for item in reversed(script.actions)
                if item.kind in {"answer", "clarify", "partial", "abort"}
            ),
            None,
        )
        final_action = final.kind if final is not None else "abort"
        response = dict(final.response) if final is not None else {}
        actions.append({"kind": final_action})
    elif not actions or actions[-1]["kind"] == "tool_call":
        final = next(
            (
                item
                for item in reversed(script.actions)
                if item.kind in {"answer", "clarify", "partial", "abort"}
            ),
            None,
        )
        if final is not None:
            response = dict(final.response)
        actions.append({"kind": final_action})

    trace = controller.trace()
    assert controller.state is not None
    return {
        "trace_version": TRACE_VERSION,
        "source_kind": "deterministic",
        "execution_mode": "state_replay",
        "task_id": task.task_id,
        "variant_id": variant.variant_id,
        "split": task.split,
        "fixture_source": task.source.model_dump(mode="json", exclude_none=True),
        "checkpoint_fen": request.model_context.position.fen if request.model_context.position else None,
        "initial_evidence_refs": list(request.model_context.allowed_evidence_refs),
        "actions": actions,
        "final_action": final_action,
        "response": response,
        "actual_engine_calls": engine_used,
        "simulated_engine_calls": engine_used,
        "actual_engine_process_starts": 0,
        "usage": {},
        "latency_ms": None,
        "error": None,
        "production_validation": "not_run",
        **trace,
    }


def _agent_response_for_script(actions: list[ScriptAction], fixture_ids: list[str]) -> AgentResponse:
    final = next(item for item in reversed(actions) if item.kind in {"answer", "clarify", "partial"})
    response = final.response
    catalog, _ = load_fixture_catalog()
    errors = [
        catalog[item.fixture_id]["result"].get("error")
        for item in actions
        if item.kind == "tool_call"
        and item.fixture_id in catalog
        and catalog[item.fixture_id].get("result")
        and not catalog[item.fixture_id]["result"].get("ok")
    ]
    errors = [item for item in errors if item]
    if final.kind == "clarify":
        grounding = AgentResponseGrounding(
            acknowledges_uncertainty=True,
            completion="partial",
            degradation="missing_context",
        )
    elif errors:
        error_code = errors[0]["code"]
        if error_code == "illegal_move":
            grounding = AgentResponseGrounding(
                completion="full",
                degradation="tool_error_handled",
                error_code="illegal_move",
            )
        else:
            grounding = AgentResponseGrounding(
                acknowledges_uncertainty=True,
                completion="partial",
                degradation="recoverable_tool_failure",
                error_code=error_code,
            )
    else:
        grounding = AgentResponseGrounding()
    return AgentResponse(
        text="Deterministic orchestration fixture response.",
        evidence_refs=response.get("evidence_refs", []),
        grounding=grounding,
    )


async def run_sdk_fake_variant(variant_id: str) -> dict[str, Any]:
    try:
        from agents.testing import ScriptedModel, assistant_message, function_call
    except ModuleNotFoundError as exc:
        raise RuntimeError("openai-agents==0.22.0 is required for sdk-fake mode") from exc

    task, variant = _task_variant(variant_id)
    script = load_scripts().scripts[variant.script_id]
    if any(
        item.kind in {"abort", "cancel", "generation_change", "invalidate_fixture"}
        or item.generation_after is not None
        for item in script.actions
    ):
        raise ValueError("sdk-fake mode supports bounded tool/final scripts; use replay for control events")
    request = _request(task, variant)
    fixture_executor = FixtureExecutor(variant.fixture_ids)
    model_steps: list[list[Any]] = []
    call_number = 0
    for action in script.actions:
        if action.kind == "tool_call":
            name, payload, _ = _payload(action)
            call_number += 1
            model_steps.append(
                [
                    function_call(
                        name,
                        payload.model_dump(mode="json", exclude_none=True),
                        call_id=f"fixture-call-{call_number}",
                    )
                ]
            )
        elif action.kind in {"answer", "clarify", "partial"}:
            model_steps.append(
                [assistant_message(_agent_response_for_script(script.actions, variant.fixture_ids).model_dump_json())]
            )
    model = ScriptedModel(model_steps)
    sessions = InMemoryConversationSessionFactory()
    controller_box: dict[str, OrchestrationController] = {}

    def orchestration_factory(run_request: AgentRunRequest) -> OrchestrationController:
        controller = OrchestrationController(
            run_request,
            SequencedCandidateProvider(script.candidate_decisions),
            allow_fallback=script.allow_fallback,
            missing_artifacts=task.checkpoint.missing_artifacts,
        )
        controller_box[run_request.run_id] = controller
        return controller

    runtime = OpenAIAgentsRuntime(
        model="p0-scripted-model",
        api_key="p0-no-network",
        base_url="https://api.openai.com/v1",
        endpoint_type="openai_responses",
        domain_tools_factory=lambda _request: fixture_executor,
        session_provider=lambda session_id: sessions.get_session(session_id),
        orchestration_factory=orchestration_factory,
        research_model=model,
    )
    error: str | None = None
    result = None
    try:
        result = await runtime.run(request)
    except AgentRuntimeFailure as exc:
        error = exc.error.code
    finally:
        await runtime.close()
        sessions.close()
    trace = runtime.take_orchestration_trace(request.run_id)
    if trace is None:
        trace = controller_box[request.run_id].trace()
    final = next(item for item in reversed(script.actions) if item.kind in {"answer", "clarify", "partial"})
    actions = []
    available_reference_ids: list[str] = []
    successful_observations = iter(trace["state"]["observations"])
    for item in fixture_executor.executions:
        actions.append(
            {
                "kind": "tool_call",
                "name": item["name"],
                "arguments": item["arguments"],
                "result": item["result"],
                "fixture_id": item["fixture_id"],
                "available_reference_ids_before": list(available_reference_ids),
            }
        )
        if item["result"]["ok"]:
            observation = next(successful_observations)
            available_reference_ids.extend(observation["reference_ids"])
    actions.append({"kind": final.kind})
    request_tools = [
        {
            "names": [tool.name for tool in call.tools],
            "schema_sha256": {
                tool.name: hashlib.sha256(
                    json.dumps(
                        tool.params_json_schema, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                for tool in call.tools
            },
            "history": [
                {
                    key: value
                    for key in ("type", "name", "call_id")
                    if (value := (
                        item.get(key)
                        if isinstance(item, dict)
                        else getattr(item, key, None)
                    )) is not None
                }
                for item in call.input
            ],
        }
        for call in model.calls
    ]
    return {
        "trace_version": TRACE_VERSION,
        "source_kind": "deterministic",
        "execution_mode": "sdk_fake_model",
        "task_id": task.task_id,
        "variant_id": variant.variant_id,
        "split": task.split,
        "fixture_source": task.source.model_dump(mode="json", exclude_none=True),
        "checkpoint_fen": request.model_context.position.fen if request.model_context.position else None,
        "initial_evidence_refs": list(request.model_context.allowed_evidence_refs),
        "actions": actions,
        "final_action": final.kind,
        "response": final.response,
        "sdk_model_requests": request_tools,
        "sdk_version": runtime.sdk_version,
        "model": "scripted-fake",
        "provider": None,
        "price": None,
        "error": error,
        "usage": result.usage if result is not None else {},
        "latency_ms": None,
        "production_validation": (
            "accepted" if trace["state"]["terminal_status"] == "succeeded" else "rejected"
        ),
        "actual_engine_calls": sum(item.engine_call_count for item in result.tool_calls) if result else 0,
        "simulated_engine_calls": sum(item["engine_calls"] for item in fixture_executor.executions),
        "actual_engine_process_starts": 0,
        **trace,
    }


async def run_replay_suite() -> dict[str, Any]:
    gold = {item.variant_id: item for item in load_gold().cases}
    traces: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for task in load_tasks().tasks:
        for variant in task.variants:
            trace = await run_replay_variant(variant.variant_id)
            traces.append(trace)
            results.append(score_trace(trace, gold[variant.variant_id]))
    return {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset_id": "orchestration-dev-v1",
        "source_kind": "deterministic",
        "execution_mode": "state_replay",
        "task_count": len(load_tasks().tasks),
        "variant_count": len(traces),
        "score": score_trace_set(results),
        "traces": traces,
    }


def score_trace_set(results: list[dict[str, Any]]) -> dict[str, Any]:
    return aggregate(results)


def build_manifest(mode: str, variants: list[str]) -> dict[str, Any]:
    files = ["tasks.json", "fixtures.json", "gold.json", "scripts.json", "registry.json"]
    code_files = [
        "server/core/agent/execution_state.py",
        "server/core/agent/routing.py",
        "server/core/agent/runtime_openai.py",
        "tests/evals/orchestration/contracts.py",
        "tests/evals/orchestration/fixtures.py",
        "tests/evals/orchestration/runner.py",
        "tests/evals/orchestration/scorer.py",
    ]
    return {
        "schema_version": 1,
        "experiment_id": EXPERIMENT_ID,
        "dataset_id": "orchestration-dev-v1",
        "variants": variants,
        "split": "dev",
        "source_kind": "deterministic",
        "execution_mode": mode,
        "registry_id": "native-agent-tools-v1",
        "scorer_version": SCORER_VERSION,
        "trace_version": TRACE_VERSION,
        "state_version": STATE_VERSION,
        "seed": 0,
        "method_id": "p0-wiring-sequence",
        "model_id": "scripted-fake" if mode == "sdk_fake_model" else None,
        "sdk_version": "0.22.0" if mode == "sdk_fake_model" else None,
        "prompt_id": "production-policy-v3" if mode == "sdk_fake_model" else None,
        "data_sha256": {name: _sha256(ROOT / name) for name in files},
        "code_sha256": {
            name: _sha256(REPOSITORY_ROOT / name) for name in code_files
        },
        "budgets": {
            variant: _task_variant(variant)[0].budget.model_dump(mode="json")
            for variant in variants
        },
        "cache_configuration": "fixture-defined",
        "contains_credentials": False,
    }


def write_artifacts(output_dir: Path, report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    traces = report.get("traces", [])
    variants = [trace["variant_id"] for trace in traces]
    payloads = {
        "manifest.json": build_manifest(report["execution_mode"], variants),
        "report.json": {
            key: value for key, value in report.items() if key != "traces"
        },
        **{f"trace-{trace['variant_id']}.json": trace for trace in traces},
    }
    for name, payload in payloads.items():
        destination = output_dir / name
        temporary = output_dir / f".{name}.incomplete"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)


async def _main(args: argparse.Namespace) -> Path:
    if args.source != "deterministic":
        raise SystemExit(f"source kind {args.source!r} is reserved and not implemented in P0")
    if args.mode == "replay":
        report = await run_replay_suite()
    else:
        variant_id = args.variant or "S01-main"
        trace = await run_sdk_fake_variant(variant_id)
        gold: GoldCase = next(item for item in load_gold().cases if item.variant_id == variant_id)
        result = score_trace(trace, gold)
        report = {
            "schema_version": 1,
            "experiment_id": EXPERIMENT_ID,
            "dataset_id": "orchestration-dev-v1",
            "source_kind": "deterministic",
            "execution_mode": "sdk_fake_model",
            "task_count": 1,
            "variant_count": 1,
            "score": score_trace_set([result]),
            "traces": [trace],
        }
    output_dir = args.output_dir or Path(tempfile.mkdtemp(prefix="chesscoach-orchestration-p0-"))
    write_artifacts(output_dir, report)
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        choices=("deterministic", "live-fixture", "engine-system"),
        default="deterministic",
    )
    parser.add_argument("--mode", choices=("replay", "sdk-fake"), default="replay")
    parser.add_argument("--variant")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    output_dir = asyncio.run(_main(args))
    print(output_dir)


if __name__ == "__main__":
    main()
