import assert from "node:assert/strict";
import test from "node:test";

import { createAnalysisRunner } from "../../frontend/modules/review/analysis-runner.js";

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

function createHarness(apiOverrides = {}) {
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
