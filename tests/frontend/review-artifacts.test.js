import assert from "node:assert/strict";
import test from "node:test";
import { createWorkspaceView } from "../../frontend/modules/review/workspace-view.js";
import { ApiError } from "../../frontend/modules/core/http.js";
import { createReviewArtifacts } from "../../frontend/modules/review/artifacts.js";

function harness({ generate, realView = false } = {}) {
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  };
  const positions = [1, 2, 3].map((id) => ({ critical_id: `p${id}` }));
  let snapshot = { currentGameId: "game", player: "white", activeCritical: positions[1],
    criticalPositions: positions, explanationArtifact: { positions: [{ critical_id: "p1" }] } };
  const calls = [];
  const view = createWorkspaceView({ $, getSnapshot: () => snapshot, wireVariationLinks() {} });
  const artifacts = createReviewArtifacts({ $, getSnapshot: () => snapshot,
    setState: (patch) => { snapshot = { ...snapshot, ...patch }; },
    api: { generateExplanations: async (game, request) => {
      calls.push(request);
      if (generate) return generate(request);
      return { artifact: { positions: [...snapshot.explanationArtifact.positions,
        { critical_id: request.critical_id }] } };
    } },
    setWorkflowState() {}, renderList() {}, refreshView() {}, renderGraph() {}, renderCritical: realView ? view.renderCritical : () => {},
  });
  return { artifacts, calls, $, view, getSnapshot: () => snapshot,
    select: (id) => { snapshot.activeCritical = positions.find((p) => p.critical_id === id); view.renderCritical(snapshot.activeCritical); } };
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


test("explanation failure survives the real view rendering and navigation", async () => {
  const h = harness({ realView: true, generate: async () => { throw new Error("Model unavailable"); } });
  await h.artifacts.generateExplanations();
  assert.equal(h.$("explanation-status").textContent, "Model unavailable");
  h.select("p3");
  assert.equal(h.$("explanation-status").textContent, "");
  h.select("p2");
  assert.equal(h.$("explanation-status").textContent, "Model unavailable");
});


test("authentication failure stops the remaining batch", async () => {
  const h = harness({ generate: async () => { throw new ApiError("Check explanation key", {
    status: 503, payload: { error: { reason: "authentication_failed" } },
  }); } });
  await h.artifacts.generateExplanations({ all: true });
  assert.equal(h.calls.length, 1);
  assert.match(h.getSnapshot().explanationStatuses.p2, /Batch stopped; 2 positions/);
  assert.equal(h.$("generate-explanation").disabled, false);
  assert.equal(h.$("generate-explanations-all").disabled, false);
});

test("ordinary failures continue the batch and retry success clears the error", async () => {
  let fail = true;
  const h = harness({ generate: async () => {
    if (fail) throw new Error("timeout");
    return {};
  } });
  await h.artifacts.generateExplanations({ all: true });
  assert.equal(h.calls.length, 2);
  assert.equal(h.getSnapshot().explanationStatuses.p2, "timeout");
  assert.equal(h.getSnapshot().explanationStatuses.p3, "timeout");
  fail = false;
  await h.artifacts.generateExplanations();
  assert.equal(h.getSnapshot().explanationStatuses.p2, "");
  assert.equal(h.getSnapshot().explanationStatuses.p3, "timeout");
});

test("switching games discards the old failure and resets status", async () => {
  let reject;
  const h = harness({ generate: () => new Promise((_, no) => { reject = no; }) });
  const pending = h.artifacts.generateExplanations();
  assert.equal(h.getSnapshot().explanationStatuses.p2, "Generating…");
  h.artifacts.reset("another-game");
  reject(new Error("old failure"));
  await pending;
  assert.deepEqual(h.getSnapshot().explanationStatuses, {});
  assert.equal(h.getSnapshot().currentGameId, "another-game");
});
