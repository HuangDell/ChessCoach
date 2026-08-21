import { createBoardController } from "./board/controller.js";
import { createBoardLayoutController } from "./board/layout.js";
import { createGamesController } from "./games/controller.js";
import { createPuzzleController } from "./puzzles/controller.js";
import { createReviewController } from "./review/controller.js";
import { createSettingsController } from "./settings/controller.js";
import { createSystemController } from "./system/controller.js";
import { puzzleApi } from "./api/puzzles.js";
import { reviewApi } from "./api/review.js";
import { systemApi } from "./api/system.js";
import { byId } from "./core/dom.js";
import { categoryLabel, renderMarkdown } from "./core/format.js";

export function createApp() {
  const $ = byId;
  const board = createBoardController();
  let layout;
  let review;
  let games;
  let puzzles;
  let settings;
  let system;
  let startupGeneration = 0;

  const reviewPort = {
    openGame(...args) {
      startupGeneration++;
      return review.openGame(...args);
    },
    openBatch(...args) {
      startupGeneration++;
      return review.openBatch(...args);
    },
    startSyncedBatch(info) {
      startupGeneration++;
      return review.startSyncedBatch(info);
    },
    setWorkflowState: (...args) => review.setWorkflowState(...args),
    setPendingCritical: (id) => review.setPendingCritical(id),
  };

  function reviewPreferences(config = {}) {
    return {
      coachAiAuto: !!config.coach_ai_auto,
      personalizeHistory: config.personalize_history !== false,
      defaultReviewSide: config.default_review_side || "auto",
      boardOrientation: config.board_orientation || "review",
      analysisPreset: config.analysis_preset || "balanced",
      explanationProvider: config.explanation_provider || "auto",
      explanationLanguage: config.explanation_language || "zh-CN",
      showThreats: config.show_threat_arrows === true,
    };
  }

  function puzzlePreferences(config = {}) {
    return {
      personalizeHistory: config.personalize_history !== false,
      animations: config.puzzle_animations !== false,
      autoAdvance: config.puzzle_auto_advance === true,
    };
  }

  function applySettings(config) {
    games.applySavedSettings(config);
    review.setPreferences(reviewPreferences(config));
    puzzles.setPreferences(puzzlePreferences(config));
    $("paste-side").value = config.default_review_side || "auto";
    review.refreshAfterSettings();
  }

  async function loadInitialState() {
    try {
      if (!sessionStorage.getItem("chessAppSession")) {
        sessionStorage.setItem("chessAppSession", "1");
        await reviewApi.resetChat().catch(() => {});
      }
    } catch (_) {}

    let config = {};
    try {
      config = await systemApi.appConfig();
    } catch (_) {}
    games.setConfig(config);
    review.setPreferences(reviewPreferences(config));
    puzzles.setPreferences(puzzlePreferences(config));
    $("paste-side").value = config.default_review_side || "auto";
    if (games.appMode) system.startHeartbeat();

    let wantPuzzle = false;
    try {
      const puzzleConfig = await puzzleApi.config();
      if (puzzleConfig && puzzleConfig.enabled) {
        puzzles.setConfig(puzzleConfig);
        $("mode-switch").hidden = false;
        wantPuzzle = puzzles.shouldResume;
      }
    } catch (_) {}

    system.checkSetup();
    system.checkUpdates();
    system.checkOnline();

    const generation = startupGeneration;
    const session = await reviewApi.session();
    if (generation !== startupGeneration) return;
    if (session.empty) {
      if (wantPuzzle) {
        await puzzles.setMode(true, { resume: true });
        return;
      }
      if (games.appMode && (await games.maybeAutoload())) return;
      $("game-meta").textContent =
        "Waiting to open a game — pick one from the Games panel or paste a PGN.";
      return;
    }

    const timeline = await reviewApi.timeline();
    if (generation !== startupGeneration) return;
    review.loadInitial(session, timeline);
    await review.loadReviewArtifacts(session.game_id, session.player);
    if (!wantPuzzle) review.selectInitial(session);
    review.restoreChat();
    review.prepareCoachAI(session);
    if (wantPuzzle) await puzzles.setMode(true, { resume: true });
  }

  function mount() {
    board.mount($("board"), { orientation: "white" });
    layout = createBoardLayoutController(board);
    layout.mount();

    review = createReviewController({
      board,
      bridge: {
        trainPuzzle: (options) => puzzles.train(options),
        closeHistory: () => layout.closeHistoryDrawer(),
        isLocalHistory: () => games.isLocalHistory(),
        activateLocalHistory: () => games.activateLocal(),
        loadHistory: (...args) => games.loadHistory(...args),
      },
    });

    puzzles = createPuzzleController({
      board,
      categoryLabel,
      renderMarkdown,
      lifecycle: {
        layoutChanged: () => layout.modeChanged(),
        positionResizer: () => layout.positionResizer(),
        enter() {
          review.prepareForPuzzle();
          layout.closeHistoryDrawer();
        },
        leave: () => review.restoreBoard(),
        async replayGame(row, puzzle) {
          review.setPendingPly(puzzle.ply != null ? puzzle.ply - 1 : null);
          await puzzles.setMode(false);
          return reviewPort.openGame(row.pgn, row.reviewed_side, row.game_id || null);
        },
      },
    });

    games = createGamesController({
      board,
      bridge: {
        review: reviewPort,
        puzzles: {
          get active() { return puzzles.active; },
          setMode: (...args) => puzzles.setMode(...args),
          train: (...args) => puzzles.train(...args),
        },
        toggleHistory: () => layout.toggleHistory(),
      },
    });

    system = createSystemController({ isAppMode: () => games.appMode });
    settings = createSettingsController({
      isPuzzleMode: () => puzzles.active,
      onSaved: applySettings,
    });

    board.setMoveHandler((orig, dest) =>
      puzzles.active ? puzzles.handleMove(orig, dest) : review.handleMove(orig, dest)
    );
    review.mount();
    puzzles.mount();
    games.mount();
    settings.mount();

    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && ["INPUT", "TEXTAREA"].includes(event.target.tagName)) {
        event.target.blur();
        return;
      }
      if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;
      if (puzzles.handleKeydown(event)) return;
      review.handleKeydown(event);
    });

    loadInitialState();
    games.loadHistory();
  }

  return { mount };
}
