import assert from "node:assert/strict";
import test from "node:test";

import { gamesApi } from "../../frontend/modules/api/games.js";
import { puzzleApi } from "../../frontend/modules/api/puzzles.js";
import { createGamesLibrary } from "../../frontend/modules/games/library.js";
import { createGamesController } from "../../frontend/modules/games/controller.js";
import { createPuzzleController } from "../../frontend/modules/puzzles/controller.js";
import { createPuzzleBoardView } from "../../frontend/modules/puzzles/board-view.js";
import { createPuzzleStorm } from "../../frontend/modules/puzzles/storm.js";
import { createReviewVariation } from "../../frontend/modules/review/variation.js";

class FakeClassList {
  values = new Set();
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : force;
    if (enabled) this.values.add(name);
    else this.values.delete(name);
    return enabled;
  }
  contains(name) { return this.values.has(name); }
}

class FakeElement {
  constructor() {
    this.children = [];
    this.classList = new FakeClassList();
    this.style = {};
    this.dataset = {};
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.scrollTop = 0;
    this.value = "";
    this._innerHTML = "";
    this.textContent = "";
  }
  set innerHTML(value) {
    this._innerHTML = value;
    this.children = [];
    this.queries = new Map();
    if (String(value).includes("h-delete")) this.queries.set(".h-delete", new FakeElement());
  }
  get innerHTML() { return this._innerHTML; }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  appendChild(child) { this.children.push(child); return child; }
  querySelector(selector) { return this.queries && this.queries.get(selector) || null; }
  querySelectorAll() { return []; }
  setAttribute(name, value) { this[name] = value; }
  focus() {}
  remove() { this.removed = true; }
  emit(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
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

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
}

test("opening PGN import reveals and focuses the existing form without leaving free analysis", () => {
  const originalDocument = globalThis.document;
  const { $ } = elementLookup();
  globalThis.document = { getElementById: $ };
  let opened = 0;
  let focused = 0;
  $("paste-pgn").value = "1. e4 e5 *";
  $("paste-pgn").focus = () => { focused += 1; };
  try {
    const games = createGamesController({ bridge: {
      showHistory: () => { opened += 1; },
      review: { exitFreeAnalysis: () => assert.fail("Opening the form must preserve the board") },
    } });
    games.openImport();
    games.openImport();
    assert.equal(opened, 2);
    assert.equal(focused, 2);
    assert.equal($("paste-form").style.display, "flex");
    assert.equal($("mode-paste").classList.contains("active"), true);
    assert.equal($("history-list").style.display, "none");
    assert.equal($("paste-pgn").value, "1. e4 e5 *");
  } finally {
    globalThis.document = originalDocument;
  }
});

test("variation playback reports every rendered board position", () => {
  const originalDocument = globalThis.document;
  const { $ } = elementLookup();
  globalThis.document = { querySelectorAll: () => [] };
  let currentFen = "";
  const positions = [];
  const variation = createReviewVariation({
    $,
    board: {
      chess: {
        load: (fen) => { currentFen = fen; },
        fen: () => currentFen,
      },
      createGame: (fen) => ({
        fen: () => fen,
        move: () => ({ san: "e4" }),
      }),
    },
    getActiveCritical: () => ({
      ply: 1,
      fen_before: "before-fen",
      best_line: { uci: ["e2e4"] },
    }),
    setContext() {},
    renderBoard() {},
    updateStatus() {},
    onPositionChange: (fen) => positions.push(fen),
  });
  try {
    variation.start("best", 1, false);
    assert.deepEqual(positions, ["before-fen"]);
  } finally {
    variation.stop();
    globalThis.document = originalDocument;
  }
});

test("puzzle board reset reports the restored position", () => {
  const { $ } = elementLookup();
  let fen = "initial-fen";
  const positions = [];
  const boardView = createPuzzleBoardView({
    $,
    board: {
      chess: {
        fen: () => fen,
        inCheck: () => false,
        load: (value) => { fen = value; },
        reset: () => { fen = "initial-fen"; },
      },
      ground: { set() {} },
      setShapes() {},
      turnColor: () => "white",
      computeDests: () => new Map(),
    },
    onPositionChange: (value) => positions.push(value),
  });

  boardView.prepare("puzzle-fen");
  boardView.reset();

  assert.deepEqual(positions, ["puzzle-fen", "initial-fen"]);
});

test("deleting a local game redraws the cached library page", async () => {
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  const originalHistory = gamesApi.history;
  const originalDelete = gamesApi.deleteGame;
  const { $, elements } = elementLookup();
  globalThis.document = { createElement: () => new FakeElement() };
  globalThis.window = { confirm: () => true };
  gamesApi.history = async () => ({
    player_id: "alice",
    games: [{
      game_id: "g1",
      reviewed_side: "white",
      black: "Bob",
      has_pgn: true,
      pgn: "1. e4",
      counts: {},
    }],
  });
  gamesApi.deleteGame = async () => ({ attempts_removed: 2 });
  let changes = 0;
  try {
    const library = createGamesLibrary({
      $,
      bridge: { review: { openGame() {} } },
      getIdentity: () => ({}),
      onHistoryChanged: () => { changes += 1; },
      onRemoteAccount() {},
    });
    await library.loadHistory();
    const list = elements.get("history-list");
    assert.equal(list.children.length, 1);
    const deleteButton = list.children[0].querySelector(".h-delete");
    deleteButton.emit("click", { stopPropagation() {} });
    await Promise.resolve();
    await Promise.resolve();
    assert.equal(list.children.length, 0);
    assert.match(elements.get("history-status").textContent, /Deleted game/);
    assert.equal(changes, 2);
  } finally {
    gamesApi.history = originalHistory;
    gamesApi.deleteGame = originalDelete;
    globalThis.document = originalDocument;
    globalThis.window = originalWindow;
  }
});

test("rapid puzzle entry and exit ignores the superseded config response", async () => {
  const originalDocument = globalThis.document;
  const originalLocalStorage = globalThis.localStorage;
  const originalSessionStorage = globalThis.sessionStorage;
  const originalAnimationFrame = globalThis.requestAnimationFrame;
  const originalConfig = puzzleApi.config;
  const originalCategories = puzzleApi.categories;
  const { $ } = elementLookup();
  const body = new FakeElement();
  globalThis.document = {
    body,
    createElement: () => new FakeElement(),
    getElementById: $,
  };
  globalThis.localStorage = memoryStorage();
  globalThis.sessionStorage = memoryStorage();
  globalThis.requestAnimationFrame = (callback) => callback();
  let resolveConfig;
  puzzleApi.config = () => new Promise((resolve) => { resolveConfig = resolve; });
  let categoryRequests = 0;
  puzzleApi.categories = async () => { categoryRequests += 1; return { categories: [] }; };
  const chess = {
    fen: () => "start",
    inCheck: () => false,
    load() {},
    reset() {},
  };
  const board = {
    chess,
    ground: { set() {} },
    redraw() {},
    setShapes() {},
    turnColor: () => "white",
    computeDests: () => new Map(),
  };
  const events = [];
  try {
    const controller = createPuzzleController({
      board,
      lifecycle: {
        layoutChanged() {},
        positionResizer() {},
        enter: () => events.push("enter"),
        leave: () => events.push("leave"),
      },
    });
    const entering = controller.setMode(true);
    await Promise.resolve();
    await controller.setMode(false);
    resolveConfig({ enabled: true, has_engine: true });
    await entering;
    assert.equal(controller.active, false);
    assert.deepEqual(events, ["enter", "leave"]);
    assert.equal(categoryRequests, 0);

    let categoriesAborted = false;
    puzzleApi.categories = ({ signal }) => new Promise((resolve, reject) => {
      signal.addEventListener("abort", () => {
        categoriesAborted = true;
        reject(new DOMException("Aborted", "AbortError"));
      });
    });
    const enteringWithConfig = controller.setMode(true);
    await Promise.resolve();
    await controller.setMode(false);
    await enteringWithConfig;
    assert.equal(categoriesAborted, true);
    assert.equal(controller.active, false);
  } finally {
    puzzleApi.config = originalConfig;
    puzzleApi.categories = originalCategories;
    globalThis.document = originalDocument;
    globalThis.localStorage = originalLocalStorage;
    globalThis.sessionStorage = originalSessionStorage;
    globalThis.requestAnimationFrame = originalAnimationFrame;
  }
});

test("Storm review renders filtered themes and opens the selected puzzle", async () => {
  const originalDocument = globalThis.document;
  const originalStart = puzzleApi.stormStart;
  const originalMove = puzzleApi.stormMove;
  const { $, elements } = elementLookup();
  globalThis.document = { createElement: () => new FakeElement() };
  const entry = {
    id: "p1",
    fen: "storm-fen",
    solved: false,
    your_move: "e2e4",
    themes: ["oneMove", "fork"],
    rating: 1200,
  };
  puzzleApi.stormStart = async () => ({
    remaining: 30,
    score: 0,
    combo: 0,
    puzzle: { fen: "storm-fen", side_to_move: "white" },
  });
  puzzleApi.stormMove = async () => ({ ended: true, score: 0, log: [entry] });
  let reviewed = null;
  const trainer = {
    cancel() {},
    cancelAutoAdvance() {},
    loadNext() {},
    openStormReview: (value) => { reviewed = value; },
  };
  const storm = createPuzzleStorm({
    $,
    board: {
      chess: { load() {}, undo() {}, move() {} },
      isPromotion: () => false,
      tryMove: () => true,
    },
    boardView: {
      blink() {}, render() {}, reset() {}, setLastMove() {}, setOrientation() {},
      setShapes() {}, shake() {},
    },
    getConfig: () => ({}),
    isPuzzleActive: () => true,
    solution: { clear() {} },
    trainer,
  });
  try {
    storm.setShown(true);
    await storm.start();
    await storm.handleMove("e2", "e4");
    const list = elements.get("pz-storm-review-list");
    assert.equal(list.children.length, 1);
    assert.doesNotMatch(list.children[0].innerHTML, /oneMove/);
    assert.match(list.children[0].innerHTML, /fork/);
    list.children[0].emit("click");
    assert.equal(reviewed, entry);
  } finally {
    storm.end();
    puzzleApi.stormStart = originalStart;
    puzzleApi.stormMove = originalMove;
    globalThis.document = originalDocument;
  }
});
