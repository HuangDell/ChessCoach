from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from server.core.agent.models import (
    AgentResponse,
    AgentRunResult,
    AnalyzePositionInput,
    ChessReference,
    CreateTrainingDraftInput,
    GetPlayerProfileInput,
    GetTrainingCandidatesInput,
)
from tests.evals.evaluator import diagnose_dataset, score_dataset
from tests.evals.portfolio_v2 import score_portfolio
from tests.evals.run_portfolio_live import (
    _FixtureTools,
    _all_quality_gates,
    _context,
    _run_cases,
)


EVAL_DIR = Path(__file__).parents[1] / "evals"


def _load(name: str) -> dict:
    return json.loads((EVAL_DIR / name).read_text(encoding="utf-8"))


class _Session:
    def __init__(self) -> None:
        self.items: list[object] = []

    async def get_items(self, limit: int | None = None) -> list[object]:
        return list(self.items[-limit:] if limit is not None else self.items)

    async def add_items(self, items: list[object]) -> None:
        self.items.extend(items)

    async def clear_session(self) -> None:
        self.items.clear()


class _Sessions:
    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}

    def get_session(self, session_id: str) -> _Session:
        return self.sessions.setdefault(session_id, _Session())

    async def clear_session(self, session_id: str) -> None:
        await self.get_session(session_id).clear_session()


def _reference(dataset: dict, position_name: str) -> ChessReference:
    values = {
        key: value
        for key, value in dataset["fixtures"]["positions"][position_name]["reference"].items()
        if value is not None
    }
    return ChessReference(
        kind=("critical_position" if values.get("critical_id") else "position"),
        **values,
    )


def _response(dataset: dict, observed: dict) -> AgentResponse:
    payload = observed["response"]
    references = [_reference(dataset, name) for name in payload["position_fixtures"]]
    personal = payload["personalization_claims"]
    if personal.get("skill_id"):
        references.append(ChessReference(kind="skill", skill_id=personal["skill_id"]))
    return AgentResponse(
        text="Grounded fixture answer.",
        references=references,
        evidence_refs=payload["evidence_refs"],
        grounding={
            "acknowledges_uncertainty": payload["acknowledges_uncertainty"],
            "claims": payload["claims"],
            "personalization_claims": personal,
            "move_claims": [
                {
                    "position": dataset["fixtures"]["positions"][item["position_fixture"]][
                        "reference"
                    ],
                    "move_uci": item["move_uci"],
                    "legal": item["legal"],
                }
                for item in payload["move_claims"]
            ],
            "completion": payload["completion"],
            "degradation": payload["degradation"],
            "error_code": payload["error_code"],
        },
    )


class _PerfectRuntime:
    def __init__(
        self,
        dataset: dict,
        observed: dict,
        tool_instances: dict,
        sessions: _Sessions,
    ) -> None:
        self.dataset = dataset
        self.runs = {item["case_id"]: item for item in observed["runs"]}
        self.tool_instances = tool_instances
        self.sessions = sessions

    async def run(self, request) -> AgentRunResult:
        index = int(request.session_id.rsplit("-", 1)[1])
        case = self.dataset["cases"][index]
        observed = self.runs[case["id"]]
        self.tool_instances[request.session_id].executions.extend(
            deepcopy(observed["tool_calls"])
        )
        await self.sessions.get_session(request.session_id).add_items(
            [{"role": "assistant", "content": "fixture"}]
        )
        return AgentRunResult(
            response=_response(self.dataset, observed),
            tool_calls=[],
        )

    def take_telemetry(self, _run_id: str) -> None:
        return None


class AgentLiveEvalTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_attempt_diagnostics_are_canonical_and_redacted(self) -> None:
        dataset = _load("agent_baseline_v1.json")
        case = next(
            item
            for item in dataset["cases"]
            if item["id"] == "profile-single-error-not-recurring-019"
        )
        tools = _FixtureTools(case, dataset["fixtures"]["tool_results"])

        await tools.execute(
            "get_player_profile",
            GetPlayerProfileInput(focus_categories=["fork"]),
        )

        self.assertEqual(
            [
                {
                    "name": "get_player_profile",
                    "matched": True,
                    "canonical_skill_ids": ["tactics.fork_detection"],
                    "unresolved_focus_count": 0,
                }
            ],
            tools.attempt_summaries,
        )
        self.assertNotIn("focus_categories", tools.attempt_summaries[0])

    def test_fixture_matching_uses_contract_semantics_not_free_text(self) -> None:
        dataset = _load("agent_baseline_v1.json")
        cases = {item["id"]: item for item in dataset["cases"]}
        fixtures = dataset["fixtures"]["tool_results"]

        profile_tools = _FixtureTools(
            cases["profile-single-error-not-recurring-019"], fixtures
        )
        profile_match = profile_tools._match(
            "get_player_profile",
            GetPlayerProfileInput(
                focus_skill_ids=["tactics.fork_detection"],
                focus_categories=["forks"],
                limit=5,
            ),
        )
        self.assertEqual("profile_single_fork", profile_match[0])

        established_tools = _FixtureTools(
            cases["training-established-weakness-022"], fixtures
        )
        established_tools.executions.append({"name": "get_player_profile"})
        candidates_match = established_tools._match(
            "get_training_candidates",
            GetTrainingCandidatesInput(limit=5),
        )
        self.assertEqual("training_candidates", candidates_match[0])

        fallback_tools = _FixtureTools(cases["budget-one-fallback-018"], fixtures)
        position = dataset["fixtures"]["positions"]["italian_before_ply_9"]
        analysis_match = fallback_tools._match(
            "analyze_position",
            AnalyzePositionInput(fen=position["fen"], purpose="explain_position"),
        )
        self.assertEqual("position_timeout", analysis_match[0])

        training_tools = _FixtureTools(cases["training-draft-subset-023"], fixtures)
        draft_request = deepcopy(fixtures["training_draft"]["request"])
        draft_request.update(
            title="Two forcing-move positions",
            rationale="Use a forcing-move scan on both positions.",
        )
        draft_match = training_tools._match(
            "create_training_draft",
            CreateTrainingDraftInput.model_validate(draft_request),
        )
        self.assertEqual("training_draft", draft_match[0])

    def test_live_context_includes_only_current_authoritative_review_facts(self) -> None:
        dataset = _load("agent_baseline_v1.json")
        case = next(
            item for item in dataset["cases"]
            if item["id"] == "profile-disabled-prioritize-review-021"
        )

        context = _context(case, dataset)

        self.assertIsNotNone(context.engine_facts)
        assert context.engine_facts is not None
        self.assertEqual(["review:italian:ply-9"], context.engine_facts.evidence_refs)
        self.assertEqual("mistake", context.engine_facts.classification)
        self.assertEqual("b1c3", context.engine_facts.best_move.uci)
        self.assertEqual("white", context.engine_facts.facts["score_pov"])

    async def test_conforming_live_observations_can_pass_every_quality_gate(self) -> None:
        dataset = _load("agent_baseline_v1.json")
        observed = _load("observed_fake_runs_v1.json")
        portfolio = _load("agent_portfolio_v2.json")
        hardening = _load("observed_fake_runs_v2.json")
        tools: dict = {}
        sessions = _Sessions()
        runtime = _PerfectRuntime(dataset, observed, tools, sessions)

        live_observed, compatibility = await _run_cases(
            runtime, sessions, dataset, tools  # type: ignore[arg-type]
        )
        baseline = score_dataset(dataset, live_observed)
        report = score_portfolio(
            portfolio,
            dataset,
            live_observed,
            hardening,
            report_metadata={},
        )

        self.assertTrue(all(compatibility.values()))
        self.assertEqual(1.0, baseline["metrics"]["grounded_response_rate"]["value"])
        self.assertEqual(1.0, baseline["metrics"]["task_completion_rate"]["value"])
        self.assertGreater(baseline["metrics"]["illegal_move_claim_rate"]["claims"], 0)
        self.assertTrue(_all_quality_gates(report))
        self.assertTrue(
            all(item["grounded"] is not False for item in diagnose_dataset(dataset, live_observed))
        )

    def test_diagnostics_identify_the_failed_grounding_dimension(self) -> None:
        dataset = _load("agent_baseline_v1.json")
        observed = _load("observed_fake_runs_v1.json")
        changed = deepcopy(observed)
        changed["runs"][0]["response"]["evidence_refs"] = []

        first = diagnose_dataset(dataset, changed)[0]

        self.assertFalse(first["grounded"])
        self.assertFalse(first["grounding_checks"]["evidence"])
        self.assertTrue(first["grounding_checks"]["positions"])
        self.assertEqual(
            {"completion": "full", "degradation": "none", "error_code": None},
            first["actual_outcome"],
        )
        self.assertNotIn("text", first)


if __name__ == "__main__":
    unittest.main()
