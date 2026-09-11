import { byId } from "../core/dom.js";

export function createBoardLayoutController(board) {
  const $ = byId;
  const ground = board.ground;

function setHistoryOpen(open, restoreFocus = false) {
  document.body.classList.toggle("history-hidden", !open);
  $("history-col").inert = !open;
  $("history-toggle").setAttribute("aria-expanded", String(open));
  if (open) $("history-collapse").focus();
  else if (restoreFocus) $("history-toggle").focus();
}
function closeHistoryDrawer() {
  setHistoryOpen(false, $("history-col").contains(document.activeElement));
}
function toggleHistory() {
  setHistoryOpen(document.body.classList.contains("history-hidden"), true);
}
function showHistory() { setHistoryOpen(true); }
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !document.body.classList.contains("history-hidden")) {
    event.preventDefault();
    setHistoryOpen(false, true);
  }
});

// --- board resizing (the iPad-style drag handle, #col-resizer) ------------------------------
// Everything board-sized derives from the CSS var --board-size, which falls back to the
// responsive --board-default unless we set an explicit --board-user (a px override). We keep the
// user's chosen px in localStorage and re-clamp it on every apply, so it never overflows the row.
const BOARD_SIZE_KEY = "chessBoardSize";
let boardSizeUser = null; // px override, or null = use the responsive default
const NAVIGATION_SIZE_KEY = "chessNavigationSize";
let navigationSizeUser = null;

function threeColumns() {
  return window.innerWidth > 1400 && !document.body.classList.contains("puzzle-mode");
}

function applyNavigationSize() {
  const root = document.documentElement;
  if (!threeColumns()) {
    root.style.removeProperty("--navigation-user");
    return;
  }
  const boardWidth = document.querySelector(".board-col").getBoundingClientRect().width;
  const max = Math.max(320, window.innerWidth - 40 - 48 - boardWidth - 380);
  const width = Math.round(Math.max(320, Math.min(max, navigationSizeUser ?? 400)));
  root.style.setProperty("--navigation-user", width + "px");
  const separator = $("navigation-resizer");
  separator.setAttribute("aria-valuemin", "320");
  separator.setAttribute("aria-valuemax", String(Math.floor(max)));
  separator.setAttribute("aria-valuenow", String(width));
}

// Reserve navigation and Analysis widths; Games is always an overlay.
function boardSizeBounds() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const puzzle = document.body.classList.contains("puzzle-mode");
  const stacked = vw <= 900;
  const padding = stacked ? 28 : 40;
  const evaluation = puzzle ? 0 : 28;
  const columns = stacked ? 0 : puzzle ? 304 : vw > 1400 ? 748 : 364;
  const maxByWidth = vw - padding - columns - evaluation - 12;
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
  if (threeColumns()) {
    const navRight = document.querySelector(".navigation-col").getBoundingClientRect().right;
    $("navigation-resizer").style.left = Math.round(navRight + RESIZER_GAP) + "px";
  }
  const bounds = boardSizeBounds();
  rez.setAttribute("aria-valuemin", String(bounds.min));
  rez.setAttribute("aria-valuemax", String(bounds.max));
  rez.setAttribute("aria-valuenow", String(Math.round($("board").getBoundingClientRect().width)));
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
  applyNavigationSize();
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
  if (col) new ResizeObserver(() => { applyNavigationSize(); positionResizer(); }).observe(col);
  const navigation = document.querySelector(".navigation-col");
  if (navigation) new ResizeObserver(positionResizer).observe(navigation);
}

function restoreBoardSize() {
  try {
    const v = parseInt(localStorage.getItem(BOARD_SIZE_KEY) || "", 10);
    if (Number.isFinite(v) && v > 0) boardSizeUser = v;
    const nav = parseInt(localStorage.getItem(NAVIGATION_SIZE_KEY) || "", 10);
    if (Number.isFinite(nav) && nav > 0) navigationSizeUser = nav;
  } catch (_) {}
  applyBoardSize();
}

function resetBoardSize() {
  boardSizeUser = null;
  navigationSizeUser = null;
  try {
    localStorage.removeItem(BOARD_SIZE_KEY);
    localStorage.removeItem(NAVIGATION_SIZE_KEY);
  } catch (_) {}
  applyBoardSize();
}

function persistBoardSize() {
  if (boardSizeUser == null) return;
  try {
    localStorage.setItem(BOARD_SIZE_KEY, String(boardSizeUser));
  } catch (_) {}
}

function initBoardResizer(id = "col-resizer") {
  const rez = $(id);
  const isNavigation = id === "navigation-resizer";
  if (!rez) return;
  let startX = 0;
  let startSize = 0;
  let dragging = false;
  let startNavigation = 0;
  const update = (delta) => {
    if (isNavigation) {
      navigationSizeUser = startNavigation + delta;
      applyNavigationSize();
      navigationSizeUser = document.querySelector(".navigation-col").getBoundingClientRect().width;
      positionResizer();
    } else {
      const bounds = boardSizeBounds();
      const max = threeColumns() ? Math.min(bounds.max, startSize + startNavigation - 320) : bounds.max;
      boardSizeUser = Math.max(bounds.min, Math.min(max, startSize + delta));
      if (threeColumns()) navigationSizeUser = startNavigation - (boardSizeUser - startSize);
      applyBoardSize();
    }
  };
  const onMove = (e) => {
    if (!dragging) return;
    update(e.clientX - startX);
  };
  const onUp = () => {
    if (!dragging) return;
    dragging = false;
    rez.classList.remove("dragging");
    document.body.style.userSelect = "";
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    persistBoardSize();
    try {
      if (navigationSizeUser != null) localStorage.setItem(NAVIGATION_SIZE_KEY, String(navigationSizeUser));
    } catch (_) {}
  };
  rez.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    dragging = true;
    startX = e.clientX;
    // Start from whatever the board is actually rendered at (works whether or not an override is set).
    startSize = Math.round($("board").getBoundingClientRect().width);
    startNavigation = document.querySelector(".navigation-col").getBoundingClientRect().width;
    rez.classList.add("dragging");
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });
  // Double-click resets to the responsive default.
  rez.addEventListener("dblclick", () => {
    if (!isNavigation) return resetBoardSize();
    navigationSizeUser = null;
    try { localStorage.removeItem(NAVIGATION_SIZE_KEY); } catch (_) {}
    applyNavigationSize();
    positionResizer();
  });
  // Keyboard nudge for accessibility (handle is focusable).
  rez.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    startSize = Math.round($("board").getBoundingClientRect().width);
    startNavigation = document.querySelector(".navigation-col").getBoundingClientRect().width;
    update(e.key === "ArrowRight" ? 24 : -24);
    dragging = true;
    onUp();
  });
  // Re-clamp a fixed board size when the window changes (so it can't overflow a now-smaller window).
  // With no override the responsive board still changes size, so always re-place the handle (and the
  // ResizeObserver redraws the pieces); with an override, re-clamp too.
  window.addEventListener("resize", () => {
    onUp();
    applyBoardSize();
  });
}


  return {
    mount() {
      closeHistoryDrawer();
      restoreBoardSize();
      initBoardResizer();
      initBoardResizer("navigation-resizer");
      observeBoardLayout();
      positionResizer();
    },
    closeHistoryDrawer,
    toggleHistory,
    showHistory,
    positionResizer,
    modeChanged() {
      applyBoardSize();
      requestAnimationFrame(() => {
        board.redraw();
        positionResizer();
      });
    },
  };
}
