import { puzzleApi } from "../api/puzzles.js";
import { createLatestRequestScope, sleep } from "../core/async.js";
import { escapeHtml } from "../core/dom.js";
import { renderMarkdown } from "../core/format.js";
import { formatClock, motifThemes } from "./helpers.js";

export function createPuzzleStorm({
  $,
  board,
  boardView,
  getConfig,
  isPuzzleActive,
  solution,
  trainer,
}) {
  const requests = createLatestRequestScope();
  const chess = board.chess;
  let request = null;
  let shown = false;
  let running = false;
  let busy = false;
  let generation = 0;
  let timerId = null;
  let deadline = 0;
  let score = 0;
  let combo = 0;
  let reviewEntries = [];
  let inReview = false;

  const status = (message) => { $("pz-status").textContent = message || ""; };

  function setShown(on) {
    if (on === shown) return;
    trainer.cancelAutoAdvance();
    inReview = false;
    solution.clear();
    $("pz-next").textContent = "Next puzzle →";
    shown = on;
    $("pz-mode-solve").classList.toggle("active", !on);
    $("pz-mode-storm").classList.toggle("active", on);
    $("pz-solve").hidden = on;
    $("pz-storm").hidden = !on;
    if (on) {
      trainer.cancel();
      status("");
      renderBests();
      resetBoard();
      showStart(false);
    } else {
      end({ abandon: true });
      if (isPuzzleActive()) trainer.loadNext();
    }
  }

  function resetBoard() {
    boardView.reset();
    setSide(null);
  }

  function showStart(gameOver, view) {
    const button = $("pz-storm-start");
    button.hidden = false;
    button.textContent = gameOver ? "↻ Play again" : "⚡ Start storm";
    setSide(null);
    const clock = $("pz-storm-clock");
    const config = getConfig() || {};
    const duration = (view && view.duration) || config.storm_duration || 180;
    clock.classList.remove("low");
    const message = $("pz-storm-msg");
    if (gameOver && view) {
      clock.textContent = "0:00";
      $("pz-storm-score").textContent = view.score;
      const newHigh = view.new_high ? ` <span class="pz-storm-nh">new best!</span>` : "";
      message.innerHTML = `Time! You solved <b>${view.score}</b>` +
        (view.misses ? ` · ${view.misses} missed` : "") +
        (view.best_combo ? ` · best combo ${view.best_combo}` : "") + newHigh;
    } else {
      clock.textContent = formatClock(duration);
      $("pz-storm-score").textContent = "0";
      $("pz-storm-combo").hidden = true;
      $("pz-storm-results").innerHTML = "";
      message.textContent =
        "Solve as many as you can before the clock runs out. A combo earns bonus time; a wrong move costs time.";
      $("pz-storm-review").hidden = true;
    }
    renderBests();
  }

  function renderBests() {
    const element = $("pz-storm-bests");
    if (!element) return;
    const config = getConfig() || {};
    const high = config.storm_high || 0;
    const bestCombo = config.storm_best_combo || 0;
    element.innerHTML = high || bestCombo
      ? `Best: <b>${high}</b> solved${bestCombo ? ` · combo ${bestCombo}` : ""}`
      : "";
  }

  async function start() {
    const button = $("pz-storm-start");
    button.hidden = true;
    inReview = false;
    solution.clear();
    $("pz-storm-review").hidden = true;
    $("pz-next").textContent = "Next puzzle →";
    status("");
    const currentGeneration = ++generation;
    request = requests.begin();
    let view;
    try {
      view = await puzzleApi.stormStart({ signal: request.signal });
    } catch (_) {
      if (currentGeneration !== generation || !request.isCurrent()) return;
      status("Couldn't start storm.");
      button.hidden = false;
      return;
    }
    if (currentGeneration !== generation || !request.isCurrent()) return;
    if (!view || view.error || !view.puzzle) {
      status((view && view.error) || "No puzzles available.");
      button.hidden = false;
      return;
    }
    running = true;
    score = 0;
    combo = 0;
    applyState(view);
    startClock();
    applyPuzzle(view.puzzle);
  }

  function startClock() {
    stopClock();
    timerId = setInterval(() => {
      const remaining = (deadline - Date.now()) / 1000;
      const clock = $("pz-storm-clock");
      clock.textContent = formatClock(remaining);
      clock.classList.toggle("low", remaining <= 10);
      if (remaining <= 0) {
        stopClock();
        timeUp();
      }
    }, 250);
  }

  function stopClock() {
    if (timerId) clearInterval(timerId);
    timerId = null;
  }

  async function timeUp() {
    if (!running) return;
    const currentGeneration = generation;
    let view;
    try {
      view = await puzzleApi.stormNext({ signal: request && request.signal });
    } catch (_) {
      view = { ended: true, score };
    }
    if (currentGeneration === generation) finish(view);
  }

  function applyState(view) {
    if (typeof view.remaining === "number") deadline = Date.now() + view.remaining * 1000;
    if (typeof view.score === "number") {
      score = view.score;
      $("pz-storm-score").textContent = score;
    }
    if (typeof view.combo === "number") {
      combo = view.combo;
      const element = $("pz-storm-combo");
      element.hidden = combo < 2;
      if (combo >= 2) element.textContent = `🔥 ${combo} combo`;
    }
    if (view.results) {
      $("pz-storm-results").innerHTML = view.results.slice(-16)
        .map((ok) => `<span class="pz-pip ${ok ? "ok" : "bad"}"></span>`)
        .join("");
    }
    const remaining = (deadline - Date.now()) / 1000;
    const clock = $("pz-storm-clock");
    clock.textContent = formatClock(remaining);
    clock.classList.toggle("low", remaining <= 10);
  }

  function applyPuzzle(puzzle) {
    generation += 1;
    busy = false;
    boardView.setShapes([]);
    boardView.setLastMove(null);
    const color = puzzle.side_to_move || "white";
    boardView.setOrientation(color);
    chess.load(puzzle.fen);
    boardView.render(true);
    setSide(color);
  }

  function setSide(color) {
    const element = $("pz-storm-side");
    if (!element) return;
    if (!color) {
      element.hidden = true;
      return;
    }
    const white = color === "white";
    element.innerHTML =
      `<span class="pz-storm-side-dot ${white ? "w" : "b"}"></span>` +
      `You play ${white ? "White" : "Black"}`;
    element.hidden = false;
  }

  async function serveNext() {
    const currentGeneration = ++generation;
    request = requests.begin();
    let view;
    try {
      view = await puzzleApi.stormNext({ signal: request.signal });
    } catch (_) {
      if (currentGeneration === generation) status("Couldn't load the next puzzle.");
      return;
    }
    if (currentGeneration !== generation || !request.isCurrent() || !running) return;
    if (view.ended || !view.puzzle) {
      finish(view);
      return;
    }
    applyState(view);
    applyPuzzle(view.puzzle);
  }

  async function handleMove(origin, destination) {
    if (busy || !running) {
      boardView.render(!busy);
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
      response = await puzzleApi.stormMove(
        { uci },
        { signal: request && request.signal }
      );
    } catch (_) {
      if (currentGeneration !== generation) return;
      chess.undo();
      boardView.render(true);
      busy = false;
      return;
    }
    if (currentGeneration !== generation || !running) return;
    if (response.error || response.ended) {
      finish(response);
      return;
    }
    applyState(response);
    if (!response.correct) {
      boardView.blink(destination, "bad");
      boardView.shake();
      await sleep(360);
      if (currentGeneration !== generation || !running) return;
      chess.undo();
      boardView.setLastMove(null);
      busy = false;
      serveNext();
      return;
    }
    if (response.puzzle_done && response.solved) {
      if (response.time_bonus) floatBonus(`+${Math.round(response.time_bonus)}s`);
      busy = false;
      serveNext();
      return;
    }
    await sleep(220);
    if (currentGeneration !== generation || !running) return;
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

  function floatBonus(text) {
    const clock = $("pz-storm-clock");
    if (!clock) return;
    const bonus = document.createElement("span");
    bonus.className = "pz-storm-bonus";
    bonus.textContent = text;
    clock.appendChild(bonus);
    setTimeout(() => bonus.remove(), 900);
  }

  function finish(view) {
    running = false;
    busy = false;
    stopClock();
    requests.cancel();
    resetBoard();
    const config = getConfig();
    if (view && config && (view.new_high || typeof view.high === "number")) {
      config.storm_high = Math.max(config.storm_high || 0, view.high || view.score || 0);
      if (view.best_combo) {
        config.storm_best_combo = Math.max(config.storm_best_combo || 0, view.best_combo);
      }
    }
    showStart(!!view, view);
    if (view && Array.isArray(view.log)) populateReview(view.log);
    else {
      const currentGeneration = generation;
      const reviewRequest = requests.begin();
      puzzleApi.stormReview({ signal: reviewRequest.signal })
        .then((response) => {
          if (currentGeneration === generation && reviewRequest.isCurrent()) {
            populateReview((response && response.log) || []);
          }
        })
        .catch(() => {
          if (currentGeneration === generation && reviewRequest.isCurrent()) populateReview([]);
        });
    }
  }

  function populateReview(log) {
    reviewEntries = Array.isArray(log) ? log : [];
    const wrapper = $("pz-storm-review");
    const list = $("pz-storm-review-list");
    const summaryButton = $("pz-storm-summary-btn");
    $("pz-storm-summary-out").hidden = true;
    $("pz-storm-summary-out").innerHTML = "";
    if (!reviewEntries.length) {
      wrapper.hidden = true;
      return;
    }
    summaryButton.hidden = !(getConfig() && getConfig().has_llm);
    summaryButton.disabled = false;
    const rows = reviewEntries.map((entry, index) => ({ entry, index }))
      .sort((left, right) => left.entry.solved === right.entry.solved
        ? left.index - right.index
        : left.entry.solved ? 1 : -1);
    list.innerHTML = "";
    for (const { entry } of rows) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `pz-storm-review-row ${entry.solved ? "ok" : "bad"}`;
      const label = motifThemes(entry.themes).slice(0, 2).join(", ") || "tactic";
      row.innerHTML = `<span class="pz-rr-mark">${entry.solved ? "✓" : "✗"}</span>` +
        `<span class="pz-rr-theme">${escapeHtml(label)}</span>` +
        `<span class="pz-rr-rating">${entry.rating || ""}</span>`;
      row.addEventListener("click", () => openReview(entry));
      list.appendChild(row);
    }
    wrapper.hidden = false;
  }

  function openReview(entry) {
    inReview = true;
    $("pz-storm").hidden = true;
    $("pz-solve").hidden = false;
    trainer.openStormReview(entry);
  }

  function closeReview() {
    if (!inReview) return false;
    inReview = false;
    trainer.cancel();
    $("pz-next").textContent = "Next puzzle →";
    $("pz-result").hidden = true;
    $("pz-explain-out").hidden = true;
    $("pz-explain-out").innerHTML = "";
    $("pz-solve").hidden = true;
    $("pz-storm").hidden = false;
    return true;
  }

  async function summarize() {
    const button = $("pz-storm-summary-btn");
    const output = $("pz-storm-summary-out");
    button.disabled = true;
    output.hidden = false;
    output.innerHTML = '<p class="muted">Snowie is reviewing your run (thinking)</p>';
    const summaryRequest = requests.begin();
    try {
      const response = await puzzleApi.stormSummary({ signal: summaryRequest.signal });
      if (!summaryRequest.isCurrent()) return;
      output.innerHTML = renderMarkdown(response.error || response.answer || "");
    } catch (_) {
      if (!summaryRequest.isCurrent()) return;
      output.innerHTML = '<p class="muted">Summary failed — try again.</p>';
    } finally {
      if (summaryRequest.isCurrent()) button.disabled = false;
    }
  }

  function end({ abandon = false } = {}) {
    stopClock();
    generation += 1;
    requests.cancel();
    if (running && abandon) puzzleApi.stormEnd().catch(() => {});
    running = false;
    busy = false;
  }

  return {
    closeReview,
    end,
    handleMove,
    setShown,
    start,
    summarize,
    get active() { return shown && running; },
    get shown() { return shown; },
  };
}
