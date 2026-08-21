import { puzzleApi } from "../api/puzzles.js";
import { gamesApi } from "../api/games.js";
import { byId, clamp, escapeHtml, sleep } from "../core/dom.js";

/**
 * Owns the complete puzzle trainer, Storm, and shared solution playback lifecycle.
 * Cross-feature navigation is delegated to the injected lifecycle adapter.
 */
export function createPuzzleController({ board, lifecycle, categoryLabel, renderMarkdown }) {
  const $ = byId;
  const chess = board.chess;
  const ground = board.ground;
  const computeDests = () => board.computeDests();
  const turnColor = () => board.turnColor();
  const isPromotion = (from, to) => board.isPromotion(from, to);
  const tryMove = (move) => board.tryMove(move);
  const drawArrows = () => board.setShapes(puzzleShapes);
  let orient = "white";
  let personalizeHistory = true;

// --- puzzle mode ---------------------------------------------------------
// A focused tactical trainer that reuses the same chessground board + chess.js instance. When
// `puzzleMode` is on the analysis layout is hidden (body.puzzle-mode) and onUserMove routes to the
// puzzle handler. `puzzleData` is the active puzzle from /api/puzzle/next; `puzzleSolveColor` is the
// side the user plays; `puzzleShapes` are the post-solve solution/refutation arrows.
let puzzleMode = false;
let puzzleConfigCache = null; // /api/puzzle/config result (rating, has_engine, has_llm)
let puzzleData = null; // current puzzle {id, fen, moves(not sent), side_to_move, ...}
let puzzleSolveColor = "white";
let puzzleDone = false; // true once solved/failed/given-up (board locked, result shown)
let puzzleFailed = false; // user played a wrong move
let puzzleHinted = false;
let puzzleAnimations = true; // Settings toggle: play the board solve/miss animations vs. text only
let puzzleAutoAdvance = false; // Settings toggle: auto-load the next puzzle a beat after a solve (default off)
let puzzleAdvanceTimer = null; // pending auto-advance timeout handle (cancelled on any nav/interaction)
// The rating summary from the FIRST wrong move (which already applied the Glicko loss). Kept so the
// final "Solved (after a miss)" card can surface that the rating already moved, rather than looking
// like nothing happened. Reset on each new puzzle.
let puzzleMissRating = null;
let puzzleBusy = false; // true while animating a forced reply (ignore input)
let puzzleShapes = []; // solution/refutation arrows shown after the fact
let puzzleLastMove = null; // [from, to] of the last move, for the square-blink
let puzzleStreak = 0; // server streak number (clean solves in a row)
let puzzleDailyStreak = 0; // consecutive days practiced (from the server)
let puzzleBestDaily = 0;
// Last few puzzle outcomes THIS session (true = solved clean/green, false = missed/red), shown as
// colour-coded pips beside the streak. Mirrored to sessionStorage so a reload keeps them.
let puzzleResults = [];
// Bumped every time a new puzzle is loaded/resumed. An in-flight move/solution handler captures it
// and bails after each await if it changed, so a stale callback from the PREVIOUS puzzle can never
// leak its result (or a forced reply) onto the puzzle now on the board.
let puzzleGen = 0;
// Snapshot of the last FINISHED puzzle (solved or solution-shown), so a "‹ Previous" button can
// restore it in review mode. This exists because auto-advance yanks you onto the next puzzle a beat
// after a solve, which otherwise strands you: the finished puzzle (and any "why?" you'd want to ask
// about it) is gone. Single-level: restoring clears it; skipping an UNSOLVED puzzle clears it too.
let prevPuzzleSnapshot = null;
// P3/P3.5 selection prefs. `puzzleSource` + the puzzle/analyze mode are persisted in localStorage
// (keyed to this host:port origin), so closing the app and reopening it via ANY launcher
// (.app/.command/.bat) returns to where you left off — the mode AND the sub-tab.
const PZ_MODE_KEY = "pzLastMode"; // "1" = was in puzzle mode
const PZ_SOURCE_KEY = "pzSource"; // "lichess" | "your_games"
const PZ_CATEGORY_KEY = "pzCategory";
function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }
let puzzleSource = lsGet(PZ_SOURCE_KEY) || "lichess"; // "lichess" curated tactics | "your_games" own-game mistakes
let puzzleCategory = lsGet(PZ_CATEGORY_KEY) || "";
let puzzleDifficulty = null; // null | "easier" | "harder"
let puzzleWeakness = false; // bias curated tactics toward the player's weak themes
// Follow-up chat after "Explain why": only exists once explain has run. Threads onto the
// explanation's claude session so questions have its context.
let puzzleChatSession = null; // claude -p session id from the explanation, for --resume
let puzzleChatFen = null; // the position the follow-up chat grounds on (the puzzle solve position)
let puzzleChatBusy = false;

let stormShown = false;
let stormRunning = false;
let stormPuzzle = null;
let stormBusy = false;
let stormGen = 0;
let stormTimerId = null;
let stormDeadline = 0;
let stormScore = 0;
let stormCombo = 0;
let stormReviewEntries = [];
let inStormReview = false;
let solutionPlay = null;
let solutionGen = 0;
let modeGen = 0;
let puzzleAbortController = null;
let stormAbortController = null;

function renewPuzzleRequests() {
  if (puzzleAbortController) puzzleAbortController.abort();
  puzzleAbortController = new AbortController();
  return puzzleAbortController.signal;
}

function renewStormRequests() {
  if (stormAbortController) stormAbortController.abort();
  stormAbortController = new AbortController();
  return stormAbortController.signal;
}

function squareXY(square, orientation) {
  let f = square.charCodeAt(0) - 97; // file a..h -> 0..7
  let r = 8 - parseInt(square[1], 10); // rank 8..1 -> 0..7 (row from top)
  if (orientation === "black") {
    f = 7 - f;
    r = 7 - r;
  }
  return { left: f * 12.5 + "%", top: r * 12.5 + "%" };
}

// Pulse a square in the decoupled overlay (survives chessground redraws). kind: ok|part|bad.
// A solve (ok/part) also gets a soft expanding ripple ring, which reads cleaner than a scaling fill.
function blinkSquare(square, kind) {
  if (!puzzleAnimations) return; // animations off -> result shown by verdict text colour only
  const overlay = $("board-overlay");
  if (!overlay || !square) return;
  const xy = squareXY(square, orient);
  const cell = document.createElement("div");
  cell.className = "sq-blink " + kind;
  cell.style.left = xy.left;
  cell.style.top = xy.top;
  overlay.appendChild(cell);
  setTimeout(() => cell.remove(), kind === "bad" ? 520 : 900);
  if (kind === "ok" || kind === "part") {
    const ring = document.createElement("div");
    ring.className = "sq-ring " + kind;
    ring.style.left = xy.left;
    ring.style.top = xy.top;
    overlay.appendChild(ring);
    setTimeout(() => ring.remove(), 720);
  }
}

// A little confetti burst from the solved square. Multicoloured green/gold for a clean solve,
// a smaller warmer amber puff when the solve came after a miss (still a win, but calmer).
function spawnConfetti(square, kind) {
  if (!puzzleAnimations) return;
  const overlay = $("board-overlay");
  if (!overlay || !square) return;
  const xy = squareXY(square, orient);
  const cx = parseFloat(xy.left) + 6.25 + "%"; // square centre
  const cy = parseFloat(xy.top) + 6.25 + "%";
  const colors =
    kind === "part"
      ? ["#e08000", "#f0a640", "#f5c451", "#ffffff"]
      : ["#7bb434", "#a3d160", "#f5c451", "#ffffff", "#5a9216"];
  const n = kind === "part" ? 10 : 18;
  for (let i = 0; i < n; i++) {
    const p = document.createElement("div");
    p.className = "confetti";
    const angle = Math.random() * Math.PI * 2;
    const dist = 26 + Math.random() * 48;
    const dx = Math.cos(angle) * dist;
    const dy = Math.sin(angle) * dist + 22; // gravity bias so pieces drift down as they fly
    p.style.left = cx;
    p.style.top = cy;
    p.style.background = colors[i % colors.length];
    p.style.setProperty("--dx", dx.toFixed(1) + "px");
    p.style.setProperty("--dy", dy.toFixed(1) + "px");
    p.style.setProperty("--rot", (Math.random() * 540 - 270).toFixed(0) + "deg");
    p.style.animationDelay = Math.floor(Math.random() * 70) + "ms";
    const s = 4 + Math.random() * 4;
    p.style.width = s.toFixed(1) + "px";
    p.style.height = (s * (0.55 + Math.random() * 0.7)).toFixed(1) + "px";
    overlay.appendChild(p);
    setTimeout(() => p.remove(), 1050);
  }
}

function shakeBoard() {
  if (!puzzleAnimations) return;
  const b = $("board");
  if (!b) return;
  b.classList.remove("shake");
  void b.offsetWidth; // restart the animation
  b.classList.add("shake");
  setTimeout(() => b.classList.remove("shake"), 400);
}

