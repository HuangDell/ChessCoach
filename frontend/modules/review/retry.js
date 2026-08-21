import { sleep } from "../core/async.js";
import { escapeHtml } from "../core/dom.js";

export function createRetryController({
  $,
  board,
  api,
  getContext,
  setContext,
  getActiveCritical,
  stopVariation,
  selectCritical,
  gotoNode,
  renderBoard,
  updateStatus,
}) {
  const chess = board.chess;
  let session = null;

  function renderState() {
    if (!session) return;
    $("retry-panel").hidden = false;
    $("retry-state").textContent = String(session.state || "idle").replaceAll("_", " ");
    const hintLabels = ["Hint: Think", "Hint: Area", "Hint: First move", "Show line"];
    const hintButton = $("retry-hint");
    hintButton.textContent = hintLabels[session.hintsUsed] || "All hints shown";
    hintButton.disabled = session.hintsUsed >= 4 || session.state === "evaluating";
    $("retry-again").hidden = !session.locked || session.state === "evaluating";
    updateStatus();
  }

  function resetPosition({ keepHints = true } = {}) {
    if (!session) return;
    session.solutionGen += 1;
    session.state = "awaiting_move";
    session.locked = false;
    session.shapes = keepHints ? (session.hintShapes || []).slice() : [];
    setContext({ boardLastMove: null });
    chess.load(session.fen);
    $("retry-feedback").hidden = true;
    $("retry-feedback").innerHTML = "";
    $("retry-prompt").textContent = "Choose a legal move directly on the board.";
    renderBoard();
    renderState();
  }

  function start() {
    const critical = getActiveCritical();
    const context = getContext();
    if (!critical || !context.currentGameId) return;
    stopVariation();
    session = {
      criticalId: critical.critical_id,
      fen: critical.fen_before,
      state: "awaiting_move",
      hintsUsed: 0,
      locked: false,
      shapes: [],
      hintShapes: [],
      solutionGen: 0,
      saved: {
        cur: context.cur,
        orient: context.orient,
        criticalId: critical.critical_id,
        bestArrowOn: context.bestArrowOn,
        threatArrowOn: context.threatArrowOn,
      },
    };
    document.body.classList.add("retry-mode");
    setContext({
      exploring: false,
      orient: critical.side || context.player,
      bestArrows: [],
      threatArrows: [],
      evalShapes: [],
    });
    $("retry-hints").innerHTML = "";
    $("retry-title").textContent = `${critical.side === "black" ? "Black" : "White"} to move`;
    resetPosition({ keepHints: false });
  }

  function exit() {
    if (!session) return;
    const saved = session.saved;
    session.solutionGen += 1;
    session = null;
    document.body.classList.remove("retry-mode");
    $("retry-panel").hidden = true;
    setContext({
      bestArrowOn: saved.bestArrowOn,
      threatArrowOn: saved.threatArrowOn,
      orient: saved.orient,
    });
    $("best-toggle").checked = saved.bestArrowOn;
    $("threat-toggle").checked = saved.threatArrowOn;
    selectCritical(saved.criticalId);
    if (saved.cur !== getContext().anchorNode) gotoNode(saved.cur);
  }

  function feedbackHtml(result) {
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

  async function handleMove(orig, dest) {
    if (!session || session.locked) {
      renderBoard();
      return;
    }
    const promotion = board.isPromotion(orig, dest) ? "q" : undefined;
    const uci = orig + dest + (promotion || "");
    const move = board.tryMove({ from: orig, to: dest, promotion });
    if (!move) {
      renderBoard();
      return;
    }
    const activeSession = session;
    const requestGeneration = activeSession.solutionGen;
    activeSession.state = "evaluating";
    activeSession.locked = true;
    activeSession.shapes = [];
    setContext({ boardLastMove: [orig, dest] });
    $("retry-prompt").textContent = "Stockfish is checking your choice…";
    renderBoard();
    renderState();

    let result;
    try {
      const context = getContext();
      result = await api.trainingAttempt({
        game_id: context.currentGameId,
        critical_id: activeSession.criticalId,
        selected_move: uci,
        review_side: context.player,
        hints_used: activeSession.hintsUsed,
        source: "retry",
      });
      if (result.error) throw new Error(result.error);
    } catch (error) {
      if (session !== activeSession || requestGeneration !== activeSession.solutionGen) return;
      chess.undo();
      setContext({ boardLastMove: null });
      activeSession.state = "awaiting_move";
      activeSession.locked = false;
      activeSession.shapes = (activeSession.hintShapes || []).slice();
      $("retry-prompt").textContent = error.message || "Move evaluation failed. Try again.";
      renderBoard();
      renderState();
      return;
    }
    if (session !== activeSession || requestGeneration !== activeSession.solutionGen) return;
    activeSession.state = result.solved ? "completed" : "feedback";
    activeSession.locked = true;
    activeSession.shapes = result.shapes || [];
    $("retry-prompt").textContent = result.solved
      ? "This choice solves the position's main problem."
      : "Review the feedback, then calculate again.";
    $("retry-feedback").innerHTML = feedbackHtml(result);
    $("retry-feedback").hidden = false;
    renderBoard();
    renderState();
  }

  async function playSolution(line) {
    if (!session || !line || !(line.uci || []).length) return;
    const activeSession = session;
    const generation = ++activeSession.solutionGen;
    activeSession.state = "showing_solution";
    activeSession.locked = true;
    chess.load(activeSession.fen);
    setContext({ boardLastMove: null });
    renderBoard();
    renderState();
    for (const uci of line.uci) {
      await sleep(520);
      if (session !== activeSession || generation !== activeSession.solutionGen) return;
      const move = chess.move({
        from: String(uci).slice(0, 2),
        to: String(uci).slice(2, 4),
        promotion: String(uci).slice(4, 5) || undefined,
      });
      if (!move) break;
      setContext({ boardLastMove: [String(uci).slice(0, 2), String(uci).slice(2, 4)] });
      renderBoard();
    }
    renderState();
  }

  async function showHint() {
    if (!session || session.hintsUsed >= 4 || session.state === "evaluating") return;
    const activeSession = session;
    const level = activeSession.hintsUsed + 1;
    let result;
    try {
      const context = getContext();
      result = await api.trainingHint(new URLSearchParams({
        game_id: context.currentGameId,
        critical_id: activeSession.criticalId,
        review_side: context.player,
        level: String(level),
      }));
      if (result.error) throw new Error(result.error);
    } catch (error) {
      $("retry-prompt").textContent = error.message || "Hint unavailable.";
      return;
    }
    if (session !== activeSession) return;
    activeSession.hintsUsed = level;
    activeSession.state = level === 4 ? "showing_solution" : "showing_hint";
    const row = document.createElement("div");
    row.className = "retry-hint-row";
    row.textContent = result.text || "";
    $("retry-hints").appendChild(row);
    activeSession.hintShapes = result.shapes || activeSession.hintShapes || [];
    activeSession.shapes = activeSession.hintShapes.slice();
    activeSession.locked = level === 4;
    if (level === 4 && result.line) await playSolution(result.line);
    else {
      chess.load(activeSession.fen);
      setContext({ boardLastMove: null });
      renderBoard();
      renderState();
    }
  }

  function mount() {
    $("retry-critical").addEventListener("click", start);
    $("retry-hint").addEventListener("click", showHint);
    $("retry-again").addEventListener("click", () => resetPosition({ keepHints: true }));
    $("retry-exit").addEventListener("click", exit);
  }

  return {
    mount,
    start,
    exit,
    handleMove,
    showHint,
    resetPosition,
    get session() { return session; },
  };
}
