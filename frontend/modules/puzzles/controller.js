import { puzzleApi } from "../api/puzzles.js";
import { createLatestRequestScope } from "../core/async.js";
import { byId } from "../core/dom.js";
import { storageGet, storageSet } from "../core/storage.js";
import { createPuzzleBoardView } from "./board-view.js";
import { createPuzzleProgress } from "./progress.js";
import { createSolutionPlayback } from "./solution-playback.js";
import { createPuzzleStorm } from "./storm.js";
import { createPuzzleTrainer, PUZZLE_MODE_KEY } from "./trainer.js";

export function createPuzzleController({ board, lifecycle }) {
  const $ = byId;
  const modeRequests = createLatestRequestScope();
  let active = false;
  let modeGeneration = 0;
  let config = null;
  let preferences = {};
  let storm;

  const boardView = createPuzzleBoardView({
    $,
    board,
    onPositionChange: (fen) => lifecycle.positionChanged?.(fen),
  });
  const progress = createPuzzleProgress({ $, getConfig: () => config });
  let trainer;
  const solution = createSolutionPlayback({ $, board, boardView });
  trainer = createPuzzleTrainer({
    $,
    board,
    boardView,
    getConfig: () => config,
    isActive: () => active,
    isStormShown: () => !!(storm && storm.shown),
    lifecycle: {
      ...lifecycle,
      completeTraining: () => setMode(false),
    },
    progress,
    solution,
  });
  storm = createPuzzleStorm({
    $,
    board,
    boardView,
    getConfig: () => config,
    isPuzzleActive: () => active,
    solution,
    trainer,
  });

  async function setMode(on, options = {}) {
    if (on === active) {
      if (on && options.puzzle) {
        if (storm.shown) storm.setShown(false);
        await trainer.loadNext(options.puzzle);
      }
      return;
    }
    const currentGeneration = ++modeGeneration;
    active = on;
    document.body.classList.toggle("puzzle-mode", on);
    $("mode-analyze").classList.toggle("active", !on);
    $("mode-puzzles").classList.toggle("active", on);
    lifecycle.layoutChanged();
    requestAnimationFrame(() => {
      board.redraw();
      lifecycle.positionResizer();
    });

    if (!on) {
      storageSet(localStorage, PUZZLE_MODE_KEY, "0");
      modeRequests.cancel();
      trainer.cancel();
      if (storm.shown) storm.setShown(false);
      else storm.end({ abandon: true });
      $("pz-next").textContent = "Next puzzle →";
      $("pz-solve").hidden = false;
      $("pz-storm").hidden = true;
      $("pz-mode-solve").classList.add("active");
      $("pz-mode-storm").classList.remove("active");
      await lifecycle.leave();
      return;
    }

    storageSet(localStorage, PUZZLE_MODE_KEY, "1");
    lifecycle.enter();
    const request = modeRequests.begin();
    if (!config) {
      try {
        config = await puzzleApi.config({ signal: request.signal });
      } catch (_) {
        config = null;
      }
      if (
        currentGeneration !== modeGeneration ||
        !active ||
        !request.isCurrent()
      ) return;
    }
    trainer.setPreferences(preferences);
    await trainer.initialize({ ...options, signal: request.signal });
  }

  function mount() {
    $("mode-analyze").addEventListener("click", () => setMode(false));
    $("mode-puzzles").addEventListener("click", () => setMode(true));
    $("pz-next").addEventListener("click", () => {
      if (!storm.closeReview()) trainer.loadNext();
    });
    $("pz-skip").addEventListener("click", () => trainer.loadNext());
    $("pz-prev").addEventListener("click", () => trainer.restorePrevious());
    $("pz-hint").addEventListener("click", () => trainer.hint());
    $("pz-solution").addEventListener("click", () => trainer.showSolution());
    $("pz-src-tactics").addEventListener("click", () => trainer.setSource("lichess"));
    $("pz-src-mine").addEventListener("click", () => trainer.setSource("your_games"));
    $("pz-category").addEventListener("change", (event) =>
      trainer.setCategory(event.target.value)
    );
    $("pz-weakness").addEventListener("change", (event) =>
      trainer.setWeakness(event.target.checked)
    );
    $("pz-easier").addEventListener("click", () => trainer.setDifficulty("easier"));
    $("pz-harder").addEventListener("click", () => trainer.setDifficulty("harder"));
    $("pz-mode-solve").addEventListener("click", () => storm.setShown(false));
    $("pz-mode-storm").addEventListener("click", () => storm.setShown(true));
    $("pz-storm-start").addEventListener("click", () => storm.start());
    $("pz-review-prev").addEventListener("click", () => solution.step(-1));
    $("pz-review-next").addEventListener("click", () => solution.step(1));
  }

  function handleKeydown(event) {
    if (solution.active && event.key === "ArrowLeft") {
      event.preventDefault();
      solution.step(-1);
      return true;
    }
    if (solution.active && event.key === "ArrowRight") {
      event.preventDefault();
      solution.step(1);
      return true;
    }
    return active;
  }

  function setPreferences(nextPreferences = {}) {
    preferences = nextPreferences;
    trainer.setPreferences(nextPreferences);
  }

  async function train({
    category = "",
    gameId = null,
    criticalId = null,
    positionReferences = [],
    objectiveSkillIds = [],
    source = null,
  } = {}) {
    const draftStart = trainer.prepareTraining({
      category,
      positionReferences,
      objectiveSkillIds,
      draftSource: source,
    });
    const puzzle = draftStart || {
      source: "your_games",
      category,
      game_id: gameId,
      critical_id: criticalId,
    };
    if (active) {
      if (storm.shown) storm.setShown(false);
      await trainer.loadNext(puzzle);
    } else {
      await setMode(true, { puzzle });
    }
  }

  return {
    mount,
    handleKeydown,
    handleMove: (origin, destination) =>
      storm.active
        ? storm.handleMove(origin, destination)
        : trainer.handleMove(origin, destination),
    setMode,
    train,
    setPreferences,
    setConfig: (nextConfig) => { config = nextConfig; },
    get shouldResume() { return storageGet(localStorage, PUZZLE_MODE_KEY) === "1"; },
    get active() { return active; },
  };
}