// Set the board to the current `chess` position for puzzle solving. `movable` gates whether the
// solver can move (locked while a forced reply animates or after the puzzle is done).
function renderPuzzleBoard(movable) {
  const color = turnColor();
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: color,
    check: chess.inCheck(),
    // Explicitly drive the yellow last-move highlight: because we set the FEN directly (rather than
    // via ground.move), chessground doesn't infer it, so without this it flickered on and off.
    lastMove: puzzleLastMove || undefined,
    movable: {
      color: movable ? color : undefined,
      dests: movable ? computeDests() : new Map(),
      free: false,
      showDests: true,
    },
    animation: { enabled: true },
  });
  drawArrows(); // draws puzzleShapes in puzzle mode
}

function pzStatus(msg) {
  $("pz-status").textContent = msg || "";
}

function updatePuzzleStats(rating, streak) {
  if (rating != null) $("pz-rating").textContent = "Puzzle · " + rating;
  if (streak != null) puzzleStreak = streak;
  renderPuzzlePips();
}

// Colour-coded pips for the last 5 outcomes THIS session (green = solved clean, red = missed),
// plus the running streak number.
function renderPuzzlePips() {
  const el = $("pz-streak");
  if (!el) return;
  const last = puzzleResults.slice(-5);
  let html = last.map((ok) => `<span class="pz-pip ${ok ? "ok" : "bad"}"></span>`).join("");
  if (puzzleStreak) html += `<span class="pz-streak-n">streak ${puzzleStreak}</span>`;
  el.innerHTML = html;
}

// Day-over-day practice streak, shown as a quiet flame chip (brightens past a day).
function renderDailyStreak(streak, best) {
  if (streak != null) puzzleDailyStreak = streak;
  if (best != null) puzzleBestDaily = best;
  const el = $("pz-daily");
  if (!el) return;
  const n = puzzleDailyStreak || 0;
  if (n < 1) { el.hidden = true; el.textContent = ""; return; }
  el.classList.toggle("hot", n >= 2);
  el.innerHTML = `🔥 <b>${n}</b> day${n === 1 ? "" : "s"}`;
  el.title = `Practised ${n} day${n === 1 ? "" : "s"} in a row` +
    (puzzleBestDaily > n ? ` · best ${puzzleBestDaily}` : "");
  el.hidden = false;
}

// The rating progress is COLLAPSED by default: the rail shows only "Rating" + your current number.
// Pressing the header expands a quiet white line curve (styled like the analysis win% plot) of your
// recent rated attempts — no colour-coding, just the trend line at the top of where the bars were.
let ratingCurveExpanded = false;

function renderRatingCurve(history) {
  const el = $("pz-progress");
  if (!el) return;
  const pts = (history || [])
    .filter((h) => h && h.rated && typeof h.rating_after === "number")
    .map((h) => h.rating_after);
  if (pts.length < 2) { el.hidden = true; el.innerHTML = ""; return; }
  const win = pts.slice(-24); // keep it compact
  const current = Math.round(win[win.length - 1]);
  const min = Math.min(...win), max = Math.max(...win);
  const span = Math.max(1, max - min);
  const net = Math.round(win[win.length - 1] - win[0]);
  const netCls = net > 0 ? "up" : net < 0 ? "down" : "flat";
  const netStr = (net > 0 ? "+" : "") + net;

  // White trend line (+ faint fill under it) over a dark panel — same visual language as renderGraph.
  const W = 260, H = 48, PAD = 4;
  const xAt = (i) => (i / (win.length - 1)) * W;
  const yAt = (r) => H - PAD - ((r - min) / span) * (H - 2 * PAD);
  const seq = win.map((r, i) => `${xAt(i).toFixed(1)},${yAt(r).toFixed(1)}`);
  const line = seq.map((p, i) => `${i === 0 ? "M" : "L"}${p}`).join(" ");
  const area = `M0,${H} L${seq.join(" L")} L${W},${H} Z`;
  const svg =
    `<svg class="pz-curve" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">` +
    `<rect x="0" y="0" width="${W}" height="${H}" fill="#14130f"/>` +
    `<path d="${area}" fill="rgba(236,234,228,0.12)"/>` +
    `<path d="${line}" fill="none" stroke="#e8e6e3" stroke-width="1.5" vector-effect="non-scaling-stroke"/>` +
    `</svg>`;

  el.classList.toggle("open", ratingCurveExpanded);
  el.innerHTML =
    `<button type="button" class="pz-progress-head" aria-expanded="${ratingCurveExpanded}">` +
    `<span class="pz-progress-title">Rating <b>${current}</b></span>` +
    `<span class="pz-progress-caret">▸</span></button>` +
    `<div class="pz-progress-body">` +
    `<div class="pz-progress-sub"><span class="pz-progress-net ${netCls}">${netStr} · last ${win.length}</span></div>` +
    svg +
    `</div>`;
  el.hidden = false;
  const head = el.querySelector(".pz-progress-head");
  if (head) head.onclick = () => {
    ratingCurveExpanded = !ratingCurveExpanded;
    el.classList.toggle("open", ratingCurveExpanded);
    head.setAttribute("aria-expanded", String(ratingCurveExpanded));
  };
}

function loadPuzzleResults() {
  try {
    puzzleResults = JSON.parse(sessionStorage.getItem("pzResults") || "[]");
  } catch (_) {
    puzzleResults = [];
  }
}

function recordPuzzleResult(ok) {
  puzzleResults.push(!!ok);
  if (puzzleResults.length > 20) puzzleResults = puzzleResults.slice(-20);
  try {
    sessionStorage.setItem("pzResults", JSON.stringify(puzzleResults));
  } catch (_) {}
  renderPuzzlePips();
}

async function setPuzzleMode(on, opts = {}) {
  if (on === puzzleMode) return;
  const myModeGen = ++modeGen;
  cancelAutoAdvance(); // don't let a queued advance fire after switching activities
  puzzleMode = on;
  document.body.classList.toggle("puzzle-mode", on);
  $("mode-analyze").classList.toggle("active", !on);
  $("mode-puzzles").classList.toggle("active", on);
  // Puzzle mode frees the eval-bar + Games-column width, so the two modes have different board-size
  // bounds. Re-clamp any user override to the mode we're entering (also redraws the board).
  lifecycle.layoutChanged();
  // The mode swap hides/shows elements around the board, shifting its on-screen position; chessground
  // caches its bounding rect, so without a redraw grabs land offset from the cursor. Recompute bounds
  // after the layout settles (next frame).
  requestAnimationFrame(() => {
    board.redraw();
    lifecycle.positionResizer();
  });
  if (on) {
    lsSet(PZ_MODE_KEY, "1"); // remember across reloads AND full app restarts (any launcher)
    lifecycle.enter();
    loadPuzzleResults();
    const signal = renewPuzzleRequests();
    if (!puzzleConfigCache) {
      try {
        puzzleConfigCache = await puzzleApi.config({ signal });
      } catch (_) {
        puzzleConfigCache = null;
      }
    }
    if (myModeGen !== modeGen || !puzzleMode) return;
    // "From your games" needs the engine (it validates moves live); disable the segment otherwise.
    const hasEngine = !!(puzzleConfigCache && puzzleConfigCache.has_engine);
    const mineBtn = $("pz-src-mine");
    mineBtn.disabled = !hasEngine;
    mineBtn.title = hasEngine
      ? "Practice positions from your own analysed games"
      : "Needs the chess engine (Stockfish) — unavailable";
    if (!hasEngine && puzzleSource === "your_games") puzzleSource = "lichess";
    try {
      await loadPuzzleCategories({ signal });
    } catch (_) {
      return;
    }
    if (myModeGen !== modeGen || !puzzleMode) return;
    syncSourceUI();
    if (puzzleConfigCache) {
      updatePuzzleStats(puzzleConfigCache.your_rating, puzzleConfigCache.streak);
      renderDailyStreak(puzzleConfigCache.daily_streak, puzzleConfigCache.best_daily_streak);
    }
    loadPuzzleStatCard(); // daily streak + discrete rating curve (quiet, under the rail)
    // On a reload, resume the in-progress puzzle at the same spot; otherwise start a fresh one.
    if (opts.resume) {
      const resumed = await resumeCurrentPuzzle();
      if (myModeGen !== modeGen || !puzzleMode || resumed) return;
    }
    await loadNextPuzzle(opts.puzzle || {});
  } else {
    puzzleGen++;
    stormGen++;
    if (puzzleAbortController) puzzleAbortController.abort();
    if (stormAbortController) stormAbortController.abort();
    lsSet(PZ_MODE_KEY, "0"); // back in analyze mode -> next open lands on analyze
    clearSolutionPlayback(); // drop any solution step-through + hide its nav
    // Leaving puzzles entirely: bank + drop any storm run and reset the sub-mode to Solve.
    if (stormShown) {
      endStormRun({ abandon: true });
      stormShown = false;
      inStormReview = false;
      $("pz-next").textContent = "Next puzzle →";
      $("pz-solve").hidden = false;
      $("pz-storm").hidden = true;
      $("pz-mode-solve").classList.add("active");
      $("pz-mode-storm").classList.remove("active");
    }
    // Restore the analysis board to wherever the game cursor was.
    puzzleChatReset();
    puzzleData = null;
    puzzleShapes = [];
    lifecycle.leave();
  }
}

