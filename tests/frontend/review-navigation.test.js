import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createReviewNavigation } from "../../frontend/modules/review/navigation.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function createHarness({ evaluate = async () => ({}), timeline: suppliedTimeline, onNavUpdate } = {}) {
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
      tryMove({ from, to }) {
        const move = { before: chess.currentFen, from, to, san: `${from}-${to}` };
        chess.moves.push(move);
        chess.currentFen = `after-${from}-${to}`;
        return move;
      },
      turnColor: () => "white",
    },
    api: { evaluate },
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

test("board controls label key-position navigation explicitly", async () => {
  const html = await readFile(path.join(root, "frontend", "index.html"), "utf8");
  assert.match(html, /id="prev-mistake"[^>]*>‹ Previous key position<\/button>/);
  assert.match(html, /id="next-mistake"[^>]*>Next key position ›<\/button>/);
  assert.doesNotMatch(html, />‹ Key<\/button>|>Key ›<\/button>/);
});
