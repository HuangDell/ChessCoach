import { puzzleApi } from "../api/puzzles.js";
import { sleep } from "../core/async.js";
import { createLatestRequestScope } from "../core/async.js";

export function createSolutionPlayback({ $, board, boardView }) {
  const requests = createLatestRequestScope();
  let playback = null;
  let animationGeneration = 0;

  function clear() {
    animationGeneration += 1;
    requests.cancel();
    playback = null;
    const navigation = $("pz-review-nav");
    if (navigation) navigation.hidden = true;
  }

  async function start({ id, yourMove = null, solved = true, animate = false }) {
    clear();
    if (!id) return;
    const request = requests.begin();
    let solution;
    try {
      solution = await puzzleApi.solution(id, { signal: request.signal });
    } catch (_) {
      return;
    }
    if (!request.isCurrent()) return;
    const base = solution && solution.solve_fen;
    const moves = (solution && solution.solution_uci) || [];
    const sans = (solution && solution.solution_san) || [];
    if (!base || !moves.length) return;
    const fens = [base];
    const lastMoves = [null];
    const game = board.createGame(base);
    for (const uci of moves) {
      const move = game.move({
        from: uci.slice(0, 2),
        to: uci.slice(2, 4),
        promotion: uci.slice(4) || undefined,
      });
      if (!move) break;
      fens.push(game.fen());
      lastMoves.push([uci.slice(0, 2), uci.slice(2, 4)]);
    }
    playback = {
      fens,
      moves: moves.slice(0, fens.length - 1),
      sans,
      lastMoves,
      index: 0,
      yourMove,
      solved,
    };
    const generation = ++animationGeneration;
    if (animate) {
      goTo(0);
      for (let index = 1; index < fens.length; index += 1) {
        await sleep(650);
        if (generation !== animationGeneration) return;
        goTo(index);
      }
    } else {
      goTo(fens.length - 1);
    }
  }

  function goTo(index) {
    if (!playback) return;
    playback.index = Math.max(0, Math.min(playback.fens.length - 1, index));
    board.chess.load(playback.fens[playback.index]);
    boardView.setLastMove(playback.lastMoves[playback.index]);
    if (playback.index === 0) {
      boardView.setShapes(!playback.solved && playback.yourMove
        ? [{
            orig: playback.yourMove.slice(0, 2),
            dest: playback.yourMove.slice(2, 4),
            brush: "red",
          }]
        : []);
    } else {
      const move = playback.moves[playback.index - 1];
      boardView.setShapes([
        { orig: move.slice(0, 2), dest: move.slice(2, 4), brush: "green" },
      ]);
    }
    boardView.render(false);
    updateNavigation();
  }

  function step(delta) {
    if (!playback) return false;
    animationGeneration += 1;
    goTo(playback.index + delta);
    return true;
  }

  function updateNavigation() {
    const navigation = $("pz-review-nav");
    if (!playback) {
      navigation.hidden = true;
      return;
    }
    navigation.hidden = false;
    $("pz-review-prev").disabled = playback.index <= 0;
    $("pz-review-next").disabled = playback.index >= playback.fens.length - 1;
    const total = playback.fens.length - 1;
    $("pz-review-label").textContent = playback.index === 0
      ? "Start position"
      : `Move ${playback.index} / ${total}: ${playback.sans[playback.index - 1] || ""}`;
  }

  return { clear, start, step, get active() { return !!playback; } };
}
