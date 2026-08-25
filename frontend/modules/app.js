import { createBoardController } from "./board/controller.js";
import { createBoardLayoutController } from "./board/layout.js";
import { createGamesController } from "./games/controller.js";
import { createPuzzleController } from "./puzzles/controller.js";
import { createReviewController } from "./review/controller.js";
import { createSettingsController } from "./settings/controller.js";
import { createSystemController } from "./system/controller.js";
import { chatApi } from "./api/chat.js";
import { gamesApi } from "./api/games.js";
import { puzzleApi } from "./api/puzzles.js";
import { reviewApi } from "./api/review.js";
import { systemApi } from "./api/system.js";
import { byId } from "./core/dom.js";
import { storageGet, storageSet } from "./core/storage.js";

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
  let agentCapability = {};

  const supersedeStartup = (action) => (...args) => {
    startupGeneration += 1;
    return action(...args);
  };

  const reviewPort = {
    openGame: supersedeStartup((...args) => review.openGame(...args)),
    openBatch: supersedeStartup((...args) => review.openBatch(...args)),
    startSyncedBatch: supersedeStartup((...args) => review.startSyncedBatch(...args)),
    setWorkflowState: (...args) => review.setWorkflowState(...args),
    setPendingCritical: (id) => review.setPendingCritical(id),
  };

  function featurePreferences(config = {}) {
    if (config.agent) agentCapability = config.agent;
    return {
      review: {
        coachAiAuto: !!config.coach_ai_auto,
        personalizeHistory: config.personalize_history !== false,
        defaultReviewSide: config.default_review_side || "auto",
        boardOrientation: config.board_orientation || "review",
        analysisPreset: config.analysis_preset || "balanced",
        explanationProvider: config.explanation_provider || "auto",
        explanationLanguage: config.explanation_language || "zh-CN",
        showThreats: config.show_threat_arrows === true,
        agent: agentCapability,
      },
      puzzles: {
        personalizeHistory: config.personalize_history !== false,
        animations: config.puzzle_animations !== false,
        autoAdvance: config.puzzle_auto_advance === true,
      },
    };
  }

  function applySettings(config) {
    const preferences = featurePreferences(config);
    games.applySavedSettings(config);
    review.setPreferences(preferences.review);
    puzzles.setPreferences(preferences.puzzles);
    $("paste-side").value = config.default_review_side || "auto";
    review.refreshAfterSettings();
  }

  async function loadInitialState() {
    const generation = startupGeneration;
    try {
      if (!storageGet(sessionStorage, "chessAppSession")) {
        storageSet(sessionStorage, "chessAppSession", "1");
        await chatApi.reset().catch(() => {});
      }
    } catch (_) {}

    let config = {};
    try {
      config = await systemApi.appConfig();
    } catch (_) {}
    games.setConfig(config);
    const preferences = featurePreferences(config);
    review.setPreferences(preferences.review);
    puzzles.setPreferences(preferences.puzzles);
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

    if (generation !== startupGeneration) return;
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
    const artifactsLoaded = await review.loadReviewArtifacts(session.game_id, session.player);
    if (generation !== startupGeneration || artifactsLoaded === false) return;
    if (!wantPuzzle) review.selectInitial(session);
    review.restoreChat();
    review.prepareCoachAI(session);
    if (wantPuzzle) await puzzles.setMode(true, { resume: true });
  }

  async function openAgentPosition(reference = {}) {
    if (!reference.game_id) throw new Error("The referenced game is no longer available.");
    const requestGeneration = ++startupGeneration;
    const data = await gamesApi.history();
    if (requestGeneration !== startupGeneration) return false;
    const game = ((data && data.games) || []).find((item) =>
      item.game_id === reference.game_id &&
      (!reference.review_side || item.reviewed_side === reference.review_side)
    );
    if (!game || !game.pgn) throw new Error("The referenced game is no longer in local history.");
    review.setPendingCritical(reference.critical_id || null);
    review.setPendingPly(reference.critical_id ? null : reference.ply);
    return reviewPort.openGame(
      game.pgn,
      reference.review_side || game.reviewed_side,
      game.game_id
    );
  }

  function mount() {
    board.mount($("board"));
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
        openAgentPosition,
      },
    });

    puzzles = createPuzzleController({
      board,
      lifecycle: {
        layoutChanged: () => layout.modeChanged(),
        positionResizer: () => layout.positionResizer(),
        enter() {
          review.prepareForPuzzle();
          layout.closeHistoryDrawer();
        },
        positionChanged: (fen) => review.setAgentTrainingPosition(fen),
        async leave() {
          review.restoreBoard();
          await games.refreshProfile();
        },
        async replayGame(row, puzzle) {
          review.setPendingPly(puzzle.ply != null ? puzzle.ply - 1 : null);
          await puzzles.setMode(false);
          return reviewPort.openGame(row.pgn, row.reviewed_side, row.game_id || null);
        },
      },
    });

    games = createGamesController({
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
