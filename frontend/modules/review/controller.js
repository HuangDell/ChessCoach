import { reviewApi } from "../api/review.js";
import { byId, clamp, sleep } from "../core/dom.js";

export function createReviewController({ board, bridge }) {
  const $ = byId;
  const chess = board.chess;
  const ground = board.ground;
  let analyzing = false;
  let pollTimer = null;
  let batchInfo = null;
  let pendingCriticalId = null;
  let pendingGotoPly = null;
  let coachAiAuto = false;
  let coachAiToken = 0;
  let personalizeHistory = true;
  let searchAbortController = null;
  let analysisAbortController = null;

let timeline = []; // nodes 0..N for the whole game
let mistakes = [];
let player = "white"; // the reviewed side (drives the header label)
let orient = "white"; // board orientation; starts at `player` but the `f` hotkey flips it
// Current game's PGN + player names, so "Review other side" can re-open the same game reviewing
// the opponent without a refetch. Set when a game is opened (provisional) and on /session.
let currentPgn = null;
let gameWhite = "";
let gameBlack = "";
// URL of the current game on Lichess/Chess.com (from the PGN's Site/Link header), for the ↗ link
// in the board header. Null when the PGN carried no such URL (e.g. an offline/local PGN).
let currentGameUrl = null;
let currentGameId = null;

// Phase 6 artifact-backed review workspace. Engine facts and model explanations are deliberately
// separate: the board remains fully usable when the latter are absent or fail.
let engineReview = null;
let criticalPositions = [];
let explanationArtifact = null;
let activeCriticalId = null;
let reviewView = "key";
let explanationGeneration = 0;
let explanationBusy = false;
let reviewVariation = null; // {kind, ucis, sans, fens, idx, timer, playing}
let boardLastMove = null;
let defaultReviewSide = "auto";
let boardOrientationPreference = "review";
let analysisPreset = "balanced";
let explanationProvider = "auto";
let explanationLanguage = "zh-CN";
let showThreatsByDefault = false;
let retrySession = null; // {criticalId,state,hintsUsed,locked,shapes,saved,solutionGen}

let cur = 0; // current timeline node (valid when !exploring)
let anchorNode = 0; // the review (mistake) node we started from
let currentMistake = -1;
let currentPrompt = "";

let exploring = false; // off the game line, free-playing variations
let exploreBaseNode = 0; // node we left the timeline from
// Verdict for the move just tried in explore mode, surfaced in the (always-visible) status
// banner under the board — the full #verdict panel lives far down the scrolling side column,
// so without this you'd never see "good / mistake / blunder" for a variation you played.
// null = none, "pending" = evaluating, move dict = result, {error:true} = failed.
let exploreVerdict = null;

let bestArrowOn = false;
// Live best-move arrows: progressively deepen and refine while you sit on a position,
// cancelled the moment the position changes, with a hard time cap so it never runs forever.
let bestArrows = [];
// Threat arrows (yellow): what the side that just moved is threatening to play next
// (a null-move engine search server-side). Toggled like the best-move arrows.
let threatArrowOn = false;
let threatArrows = [];
const THREAT_DEPTH = 16; // one fixed-depth probe is enough for "what's the threat?"
let searchGen = 0; // bumped on every position change to invalidate in-flight searches
const SEARCH_DEPTHS = [14, 18, 22]; // escalating precision; arrows update after each
const SEARCH_MAX_MS = 5000; // stop deepening after this, even if more depth is available
const SEARCH_DEBOUNCE_MS = 120; // coalesce rapid navigation before hitting the engine
let evalShapes = []; // extra board shapes from the last /api/evaluate (e.g. red refutation arrow)
// Chat context: always the position BEFORE the move in question + that move's SAN, so Claude
// can ground "why is this bad?" on the exact move regardless of timeline vs. explore mode.
let chatFen = null;
let chatMove = null;
let chatSession = null; // claude -p session id, threaded across questions
let chatGen = 0; // bumped on each game open; invalidates an in-flight restoreChat for the old game


  const apiErrorMessage = (value, fallback) => {
    if (value && typeof value === "object") return value.message || fallback;
    return value || fallback;
  };

  function gameLink(url, className) {
    if (!url || !/^https?:\/\//i.test(url)) return null;
    let host = "the source site";
    try {
      host = new URL(url).hostname.replace(/^www[.]/, "");
    } catch (_) {}
    const link = document.createElement("a");
    link.className = className || "game-open";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "↗";
    link.title = "Open on " + host;
    link.setAttribute("aria-label", "Open this game on " + host);
    link.addEventListener("click", (event) => event.stopPropagation());
    return link;
  }

  function setGameMeta(white, black, url, tail) {
    const element = $("game-meta");
    if (!element) return;
    element.textContent = "";
    const names = document.createElement("span");
    names.textContent = `${white || "White"} vs ${black || "Black"}`;
    element.appendChild(names);
    const link = gameLink(url, "meta-open");
    if (link) element.appendChild(link);
    if (tail) element.appendChild(document.createTextNode(tail));
  }

  function gameUrlFromHeaders(headers) {
    for (const key of ["Site", "Link"]) {
      const value = (headers && headers[key] != null ? String(headers[key]) : "").trim();
      if (/^https?:\/\//i.test(value)) return value;
    }
    return null;
  }

  function pgnHeaders(pgn) {
    try {
      const game = board.createGame();
      game.loadPgn(pgn);
      const headers = game.header();
      return { white: headers.White, black: headers.Black, url: gameUrlFromHeaders(headers) };
    } catch (_) {
      return {};
    }
  }

// --- chess helpers -------------------------------------------------------
function computeDests() {
  return board.computeDests();
}
function turnColor() {
  return board.turnColor();
}
function isPromotion(from, to) {
  return board.isPromotion(from, to);
}
function tryMove(move) {
  return board.tryMove(move);
}
function samePosition(fenA, fenB) {
  // compare board + side-to-move + castling + ep, ignore clocks
  return fenA.split(" ").slice(0, 4).join(" ") === fenB.split(" ").slice(0, 4).join(" ");
}
function pieceGlyph(san) {
  if (san.startsWith("O-O")) return "♚";
  return { N: "♞", B: "♝", R: "♜", Q: "♛", K: "♚" }[san[0]] || "♟";
}

// --- board rendering -----------------------------------------------------
function renderBoard() {
  const color = turnColor();
  const movable = !retrySession || !retrySession.locked;
  ground.set({
    fen: chess.fen(),
    orientation: orient,
    turnColor: color,
    check: chess.inCheck(),
    lastMove: boardLastMove,
    movable: {
      color: movable ? color : undefined,
      dests: movable ? computeDests() : new Map(),
      free: false,
      showDests: true,
    },
  });
  drawArrows();
}

function arrowShape(uci, brush) {
  return { orig: uci.slice(0, 2), dest: uci.slice(2, 4), brush };
}
// True when we're parked on a selected mistake's anchor: the position BEFORE your move, with you
// to move. Free browsing (no mistake selected, or scrubbed away) sits AFTER the last move instead.
function atMistakeAnchor() {
  return currentMistake >= 0 && cur === anchorNode;
}

function activeCritical() {
  return criticalPositions.find((item) => item.critical_id === activeCriticalId) || null;
}

function reviewArrowShapes() {
  const critical = activeCritical();
  if (!critical || exploring || cur !== Number(critical.ply) - 1) return [];
  const shapes = [];
  const played = critical.played_move && critical.played_move.uci;
  const best = critical.best_line && critical.best_line.uci && critical.best_line.uci[0];
  const reply = critical.opponent_best_reply && critical.opponent_best_reply.uci;
  if (played) shapes.push(arrowShape(played, "grey"));
  if (best && best !== played) shapes.push(arrowShape(best, "green"));
  if (reply) shapes.push(arrowShape(reply, "red"));
  return shapes;
}

// The timeline node whose move is "under review" at the cursor: at a mistake anchor it's this
// node's own OUTGOING move (you're before it, playing it back); while browsing it's the move that
// just landed us here (cur - 1). Returns -1 at the very start (no move to show).
function reviewedMoveNode() {
  const critical = activeCritical();
  if (critical && cur === Number(critical.ply) - 1) return Number(critical.ply) - 1;
  return atMistakeAnchor() ? cur : cur - 1;
}

function drawArrows() {
  if (retrySession) {
    ground.setAutoShapes(retrySession.shapes || []);
    return;
  }
  const shapes = reviewArrowShapes();
  // The move you actually played, drawn only at a mistake anchor. There the board sits on the
  // position BEFORE your move (you're to move, so the green best-move arrow is for YOUR side), so
  // the played move is this node's OUTGOING move. Grey = neutral "here's what you did", shown
  // alongside the green best move so you can compare what you did vs. what was best.
  if (!activeCritical() && !exploring && !analyzing && atMistakeAnchor() && timeline[cur] && timeline[cur].move_uci) {
    shapes.push(arrowShape(timeline[cur].move_uci, "grey"));
  }
  if (bestArrowOn) for (const a of bestArrows) shapes.push(a);
  if (threatArrowOn) for (const a of threatArrows) shapes.push(a);
  for (const s of evalShapes) shapes.push(s);
  // autoShapes (not setShapes): app-managed annotations that survive piece press/drag and
  // only change when we redraw — so the played-move arrow stays until you actually move.
  ground.setAutoShapes(shapes);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Map top engine moves → arrows. The best move is a bold arrow; alternatives are
// clearly thinner (with proportionally smaller heads, since chessground scales the arrowhead
// with stroke width) so the recommendation stands out at a glance.
function movesToArrows(moves, brush = "green", boldWidth = 13) {
  if (!moves.length) return [];
  const best = moves[0].win_percent;
  const out = [];
  for (let i = 0; i < moves.length; i++) {
    const delta = best - moves[i].win_percent;
    if (i > 0 && delta > 12) break; // only surface genuinely good alternatives
    // best = bold; alternatives start much thinner (≤7) and taper with how much worse.
    const lineWidth = i === 0 ? boldWidth : Math.max(4, 7 - delta);
    out.push({
      orig: moves[i].uci.slice(0, 2),
      dest: moves[i].uci.slice(2, 4),
      brush,
      modifiers: { lineWidth },
    });
  }
  return out;
}

// Refresh the engine-driven arrows (best moves + threats) for the current position. Bumps
// searchGen so any in-flight search for a previous position cancels itself; each enabled
// arrow kind then fetches independently.
function refreshBestMoves() {
  if (searchAbortController) searchAbortController.abort();
  searchAbortController = new AbortController();
  searchGen += 1; // cancel any in-flight search
  bestArrows = [];
  threatArrows = [];
  drawArrows();
  const myGen = searchGen;
  const fen = chess.fen();
  if (bestArrowOn) deepenBestMoves(fen, myGen, searchAbortController.signal);
  if (threatArrowOn) fetchThreats(fen, myGen, searchAbortController.signal);
}

// Run an escalating-depth best-move search; cancels itself on any position change (searchGen)
// and stops after SEARCH_MAX_MS.
async function deepenBestMoves(fen, myGen, signal) {
  await sleep(SEARCH_DEBOUNCE_MS); // coalesce rapid arrow-key scrubbing
  if (myGen !== searchGen) return;
  const t0 = performance.now();
  for (const depth of SEARCH_DEPTHS) {
    if (myGen !== searchGen) return;
    let res;
    try {
      res = await reviewApi.bestMoves({ fen, depth, multipv: 3 }, { signal });
    } catch (_) {
      return;
    }
    if (myGen !== searchGen) return; // superseded while the engine was thinking
    if (res && res.moves && res.moves.length) {
      bestArrows = movesToArrows(res.moves);
      drawArrows();
    }
    if (performance.now() - t0 > SEARCH_MAX_MS) break; // time cap
  }
}

// One fixed-depth null-move probe: what does the side that just moved threaten to play next?
// Drawn as yellow arrows, slightly thinner than the green best-move arrows.
async function fetchThreats(fen, myGen, signal) {
  await sleep(SEARCH_DEBOUNCE_MS);
  if (myGen !== searchGen) return;
  let res;
  try {
    res = await reviewApi.threats({ fen, depth: THREAT_DEPTH, multipv: 3 }, { signal });
  } catch (_) {
    return;
  }
  if (myGen !== searchGen) return;
  if (res && res.moves) {
    threatArrows = movesToArrows(res.moves, "yellow", 11);
    drawArrows();
  }
}

// The eval bar matches board orientation: the side at the BOTTOM of the board fills from
// the bottom. White-at-bottom (reviewing white) → white fills up; black-at-bottom → black.
function applyEvalBarTheme() {
  const light = "#f0f0f0";
  const dark = "#2b2a27";
  const fill = $("evalbar-fill");
  const bar = $("evalbar");
  if (orient === "white") {
    fill.style.background = light;
    bar.style.background = dark;
  } else {
    fill.style.background = dark;
    bar.style.background = light;
  }
}
function setEvalBar(winWhite) {
  const w = winWhite == null ? 50 : winWhite; // phase-1 (no eval yet) -> neutral
  const bottomShare = orient === "white" ? w : 100 - w;
  $("evalbar-fill").style.height = `${clamp(bottomShare, 0, 100)}%`;
}

// --- verdict / status ----------------------------------------------------
function renderVerdict(payload) {
  if (!payload) return void ($("verdict").innerHTML = "");
  if (payload.error) return void ($("verdict").innerHTML = `<span class="line">${payload.error}</span>`);
  const m = payload.move;
  const refute = m.refutation_line_san.slice(0, 6).join(" ");
  const better = m.is_engine_best ? "Engine's top choice." : `Best was <b>${m.better_move_san}</b>.`;
  // "best" classification = within BEST_EPS of the top move. If it's NOT literally the engine's
  // top choice, show it as "good" so the badge doesn't contradict the "Best was …" text.
  const label = m.classification === "best" && !m.is_engine_best ? "good" : m.classification;
  $("verdict").innerHTML =
    `<span class="tag ${label}">${label}</span>` +
    `<b>${m.move_san}</b> — win ${m.win_before}% → ${m.win_after}% ` +
    `(swing ${m.win_swing}, eval ${m.eval_after}). ${better}` +
    (refute ? `<div class="line">Reply: ${refute}</div>` : "");
}

function nodeLabel(i) {
  const n = timeline[i];
  if (!n || i === 0) return "the start";
  const prev = timeline[i - 1];
  return `${prev.move_number}${prev.color === "white" ? "." : "…"} ${prev.move_san}`;
}

// Compact verdict for the move just tried in explore mode, shown inline in the status banner.
function exploreVerdictHtml() {
  if (exploreVerdict === "pending") return ` <span class="line">evaluating…</span>`;
  if (!exploreVerdict) return "";
  if (exploreVerdict.error) return ` <span class="line">couldn't evaluate that move</span>`;
  const m = exploreVerdict;
  // Mirror renderVerdict: a "best" that isn't literally the engine's #1 reads as "good".
  const label = m.classification === "best" && !m.is_engine_best ? "good" : m.classification;
  return (
    ` <span class="tag ${label}">${label}</span>` +
    `<b>${escapeHtml(m.move_san)}</b> — win ${m.win_before}% → ${m.win_after}%` +
    (m.is_engine_best ? "" : ` · best was <b>${escapeHtml(m.better_move_san || "")}</b>`)
  );
}

function updateStatus() {
  const el = $("status");
  if (retrySession) {
    el.className = "status";
    const labels = {
      awaiting_move: "Retry: choose a move on the board.",
      evaluating: "Retry: evaluating your move…",
      feedback: "Retry feedback is ready.",
      showing_hint: "Retry: use the hint, then choose a move.",
      showing_solution: "Retry: studying the Engine line.",
      completed: "Retry completed.",
    };
    el.textContent = labels[retrySession.state] || "Retry this position.";
  } else if (exploring) {
    el.className = "status away";
    el.innerHTML =
      `🔍 Exploring a variation.${exploreVerdictHtml()} ` +
      `<button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else if (cur !== anchorNode) {
    el.className = "status away";
    el.innerHTML = `Viewing ${nodeLabel(cur)} — not the review move. <button id="ret">Back to review move</button>`;
    $("ret").onclick = returnToReview;
  } else {
    el.className = "status";
    // Grade the move under review (the mistake's own move at an anchor, else the last move played).
    const mv = reviewedMoveNode();
    const g = mv >= 0 && timeline[mv] ? classGlyph(timeline[mv].classification) : "";
    el.innerHTML = g + (g ? " " : "") + escapeHtml(currentPrompt || nodeLabel(cur));
  }
}

// --- navigation ----------------------------------------------------------
function gotoNode(n) {
  if (!timeline.length) return;
  stopReviewVariation();
  exploring = false;
  cur = clamp(n, 0, timeline.length - 1);
  evalShapes = [];
  // chat context: the "move in question" is the reviewed move for this cursor (the mistake's own
  // move at an anchor, else the move that just landed us here). chatFen is where it was played from.
  const mv = reviewedMoveNode();
  if (mv >= 0 && timeline[mv] && timeline[mv].move_san) {
    chatFen = timeline[mv].fen;
    chatMove = timeline[mv].move_san;
  } else {
    chatFen = timeline[cur] ? timeline[cur].fen : null;
    chatMove = null;
  }
  const lastUci = mv >= 0 && timeline[mv] ? timeline[mv].move_uci : null;
  boardLastMove = lastUci ? [lastUci.slice(0, 2), lastUci.slice(2, 4)] : null;
  chess.load(timeline[cur].fen);
  renderBoard();
  setEvalBar(timeline[cur].win_white);
  renderVerdict(null);
  updateStatus();
  updateNav();
  renderGraph();
  highlightCurrentMove();
  syncReviewCursor();
  refreshBestMoves();
}

function returnToReview() {
  const critical = activeCritical();
  if (critical) selectCritical(critical.critical_id);
  else gotoNode(anchorNode);
}

// Flip the board (hotkey `f`). The eval bar + win graph follow `orient`, so flip them too.
function flipBoard() {
  orient = orient === "white" ? "black" : "white";
  applyEvalBarTheme();
  renderBoard();
  setEvalBar(timeline[cur] ? timeline[cur].win_white : 50);
  renderGraph();
}

// Toggle the "Show best move" arrows from the keyboard (hotkey `l`), keeping the checkbox in sync.
function toggleBestArrows() {
  const box = $("best-toggle");
  box.checked = !box.checked;
  bestArrowOn = box.checked;
  refreshBestMoves();
}

// Toggle the yellow "Show threats" arrows from the keyboard (hotkey `t`), keeping the checkbox in sync.
function toggleThreatArrows() {
  const box = $("threat-toggle");
  box.checked = !box.checked;
  threatArrowOn = box.checked;
  refreshBestMoves();
}

function stepBack() {
  if (exploring) undoOne();
  else if (cur > 0) gotoNode(cur - 1);
}
function stepForward() {
  if (!exploring && cur < timeline.length - 1) gotoNode(cur + 1);
}

function undoOne() {
  chess.undo();
  if (samePosition(chess.fen(), timeline[exploreBaseNode].fen)) {
    gotoNode(exploreBaseNode); // rejoined the game line
    return;
  }
  chatFen = chess.fen(); // backed up mid-line: ask about the position, no single move
  chatMove = null;
  exploreVerdict = null; // no specific move under judgement at the backed-up position
  renderBoard();
  renderVerdict(null);
  updateStatus();
  renderGraph();
  syncExplore(); // refresh the eval bar for the new explored position
  refreshBestMoves(); // and the best-move arrows
}

async function syncExplore() {
  try {
    const info = await reviewApi.bestMove({ fen: chess.fen() });
    setEvalBar(info.side_to_move === "white" ? info.win_percent : 100 - info.win_percent);
  } catch (_) {}
}

// --- user moves ----------------------------------------------------------
async function onUserMove(orig, dest) {
  if (retrySession) return onRetryMove(orig, dest);
  if (reviewVariation) stopReviewVariation();
  const moverColor = turnColor();
  const fenBefore = chess.fen();
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo ?? "");

  // Following the actual game move while on the timeline → just advance.
  if (!exploring && timeline[cur] && timeline[cur].move_uci === uci) {
    const fm = tryMove({ from: orig, to: dest, promotion: promo });
    if (!fm) {
      renderBoard();
      return;
    }
    chatFen = fenBefore;
    chatMove = (fm && fm.san) || null;
    cur += 1;
    boardLastMove = [orig, dest];
    renderBoard();
    setEvalBar(timeline[cur].win_white);
    renderVerdict(null);
    updateStatus();
    updateNav();
    renderGraph();
    highlightCurrentMove();
    syncReviewCursor();
    refreshBestMoves();
    return;
  }

  // Otherwise we're exploring a variation.
  if (!exploring) {
    exploring = true;
    exploreBaseNode = cur;
  }
  const moveObj = tryMove({ from: orig, to: dest, promotion: promo });
  if (!moveObj) {
    renderBoard();
    return;
  }
  boardLastMove = [orig, dest];
  chatFen = fenBefore; // position before the move in question (consistent in explore mode)
  chatMove = (moveObj && moveObj.san) || null;
  evalShapes = [];
  exploreVerdict = "pending"; // banner shows "evaluating…" until the engine replies
  renderBoard();
  updateStatus();
  renderGraph();
  refreshBestMoves(); // live best-move arrows for the new position

  $("verdict").innerHTML = `<span class="line">Evaluating…</span>`;
  let res;
  try {
    res = await reviewApi.evaluate({ fen: fenBefore, move: uci });
  } catch (err) {
    // Never leave the verdict stuck on "Evaluating…": surface the failure so the user
    // can retry instead of thinking the board froze.
    exploreVerdict = { error: true };
    updateStatus();
    renderVerdict({ error: "Couldn't evaluate that move — the engine may be busy or restarting. Try again." });
    return;
  }
  exploreVerdict = res.move || (res.error ? { error: true } : null);
  updateStatus(); // surface good/mistake/blunder in the always-visible banner under the board
  renderVerdict(res);
  if (res.move) {
    setEvalBar(moverColor === "white" ? res.move.win_after : 100 - res.move.win_after);
    evalShapes = res.shapes || []; // red refutation arrow drawn on the resulting position
    drawArrows();
  }
}

// --- win graph -----------------------------------------------------------
const GW = 1000;
const GH = 100;

function updateTimelineReadout() {
  const out = $("timeline-readout");
  const node = timeline[cur];
  if (!out || !node) return;
  let score = null;
  if (engineReview && engineReview.moves) {
    const move = engineReview.moves.find((item) => Number(item.ply) === cur + 1);
    const previous = engineReview.moves.find((item) => Number(item.ply) === cur);
    score = (move && move.eval_before) || (previous && previous.eval_after) || null;
  }
  let scoreText = "";
  if (score && score.type === "mate") {
    const value = Number(score.value) || 0;
    scoreText = ` · ${value < 0 ? "-" : ""}M${Math.abs(value)}`;
  } else if (score && score.type === "cp") {
    const value = Number(score.value) / 100;
    scoreText = ` · ${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
  }
  out.textContent = `Ply ${cur} · White ${node.win_white == null ? "—" : `${node.win_white}%`}${scoreText}`;
}

function renderGraph() {
  const svg = $("graph");
  const n = timeline.length;
  updateTimelineReadout();
  if (n < 2) {
    svg.innerHTML = "";
    return;
  }
  svg.setAttribute("viewBox", `0 0 ${GW} ${GH}`);
  const x = (i) => (i / (n - 1)) * GW;
  const y = (w) => GH - (w / 100) * GH;
  // Plot from the reviewed player's perspective, matching the eval bar: the filled area
  // grows from the bottom as YOUR side does better, so for black it reads black-on-bottom.
  // During phase-1 (analysing) nodes have no win_white yet -> treat as 50 (flat baseline).
  const hasEval = timeline.some((nd) => nd.win_white != null);
  const val = (nd) => {
    const w = nd.win_white == null ? 50 : nd.win_white;
    return orient === "white" ? w : 100 - w;
  };

  // Two-tone fill split at the eval curve, mirroring the eval bar: each side keeps its own
  // colour (light = White, dark = Black) and the reviewed player's side sits on the bottom.
  const pts = timeline.map((nd, i) => `${x(i).toFixed(1)},${y(val(nd)).toFixed(1)}`).join(" L");
  const belowArea = `M0,${GH} L${pts} L${GW},${GH} Z`; // bottom = the player's side
  const aboveArea = `M0,0 L${pts} L${GW},0 Z`; // top = the opponent's side
  const LIGHT = "rgba(236,234,228,0.22)"; // White
  const DARK = "rgba(0,0,0,0.45)"; // Black
  const bottomFill = orient === "white" ? LIGHT : DARK;
  const topFill = orient === "white" ? DARK : LIGHT;

  const line = timeline
    .map((nd, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(val(nd)).toFixed(1)}`)
    .join(" ");

  const flagged = criticalPositions.length
    ? criticalPositions
        .map((critical) => ({
          nd: timeline[Number(critical.ply) - 1],
          critical,
        }))
        .filter((item) => item.nd)
    : timeline
        .filter((nd) => nd.mistake_index != null)
        .map((nd) => ({ nd, critical: null }));
  const mistakeDots = flagged
    .map(
      ({ nd, critical }) =>
        `<circle cx="${x(nd.node).toFixed(1)}" cy="${y(val(nd)).toFixed(1)}" r="3.5" ` +
        `fill="${classColor((critical && critical.classification) || nd.classification)}" vector-effect="non-scaling-stroke"/>`
    )
    .join("");

  // Transparent, full-height click targets over each mistake (a ply-wide band) so clicking a dot
  // reliably opens it via real SVG hit-testing — no fragile pixel math — while clicks elsewhere on
  // the graph hit nothing. `data-mi` is the index into the mistakes array (see onGraphClick).
  const half = GW / (n - 1) / 2;
  const mistakeHits = flagged
    .map(
      ({ nd, critical }) =>
        `<rect class="mdot-hit" ${critical ? `data-critical="${escapeHtml(critical.critical_id)}"` : `data-mi="${nd.mistake_index}"`} pointer-events="all" ` +
        `x="${(x(nd.node) - half).toFixed(1)}" y="0" width="${(half * 2).toFixed(1)}" ` +
        `height="${GH}" fill="transparent"/>`
    )
    .join("");

  const mateLabels = (engineReview && engineReview.moves ? engineReview.moves : [])
    .filter((move) => move.eval_after && move.eval_after.type === "mate")
    .map((move) => {
      const nd = timeline[Number(move.ply)];
      if (!nd) return "";
      const raw = Number(move.eval_after.value) || 0;
      const label = `${raw < 0 ? "-" : ""}M${Math.abs(raw)}`;
      return `<text x="${x(nd.node).toFixed(1)}" y="${Math.max(9, y(val(nd)) - 6).toFixed(1)}" ` +
        `fill="#f3c9c9" font-size="8" text-anchor="middle" vector-effect="non-scaling-stroke">${label}</text>`;
    })
    .join("");

  const cx = x(cur).toFixed(1);
  const cy = y(val(timeline[cur])).toFixed(1);
  const marker =
    `<line x1="${cx}" y1="0" x2="${cx}" y2="${GH}" stroke="#629924" stroke-width="1" vector-effect="non-scaling-stroke"/>` +
    `<circle cx="${cx}" cy="${cy}" r="4" fill="#629924" vector-effect="non-scaling-stroke"/>`;

  const analyzingNote = hasEval
    ? ""
    : `<text x="${GW / 2}" y="${GH / 2 - 4}" fill="#9c9890" font-size="9" text-anchor="middle" ` +
      `vector-effect="non-scaling-stroke">analyzing… moves are navigable now</text>`;

  svg.innerHTML =
    `<rect x="0" y="0" width="${GW}" height="${GH}" fill="#14130f"/>` +
    `<path d="${aboveArea}" fill="${topFill}"/>` +
    `<path d="${belowArea}" fill="${bottomFill}"/>` +
    `<line x1="0" y1="${GH / 2}" x2="${GW}" y2="${GH / 2}" stroke="#4a4843" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` +
    (hasEval
      ? `<path d="${line}" fill="none" stroke="#e8e6e3" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`
      : "") +
    mistakeDots +
    mateLabels +
    marker +
    analyzingNote +
    mistakeHits; // last = on top, so the transparent bands reliably catch clicks
}

function classColor(cls) {
  return (
    { inaccuracy: "#e0a800", mistake: "#e08000", blunder: "#dd3333" }[cls] || "#629924"
  );
}

// A small graded badge for a move's classification (only the reviewed player's moves carry one).
// `good` and opponent moves (null) get no glyph so the notation stays readable.
const GLYPHS = { blunder: "??", mistake: "?", inaccuracy: "?!", best: "✓" };
function classGlyph(cls) {
  const g = GLYPHS[cls];
  return g ? `<span class="glyph ${cls}">${g}</span>` : "";
}

// Clicking a flagged-mistake dot opens that mistake (same as the mistakes tab, via selectMistake) —
// detected by real SVG hit-testing of the transparent bands drawn in renderGraph (robust to the
// stretched viewBox). Clicking anywhere else on the graph scrubs to that ply (plain gotoNode jump).
function onGraphClick(ev) {
  const criticalTarget = ev.target && ev.target.closest && ev.target.closest("[data-critical]");
  if (criticalTarget) {
    selectCritical(criticalTarget.getAttribute("data-critical"));
    return;
  }
  const target = ev.target && ev.target.closest && ev.target.closest("[data-mi]");
  if (target) {
    selectMistake(Number(target.getAttribute("data-mi")));
    return;
  }
  const n = timeline.length;
  if (n < 2) return;
  const rect = $("graph").getBoundingClientRect();
  const frac = (ev.clientX - rect.left) / rect.width;
  gotoNode(Math.round(frac * (n - 1))); // gotoNode clamps to a valid node
}

// --- mistakes list -------------------------------------------------------
function renderMistakeList() {
  const ol = $("mistakes");
  ol.innerHTML = "";
  if (analyzing && !mistakes.length) {
    const li = document.createElement("li");
    li.className = "ph";
    li.textContent = "Analyzing… mistakes will appear here when the engine finishes.";
    ol.appendChild(li);
    return;
  }
  mistakes.forEach((m, i) => {
    const li = document.createElement("li");
    li.dataset.index = i;
    const num = `${m.move_number}${m.color === "white" ? "." : "…"}`;
    li.innerHTML =
      `<span class="move"><span class="dot ${m.classification}"></span>` +
      `<span class="piece-glyph">${pieceGlyph(m.move_san)}</span>${num} ${m.move_san}</span>` +
      `<span class="muted">${m.classification} −${m.win_swing}</span>`;
    li.addEventListener("click", () => selectMistake(i));
    ol.appendChild(li);
  });
}

async function selectMistake(i) {
  const stored = mistakes[i];
  const critical = stored && criticalPositions.find((item) => Number(item.ply) === Number(stored.ply));
  if (critical) return selectCritical(critical.critical_id);
  const myGen = chatGen;
  const pos = await reviewApi.position(i);
  if (myGen !== chatGen) return; // a different game opened while we were fetching
  currentMistake = i;
  currentPrompt = pos.error ? "" : pos.prompt;
  // Land on the position BEFORE the mistake (you're on the move) so the engine's best-move arrow
  // and any move you try are for YOUR side; the grey arrow still shows the move you actually played.
  anchorNode = mistakes[i].node_index;
  [...$("mistakes").children].forEach((li) =>
    li.classList.toggle("active", Number(li.dataset.index) === i)
  );
  gotoNode(anchorNode);
  $("comment").textContent = mistakes[i].comment || "";
}

// --- scoreboard (game report header) -------------------------------------
// Headline stats for the reviewed side, derived entirely from the /session payload: opening,
// per-side accuracy, and counts of blunders / mistakes / inaccuracies (the `mistakes` array is
// exactly the reviewed player's flagged moves). Clicking a count chip jumps to the first of that
// class. Rendered from applySession (phase-2), so it only ever shows real numbers.
function renderScoreboard(session) {
  const board = $("scoreboard");
  if (!board) return;
  const reviewed = session.player === "black" ? "black" : "white";
  const sideLabel = reviewed === "white" ? "White" : "Black";
  const myAcc = reviewed === "white" ? session.accuracy_white : session.accuracy_black;
  const oppAcc = reviewed === "white" ? session.accuracy_black : session.accuracy_white;
  const counts = { blunder: 0, mistake: 0, inaccuracy: 0 };
  (session.mistakes || []).forEach((m) => {
    if (counts[m.classification] != null) counts[m.classification] += 1;
  });
  board.innerHTML =
    `<div class="sb-opening" title="Opening">${escapeHtml(session.opening || "—")}</div>` +
    `<div class="sb-acc">` +
    `<span class="sb-acc-main"><b>${myAcc}</b><span class="sb-acc-lbl">accuracy (${sideLabel})</span></span>` +
    `<span class="sb-acc-opp">opponent ${oppAcc}</span>` +
    `</div>` +
    `<div class="sb-counts">` +
    scoreboardChip("blunder", counts.blunder, "Blunders") +
    scoreboardChip("mistake", counts.mistake, "Mistakes") +
    scoreboardChip("inaccuracy", counts.inaccuracy, "Inaccuracies") +
    `</div>`;
  board.hidden = false;
  board.querySelectorAll(".chip[data-cls]").forEach((el) =>
    el.addEventListener("click", () => jumpToClass(el.dataset.cls))
  );
}

function scoreboardChip(cls, n, label) {
  return (
    `<button type="button" class="chip ${cls}" data-cls="${cls}" title="${label}">` +
    `<span class="chip-n">${n}</span> <span class="chip-lbl">${label}</span></button>`
  );
}

function jumpToClass(cls) {
  const i = mistakes.findIndex((m) => m.classification === cls);
  if (i >= 0) selectMistake(i);
}

// --- move list (clickable notation) --------------------------------------
// The full game as a compact, scrollable two-column notation panel. Built from the same `timeline`
// the graph/arrows use, so it works on the provisional (phase-1) timeline too — glyphs just fill in
// when engine analysis lands. Clicking a flagged move routes through selectMistake (surfacing its
// comment + anchor); any other move is a plain gotoNode jump.
function renderMoveList() {
  const ol = $("movelist");
  if (!ol) return;
  ol.innerHTML = "";
  const plies = timeline.filter((nd) => nd.move_san); // skip the final (terminal) node
  if (!plies.length) return;
  const rows = new Map(); // move_number -> {w, b}
  for (const nd of plies) {
    if (!rows.has(nd.move_number)) rows.set(nd.move_number, { w: null, b: null });
    rows.get(nd.move_number)[nd.color === "white" ? "w" : "b"] = nd;
  }
  for (const [num, pair] of rows) {
    const li = document.createElement("li");
    li.className = "move-row";
    li.innerHTML = `<span class="moveno">${num}.</span>${plyCell(pair.w)}${plyCell(pair.b)}`;
    ol.appendChild(li);
  }
  ol.querySelectorAll(".ply[data-node]").forEach((el) =>
    el.addEventListener("click", () => onMoveClick(Number(el.dataset.node)))
  );
  highlightCurrentMove();
}

function plyCell(nd) {
  if (!nd) return `<span class="ply empty"></span>`;
  return `<span class="ply" data-node="${nd.node}">${classGlyph(nd.classification)}${nd.move_san}</span>`;
}

function onMoveClick(i) {
  const nd = timeline[i];
  const critical = nd && criticalPositions.find((item) => Number(item.ply) === Number(nd.ply));
  if (critical) selectCritical(critical.critical_id);
  else if (nd && engineReview) selectEngineMove(nd.ply);
  else if (nd && nd.mistake_index != null) selectMistake(nd.mistake_index);
  else gotoNode(i + 1); // show the position with the clicked move just completed
}

// Highlight the move at the current node and keep it scrolled into view (works in compact mode).
function highlightCurrentMove() {
  const ol = $("movelist");
  if (!ol) return;
  let active = null;
  ol.querySelectorAll(".ply[data-node]").forEach((el) => {
    // Highlight the move under review: the mistake's own move at an anchor, else the last move.
    const on = Number(el.dataset.node) === reviewedMoveNode();
    el.classList.toggle("active", on);
    if (on) active = el;
  });
  if (active) active.scrollIntoView({ block: "nearest" });
}

// Compact (a few rows, scrollable) <-> expanded (whole game). Default is compact.
function toggleMoveList() {
  const ol = $("movelist");
  if (!ol) return;
  const expanded = ol.classList.toggle("expanded");
  ol.classList.toggle("compact", !expanded);
  $("movelist-expand").textContent = expanded ? "Collapse ▴" : "Show all ▾";
  highlightCurrentMove();
}

function updateNav() {
  $("back").disabled = !exploring && cur <= 0;
  $("fwd").disabled = exploring || cur >= timeline.length - 1;
  const criticalIndex = criticalPositions.findIndex((item) => item.critical_id === activeCriticalId);
  $("prev-mistake").disabled = criticalPositions.length
    ? criticalIndex <= 0
    : currentMistake <= 0;
  $("next-mistake").disabled = criticalPositions.length
    ? criticalIndex < 0 || criticalIndex >= criticalPositions.length - 1
    : currentMistake < 0 || currentMistake >= mistakes.length - 1;
}

// --- artifact-backed review workspace ----------------------------------
function setWorkflowState(state, label, detail = "", count = "") {
  const box = $("workflow-status");
  if (!box) return;
  box.dataset.state = state;
  $("workflow-label").textContent = label;
  $("workflow-detail").textContent = detail;
  $("workflow-count").textContent = count;
}

function reviewMoveLabel(item) {
  const side = item.side || item.color;
  const san = (item.played_move && item.played_move.san) || item.move_san || "—";
  return `${item.move_number}${side === "black" ? "…" : "."} ${san}`;
}

function categoryLabel(value) {
  const labels = {
    allowed_mate: "Allowed mate",
    missed_mate: "Missed mate",
    wrong_exchange_sequence: "Exchange sequence",
    missed_opponent_threat: "Opponent threat",
    hanging_piece: "Hanging piece",
    missed_capture: "Missed capture",
    fork: "Fork",
  };
  return labels[value] || String(value || "Uncategorized").replaceAll("_", " ");
}

function scoreLabel(score) {
  if (!score) return "—";
  const value = Number(score.value) || 0;
  if (score.type === "mate") return `${value < 0 ? "-" : ""}M${Math.abs(value)}`;
  const pawns = value / 100;
  return `${pawns >= 0 ? "+" : ""}${pawns.toFixed(2)}`;
}

function criticalSwingLabel(critical) {
  if (critical.win_loss != null) return `−${Number(critical.win_loss).toFixed(1)}% win chance`;
  return `${scoreLabel(critical.eval_before)} → ${scoreLabel(critical.eval_after)}`;
}

function explanationFor(criticalId) {
  return ((explanationArtifact && explanationArtifact.positions) || []).find(
    (item) => item.critical_id === criticalId
  ) || null;
}

function mistakeReviewItems() {
  if (engineReview && engineReview.moves) {
    return engineReview.moves.filter(
      (move) =>
        move.side === player &&
        ["inaccuracy", "mistake", "blunder"].includes(move.classification)
    );
  }
  return mistakes;
}

function setReviewView(view) {
  reviewView = ["key", "mistakes", "all"].includes(view) ? view : "key";
  document.querySelectorAll(".review-tabs button").forEach((button) => {
    const active = button.dataset.view === reviewView;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("movelist-panel").hidden = reviewView !== "all";
  $("review-position-list").hidden = reviewView === "all";
  renderReviewList();
}

function renderReviewList() {
  const list = $("review-position-list");
  if (!list) return;
  list.innerHTML = "";
  if (reviewView === "all") return;
  const items = reviewView === "key" ? criticalPositions : mistakeReviewItems();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "position-meta";
    empty.textContent = analyzing ? "Key positions will appear after analysis." : "No positions in this view.";
    list.appendChild(empty);
    return;
  }
  for (const item of items) {
    const critical = item.critical_id
      ? item
      : criticalPositions.find((entry) => Number(entry.ply) === Number(item.ply));
    const button = document.createElement("button");
    button.type = "button";
    button.className = "position-item";
    button.dataset.ply = String(item.ply);
    if (critical) button.dataset.criticalId = critical.critical_id;
    const facts = critical && critical.facts;
    const category = facts && facts.primary_category ? categoryLabel(facts.primary_category) : "Engine review";
    const swing = critical ? criticalSwingLabel(critical) : `−${Number(item.win_percent_loss ?? item.win_swing ?? 0).toFixed(1)}%`;
    button.innerHTML =
      `<span class="position-dot ${escapeHtml(item.classification || "")}"></span>` +
      `<span class="position-copy"><span class="position-move">${escapeHtml(reviewMoveLabel(item))}</span>` +
      `<span class="position-meta">${escapeHtml(item.classification || "move")} · ${escapeHtml(swing)}<br>${escapeHtml(category)}</span></span>`;
    button.classList.toggle(
      "active",
      critical ? critical.critical_id === activeCriticalId : Number(item.ply) === reviewedMoveNode() + 1
    );
    button.addEventListener("click", () => {
      if (critical) selectCritical(critical.critical_id);
      else selectEngineMove(Number(item.ply));
    });
    list.appendChild(button);
  }
}

function selectEngineMove(ply) {
  const critical = criticalPositions.find((item) => Number(item.ply) === Number(ply));
  if (critical) return selectCritical(critical.critical_id);
  activeCriticalId = null;
  currentMistake = -1;
  anchorNode = Math.min(timeline.length - 1, Number(ply));
  currentPrompt = `Reviewing ${reviewMoveLabel((engineReview.moves || []).find((item) => Number(item.ply) === Number(ply)) || {})}`;
  gotoNode(anchorNode);
  renderCursorEngineReview();
}

function selectCritical(criticalId) {
  const critical = criticalPositions.find((item) => item.critical_id === criticalId);
  if (!critical) return;
  activeCriticalId = criticalId;
  anchorNode = Math.max(0, Number(critical.ply) - 1);
  currentMistake = mistakes.findIndex((item) => Number(item.ply) === Number(critical.ply));
  currentPrompt = `Key position · ${reviewMoveLabel(critical)}`;
  gotoNode(anchorNode);
  renderCriticalReview(critical);
  renderReviewList();
  updateNav();
}

function syncReviewCursor() {
  if (!engineReview || exploring) return;
  const critical = criticalPositions.find((item) => Number(item.ply) - 1 === cur);
  activeCriticalId = critical ? critical.critical_id : null;
  if (critical) renderCriticalReview(critical);
  else renderCursorEngineReview();
  renderReviewList();
}

function renderCursorEngineReview() {
  if (!engineReview || !engineReview.moves || !timeline.length) return;
  const moveNode = reviewedMoveNode();
  const move = engineReview.moves.find((item) => Number(item.ply) === moveNode + 1);
  if (!move) return;
  $("review-empty").hidden = true;
  $("critical-review").hidden = false;
  $("critical-class").className = `review-class ${move.classification || ""}`;
  $("critical-class").textContent = move.classification || "move";
  $("critical-swing").textContent = `−${Number(move.win_percent_loss || 0).toFixed(1)}% win chance`;
  $("critical-title").textContent = reviewMoveLabel(move);
  $("critical-count").textContent = "All moves";
  $("critical-prev").disabled = true;
  $("critical-next").disabled = true;
  const best = move.best_move || {};
  $("explanation-content").innerHTML =
    `<section class="explanation-section"><h3>You played</h3><p><strong>${escapeHtml(reviewMoveLabel(move))}</strong></p></section>` +
    `<section class="explanation-section"><h3>Engine result</h3><div class="engine-fallback">` +
    `White eval ${escapeHtml(scoreLabel(move.eval_before))} → ${escapeHtml(scoreLabel(move.eval_after))}; ` +
    `the move lost ${Number(move.win_percent_loss || 0).toFixed(1)} percentage points for the mover.</div></section>` +
    `<section class="explanation-section"><h3>Better move</h3><p><strong>${escapeHtml(best.san || "—")}</strong></p></section>` +
    variationBlockHtml(move.best_pv || { uci: [], san: [] }, "best", "Best line", "");
  $("explanation-action").hidden = true;
  $("variation-controls").hidden = true;
}

function sanLineHtml(line, kind) {
  const sans = (line && line.san) || [];
  if (!sans.length) return `<span class="muted">No legal variation stored.</span>`;
  return `<span class="san-line">${sans
    .map(
      (san, index) =>
        `<button type="button" class="san-move" data-variation="${kind}" data-step="${index + 1}">${escapeHtml(san)}</button>`
    )
    .join("")}</span>`;
}

function variationBlockHtml(line, kind, name, summary) {
  return (
    `<section class="explanation-section"><h3>${escapeHtml(name)}</h3>` +
    `<div class="variation-row"><button type="button" class="san-move" data-play-line="${kind}" title="Play ${escapeHtml(name)}" aria-label="Play ${escapeHtml(name)}">▶</button>` +
    `${sanLineHtml(line, kind)}</div>` +
    (summary ? `<div class="variation-summary">${escapeHtml(summary)}</div>` : "") +
    `</section>`
  );
}

function factsProblemHtml(critical) {
  const facts = critical.facts || {};
  const motifs = facts.motifs || [];
  if (motifs.length) {
    return motifs
      .flatMap((motif) => motif.evidence || [])
      .slice(0, 3)
      .map((text) => escapeHtml(text))
      .join(" ");
  }
  return `The move was classified ${escapeHtml(critical.classification || "critical")} and lost ${Number(critical.win_loss || 0).toFixed(1)} percentage points of win chance.`;
}

function factReasons(critical) {
  const facts = critical.facts || {};
  const effects = (((facts.move_effects || {}).best || {}).effects || []).map((item) =>
    categoryLabel(item)
  );
  const delta = (((facts.deltas || {}).material_delta || {}).best_minus_played);
  const reasons = effects.slice(0, 4);
  if (delta != null && Number(delta) !== 0) reasons.push(`Material outcome improves by ${Number(delta)} point(s) in the stored lines.`);
  return reasons.length ? reasons : ["It preserves the best Engine evaluation in the supplied legal line."];
}

function renderCriticalReview(critical) {
  if (!critical) return;
  const index = criticalPositions.indexOf(critical);
  const explanation = explanationFor(critical.critical_id);
  const bestMove = ((critical.best_line || {}).san || ["—"])[0];
  const categories = [
    critical.facts && critical.facts.primary_category,
    ...((critical.facts && critical.facts.secondary_categories) || []),
  ].filter(Boolean);
  $("review-empty").hidden = true;
  $("critical-review").hidden = false;
  $("critical-class").className = `review-class ${critical.classification || ""}`;
  $("critical-class").textContent = critical.classification || "critical";
  $("critical-swing").textContent = criticalSwingLabel(critical);
  $("critical-title").textContent = reviewMoveLabel(critical);
  $("critical-count").textContent = `${index + 1} / ${criticalPositions.length}`;
  $("critical-prev").disabled = index <= 0;
  $("critical-next").disabled = index < 0 || index >= criticalPositions.length - 1;

  let html = "";
  if (explanation) {
    html += `<section class="explanation-section"><h3>You played</h3><p><strong>${escapeHtml(explanation.played_move)}</strong></p></section>`;
    html += `<section class="explanation-section"><h3>Why it looked reasonable</h3><p>${escapeHtml(explanation.why_it_looked_reasonable)}</p></section>`;
    html += `<section class="explanation-section"><h3>Core problem</h3><p>${escapeHtml(explanation.core_problem)}</p></section>`;
    html += `<section class="explanation-section"><h3>Engine recommendation</h3><p><strong>${escapeHtml(explanation.recommended_move)}</strong></p></section>`;
    html += `<section class="explanation-section"><h3>Why it works</h3><ul class="explanation-list">${explanation.why_recommended.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
    html += variationBlockHtml(critical.played_line, "played", "Played line", explanation.played_line_summary);
    html += variationBlockHtml(critical.best_line, "best", "Best line", explanation.best_line_summary);
    html += `<section class="explanation-section"><h3>Error category</h3><div class="category-row">${[explanation.primary_category, ...explanation.secondary_categories].filter(Boolean).map((item) => `<span class="category-chip">${escapeHtml(categoryLabel(item))}</span>`).join("") || '<span class="muted">Uncategorized</span>'}</div></section>`;
    html += `<section class="explanation-section"><h3>Transferable principle</h3><p>${escapeHtml(explanation.transferable_principle)}</p></section>`;
    html += `<section class="explanation-section"><h3>Next-time checklist</h3><ul class="explanation-list">${explanation.next_time_checklist.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
  } else {
    html += `<div class="engine-fallback"><strong>Engine review is ready.</strong> AI explanation is optional; every statement below comes from the stored analysis and deterministic facts.</div>`;
    html += `<section class="explanation-section"><h3>You played</h3><p><strong>${escapeHtml(reviewMoveLabel(critical))}</strong></p></section>`;
    html += `<section class="explanation-section"><h3>Core problem</h3><p>${factsProblemHtml(critical)}</p></section>`;
    html += `<section class="explanation-section"><h3>Engine recommendation</h3><p><strong>${escapeHtml(bestMove)}</strong> · ${escapeHtml(critical.criticality || "critical")}</p></section>`;
    html += `<section class="explanation-section"><h3>Why it works</h3><ul class="explanation-list">${factReasons(critical).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
    html += variationBlockHtml(critical.played_line, "played", "Played line", "");
    html += variationBlockHtml(critical.best_line, "best", "Best line", "");
    html += `<section class="explanation-section"><h3>Error category</h3><div class="category-row">${categories.map((item) => `<span class="category-chip">${escapeHtml(categoryLabel(item))}</span>`).join("") || '<span class="muted">No deterministic motif was assigned.</span>'}</div></section>`;
  }
  $("explanation-content").innerHTML = html;
  $("explanation-action").hidden = false;
  $("generate-explanation").textContent = explanation ? "Regenerate this explanation" : "Generate AI explanations";
  $("explanation-status").textContent = explanation
    ? `${explanationArtifact.provider} · ${explanationArtifact.model} · ${explanationArtifact.language}`
    : "Engine review remains available if generation fails.";
  wireVariationLinks();
}

// --- Phase 7 Retry -------------------------------------------------------
function retryStateLabel(state) {
  return String(state || "idle").replaceAll("_", " ");
}

function renderRetryState() {
  if (!retrySession) return;
  $("retry-panel").hidden = false;
  $("retry-state").textContent = retryStateLabel(retrySession.state);
  const hintLabels = ["Hint: Think", "Hint: Area", "Hint: First move", "Show line"];
  const hintButton = $("retry-hint");
  hintButton.textContent = hintLabels[retrySession.hintsUsed] || "All hints shown";
  hintButton.disabled = retrySession.hintsUsed >= 4 || retrySession.state === "evaluating";
  $("retry-again").hidden = !retrySession.locked || retrySession.state === "evaluating";
  updateStatus();
}

function resetRetryPosition({ keepHints = true } = {}) {
  if (!retrySession) return;
  retrySession.solutionGen += 1;
  retrySession.state = "awaiting_move";
  retrySession.locked = false;
  retrySession.shapes = keepHints ? (retrySession.hintShapes || []).slice() : [];
  boardLastMove = null;
  chess.load(retrySession.fen);
  $("retry-feedback").hidden = true;
  $("retry-feedback").innerHTML = "";
  $("retry-prompt").textContent = "Choose a legal move directly on the board.";
  renderBoard();
  renderRetryState();
}

function startRetry() {
  const critical = activeCritical();
  if (!critical || !currentGameId) return;
  stopReviewVariation();
  const saved = {
    cur,
    orient,
    criticalId: critical.critical_id,
    bestArrowOn,
    threatArrowOn,
  };
  retrySession = {
    criticalId: critical.critical_id,
    fen: critical.fen_before,
    state: "awaiting_move",
    hintsUsed: 0,
    locked: false,
    shapes: [],
    hintShapes: [],
    solutionGen: 0,
    saved,
  };
  document.body.classList.add("retry-mode");
  exploring = false;
  orient = critical.side || player;
  bestArrows = [];
  threatArrows = [];
  evalShapes = [];
  $("retry-hints").innerHTML = "";
  $("retry-title").textContent = `${critical.side === "black" ? "Black" : "White"} to move`;
  resetRetryPosition({ keepHints: false });
}

function exitRetry() {
  if (!retrySession) return;
  const saved = retrySession.saved;
  retrySession.solutionGen += 1;
  retrySession = null;
  document.body.classList.remove("retry-mode");
  $("retry-panel").hidden = true;
  bestArrowOn = saved.bestArrowOn;
  threatArrowOn = saved.threatArrowOn;
  $("best-toggle").checked = bestArrowOn;
  $("threat-toggle").checked = threatArrowOn;
  orient = saved.orient;
  selectCritical(saved.criticalId);
  if (saved.cur !== anchorNode) gotoNode(saved.cur);
}

function retryFeedbackHtml(result) {
  const selected = (result.selected_move || {}).san || (result.selected_move || {}).uci || "—";
  const best = (result.best_move || {}).san || (result.best_move || {}).uci || "—";
  const reply = (result.opponent_best_reply || {}).san || (result.opponent_best_reply || {}).uci || "None";
  const gap = result.win_gap_from_best == null ? "Unknown" : `${Number(result.win_gap_from_best).toFixed(1)}%`;
  const variation = ((result.variation || {}).san || []).join(" ") || "No continuation available.";
  return (
    `<div class="retry-feedback-head"><span class="retry-verdict ${escapeHtml(result.verdict)}">${escapeHtml(result.verdict)}</span>` +
    `<strong>${escapeHtml(selected)}</strong></div>` +
    `<dl class="retry-feedback-grid">` +
    `<dt>Engine evaluation</dt><dd>${escapeHtml((result.engine_evaluation || {}).label || "unknown")}</dd>` +
    `<dt>Gap from best</dt><dd>${escapeHtml(gap)}</dd>` +
    `<dt>Engine choice</dt><dd>${escapeHtml(best)}</dd>` +
    `<dt>Best reply</dt><dd>${escapeHtml(reply)}</dd>` +
    `<dt>Original problem</dt><dd>${result.problem_resolved ? "Resolved" : "Not resolved"}</dd>` +
    `<dt>Variation</dt><dd>${escapeHtml(variation)}</dd>` +
    `</dl><p class="retry-feedback-message">${escapeHtml(result.message || "")}</p>`
  );
}

async function onRetryMove(orig, dest) {
  if (!retrySession || retrySession.locked) {
    renderBoard();
    return;
  }
  const promo = isPromotion(orig, dest) ? "q" : undefined;
  const uci = orig + dest + (promo || "");
  const move = tryMove({ from: orig, to: dest, promotion: promo });
  if (!move) {
    renderBoard();
    return;
  }
  const session = retrySession;
  const requestGen = session.solutionGen;
  session.state = "evaluating";
  session.locked = true;
  session.shapes = [];
  boardLastMove = [orig, dest];
  $("retry-prompt").textContent = "Stockfish is checking your choice…";
  renderBoard();
  renderRetryState();

  let result;
  try {
    result = await reviewApi.trainingAttempt({
        game_id: currentGameId,
        critical_id: session.criticalId,
        selected_move: uci,
        review_side: player,
        hints_used: session.hintsUsed,
        source: "retry",
      });
    if (result.error) throw new Error(result.error);
  } catch (error) {
    if (retrySession !== session || requestGen !== session.solutionGen) return;
    chess.undo();
    boardLastMove = null;
    session.state = "awaiting_move";
    session.locked = false;
    session.shapes = (session.hintShapes || []).slice();
    $("retry-prompt").textContent = error.message || "Move evaluation failed. Try again.";
    renderBoard();
    renderRetryState();
    return;
  }
  if (retrySession !== session || requestGen !== session.solutionGen) return;
  session.state = result.solved ? "completed" : "feedback";
  session.locked = true;
  session.shapes = result.shapes || [];
  $("retry-prompt").textContent = result.solved
    ? "This choice solves the position's main problem."
    : "Review the feedback, then calculate again.";
  $("retry-feedback").innerHTML = retryFeedbackHtml(result);
  $("retry-feedback").hidden = false;
  renderBoard();
  renderRetryState();
}

async function playRetrySolution(line) {
  if (!retrySession || !line || !(line.uci || []).length) return;
  const session = retrySession;
  const gen = ++session.solutionGen;
  session.state = "showing_solution";
  session.locked = true;
  chess.load(session.fen);
  boardLastMove = null;
  renderBoard();
  renderRetryState();
  for (const uci of line.uci) {
    await sleep(520);
    if (retrySession !== session || gen !== session.solutionGen) return;
    const move = chess.move({
      from: String(uci).slice(0, 2),
      to: String(uci).slice(2, 4),
      promotion: String(uci).slice(4, 5) || undefined,
    });
    if (!move) break;
    boardLastMove = [String(uci).slice(0, 2), String(uci).slice(2, 4)];
    renderBoard();
  }
  renderRetryState();
}

async function showRetryHint() {
  if (!retrySession || retrySession.hintsUsed >= 4 || retrySession.state === "evaluating") return;
  const session = retrySession;
  const level = session.hintsUsed + 1;
  let result;
  try {
    const q = new URLSearchParams({
      game_id: currentGameId,
      critical_id: session.criticalId,
      review_side: player,
      level: String(level),
    });
    result = await reviewApi.trainingHint(q);
    if (result.error) throw new Error(result.error);
  } catch (error) {
    $("retry-prompt").textContent = error.message || "Hint unavailable.";
    return;
  }
  if (retrySession !== session) return;
  session.hintsUsed = level;
  session.state = level === 4 ? "showing_solution" : "showing_hint";
  const row = document.createElement("div");
  row.className = "retry-hint-row";
  row.textContent = result.text || "";
  $("retry-hints").appendChild(row);
  session.hintShapes = result.shapes || session.hintShapes || [];
  session.shapes = session.hintShapes.slice();
  session.locked = level === 4;
  if (level === 4 && result.line) await playRetrySolution(result.line);
  else {
    chess.load(session.fen);
    boardLastMove = null;
    renderBoard();
    renderRetryState();
  }
}

async function trainActiveCritical() {
  const critical = activeCritical();
  if (!critical || !currentGameId) return;
  await bridge.trainPuzzle({
    category: (critical.facts && critical.facts.primary_category) || "",
    gameId: currentGameId,
    criticalId: critical.critical_id,
  });
}

function wireVariationLinks() {
  document.querySelectorAll("[data-variation]").forEach((button) =>
    button.addEventListener("click", () =>
      startReviewVariation(button.dataset.variation, Number(button.dataset.step), false)
    )
  );
  document.querySelectorAll("[data-play-line]").forEach((button) =>
    button.addEventListener("click", () => startReviewVariation(button.dataset.playLine, 0, true))
  );
}

function stopReviewVariation(hide = true) {
  if (reviewVariation && reviewVariation.timer) clearInterval(reviewVariation.timer);
  reviewVariation = null;
  if (hide && $("variation-controls")) $("variation-controls").hidden = true;
  document.querySelectorAll(".san-move.active").forEach((item) => item.classList.remove("active"));
}

function renderVariationStep() {
  if (!reviewVariation) return;
  const step = clamp(reviewVariation.idx, 0, reviewVariation.fens.length - 1);
  reviewVariation.idx = step;
  chess.load(reviewVariation.fens[step]);
  const uci = step > 0 ? reviewVariation.ucis[step - 1] : null;
  boardLastMove = uci ? [uci.slice(0, 2), uci.slice(2, 4)] : null;
  renderBoard();
  $("variation-play").textContent = reviewVariation.playing ? "Ⅱ" : "▶";
  $("variation-label").textContent = `${reviewVariation.kind === "best" ? "Best line" : "Played line"} · ${step} / ${reviewVariation.ucis.length}`;
  document.querySelectorAll(".san-move[data-variation]").forEach((button) => {
    button.classList.toggle(
      "active",
      button.dataset.variation === reviewVariation.kind && Number(button.dataset.step) === step
    );
  });
  updateStatus();
}

function startReviewVariation(kind, step = 0, autoplay = false) {
  const critical = activeCritical();
  const line = critical && (kind === "played" ? critical.played_line : critical.best_line);
  if (!critical || !line || !(line.uci || []).length) return;
  stopReviewVariation(false);
  const probe = board.createGame(critical.fen_before);
  const fens = [probe.fen()];
  const ucis = [];
  const sans = [];
  for (const uci of line.uci) {
    const move = probe.move({
      from: String(uci).slice(0, 2),
      to: String(uci).slice(2, 4),
      promotion: String(uci).slice(4, 5) || undefined,
    });
    if (!move) break;
    ucis.push(uci);
    sans.push(move.san);
    fens.push(probe.fen());
  }
  if (!ucis.length) return;
  exploring = true;
  exploreBaseNode = Number(critical.ply) - 1;
  exploreVerdict = null;
  reviewVariation = {
    kind,
    ucis,
    sans,
    fens,
    idx: clamp(step, 0, ucis.length),
    timer: null,
    playing: autoplay,
  };
  $("variation-controls").hidden = false;
  renderVariationStep();
  if (autoplay) playReviewVariation();
}

function playReviewVariation() {
  if (!reviewVariation) return;
  if (reviewVariation.idx >= reviewVariation.ucis.length) reviewVariation.idx = 0;
  reviewVariation.playing = true;
  if (reviewVariation.timer) clearInterval(reviewVariation.timer);
  reviewVariation.timer = setInterval(() => {
    if (!reviewVariation) return;
    if (reviewVariation.idx >= reviewVariation.ucis.length) {
      clearInterval(reviewVariation.timer);
      reviewVariation.timer = null;
      reviewVariation.playing = false;
      renderVariationStep();
      return;
    }
    reviewVariation.idx += 1;
    renderVariationStep();
  }, 750);
  renderVariationStep();
}

function toggleReviewVariation() {
  if (!reviewVariation) return;
  if (reviewVariation.playing) {
    if (reviewVariation.timer) clearInterval(reviewVariation.timer);
    reviewVariation.timer = null;
    reviewVariation.playing = false;
    renderVariationStep();
  } else {
    playReviewVariation();
  }
}

async function generateReviewExplanations() {
  const critical = activeCritical();
  if (!currentGameId || !critical || explanationBusy) return;
  const currentExists = !!explanationFor(critical.critical_id);
  const targets = currentExists
    ? [critical]
    : criticalPositions.filter((item) => !explanationFor(item.critical_id));
  if (!targets.length) return;
  const token = ++explanationGeneration;
  explanationBusy = true;
  const button = $("generate-explanation");
  button.disabled = true;
  let failures = 0;
  for (let index = 0; index < targets.length; index++) {
    if (token !== explanationGeneration) return;
    setWorkflowState(
      "generating_explanations",
      `Preparing explanations: ${index} / ${targets.length}`,
      "The Engine review and board remain available.",
      `${index} / ${targets.length}`
    );
    $("explanation-status").textContent = `Generating ${targets[index].critical_id}…`;
    try {
      const data = await reviewApi.generateExplanations(currentGameId, {
          review_side: player,
          critical_id: targets[index].critical_id,
          force: currentExists,
        });
      if (token !== explanationGeneration) return;
      if (data.error) throw new Error(apiErrorMessage(data.error, "Explanation failed."));
      explanationArtifact = data.artifact || explanationArtifact;
    } catch (error) {
      failures += 1;
      $("explanation-status").textContent = error.message || "Explanation failed.";
    }
  }
  if (token !== explanationGeneration) return;
  explanationBusy = false;
  button.disabled = false;
  const ready = ((explanationArtifact && explanationArtifact.positions) || []).length;
  const total = criticalPositions.length;
  setWorkflowState(
    failures || ready < total ? "partial_ready" : "review_ready",
    failures ? "Engine review ready · some explanations failed" : "Review ready",
    failures ? "Retry from any key position; Engine facts are unaffected." : `${ready} grounded explanations available.`,
    `${ready} / ${total}`
  );
  renderCriticalReview(activeCritical());
}

async function loadReviewArtifacts(gameId, side) {
  currentGameId = gameId || currentGameId;
  engineReview = null;
  criticalPositions = [];
  explanationArtifact = null;
  activeCriticalId = null;
  if (!currentGameId) {
    setWorkflowState("partial_ready", "Engine review ready", "This legacy game has no stored Phase 6 artifact.");
    renderReviewList();
    return;
  }
  try {
    engineReview = await reviewApi.analysis(currentGameId, side);
  } catch (_) {
    setWorkflowState("partial_ready", "Engine review ready", "Structured artifact is unavailable; timeline navigation still works.");
    renderReviewList();
    return;
  }
  criticalPositions = (engineReview.critical_positions || []).slice();
  try {
    explanationArtifact = await reviewApi.explanations(currentGameId, { review_side: side });
  } catch (_) {}
  const ready = ((explanationArtifact && explanationArtifact.positions) || []).length;
  const total = criticalPositions.length;
  setWorkflowState(
    ready === total && total ? "review_ready" : "partial_ready",
    ready === total && total ? "Review ready" : "Engine review ready",
    total ? `${total} key positions · AI explanations are optional.` : "No critical positions were selected.",
    total ? `${ready} / ${total}` : ""
  );
  $("review-empty").hidden = !!criticalPositions.length;
  $("critical-review").hidden = !criticalPositions.length;
  setReviewView(reviewView);
  renderGraph();
}

// --- chat ("why?") -------------------------------------------------------
const escapeHtml = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

// Themes that describe a puzzle's length/format/phase/outcome, not a tactical motif — mirrors
// `_STORM_NON_MOTIF_THEMES` in claude_bridge.py so a review row labels the real motif (fork/pin/…)
// rather than "middlegame". Falls back to the raw themes when nothing tactical is tagged.
const NON_MOTIF_THEMES = new Set([
  "oneMove", "short", "long", "veryLong", "master", "masterVsMaster", "superGM",
  "opening", "middlegame", "endgame",
  "rookEndgame", "bishopEndgame", "knightEndgame", "pawnEndgame", "queenEndgame", "queenRookEndgame",
  "crushing", "advantage", "equality", "mate",
]);
function motifThemes(themes) {
  const t = (themes || []).filter((x) => x && !NON_MOTIF_THEMES.has(x) && !/^mateIn\d/.test(x));
  return t.length ? t : (themes || []).filter((x) => x && !/^mateIn\d/.test(x));
}

// Minimal, safe markdown → HTML: escape first, then bold / italic / code / lists / paragraphs.
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "";
  let inList = false;
  const inline = (s) =>
    s
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  for (const raw of lines) {
    const line = raw.trim();
    const li = line.match(/^[-*]\s+(.*)/);
    if (li) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inline(li[1])}</li>`;
    } else {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      if (line) html += `<p>${inline(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html || "<p></p>";
}

function addChatMsg(cls, text) {
  const d = document.createElement("div");
  d.className = `chat-msg ${cls}`;
  if (cls === "bot") d.innerHTML = renderMarkdown(text); // only the final answer is markdown
  else d.textContent = text;
  const box = $("chat-messages");
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}

// Repopulate the chat panel from the server's in-memory transcript for the current game, so
// switching to another game and back shows the conversation you'd had. Best-effort: a failure
// just leaves the (already-cleared) panel empty.
async function restoreChat() {
  const myGen = chatGen;
  let hist;
  try {
    hist = await reviewApi.chatHistory();
  } catch (_) {
    return;
  }
  if (myGen !== chatGen) return; // a newer game opened while we were fetching — don't clobber it
  const msgs = (hist && hist.messages) || [];
  $("chat-messages").innerHTML = "";
  for (const m of msgs) addChatMsg(m.role === "bot" ? "bot" : "user", m.text);
  chatSession = (hist && hist.session_id) || null;
}

async function sendChat(ev) {
  ev.preventDefault();
  const input = $("chat-input");
  // Empty box → context-aware default (the placeholder becomes a one-click question).
  const typed = input.value.trim();
  const q =
    typed ||
    (chatMove
      ? `Why is ${chatMove} bad here?`
      : "What's the best move in this position, and why?");
  input.value = "";
  addChatMsg("user", q);
  $("chat-send").disabled = true;
  const pending = addChatMsg("bot pending", "Snowie is thinking… (a few seconds)");
  try {
    const res = await reviewApi.chat({
        question: q,
        fen: chess.fen(), // the exact board on screen → "what should I do here?"
        last_move: chatMove, // the move in question → "why is this bad?"
        move_fen: chatFen, // the position that move was played from
        session_id: chatSession,
        use_profile: personalizeHistory, // personalize with cross-game history (Settings toggle)
      });
    pending.remove();
    if (res.error) {
      addChatMsg("bot err", res.error);
    } else {
      addChatMsg("bot", res.answer || "(no answer)");
      if (res.session_id) chatSession = res.session_id;
    }
  } catch (e) {
    pending.remove();
    addChatMsg("bot err", "Request failed: " + e);
  } finally {
    $("chat-send").disabled = false;
    input.focus();
  }
}

// --- init ----------------------------------------------------------------
function applySession(session) {
  currentGameId = session.game_id || currentGameId;
  const sens = session.review_elo
    ? ` · sensitivity ~${Math.round(session.review_elo)} Elo`
    : "";
  // Names first (with an "open on Lichess/Chess.com" ↗ right after them), then the review details.
  // The names + link live in the always-visible board header so they're reachable even when the
  // Games panel is collapsed on narrow screens.
  currentGameUrl = session.game_url || null;
  setGameMeta(
    session.white,
    session.black,
    currentGameUrl,
    ` — ${session.result} · reviewing ${session.player} ` +
      `(acc W ${session.accuracy_white} / B ${session.accuracy_black}) · ${session.num_mistakes} mistakes${sens}`
  );
  mistakes = session.mistakes;
  // Remember the game (PGN + names) so "Review other side" can re-open it for the opponent.
  if (session.pgn) currentPgn = session.pgn;
  gameWhite = session.white || gameWhite;
  gameBlack = session.black || gameBlack;
  updateFlipReviewButton();
  renderMistakeList();
  renderScoreboard(session);
  renderCoach(session);
  // NB: prepareCoachAI() is intentionally NOT called here — it's run by the caller AFTER
  // applyTimeline(), so it sees the new game's timeline (applySession runs before applyTimeline).
}

// Free, engine-grounded templated blurb (rides on /api/session) — always shown when present.
// The templated quick summary + whether the user has manually expanded it while an AI summary is up.
let quickSummaryHasText = false;
let quickSummaryUserExpanded = false;
let coachAiReady = false;

function renderCoach(session) {
  const el = $("coach");
  if (!el) return;
  const text = (session && session.coach_summary) || "";
  el.textContent = text;
  quickSummaryHasText = !!text;
  quickSummaryUserExpanded = false;
  syncQuickSummary();
}

// Collapse the templated quick summary behind a toggle once the fuller AI (Snowie) summary is on
// screen — the AI one is the richer version, so the quick summary is redundant but kept one click
// away. With no AI summary the quick summary shows outright (no toggle).
function syncQuickSummary() {
  const el = $("coach");
  const toggle = $("coach-toggle");
  if (!el || !toggle) return;
  if (!quickSummaryHasText) {
    el.hidden = true;
    toggle.hidden = true;
    return;
  }
  if (!coachAiReady) {
    el.hidden = false;
    toggle.hidden = true;
    return;
  }
  // AI summary present: default to collapsed, expandable on demand.
  toggle.hidden = false;
  el.hidden = !quickSummaryUserExpanded;
  toggle.textContent = quickSummaryUserExpanded ? "▾ Hide quick summary" : "▸ Show quick summary";
}

// Claude-written summary (board column, bottom). The card shows its state; the button offers
// on-demand generation. Server caches per game, so re-requests don't spend Claude again.
function showCoachButton(show) {
  const btn = $("coach-ai-btn");
  if (btn) btn.hidden = !show;
}

function setCoachAI(state, text) {
  const el = $("coach-ai");
  if (!el) return;
  if (state === "hidden") {
    el.hidden = true;
    el.className = "coach-ai";
    el.textContent = "";
  } else if (state === "pending") {
    el.hidden = false;
    el.className = "coach-ai pending";
    el.textContent = "Snowie is writing up a full game summary…";
  } else if (state === "error") {
    el.hidden = false;
    el.className = "coach-ai err";
    el.textContent = text || "Couldn't generate the summary.";
  } else {
    el.hidden = false;
    el.className = "coach-ai";
    // Render bold / italics / lists / paragraphs (same safe markdown as the chat answers). The ⟳
    // button re-runs the summary on demand (spends Claude) — handy if it's a stale saved one.
    el.innerHTML =
      `<span class="coach-ai-tag">AI coach (Snowie)` +
      `<button id="coach-ai-refresh" class="coach-ai-refresh" type="button" ` +
      `title="Regenerate this summary (uses your Claude subscription)" aria-label="Regenerate summary">⟳</button>` +
      `</span>${renderMarkdown(text)}`;
    const rb = $("coach-ai-refresh");
    if (rb) rb.addEventListener("click", () => fetchCoachAI(true));
  }
  // Only a finished AI summary supersedes the templated quick one; while it's pending/errored the
  // quick summary stays visible so there's never a gap with no overview.
  coachAiReady = state === "ready";
  syncQuickSummary();
}

// Set up the AI-summary UI for the freshly-loaded game: show an already-saved summary outright,
// else auto-generate (if enabled), else just offer the button.
function prepareCoachAI(session) {
  coachAiToken++; // any earlier in-flight request is now stale
  setCoachAI("hidden");
  if (!timeline.length) { showCoachButton(false); return; }
  // Already generated for this game (this session, or restored from the cache on reopen) → show it
  // immediately, no button press and no second Claude call.
  if (session && session.coach_ai_text) {
    setCoachAI("ready", session.coach_ai_text);
    showCoachButton(false);
    return;
  }
  if (coachAiAuto) fetchCoachAI();
  else showCoachButton(true);
}

// Actually request the summary (button press, or the auto path). Always allowed — it only ever
// runs from an explicit user choice, so it spends Claude only when asked.
async function fetchCoachAI(force = false) {
  const tok = ++coachAiToken;
  showCoachButton(false);
  setCoachAI("pending");
  let res;
  try {
    res = await reviewApi.coach({ force: !!force });
  } catch (_) {
    if (tok === coachAiToken) { setCoachAI("error", "Couldn't reach the summary service."); showCoachButton(true); }
    return;
  }
  if (tok !== coachAiToken) return; // a newer game superseded this request
  if (res && res.summary) {
    setCoachAI("ready", res.summary);
  } else {
    setCoachAI(res && res.error ? "error" : "hidden", res && res.error);
    showCoachButton(true); // let the user retry
  }
}

function applyTimeline(tl) {
  timeline = tl.nodes || [];
  player = tl.player || "white";
  orient = boardOrientationPreference === "review" ? player : boardOrientationPreference;
  applyEvalBarTheme();
  renderMoveList();
}

// --- progressive open: navigate the PGN immediately, swap in engine analysis when ready ----
// Build a provisional timeline from a PGN entirely client-side (chess.js), so the board is
// steppable the instant a game is opened — no engine, no win%/classifications yet (those arrive
// in phase 2). Shape matches the server timeline's navigation fields. Throws on an unparseable PGN.
function buildProvisionalTimeline(pgn) {
  const c = board.createGame();
  c.loadPgn(pgn);
  const moves = c.history({ verbose: true });
  if (!moves.length) throw new Error("no moves");
  const nodes = moves.map((mv, i) => ({
    node: i,
    fen: mv.before,
    win_white: null,
    color: mv.color === "w" ? "white" : "black",
    move_number: Math.floor(i / 2) + 1,
    ply: i + 1,
    move_san: mv.san,
    move_uci: mv.from + mv.to + (mv.promotion || ""),
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
    color: c.turn() === "w" ? "white" : "black",
    move_number: Math.floor(moves.length / 2) + 1,
  });
  return nodes;
}

function setAnalyzingUI(on) {
  $("best-toggle").disabled = on; // engine pool is busy with the sweep
  $("threat-toggle").disabled = on;
  if (on) {
    bestArrowOn = false;
    $("best-toggle").checked = false;
    threatArrowOn = false;
    $("threat-toggle").checked = false;
  }
  const box = $("analysis-progress");
  if (box) box.hidden = !on;
  if (on) renderProgress(null); // start indeterminate until the first status arrives
}

// Render the sweep progress bar over the win graph. `st` is the /analysis-status payload (or null
// for the initial indeterminate state). We show a measured fill + ETA once the job reports a stable
// per-ply rate (eta_seconds), and an indeterminate shimmer before that (engine pool warming up).
function renderProgress(st) {
  const fill = $("analysis-progress-fill");
  const label = $("analysis-progress-label");
  if (!fill || !label) return;
  // Multi-game batch: prefix the per-game bar with "Game k of N".
  const multi = st && (st.total_games || 1) > 1;
  const prefix = multi ? `Game ${st.current_game} of ${st.total_games} · ` : "";
  const done = st && st.total ? st.done : 0;
  const total = st && st.total ? st.total : 0;
  const eta = st ? st.eta_seconds : null;
  const phase = st && st.phase ? st.phase : "scanning";
  const phaseLabels = {
    queued: "Queued",
    scanning: "Scanning every position",
    selecting_critical: "Selecting key positions",
    deep_analysis: "Deep-analyzing key positions",
    extracting_facts: "Preparing engine facts",
  };
  const phaseLabel = phaseLabels[phase] || "Analyzing";
  const critical = phase === "deep_analysis" && st && st.critical_total
    ? ` ${st.critical_done || 0}/${st.critical_total}`
    : "";
  const workflowState = {
    queued: "ready_to_analyze",
    scanning: "analyzing_scan",
    selecting_critical: "analyzing_scan",
    deep_analysis: "analyzing_deep",
    extracting_facts: "extracting_facts",
  }[phase] || "analyzing_scan";
  const workflowLabel = phase === "scanning"
    ? `Scanning game: ${done} / ${total || "—"} positions`
    : phase === "deep_analysis"
    ? `Deep analysis: ${(st && st.critical_done) || 0} / ${(st && st.critical_total) || "—"} critical positions`
    : phase === "extracting_facts"
    ? `Preparing facts: ${(st && st.critical_done) || 0} / ${(st && st.critical_total) || "—"}`
    : `${phaseLabel}…`;
  setWorkflowState(
    workflowState,
    workflowLabel,
    "The main line remains navigable during analysis.",
    total ? `${done} / ${total}` : ""
  );
  if (!total || eta == null) {
    fill.classList.add("indeterminate");
    label.textContent = `${prefix}${phaseLabel}${critical}…`;
    return;
  }
  fill.classList.remove("indeterminate");
  const pct = Math.max(0, Math.min(100, Math.round((done / total) * 100)));
  fill.style.width = pct + "%";
  const secs = Math.max(1, Math.round(eta));
  label.textContent = `${prefix}${phaseLabel}${critical} · ${pct}% · ~${secs}s left`;
}

// Set up the board to navigate a PGN immediately (provisional timeline, no engine yet) and reset
// per-game UI state. Shared by single-game opens and the first game of a batch upload.
function beginProvisional(pgn, side, metaText, gameId = null) {
  analyzing = true;
  explanationGeneration += 1;
  explanationBusy = false;
  $("generate-explanation").disabled = false;
  currentGameId = gameId;
  engineReview = null;
  criticalPositions = [];
  explanationArtifact = null;
  activeCriticalId = null;
  stopReviewVariation();
  $("critical-review").hidden = true;
  $("review-empty").hidden = false;
  $("review-empty").innerHTML = "<strong>Analysis in progress</strong><span>You can navigate the main line while Stockfish works.</span>";
  setWorkflowState("analyzing_scan", "Scanning game", "Waiting for the first measured position count.");
  renderReviewList();
  currentMistake = -1;
  anchorNode = 0;
  mistakes = [];
  currentPgn = pgn; // enable "Review other side" immediately; names fill in at phase-2
  gameWhite = "";
  gameBlack = "";
  currentPrompt = "";
  $("comment").textContent = "";
  $("verdict").innerHTML = "";
  setAnalyzingUI(true);
  renderMistakeList();
  $("scoreboard").hidden = true; // stale until the new game's stats land in phase-2
  quickSummaryHasText = false;
  quickSummaryUserExpanded = false;
  coachAiReady = false;
  $("coach").hidden = true;
  $("coach-toggle").hidden = true;
  coachAiToken++; // invalidate any in-flight AI summary from the previous game
  setCoachAI("hidden");
  showCoachButton(false);
  // Clear the chat panel for the new game; its prior in-memory transcript (if any) is restored
  // in onAnalysisReady once the session is loaded. chatSession is reset so we don't thread the
  // previous game's conversation into this one. Bumping chatGen invalidates any in-flight
  // restoreChat from the game we're leaving, so a late response can't repopulate this panel.
  chatGen++;
  $("chat-messages").innerHTML = "";
  chatSession = null;

  let prov = null;
  try {
    prov = buildProvisionalTimeline(pgn);
  } catch (_) {
    prov = null; // unparseable PGN -> fall back to a blocking spinner (phase-2 still works)
  }
  // Pull names + the source-site URL straight from the PGN headers so the header line (names + ↗
  // link) is populated during analysis too, not only once phase-2 lands.
  const hdr = pgnHeaders(pgn);
  currentGameUrl = hdr.url || null;
  if (prov && prov.length >= 2) {
    timeline = prov;
    player = side === "white" || side === "black" ? side : "white";
    orient = player;
    applyEvalBarTheme();
    renderMoveList(); // provisional notation: navigable immediately, glyphs fill in later
    gotoNode(0);
    const tail = metaText || "analyzing… you can step through the moves now (← / →)";
    setGameMeta(hdr.white, hdr.black, currentGameUrl, ` — ${tail}`);
  } else {
    timeline = [];
    renderMoveList();
    $("game-meta").textContent = "Analyzing…";
  }
  updateFlipReviewButton(); // side is known now; names fill in at phase-2
}

// Show/label the "Review other side" button for the loaded game (hidden until a game is open).
// It re-analyses the SAME game from the opponent's perspective — a separate history record
// (keyed by reviewed_side, so it never collides with the side already analysed).
function updateFlipReviewButton() {
  const btn = $("flip-review");
  if (!btn) return;
  if (!currentPgn || !timeline.length) {
    btn.hidden = true;
    return;
  }
  const otherColor = player === "white" ? "black" : "white";
  const name = otherColor === "white" ? gameWhite : gameBlack;
  const label = name && name !== "?" ? name : otherColor === "white" ? "White" : "Black";
  btn.textContent = `↺ Review ${label}'s side`;
  btn.hidden = false;
}

// Re-open the current game reviewing the opponent (the wrong side may have been auto-picked).
function reviewOtherSide() {
  if (!currentPgn) return;
  openGame(currentPgn, player === "white" ? "black" : "white", currentGameId);
}

async function openGame(pgn, side, gameId = null) {
  if (analysisAbortController) analysisAbortController.abort();
  analysisAbortController = new AbortController();
  const signal = analysisAbortController.signal;
  if ((!side || side === "auto") && defaultReviewSide !== "auto") side = defaultReviewSide;
  batchInfo = null; // a single open is not a batch
  bridge.closeHistory(); // on small screens the drawer covers the board
  beginProvisional(pgn, side, null, gameId);
  // AWAIT the POST: jobs.start switches the server's session/job synchronously, so by the time it
  // returns the status reflects THIS game. Polling before that could observe the *previous* game's
  // lingering "ready" and load the wrong session (showing its chat/board). On a cache hit the server
  // is already "ready", so we skip polling and render immediately.
  let st = null;
  try {
    const requestBody = gameId
      ? { review_side: side || "auto" }
      : { pgn, player: side || "auto" };
    st = gameId
      ? await reviewApi.analyzeGame(gameId, requestBody, { signal })
      : await reviewApi.analyze(requestBody, { signal });
  } catch (error) {
    if (error && error.name === "AbortError") return;
  }
  if (st && st.status === "ready") { onAnalysisReady(); return; }
  if (st && st.error) {
    onAnalysisError(apiErrorMessage(st.error, "Could not start analysis."));
    return;
  }
  startPolling();
}

// Analyze a multi-game PGN (e.g. a Chess.com export): the backend splits + analyzes each game in
// the background and records them to "My games"; we show the first game while the rest run.
async function openBatch(pgnText, side, username) {
  if (analysisAbortController) analysisAbortController.abort();
  analysisAbortController = new AbortController();
  const signal = analysisAbortController.signal;
  let res;
  try {
    res = await reviewApi.analyzeBatch(
      { pgn: pgnText, player: side || "auto", username: username || "" },
      { signal }
    );
  } catch (error) {
    if (error && error.name === "AbortError") return;
    $("history-status").textContent = "Could not start analysis.";
    return;
  }
  if (res.error || !res.total_games) {
    $("history-status").textContent = res.error || "No valid games found in that PGN.";
    return;
  }
  batchInfo = { total: res.total_games, self_handle: res.self_handle, lastDone: -1 };
  const who = res.self_handle ? ` as ${res.self_handle}` : "";
  $("history-status").textContent = `Analyzing ${res.total_games} games${who} → they'll appear in My games.`;
  bridge.closeHistory(); // on small screens the drawer covers the board
  beginProvisional(res.first_pgn, res.first_side, `Analyzing game 1 of ${res.total_games}…`);
  startPolling();
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    let st;
    try {
      st = await reviewApi.analysisStatus();
    } catch (_) {
      return;
    }
    // Batch: surface each finished game in "My games" as it lands.
    if (batchInfo && st.done_games != null && st.done_games !== batchInfo.lastDone) {
      batchInfo.lastDone = st.done_games;
      if (bridge.isLocalHistory()) bridge.loadHistory();
    }
    if (st.status === "ready") {
      stopPolling();
      onAnalysisReady();
    } else if (st.status === "error" && (!batchInfo || (st.total_games || 1) === 1)) {
      // A single-game failure is terminal; in a batch we keep going (error is just the last note).
      stopPolling();
      onAnalysisError(st.error);
    } else {
      renderProgress(st); // pending: advance the bar / ETA
    }
  }, 800);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

async function onAnalysisReady() {
  // Where the user navigated during phase 1 — only meaningful if a provisional timeline existed
  // for THIS game (when the PGN couldn't be replayed client-side, `cur` is a stale index from the
  // previous game and honouring it would land on an arbitrary move with no mistake selected).
  const prevCur = timeline.length ? cur : 0;
  const session = await reviewApi.session();
  const tl = await reviewApi.timeline();
  if (session.empty) return; // superseded/cleared
  analyzing = false;
  setAnalyzingUI(false); // hides the progress bar
  applySession(session);
  applyTimeline(tl);
  await loadReviewArtifacts(session.game_id, session.player);
  // "Replay in full game" from a mistake puzzle wins: land on the exact position of the mistake.
  if (pendingCriticalId) {
    const target = pendingCriticalId;
    pendingCriticalId = null;
    if (criticalPositions.some((item) => item.critical_id === target)) selectCritical(target);
    else if (prevCur === 0 && criticalPositions.length) selectCritical(criticalPositions[0].critical_id);
  }
  else if (pendingGotoPly != null) {
    gotoNode(clamp(pendingGotoPly, 0, timeline.length - 1));
    pendingGotoPly = null;
  }
  // Keep the user where they were navigating; if they hadn't moved, jump to the first mistake.
  else if (prevCur === 0 && criticalPositions.length) selectCritical(criticalPositions[0].critical_id);
  else if (prevCur === 0 && mistakes.length) selectMistake(session.current_index ?? 0);
  else gotoNode(clamp(prevCur, 0, timeline.length - 1));
  restoreChat(); // repopulate this game's in-memory Q&A (if we've chatted about it this session)
  prepareCoachAI(session); // timeline is set now → show saved summary, auto-generate, or offer button
  if (batchInfo) {
    // Whole upload done: surface every game in "My games".
    const n = batchInfo.total;
    const who = batchInfo.self_handle ? ` as ${batchInfo.self_handle}` : "";
    batchInfo = null;
    bridge.activateLocalHistory();
    bridge.loadHistory(`Analyzed ${n} game${n === 1 ? "" : "s"}${who}. Showing the first below.`);
  } else if (bridge.isLocalHistory()) {
    bridge.loadHistory(); // the just-analyzed game now appears in the list
  }
}

function onAnalysisError(msg) {
  analyzing = false;
  setAnalyzingUI(false);
  renderGraph();
  $("history-status").textContent = `Analysis failed: ${msg || "unknown error"}`;
  $("game-meta").textContent = "Analysis failed — you can still step through the moves.";
  setWorkflowState("failed", "Analysis failed", msg || "The PGN main line is still available.");
}


  function mount() {
    $("back").addEventListener("click", stepBack);
    $("fwd").addEventListener("click", stepForward);
    $("start").addEventListener("click", () => gotoNode(0));
    $("end").addEventListener("click", () => gotoNode(timeline.length - 1));
    $("flip-board").addEventListener("click", flipBoard);
    $("prev-mistake").addEventListener("click", () => selectAdjacentCritical(-1));
    $("next-mistake").addEventListener("click", () => selectAdjacentCritical(1));
    $("reset").addEventListener("click", returnToReview);
    $("flip-review").addEventListener("click", reviewOtherSide);
    $("best-toggle").addEventListener("change", (event) => {
      bestArrowOn = event.target.checked;
      refreshBestMoves();
    });
    $("threat-toggle").addEventListener("change", (event) => {
      threatArrowOn = event.target.checked;
      refreshBestMoves();
    });
    $("graph").addEventListener("click", onGraphClick);
    $("movelist-expand").addEventListener("click", toggleMoveList);
    document.querySelectorAll(".review-tabs button").forEach((button) =>
      button.addEventListener("click", () => setReviewView(button.dataset.view))
    );
    $("critical-prev").addEventListener("click", () => selectAdjacentCritical(-1, true));
    $("critical-next").addEventListener("click", () => selectAdjacentCritical(1, true));
    $("retry-critical").addEventListener("click", startRetry);
    $("train-critical").addEventListener("click", trainActiveCritical);
    $("retry-hint").addEventListener("click", showRetryHint);
    $("retry-again").addEventListener("click", () => resetRetryPosition({ keepHints: true }));
    $("retry-exit").addEventListener("click", exitRetry);
    $("generate-explanation").addEventListener("click", generateReviewExplanations);
    $("variation-play").addEventListener("click", toggleReviewVariation);
    $("variation-mainline").addEventListener("click", returnToReview);
    $("coach-ai-btn").addEventListener("click", fetchCoachAI);
    $("coach-toggle").addEventListener("click", () => {
      quickSummaryUserExpanded = !quickSummaryUserExpanded;
      syncQuickSummary();
    });
    $("chat-form").addEventListener("submit", sendChat);
  }

  function selectAdjacentCritical(delta, criticalOnly = false) {
    const index = criticalPositions.findIndex((item) => item.critical_id === activeCriticalId);
    const next = index + delta;
    if (criticalPositions.length && next >= 0 && next < criticalPositions.length) {
      selectCritical(criticalPositions[next].critical_id);
    } else if (!criticalOnly) {
      const mistake = currentMistake + delta;
      if (!criticalPositions.length && mistake >= 0 && mistake < mistakes.length) selectMistake(mistake);
    }
  }

  function handleKeydown(event) {
    if (retrySession) {
      if (event.key === "Escape") {
        event.preventDefault();
        exitRetry();
      }
      return true;
    }
    if (event.key === "ArrowLeft") { event.preventDefault(); stepBack(); return true; }
    if (event.key === "ArrowRight") { event.preventDefault(); stepForward(); return true; }
    if (event.key === "ArrowUp") { event.preventDefault(); gotoNode(0); return true; }
    if (event.key === "ArrowDown") { event.preventDefault(); gotoNode(timeline.length - 1); return true; }
    if (event.key === " ") { event.preventDefault(); stepForward(); return true; }
    if (event.key === "f" || event.key === "F") { event.preventDefault(); flipBoard(); return true; }
    if (event.key === "l" || event.key === "L") { event.preventDefault(); toggleBestArrows(); return true; }
    if (event.key === "t" || event.key === "T") { event.preventDefault(); toggleThreatArrows(); return true; }
    if (event.key === "n" || event.key === "N") { event.preventDefault(); selectAdjacentCritical(1); return true; }
    if (event.key === "p" || event.key === "P") { event.preventDefault(); selectAdjacentCritical(-1); return true; }
    return false;
  }

  function setPreferences(preferences = {}) {
    coachAiAuto = !!preferences.coachAiAuto;
    personalizeHistory = preferences.personalizeHistory !== false;
    defaultReviewSide = preferences.defaultReviewSide || "auto";
    boardOrientationPreference = preferences.boardOrientation || "review";
    analysisPreset = preferences.analysisPreset || "balanced";
    explanationProvider = preferences.explanationProvider || "auto";
    explanationLanguage = preferences.explanationLanguage || "zh-CN";
    showThreatsByDefault = !!preferences.showThreats;
    threatArrowOn = showThreatsByDefault;
    $("threat-toggle").checked = threatArrowOn;
  }

  return {
    mount,
    handleMove: onUserMove,
    handleKeydown,
    openGame,
    openBatch,
    setWorkflowState,
    applySession,
    applyTimeline,
    loadReviewArtifacts,
    restoreChat,
    prepareCoachAI,
    setPreferences,
    setPendingCritical(id) { pendingCriticalId = id; },
    setPendingPly(ply) { pendingGotoPly = ply; },
    prepareForPuzzle() {
      if (reviewVariation) stopReviewVariation();
      if (retrySession) exitRetry();
      evalShapes = [];
      bestArrows = [];
    },
    restoreBoard() {
      if (timeline.length) gotoNode(cur);
      else {
        chess.reset();
        renderBoard();
      }
    },
    startSyncedBatch(info) {
      batchInfo = { total: info.new_games, self_handle: info.self_handle, lastDone: -1 };
      beginProvisional(
        info.first_pgn,
        info.first_side,
        `Syncing ${info.new_games} new chess.com game${info.new_games === 1 ? "" : "s"}… you can step through this one now.`
      );
      startPolling();
    },
    loadInitial(session, timelineData) {
      applySession(session);
      applyTimeline(timelineData);
    },
    selectInitial(session) {
      if (criticalPositions.length) selectCritical(criticalPositions[0].critical_id);
      else if (mistakes.length) selectMistake(session.current_index ?? 0);
      else gotoNode(0);
    },
    applySavedPreferences: setPreferences,
    refreshAfterSettings() {
      if (timeline.length) {
        orient = boardOrientationPreference === "review" ? player : boardOrientationPreference;
        applyEvalBarTheme();
        renderBoard();
        renderGraph();
        refreshBestMoves();
      }
      const card = $("coach-ai");
      const busy = card && !card.hidden && !card.classList.contains("err");
      if (timeline.length && !busy) {
        if (coachAiAuto) fetchCoachAI();
        else { setCoachAI("hidden"); showCoachButton(true); }
      } else if (!timeline.length) {
        setCoachAI("hidden");
        showCoachButton(false);
      }
    },
    get hasTimeline() { return timeline.length > 0; },
    get gameId() { return currentGameId; },
    get generation() { return chatGen; },
  };
}
