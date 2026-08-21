import { byId } from "../core/dom.js";

export function createBoardLayoutController(board) {
  const $ = byId;
  const ground = board.ground;

const HISTORY_DRAWER_MAX = 1400;
function historyIsDrawer() {
  return window.innerWidth <= HISTORY_DRAWER_MAX;
}
function closeHistoryDrawer() {
  if (historyIsDrawer()) document.body.classList.add("history-hidden");
}

function toggleHistory() {
  document.body.classList.toggle("history-hidden");
}

// Keep the drawer state sane when the window crosses the 1400px breakpoint. Without this, the
// `history-hidden` class is whatever it was last set to (e.g. never set, if the page loaded wide),
// so shrinking below 1400 can leave the panel stuck open as a fixed drawer overlaying the board —
// and the open drawer covers the ☰ Games button, so toggling it looks like nothing happens.
// Entering drawer mode → start closed (☰ Games opens it); back to wide → show the column.
let wasDrawer = historyIsDrawer();
window.addEventListener("resize", () => {
  const now = historyIsDrawer();
  if (now === wasDrawer) return;
  wasDrawer = now;
  document.body.classList.toggle("history-hidden", now);
});

// --- board resizing (the iPad-style drag handle, #col-resizer) ------------------------------
// Everything board-sized derives from the CSS var --board-size, which falls back to the
// responsive --board-default unless we set an explicit --board-user (a px override). We keep the
// user's chosen px in localStorage and re-clamp it on every apply, so it never overflows the row.
const BOARD_SIZE_KEY = "chessBoardSize";
let boardSizeUser = null; // px override, or null = use the responsive default

// Looser than the default's 660px / 48vw caps (the user asked for less restriction) but still
// leaves the analysis column (and the Games column, when it's not a drawer) enough room.
function boardSizeBounds() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const PAD = 40; // main's left+right padding
  const GAP = 24; // main's column gap
  const puzzle = document.body.classList.contains("puzzle-mode");
  const SIDE_MIN = 280; // keep the analysis column / puzzle rail usable
  const EVALBAR = puzzle ? 0 : 28; // eval bar + its gap (col-width = board-size + 28); hidden in puzzle mode
  // The Games column only exists in analysis mode above the drawer breakpoint.
  const historyCol = !puzzle && vw > HISTORY_DRAWER_MAX ? 280 + GAP : 0;
  const maxByWidth = vw - PAD - GAP - SIDE_MIN - historyCol - EVALBAR;
  const maxByHeight = Math.round(vh * 0.92);
  const min = 240;
  const max = Math.max(min, Math.min(maxByWidth, maxByHeight));
  return { min, max };
}

let boardRedrawPending = false;
function scheduleBoardRedraw() {
  // Chessground caches its bounds, so it needs a redraw to re-place pieces after the board's
  // rendered size changes (a resize, an override, or the panel scrollbar appearing/disappearing).
  // Debounced to one redraw per frame — a ResizeObserver can fire many times in a burst.
  if (!ground || boardRedrawPending) return;
  boardRedrawPending = true;
  requestAnimationFrame(() => {
    boardRedrawPending = false;
    if (ground) ground.redrawAll();
  });
}

// Anchor the fixed drag handle to the board panel's *measured* right edge (+ a small gap) instead
// of a CSS var that can't see the panel's scrollbar gutter. This keeps the separator and the board
// reading the same width, so the handle clears the scrollbar in every browser (Chrome overlay,
// Firefox reserved-gutter) and in puzzle mode (no eval bar) alike. See issue #5.
const RESIZER_GAP = 6;
function positionResizer() {
  const rez = $("col-resizer");
  const col = document.querySelector(".board-col");
  if (!rez || !col) return;
  // Hidden on narrow/stacked layouts (CSS display:none) — nothing to place.
  if (getComputedStyle(rez).display === "none") return;
  const right = col.getBoundingClientRect().right; // viewport coords == fixed-position origin
  rez.style.left = Math.round(right + RESIZER_GAP) + "px";
}

