import { clamp, escapeHtml } from "../core/dom.js";
import { createEngineArrowSearch } from "./engine-arrows.js";
import { arrowShape, classGlyph, nodeLabel, samePosition } from "./helpers.js";

export function createReviewNavigation({
  $,
  board,
  api,
  getTimeline,
  getAnalyzing,
  getActiveCritical,
  getRetrySession,
  onRetryMove,
  isVariationActive,
  stopVariation,
  setChatContext,
  onSelectCritical,
  onGraphRender,
  onNotationHighlight,
  onReviewCursorSync,
  onNavUpdate,
}) {
  const chess = board.chess;
  const ground = board.ground;
  const state = {
    orient: "white",
    cur: 0,
    anchorNode: 0,
    currentMistake: -1,
    currentPrompt: "",
    exploring: false,
    exploreBaseNode: 0,
    exploreBaseFen: null,
    exploreGeneration: 0,
    exploreVerdict: null,
    bestArrowOn: false,
    bestArrows: [],
    threatArrowOn: false,
    threatArrows: [],
    evalShapes: [],
    boardLastMove: null,
  };

  const engineSearch = createEngineArrowSearch({
    api,
    getFen: () => chess.fen(),
    isBestEnabled: () => state.bestArrowOn,
    isThreatEnabled: () => state.threatArrowOn,
    setBestArrows: (arrows) => { state.bestArrows = arrows; },
    setThreatArrows: (arrows) => { state.threatArrows = arrows; },
    onUpdate: drawArrows,
  });

  function patch(values) {
    Object.assign(state, values);
  }

  function atMistakeAnchor() {
    return state.currentMistake >= 0 && state.cur === state.anchorNode;
  }

  function reviewedMoveNode() {
    const critical = getActiveCritical();
    if (critical && state.cur === Number(critical.ply) - 1) return Number(critical.ply) - 1;
    return atMistakeAnchor() ? state.cur : state.cur - 1;
  }

  function reviewArrowShapes() {
    const critical = getActiveCritical();
    if (!critical || state.exploring || state.cur !== Number(critical.ply) - 1) return [];
    const shapes = [];
    const played = critical.played_move && critical.played_move.uci;
    const best = critical.best_line && critical.best_line.uci && critical.best_line.uci[0];
    const reply = critical.opponent_best_reply && critical.opponent_best_reply.uci;
    if (played) shapes.push(arrowShape(played, "grey"));
    if (best && best !== played) shapes.push(arrowShape(best, "green"));
    if (reply) shapes.push(arrowShape(reply, "red"));
    return shapes;
  }

  function drawArrows() {
    const retrySession = getRetrySession();
    if (retrySession) {
      ground.setAutoShapes(retrySession.shapes || []);
      return;
    }
    const timeline = getTimeline();
    const shapes = reviewArrowShapes();
    if (
      !getActiveCritical() &&
      !state.exploring &&
      !getAnalyzing() &&
      atMistakeAnchor() &&
      timeline[state.cur] &&
      timeline[state.cur].move_uci
    ) {
      shapes.push(arrowShape(timeline[state.cur].move_uci, "grey"));
    }
    if (state.bestArrowOn) shapes.push(...state.bestArrows);
    if (state.threatArrowOn) shapes.push(...state.threatArrows);
    shapes.push(...state.evalShapes);
    ground.setAutoShapes(shapes);
  }

  function renderBoard() {
    const color = board.turnColor();
    const retrySession = getRetrySession();
    const movable = !retrySession || !retrySession.locked;
    ground.set({
      fen: chess.fen(),
      orientation: state.orient,
      turnColor: color,
      check: chess.inCheck(),
      lastMove: state.boardLastMove,
      movable: {
        color: movable ? color : undefined,
        dests: movable ? board.computeDests() : new Map(),
        free: false,
        showDests: true,
      },
    });
    drawArrows();
  }

  function applyEvalBarTheme() {
    const light = "#f0f0f0";
    const dark = "#2b2a27";
    const fill = $("evalbar-fill");
    const bar = $("evalbar");
    if (state.orient === "white") {
      fill.style.background = light;
      bar.style.background = dark;
    } else {
      fill.style.background = dark;
      bar.style.background = light;
    }
  }

  function setEvalBar(winWhite) {
    const white = winWhite == null ? 50 : winWhite;
    const bottomShare = state.orient === "white" ? white : 100 - white;
    $("evalbar-fill").style.height = `${clamp(bottomShare, 0, 100)}%`;
  }

  function renderVerdict(payload) {
    if (!payload) {
      $("verdict").innerHTML = "";
      return;
    }
    if (payload.error) {
      $("verdict").innerHTML = `<span class="line">${payload.error}</span>`;
      return;
    }
    const move = payload.move;
    const refutation = move.refutation_line_san.slice(0, 6).join(" ");
    const better = move.is_engine_best
      ? "Engine's top choice."
      : `Best was <b>${move.better_move_san}</b>.`;
    const label = move.classification === "best" && !move.is_engine_best
      ? "good"
      : move.classification;
    $("verdict").innerHTML =
      `<span class="tag ${label}">${label}</span>` +
      `<b>${move.move_san}</b> — win ${move.win_before}% → ${move.win_after}% ` +
      `(swing ${move.win_swing}, eval ${move.eval_after}). ${better}` +
      (refutation ? `<div class="line">Reply: ${refutation}</div>` : "");
  }

  function exploreVerdictHtml() {
    if (state.exploreVerdict === "pending") return ` <span class="line">evaluating…</span>`;
    if (!state.exploreVerdict) return "";
    if (state.exploreVerdict.error) return ` <span class="line">couldn't evaluate that move</span>`;
    const move = state.exploreVerdict;
    const label = move.classification === "best" && !move.is_engine_best
      ? "good"
      : move.classification;
    return (
      ` <span class="tag ${label}">${label}</span>` +
      `<b>${escapeHtml(move.move_san)}</b> — win ${move.win_before}% → ${move.win_after}%` +
      (move.is_engine_best
        ? ""
        : ` · best was <b>${escapeHtml(move.better_move_san || "")}</b>`)
    );
  }

  function updateStatus() {
    const element = $("status");
    const retrySession = getRetrySession();
    const timeline = getTimeline();
    if (retrySession) {
      const labels = {
        awaiting_move: "Retry: choose a move on the board.",
        evaluating: "Retry: evaluating your move…",
        feedback: "Retry feedback is ready.",
        showing_hint: "Retry: use the hint, then choose a move.",
        showing_solution: "Retry: studying the Engine line.",
        completed: "Retry completed.",
      };
      element.className = "status";
      element.textContent = labels[retrySession.state] || "Retry this position.";
    } else if (state.exploring) {
      element.className = "status away";
      element.innerHTML =
        `🔍 Exploring a variation.${exploreVerdictHtml()} ` +
        `<button id="ret">Back to review move</button>`;
      $("ret").onclick = returnToReview;
    } else if (state.cur !== state.anchorNode) {
      element.className = "status away";
      element.innerHTML =
        `Viewing ${nodeLabel(timeline, state.cur)} — not the review move. ` +
        `<button id="ret">Back to review move</button>`;
      $("ret").onclick = returnToReview;
    } else {
      element.className = "status";
      const moveNode = reviewedMoveNode();
      const glyph = moveNode >= 0 && timeline[moveNode]
        ? classGlyph(timeline[moveNode].classification)
        : "";
      element.innerHTML =
        glyph +
        (glyph ? " " : "") +
        escapeHtml(state.currentPrompt || nodeLabel(timeline, state.cur));
    }
  }

  function refreshEngineArrows() {
    engineSearch.refresh();
  }

  function explorationDetails() {
    const history = chess.history({ verbose: true });
    const timeline = getTimeline();
    const baseNode = timeline[state.exploreBaseNode];
    return {
      mode: "exploration",
      basePly: state.exploreBaseNode,
      baseFen: state.exploreBaseFen || (baseNode && baseNode.fen) || chess.fen(),
      explorationMovesUci: history.map((move) =>
        `${move.from}${move.to}${move.promotion || ""}`
      ),
      explorationMovesSan: history.map((move) => move.san),
    };
  }

  function gotoNode(index) {
    const timeline = getTimeline();
    if (!timeline.length) return;
    stopVariation();
    state.exploreGeneration += 1;
    state.exploring = false;
    state.exploreBaseFen = null;
    state.cur = clamp(index, 0, timeline.length - 1);
    state.evalShapes = [];
    const moveNode = reviewedMoveNode();
    const selectedNode = moveNode >= 0 && timeline[moveNode] && timeline[moveNode].move_san
      ? timeline[moveNode]
      : null;
    // The reviewed move can be the outgoing move at a key position, while the board still shows
    // the position before it. Last-move highlighting must follow the FEN, not that review focus.
    const incomingNode = state.cur - 1;
    const lastUci = incomingNode >= 0 && timeline[incomingNode]
      ? timeline[incomingNode].move_uci
      : null;
    state.boardLastMove = lastUci ? [lastUci.slice(0, 2), lastUci.slice(2, 4)] : null;
    chess.load(timeline[state.cur].fen);
    renderBoard();
    setEvalBar(timeline[state.cur].win_white);
    renderVerdict(null);
    updateStatus();
    onNavUpdate();
    onGraphRender();
    onNotationHighlight();
    onReviewCursorSync();
    setChatContext(
      chess.fen(),
      selectedNode ? selectedNode.move_san : null,
      selectedNode ? selectedNode.move_uci : null,
      {
        mode: "mainline",
        ply: state.cur,
        selectedFen: selectedNode ? selectedNode.fen : null,
      }
    );
    refreshEngineArrows();
  }

  function returnToReview() {
    const critical = getActiveCritical();
    if (critical) onSelectCritical(critical.critical_id);
    else gotoNode(state.anchorNode);
  }

  function flipBoard() {
    state.orient = state.orient === "white" ? "black" : "white";
    applyEvalBarTheme();
    renderBoard();
    const timeline = getTimeline();
    setEvalBar(timeline[state.cur] ? timeline[state.cur].win_white : 50);
    onGraphRender();
  }

  function toggleBestArrows() {
    const checkbox = $("best-toggle");
    checkbox.checked = !checkbox.checked;
    state.bestArrowOn = checkbox.checked;
    refreshEngineArrows();
  }

  function toggleThreatArrows() {
    const checkbox = $("threat-toggle");
    checkbox.checked = !checkbox.checked;
    state.threatArrowOn = checkbox.checked;
    refreshEngineArrows();
  }

  function stepBack() {
    if (state.exploring) undoOne();
    else if (state.cur > 0) gotoNode(state.cur - 1);
  }

  function stepForward() {
    if (!state.exploring && state.cur < getTimeline().length - 1) gotoNode(state.cur + 1);
  }

  function undoOne() {
    state.exploreGeneration += 1;
    const undone = chess.undo();
    if (!undone) return;
    const timeline = getTimeline();
    const baseNode = timeline[state.exploreBaseNode];
    const baseFen = state.exploreBaseFen || (baseNode && baseNode.fen);
    if (baseFen && samePosition(chess.fen(), baseFen)) {
      if (baseNode) {
        gotoNode(state.exploreBaseNode);
        return;
      }
      state.exploring = false;
      state.exploreBaseFen = null;
      state.exploreVerdict = null;
      state.evalShapes = [];
      state.boardLastMove = null;
      setChatContext(chess.fen(), null, null, explorationDetails());
      renderBoard();
      renderVerdict(null);
      updateStatus();
      onNavUpdate();
      onGraphRender();
      onNotationHighlight();
      onReviewCursorSync();
      refreshEngineArrows();
      return;
    }
    const history = chess.history({ verbose: true });
    const previous = history.length ? history[history.length - 1] : null;
    state.boardLastMove = previous ? [previous.from, previous.to] : null;
    setChatContext(chess.fen(), null, null, explorationDetails());
    state.exploreVerdict = null;
    renderBoard();
    renderVerdict(null);
    updateStatus();
    onNavUpdate();
    onGraphRender();
    syncExplore();
    refreshEngineArrows();
  }

  async function syncExplore() {
    try {
      const info = await api.bestMove({ fen: chess.fen() });
      setEvalBar(info.side_to_move === "white" ? info.win_percent : 100 - info.win_percent);
    } catch (_) {}
  }

  async function handleMove(orig, dest) {
    const retrySession = getRetrySession();
    if (retrySession) return onRetryMove(orig, dest);
    if (isVariationActive()) stopVariation();
    const timeline = getTimeline();
    const moverColor = board.turnColor();
    const fenBefore = chess.fen();
    const promotion = board.isPromotion(orig, dest) ? "q" : undefined;
    const uci = orig + dest + (promotion ?? "");

    if (!state.exploring && timeline[state.cur] && timeline[state.cur].move_uci === uci) {
      const move = board.tryMove({ from: orig, to: dest, promotion });
      if (!move) return renderBoard();
      state.cur += 1;
      state.boardLastMove = [orig, dest];
      renderBoard();
      setEvalBar(timeline[state.cur].win_white);
      renderVerdict(null);
      updateStatus();
      onNavUpdate();
      onGraphRender();
      onNotationHighlight();
      onReviewCursorSync();
      setChatContext(chess.fen(), null, null, { mode: "mainline", ply: state.cur });
      refreshEngineArrows();
      return;
    }

    if (!state.exploring) {
      state.exploring = true;
      state.exploreBaseNode = state.cur;
      state.exploreBaseFen = fenBefore;
    }
    const move = board.tryMove({ from: orig, to: dest, promotion });
    if (!move) return renderBoard();
    state.boardLastMove = [orig, dest];
    state.evalShapes = [];
    state.exploreVerdict = "pending";
    const requestGeneration = ++state.exploreGeneration;
    renderBoard();
    updateStatus();
    onNavUpdate();
    onGraphRender();
    setChatContext(chess.fen(), null, null, explorationDetails());
    refreshEngineArrows();

    $("verdict").innerHTML = `<span class="line">Evaluating…</span>`;
    let result;
    try {
      result = await api.evaluate({ fen: fenBefore, move: uci });
    } catch (_) {
      if (requestGeneration !== state.exploreGeneration || !state.exploring) return;
      state.exploreVerdict = { error: true };
      updateStatus();
      renderVerdict({
        error: "Couldn't evaluate that move — the engine may be busy or restarting. Try again.",
      });
      return;
    }
    if (requestGeneration !== state.exploreGeneration || !state.exploring) return;
    state.exploreVerdict = result.move || (result.error ? { error: true } : null);
    updateStatus();
    renderVerdict(result);
    if (result.move) {
      setEvalBar(moverColor === "white" ? result.move.win_after : 100 - result.move.win_after);
      state.evalShapes = result.shapes || [];
      drawArrows();
    }
  }

  function resetVisualState() {
    state.exploreGeneration += 1;
    patch({
      currentMistake: -1,
      anchorNode: 0,
      currentPrompt: "",
      evalShapes: [],
      bestArrows: [],
      threatArrows: [],
      boardLastMove: null,
      exploreBaseFen: null,
    });
  }

  return {
    patch,
    renderBoard,
    drawArrows,
    applyEvalBarTheme,
    setEvalBar,
    renderVerdict,
    updateStatus,
    gotoNode,
    returnToReview,
    flipBoard,
    toggleBestArrows,
    toggleThreatArrows,
    stepBack,
    stepForward,
    handleMove,
    refreshEngineArrows,
    reviewedMoveNode,
    resetVisualState,
    get orient() { return state.orient; },
    get cur() { return state.cur; },
    get anchorNode() { return state.anchorNode; },
    get currentMistake() { return state.currentMistake; },
    get exploring() { return state.exploring; },
    get exploreBaseNode() { return state.exploreBaseNode; },
    get bestArrowOn() { return state.bestArrowOn; },
    get threatArrowOn() { return state.threatArrowOn; },
  };
}
