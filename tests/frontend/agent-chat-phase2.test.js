import assert from "node:assert/strict";
import test from "node:test";

import {
  buildReviewAgentContext,
  buildTrainingAgentContext,
  createReviewChat,
} from "../../frontend/modules/review/chat.js";

const START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const AFTER_E4 = "rnbqkbnr/pppppppp/8/8/8/4P3/PPPP1PPP/RNBQKBNR b KQkq - 0 1";

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
  focus() {}
  remove() { this.removed = true; }
  async emit(type, event = {}) {
    return Promise.all((this.listeners.get(type) || []).map((listener) => listener(event)));
  }
}

function elements() {
  const values = new Map();
  return {
    $: (id) => {
      if (!values.has(id)) values.set(id, new FakeElement());
      return values.get(id);
    },
    values,
  };
}

function memoryStorage(entries = {}) {
  const values = new Map(Object.entries(entries));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
}

function snapshot(overrides = {}) {
  return {
    currentGameId: "game-1",
    player: "white",
    timeline: [
      { fen: START_FEN, move_uci: "e2e4", move_san: "e4" },
      { fen: AFTER_E4, move_uci: "e7e5", move_san: "e5" },
    ],
    criticalPositions: [{
      critical_id: "ply-1",
      ply: 1,
      fen_before: START_FEN,
    }],
    activeCriticalId: "ply-1",
    retryActive: false,
    navigation: { cur: 0, exploring: false, exploreBaseNode: 0 },
    fen: START_FEN,
    ...overrides,
  };
}

test("review contexts separate mainline history from a legal exploration replay", () => {
  const variation = buildReviewAgentContext(snapshot(), {
    mode: "variation",
    fen: AFTER_E4,
    basePly: 0,
    baseFen: START_FEN,
    criticalId: "ply-1",
    explorationMovesUci: ["e2e4"],
    explorationMovesSan: ["e4"],
  });
  assert.equal(variation.game_id, "game-1");
  assert.equal(variation.review_side, "white");
  assert.equal(variation.active_ply, 0);
  assert.equal(variation.active_critical_id, "ply-1");
  assert.equal(variation.activity, "position_analysis");
  assert.equal(variation.position.fen, AFTER_E4);
  assert.equal(variation.position.reference.fen, START_FEN);
  assert.deepEqual(variation.position.recent_moves_uci, []);
  assert.deepEqual(variation.position.exploration_moves_uci, ["e2e4"]);
  assert.deepEqual(variation.position.exploration_moves_san, ["e4"]);

  const retry = buildReviewAgentContext(snapshot({ retryActive: true }), {
    fen: AFTER_E4,
    explorationMovesUci: ["e2e4"],
    explorationMovesSan: ["e4"],
  });
  assert.equal(retry.activity, "retry");
  assert.equal(retry.focus_ref, "retry:ply-1");
  assert.equal(retry.position.fen, AFTER_E4);
  assert.equal(retry.position.reference.fen, START_FEN);
  assert.deepEqual(retry.position.exploration_moves_uci, ["e2e4"]);
  assert.deepEqual(retry.position.exploration_moves_san, ["e4"]);
});

test("training context follows the puzzle board without claiming review ownership", () => {
  assert.deepEqual(buildTrainingAgentContext(AFTER_E4), {
    game_id: null,
    review_side: null,
    active_ply: null,
    active_critical_id: null,
    activity: "training",
    focus_ref: null,
    position: {
      fen: AFTER_E4,
      recent_moves_uci: [],
      recent_moves_san: [],
      reference: { fen: AFTER_E4 },
    },
  });
});

test("Agent response references, actions, and tool summary use injected callbacks", async () => {
  const originalDocument = globalThis.document;
  const { $, values } = elements();
  globalThis.document = { createElement: () => new FakeElement() };
  let serverGeneration = 0;
  const opened = [];
  const actions = [];
  const context = buildReviewAgentContext(snapshot());
  const chat = createReviewChat({
    $,
    getAgentContext: () => context,
    onReference: (reference) => { opened.push(reference); },
    onAction: (action) => { actions.push(action); },
    sessionStore: memoryStorage(),
    agentApi: {
      createSession: async () => ({ session: { session_id: "phase2", generation: 0 } }),
      updateContext: async (id, body) => {
        serverGeneration += 1;
        return { session: { session_id: id, generation: serverGeneration } };
      },
      sendMessage: async (id, body) => ({
        session: { session_id: id, generation: body.expected_generation },
        response: {
          text: "Start with the first key position.",
          references: [{
            kind: "critical_position",
            game_id: "game-1",
            review_side: "white",
            critical_id: "ply-1",
            ply: 1,
            fen: START_FEN,
          }],
          suggested_actions: [{
            kind: "start_retry",
            label: "Retry this position",
            target: { game_id: "game-1", critical_id: "ply-1" },
          }],
        },
        tool_calls: [{
          name: "get_review_context",
          status: "ok",
          cache_hit: true,
        }],
      }),
    },
  });
  try {
    chat.mount();
    $("chat-input").value = "Where should I start?";
    await $("chat-form").emit("submit", { preventDefault() {} });
    const response = values.get("chat-messages").children.find((item) =>
      item.className === "chat-msg bot"
    );
    assert.ok(response);
    const controls = response.children.find((item) => item.className === "chat-response-actions");
    const summary = response.children.find((item) => item.className === "chat-tool-summary");
    assert.equal(controls.children.length, 2);
    assert.equal(summary.children[0].textContent, "1 coach tool");
    assert.match(summary.children[1].textContent, /get_review_context: cached/);
    await controls.children[0].emit("click");
    await controls.children[1].emit("click");
    assert.equal(opened[0].critical_id, "ply-1");
    assert.equal(actions[0].kind, "start_retry");
  } finally {
    globalThis.document = originalDocument;
  }
});

test("restore adopts the persisted session checkpoint and renders its compact summary", async () => {
  const originalDocument = globalThis.document;
  const { $, values } = elements();
  globalThis.document = { createElement: () => new FakeElement() };
  const store = memoryStorage({ chessAgentSessionId: "restored-session" });
  const expectedGenerations = [];
  const chat = createReviewChat({
    $,
    getAgentContext: () => buildReviewAgentContext(snapshot()),
    sessionStore: store,
    agentApi: {
      getSession: async () => ({
        session: {
          session_id: "restored-session",
          generation: 7,
          conversation_summary: "Focus on candidate checks before captures.",
        },
      }),
      updateContext: async (id, body) => {
        expectedGenerations.push(body.expected_generation);
        return { session: { session_id: id, generation: 8 } };
      },
    },
  });
  try {
    await chat.restore();
    assert.equal(chat.sessionId, "restored-session");
    assert.equal(store.getItem("chessAgentSessionId"), "restored-session");
    assert.deepEqual(expectedGenerations, [7]);
    const summary = values.get("chat-messages").children[0];
    assert.equal(summary.className, "chat-msg bot summary");
    assert.match(summary.innerHTML, /candidate checks/);
  } finally {
    globalThis.document = originalDocument;
  }
});