function applyBoardSize() {
  const root = document.documentElement;
  if (boardSizeUser == null) {
    root.style.removeProperty("--board-user");
  } else {
    const { min, max } = boardSizeBounds();
    boardSizeUser = Math.round(Math.max(min, Math.min(max, boardSizeUser)));
    root.style.setProperty("--board-user", boardSizeUser + "px");
  }
  scheduleBoardRedraw();
  positionResizer();
}

// Watch the board's rendered size (changes on window resize even with no user override, on an
// override drag, and when the panel scrollbar toggles) and keep chessground's bounds + the
// separator in sync. Without this, a responsive resize with no --board-user left the pieces
// mis-centered against stale bounds (the old window-resize handler only redrew when an override
// was set). See issue #5.
function observeBoardLayout() {
  const board = $("board");
  const col = document.querySelector(".board-col");
  if (typeof ResizeObserver === "undefined") return;
  if (board) {
    new ResizeObserver(() => {
      scheduleBoardRedraw();
      positionResizer();
    }).observe(board);
  }
  // The panel's own width changes independently of the board when its scrollbar gutter appears or
  // the eval bar is hidden (puzzle mode) — reposition the handle for those too.
  if (col) new ResizeObserver(() => positionResizer()).observe(col);
}

function restoreBoardSize() {
  try {
    const v = parseInt(localStorage.getItem(BOARD_SIZE_KEY) || "", 10);
    if (Number.isFinite(v) && v > 0) boardSizeUser = v;
  } catch (_) {}
  applyBoardSize();
}

function resetBoardSize() {
  boardSizeUser = null;
  try {
    localStorage.removeItem(BOARD_SIZE_KEY);
  } catch (_) {}
  applyBoardSize();
}

function persistBoardSize() {
  if (boardSizeUser == null) return;
  try {
    localStorage.setItem(BOARD_SIZE_KEY, String(boardSizeUser));
  } catch (_) {}
}

function initBoardResizer() {
  const rez = $("col-resizer");
  if (!rez) return;
  let startX = 0;
  let startSize = 0;
  let dragging = false;
  const onMove = (e) => {
    if (!dragging) return;
    // Board is on the left, so dragging right (positive delta) grows it.
    boardSizeUser = startSize + (e.clientX - startX);
    applyBoardSize();
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    rez.classList.remove("dragging");
    document.body.style.userSelect = "";
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    persistBoardSize();
  };
  rez.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    // Start from whatever the board is actually rendered at (works whether or not an override is set).
    startSize = Math.round($("board").getBoundingClientRect().width);
    rez.classList.add("dragging");
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  });
  // Double-click resets to the responsive default.
  rez.addEventListener("dblclick", resetBoardSize);
  // Keyboard nudge for accessibility (handle is focusable).
  rez.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const base = boardSizeUser == null ? Math.round($("board").getBoundingClientRect().width) : boardSizeUser;
    boardSizeUser = base + (e.key === "ArrowRight" ? 24 : -24);
    applyBoardSize();
    persistBoardSize();
  });
  // Re-clamp a fixed board size when the window changes (so it can't overflow a now-smaller window).
  // With no override the responsive board still changes size, so always re-place the handle (and the
  // ResizeObserver redraws the pieces); with an override, re-clamp too.
  window.addEventListener("resize", () => {
    if (boardSizeUser != null) applyBoardSize();
    else positionResizer();
  });
}


  return {
    mount() {
      closeHistoryDrawer();
      restoreBoardSize();
      initBoardResizer();
      observeBoardLayout();
      positionResizer();
    },
    closeHistoryDrawer,
    toggleHistory,
    positionResizer,
    modeChanged() {
      if (boardSizeUser != null) applyBoardSize();
      requestAnimationFrame(() => {
        board.redraw();
        positionResizer();
      });
    },
  };
}

