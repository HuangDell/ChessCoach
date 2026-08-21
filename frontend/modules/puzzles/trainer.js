import { gamesApi } from "../api/games.js";
import { puzzleApi } from "../api/puzzles.js";
import { createLatestRequestScope, sleep } from "../core/async.js";
import { escapeHtml } from "../core/dom.js";
import { categoryLabel, renderMarkdown } from "../core/format.js";
import { storageGet, storageSet } from "../core/storage.js";
import { historyRows } from "../games/helpers.js";
import { motifThemes } from "./helpers.js";

export const PUZZLE_MODE_KEY = "pzLastMode";
const SOURCE_KEY = "pzSource";
const CATEGORY_KEY = "pzCategory";

export function createPuzzleTrainer({
  $,
  board,
  boardView,
  chat,
  getConfig,
  isActive,
  isStormShown,
  lifecycle,
  progress,
  solution,
}) {
  const requests = createLatestRequestScope();
  const chess = board.chess;
  let request = null;
  let generation = 0;
  let data = null;
  let solveColor = "white";
  let done = false;
  let failed = false;
  let hinted = false;
  let busy = false;
  let missRating = null;
  let source = storageGet(localStorage, SOURCE_KEY, "lichess");
  let category = storageGet(localStorage, CATEGORY_KEY, "");
  let difficulty = null;
  let weakness = false;
  let autoAdvance = false;
  let animations = true;
  let advanceTimer = null;
  let previous = null;

  const status = (message) => { $("pz-status").textContent = message || ""; };

  function setPreferences(preferences = {}) {
    animations = preferences.animations !== false;
    autoAdvance = preferences.autoAdvance === true;
    boardView.setAnimations(animations);
  }

  async function initialize({ resume = false, puzzle = {}, signal } = {}) {
    progress.loadSession();
    progress.applyConfig();
    const config = getConfig() || {};
    const mineButton = $("pz-src-mine");
    mineButton.disabled = !config.has_engine;
    mineButton.title = config.has_engine
      ? "Practice positions from your own analysed games"
      : "Needs the chess engine (Stockfish) — unavailable";
    if (!config.has_engine && source === "your_games") source = "lichess";
    await loadCategories({ signal });
    if (!isActive()) return;
    syncSourceUi();
    progress.loadCard();
    if (resume && await resumeCurrent()) return;
    await loadNext(puzzle);
  }

  function cancel() {
    generation += 1;
    requests.cancel();
    cancelAutoAdvance();
    progress.cancel();
    solution.clear();
    chat.reset();
    data = null;
    busy = false;
    boardView.setShapes([]);
  }

  async function resumeCurrent() {
    const currentGeneration = ++generation;
    request = requests.begin();
    let current;
    try {
      current = await puzzleApi.current({ signal: request.signal });
    } catch (_) {
      return false;
    }
    if (currentGeneration !== generation || !request.isCurrent()) return true;
    if (!current || !current.active || current.finished) return false;
    data = {
      id: current.id,
      source: current.source || "lichess",
      side_to_move: current.side_to_move,
      themes: current.themes || [],
      rating: current.rating,
      your_rating: current.your_rating,
      game_id: current.game_id,
      critical_id: current.critical_id,
      reviewed_side: current.reviewed_side,
      ply: current.ply,
      win_drop: current.win_drop,
      category: current.category,
      phase: current.phase,
      badge: current.badge,
      game_url: current.game_url,
      fen: current.fen,
      solve_fen: current.fen,
      played_uci: current.played_uci,
      played_san: current.played_san,
    };
    source = data.source;
    done = false;
    failed = !!current.failed;
    hinted = !!current.hinted;
    missRating = null;
    busy = false;
    chat.reset();
    boardView.setShapes([]);
    boardView.setLastMove(null);
    solveColor = current.side_to_move || "white";
    boardView.setOrientation(solveColor);
    resetCards(data);
    syncSourceUi();
    progress.updateStats(current.your_rating, null);
    chess.load(current.fen);
    boardView.render(true);
    status(failed ? "Resumed — keep trying, or press Show solution." : "Resumed your puzzle.");
    return true;
  }

  async function loadNext(options = {}) {
    if (done) capturePrevious();
    else clearPrevious();
    cancelAutoAdvance();
    solution.clear();
    chat.reset();
    const currentGeneration = ++generation;
    request = requests.begin();
    busy = true;
    status("Loading…");
    const query = new URLSearchParams();
    const nextSource = options.source !== undefined ? options.source : source;
    if (nextSource && nextSource !== "lichess") query.set("source", nextSource);
    if (nextSource === "your_games") {
      const nextCategory = options.category !== undefined ? options.category : category;
      if (nextCategory) query.set("category", nextCategory);
      if (options.game_id) query.set("game_id", options.game_id);
      if (options.critical_id) query.set("critical_id", options.critical_id);
    }
    const nextDifficulty = options.difficulty !== undefined ? options.difficulty : difficulty;
    if (nextDifficulty) query.set("difficulty", nextDifficulty);
    if (weakness && nextSource === "lichess") query.set("weakness", "1");
    let puzzle;
    try {
      puzzle = await puzzleApi.next(query, { signal: request.signal });
    } catch (_) {
      if (currentGeneration !== generation || !request.isCurrent()) return;
      busy = false;
      status("Couldn't load a puzzle.");
      return;
    }
    if (currentGeneration !== generation || !request.isCurrent()) return;
    if (!puzzle || puzzle.error || !puzzle.fen) {
      busy = false;
      status((puzzle && puzzle.error) || "No puzzles available.");
      return;
    }
    applyPuzzle(puzzle, currentGeneration);
  }

  function applyPuzzle(puzzle, currentGeneration) {
    data = puzzle;
    done = false;
    failed = false;
    hinted = false;
    missRating = null;
    busy = true;
    solveColor = puzzle.side_to_move || "white";
    boardView.setOrientation(solveColor);
    boardView.setShapes([]);
    boardView.setLastMove(null);
    status("");
    resetCards(puzzle);
    progress.updateStats(puzzle.your_rating, null);
    boardView.prepare(puzzle.fen);
    setTimeout(() => {
      if (currentGeneration !== generation || !isActive() || isStormShown()) return;
      const setup = puzzle.setup_move;
      if (setup) {
        chess.move({
          from: setup.slice(0, 2),
          to: setup.slice(2, 4),
          promotion: setup.slice(4) || undefined,
        });
        boardView.setLastMove([setup.slice(0, 2), setup.slice(2, 4)]);
      }
      data.solve_fen ||= chess.fen();
      boardView.render(true);
      busy = false;
    }, 430);
    updatePreviousButton();
  }

  function resetCards(puzzle) {
    $("pz-result").hidden = true;
    $("pz-explain-out").hidden = true;
    $("pz-explain-out").innerHTML = "";
    $("pz-prompt").hidden = false;
    $("pz-ghosts").hidden = false;
    $("pz-explain").disabled = false;
    $("pz-prompt-line").textContent = `${solveColor === "white" ? "White" : "Black"} to move`;
    const mine = puzzle.source === "your_games";
    $("pz-prompt-sub").textContent = mine
      ? "Find a better move than you played"
      : "Find the best move";
    renderBadge(mine ? puzzle : null);
  }

  async function handleMove(origin, destination) {
    if (done || busy || !data) {
      boardView.render(!done);
      return;
    }
    const promotion = board.isPromotion(origin, destination) ? "q" : undefined;
    const uci = origin + destination + (promotion || "");
    if (!board.tryMove({ from: origin, to: destination, promotion })) {
      boardView.render(true);
      return;
    }
    boardView.setLastMove([origin, destination]);
    busy = true;
    const currentGeneration = generation;
    boardView.render(false);
    let response;
    try {
      response = await puzzleApi.move(
        { id: data.id, uci },
        { signal: request && request.signal }
      );
    } catch (_) {
      if (currentGeneration !== generation) return;
      chess.undo();
      boardView.render(true);
      busy = false;
      status("Move check failed — try again.");
      return;
    }
    if (currentGeneration !== generation) return;
    if (response.error) {
      chess.undo();
      boardView.render(true);
      busy = false;
      status(response.error);
      return;
    }
    if (!response.correct) {
      boardView.blink(destination, "bad");
      boardView.shake();
      await sleep(440);
      if (currentGeneration !== generation) return;
      chess.undo();
      boardView.setLastMove(null);
      failed = true;
      if (response.source === "your_games") boardView.setShapes(response.shapes || []);
      boardView.render(true);
      if (response.rating) {
        missRating = response.rating;
        progress.updateStats(response.rating.rating_after, response.rating.streak);
      }
      status(response.source === "your_games"
        ? response.message || "Still drops too much — try another move, or press Show solution."
        : response.rating && response.rating.rated
        ? "Not quite — that cost some rating. Try again, or press Show solution."
        : "Not quite — try again, or press Show solution.");
      busy = false;
      return;
    }
    if (response.is_complete) {
      const kind = failed ? "part" : "ok";
      boardView.blink(destination, kind);
      boardView.confetti(destination, kind);
      done = true;
      if (response.source === "your_games") boardView.setShapes(response.shapes || []);
      boardView.render(false);
      finish(
        failed || hinted ? "solved_with_hints" : "solved_first_try",
        response.rating,
        uci,
        response.source === "your_games" ? response : null
      );
      busy = false;
      return;
    }
    await sleep(280);
    if (currentGeneration !== generation) return;
    const reply = response.opponent_reply_uci;
    if (reply) {
      chess.move({
        from: reply.slice(0, 2),
        to: reply.slice(2, 4),
        promotion: reply.slice(4) || undefined,
      });
      boardView.setLastMove([reply.slice(0, 2), reply.slice(2, 4)]);
    }
    boardView.render(true);
    busy = false;
  }

  function finish(outcome, ratingSummary, yourMove, trainingFeedback = null) {
    done = true;
    $("pz-prompt").hidden = true;
    $("pz-ghosts").hidden = true;
    $("pz-result").hidden = false;
    const solved = outcome !== "failed";
    const clean = solved && !failed;
    const mine = data.source === "your_games";
    const verdict = $("pz-verdict");
    verdict.className = `pz-verdict ${!solved ? "bad" : clean ? "ok" : "part"}`;
    let heading;
    if (mine) {
      heading = solved
        ? clean ? "✓ Better than your game move!" : "✓ Better move found (after a try)"
        : "Solution shown";
    } else {
      heading = solved ? (clean ? "✓ Solved!" : "✓ Solved (after a miss)") : "Solution shown";
      const applied = ratingSummary && ratingSummary.rated
        ? ratingSummary
        : missRating && missRating.rated ? missRating : null;
      if (applied) {
        heading += `  ${applied.rating_before} → ${applied.rating_after} ` +
          `(${applied.delta >= 0 ? "+" : ""}${applied.delta})`;
        progress.updateStats(applied.rating_after, applied.streak);
      } else if (ratingSummary || missRating) {
        const summary = ratingSummary || missRating;
        heading += "  (unrated)";
        progress.updateStats(summary.rating_after, summary.streak);
      }
    }
    verdict.textContent = heading;
    if (!mine) progress.record(clean);
    if (mine) {
      data._trainingFeedback = trainingFeedback;
      $("pz-theme").innerHTML = personalResultHtml(trainingFeedback, yourMove);
      const replay = $("pz-replay");
      replay.hidden = false;
      replay.onclick = (event) => {
        event.preventDefault();
        replayMistake(data);
      };
    } else {
      const themes = motifThemes(data.themes);
      $("pz-theme").innerHTML = themes.length
        ? `Theme: <b>${escapeHtml(themes.slice(0, 3).join(", "))}</b>`
        : "";
      $("pz-replay").hidden = true;
    }
    data._outcome = outcome;
    data._yourMove = yourMove || null;
    $("pz-explain").hidden = !(getConfig() && getConfig().has_llm);
    progress.loadCard();
    if (!mine) solution.start({ id: data.id, solved, animate: false });
    else solution.clear();
    if (solved) scheduleAutoAdvance();
  }

  function personalResultHtml(feedback, yourMove) {
    if (!feedback) return "";
    const selected = (feedback.selected_move || {}).san || yourMove || "No move submitted";
    const gameMove = (feedback.game_move || {}).san || data.played_san || "—";
    const bestMove = (feedback.best_move || {}).san || "—";
    const variation = ((feedback.best_line || feedback.variation || {}).san || []).join(" ") || "—";
    return `<dl class="training-result">` +
      `<dt>Your choice</dt><dd>${escapeHtml(selected)}</dd>` +
      `<dt>Game choice</dt><dd>${escapeHtml(gameMove)}</dd>` +
      `<dt>Engine choice</dt><dd>${escapeHtml(bestMove)}</dd>` +
      `<dt>Key variation</dt><dd>${escapeHtml(variation)}</dd>` +
      `<dt>Error reason</dt><dd>${escapeHtml(feedback.original_error_reason || "—")}</dd>` +
      `<dt>Principle</dt><dd>${escapeHtml(feedback.transferable_principle || "—")}</dd></dl>`;
  }

  async function showSolution() {
    if (!data || done || busy) return;
    const currentGeneration = generation;
    busy = true;
    boardView.render(false);
    let response;
    try {
      response = await puzzleApi.giveUp(data.id, { signal: request && request.signal });
    } catch (_) {
      if (currentGeneration !== generation) return;
      busy = false;
      boardView.render(true);
      return;
    }
    if (currentGeneration !== generation) return;
    for (const uci of (response && response.solution_uci) || []) {
      chess.move({
        from: uci.slice(0, 2),
        to: uci.slice(2, 4),
        promotion: uci.slice(4) || undefined,
      });
      boardView.setLastMove([uci.slice(0, 2), uci.slice(2, 4)]);
      boardView.render(false);
      await sleep(480);
      if (currentGeneration !== generation) return;
    }
    busy = false;
    if (data.source === "your_games") {
      const best = (response && response.best_move) || {};
      if (best.uci) {
        boardView.setShapes([
          { orig: best.uci.slice(0, 2), dest: best.uci.slice(2, 4), brush: "green" },
        ]);
        boardView.render(false);
      }
    }
    finish("failed", null, null, data.source === "your_games" ? response : null);
  }

  async function explain() {
    if (!data) return;
    cancelAutoAdvance();
    const button = $("pz-explain");
    const output = $("pz-explain-out");
    const currentGeneration = generation;
    button.disabled = true;
    output.hidden = false;
    output.innerHTML = '<p class="muted">Snowie is sniffing around (thinking)</p>';
    try {
      const response = await puzzleApi.explain({
        id: data.id,
        outcome: data._outcome,
        your_move: data._yourMove,
      }, { signal: request && request.signal });
      if (currentGeneration !== generation) return;
      if (response.error) output.innerHTML = renderMarkdown(response.error);
      else {
        output.innerHTML = renderMarkdown(response.answer || "");
        chat.setContext(
          response.session_id,
          response.chat_fen || data.solve_fen || data.fen || null
        );
      }
    } catch (_) {
      if (currentGeneration === generation) {
        output.innerHTML = '<p class="muted">Explanation failed — try again.</p>';
      }
    } finally {
      if (currentGeneration === generation) button.disabled = false;
    }
  }

  async function hint() {
    if (!data || done) return;
    const currentGeneration = generation;
    let response;
    try {
      response = await puzzleApi.hint(data.id, { signal: request && request.signal });
    } catch (_) {
      return;
    }
    if (currentGeneration !== generation) return;
    if (response && response.kind) {
      hinted = true;
      boardView.setShapes(response.shapes || []);
      status(response.text || "Hint revealed.");
      if (response.line) await playPersonalHintLine(response.line, currentGeneration);
    } else if (response && response.from_square) {
      hinted = true;
      boardView.setShapes([{ orig: response.from_square, brush: "blue" }]);
      status(`Hint: move the piece on ${response.from_square} (this attempt is now unrated).`);
    }
  }

  async function playPersonalHintLine(line, currentGeneration) {
    if (!data || !(line && line.uci || []).length) return;
    busy = true;
    chess.load(data.solve_fen || data.fen);
    boardView.setLastMove(null);
    boardView.render(false);
    for (const uci of line.uci) {
      await sleep(480);
      if (currentGeneration !== generation) return;
      const move = chess.move({
        from: String(uci).slice(0, 2),
        to: String(uci).slice(2, 4),
        promotion: String(uci).slice(4, 5) || undefined,
      });
      if (!move) break;
      boardView.setLastMove([String(uci).slice(0, 2), String(uci).slice(2, 4)]);
      boardView.render(false);
    }
    await sleep(650);
    if (currentGeneration !== generation) return;
    chess.load(data.solve_fen || data.fen);
    boardView.setLastMove(null);
    busy = false;
    boardView.render(true);
  }

  async function replayMistake(puzzle) {
    let row = null;
    try {
      const response = await gamesApi.history();
      row = historyRows(response).find((game) =>
        game.game_id === puzzle.game_id &&
        game.reviewed_side === puzzle.reviewed_side &&
        game.has_pgn && game.pgn
      );
    } catch (_) {}
    if (row) await lifecycle.replayGame(row, puzzle);
    else if (puzzle.game_url) window.open(puzzle.game_url, "_blank", "noopener");
  }

  function renderBadge(puzzle) {
    const element = $("pz-source-badge");
    if (!puzzle) {
      element.hidden = true;
      element.textContent = "";
      return;
    }
    const badge = puzzle.badge || {};
    const opponent = puzzle.reviewed_side === "white" ? badge.black : badge.white;
    const parts = ["From your game"];
    if (opponent) parts.push(`vs ${opponent}`);
    if (badge.speed && badge.speed !== "unknown") parts.push(badge.speed);
    if (badge.date) parts.push(badge.date);
    if (puzzle.category) parts.push(categoryLabel(puzzle.category));
    element.textContent = parts.join(" · ");
    element.hidden = false;
  }

  function capturePrevious() {
    if (!done || !data) return;
    previous = {
      data,
      shapes: boardView.shapes,
      failed,
      hinted,
      solveColor,
      missRating,
      fen: chess.fen(),
      lastMove: boardView.lastMove,
      chat: chat.snapshot(),
      verdictText: $("pz-verdict").textContent,
      verdictClass: $("pz-verdict").className,
      themeHtml: $("pz-theme").innerHTML,
      replayHidden: $("pz-replay").hidden,
      explainHidden: $("pz-explain").hidden,
      explainDisabled: $("pz-explain").disabled,
      explainHtml: $("pz-explain-out").innerHTML,
      explainOutHidden: $("pz-explain-out").hidden,
      chatHtml: $("pz-chat-messages").innerHTML,
      chatHidden: $("pz-chat").hidden,
    };
    updatePreviousButton();
  }

  function restorePrevious() {
    if (!previous) return;
    cancelAutoAdvance();
    solution.clear();
    generation += 1;
    requests.cancel();
    request = requests.begin();
    const snapshot = previous;
    previous = null;
    data = snapshot.data;
    done = true;
    busy = false;
    failed = snapshot.failed;
    hinted = snapshot.hinted;
    solveColor = snapshot.solveColor;
    missRating = snapshot.missRating;
    boardView.setOrientation(solveColor);
    boardView.setShapes(snapshot.shapes);
    boardView.setLastMove(snapshot.lastMove);
    chess.load(snapshot.fen);
    chat.restore(snapshot.chat);
    $("pz-prompt").hidden = true;
    $("pz-ghosts").hidden = true;
    $("pz-result").hidden = false;
    $("pz-verdict").textContent = snapshot.verdictText;
    $("pz-verdict").className = snapshot.verdictClass;
    $("pz-theme").innerHTML = snapshot.themeHtml;
    $("pz-replay").hidden = snapshot.replayHidden;
    if (!snapshot.replayHidden) {
      $("pz-replay").onclick = (event) => {
        event.preventDefault();
        replayMistake(data);
      };
    }
    $("pz-explain").hidden = snapshot.explainHidden;
    $("pz-explain").disabled = snapshot.explainDisabled;
    $("pz-explain-out").hidden = snapshot.explainOutHidden;
    $("pz-explain-out").innerHTML = snapshot.explainHtml;
    $("pz-chat").hidden = snapshot.chatHidden;
    $("pz-chat-messages").innerHTML = snapshot.chatHtml;
    boardView.render(false);
    status("Reviewing your previous puzzle — press Explain or ask below, then Next to continue.");
    updatePreviousButton();
  }

  function clearPrevious() {
    previous = null;
    updatePreviousButton();
  }

  function updatePreviousButton() {
    const button = $("pz-prev");
    if (button) button.hidden = !previous;
  }

  function cancelAutoAdvance() {
    if (advanceTimer !== null) {
      clearTimeout(advanceTimer);
      advanceTimer = null;
    }
  }

  function scheduleAutoAdvance() {
    cancelAutoAdvance();
    if (!autoAdvance) return;
    const currentGeneration = generation;
    advanceTimer = setTimeout(() => {
      advanceTimer = null;
      if (!isActive() || isStormShown() || currentGeneration !== generation || !done) return;
      loadNext();
    }, animations ? 1900 : 1000);
  }

  async function loadCategories(options) {
    const select = $("pz-category");
    if (!select) return;
    let categories = [];
    try {
      const response = await puzzleApi.categories(options);
      categories = (response && response.categories) || [];
    } catch (_) {
      return;
    }
    select.innerHTML = '<option value="">All categories</option>' + categories
      .map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(categoryLabel(item))}</option>`)
      .join("");
    if (category && !categories.includes(category)) category = "";
    select.value = category;
  }

  function syncSourceUi() {
    const mine = source === "your_games";
    $("pz-src-tactics").classList.toggle("active", !mine);
    $("pz-src-mine").classList.toggle("active", mine);
    $("pz-controls").style.display = mine ? "none" : "";
    $("pz-mine-filter").hidden = !mine;
    $("pz-category").value = category;
  }

  function setSource(nextSource) {
    if (nextSource === source) return;
    if (nextSource === "your_games" && !(getConfig() && getConfig().has_engine)) return;
    source = nextSource;
    storageSet(localStorage, SOURCE_KEY, source);
    syncSourceUi();
    if (source === "your_games") loadCategories();
    loadNext();
  }

  function setCategory(nextCategory) {
    category = nextCategory || "";
    storageSet(localStorage, CATEGORY_KEY, category);
    if (isActive() && source === "your_games") loadNext();
  }

  function setDifficulty(nextDifficulty) {
    difficulty = difficulty === nextDifficulty ? null : nextDifficulty;
    $("pz-easier").classList.toggle("active", difficulty === "easier");
    $("pz-harder").classList.toggle("active", difficulty === "harder");
    loadNext();
  }

  function prepareTraining({ category: nextCategory = "" } = {}) {
    source = "your_games";
    category = nextCategory;
    storageSet(localStorage, SOURCE_KEY, source);
    storageSet(localStorage, CATEGORY_KEY, category);
  }

  function openStormReview(entry) {
    cancel();
    generation += 1;
    request = requests.begin();
    data = {
      id: entry.id,
      fen: entry.fen,
      solve_fen: entry.fen,
      themes: entry.themes || [],
      source: "lichess",
      side_to_move: entry.side_to_move || "white",
      _outcome: entry.solved ? "solved_first_try" : "failed",
      _yourMove: entry.your_move || null,
    };
    failed = !entry.solved;
    hinted = false;
    done = true;
    busy = false;
    solveColor = data.side_to_move;
    boardView.setOrientation(solveColor);
    boardView.setLastMove(null);
    boardView.setShapes(!entry.solved && entry.your_move
      ? [{ orig: entry.your_move.slice(0, 2), dest: entry.your_move.slice(2, 4), brush: "red" }]
      : []);
    $("pz-prompt").hidden = true;
    $("pz-ghosts").hidden = true;
    $("pz-progress").hidden = true;
    $("pz-statcard").hidden = true;
    $("pz-source-badge").hidden = true;
    $("pz-result").hidden = false;
    const verdict = $("pz-verdict");
    verdict.className = `pz-verdict ${entry.solved ? "ok" : "bad"}`;
    verdict.textContent = entry.solved ? "✓ You solved this" : "✗ You missed this";
    const themes = motifThemes(entry.themes);
    $("pz-theme").innerHTML = themes.length
      ? `Theme: <b>${escapeHtml(themes.slice(0, 3).join(", "))}</b>`
      : "";
    $("pz-replay").hidden = true;
    $("pz-explain-out").hidden = true;
    $("pz-explain-out").innerHTML = "";
    $("pz-explain").hidden = !(getConfig() && getConfig().has_llm);
    $("pz-explain").disabled = false;
    $("pz-next").textContent = "‹ Back to results";
    chess.load(entry.fen);
    boardView.render(false);
    status("Reviewing a storm puzzle — step through the solution below the board, or press Explain.");
    solution.start({
      id: entry.id,
      yourMove: entry.your_move,
      solved: entry.solved,
      animate: true,
    });
  }

  return {
    cancel,
    cancelAutoAdvance,
    explain,
    handleMove,
    hint,
    initialize,
    loadNext,
    openStormReview,
    prepareTraining,
    restorePrevious,
    setCategory,
    setDifficulty,
    setPreferences,
    setSource,
    setWeakness: (enabled) => {
      weakness = enabled;
      loadNext();
    },
    showSolution,
    get current() { return data; },
  };
}
