import { createAnalysisTabs } from "./analysis-tabs.js";
import { agentApi } from "../api/agent.js";
import { reviewApi } from "../api/review.js";
import { byId, clamp } from "../core/dom.js";
import { createAnalysisRunner } from "./analysis-runner.js";
import {
  buildReviewAgentContext,
  buildTrainingAgentContext,
  createReviewChat,
} from "./chat.js";
import { createReviewCoach } from "./coach.js";
import { createReviewArtifacts } from "./artifacts.js";
import { createReviewGraph } from "./graph.js";
import { buildProvisionalTimeline, pgnHeaders, reviewMoveLabel } from "./helpers.js";
import { createReviewNavigation } from "./navigation.js";
import { createReviewNotation } from "./notation.js";
import { createAnalysisProgress } from "./progress.js";
import { createRetryController } from "./retry.js";
import { createReviewSummaryView } from "./summary-view.js";
import { createReviewVariation } from "./variation.js";
import { createWorkspaceView } from "./workspace-view.js";

export function createReviewController({ board, bridge }) {
  const $ = byId;
  const chess = board.chess;
  let analyzing = false;
  let pendingCriticalId = null;
  let pendingGotoPly = null;

let timeline = []; // nodes 0..N for the whole game
let mistakes = [];
let player = "white"; // the reviewed side (drives the header label)
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
let defaultReviewSide = "auto";
let boardOrientationPreference = "review";
let analysisPreset = "balanced";
let explanationProvider = "auto";
let explanationLanguage = "zh-CN";
let showThreatsByDefault = false;

  let navigation = null;
  let retry = null;
  let variation = null;

  const chat = createReviewChat({
    $,
    agentApi,
    getAgentContext: () => buildAgentContext(),
    onReference: openAgentReference,
    onAction: runAgentAction,
  });
  const coach = createReviewCoach({ $ });
  const graph = createReviewGraph({
    $,
    getSnapshot: () => ({
      timeline,
      cur: navigation.cur,
      orient: navigation.orient,
      engineReview,
      criticalPositions,
    }),
    onGotoNode: (...args) => navigation.gotoNode(...args),
    onSelectCritical: selectCritical,
    onSelectMistake: selectMistake,
  });
  const notation = createReviewNotation({
    $,
    getSnapshot: () => ({
      timeline,
      criticalPositions,
      engineReview,
      reviewedMoveNode: navigation.reviewedMoveNode(),
    }),
    onGotoNode: (...args) => navigation.gotoNode(...args),
    onSelectCritical: selectCritical,
    onSelectEngineMove: selectEngineMove,
    onSelectMistake: selectMistake,
  });
  navigation = createReviewNavigation({
    $,
    board,
    api: reviewApi,
    getTimeline: () => timeline,
    getAnalyzing: () => analyzing,
    getActiveCritical: activeCritical,
    getRetrySession: () => retry && retry.session,
    onRetryMove: (...args) => retry.handleMove(...args),
    isVariationActive: () => !!(variation && variation.active),
    stopVariation: () => variation && variation.stop(),
    setChatContext: syncChatContext,
    onSelectCritical: selectCritical,
    onGraphRender: () => graph.render(),
    onNotationHighlight: () => notation.highlightCurrent(),
    onReviewCursorSync: syncReviewCursor,
    onNavUpdate: updateNav,
    onLiveAnalysisChange: () => chat.deferContext(buildAgentContext()),
    onFreeAnalysisLine: renderFreeAnalysisLine,
  });
  const renderBoard = () => navigation.renderBoard();
  const applyEvalBarTheme = () => navigation.applyEvalBarTheme();
  const gotoNode = (index) => navigation.gotoNode(index);
  const returnToReview = () => navigation.returnToReview();
  const stepBack = () => navigation.stepBack();
  const stepForward = () => navigation.stepForward();
  const flipBoard = () => navigation.flipBoard();
  const toggleBestArrows = () => navigation.toggleBestArrows();
  const toggleThreatArrows = () => navigation.toggleThreatArrows();
  const refreshBestMoves = () => navigation.refreshEngineArrows();

  retry = createRetryController({
    $,
    board,
    api: reviewApi,
    getContext: () => ({
      cur: navigation.cur,
      orient: navigation.orient,
      player,
      anchorNode: navigation.anchorNode,
      bestArrowOn: navigation.bestArrowOn,
      threatArrowOn: navigation.threatArrowOn,
      currentGameId,
    }),
    setContext: navigation.patch,
    getActiveCritical: activeCritical,
    stopVariation: () => variation.stop(),
    selectCritical,
    gotoNode,
    renderBoard: navigation.renderBoard,
    updateStatus: navigation.updateStatus,
    onPositionChange: (fen, details) => syncChatContext(fen, null, null, details),
  });
  variation = createReviewVariation({
    $,
    board,
    getActiveCritical: activeCritical,
    setContext: navigation.patch,
    renderBoard: navigation.renderBoard,
    updateStatus: navigation.updateStatus,
    onPositionChange: (fen, details) => syncChatContext(fen, null, null, details),
  });
  const workspaceView = createWorkspaceView({
    $,
    setWorkflowState,
    getSnapshot: () => ({
      analyzing,
      criticalPositions,
      engineReview,
      player,
      mistakes,
      activeCriticalId,
      reviewedMoveNode: navigation.reviewedMoveNode(),
      timeline,
      explanationArtifact,
    }),
    onSelectCritical: selectCritical,
    onSelectEngineMove: selectEngineMove,
    wireVariationLinks: variation.wireLinks,
  });
  const summaryView = createReviewSummaryView({
    $,
    getSnapshot: () => ({ analyzing, mistakes }),
    onSelectMistake: selectMistake,
  });
  const setReviewView = (view) => workspaceView.setView(view);
  const renderReviewList = () => workspaceView.renderList();
  const renderCursorEngineReview = () => workspaceView.renderCursor();
  const renderCriticalReview = (critical) => workspaceView.renderCritical(critical);
  const renderMistakeList = () => summaryView.renderMistakes();
  const renderScoreboard = (session) => summaryView.renderScoreboard(session);
  const renderGraph = () => graph.render();
  const onGraphClick = (event) => graph.handleClick(event);
  const renderMoveList = () => notation.render();
  const highlightCurrentMove = () => notation.highlightCurrent();
  const toggleMoveList = () => notation.toggleExpanded();

  const progress = createAnalysisProgress({ $, setWorkflowState });
  const artifacts = createReviewArtifacts({
    $,
    api: reviewApi,
    getSnapshot: () => ({
      currentGameId,
      player,
      criticalPositions,
      explanationArtifact,
      activeCritical: activeCritical(),
    }),
    setState: (patch) => {
      if ("currentGameId" in patch) currentGameId = patch.currentGameId;
      if ("engineReview" in patch) engineReview = patch.engineReview;
      if ("criticalPositions" in patch) criticalPositions = patch.criticalPositions;
      if ("explanationArtifact" in patch) explanationArtifact = patch.explanationArtifact;
      if ("activeCriticalId" in patch) activeCriticalId = patch.activeCriticalId;
    },
    setWorkflowState,
    renderList: renderReviewList,
    refreshView: workspaceView.refreshView,
    renderGraph,
    renderCritical: renderCriticalReview,
  });

  const generateReviewExplanations = () => artifacts.generateExplanations();
  const loadReviewArtifacts = (gameId, side, options) => artifacts.load(gameId, side, options);
  const analysis = createAnalysisRunner({
    $,
    api: reviewApi,
    bridge,
    getDefaultReviewSide: () => defaultReviewSide,
    beginProvisional,
    applyReady: applyAnalysisReady,
    reportError: onAnalysisError,
    renderProgress: progress.render,
  });
  const openGame = (...args) => analysis.openGame(...args);
  const openBatch = (...args) => analysis.openBatch(...args);

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

function activeCritical() {
  return criticalPositions.find((item) => item.critical_id === activeCriticalId) || null;
}

function buildAgentContext(details = {}) {
  return buildReviewAgentContext({
    currentGameId,
    player,
    timeline,
    criticalPositions,
    activeCriticalId,
    retryActive: !!(retry && retry.session),
    navigation: navigation && {
      cur: navigation.cur,
      exploring: navigation.exploring,
      exploreBaseNode: navigation.exploreBaseNode,
    },
    fen: chess.fen(),
    liveAnalysisRef: navigation && navigation.freeAnalysis ? navigation.liveAnalysisRef : null,
  }, details);
}

function syncChatContext(fen, san = null, uci = null, details = {}) {
  return chat.setContext(buildAgentContext({
    ...details,
    fen: fen || details.fen,
    selectedFen: details.selectedFen,
    selectedMoveSan: san || details.selectedMoveSan,
    selectedMoveUci: uci || details.selectedMoveUci,
  }));
}

function syncTrainingContext(fen = chess.fen()) {
  return chat.setContext(buildTrainingAgentContext(fen));
}

async function openAgentTarget(target = {}) {
  const targetSide = target.review_side || player;
  const localGame = !target.game_id || (
    target.game_id === currentGameId && targetSide === player
  );
  if (!localGame) return bridge.openAgentPosition(target);
  if (target.critical_id) {
    const critical = criticalPositions.find((item) => item.critical_id === target.critical_id);
    if (!critical) throw new Error("That key position is no longer available in this review.");
    selectCritical(critical.critical_id);
    return true;
  }
  if (target.ply != null) {
    gotoNode(clamp(Number(target.ply), 0, timeline.length - 1));
    return true;
  }
  if (!target.fen || target.fen === chess.fen()) return true;
  throw new Error("That position is not available in the current review.");
}

function openAgentReference(reference) {
  return openAgentTarget(reference);
}
async function runAgentAction(action = {}, capture = {}) {
  const target = action.target || {};
  if (action.kind === "open_position" || action.kind === "compare_move") {
    return openAgentTarget(target);
  }
  if (action.kind === "start_retry") {
    const local = await openAgentTarget(target);
    if (!local || !activeCritical()) {
      throw new Error("Open the referenced key position before starting Retry.");
    }
    retry.start();
    return true;
  }
  if (action.kind === "start_training") {
    if (!capture.sessionId || !Number.isInteger(capture.expectedGeneration)) {
      throw new Error("That training draft is missing its Agent context.");
    }
    const verified = await agentApi.startTraining(capture.sessionId, {
      expected_generation: capture.expectedGeneration,
      action,
    }, { signal: capture.signal });
    if (capture.isCurrent && !capture.isCurrent())
      throw new DOMException("Superseded", "AbortError");
    return bridge.trainPuzzle({
      positionReferences: verified.position_references || [],
      objectiveSkillIds: verified.objective_skill_ids || [],
      source: verified.source,
    });
  }
  if (action.kind === "review_weakness") return openAgentTarget(target);
  throw new Error("That coach action is not supported in Review yet.");
}
async function selectMistake(i) {
  const stored = mistakes[i];
  const critical = stored && criticalPositions.find((item) => Number(item.ply) === Number(stored.ply));
  if (critical) return selectCritical(critical.critical_id);
  const myGen = chat.generation;
  const pos = await reviewApi.position(i);
  if (myGen !== chat.generation) return; // a different game opened while we were fetching
  const anchorNode = mistakes[i].node_index;
  navigation.patch({
    currentMistake: i,
    currentPrompt: pos.error ? "" : pos.prompt,
    anchorNode,
  });
  // Land on the position BEFORE the mistake (you're on the move) so the engine's best-move arrow
  // and any move you try are for YOUR side; the grey arrow still shows the move you actually played.
  [...$("mistakes").children].forEach((li) =>
    li.classList.toggle("active", Number(li.dataset.index) === i)
  );
  gotoNode(anchorNode);
  $("comment").textContent = mistakes[i].comment || "";
}

function updateNav() {
  const freePly = navigation.freePly;
  $("start").disabled = navigation.freeAnalysis ? freePly === 0 : navigation.cur <= 0;
  $("back").disabled = navigation.freeAnalysis
    ? freePly === 0
    : !navigation.exploring && navigation.cur <= 0;
  $("fwd").disabled = navigation.freeAnalysis || navigation.exploring || navigation.cur >= timeline.length - 1;
  $("end").disabled = navigation.freeAnalysis || navigation.cur >= timeline.length - 1;
  $("reset").disabled = navigation.freeAnalysis && freePly === 0;
  const criticalIndex = criticalPositions.findIndex((item) => item.critical_id === activeCriticalId);
  $("prev-mistake").disabled = navigation.freeAnalysis || (criticalPositions.length
    ? criticalIndex <= 0
    : navigation.currentMistake <= 0);
  $("next-mistake").disabled = navigation.freeAnalysis || (criticalPositions.length
    ? criticalIndex < 0 || criticalIndex >= criticalPositions.length - 1
    : navigation.currentMistake < 0 || navigation.currentMistake >= mistakes.length - 1);
}

// --- artifact-backed review workspace ----------------------------------
function setWorkflowState(state, label, detail = "", count = "") {
  workspaceView.renderWorkflow(state, label, detail, count);
}

function selectEngineMove(ply) {
  const critical = criticalPositions.find((item) => Number(item.ply) === Number(ply));
  if (critical) return selectCritical(critical.critical_id);
  activeCriticalId = null;
  const anchorNode = Math.min(timeline.length - 1, Number(ply));
  navigation.patch({
    currentMistake: -1,
    anchorNode,
    currentPrompt: `Reviewing ${reviewMoveLabel((engineReview.moves || []).find((item) => Number(item.ply) === Number(ply)) || {})}`,
  });
  gotoNode(anchorNode);
  renderCursorEngineReview();
}

function selectCritical(criticalId) {
  const critical = criticalPositions.find((item) => item.critical_id === criticalId);
  if (!critical) return;
  activeCriticalId = criticalId;
  const anchorNode = Math.max(0, Number(critical.ply) - 1);
  navigation.patch({
    anchorNode,
    currentMistake: mistakes.findIndex((item) => Number(item.ply) === Number(critical.ply)),
    currentPrompt: `Key position · ${reviewMoveLabel(critical)}`,
  });
  gotoNode(anchorNode);
  renderCriticalReview(critical);
  renderReviewList();
  updateNav();
}

function syncReviewCursor() {
  if (!engineReview || navigation.exploring) return;
  const critical = criticalPositions.find((item) => Number(item.ply) - 1 === navigation.cur);
  activeCriticalId = critical ? critical.critical_id : null;
  if (critical) renderCriticalReview(critical);
  else renderCursorEngineReview();
  renderReviewList();
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
  coach.renderQuickSummary(session);
  // The AI summary is prepared by the caller AFTER
  // applyTimeline(), so it sees the new game's timeline (applySession runs before applyTimeline).
}

function applyTimeline(tl) {
  timeline = tl.nodes || [];
  player = tl.player || "white";
  navigation.patch({
    orient: boardOrientationPreference === "review" ? player : boardOrientationPreference,
  });
  applyEvalBarTheme();
  renderMoveList();
}

// --- progressive open: navigate the PGN immediately, swap in engine analysis when ready ----
function setAnalyzingUI(on) {
  $("best-toggle").disabled = on; // engine pool is busy with the sweep
  $("threat-toggle").disabled = on;
  if (on) {
    navigation.patch({ bestArrowOn: false, threatArrowOn: false });
    $("best-toggle").checked = false;
    $("threat-toggle").checked = false;
  }
  progress.setVisible(on);
}

function setFreeAnalysisUI(on) {
  $("review-tabs").hidden = on;
  $("free-analysis-line").hidden = !on;
  $("reset").textContent = on ? "Reset" : "↩ Main line";
  $("reset").title = on ? "Reset to the start position" : "Return to the main line";
  if (on) {
    $("review-position-list").hidden = true;
    $("movelist-panel").hidden = true;
    $("critical-review").hidden = true;
  } else {
    workspaceView.refreshView();
  }
}

function renderFreeAnalysisLine(line, ply, details = {}) {
  workspaceView.renderFreeAnalysis(line, ply, details);
}

function exitFreeAnalysis() {
  if (!navigation.freeAnalysis) return;
  navigation.exitFreeAnalysis();
  setFreeAnalysisUI(false);
  $("review-empty-title").textContent = "No game loaded";
  $("review-empty-detail").textContent = "Open Games and import a PGN to start reviewing.";
  $("game-meta").textContent = "Waiting to open a game…";
}

function enterFreeAnalysis() {
  analysis.cancel();
  analyzing = false;
  setAnalyzingUI(false);
  if (retry.session) retry.exit();
  variation.stop();
  artifacts.reset();
  timeline = [];
  mistakes = [];
  currentPgn = null;
  currentGameId = null;
  currentGameUrl = null;
  gameWhite = "";
  gameBlack = "";
  pendingCriticalId = null;
  pendingGotoPly = null;
  $("scoreboard").hidden = true;
  $("comment").textContent = "";
  $("verdict").innerHTML = "";
  coach.reset();
  chat.contextChanged();
  renderMoveList();
  renderMistakeList();
  setWorkflowState("free_analysis", "Free analysis", "Temporary position workspace.");
  $("game-meta").textContent = "Free analysis · not saved";
  updateFlipReviewButton();
  setFreeAnalysisUI(true);
  navigation.enterFreeAnalysis();
}

function handleMove(orig, dest) {
  if (!timeline.length && !navigation.freeAnalysis) enterFreeAnalysis();
  return navigation.handleMove(orig, dest);
}

// Set up the board to navigate a PGN immediately (provisional timeline, no engine yet) and reset
// per-game UI state. Shared by single-game opens and the first game of a batch upload.
function beginProvisional(pgn, side, metaText, gameId = null) {
  exitFreeAnalysis();
  analyzing = true;
  artifacts.reset(gameId);
  variation.stop();
  $("critical-review").hidden = true;
  $("review-empty").hidden = false;
  $("review-empty-title").textContent = "Analysis in progress";
  $("review-empty-detail").textContent = "You can navigate the main line while Stockfish works.";
  setWorkflowState("analyzing_scan", "Scanning game", "Waiting for the first measured position count.");
  renderReviewList();
  navigation.resetVisualState();
  mistakes = [];
  currentPgn = pgn; // enable "Review other side" immediately; names fill in at phase-2
  gameWhite = "";
  gameBlack = "";
  $("comment").textContent = "";
  $("verdict").innerHTML = "";
  setAnalyzingUI(true);
  renderMistakeList();
  $("scoreboard").hidden = true; // stale until the new game's stats land in phase-2
  coach.reset();
  // A game switch invalidates an in-flight answer but preserves the stateful Agent session.
  chat.contextChanged();

  let prov = null;
  try {
    prov = buildProvisionalTimeline(board, pgn);
  } catch (_) {
    prov = null; // unparseable PGN -> fall back to a blocking spinner (phase-2 still works)
  }
  // Pull names + the source-site URL straight from the PGN headers so the header line (names + ↗
  // link) is populated during analysis too, not only once phase-2 lands.
  const hdr = pgnHeaders(board, pgn);
  currentGameUrl = hdr.url || null;
  if (prov && prov.length >= 2) {
    timeline = prov;
    player = side === "white" || side === "black" ? side : "white";
    navigation.patch({ orient: player });
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

async function applyAnalysisReady(session, tl, operation = {}) {
  const isCurrent = operation.isCurrent || (() => true);
  if (!isCurrent()) return;
  // Where the user navigated during phase 1 — only meaningful if a provisional timeline existed
  // for THIS game (when the PGN couldn't be replayed client-side, the cursor is a stale index from the
  // previous game and honouring it would land on an arbitrary move with no mistake selected).
  const prevCur = timeline.length ? navigation.cur : 0;
  analyzing = false;
  setAnalyzingUI(false); // hides the progress bar
  applySession(session);
  applyTimeline(tl);
  const artifactsLoaded = await loadReviewArtifacts(
    session.game_id,
    session.player,
    { signal: operation.signal }
  );
  if (!isCurrent() || artifactsLoaded === false) return;
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
  chat.restore(); // repopulate this game's in-memory Q&A (if we've chatted about it this session)
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
    createAnalysisTabs($).mount();
    $("review-import").addEventListener("click", () => bridge.openImport());
    $("back").addEventListener("click", stepBack);
    $("fwd").addEventListener("click", stepForward);
    $("start").addEventListener("click", () => navigation.freeAnalysis ? navigation.resetFreeAnalysis() : gotoNode(0));
    $("end").addEventListener("click", () => gotoNode(timeline.length - 1));
    $("flip-board").addEventListener("click", flipBoard);
    $("prev-mistake").addEventListener("click", () => selectAdjacentCritical(-1));
    $("next-mistake").addEventListener("click", () => selectAdjacentCritical(1));
    $("reset").addEventListener("click", () => navigation.freeAnalysis ? navigation.resetFreeAnalysis() : returnToReview());
    $("flip-review").addEventListener("click", reviewOtherSide);
    $("best-toggle").addEventListener("change", (event) => {
      navigation.patch({ bestArrowOn: event.target.checked });
      refreshBestMoves();
    });
    $("threat-toggle").addEventListener("change", (event) => {
      navigation.patch({ threatArrowOn: event.target.checked });
      refreshBestMoves();
    });
    $("graph").addEventListener("click", onGraphClick);
    $("movelist-expand").addEventListener("click", toggleMoveList);
    document.querySelectorAll(".review-tabs button").forEach((button) =>
      button.addEventListener("click", () => setReviewView(button.dataset.view))
    );
    $("critical-prev").addEventListener("click", () => selectAdjacentCritical(-1, true));
    $("critical-next").addEventListener("click", () => selectAdjacentCritical(1, true));
    $("train-critical").addEventListener("click", trainActiveCritical);
    $("generate-explanation").addEventListener("click", generateReviewExplanations);
    $("generate-explanations-all").addEventListener("click", () => artifacts.generateExplanations({ all: true }));
    $("variation-play").addEventListener("click", variation.toggle);
    $("variation-mainline").addEventListener("click", returnToReview);
    chat.mount();
    retry.mount();
  }

  function selectAdjacentCritical(delta, criticalOnly = false) {
    const index = criticalPositions.findIndex((item) => item.critical_id === activeCriticalId);
    const next = index + delta;
    if (criticalPositions.length && next >= 0 && next < criticalPositions.length) {
      selectCritical(criticalPositions[next].critical_id);
    } else if (!criticalOnly) {
      const mistake = navigation.currentMistake + delta;
      if (!criticalPositions.length && mistake >= 0 && mistake < mistakes.length) selectMistake(mistake);
    }
  }

  function handleKeydown(event) {
    if (retry.session) {
      if (event.key === "Escape") {
        event.preventDefault();
        retry.exit();
      }
      return true;
    }
    if (event.key === "ArrowLeft") { event.preventDefault(); stepBack(); return true; }
    if (event.key === "ArrowRight") { event.preventDefault(); stepForward(); return true; }
    if (event.key === "ArrowUp") { event.preventDefault(); navigation.freeAnalysis ? navigation.resetFreeAnalysis() : gotoNode(0); return true; }
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
    chat.setAgentCapability(preferences.agent);
    defaultReviewSide = preferences.defaultReviewSide || "auto";
    boardOrientationPreference = preferences.boardOrientation || "review";
    analysisPreset = preferences.analysisPreset || "balanced";
    explanationProvider = preferences.explanationProvider || "auto";
    explanationLanguage = preferences.explanationLanguage || "zh-CN";
    showThreatsByDefault = !!preferences.showThreats;
    navigation.patch({ threatArrowOn: showThreatsByDefault });
    $("threat-toggle").checked = navigation.threatArrowOn;
  }

  return {
    mount,
    handleMove,
    handleKeydown,
    openGame,
    openBatch,
    exitFreeAnalysis,
    setWorkflowState,
    applySession,
    applyTimeline,
    loadReviewArtifacts,
    restoreChat: chat.restore,
    setPreferences,
    setPendingCritical(id) { pendingCriticalId = id; },
    setPendingPly(ply) { pendingGotoPly = ply; },
    prepareForPuzzle() {
      if (variation.active) variation.stop();
      if (retry.session) retry.exit();
      navigation.patch({ evalShapes: [], bestArrows: [] });
      syncTrainingContext();
    },
    setAgentTrainingPosition: syncTrainingContext,
    restoreBoard() {
      if (navigation.freeAnalysis) navigation.resetFreeAnalysis();
      else if (timeline.length) {
        gotoNode(navigation.cur);
        chat.setContext(buildAgentContext());
      }
      else {
        chess.reset();
        renderBoard();
      }
    },
    startSyncedBatch(info) {
      analysis.startSyncedBatch(info);
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
    refreshAfterSettings() {
      if (timeline.length) {
        navigation.patch({
          orient: boardOrientationPreference === "review" ? player : boardOrientationPreference,
        });
        applyEvalBarTheme();
        renderBoard();
        renderGraph();
        refreshBestMoves();
      }
    },
  };
}
