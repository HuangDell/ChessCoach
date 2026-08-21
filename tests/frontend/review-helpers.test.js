import assert from "node:assert/strict";
import test from "node:test";

import {
  apiErrorMessage,
  buildProvisionalTimeline,
  classGlyph,
  movesToArrows,
  samePosition,
  scoreLabel,
} from "../../frontend/modules/review/helpers.js";

test("samePosition ignores move clocks but compares playable position fields", () => {
  assert.equal(
    samePosition(
      "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 12 34"
    ),
    true
  );
  assert.equal(
    samePosition(
      "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
      "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR b KQkq - 0 1"
    ),
    false
  );
});

test("movesToArrows emphasizes the best move and omits weak alternatives", () => {
  const arrows = movesToArrows([
    { uci: "e2e4", win_percent: 61 },
    { uci: "d2d4", win_percent: 58 },
    { uci: "g1f3", win_percent: 45 },
  ]);
  assert.equal(arrows.length, 2);
  assert.deepEqual(arrows[0], {
    orig: "e2",
    dest: "e4",
    brush: "green",
    modifiers: { lineWidth: 13 },
  });
  assert.equal(arrows[1].modifiers.lineWidth, 4);
});

test("review labels format engine scores and classifications", () => {
  assert.equal(scoreLabel({ type: "cp", value: 34 }), "+0.34");
  assert.equal(scoreLabel({ type: "mate", value: -3 }), "-M3");
  assert.equal(classGlyph("blunder"), '<span class="glyph blunder">??</span>');
  assert.equal(apiErrorMessage({ message: "bad request" }, "fallback"), "bad request");
});

test("buildProvisionalTimeline maps verbose chess history to review nodes", () => {
  const moves = [
    {
      before: "start-fen",
      after: "after-e4",
      color: "w",
      san: "e4",
      from: "e2",
      to: "e4",
    },
    {
      before: "after-e4",
      after: "after-e5",
      color: "b",
      san: "e5",
      from: "e7",
      to: "e5",
    },
  ];
  const game = {
    loadPgn(pgn) { assert.equal(pgn, "1. e4 e5"); },
    history() { return moves; },
    turn() { return "w"; },
  };
  const timeline = buildProvisionalTimeline({ createGame: () => game }, "1. e4 e5");
  assert.equal(timeline.length, 3);
  assert.equal(timeline[0].move_uci, "e2e4");
  assert.equal(timeline[1].move_number, 1);
  assert.equal(timeline[2].fen, "after-e5");
});
