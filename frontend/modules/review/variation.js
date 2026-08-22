import { clamp } from "../core/dom.js";

export function createReviewVariation({
  $,
  board,
  getActiveCritical,
  setContext,
  renderBoard,
  updateStatus,
  onPositionChange = () => {},
}) {
  const chess = board.chess;
  let current = null;

  function wireLinks() {
    document.querySelectorAll("[data-variation]").forEach((button) =>
      button.addEventListener("click", () =>
        start(button.dataset.variation, Number(button.dataset.step), false)
      )
    );
    document.querySelectorAll("[data-play-line]").forEach((button) =>
      button.addEventListener("click", () => start(button.dataset.playLine, 0, true))
    );
  }

  function stop(hide = true) {
    if (current && current.timer) clearInterval(current.timer);
    current = null;
    if (hide && $("variation-controls")) $("variation-controls").hidden = true;
    document.querySelectorAll(".san-move.active").forEach((item) => item.classList.remove("active"));
  }

  function renderStep() {
    if (!current) return;
    const step = clamp(current.index, 0, current.fens.length - 1);
    current.index = step;
    chess.load(current.fens[step]);
    onPositionChange(chess.fen(), {
      mode: "variation",
      basePly: current.basePly,
      baseFen: current.fens[0],
      criticalId: current.criticalId,
      explorationMovesUci: current.ucis.slice(0, step),
      explorationMovesSan: current.sans.slice(0, step),
    });
    const uci = step > 0 ? current.ucis[step - 1] : null;
    setContext({ boardLastMove: uci ? [uci.slice(0, 2), uci.slice(2, 4)] : null });
    renderBoard();
    $("variation-play").textContent = current.playing ? "Ⅱ" : "▶";
    $("variation-label").textContent = `${current.kind === "best" ? "Best line" : "Played line"} · ${step} / ${current.ucis.length}`;
    document.querySelectorAll(".san-move[data-variation]").forEach((button) => {
      button.classList.toggle(
        "active",
        button.dataset.variation === current.kind && Number(button.dataset.step) === step
      );
    });
    updateStatus();
  }

  function start(kind, step = 0, autoplay = false) {
    const critical = getActiveCritical();
    const line = critical && (kind === "played" ? critical.played_line : critical.best_line);
    if (!critical || !line || !(line.uci || []).length) return;
    stop(false);
    const game = board.createGame(critical.fen_before);
    const fens = [game.fen()];
    const ucis = [];
    const sans = [];
    for (const uci of line.uci) {
      const move = game.move({
        from: String(uci).slice(0, 2),
        to: String(uci).slice(2, 4),
        promotion: String(uci).slice(4, 5) || undefined,
      });
      if (!move) break;
      ucis.push(uci);
      sans.push(move.san);
      fens.push(game.fen());
    }
    if (!ucis.length) return;
    setContext({
      exploring: true,
      exploreBaseNode: Number(critical.ply) - 1,
      exploreVerdict: null,
    });
    current = {
      kind,
      ucis,
      sans,
      fens,
      basePly: Number(critical.ply) - 1,
      criticalId: critical.critical_id,
      index: clamp(step, 0, ucis.length),
      timer: null,
      playing: autoplay,
    };
    $("variation-controls").hidden = false;
    renderStep();
    if (autoplay) play();
  }

  function play() {
    if (!current) return;
    if (current.index >= current.ucis.length) current.index = 0;
    current.playing = true;
    if (current.timer) clearInterval(current.timer);
    current.timer = setInterval(() => {
      if (!current) return;
      if (current.index >= current.ucis.length) {
        clearInterval(current.timer);
        current.timer = null;
        current.playing = false;
        renderStep();
        return;
      }
      current.index += 1;
      renderStep();
    }, 750);
    renderStep();
  }

  function toggle() {
    if (!current) return;
    if (current.playing) {
      if (current.timer) clearInterval(current.timer);
      current.timer = null;
      current.playing = false;
      renderStep();
    } else {
      play();
    }
  }

  return {
    wireLinks,
    stop,
    start,
    toggle,
    get active() { return !!current; },
  };
}
