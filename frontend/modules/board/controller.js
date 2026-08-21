import { Chessground } from "/vendor/chessground.min.js";
import { Chess } from "/vendor/chess.min.js";

export function createBoardController() {
  const chess = new Chess();
  let ground = null;
  let moveHandler = null;

  function mount(element, { orientation = "white" } = {}) {
    ground = Chessground(element, {
      fen: chess.fen(),
      orientation,
      movable: { free: false, color: "white", dests: computeDests(), showDests: true },
      events: { move: (orig, dest) => moveHandler && moveHandler(orig, dest) },
      drawable: { enabled: true },
    });
    ground.state.drawable.brushes.grey = {
      key: "grey",
      color: "#7c7c7c",
      opacity: 0.9,
      lineWidth: 10,
    };
    return ground;
  }

  function computeDests() {
    const dests = new Map();
    for (const move of chess.moves({ verbose: true })) {
      if (!dests.has(move.from)) dests.set(move.from, []);
      dests.get(move.from).push(move.to);
    }
    return dests;
  }

  function turnColor() {
    return chess.turn() === "w" ? "white" : "black";
  }

  function isPromotion(from, to) {
    return chess
      .moves({ verbose: true })
      .some((move) => move.from === from && move.to === to && move.flags.includes("p"));
  }

  return {
    chess,
    createGame(fen) {
      return new Chess(fen);
    },
    mount,
    computeDests,
    turnColor,
    isPromotion,
    tryMove(move) {
      try {
        return chess.move(move);
      } catch (_) {
        return null;
      }
    },
    setMoveHandler(handler) {
      moveHandler = handler;
    },
    get ground() {
      return ground;
    },
    redraw() {
      if (ground) ground.redrawAll();
    },
    setShapes(shapes) {
      if (ground) ground.setAutoShapes(shapes || []);
    },
  };
}
