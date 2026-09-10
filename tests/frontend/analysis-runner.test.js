import assert from "node:assert/strict";
import test from "node:test";

import { createAnalysisRunner } from "../../frontend/modules/review/analysis-runner.js";
import { createReviewArtifacts } from "../../frontend/modules/review/artifacts.js";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function waitFor(predicate, timeoutMs = 250) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("Timed out waiting for condition");
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
}

function createHarness(apiOverrides = {}, runnerOverrides = {}) {
  const elements = new Map();
  const ready = [];
  const api = {
    analyze: async () => ({ status: "pending" }),
    analysisStatus: async () => ({ status: "ready" }),
    session: async () => ({ game_id: "game", empty: false }),
    timeline: async () => ({ nodes: [] }),
    ...apiOverrides,
  };
  const runner = createAnalysisRunner({
    $: (id) => {
      if (!elements.has(id)) elements.set(id, { textContent: "" });
      return elements.get(id);
    },
    api,
    bridge: {
      closeHistory() {},
      isLocalHistory: () => false,
      loadHistory() {},
      activateLocalHistory() {},
    },
    getDefaultReviewSide: () => "auto",
    beginProvisional() {},
    applyReady: async (session, timeline) => { ready.push({ session, timeline }); },
    reportError() {},
    renderProgress() {},
    pollDelayMs: 2,
    ...runnerOverrides,
  });
  return { runner, ready };
}

test("analysis polling waits for the current request before scheduling another", async () => {
  const firstStatus = deferred();
  let statusCalls = 0;
  const { runner, ready } = createHarness({
    analysisStatus: async () => {
      statusCalls += 1;
      return statusCalls === 1 ? firstStatus.promise : { status: "ready" };
    },
  });

  await runner.openGame("1. e4", "white");
  await waitFor(() => statusCalls === 1);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(statusCalls, 1);

  firstStatus.resolve({ status: "pending" });
  await waitFor(() => ready.length === 1);
  assert.equal(statusCalls, 2);
});

test("a late ready application cannot finish after a newer open begins", async () => {
  const releaseOld = deferred();
  const oldStarted = deferred();
  let sessionCalls = 0;
  const applied = [];
  const { runner } = createHarness(
    {
      analyze: async () => ({ status: "ready" }),
      session: async () => ({
        game_id: ++sessionCalls === 1 ? "old-game" : "new-game",
        empty: false,
      }),
    },
    {
      applyReady: async (session, _timeline, operation) => {
        if (session.game_id === "old-game") {
          oldStarted.resolve();
          await releaseOld.promise;
        }
        if (operation.isCurrent()) applied.push(session.game_id);
      },
    }
  );

  const oldOpen = runner.openGame("old", "white");
  await oldStarted.promise;
  await runner.openGame("new", "black");
  releaseOld.resolve();
  await oldOpen;

  assert.deepEqual(applied, ["new-game"]);
});

test("a reset prevents a late artifact response from replacing the new game", async () => {
  const oldAnalysis = deferred();
  const snapshot = {
    currentGameId: null,
    engineReview: null,
    criticalPositions: [],
    explanationArtifact: null,
    activeCriticalId: null,
  };
  const artifacts = createReviewArtifacts({
    $: () => ({ disabled: false, hidden: false }),
    api: {
      analysis: async () => oldAnalysis.promise,
      explanations: async () => ({ positions: [] }),
    },
    getSnapshot: () => snapshot,
    setState: (values) => Object.assign(snapshot, values),
    setWorkflowState() {},
    renderList() {},
    refreshView() {},
    renderGraph() {},
    renderCritical() {},
  });

  const loading = artifacts.load("old-game", "white");
  artifacts.reset("new-game");
  oldAnalysis.resolve({ critical_positions: [{ critical_id: "old-critical" }] });
  assert.equal(await loading, false);
  assert.equal(snapshot.currentGameId, "new-game");
  assert.equal(snapshot.engineReview, null);
  assert.deepEqual(snapshot.criticalPositions, []);
});

test("a superseded poll cannot load ready data for the previous game", async () => {
  const oldStatus = deferred();
  let statusCalls = 0;
  let sessionCalls = 0;
  const { runner, ready } = createHarness({
    analysisStatus: async () => {
      statusCalls += 1;
      return statusCalls === 1 ? oldStatus.promise : { status: "ready" };
    },
    session: async () => {
      sessionCalls += 1;
      return { game_id: "new-game", empty: false };
    },
  });

  await runner.openGame("old", "white");
  await waitFor(() => statusCalls === 1);
  await runner.openGame("new", "black");
  oldStatus.resolve({ status: "ready" });

  await waitFor(() => ready.length === 1);
  assert.equal(sessionCalls, 1);
  assert.equal(ready[0].session.game_id, "new-game");
});

test("cancel stops polling and prevents a late ready response from applying", async () => {
  const status = deferred();
  let statusCalls = 0;
  const { runner, ready } = createHarness({
    analysisStatus: async () => {
      statusCalls += 1;
      return status.promise;
    },
  });

  await runner.openGame("old", "white");
  await waitFor(() => statusCalls === 1);
  runner.cancel();
  status.resolve({ status: "ready" });
  await new Promise((resolve) => setTimeout(resolve, 5));

  assert.deepEqual(ready, []);
  assert.equal(statusCalls, 1);
});
