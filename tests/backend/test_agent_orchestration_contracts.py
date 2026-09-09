from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import chess

from server.core.agent.models import AGENT_TOOL_PERMISSIONS, ToolResult
from server.core.agent.routing import TOOL_CAPABILITIES
from server.core.agent.runtime_openai import (
    OpenAIAgentsRuntime,
    _LocalRunContext,
    _ToolBudget,
)
from tests.evals.orchestration.contracts import (
    ROOT,
    load_fixture_manifest,
    load_gold,
    load_json,
    load_scripts,
    load_tasks,
)
from tests.evals.orchestration.fixtures import (
    INPUT_TYPES,
    RESULT_TYPES,
    FixtureExecutor,
    load_fixture_catalog,
)
from tests.evals.orchestration.runner import (
    _request,
    _main,
    _task_variant,
    run_replay_suite,
    run_replay_variant,
    write_artifacts,
)
from tests.evals.orchestration.scorer import score_trace


class OrchestrationContractTests(unittest.IsolatedAsyncioTestCase):
    def test_frozen_baseline_files_are_unchanged(self) -> None:
        frozen = load_json("frozen_baseline.json")
        eval_root = ROOT.parent
        for name, expected in frozen["sha256"].items():
            actual = hashlib.sha256((eval_root / name).read_bytes()).hexdigest()
            self.assertEqual(expected, actual, name)

    def test_dataset_has_required_slices_variants_and_independent_labels(self) -> None:
        tasks = load_tasks().tasks
        scripts = load_scripts().scripts
        gold = load_gold().cases
        variants = [variant for task in tasks for variant in task.variants]

        self.assertEqual(24, len(tasks))
        self.assertEqual(30, len(variants))
        self.assertEqual({slice_name: 4 for slice_name in "NSAORX"}, Counter(task.slice for task in tasks))
        self.assertEqual({item.variant_id for item in variants}, set(scripts))
        self.assertEqual({item.variant_id for item in variants}, {item.variant_id for item in gold})
        self.assertEqual(len(variants), len({item.variant_id for item in variants}))
        self.assertTrue(all(task.split == "dev" for task in tasks))

        paired = next(task for task in tasks if task.task_id == "O02")
        first, second = paired.variants
        first_gold = next(item for item in gold if item.variant_id == first.variant_id)
        second_gold = next(item for item in gold if item.variant_id == second.variant_id)
        self.assertEqual(
            first_gold.acceptable_paths[0][0], second_gold.acceptable_paths[0][0]
        )
        self.assertNotEqual(
            first_gold.acceptable_paths[0][1], second_gold.acceptable_paths[0][1]
        )

    def test_fixture_catalog_uses_production_dtos_and_legal_positions(self) -> None:
        manifest = load_fixture_manifest()
        fixtures, positions = load_fixture_catalog()
        expected_ids = set(manifest.imported_fixture_ids) | set(manifest.custom_fixtures)
        self.assertEqual(expected_ids, set(fixtures))

        for fixture_id, fixture in fixtures.items():
            name = fixture["tool"]
            INPUT_TYPES[name].model_validate(fixture["request"])
            if fixture.get("result") is not None:
                ToolResult[RESULT_TYPES[name]].model_validate(fixture["result"])
        for position in positions.values():
            chess.Board(position["fen"])

    async def test_deterministic_suite_passes_and_each_slice_has_a_rejected_mutation(self) -> None:
        report = await run_replay_suite()
        metrics = report["score"]["metrics"]
        self.assertEqual(1.0, metrics["complete_success_rate"])
        self.assertEqual(1.0, metrics["next_action_accuracy"])
        self.assertEqual(1.0, metrics["parameter_accuracy"])
        self.assertEqual(1.0, metrics["recall_at_k"])
        self.assertEqual(1.0, metrics["precision_at_k"])

        gold_by_variant = {item.variant_id: item for item in load_gold().cases}
        for variant_id in (
            "N01-main",
            "S01-main",
            "A01-main",
            "O01-evidence",
            "R01-main",
            "X03-main",
        ):
            trace = deepcopy(await run_replay_variant(variant_id))
            trace["actions"][-1]["kind"] = "clarify"
            result = score_trace(trace, gold_by_variant[variant_id])
            self.assertFalse(result["complete_success"], variant_id)
            self.assertIsNotNone(result["first_divergence"], variant_id)

        fabricated = deepcopy(await run_replay_variant("N01-main"))
        fabricated["response"]["evidence_refs"] = ["fabricated:evidence"]
        fabricated_result = score_trace(fabricated, gold_by_variant["N01-main"])
        self.assertFalse(fabricated_result["evidence_satisfied"])

        wrong_claim = deepcopy(await run_replay_variant("O04-unsupported"))
        wrong_claim["response"]["fact_tags"] = ["recent_improvement_supported"]
        wrong_claim_result = score_trace(
            wrong_claim, gold_by_variant["O04-unsupported"]
        )
        self.assertFalse(wrong_claim_result["response_claims_satisfied"])

    async def test_unmatched_fixture_parameters_fail_without_a_success_artifact(self) -> None:
        catalog, _ = load_fixture_catalog()
        fixture = catalog["initial_e4_cached"]
        payload = INPUT_TYPES[fixture["tool"]].model_validate(
            {**fixture["request"], "move_uci": "d2d4"}
        )
        executor = FixtureExecutor(["initial_e4_cached"])

        execution = await executor.execute(fixture["tool"], payload)

        self.assertFalse(execution.result.ok)
        self.assertEqual([], executor.executions)
        self.assertEqual([], executor.successful_tool_results())

    async def test_artifacts_are_isolated_versioned_and_complete(self) -> None:
        report = await run_replay_suite()
        trace_count = len(report["traces"])
        with tempfile.TemporaryDirectory(prefix="orchestration-output-") as directory:
            output = Path(directory)
            write_artifacts(output, report)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            summary = json.loads((output / "report.json").read_text(encoding="utf-8"))

            self.assertEqual(trace_count, len(report["traces"]))
            self.assertEqual(trace_count, len(list(output.glob("trace-*.json"))))
            self.assertNotIn("traces", summary)
            self.assertEqual(trace_count, len(manifest["budgets"]))
            self.assertTrue(manifest["code_sha256"])
            self.assertTrue(manifest["data_sha256"])
            self.assertFalse(manifest["contains_credentials"])
            self.assertEqual([], list(output.glob("*.incomplete")))

    async def test_reserved_source_kinds_fail_explicitly(self) -> None:
        for source in ("live-fixture", "engine-system"):
            with self.subTest(source=source), self.assertRaisesRegex(
                SystemExit, "reserved and not implemented"
            ):
                await _main(
                    argparse.Namespace(
                        source=source,
                        mode="replay",
                        variant=None,
                        output_dir=None,
                    )
                )