// Resume the puzzle the server still has in progress (after a browser reload). Returns false if
// there's nothing to resume (e.g. the server restarted), so the caller loads a fresh puzzle.
async function resumeCurrentPuzzle() {
  const myGen = ++puzzleGen;
  const signal = renewPuzzleRequests();
  let cur;
  try {
    cur = await puzzleApi.current({ signal });
  } catch (_) {
    return false;
  }
  if (myGen !== puzzleGen) return true; // superseded; caller should not load another
  if (!cur || !cur.active || cur.finished) return false;
  const isMine = cur.source === "your_games";
  puzzleData = {
    id: cur.id,
    source: cur.source || "lichess",
    side_to_move: cur.side_to_move,
    themes: cur.themes || [],
    rating: cur.rating,
    your_rating: cur.your_rating,
    game_id: cur.game_id,
    critical_id: cur.critical_id,
    reviewed_side: cur.reviewed_side,
    ply: cur.ply,
    win_drop: cur.win_drop,
    category: cur.category,
    phase: cur.phase,
    badge: cur.badge,
    game_url: cur.game_url,
    fen: cur.fen,
    solve_fen: cur.fen,
    played_uci: cur.played_uci,
    played_san: cur.played_san,
  };
  puzzleSource = puzzleData.source;
  puzzleDone = false;
  puzzleFailed = !!cur.failed;
  puzzleHinted = !!cur.hinted;
  puzzleMissRating = null;
  puzzleBusy = false;
  puzzleChatReset();
  puzzleShapes = []; // no answer-bearing arrows before the personal puzzle is submitted
  puzzleSolveColor = cur.side_to_move || "white";
  orient = puzzleSolveColor;
  $("pz-result").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-prompt").hidden = false;
  $("pz-ghosts").hidden = false;
  $("pz-prompt-line").textContent = (puzzleSolveColor === "white" ? "White" : "Black") + " to move";
  $("pz-prompt-sub").textContent = isMine ? "Find a better move than you played" : "Find the best move";
  renderPuzzleBadge(isMine ? puzzleData : null);
  syncSourceUI();
  updatePuzzleStats(cur.your_rating, puzzleStreak);
  chess.load(cur.fen);
  renderPuzzleBoard(true);
  pzStatus(puzzleFailed ? "Resumed — keep trying, or press Show solution." : "Resumed your puzzle.");
  return true;
}

async function loadNextPuzzle(opts = {}) {
  // Remember the puzzle we're leaving so "‹ Previous" can bring it back — but only if it was
  // finished (a solve or a shown solution is worth revisiting; a skipped, unsolved one isn't, and
  // can't be re-rendered in review mode anyway, so leaving it just drops any stale snapshot).
  if (puzzleDone) capturePrevPuzzle();
  else clearPrevPuzzle();
  cancelAutoAdvance(); // supersede any pending auto-advance (also covers manual Next/Skip)
  const myGen = ++puzzleGen; // cancel any in-flight handler from the puzzle we're leaving
  const signal = renewPuzzleRequests();
  puzzleBusy = true; // lock the board until the new puzzle is in place
  pzStatus("Loading…");
  const q = new URLSearchParams();
  const src = opts.source !== undefined ? opts.source : puzzleSource;
  if (src && src !== "lichess") q.set("source", src);
  if (src === "your_games") {
    const category = opts.category !== undefined ? opts.category : puzzleCategory;
    if (category) q.set("category", category);
    if (opts.game_id) q.set("game_id", opts.game_id);
    if (opts.critical_id) q.set("critical_id", opts.critical_id);
  }
  const diff = opts.difficulty !== undefined ? opts.difficulty : puzzleDifficulty;
  if (diff) q.set("difficulty", diff);
  if (puzzleWeakness && src === "lichess") q.set("weakness", "1");
  let p;
  try {
    p = await puzzleApi.next(q, { signal });
  } catch (_) {
    if (myGen !== puzzleGen) return;
    puzzleBusy = false;
    pzStatus("Couldn't load a puzzle.");
    return;
  }
  if (myGen !== puzzleGen) return; // superseded by another load
  if (!p || p.error || !p.fen) {
    puzzleBusy = false;
    pzStatus((p && p.error) || "No puzzles available.");
    return;
  }
  applyPuzzle(p);
}

function applyPuzzle(p) {
  const myGen = ++puzzleGen; // this is now the current puzzle; older handlers are superseded
  puzzleData = p;
  puzzleDone = false;
  puzzleFailed = false;
  puzzleHinted = false;
  puzzleMissRating = null;
  puzzleBusy = true;
  // A personal training position starts clean: no game move, best move, eval, or arrows leak.
  puzzleShapes = [];
  puzzleLastMove = null; // no carry-over highlight from the previous puzzle
  puzzleChatReset(); // a new puzzle drops any follow-up chat thread
  clearSolutionPlayback(); // a new puzzle supersedes the previous puzzle's solution step-through
  puzzleSolveColor = p.side_to_move || "white";
  orient = puzzleSolveColor;
  pzStatus("");

  // Reset the result/prompt cards.
  $("pz-result").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-explain-out").innerHTML = "";
  $("pz-prompt").hidden = false;
  $("pz-ghosts").hidden = false;
  $("pz-explain").disabled = false;
  $("pz-prompt-line").textContent = (puzzleSolveColor === "white" ? "White" : "Black") + " to move";
  const isMine = p.source === "your_games";
  $("pz-prompt-sub").textContent = isMine ? "Find a better move than you played" : "Find the best move";
  renderPuzzleBadge(isMine ? p : null);
  if (puzzleConfigCache) updatePuzzleStats(p.your_rating, puzzleConfigCache.streak);
  else updatePuzzleStats(p.your_rating, null);

  // Show the position BEFORE the setup move, then play it in with a short animation.
  chess.load(p.fen);
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: turnColor(),
    check: chess.inCheck(),
    lastMove: undefined, // clear any highlight carried over from the previous puzzle
    movable: { color: undefined, dests: new Map() },
    animation: { enabled: true },
  });
  drawArrows();

  setTimeout(() => {
    if (myGen !== puzzleGen) return; // a newer puzzle loaded while we waited
    const su = p.setup_move;
    if (su) {
      chess.move({ from: su.slice(0, 2), to: su.slice(2, 4), promotion: su.slice(4) || undefined });
      puzzleLastMove = [su.slice(0, 2), su.slice(2, 4)];
    }
    renderPuzzleBoard(true);
    puzzleBusy = false;
  }, 430);
  updatePrevPuzzleButton(); // show "‹ Previous" if we just auto-advanced off a finished puzzle
}

// --- puzzle follow-up chat (revealed only after "Explain why") -----------------------------------

// Drop any follow-up chat thread + hide the panel (called on each new/resumed puzzle).
function puzzleChatReset() {
  puzzleChatSession = null;
  puzzleChatFen = null;
  const box = $("pz-chat-messages");
  if (box) box.innerHTML = "";
  const chat = $("pz-chat");
  if (chat) chat.hidden = true;
  const input = $("pz-chat-input");
  if (input) input.value = "";
}

function pzChatMsg(cls, text) {
  const d = document.createElement("div");
  d.className = `chat-msg ${cls}`;
  if (cls === "bot") d.innerHTML = renderMarkdown(text); // only the final answer is markdown
  else d.textContent = text;
  const box = $("pz-chat-messages");
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}

async function sendPuzzleChat(ev) {
  ev.preventDefault();
  if (puzzleChatBusy || !puzzleData) return;
  cancelAutoAdvance(); // engaging with the coach means stay on this puzzle
  const input = $("pz-chat-input");
  const q = input.value.trim() || "Can you explain that a bit more?";
  input.value = "";
  pzChatMsg("user", q);
  puzzleChatBusy = true;
  $("pz-chat-send").disabled = true;
  const pending = pzChatMsg("bot pending", "Snowie is sniffing around (thinking)");
  // The move to reason about: for a mistake puzzle it's the move played in the game; for a tactic
  // it's whatever wrong move they tried (if any). The chat grounds on the puzzle's solve position.
  const moveInQuestion =
    puzzleData.source === "your_games" ? puzzleData.played_uci : puzzleData._yourMove || null;
  try {
    const res = await puzzleApi.chat({
        question: q,
        fen: puzzleChatFen || puzzleData.solve_fen || puzzleData.fen || null,
        last_move: moveInQuestion,
        session_id: puzzleChatSession,
        use_profile: personalizeHistory,
      });
    pending.remove();
    if (res.error) {
      pzChatMsg("bot err", res.error);
    } else {
      pzChatMsg("bot", res.answer || "(no answer)");
      if (res.session_id) puzzleChatSession = res.session_id;
    }
  } catch (e) {
    pending.remove();
    pzChatMsg("bot err", "Request failed: " + e);
  } finally {
    puzzleChatBusy = false;
    $("pz-chat-send").disabled = false;
    input.focus();
  }
}

