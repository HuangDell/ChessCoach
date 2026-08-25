import assert from "node:assert/strict";
import test from "node:test";

import { agentApi } from "../../frontend/modules/api/agent.js";
import { http } from "../../frontend/modules/core/http.js";

test("agent API owns the complete session endpoint contract", async () => {
  const original = { get: http.get, post: http.post, delete: http.delete };
  const calls = [];
  const signal = new AbortController().signal;
  http.get = async (...args) => { calls.push(["GET", ...args]); return {}; };
  http.post = async (...args) => { calls.push(["POST", ...args]); return {}; };
  http.delete = async (...args) => { calls.push(["DELETE", ...args]); return null; };
  try {
    await agentApi.metrics(250, { signal });
    await agentApi.clearRuns({ signal });
    await agentApi.createSession({ game_id: "game-1" }, { signal });
    await agentApi.getSession("session/one", { signal });
    await agentApi.updateContext("session/one", { expected_generation: 2 }, { signal });
    await agentApi.sendMessage("session/one", { message: "Why?", expected_generation: 3 }, { signal });
    await agentApi.deleteSession("session/one", { signal });
  } finally {
    http.get = original.get;
    http.post = original.post;
    http.delete = original.delete;
  }

  assert.deepEqual(calls, [
    ["GET", "/api/agent/metrics", { signal, query: { limit: 250 } }],
    ["DELETE", "/api/agent/runs", { signal }],
    ["POST", "/api/agent/sessions", { game_id: "game-1" }, { signal }],
    ["GET", "/api/agent/sessions/session%2Fone", { signal }],
    ["POST", "/api/agent/sessions/session%2Fone/context", { expected_generation: 2 }, { signal }],
    ["POST", "/api/agent/sessions/session%2Fone/messages", {
      message: "Why?",
      expected_generation: 3,
    }, { signal }],
    ["DELETE", "/api/agent/sessions/session%2Fone", { signal }],
  ]);
});
