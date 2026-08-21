import { gamesApi } from "../api/games.js";
import { settingsApi } from "../api/settings.js";
import { systemApi } from "../api/system.js";
import { byId } from "../core/dom.js";
import { errorMessage } from "../core/errors.js";
import { createGamesImporter } from "./importer.js";
import { createGamesInsights } from "./insights.js";
import { createGamesLibrary } from "./library.js";

const CHESSCOM_REFRESH_WINDOW_MS = 20_000;
const CHESSCOM_REFRESH_TRIGGER = 3;

export function createGamesController({ bridge }) {
  const $ = byId;
  let historyMode = "normal";
  let appMode = false;
  let appUsername = "";
  let chesscomUsername = "";
  let chesscomSync = true;
  let chesscomRefreshTimes = [];
  let lastLichessUser = "";
  let insights;

  const library = createGamesLibrary({
    $,
    bridge,
    getIdentity: () => ({ appUsername, chesscomUsername }),
    onHistoryChanged: () => insights.load(),
    onRemoteAccount: reflectSetAsMe,
  });
  const importer = createGamesImporter({
    $,
    bridge,
    setMode,
    isPasteMode: () => historyMode === "paste",
  });
  insights = createGamesInsights({ $, bridge, library });

  function noteChesscomRefresh() {
    const now = Date.now();
    chesscomRefreshTimes.push(now);
    chesscomRefreshTimes = chesscomRefreshTimes.filter(
      (time) => now - time <= CHESSCOM_REFRESH_WINDOW_MS
    );
    if (chesscomRefreshTimes.length >= CHESSCOM_REFRESH_TRIGGER) {
      const hint = $("chesscom-hint");
      if (hint) hint.hidden = false;
    }
  }

  function clearChesscomHint() {
    chesscomRefreshTimes = [];
    const hint = $("chesscom-hint");
    if (hint) hint.hidden = true;
  }

  async function syncChesscom(quiet) {
    if (!chesscomUsername) return false;
    if (!quiet) $("history-status").textContent = "Syncing chess.com games…";
    let response;
    try {
      response = await gamesApi.syncChesscom({});
    } catch (_) {
      if (!quiet) $("history-status").textContent = "Could not reach Chess.com.";
      return false;
    }
    if (!response || response.error || !response.new_games) {
      if (!quiet) {
        $("history-status").textContent = response && response.error
          ? errorMessage(response.error)
          : "chess.com is up to date — no new games.";
      }
      return false;
    }
    const count = response.new_games;
    clearChesscomHint();
    $("history-status").textContent =
      `Syncing ${count} new chess.com game${count === 1 ? "" : "s"} → they'll appear in My games.`;
    bridge.review.startSyncedBatch(response);
    return true;
  }

  async function saveIdentity(lichess, chesscom) {
    appUsername = (lichess || "").trim();
    chesscomUsername = (chesscom || "").trim();
    if (appUsername) $("lichess-user").placeholder = appUsername;
    try {
      await settingsApi.save({
        username: appUsername,
        chesscom_username: chesscomUsername,
      });
    } catch (_) {}
  }

  async function saveUsername(username) {
    appUsername = (username || "").trim();
    if (appUsername) $("lichess-user").placeholder = appUsername;
    try {
      await settingsApi.save({ username: appUsername });
    } catch (_) {}
  }

  function showFirstRun(defaultUsername) {
    const overlay = $("firstrun");
    if (!overlay) return;
    $("firstrun-user").value = defaultUsername || "";
    $("firstrun-chesscom-user").value = chesscomUsername || "";
    overlay.hidden = false;
    $("firstrun-user").focus();
  }

  async function maybeAutoload() {
    if (chesscomSync && (await syncChesscom(true))) return true;
    if (appUsername) await library.autoOpenLatest("lichess", appUsername);
    else if (chesscomUsername) await library.autoOpenLatest("chesscom", chesscomUsername);
    else showFirstRun("");
    return true;
  }

  function reflectSetAsMe(username) {
    lastLichessUser = username;
    const button = $("set-as-me");
    if (!button) return;
    const isMe = !!appUsername && appUsername.toLowerCase() === username && !!username;
    button.disabled = isMe;
    button.textContent = isMe ? "✓ This is your account" : "Set as my account";
  }

  function activateTab(mode) {
    historyMode = mode;
    clearChesscomHint();
    for (const name of ["normal", "lichess", "chesscom", "paste"]) {
      $(`mode-${name}`).classList.toggle("active", mode === name);
    }
    $("lichess-form").style.display = mode === "lichess" ? "flex" : "none";
    $("chesscom-form").style.display = mode === "chesscom" ? "flex" : "none";
    $("paste-form").style.display = mode === "paste" ? "flex" : "none";
    $("history-list").style.display = mode === "paste" ? "none" : "";
    if (mode !== "paste") {
      $("history-status").classList.remove("import-error", "import-success", "import-action");
    }
  }

  function setMode(mode) {
    activateTab(mode);
    if (mode === "normal") library.loadHistory();
    else if (mode === "lichess" || mode === "chesscom") library.resetAndLoadRemote(mode);
    else {
      library.cancelRequests();
      importer.updateHint();
    }
  }

  async function clearEngineCache() {
    const status = $("data-action-status");
    status.textContent = "Clearing…";
    try {
      const data = await systemApi.clearEngineCache();
      if (data.error) throw new Error(errorMessage(data.error, "Cache cleanup failed."));
      status.textContent = `${data.files_removed || 0} cached file(s) removed.`;
    } catch (error) {
      status.textContent = errorMessage(error, "Cache cleanup failed.");
    }
  }

  function mount() {
    $("history-toggle").addEventListener("click", bridge.toggleHistory);
    $("history-collapse").addEventListener("click", bridge.toggleHistory);
    for (const mode of ["normal", "lichess", "chesscom", "paste"]) {
      $(`mode-${mode}`).addEventListener("click", () => setMode(mode));
    }
    $("lichess-form").addEventListener("submit", (event) => {
      event.preventDefault();
      library.resetAndLoadRemote("lichess");
    });
    $("chesscom-form").addEventListener("submit", (event) => {
      event.preventDefault();
      noteChesscomRefresh();
      library.resetAndLoadRemote("chesscom");
    });
    $("chesscom-sync").addEventListener("click", () => {
      if (!chesscomUsername) {
        $("history-status").textContent = "Set your chess.com username in Settings first.";
        return;
      }
      noteChesscomRefresh();
      syncChesscom(false);
    });
    $("chesscom-hint-paste").addEventListener("click", () => setMode("paste"));
    $("set-as-me").addEventListener("click", async () => {
      const username = ($("lichess-user").value.trim() || lastLichessUser || appUsername).trim();
      if (!username) return;
      await saveUsername(username);
      reflectSetAsMe(username.toLowerCase());
      library.loadHistory(undefined, { resetPaging: true });
    });
    $("firstrun-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const lichess = $("firstrun-user").value.trim();
      const chesscom = $("firstrun-chesscom-user").value.trim();
      if (!lichess && !chesscom) return;
      await saveIdentity(lichess, chesscom);
      $("firstrun").hidden = true;
      maybeAutoload();
    });
    $("clear-engine-cache").addEventListener("click", clearEngineCache);
    importer.mount();
    insights.mount();
  }

  function setConfig(config = {}) {
    appMode = !!config.app_mode;
    appUsername = (config.lichess_username || "").trim();
    chesscomUsername = (config.chesscom_username || "").trim();
    chesscomSync = config.chesscom_sync !== false;
    if (appUsername) $("lichess-user").placeholder = appUsername;
    if (chesscomUsername) $("chesscom-user").placeholder = chesscomUsername;
  }

  function applySavedSettings(settings = {}) {
    appUsername = settings.username || "";
    chesscomUsername = settings.chesscom_username || "";
    chesscomSync = settings.chesscom_sync !== false;
    if (appUsername) $("lichess-user").placeholder = appUsername;
    if (historyMode === "normal") library.loadHistory();
  }

  return {
    mount,
    loadHistory: library.loadHistory,
    maybeAutoload,
    setConfig,
    applySavedSettings,
    isLocalHistory: () => historyMode === "normal",
    activateLocal: () => activateTab("normal"),
    get appMode() { return appMode; },
  };
}
