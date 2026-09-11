import assert from "node:assert/strict";
import test from "node:test";
import { createReviewArtifacts } from "../../frontend/modules/review/artifacts.js";

function harness() {
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  };
  const positions = [1, 2, 3].map((id) => ({ critical_id: `p${id}` }));
  let snapshot = { currentGameId: "game", player: "white", activeCritical: positions[1],
    criticalPositions: positions, explanationArtifact: { positions: [{ critical_id: "p1" }] } };
  const calls = [];
  const artifacts = createReviewArtifacts({ $, getSnapshot: () => snapshot,
    setState: (patch) => { snapshot = { ...snapshot, ...patch }; },
    api: { generateExplanations: async (game, request) => {
      calls.push(request);
      return { artifact: { positions: [...snapshot.explanationArtifact.positions,
        { critical_id: request.critical_id }] } };
    } },
    setWorkflowState() {}, renderList() {}, refreshView() {}, renderGraph() {}, renderCritical() {},
  });
  return { artifacts, calls, $ };
}

test("explain this position does not silently generate other positions", async () => {
  const { artifacts, calls } = harness();
  await artifacts.generateExplanations();
  assert.deepEqual(calls.map((call) => call.critical_id), ["p2"]);
  assert.equal(calls[0].force, false);
  await artifacts.generateExplanations();
  assert.equal(calls[1].force, true);
});

test("explicit bulk explanation only generates missing positions", async () => {
  const { artifacts, calls, $ } = harness();
  await artifacts.generateExplanations({ all: true });
  assert.deepEqual(calls.map((call) => call.critical_id), ["p2", "p3"]);
  assert.ok(calls.every((call) => !call.force));
  assert.equal($("generate-explanations-all").disabled, false);
});
