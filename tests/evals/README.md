# Agent baseline evals

`agent_baseline_v1.json` is the fixed Phase 0 evaluation set described in
`docs/requirements/agent-design.md` section 24.2. It is intentionally independent
of an Agent SDK, model, Engine process, network access, and user data.

Each case names shared fixtures and records expectations that the deterministic
runner can score without inferring policy from prose:

- `expected.tools` defines exact required calls, the allowed/forbidden tool set,
  and total/Engine call budgets.
- `expected.grounding` defines evidence, chess references, uncertainty, and an
  executable matcher list for required and forbidden claims.
- `expected.personalization` defines whether profile-derived claims are allowed
  and the minimum evidence needed for recurring-weakness language.
- `expected.outcome` defines full, partial, or error completion and the expected
  degradation code.

Every fake tool fixture contains a complete request plus a concrete Pydantic
`ToolResult[ResultDTO]` envelope. Referenced critical positions are the exact
`fen_before` obtained by legally replaying plies `1..N-1` for `critical_id=ply-N`.

`observed_fake_runs_v1.json` is the fixed Phase 0 observation set. `evaluator.py`
scores all documented metrics without a model, Engine process, filesystem state,
or network access. `baseline_report.json` is the reproducible aggregate output.

The backend tests validate DTO schemas, timeline replay, fixture ownership,
matcher behavior, scorer sensitivity to regressions, and exact report
reproduction:

```bash
.venv/bin/python -m unittest \
  tests.backend.test_agent_eval_dataset \
  tests.backend.test_agent_eval_runner
```