async function onPuzzleMove(orig, dest) {
  if (puzzleDone || puzzleBusy) {
    renderPuzzleBoard(!puzzleDone); // snap the piece back; chessground already moved it visually
    return;
  }
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");
  const mv = tryMove({ from: orig, to: dest, promotion: promo });
  if (!mv) {
    renderPuzzleBoard(true);
    return;
  }
  puzzleLastMove = [orig, dest];
  puzzleBusy = true;
  const myGen = puzzleGen;
  renderPuzzleBoard(false);

  let res;
  try {
    res = await puzzleApi.move(
      { id: puzzleData.id, uci },
      { signal: puzzleAbortController && puzzleAbortController.signal }
    );
  } catch (_) {
    if (myGen !== puzzleGen) return;
    chess.undo();
    renderPuzzleBoard(true);
    puzzleBusy = false;
    pzStatus("Move check failed — try again.");
    return;
  }
  if (myGen !== puzzleGen) return; // a new puzzle loaded while the server was validating

  if (res.error) {
    chess.undo();
    renderPuzzleBoard(true);
    puzzleBusy = false;
    pzStatus(res.error);
    return;
  }

  if (!res.correct) {
    // Wrong move: flash red and take it back, but DON'T reveal the solution — let them try again.
    // The first miss already cost the rating server-side; reflect that, but keep the board live.
    blinkSquare(dest, "bad");
    shakeBoard();
    await sleep(440);
    if (myGen !== puzzleGen) return;
    chess.undo();
    puzzleLastMove = null;
    puzzleFailed = true;
    if (res.source === "your_games") puzzleShapes = res.shapes || [];
    renderPuzzleBoard(true);
    if (res.rating) {
      puzzleMissRating = res.rating; // remember the applied loss for the final card
      updatePuzzleStats(res.rating.rating_after, res.rating.streak);
    }
    pzStatus(
      res.source === "your_games"
        ? (res.message || "Still drops too much — try another move, or press Show solution.")
        : res.rating && res.rating.rated
        ? "Not quite — that cost some rating. Try again, or press Show solution."
        : "Not quite — try again, or press Show solution."
    );
    puzzleBusy = false;
    return;
  }

  if (res.is_complete) {
    // Celebratory green for a clean solve, calmer amber when it took a miss — matches the verdict
    // colour so the board and the text tell the same story, and a little confetti off the piece.
    const kind = puzzleFailed ? "part" : "ok";
    blinkSquare(dest, kind);
    spawnConfetti(dest, kind);
    puzzleDone = true;
    if (res.source === "your_games") puzzleShapes = res.shapes || [];
    renderPuzzleBoard(false);
    const outcome = puzzleFailed || puzzleHinted ? "solved_with_hints" : "solved_first_try";
    finishPuzzle(outcome, res.rating, uci, res.source === "your_games" ? res : null);
    puzzleBusy = false;
    return;
  }

  // Correct, more to come: play the forced reply, then hand the move back.
  await sleep(280);
  if (myGen !== puzzleGen) return;
  const reply = res.opponent_reply_uci;
  if (reply) {
    chess.move({ from: reply.slice(0, 2), to: reply.slice(2, 4), promotion: reply.slice(4) || undefined });
    puzzleLastMove = [reply.slice(0, 2), reply.slice(2, 4)];
  }
  renderPuzzleBoard(true);
  puzzleBusy = false;
}

// --- puzzle storm (timed rush) -----------------------------------------------------------------

// Switch the rail between the per-puzzle "Solve" trainer and the "Storm" scoreboard. Ending an
// in-progress run when we leave storm is handled by the caller (setStormMode(false)).
function setStormMode(on) {
  if (on === stormShown) return;
  cancelAutoAdvance(); // a solve-trainer auto-advance must not fire into the storm sub-mode
  inStormReview = false; // any open post-run review ends when we switch sub-mode
  clearSolutionPlayback();
  $("pz-next").textContent = "Next puzzle →"; // undo the "‹ Back to results" repurposing
  stormShown = on;
  $("pz-mode-solve").classList.toggle("active", !on);
  $("pz-mode-storm").classList.toggle("active", on);
  $("pz-solve").hidden = on;
  $("pz-storm").hidden = !on;
  if (on) {
    // Leaving the Solve trainer: cancel any in-flight puzzle handler + clear its board state.
    puzzleGen++;
    puzzleData = null;
    puzzleShapes = [];
    puzzleDone = false;
    pzStatus("");
    renderStormBests();
    stormResetBoard();
    stormShowStart(false);
  } else {
    endStormRun({ abandon: true });
    // Back to the Solve trainer: reload a puzzle (we cleared puzzleData on entering storm).
    if (puzzleMode) loadNextPuzzle();
  }
}

// A calm, empty board with the run stats reset — the between-runs resting state.
function stormResetBoard() {
  chess.reset();
  puzzleLastMove = null;
  puzzleShapes = [];
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    lastMove: undefined,
    movable: { color: undefined, dests: new Map() },
    animation: { enabled: false },
  });
  drawArrows();
}

function fmtClock(secs) {
  secs = Math.max(0, Math.ceil(secs));
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return m + ":" + String(s).padStart(2, "0");
}

// Show the "start / play again" button + the intro/game-over message (vs the live clock).
function stormShowStart(gameOver, view) {
  const btn = $("pz-storm-start");
  btn.hidden = false;
  btn.textContent = gameOver ? "↻ Play again" : "⚡ Start storm";
  setStormSide(null); // no live puzzle on the start / game-over screen
  const clockEl = $("pz-storm-clock");
  const cfg = puzzleConfigCache || {};
  const dur = (view && view.duration) || cfg.storm_duration || 180;
  clockEl.classList.remove("low");
  const msg = $("pz-storm-msg");
  if (gameOver && view) {
    clockEl.textContent = "0:00";
    $("pz-storm-score").textContent = view.score;
    const nh = view.new_high ? ` <span class="pz-storm-nh">new best!</span>` : "";
    msg.innerHTML =
      `Time! You solved <b>${view.score}</b>` +
      (view.misses ? ` · ${view.misses} missed` : "") +
      (view.best_combo ? ` · best combo ${view.best_combo}` : "") +
      nh;
  } else {
    clockEl.textContent = fmtClock(dur);
    $("pz-storm-score").textContent = "0";
    $("pz-storm-combo").hidden = true;
    $("pz-storm-results").innerHTML = "";
    msg.innerHTML =
      "Solve as many as you can before the clock runs out. A combo earns bonus time; a wrong move costs time.";
    $("pz-storm-review").hidden = true; // no run to review on the intro screen
  }
  renderStormBests();
}

function renderStormBests() {
  const el = $("pz-storm-bests");
  if (!el) return;
  const cfg = puzzleConfigCache || {};
  const high = cfg.storm_high || 0;
  const combo = cfg.storm_best_combo || 0;
  el.innerHTML = high || combo
    ? `Best: <b>${high}</b> solved` + (combo ? ` · combo ${combo}` : "")
    : "";
}

async function startStorm() {
  const btn = $("pz-storm-start");
  btn.hidden = true;
  inStormReview = false; // a new run supersedes any previous run's review
  clearSolutionPlayback();
  $("pz-storm-review").hidden = true;
  $("pz-next").textContent = "Next puzzle →";
  pzStatus("");
  let view;
  try {
    view = await puzzleApi.stormStart({ signal: renewStormRequests() });
  } catch (_) {
    pzStatus("Couldn't start storm.");
    btn.hidden = false;
    return;
  }
  if (!view || view.error || !view.puzzle) {
    pzStatus((view && view.error) || "No puzzles available.");
    btn.hidden = false;
    return;
  }
  stormRunning = true;
  stormScore = 0;
  stormCombo = 0;
  applyStormState(view);
  startStormClock();
  applyStormPuzzle(view.puzzle);
}

function startStormClock() {
  stopStormClock();
  stormTimerId = setInterval(() => {
    const remaining = (stormDeadline - Date.now()) / 1000;
    const el = $("pz-storm-clock");
    el.textContent = fmtClock(remaining);
    el.classList.toggle("low", remaining <= 10);
    if (remaining <= 0) {
      stopStormClock();
      stormTimeUp();
    }
  }, 250);
}

function stopStormClock() {
  if (stormTimerId) {
    clearInterval(stormTimerId);
    stormTimerId = null;
  }
}

// The client clock hit zero: ask the server for the final (it finishes a run whose time is up).
async function stormTimeUp() {
  if (!stormRunning) return;
  let view;
  try {
    view = await puzzleApi.stormNext({ signal: stormAbortController && stormAbortController.signal });
  } catch (_) {
    view = { ended: true, score: stormScore };
  }
  finishStorm(view);
}

// Update the live scoreboard from a server view (authoritative remaining/score/combo).
function applyStormState(view) {
  if (typeof view.remaining === "number") stormDeadline = Date.now() + view.remaining * 1000;
  if (typeof view.score === "number") {
    stormScore = view.score;
    $("pz-storm-score").textContent = stormScore;
  }
  if (typeof view.combo === "number") {
    stormCombo = view.combo;
    const el = $("pz-storm-combo");
    if (stormCombo >= 2) {
      el.hidden = false;
      el.textContent = "🔥 " + stormCombo + " combo";
    } else {
      el.hidden = true;
    }
  }
  if (view.results) {
    $("pz-storm-results").innerHTML = view.results
      .slice(-16)
      .map((ok) => `<span class="pz-pip ${ok ? "ok" : "bad"}"></span>`)
      .join("");
  }
  const clockEl = $("pz-storm-clock");
  const remaining = (stormDeadline - Date.now()) / 1000;
  clockEl.textContent = fmtClock(remaining);
  clockEl.classList.toggle("low", remaining <= 10);
}

