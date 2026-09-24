import assert from "node:assert/strict";
import test from "node:test";
import { createReviewSummaryView } from "../../frontend/modules/review/summary-view.js";
import { createWorkspaceView } from "../../frontend/modules/review/workspace-view.js";
import { createReviewVariation } from "../../frontend/modules/review/variation.js";
import { classGlyph, MOVE_CLASSES } from "../../frontend/modules/review/helpers.js";

function elements() {
  const map = new Map();
  return (id) => {
    if (!map.has(id)) map.set(id, { innerHTML: "", querySelectorAll: () => [] });
    return map.get(id);
  };
}

test("both-side statistics show all labels and distinguish old and incomplete analysis", () => {
  const $ = elements();
  const view = createReviewSummaryView({ $, getSnapshot: () => ({}), onSelectMistake() {} });
  const session = { player: "white", accuracy_white: 90, accuracy_black: 80, mistakes: [],
    classification_version: 1, classification_summary: {
      classifications_by_side: { white: { brilliant: 2 }, black: { brilliant: 1 } },
      positive_verification: "complete",
    } };
  view.renderScoreboard(session);
  assert.match($("scoreboard").innerHTML, /Brilliant<\/th><td>2<\/td><td>1<\/td>/);
  for (const label of MOVE_CLASSES) assert.ok(classGlyph(label));
  assert.doesNotMatch($("scoreboard").innerHTML, /Reanalyze/);
  session.classification_summary.positive_verification = "incomplete";
  view.renderScoreboard(session);
  assert.match($("scoreboard").innerHTML, /verification incomplete/);
  delete session.classification_version;
  view.renderScoreboard(session);
  assert.match($("scoreboard").innerHTML, /Reanalyze to identify highlights/);
  assert.doesNotMatch($("scoreboard").innerHTML, /<table/);
});

test("Engine highlights explain the award without AI and restore error actions on navigation", () => {
  const $ = elements();
  const highlight = { critical_id: "ply-1", ply: 1, move_number: 1, side: "white", classification: "brilliant",
    played_move: { san: "Ng5" }, best_line: { san: ["Ng5"], uci: ["f3g5"] },
    played_line: { san: ["Ng5"], uci: ["f3g5"] },
    classification_reason: { sacrifice: { piece: "N", material_invested: 3,
      line: { san: ["Ng5", "hxg5", "Kf2"], uci: ["f3g5", "h6g5", "e1f2"] } } }, facts: {},
  };
  const error = { ...highlight, critical_id: "ply-3", classification: "mistake", classification_reason: null };
  const view = createWorkspaceView({ $, getSnapshot: () => ({ criticalPositions: [highlight, error] }),
    setWorkflowState() {}, onSelectCritical() {}, onSelectEngineMove() {}, wireVariationLinks() {} });
  view.renderCritical(highlight);
  assert.match($("explanation-content").innerHTML, /What worked/);
  assert.match($("explanation-content").innerHTML, /sound N sacrifice/);
  assert.match($("explanation-content").innerHTML, /data-variation="sacrifice"/);
  assert.doesNotMatch($("explanation-content").innerHTML, /Core problem|Error category/);
  assert.equal($("retry-critical").hidden, true);
  assert.equal($("train-critical").hidden, true);
  view.renderCritical(error);
  assert.match($("explanation-content").innerHTML, /Core problem/);
  assert.equal($("retry-critical").hidden, false);
  assert.equal($("train-critical").hidden, false);
});

test("sacrifice playback uses its verified line and ignores a stale animation after switching", (t) => {
  const $ = elements();
  const previousDocument = globalThis.document;
  globalThis.document = { querySelectorAll: () => [] };
  t.after(() => { globalThis.document = previousDocument; });
  const timers = [];
  t.mock.method(globalThis, "setInterval", (callback) => { timers.push(callback); return timers.length; });
  t.mock.method(globalThis, "clearInterval", () => {});
  let loaded = "";
  const board = { chess: { load: (fen) => { loaded = fen; }, fen: () => loaded },
    createGame: () => { const moves = []; return {
      move: ({ from, to }) => { moves.push(from + to); return { san: from + to }; },
      fen: () => moves.join(" "),
    }; } };
  const critical = { ply: 1, fen_before: "start", best_line: { uci: ["e2e4"] },
    played_line: { uci: ["d2d4"] }, classification_reason: { sacrifice: { line: { uci: ["f3g5", "h6g5"] } } } };
  const variation = createReviewVariation({ $, board, getActiveCritical: () => critical,
    setContext() {}, renderBoard() {}, updateStatus() {} });
  variation.start("sacrifice", 2);
  assert.equal(loaded, "f3g5 h6g5");
  assert.match($("variation-label").textContent, /Sacrifice accepted/);
  variation.start("sacrifice", 0, true);
  variation.start("best", 0, true);
  timers[0]();
  assert.equal(loaded, "");
  timers[1]();
  assert.equal(loaded, "e2e4");
  variation.stop();
});
