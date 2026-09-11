import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createReviewGraph } from "../../frontend/modules/review/graph.js";
import { createReviewNavigation } from "../../frontend/modules/review/navigation.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function createHarness({
  evaluate = async () => ({}),
  timeline: suppliedTimeline,
  onNavUpdate,
  onFreeAnalysisLine,
} = {}) {
  const elements = new Map();
  const $ = (id) => {
    if (!elements.has(id)) {
      elements.set(id, {
        checked: false,
        className: "",
        innerHTML: "",
        onclick: null,
        style: {},
        textContent: "",
      });
    }
    return elements.get(id);
  };
  const timeline = suppliedTimeline || [
    { fen: "fen-0", move_uci: "a2a3", move_san: "a3", win_white: 50 },
    { fen: "fen-1", move_uci: "b7b6", move_san: "b6", win_white: 51 },
    { fen: "fen-2", move_uci: "c2c3", move_san: "c3", win_white: 52 },
    { fen: "fen-3", win_white: 53 },
  ];
  const boardStates = [];
  const chess = {
    initialFen: timeline[0]?.fen || "fen-0",
    currentFen: timeline[0]?.fen || "fen-0",
    moves: [],
    fen() { return this.currentFen; },
    history() { return this.moves.slice(); },
    inCheck: () => false,
    load(fen) {
      this.currentFen = fen;
      this.moves = [];
    },
    undo() {
      const move = this.moves.pop();
      if (move) this.currentFen = move.before;
      return move || null;
    },
    reset() {
      this.currentFen = this.initialFen;
      this.moves = [];
    },
  };
  const critical = {
    critical_id: "ply-3",
    ply: 3,
    played_move: { uci: "c2c3" },
    best_line: { uci: ["c2c4"] },
  };
  const navigation = createReviewNavigation({
    $,
    board: {
      chess,
      ground: {
        set(config) { boardStates.push(config); },
        setAutoShapes() {},
      },
      computeDests: () => new Map(),
      isPromotion: () => false,
      tryMove({ from, to, promotion }) {
        const sans = { e2e4: "e4", e7e5: "e5", g1f3: "Nf3" };
        const move = {
          before: chess.currentFen,
          from,
          to,
          promotion,
          san: sans[`${from}${to}`] || `${from}-${to}`,
        };
        chess.moves.push(move);
        chess.currentFen = `after-${from}-${to}`;
        return move;
      },
      turnColor: () => "white",
    },
    api: {
      evaluate,
      bestMove: async () => ({ side_to_move: "white", win_percent: 50 }),
    },
    getTimeline: () => timeline,
    getAnalyzing: () => false,
    getActiveCritical: () => critical,
    getRetrySession: () => null,
    onRetryMove() {},
    isVariationActive: () => false,
    stopVariation() {},
    setChatContext() {},
    onSelectCritical() {},
    onGraphRender() {},
    onNotationHighlight() {},
    onReviewCursorSync() {},
    onNavUpdate: onNavUpdate || (() => {}),
    onFreeAnalysisLine,
  });
  navigation.patch({ anchorNode: 2, currentMistake: 0 });
  return { boardStates, chess, navigation };
}

test("key-position navigation highlights the move that actually reached the board FEN", () => {
  const { boardStates, navigation } = createHarness();

  navigation.gotoNode(2);
  assert.deepEqual(boardStates.at(-1).lastMove, ["b7", "b6"]);

  navigation.stepBack();
  assert.deepEqual(boardStates.at(-1).lastMove, ["a2", "a3"]);
});

test("without an imported game, a move from the initial position can be undone", async () => {
  let resolveEvaluation;
  const evaluation = new Promise((resolve) => { resolveEvaluation = resolve; });
  let navUpdates = 0;
  const { chess, navigation } = createHarness({
    evaluate: () => evaluation,
    timeline: [],
    onNavUpdate: () => { navUpdates += 1; },
  });

  const pendingMove = navigation.handleMove("e2", "e4");
  await Promise.resolve();
  assert.equal(navigation.exploring, true);
  assert.equal(navUpdates, 1, "the Previous move button must be re-enabled");

  navigation.stepBack();
  assert.equal(chess.fen(), "fen-0");
  assert.equal(navigation.exploring, false);
  assert.equal(navUpdates, 2);

  resolveEvaluation({ move: { classification: "best" } });
  await pendingMove;
  assert.equal(chess.fen(), "fen-0", "a late evaluation must not restore the explored move");
});

function moveEvaluation(san, overrides = {}) {
  return {
    move: {
      move_san: san,
      classification: "best",
      refutation_line_san: [],
      is_engine_best: true,
      win_before: 50,
      win_after: 51,
      win_swing: 1,
      eval_after: "+0.10",
      better_move_san: san,
      ...overrides,
    },
    shapes: [],
  };
}