// Load a storm puzzle onto the board. Storm serves the solve position directly (no setup-move
// animation), so we just render it and hand the move to the solver.
function applyStormPuzzle(pz) {
  const myGen = ++stormGen;
  stormPuzzle = pz;
  stormBusy = false;
  puzzleShapes = [];
  puzzleLastMove = null;
  puzzleSolveColor = pz.side_to_move || "white";
  orient = puzzleSolveColor;
  chess.load(pz.fen);
  renderPuzzleBoard(true);
  setStormSide(puzzleSolveColor);
  return myGen;
}

// Show which side the solver plays for the current storm puzzle (the board also flips to it, but
// the explicit label removes any doubt in a fast-paced run). Hidden when no puzzle is on the board.
function setStormSide(color) {
  const el = $("pz-storm-side");
  if (!el) return;
  if (!color) {
    el.hidden = true;
    return;
  }
  const white = color === "white";
  el.innerHTML = `<span class="pz-storm-side-dot ${white ? "w" : "b"}"></span>You play ${white ? "White" : "Black"}`;
  el.hidden = false;
}

async function stormServeNext() {
  let view;
  try {
    view = await puzzleApi.stormNext({ signal: renewStormRequests() });
  } catch (_) {
    pzStatus("Couldn't load the next puzzle.");
    return;
  }
  if (!stormRunning) return;
  if (view.ended || !view.puzzle) {
    finishStorm(view);
    return;
  }
  applyStormState(view);
  applyStormPuzzle(view.puzzle);
}

async function onStormMove(orig, dest) {
  if (stormBusy || !stormRunning) {
    renderPuzzleBoard(!stormBusy);
    return;
  }
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");
  const mv = tryMove({ from: orig, to: dest, promotion: promo });
  if (!mv) {
    renderPuzzleBoard(true);
    return;
  }
  puzzleLastMove = [orig, dest];
  stormBusy = true;
  const myGen = stormGen;
  renderPuzzleBoard(false);

  let res;
  try {
    res = await puzzleApi.stormMove(
      { uci },
      { signal: stormAbortController && stormAbortController.signal }
    );
  } catch (_) {
    if (myGen !== stormGen) return;
    chess.undo();
    renderPuzzleBoard(true);
    stormBusy = false;
    return;
  }
  if (myGen !== stormGen || !stormRunning) return;

  if (res.error || res.ended) {
    finishStorm(res);
    return;
  }
  applyStormState(res);

  if (!res.correct) {
    // Wrong move: red flash, undo, and move straight on to the next puzzle (storm never retries).
    blinkSquare(dest, "bad");
    shakeBoard();
    await sleep(360);
    if (myGen !== stormGen || !stormRunning) return;
    chess.undo();
    puzzleLastMove = null;
    stormBusy = false;
    stormServeNext();
    return;
  }

  if (res.puzzle_done && res.solved) {
    // Storm is a race, so a clean solve jumps straight to the next puzzle — no green-tile blink or
    // pause (they'd bleed onto the puzzle that's already replaced them, looking janky). The clock
    // bonus still floats over the clock, which is off-board and doesn't delay anything.
    if (res.time_bonus) floatBonus("+" + Math.round(res.time_bonus) + "s");
    stormBusy = false;
    stormServeNext();
    return;
  }

  // Correct but more to come: play the forced reply, keep the same puzzle.
  await sleep(220);
  if (myGen !== stormGen || !stormRunning) return;
  const reply = res.opponent_reply_uci;
  if (reply) {
    chess.move({ from: reply.slice(0, 2), to: reply.slice(2, 4), promotion: reply.slice(4) || undefined });
    puzzleLastMove = [reply.slice(0, 2), reply.slice(2, 4)];
  }
  renderPuzzleBoard(true);
  stormBusy = false;
}

// A brief floating "+5s" over the clock when a combo grants bonus time.
function floatBonus(text) {
  const clockEl = $("pz-storm-clock");
  if (!clockEl) return;
  const b = document.createElement("span");
  b.className = "pz-storm-bonus";
  b.textContent = text;
  clockEl.appendChild(b);
  setTimeout(() => b.remove(), 900);
}

// The run ended (time up or server-finished). Show the game-over card + refresh personal bests.
function finishStorm(view) {
  stormRunning = false;
  stormBusy = false;
  stopStormClock();
  stormResetBoard();
  if (view) {
    if (view.new_high || typeof view.high === "number") {
      if (puzzleConfigCache) {
        puzzleConfigCache.storm_high = Math.max(puzzleConfigCache.storm_high || 0, view.high || view.score || 0);
        if (view.best_combo) {
          puzzleConfigCache.storm_best_combo = Math.max(puzzleConfigCache.storm_best_combo || 0, view.best_combo);
        }
      }
    }
    stormShowStart(true, view);
  } else {
    stormShowStart(false);
  }
  // Turn the finished rush into study time: list every puzzle for AI review. The finish view
  // usually carries the log inline; fall back to an API request (refresh-safe while the run lingers).
  if (view && Array.isArray(view.log)) {
    populateStormReview(view.log);
  } else {
    puzzleApi.stormReview()
      .then((d) => populateStormReview((d && d.log) || []))
      .catch(() => populateStormReview([]));
  }
}

// --- post-run review: study the puzzles from a finished storm with the AI coach -----------------

// Show the review list on the game-over card (misses pinned first). `has_llm` gates the AI bits.
function populateStormReview(log) {
  stormReviewEntries = Array.isArray(log) ? log : [];
  const wrap = $("pz-storm-review");
  const list = $("pz-storm-review-list");
  const summaryBtn = $("pz-storm-summary-btn");
  $("pz-storm-summary-out").hidden = true;
  $("pz-storm-summary-out").innerHTML = "";
  if (!stormReviewEntries.length) {
    wrap.hidden = true;
    return;
  }
  const hasLlm = !!(puzzleConfigCache && puzzleConfigCache.has_llm);
  summaryBtn.hidden = !hasLlm;
  summaryBtn.disabled = false;
  // Misses first (that's where the learning is), then solves; stable within each group.
  const rows = stormReviewEntries
    .map((e, i) => ({ e, i }))
    .sort((a, b) => (a.e.solved === b.e.solved ? a.i - b.i : a.e.solved ? 1 : -1));
  list.innerHTML = "";
  rows.forEach(({ e }) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "pz-storm-review-row " + (e.solved ? "ok" : "bad");
    const label = motifThemes(e.themes).slice(0, 2).join(", ") || "tactic";
    row.innerHTML =
      `<span class="pz-rr-mark">${e.solved ? "✓" : "✗"}</span>` +
      `<span class="pz-rr-theme">${escapeHtml(label)}</span>` +
      `<span class="pz-rr-rating">${e.rating || ""}</span>`;
    row.addEventListener("click", () => openStormReview(e));
    list.appendChild(row);
  });
  wrap.hidden = false;
}

// Open one finished storm puzzle on the board for review, reusing the Solve rail's result card so
// the existing "Explain why" + follow-up chat work unchanged (they ground on puzzleData.id + fen).
function openStormReview(entry) {
  inStormReview = true;
  ++puzzleGen; // become the current puzzle; supersede any in-flight Solve-trainer handler
  puzzleBusy = false;
  puzzleDone = true;
  puzzleSource = "lichess";
  puzzleData = {
    id: entry.id,
    fen: entry.fen,
    solve_fen: entry.fen,
    themes: entry.themes || [],
    source: "lichess",
    side_to_move: entry.side_to_move || "white",
    _outcome: entry.solved ? "solved_first_try" : "failed",
    _yourMove: entry.your_move || null,
  };
  puzzleFailed = !entry.solved;
  puzzleHinted = false;
  puzzleMissRating = null;
  puzzleChatReset();
  puzzleSolveColor = entry.side_to_move || "white";
  orient = puzzleSolveColor;
  // A miss shows the move the solver played, in red, so the review has a concrete starting point.
  puzzleShapes = !entry.solved && entry.your_move
    ? [{ orig: entry.your_move.slice(0, 2), dest: entry.your_move.slice(2, 4), brush: "red" }]
    : [];

  // Swap the storm scoreboard for the Solve result card (we stay in the storm sub-mode).
  $("pz-storm").hidden = true;
  $("pz-solve").hidden = false;
  $("pz-prompt").hidden = true;
  $("pz-ghosts").hidden = true;
  $("pz-progress").hidden = true;
  $("pz-statcard").hidden = true;
  $("pz-source-badge").hidden = true;
  $("pz-result").hidden = false;

  const verdict = $("pz-verdict");
  verdict.className = "pz-verdict " + (entry.solved ? "ok" : "bad");
  verdict.textContent = entry.solved ? "✓ You solved this" : "✗ You missed this";
  const themes = motifThemes(entry.themes);
  $("pz-theme").innerHTML = themes.length ? "Theme: <b>" + escapeHtml(themes.slice(0, 3).join(", ")) + "</b>" : "";
  $("pz-replay").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-explain-out").innerHTML = "";
  $("pz-explain").hidden = !(puzzleConfigCache && puzzleConfigCache.has_llm);
  $("pz-explain").disabled = false;
  $("pz-chat").hidden = true;
  $("pz-chat-messages").innerHTML = "";
  $("pz-next").textContent = "‹ Back to results"; // repurposed while reviewing (routed in the handler)

  chess.load(entry.fen);
  renderPuzzleBoard(false); // locked review board; draws the played-move arrow at the solve position
  pzStatus("Reviewing a storm puzzle — step through the solution below the board, or press Explain.");
  // Fetch + animate the solution, then leave the step-nav for scrubbing (storm review starts at the
  // solve position and plays forward).
  startSolutionPlayback({
    id: entry.id, yourMove: entry.your_move, solved: entry.solved, animate: true,
  });
}

