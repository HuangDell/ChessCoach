import assert from "node:assert/strict";
import test from "node:test";

import { createLatestRequestScope } from "../../frontend/modules/core/async.js";
import { errorMessage } from "../../frontend/modules/core/errors.js";
import {
  storageGet,
  storageJsonGet,
  storageJsonSet,
  storageSet,
} from "../../frontend/modules/core/storage.js";
import {
  countPgnGames,
  firstPgnFile,
  historyRows,
  resultClass,
  sideForUser,
} from "../../frontend/modules/games/helpers.js";
import {
  formatClock,
  motifThemes,
  squarePosition,
} from "../../frontend/modules/puzzles/helpers.js";

test("latest request scope aborts and invalidates superseded work", () => {
  const scope = createLatestRequestScope();
  const first = scope.begin();
  assert.equal(first.isCurrent(), true);
  const second = scope.begin();
  assert.equal(first.signal.aborted, true);
  assert.equal(first.isCurrent(), false);
  assert.equal(second.isCurrent(), true);
  scope.cancel();
  assert.equal(second.signal.aborted, true);
  assert.equal(second.isCurrent(), false);
});

test("storage helpers tolerate unavailable and malformed storage", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  assert.equal(storageGet(storage, "missing", "fallback"), "fallback");
  assert.equal(storageSet(storage, "name", "Snowie"), true);
  assert.equal(storageGet(storage, "name"), "Snowie");
  assert.equal(storageJsonSet(storage, "data", { ok: true }), true);
  assert.deepEqual(storageJsonGet(storage, "data", null), { ok: true });
  values.set("broken", "{");
  assert.deepEqual(storageJsonGet(storage, "broken", []), []);
  const unavailable = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
  };
  assert.equal(storageGet(unavailable, "x", "safe"), "safe");
  assert.equal(storageSet(unavailable, "x", "y"), false);
  const circular = {};
  circular.self = circular;
  assert.equal(storageJsonSet(storage, "circular", circular), false);
});

test("error messages preserve structured API detail", () => {
  assert.equal(errorMessage({ detail: "Invalid PGN" }), "Invalid PGN");
  assert.equal(errorMessage({ error: { message: "No legal moves" } }), "No legal moves");
  assert.equal(
    errorMessage({ payload: { error: { detail: "Engine unavailable" } } }),
    "Engine unavailable"
  );
  assert.equal(
    errorMessage({ detail: [{ message: "Username is required" }, { msg: "Invalid side" }] }),
    "Username is required; Invalid side"
  );
  assert.equal(errorMessage(null, "Fallback"), "Fallback");
});

test("games helpers normalize results, sides, PGNs, and history responses", () => {
  assert.equal(sideForUser({ white: "Alice", black: "Bob" }, "BOB"), "black");
  assert.equal(sideForUser({ white: "Alice", black: "Bob" }, "Other"), "auto");
  assert.equal(resultClass("loss"), "loss");
  assert.equal(countPgnGames('[Event "One"]\n\n1. e4 e5\n[Event "Two"]'), 2);
  assert.deepEqual(historyRows({ games: [{ game_id: "g1" }] }), [{ game_id: "g1" }]);
  assert.deepEqual(historyRows([{ game_id: "legacy-shape" }]), []);
  const pgn = { name: "game.pgn" };
  assert.equal(firstPgnFile({ files: [{ name: "note.md" }, pgn] }), pgn);
});

test("puzzle helpers filter metadata and format clocks and board squares", () => {
  assert.deepEqual(
    motifThemes(["fork", "oneMove", "master", "mateIn2", "backRankMate"]),
    ["fork", "backRankMate"]
  );
  assert.equal(formatClock(61.1), "1:02");
  assert.equal(formatClock(-5), "0:00");
  assert.deepEqual(squarePosition("a8", "white"), { left: "0%", top: "0%" });
  assert.deepEqual(squarePosition("a8", "black"), { left: "87.5%", top: "87.5%" });
  assert.equal(squarePosition("z9", "white"), null);
});
