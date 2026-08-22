import assert from "node:assert/strict";
import test from "node:test";

import { createReviewChat } from "../../frontend/modules/review/chat.js";

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

class FakeElement {
  constructor() {
    this.children = [];
    this.listeners = new Map();
    this.disabled = false;
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.value = "";
    this.textContent = "";
    this.innerHTML = "";
    this.removed = false;
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  appendChild(child) {
    this.children.push(child);
    this.scrollHeight = this.children.length;
    return child;
  }
  focus() { this.focused = true; }
  remove() { this.removed = true; }
  async emit(type, event = {}) {
    return Promise.all((this.listeners.get(type) || []).map((listener) => listener(event)));
  }
}

function elementLookup() {
  const elements = new Map();
  return {
    $: (id) => {
      if (!elements.has(id)) elements.set(id, new FakeElement());
      return elements.get(id);
    },
    elements,
  };
}

function memoryStorage(entries = {}) {
  const values = new Map(Object.entries(entries));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function waitFor(predicate) {
  for (let index = 0; index < 30; index += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  throw new Error("Timed out waiting for async chat state.");
}

function positionContext(fen = START_FEN) {
  return {
    game_id: "game-1",
    review_side: "white",
    active_ply: 0,
    active_critical_id: "ply-1",
    activity: "game_review",
    position: {
      fen,
      recent_moves_uci: [],
      recent_moves_san: [],
      reference: {
        game_id: "game-1",
        review_side: "white",
        critical_id: "ply-1",
        ply: 0,
        fen,
      },
    },
  };
}

function enabledCapability(overrides = {}) {
  return {
    enabled: true,
    available: true,
    features: { review_chat: true },
    ...overrides,
  };
}

function setupChat({ legacyApi, agentApi, sessionStore, getAgentContext } = {}) {
  const originalDocument = globalThis.document;
  const { $, elements } = elementLookup();
  globalThis.document = { createElement: () => new FakeElement() };
  const chat = createReviewChat({
    $,
    api: legacyApi || {
      chatHistory: async () => ({ messages: [] }),
      chat: async () => ({ answer: "legacy" }),
    },
    agentApi: agentApi || {},
    getBoardFen: () => START_FEN,
    getAgentContext: getAgentContext || (() => positionContext()),
    usePersonalHistory: () => true,
    sessionStore: sessionStore || memoryStorage(),
  });
  chat.mount();
  return {
    $,
    elements,
    chat,
    cleanup: () => { globalThis.document = originalDocument; },
  };
}

function visibleMessages(elements) {
  return (elements.get("chat-messages").children || []).filter((item) => !item.removed);
}

test("legacy chat remains the default unless every Agent feature flag is available", async () => {
  for (const capability of [
    {},
    enabledCapability({ enabled: false }),
    enabledCapability({ available: false }),
    enabledCapability({ features: { review_chat: false } }),
  ]) {
    const legacyCalls = [];
    const agentCalls = [];
    const fixture = setupChat({
      legacyApi: {
        chatHistory: async () => ({ messages: [] }),
        chat: async (body, options) => {
          legacyCalls.push({ body, options });
          return { answer: "Legacy answer", session_id: "legacy-1" };
        },
      },
      agentApi: {
        createSession: async () => { agentCalls.push("create"); },
      },
    });
    try {
      fixture.chat.setAgentCapability(capability);
      fixture.$("chat-input").value = "Why this move?";
      await fixture.$("chat-form").emit("submit", { preventDefault() {} });
      assert.equal(legacyCalls.length, 1);
      assert.equal(legacyCalls[0].body.question, "Why this move?");
      assert.equal(legacyCalls[0].options.signal instanceof AbortSignal, true);
      assert.deepEqual(agentCalls, []);
      assert.equal(visibleMessages(fixture.elements).at(-1).className, "chat-msg bot");
    } finally {
      fixture.cleanup();
    }
  }
});

test("Agent chat restores the browser session, syncs context, and sends the CAS generation", async () => {
  const sessionStore = memoryStorage({ chessAgentSessionId: "agent-1" });
  let serverGeneration = 2;
  const calls = [];
  const messageGate = deferred();
  const fixture = setupChat({
    sessionStore,
    legacyApi: {
      chatHistory: async () => { throw new Error("legacy history should not run"); },
      chat: async () => { throw new Error("legacy chat should not run"); },
    },
    agentApi: {
      getSession: async (id) => {
        calls.push(["get", id]);
        return { session: { session_id: id, generation: serverGeneration } };
      },
      createSession: async () => { throw new Error("stored session should be reused"); },
      updateContext: async (id, body) => {
        calls.push(["context", id, body]);
        assert.equal(body.expected_generation, serverGeneration);
        serverGeneration += 1;
        return { session: { session_id: id, generation: serverGeneration } };
      },
      sendMessage: async (id, body, options) => {
        calls.push(["message", id, body, options]);
        return messageGate.promise;
      },
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    await fixture.chat.restore();
    fixture.$("chat-input").value = "Why not Qxd5?";
    const sending = fixture.$("chat-form").emit("submit", { preventDefault() {} });
    await waitFor(() => calls.some(([kind]) => kind === "message"));
    const message = calls.find(([kind]) => kind === "message");
    assert.deepEqual(message.slice(0, 3), [
      "message",
      "agent-1",
      { message: "Why not Qxd5?", expected_generation: serverGeneration },
    ]);
    assert.equal(message[3].signal instanceof AbortSignal, true);
    assert.equal(fixture.$("chat-send").disabled, true);
    assert.equal(fixture.$("chat-input").disabled, false);

    messageGate.resolve({
      session: { session_id: "agent-1", generation: serverGeneration },
      response: { text: "Qxd5 leaves the queen exposed." },
      tool_calls: [],
    });
    await sending;
    assert.equal(fixture.$("chat-send").disabled, false);
    assert.equal(sessionStore.getItem("chessAgentSessionId"), "agent-1");
    assert.equal(visibleMessages(fixture.elements).at(-1).innerHTML.includes("queen exposed"), true);
    assert.equal(calls.filter(([kind]) => kind === "context").length, 2);
  } finally {
    fixture.cleanup();
  }
});

test("navigation aborts and discards a late Agent response while syncing the new context", async () => {
  let serverGeneration = 0;
  let sentSignal = null;
  const messageGate = deferred();
  const fixture = setupChat({
    agentApi: {
      createSession: async () => ({ session: { session_id: "agent-new", generation: 0 } }),
      getSession: async () => ({
        session: { session_id: "agent-new", generation: serverGeneration },
      }),
      updateContext: async (_id, body) => {
        assert.equal(body.expected_generation, serverGeneration);
        serverGeneration += 1;
        return { session: { session_id: "agent-new", generation: serverGeneration } };
      },
      sendMessage: async (_id, _body, options) => {
        sentSignal = options.signal;
        return messageGate.promise;
      },
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    fixture.$("chat-input").value = "Explain this.";
    const sending = fixture.$("chat-form").emit("submit", { preventDefault() {} });
    await waitFor(() => sentSignal !== null);
    fixture.chat.setMoveContext(START_FEN, "e4");
    assert.equal(sentSignal.aborted, true);
    assert.equal(fixture.$("chat-send").disabled, false);

    messageGate.resolve({
      session: { session_id: "agent-new", generation: serverGeneration },
      response: { text: "stale answer" },
      tool_calls: [],
    });
    await sending;
    await waitFor(() => serverGeneration >= 2);
    assert.equal(
      visibleMessages(fixture.elements).some((message) =>
        message.textContent.includes("stale answer") || message.innerHTML.includes("stale answer")
      ),
      false
    );
  } finally {
    fixture.cleanup();
  }
});

test("context sync reconciles one stale generation and retries with the refreshed session", async () => {
  const expectedGenerations = [];
  let gets = 0;
  const fixture = setupChat({
    sessionStore: memoryStorage({ chessAgentSessionId: "agent-stale" }),
    agentApi: {
      getSession: async () => {
        gets += 1;
        return {
          session: { session_id: "agent-stale", generation: gets === 1 ? 4 : 7 },
        };
      },
      createSession: async () => { throw new Error("session should exist"); },
      updateContext: async (_id, body) => {
        expectedGenerations.push(body.expected_generation);
        if (expectedGenerations.length === 1) throw Object.assign(new Error("stale"), { status: 409 });
        return { session: { session_id: "agent-stale", generation: 8 } };
      },
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    await fixture.chat.restore();
    assert.deepEqual(expectedGenerations, [4, 7]);
    assert.equal(gets, 2);
  } finally {
    fixture.cleanup();
  }
});

test("rapid board changes serialize Agent context compare-and-set updates", async () => {
  let serverGeneration = 0;
  let updateCount = 0;
  let activeUpdates = 0;
  let maxActiveUpdates = 0;
  const gates = [];
  const fixture = setupChat({
    agentApi: {
      createSession: async () => ({ session: { session_id: "agent-queue", generation: 0 } }),
      getSession: async () => ({
        session: { session_id: "agent-queue", generation: serverGeneration },
      }),
      updateContext: async (_id, body) => {
        assert.equal(body.expected_generation, serverGeneration);
        updateCount += 1;
        if (updateCount === 1) {
          serverGeneration += 1;
          return { session: { session_id: "agent-queue", generation: serverGeneration } };
        }
        activeUpdates += 1;
        maxActiveUpdates = Math.max(maxActiveUpdates, activeUpdates);
        const gate = deferred();
        gates.push(gate);
        await gate.promise;
        activeUpdates -= 1;
        serverGeneration += 1;
        return { session: { session_id: "agent-queue", generation: serverGeneration } };
      },
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    await fixture.chat.restore();
    fixture.chat.setMoveContext(START_FEN, "e4");
    await waitFor(() => gates.length === 1);
    fixture.chat.setMoveContext(START_FEN, "d4");
    assert.equal(gates.length, 1);
    gates[0].resolve();
    await waitFor(() => gates.length === 2);
    assert.equal(maxActiveUpdates, 1);
    gates[1].resolve();
    await waitFor(() => activeUpdates === 0 && serverGeneration === 3);
    assert.equal(updateCount, 3);
  } finally {
    fixture.cleanup();
  }
});

test("restore and send share one in-flight Agent session creation", async () => {
  const createGate = deferred();
  let creates = 0;
  const fixture = setupChat({
    agentApi: {
      createSession: async () => {
        creates += 1;
        return createGate.promise;
      },
      updateContext: async (id, body) => ({
        session: { session_id: id, generation: body.expected_generation + 1 },
      }),
      sendMessage: async (id, body) => ({
        session: { session_id: id, generation: body.expected_generation },
        response: { text: "One session." },
        tool_calls: [],
      }),
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    const restoring = fixture.chat.restore();
    fixture.$("chat-input").value = "Explain this.";
    const sending = fixture.$("chat-form").emit("submit", { preventDefault() {} });
    await waitFor(() => creates === 1);
    createGate.resolve({ session: { session_id: "agent-one", generation: 0 } });
    await Promise.all([restoring, sending]);
    assert.equal(creates, 1);
  } finally {
    fixture.cleanup();
  }
});

test("reset deletes the Agent session and clears browser reuse state", async () => {
  const sessionStore = memoryStorage({ chessAgentSessionId: "agent-reset" });
  const deleted = [];
  const fixture = setupChat({
    sessionStore,
    agentApi: {
      getSession: async (id) => ({ session: { session_id: id, generation: 3 } }),
      updateContext: async (id, body) => ({
        session: { session_id: id, generation: body.expected_generation + 1 },
      }),
      deleteSession: async (id) => { deleted.push(id); },
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    await fixture.chat.restore();
    fixture.chat.reset();
    await waitFor(() => deleted.length === 1);
    assert.deepEqual(deleted, ["agent-reset"]);
    assert.equal(sessionStore.getItem("chessAgentSessionId"), "");
  } finally {
    fixture.cleanup();
  }
});

test("reset prevents a late context response from restoring the deleted session", async () => {
  const sessionStore = memoryStorage({ chessAgentSessionId: "agent-late" });
  const lateUpdate = deferred();
  let updates = 0;
  const fixture = setupChat({
    sessionStore,
    agentApi: {
      getSession: async (id) => ({ session: { session_id: id, generation: updates } }),
      updateContext: async (id, body) => {
        updates += 1;
        if (updates === 1) {
          return { session: { session_id: id, generation: body.expected_generation + 1 } };
        }
        return lateUpdate.promise;
      },
      deleteSession: async () => {},
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    await fixture.chat.restore();
    fixture.chat.setMoveContext(START_FEN, "e4", "e2e4");
    await waitFor(() => updates === 2);
    fixture.chat.reset();
    lateUpdate.resolve({ session: { session_id: "agent-late", generation: 2 } });
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(sessionStore.getItem("chessAgentSessionId"), "");
  } finally {
    fixture.cleanup();
  }
});

test("selected review move is included in the canonical context sent before the question", async () => {
  const contexts = [];
  let generation = 0;
  const fixture = setupChat({
    agentApi: {
      createSession: async () => ({ session: { session_id: "agent-move", generation: 0 } }),
      updateContext: async (id, body) => {
        contexts.push(body);
        generation += 1;
        return { session: { session_id: id, generation } };
      },
      sendMessage: async (id, body) => ({
        session: { session_id: id, generation: body.expected_generation },
        response: { text: "The move loses time." },
        tool_calls: [],
      }),
    },
  });
  try {
    fixture.chat.setAgentCapability(enabledCapability());
    fixture.chat.setMoveContext(START_FEN, "e4", "e2e4");
    fixture.$("chat-input").value = "Why is e4 inaccurate?";
    await fixture.$("chat-form").emit("submit", { preventDefault() {} });
    const selected = contexts.at(-1).position;
    assert.equal(selected.fen, START_FEN);
    assert.equal(selected.selected_move_uci, "e2e4");
    assert.equal(selected.selected_move_san, "e4");
  } finally {
    fixture.cleanup();
  }
});