// Fetch a curated puzzle's solution line and set up the step-through nav below the board. Shared by
// Storm review and the normal Solve trainer. `animate` plays the line forward once (storm review);
// otherwise it rests at the final position (the Solve board has already played the moves out).
// Cancels cleanly (via `solutionGen`) if the user leaves or loads another puzzle mid-fetch.
async function startSolutionPlayback({ id, yourMove = null, solved = true, animate = false }) {
  const myGen = ++solutionGen;
  solutionPlay = null;
  $("pz-review-nav").hidden = true;
  if (!id) return;
  let sol;
  try {
    sol = await puzzleApi.solution(id);
  } catch (_) {
    return; // no solution available (e.g. a mistake puzzle) -> the card still works, just no playback
  }
  if (myGen !== solutionGen) return;
  const base = (sol && sol.solve_fen) || null;
  const ucis = (sol && sol.solution_uci) || [];
  const sans = (sol && sol.solution_san) || [];
  if (!base || !ucis.length) return;
  // Build the board position at each step of the solution, starting from the solve position.
  const fens = [base];
  const lastMoves = [null];
  const tmp = board.createGame(base);
  for (const u of ucis) {
    const mv = tmp.move({ from: u.slice(0, 2), to: u.slice(2, 4), promotion: u.slice(4) || undefined });
    if (!mv) break; // defensive: a bad line just stops the playback where it is
    fens.push(tmp.fen());
    lastMoves.push([u.slice(0, 2), u.slice(2, 4)]);
  }
  solutionPlay = {
    fens, ucis: ucis.slice(0, fens.length - 1), sans, lastMoves,
    idx: 0, yourMove, solved,
  };
  if (animate) {
    solutionGotoStep(0);
    for (let i = 1; i < fens.length; i++) {
      await sleep(650);
      if (myGen !== solutionGen) return;
      solutionGotoStep(i);
    }
  } else {
    // Rest at the final position (the Solve board is already there); the user scrubs backward.
    solutionGotoStep(fens.length - 1);
  }
}

// Show a specific step of the solution on the board (idx 0 = the solve position).
function solutionGotoStep(i) {
  const p = solutionPlay;
  if (!p) return;
  p.idx = Math.max(0, Math.min(p.fens.length - 1, i));
  chess.load(p.fens[p.idx]);
  puzzleLastMove = p.lastMoves[p.idx] || null;
  if (p.idx === 0) {
    // At the start, a miss shows the move the solver actually played (red); a clean solve shows nothing.
    puzzleShapes = !p.solved && p.yourMove
      ? [{ orig: p.yourMove.slice(0, 2), dest: p.yourMove.slice(2, 4), brush: "red" }]
      : [];
  } else {
    const u = p.ucis[p.idx - 1];
    puzzleShapes = [{ orig: u.slice(0, 2), dest: u.slice(2, 4), brush: "green" }];
  }
  renderPuzzleBoard(false);
  updateSolutionNav();
}

// Manual scrub: cancel any running auto-animation, then step one move.
function solutionStep(delta) {
  if (!solutionPlay) return;
  ++solutionGen;
  solutionGotoStep(solutionPlay.idx + delta);
}

function updateSolutionNav() {
  const nav = $("pz-review-nav");
  const p = solutionPlay;
  if (!p) {
    nav.hidden = true;
    return;
  }
  nav.hidden = false;
  $("pz-review-prev").disabled = p.idx <= 0;
  $("pz-review-next").disabled = p.idx >= p.fens.length - 1;
  const total = p.fens.length - 1;
  $("pz-review-label").textContent =
    p.idx === 0 ? "Start position" : `Move ${p.idx} / ${total}: ${p.sans[p.idx - 1] || ""}`;
}

// Tear down any active solution playback + hide the step nav (new puzzle, leaving, mode switch).
function clearSolutionPlayback() {
  ++solutionGen;
  solutionPlay = null;
  const nav = $("pz-review-nav");
  if (nav) nav.hidden = true;
}

// Leave the single-puzzle review and return to the game-over card + review list.
function closeStormReview() {
  inStormReview = false;
  clearSolutionPlayback();
  puzzleChatReset();
  $("pz-next").textContent = "Next puzzle →";
  $("pz-result").hidden = true;
  $("pz-explain-out").hidden = true;
  $("pz-explain-out").innerHTML = "";
  $("pz-solve").hidden = true;
  $("pz-storm").hidden = false;
}

async function summarizeStormRun() {
  const btn = $("pz-storm-summary-btn");
  const out = $("pz-storm-summary-out");
  btn.disabled = true;
  out.hidden = false;
  out.innerHTML = '<p class="muted">Snowie is reviewing your run (thinking)</p>';
  let res;
  try {
    res = await puzzleApi.stormSummary();
  } catch (_) {
    out.innerHTML = '<p class="muted">Summary failed — try again.</p>';
    btn.disabled = false;
    return;
  }
  out.innerHTML = renderMarkdown(res.error || res.answer || "");
  btn.disabled = false;
}

// End the run when leaving storm mode entirely (tell the server so the highscore is banked).
function endStormRun({ abandon } = {}) {
  stopStormClock();
  if (stormRunning && abandon) {
    puzzleApi.stormEnd().catch(() => {});
  }
  stormRunning = false;
  stormBusy = false;
  stormPuzzle = null;
}

// The amber "From your game" badge for a mistake puzzle (null hides it for curated tactics).
function renderPuzzleBadge(p) {
  const el = $("pz-source-badge");
  if (!p) { el.hidden = true; el.textContent = ""; return; }
  const b = p.badge || {};
  const opp = p.reviewed_side === "white" ? b.black : b.white;
  const bits = ["From your game"];
  if (opp) bits.push("vs " + opp);
  if (b.speed && b.speed !== "unknown") bits.push(b.speed);
  if (b.date) bits.push(b.date);
  if (p.category) bits.push(categoryLabel(p.category));
  el.textContent = bits.join(" · ");
  el.hidden = false;
}

// "Replay in full game": leave puzzle mode and open the source game at the mistake position. Needs
// the game's PGN, which the history list carries; falls back to the external game link if absent.
async function replayMistake(p) {
  let row = null;
  try {
    const rows = await gamesApi.history();
    row = (rows || []).find(
      (r) => r.game_id === p.game_id && r.reviewed_side === p.reviewed_side && r.has_pgn && r.pgn
    );
  } catch (_) {}
  if (row) {
    await lifecycle.replayGame(row, p);
  } else if (p.game_url) {
    window.open(p.game_url, "_blank", "noopener");
  }
}

// Cancel a pending auto-advance (manual nav, leaving puzzle mode, or the user chose to Explain/chat
// about this puzzle — in which case yanking them to the next one would be rude).
function cancelAutoAdvance() {
  if (puzzleAdvanceTimer !== null) {
    clearTimeout(puzzleAdvanceTimer);
    puzzleAdvanceTimer = null;
  }
}

// Show/hide the "‹ Previous" ghost button (only meaningful while a restorable snapshot exists).
function updatePrevPuzzleButton() {
  const btn = $("pz-prev");
  if (btn) btn.hidden = !prevPuzzleSnapshot;
}

function clearPrevPuzzle() {
  prevPuzzleSnapshot = null;
  updatePrevPuzzleButton();
}

// Snapshot the current FINISHED puzzle (board + fully-rendered result card, incl. any Explain text
// and follow-up chat) so restorePrevPuzzle can bring it back verbatim for review. Called as we leave
// a done puzzle. Restoring is review-only (board stays locked), so we don't need server puzzle state;
// Explain/chat re-ground on puzzleData.id + the stored fen.
function capturePrevPuzzle() {
  if (!puzzleDone || !puzzleData) return;
  prevPuzzleSnapshot = {
    data: puzzleData,
    shapes: puzzleShapes,
    failed: puzzleFailed,
    hinted: puzzleHinted,
    solveColor: puzzleSolveColor,
    missRating: puzzleMissRating,
    fen: chess.fen(),
    lastMove: puzzleLastMove,
    chatSession: puzzleChatSession,
    chatFen: puzzleChatFen,
    // Rendered result-card DOM so the verdict / theme / explanation / chat all survive intact.
    verdictText: $("pz-verdict").textContent,
    verdictClass: $("pz-verdict").className,
    themeHTML: $("pz-theme").innerHTML,
    replayHidden: $("pz-replay").hidden,
    explainHidden: $("pz-explain").hidden,
    explainDisabled: $("pz-explain").disabled,
    explainOutHTML: $("pz-explain-out").innerHTML,
    explainOutHidden: $("pz-explain-out").hidden,
    chatHTML: $("pz-chat-messages").innerHTML,
    chatHidden: $("pz-chat").hidden,
  };
  updatePrevPuzzleButton();
}