@unittest.skipUnless(importlib.util.find_spec("agents"), "optional Agent SDK is not installed")
class OrchestrationRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_matches_production_permissions_dtos_and_sdk_schemas(self) -> None:
        registry = load_json("registry.json")
        entries = {item["name"]: item for item in registry["tools"]}
        capabilities = {item.name: item for item in TOOL_CAPABILITIES}
        self.assertEqual(set(AGENT_TOOL_PERMISSIONS), set(entries))
        self.assertEqual(set(entries), set(capabilities))

        task, variant = _task_variant("S01-main")
        request = _request(task, variant).model_copy(
            update={"allowed_tools": list(AGENT_TOOL_PERMISSIONS)}
        )
        local = _LocalRunContext(
            request=request,
            tools=FixtureExecutor(variant.fixture_ids),
            budget=_ToolBudget(max_total=6, max_engine=2),
        )
        runtime = OpenAIAgentsRuntime(
            model="registry-test",
            api_key="no-network",
            base_url="https://api.openai.com/v1",
            endpoint_type="openai_responses",
            domain_tools_factory=lambda _request: local.tools,
            session_provider=lambda _session_id: object(),
        )
        try:
            sdk_tools = {item.name: item for item in runtime._sdk_tools(local)}
            for name, entry in entries.items():
                capability = capabilities[name]
                self.assertEqual(AGENT_TOOL_PERMISSIONS[name], entry["permission"])
                self.assertEqual(capability.input_type.__name__, entry["input_type"])
                self.assertEqual(capability.result_type.__name__, entry["result_type"])
                schema = json.dumps(
                    sdk_tools[name].params_json_schema,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.assertEqual(
                    entry["sdk_schema_sha256"], hashlib.sha256(schema).hexdigest()
                )
                self.assertEqual(
                    entry["sdk_description_sha256"],
                    hashlib.sha256(sdk_tools[name].description.encode("utf-8")).hexdigest(),
                )
        finally:
            await runtime.close()
