import { squarePosition } from "./helpers.js";

export function createPuzzleBoardView({ $, board, onPositionChange = () => {} }) {
  const chess = board.chess;
  const ground = board.ground;
  let orientation = "white";
  let shapes = [];
  let lastMove = null;
  let animations = true;

  function render(movable) {
    const color = board.turnColor();
    ground.set({
      fen: chess.fen(),
      orientation,
      turnColor: color,
      check: chess.inCheck(),
      lastMove: lastMove || undefined,
      movable: {
        color: movable ? color : undefined,
        dests: movable ? board.computeDests() : new Map(),
        free: false,
        showDests: true,
      },
      animation: { enabled: true },
    });
    board.setShapes(shapes);
    onPositionChange(chess.fen());
  }

  function prepare(fen) {
    chess.load(fen);
    ground.set({
      fen: chess.fen(),
      orientation,
      turnColor: board.turnColor(),
      check: chess.inCheck(),
      lastMove: undefined,
      movable: { color: undefined, dests: new Map() },
      animation: { enabled: true },
    });
    board.setShapes(shapes);
    onPositionChange(chess.fen());
  }

  function reset() {
    chess.reset();
    shapes = [];
    lastMove = null;
    ground.set({
      fen: chess.fen(),
      orientation,
      lastMove: undefined,
      movable: { color: undefined, dests: new Map() },
      animation: { enabled: false },
    });
    board.setShapes([]);
    onPositionChange(chess.fen());
  }

  function blink(square, kind) {
    if (!animations) return;
    const overlay = $("board-overlay");
    const position = squarePosition(square, orientation);
    if (!overlay || !position) return;
    const cell = document.createElement("div");
    cell.className = `sq-blink ${kind}`;
    Object.assign(cell.style, position);
    overlay.appendChild(cell);
    setTimeout(() => cell.remove(), kind === "bad" ? 520 : 900);
    if (kind === "ok" || kind === "part") {
      const ring = document.createElement("div");
      ring.className = `sq-ring ${kind}`;
      Object.assign(ring.style, position);
      overlay.appendChild(ring);
      setTimeout(() => ring.remove(), 720);
    }
  }

  function confetti(square, kind) {
    if (!animations) return;
    const overlay = $("board-overlay");
    const position = squarePosition(square, orientation);
    if (!overlay || !position) return;
    const centerX = `${parseFloat(position.left) + 6.25}%`;
    const centerY = `${parseFloat(position.top) + 6.25}%`;
    const colors = kind === "part"
      ? ["#e08000", "#f0a640", "#f5c451", "#ffffff"]
      : ["#7bb434", "#a3d160", "#f5c451", "#ffffff", "#5a9216"];
    const count = kind === "part" ? 10 : 18;
    for (let index = 0; index < count; index += 1) {
      const piece = document.createElement("div");
      piece.className = "confetti";
      const angle = Math.random() * Math.PI * 2;
      const distance = 26 + Math.random() * 48;
      const size = 4 + Math.random() * 4;
      piece.style.left = centerX;
      piece.style.top = centerY;
      piece.style.background = colors[index % colors.length];
      piece.style.setProperty("--dx", `${(Math.cos(angle) * distance).toFixed(1)}px`);
      piece.style.setProperty("--dy", `${(Math.sin(angle) * distance + 22).toFixed(1)}px`);
      piece.style.setProperty("--rot", `${(Math.random() * 540 - 270).toFixed(0)}deg`);
      piece.style.animationDelay = `${Math.floor(Math.random() * 70)}ms`;
      piece.style.width = `${size.toFixed(1)}px`;
      piece.style.height = `${(size * (0.55 + Math.random() * 0.7)).toFixed(1)}px`;
      overlay.appendChild(piece);
      setTimeout(() => piece.remove(), 1050);
    }
  }

  function shake() {
    if (!animations) return;
    const element = $("board");
    if (!element) return;
    element.classList.remove("shake");
    void element.offsetWidth;
    element.classList.add("shake");
    setTimeout(() => element.classList.remove("shake"), 400);
  }

  return {
    blink,
    confetti,
    prepare,
    render,
    reset,
    shake,
    setAnimations: (enabled) => { animations = enabled; },
    setLastMove: (move) => { lastMove = move || null; },
    setOrientation: (color) => { orientation = color || "white"; },
    setShapes: (nextShapes) => {
      shapes = nextShapes || [];
      board.setShapes(shapes);
    },
    get lastMove() { return lastMove; },
    get orientation() { return orientation; },
    get shapes() { return shapes; },
  };
}