// Bring back the last finished puzzle in review mode: the board (locked) + its result card, so the
// player can still press Explain / ask a follow-up about a puzzle auto-advance already moved past.
function restorePrevPuzzle() {
  const s = prevPuzzleSnapshot;
  if (!s) return;
  cancelAutoAdvance();
  clearSolutionPlayback(); // restored review shows the snapshot board, not a live step-through
  ++puzzleGen; // become the current puzzle; supersede any in-flight handler
  prevPuzzleSnapshot = null; // single-level back; a later Next re-captures this one
  puzzleBusy = false;
  puzzleDone = true;
  puzzleData = s.data;
  puzzleShapes = s.shapes;
  puzzleFailed = s.failed;
  puzzleHinted = s.hinted;
  puzzleSolveColor = s.solveColor;
  puzzleMissRating = s.missRating;
  puzzleLastMove = s.lastMove;
  puzzleChatSession = s.chatSession;
  puzzleChatFen = s.chatFen;
  orient = puzzleSolveColor;
  chess.load(s.fen);

  // Restore the result card verbatim; hide the prompt/ghosts (it's a finished puzzle).
  $("pz-prompt").hidden = true;
  $("pz-ghosts").hidden = true;
  $("pz-result").hidden = false;
  $("pz-verdict").textContent = s.verdictText;
  $("pz-verdict").className = s.verdictClass;
  $("pz-theme").innerHTML = s.themeHTML;
  $("pz-replay").hidden = s.replayHidden;
  if (!s.replayHidden) $("pz-replay").onclick = (e) => { e.preventDefault(); replayMistake(puzzleData); };
  $("pz-explain").hidden = s.explainHidden;
  $("pz-explain").disabled = s.explainDisabled;
  $("pz-explain-out").hidden = s.explainOutHidden;
  $("pz-explain-out").innerHTML = s.explainOutHTML;
  $("pz-chat").hidden = s.chatHidden;
  $("pz-chat-messages").innerHTML = s.chatHTML;

  renderPuzzleBoard(false); // locked (done): draws the stored solution/refutation arrows
  pzStatus("Reviewing your previous puzzle — press Explain or ask below, then Next to continue.");
  updatePrevPuzzleButton();
}

// After a solve, queue the next puzzle — but only once the solve animation has had time to play out,
// per the §7A "let the green pulse be the reward" feel. Delay comfortably exceeds the confetti/ripple
// (~1.1s); with animations off, a shorter beat so the result text is still readable first.
function scheduleAutoAdvance() {
  cancelAutoAdvance();
  if (!puzzleAutoAdvance) return;
  const myGen = puzzleGen;
  const delay = puzzleAnimations ? 1900 : 1000;
  puzzleAdvanceTimer = setTimeout(() => {
    puzzleAdvanceTimer = null;
    if (!puzzleMode || stormShown) return; // left the trainer while waiting
    if (myGen !== puzzleGen) return; // a newer puzzle/nav superseded this one
    if (!puzzleDone) return; // defensive: only advance from a finished puzzle
    loadNextPuzzle();
  }, delay);
}

function personalTrainingResultHtml(feedback, yourMove) {
  if (!feedback) return "";
  const selected = (feedback.selected_move || {}).san || yourMove || "No move submitted";
  const gameMove = (feedback.game_move || {}).san || puzzleData.played_san || "—";
  const bestMove = (feedback.best_move || {}).san || "—";
  const variation = ((feedback.best_line || feedback.variation || {}).san || []).join(" ") || "—";
  return (
    `<dl class="training-result">` +
    `<dt>Your choice</dt><dd>${escapeHtml(selected)}</dd>` +
    `<dt>Game choice</dt><dd>${escapeHtml(gameMove)}</dd>` +
    `<dt>Engine choice</dt><dd>${escapeHtml(bestMove)}</dd>` +
    `<dt>Key variation</dt><dd>${escapeHtml(variation)}</dd>` +
    `<dt>Error reason</dt><dd>${escapeHtml(feedback.original_error_reason || "—")}</dd>` +
    `<dt>Principle</dt><dd>${escapeHtml(feedback.transferable_principle || "—")}</dd>` +
    `</dl>`
  );
}

function finishPuzzle(outcome, ratingSummary, yourMove, trainingFeedback = null) {
  puzzleDone = true;
  $("pz-prompt").hidden = true;
  $("pz-ghosts").hidden = true;
  $("pz-result").hidden = false;

  const solved = outcome !== "failed";
  const clean = solved && !puzzleFailed; // green only when solved with no wrong move
  const isMine = puzzleData.source === "your_games";
  const verdict = $("pz-verdict");
  // Three states, so a correct final attempt never wears the same red as a genuine failure:
  //   green  = solved first try, orange = solved after a miss (still a win!), red = solution shown.
  verdict.className = "pz-verdict " + (!solved ? "bad" : clean ? "ok" : "part");
  let head;
  if (isMine) {
    // Coaching tone: these are the player's own past positions, not pass/fail tactics, and unrated.
    head = solved
      ? clean ? "✓ Better than your game move!" : "✓ Better move found (after a try)"
      : "Solution shown";
  } else {
    head = solved ? (clean ? "✓ Solved!" : "✓ Solved (after a miss)") : "Solution shown";
    // The rating may have been applied on the FIRST wrong move (Lichess-style), so the completion
    // itself returns no fresh summary. Fall back to that stored miss summary so a solve-after-miss
    // still shows the rating already moved — otherwise it looks like the miss cost nothing.
    const applied =
      ratingSummary && ratingSummary.rated
        ? ratingSummary
        : puzzleMissRating && puzzleMissRating.rated
        ? puzzleMissRating
        : null;
    if (applied) {
      const sign = applied.delta >= 0 ? "+" : "";
      head += `  ${applied.rating_before} → ${applied.rating_after} (${sign}${applied.delta})`;
      updatePuzzleStats(applied.rating_after, applied.streak);
    } else if (ratingSummary || puzzleMissRating) {
      const su = ratingSummary || puzzleMissRating;
      head += "  (unrated)";
      updatePuzzleStats(su.rating_after, su.streak);
    }
  }
  verdict.textContent = head;

  // Session pips track curated-tactic outcomes only (mistake puzzles are unrated practice).
  if (!isMine) recordPuzzleResult(clean);

  if (isMine) {
    puzzleData._trainingFeedback = trainingFeedback;
    $("pz-theme").innerHTML = personalTrainingResultHtml(trainingFeedback, yourMove);
    const replay = $("pz-replay");
    replay.hidden = false;
    replay.onclick = (e) => { e.preventDefault(); replayMistake(puzzleData); };
  } else {
    const themes = (puzzleData.themes || []).filter((t) => !/^mateIn\d|^oneMove$|^short$|^long$/.test(t));
    $("pz-theme").innerHTML = themes.length ? "Theme: <b>" + themes.slice(0, 3).join(", ") + "</b>" : "";
    $("pz-replay").hidden = true;
  }

  // Stash for the Explain call.
  puzzleData._outcome = outcome;
  puzzleData._yourMove = yourMove || null;
  $("pz-explain").hidden = !(puzzleConfigCache && puzzleConfigCache.has_llm);
  loadPuzzleStatCard();

  // Let the player walk the solution move-by-move with the step-nav below the board. Curated tactics
  // only — "from your games" puzzles have no forced line to replay (they use "replay in full game").
  // The board is already at the end of the line, so we rest there and let them scrub backward.
  if (!isMine) {
    startSolutionPlayback({ id: puzzleData.id, solved, animate: false });
  } else {
    clearSolutionPlayback();
  }

  // Flow-state grinding: on a solve, roll straight into the next puzzle after the animation plays
  // (opt-out in Settings). Never on "Show solution" (outcome "failed") — that's a study moment.
  if (solved) scheduleAutoAdvance();
}

// Weakest-theme stats card, from /api/puzzle/state (quiet; hidden when there's nothing to show).
async function loadPuzzleStatCard() {
  const card = $("pz-statcard");
  if (!card) return;
  let st;
  try {
    st = await puzzleApi.state();
  } catch (_) {
    return;
  }
  // Daily streak + the discrete rating curve ride on the same state fetch.
  renderDailyStreak(st && st.daily_streak, st && st.best_daily_streak);
  renderRatingCurve(st && st.history);
  // The server already filters to trainable motifs (no metadata tags like master/oneMove) and
  // ranks them worst-first, so the card just renders what it's given.
  const weak = (st && st.weak_themes) || [];
  if (!weak.length) { card.hidden = true; card.innerHTML = ""; return; }
  // Collapsed by default to keep the rail quiet: just a "Work on" toggle. Click reveals the themes
  // (the open/closed choice is remembered).
  const open = lsGet("pzWorkOnOpen") === "1";
  card.innerHTML =
    `<button type="button" class="pz-statcard-toggle" aria-expanded="${open}">` +
      `Work on <span class="pz-statcard-caret">${open ? "▾" : "▸"}</span></button>` +
    `<div class="pz-statcard-body"${open ? "" : " hidden"}>` +
      weak
        .map((x) => `<span class="pz-weak-theme">${x.theme} <b>${Math.round(x.rate * 100)}%</b></span>`)
        .join("") +
    `</div>`;
  card.hidden = false;
  card.querySelector(".pz-statcard-toggle").addEventListener("click", () => {
    const body = card.querySelector(".pz-statcard-body");
    const nowOpen = body.hidden; // about to open
    body.hidden = !nowOpen;
    card.querySelector(".pz-statcard-caret").textContent = nowOpen ? "▾" : "▸";
    card.querySelector(".pz-statcard-toggle").setAttribute("aria-expanded", String(nowOpen));
    lsSet("pzWorkOnOpen", nowOpen ? "1" : "0");
  });
}

