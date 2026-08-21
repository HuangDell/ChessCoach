export function apiErrorMessage(value, fallback) {
  if (value && typeof value === "object") return value.message || fallback;
  return value || fallback;
}

export function gameUrlFromHeaders(headers) {
  for (const key of ["Site", "Link"]) {
    const value = (headers && headers[key] != null ? String(headers[key]) : "").trim();
    if (/^https?:\/\//i.test(value)) return value;
  }
  return null;
}

export function pgnHeaders(board, pgn) {
  try {
    const game = board.createGame();
    game.loadPgn(pgn);
    const headers = game.header();
    return { white: headers.White, black: headers.Black, url: gameUrlFromHeaders(headers) };
  } catch (_) {
    return {};
  }
}

export function samePosition(fenA, fenB) {
  return fenA.split(" ").slice(0, 4).join(" ") === fenB.split(" ").slice(0, 4).join(" ");
}

export function pieceGlyph(san) {
  if (san.startsWith("O-O")) return "♚";
  return { N: "♞", B: "♝", R: "♜", Q: "♛", K: "♚" }[san[0]] || "♟";
}

export function arrowShape(uci, brush) {
  return { orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush };
}

export function movesToArrows(moves, brush = "green", boldWidth = 13) {
  if (!moves.length) return [];
  const best = moves[0].win_percent;
  const arrows = [];
  for (let index = 0; index < moves.length; index += 1) {
    const delta = best - moves[index].win_percent;
    if (index > 0 && delta > 12) break;
    arrows.push({
      orig: moves[index].uci.slice(0, 2),
      dest: moves[index].uci.slice(2, 4),
      brush,
      modifiers: { lineWidth: index === 0 ? boldWidth : Math.max(4, 7 - delta) },
    });
  }
  return arrows;
}

export function nodeLabel(timeline, index) {
  if (!timeline[index] || index === 0) return "the start";
  const previous = timeline[index - 1];
  return `${previous.move_number}${previous.color === "white" ? "." : "…"} ${previous.move_san}`;
}

export function classColor(classification) {
  return (
    { inaccuracy: "#e0a800", mistake: "#e08000", blunder: "#dd3333" }[classification] ||
    "#629924"
  );
}

const CLASS_GLYPHS = { blunder: "??", mistake: "?", inaccuracy: "?!", best: "✓" };

export function classGlyph(classification) {
  const glyph = CLASS_GLYPHS[classification];
  return glyph ? `<span class="glyph ${classification}">${glyph}</span>` : "";
}

export function reviewMoveLabel(item) {
  const side = item.side || item.color;
  const san = (item.played_move && item.played_move.san) || item.move_san || "—";
  return `${item.move_number}${side === "black" ? "…" : "."} ${san}`;
}

export function scoreLabel(score) {
  if (!score) return "—";
  const value = Number(score.value) || 0;
  if (score.type === "mate") return `${value < 0 ? "-" : ""}M${Math.abs(value)}`;
  const pawns = value / 100;
  return `${pawns >= 0 ? "+" : ""}${pawns.toFixed(2)}`;
}

export function criticalSwingLabel(critical) {
  if (critical.win_loss != null) {
    return `−${Number(critical.win_loss).toFixed(1)}% win chance`;
  }
  return `${scoreLabel(critical.eval_before)} → ${scoreLabel(critical.eval_after)}`;
}

export function buildProvisionalTimeline(board, pgn) {
  const game = board.createGame();
  game.loadPgn(pgn);
  const moves = game.history({ verbose: true });
  if (!moves.length) throw new Error("no moves");

  const nodes = moves.map((move, index) => ({
    node: index,
    fen: move.before,
    win_white: null,
    color: move.color === "w" ? "white" : "black",
    move_number: Math.floor(index / 2) + 1,
    ply: index + 1,
    move_san: move.san,
    move_uci: move.from + move.to + (move.promotion || ""),
    best_uci: null,
    best_san: null,
    classification: null,
    mistake_index: null,
  }));
  const last = moves[moves.length - 1];
  nodes.push({
    node: moves.length,
    fen: last.after,
    win_white: null,
    color: game.turn() === "w" ? "white" : "black",
    move_number: Math.floor(moves.length / 2) + 1,
  });
  return nodes;
}