test("free analysis tracks one SAN line and evaluates each move from its prior FEN", async () => {
  const calls = [];
  const lines = [];
  const { chess, navigation } = createHarness({
    timeline: [],
    evaluate: async (body) => {
      calls.push(body);
      const san = { e2e4: "e4", e7e5: "e5", g1f3: "Nf3" }[body.move];
      return moveEvaluation(san);
    },
    onFreeAnalysisLine: (line, ply) => lines.push({ line, ply }),
  });

  navigation.enterFreeAnalysis();
  assert.equal(navigation.freeAnalysis, true);
  assert.deepEqual(lines.at(-1), { line: "Start position", ply: 0 });

  await navigation.handleMove("e2", "e4");
  await navigation.handleMove("e7", "e5");
  await navigation.handleMove("g1", "f3");

  assert.deepEqual(calls, [
    { fen: "fen-0", move: "e2e4" },
    { fen: "after-e2-e4", move: "e7e5" },
    { fen: "after-e7-e5", move: "g1f3" },
  ]);
  assert.deepEqual(lines.at(-1), { line: "1. e4 e5 2. Nf3", ply: 3 });

  navigation.stepBack();
  assert.deepEqual(lines.at(-1), { line: "1. e4 e5", ply: 2 });
  navigation.stepBack();
  assert.deepEqual(lines.at(-1), { line: "1. e4", ply: 1 });
  navigation.resetFreeAnalysis();
  assert.equal(chess.fen(), "fen-0");
  assert.deepEqual(lines.at(-1), { line: "Start position", ply: 0 });
});

test("reset and exit ignore a late free-analysis evaluation", async () => {
  let resolveEvaluation;
  const evaluation = new Promise((resolve) => { resolveEvaluation = resolve; });
  const lines = [];
  const { chess, navigation } = createHarness({
    timeline: [],
    evaluate: () => evaluation,
    onFreeAnalysisLine: (line) => lines.push(line),
  });

  navigation.enterFreeAnalysis();
  const pendingMove = navigation.handleMove("e2", "e4");
  navigation.resetFreeAnalysis();
  navigation.exitFreeAnalysis();
  chess.load("opened-game-fen");
  resolveEvaluation(moveEvaluation("e4", { classification: "blunder" }));
  await pendingMove;

  assert.equal(navigation.freeAnalysis, false);
  assert.equal(chess.fen(), "opened-game-fen");
  assert.equal(lines.at(-1), "Start position");
});

test("board controls label key-position navigation explicitly", async () => {
  const html = await readFile(path.join(root, "frontend", "index.html"), "utf8");
  assert.match(html, /id="prev-mistake"[^>]*>‹ Previous key position<\/button>/);
  assert.match(html, /id="next-mistake"[^>]*>Next key position ›<\/button>/);
  assert.doesNotMatch(html, />‹ Key<\/button>|>Key ›<\/button>/);
  assert.doesNotMatch(html, /id="free-analysis"/);
  assert.match(html, /id="free-analysis-line"[^>]*>Start position<\/span>/);
});

test("win-chance timeline belongs to navigation beside the board", async () => {
  const html = await readFile(path.join(root, "frontend", "index.html"), "utf8");
  const board = html.indexOf('<div class="board-wrap">');
  const controls = html.indexOf('<div class="controls">', board);
  const timeline = html.indexOf('<div id="timeline-meta"');
  const graph = html.indexOf('<div id="graph-wrap"');

  assert.ok(board >= 0);
  assert.ok(controls > board);
  assert.ok(timeline >= 0);
  assert.ok(graph > timeline);
  const navigation = html.indexOf('<aside class="navigation-col">');
  const analysis = html.indexOf('<aside class="side-col"');
  assert.ok(graph > navigation && graph < analysis);
  assert.ok(html.indexOf('id="critical-review"') > analysis);
  assert.ok(html.indexOf('id="chat-form"') > analysis);
  for (const id of ["timeline-meta", "timeline-readout", "graph-wrap", "graph", "analysis-progress"]) {
    assert.equal(html.split(`id="${id}"`).length - 1, 1, `${id} must remain unique`);
  }
});

test("Games is a drawer on wide screens too", async () => {
  const css = await readFile(path.join(root, "frontend", "styles.css"), "utf8");
  assert.match(css, /\.history-col\s*\{\s*position: fixed;/);
});

test("win-chance timeline stays hidden until a game timeline exists", () => {
  const elements = {
    graph: { innerHTML: "stale", setAttribute() {} },
    "graph-wrap": { toggleAttribute(name, force) { this[name] = force; } },
    "timeline-meta": { toggleAttribute(name, force) { this[name] = force; } },
    "timeline-readout": { textContent: "" },
  };
  let timeline = [];
  const graph = createReviewGraph({
    $: (id) => elements[id],
    getSnapshot: () => ({
      timeline,
      cur: 0,
      orient: "white",
      engineReview: null,
      criticalPositions: [],
    }),
    onGotoNode() {},
    onSelectCritical() {},
    onSelectMistake() {},
  });

  graph.render();
  assert.equal(elements["timeline-meta"].hidden, true);
  assert.equal(elements["graph-wrap"].hidden, true);
  assert.equal(elements.graph.innerHTML, "");

  timeline = [
    { node: 0, win_white: null },
    { node: 1, win_white: null },
  ];
  graph.render();
  assert.equal(elements["timeline-meta"].hidden, false);
  assert.equal(elements["graph-wrap"].hidden, false);
  assert.match(elements.graph.innerHTML, /analyzing/);
});