// "Show solution": reveal + play out the remaining solution line, then end the puzzle.
async function puzzleShowSolution() {
  if (!puzzleData || puzzleDone || puzzleBusy) return;
  const myGen = puzzleGen;
  puzzleBusy = true;
  renderPuzzleBoard(false);
  let res;
  try {
    res = await puzzleApi.giveUp(puzzleData.id);
  } catch (_) {
    if (myGen !== puzzleGen) return;
    puzzleBusy = false;
    renderPuzzleBoard(true);
    return;
  }
  if (myGen !== puzzleGen) return;
  const line = (res && res.solution_uci) || [];
  // Play the solution out move by move so the user sees the idea.
  for (const uci of line) {
    chess.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci.slice(4) || undefined });
    puzzleLastMove = [uci.slice(0, 2), uci.slice(2, 4)];
    renderPuzzleBoard(false);
    await sleep(480);
    if (myGen !== puzzleGen) return;
  }
  puzzleBusy = false;
  if (puzzleData.source === "your_games") {
    const best = (res && res.best_move) || {};
    if (best.uci) {
      puzzleShapes = [{ orig: best.uci.slice(0, 2), dest: best.uci.slice(2, 4), brush: "green" }];
      renderPuzzleBoard(false);
    }
  }
  finishPuzzle("failed", null, null, puzzleData.source === "your_games" ? res : null);
}

async function puzzleExplain() {
  cancelAutoAdvance(); // the user wants to study this one — don't yank them to the next puzzle
  const btn = $("pz-explain");
  const out = $("pz-explain-out");
  btn.disabled = true;
  out.hidden = false;
  out.innerHTML = '<p class="muted">Snowie is sniffing around (thinking)</p>';
  let res;
  try {
    res = await puzzleApi.explain({
        id: puzzleData.id,
        outcome: puzzleData._outcome,
        your_move: puzzleData._yourMove,
      });
  } catch (_) {
    out.innerHTML = '<p class="muted">Explanation failed — try again.</p>';
    btn.disabled = false;
    return;
  }
  if (res.error) {
    out.innerHTML = renderMarkdown(res.error);
  } else {
    out.innerHTML = renderMarkdown(res.answer || "");
    // Reveal the follow-up chat, threaded onto this explanation so questions have its context.
    puzzleChatSession = res.session_id || null;
    puzzleChatFen = res.chat_fen || puzzleData.solve_fen || puzzleData.fen || null;
    $("pz-chat").hidden = false;
  }
  btn.disabled = false;
}

async function playPersonalHintLine(line) {
  if (!puzzleData || !(line && line.uci || []).length) return;
  const myGen = puzzleGen;
  puzzleBusy = true;
  chess.load(puzzleData.solve_fen || puzzleData.fen);
  puzzleLastMove = null;
  renderPuzzleBoard(false);
  for (const uci of line.uci) {
    await sleep(480);
    if (myGen !== puzzleGen) return;
    const move = chess.move({
      from: String(uci).slice(0, 2),
      to: String(uci).slice(2, 4),
      promotion: String(uci).slice(4, 5) || undefined,
    });
    if (!move) break;
    puzzleLastMove = [String(uci).slice(0, 2), String(uci).slice(2, 4)];
    renderPuzzleBoard(false);
  }
  await sleep(650);
  if (myGen !== puzzleGen) return;
  chess.load(puzzleData.solve_fen || puzzleData.fen);
  puzzleLastMove = null;
  puzzleBusy = false;
  renderPuzzleBoard(true);
}

async function puzzleHint() {
  if (!puzzleData || puzzleDone) return;
  let res;
  try {
    res = await puzzleApi.hint(puzzleData.id);
  } catch (_) {
    return;
  }
  if (res && res.kind) {
    puzzleHinted = true;
    puzzleShapes = res.shapes || [];
    drawArrows();
    pzStatus(res.text || "Hint revealed.");
    if (res.line) await playPersonalHintLine(res.line);
  } else if (res && res.from_square) {
    puzzleHinted = true;
    puzzleShapes = [{ orig: res.from_square, brush: "blue" }];
    drawArrows();
    pzStatus("Hint: move the piece on " + res.from_square + " (this attempt is now unrated).");
  }
}

// Reflect the current source in the segmented control + show difficulty/weakness only for tactics.
function syncSourceUI() {
  const mine = puzzleSource === "your_games";
  $("pz-src-tactics").classList.toggle("active", !mine);
  $("pz-src-mine").classList.toggle("active", mine);
  $("pz-controls").style.display = mine ? "none" : "";
  $("pz-mine-filter").hidden = !mine;
  $("pz-category").value = puzzleCategory;
}

async function loadPuzzleCategories(options) {
  const select = $("pz-category");
  if (!select) return;
  let categories = [];
  try {
    const result = await puzzleApi.categories(options);
    categories = (result && result.categories) || [];
  } catch (error) {
    if (error && error.name === "AbortError") throw error;
  }
  select.innerHTML = '<option value="">All categories</option>' + categories
    .map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(categoryLabel(item))}</option>`)
    .join("");
  if (puzzleCategory && !categories.includes(puzzleCategory)) puzzleCategory = "";
  select.value = puzzleCategory;
}

function setPuzzleSource(src) {
  if (src === puzzleSource) return;
  if (src === "your_games" && !(puzzleConfigCache && puzzleConfigCache.has_engine)) return;
  puzzleSource = src;
  lsSet(PZ_SOURCE_KEY, src); // remember the sub-tab across reloads + full app restarts
  syncSourceUI();
  if (src === "your_games") loadPuzzleCategories();
  loadNextPuzzle();
}

function setPuzzleDifficulty(which) {
  puzzleDifficulty = puzzleDifficulty === which ? null : which; // click again to clear
  $("pz-easier").classList.toggle("active", puzzleDifficulty === "easier");
  $("pz-harder").classList.toggle("active", puzzleDifficulty === "harder");
  loadNextPuzzle();
}


  function mount() {
    $("mode-analyze").addEventListener("click", () => setPuzzleMode(false));
    $("mode-puzzles").addEventListener("click", () => setPuzzleMode(true));
    $("pz-next").addEventListener("click", () => (inStormReview ? closeStormReview() : loadNextPuzzle()));
    $("pz-skip").addEventListener("click", () => loadNextPuzzle());
    $("pz-prev").addEventListener("click", restorePrevPuzzle);
    $("pz-hint").addEventListener("click", puzzleHint);
    $("pz-solution").addEventListener("click", puzzleShowSolution);
    $("pz-explain").addEventListener("click", puzzleExplain);
    $("pz-chat-form").addEventListener("submit", sendPuzzleChat);
    $("pz-src-tactics").addEventListener("click", () => setPuzzleSource("lichess"));
    $("pz-src-mine").addEventListener("click", () => setPuzzleSource("your_games"));
    $("pz-category").addEventListener("change", (event) => {
      puzzleCategory = event.target.value || "";
      lsSet(PZ_CATEGORY_KEY, puzzleCategory);
      if (puzzleMode && puzzleSource === "your_games") loadNextPuzzle();
    });
    $("pz-weakness").addEventListener("change", (event) => {
      puzzleWeakness = event.target.checked;
      loadNextPuzzle();
    });
    $("pz-easier").addEventListener("click", () => setPuzzleDifficulty("easier"));
    $("pz-harder").addEventListener("click", () => setPuzzleDifficulty("harder"));
    $("pz-mode-solve").addEventListener("click", () => setStormMode(false));
    $("pz-mode-storm").addEventListener("click", () => setStormMode(true));
    $("pz-storm-start").addEventListener("click", startStorm);
    $("pz-storm-summary-btn").addEventListener("click", summarizeStormRun);
    $("pz-review-prev").addEventListener("click", () => solutionStep(-1));
    $("pz-review-next").addEventListener("click", () => solutionStep(1));
  }

  function handleKeydown(event) {
    if (solutionPlay) {
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        solutionStep(-1);
        return true;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        solutionStep(1);
        return true;
      }
    }
    return puzzleMode;
  }

  function setPreferences(preferences = {}) {
    personalizeHistory = preferences.personalizeHistory !== false;
    puzzleAnimations = preferences.animations !== false;
    puzzleAutoAdvance = preferences.autoAdvance === true;
  }

  return {
    mount,
    handleKeydown,
    handleMove(orig, dest) {
      return stormShown && stormRunning ? onStormMove(orig, dest) : onPuzzleMove(orig, dest);
    },
    setMode: setPuzzleMode,
    async train({ category = "", gameId = null, criticalId = null } = {}) {
      puzzleSource = "your_games";
      puzzleCategory = category;
      lsSet(PZ_SOURCE_KEY, puzzleSource);
      lsSet(PZ_CATEGORY_KEY, puzzleCategory);
      await setPuzzleMode(true, {
        puzzle: { source: "your_games", category, game_id: gameId, critical_id: criticalId },
      });
    },
    setPreferences,
    setConfig(config) {
      puzzleConfigCache = config;
    },
    get modeKey() {
      return PZ_MODE_KEY;
    },
    get shouldResume() {
      return lsGet(PZ_MODE_KEY) === "1";
    },
    get active() {
      return puzzleMode;
    },
    get stormActive() {
      return stormShown && stormRunning;
    },
  };
}
