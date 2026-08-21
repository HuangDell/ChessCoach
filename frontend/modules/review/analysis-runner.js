import { apiErrorMessage } from "./helpers.js";

export function createAnalysisRunner({
  $,
  api,
  bridge,
  getDefaultReviewSide,
  beginProvisional,
  applyReady,
  reportError,
  renderProgress,
}) {
  let abortController = null;
  let pollTimer = null;
  let batchInfo = null;

  function abortCurrentRequest() {
    if (abortController) abortController.abort();
    abortController = new AbortController();
    return abortController.signal;
  }

  async function openGame(pgn, side, gameId = null) {
    const signal = abortCurrentRequest();
    if ((!side || side === "auto") && getDefaultReviewSide() !== "auto") {
      side = getDefaultReviewSide();
    }
    batchInfo = null;
    bridge.closeHistory();
    beginProvisional(pgn, side, null, gameId);
    let status = null;
    try {
      const requestBody = gameId
        ? { review_side: side || "auto" }
        : { pgn, player: side || "auto" };
      status = gameId
        ? await api.analyzeGame(gameId, requestBody, { signal })
        : await api.analyze(requestBody, { signal });
    } catch (error) {
      if (error && error.name === "AbortError") return;
    }
    if (status && status.status === "ready") {
      await loadReady();
      return;
    }
    if (status && status.error) {
      reportError(apiErrorMessage(status.error, "Could not start analysis."));
      return;
    }
    startPolling();
  }

  async function openBatch(pgnText, side, username) {
    const signal = abortCurrentRequest();
    let result;
    try {
      result = await api.analyzeBatch(
        { pgn: pgnText, player: side || "auto", username: username || "" },
        { signal }
      );
    } catch (error) {
      if (error && error.name === "AbortError") return;
      $("history-status").textContent = "Could not start analysis.";
      return;
    }
    if (result.error || !result.total_games) {
      $("history-status").textContent = result.error || "No valid games found in that PGN.";
      return;
    }
    batchInfo = {
      total: result.total_games,
      self_handle: result.self_handle,
      lastDone: -1,
    };
    const who = result.self_handle ? ` as ${result.self_handle}` : "";
    $("history-status").textContent =
      `Analyzing ${result.total_games} games${who} → they'll appear in My games.`;
    bridge.closeHistory();
    beginProvisional(
      result.first_pgn,
      result.first_side,
      `Analyzing game 1 of ${result.total_games}…`
    );
    startPolling();
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(async () => {
      let status;
      try {
        status = await api.analysisStatus();
      } catch (_) {
        return;
      }
      if (batchInfo && status.done_games != null && status.done_games !== batchInfo.lastDone) {
        batchInfo.lastDone = status.done_games;
        if (bridge.isLocalHistory()) bridge.loadHistory();
      }
      if (status.status === "ready") {
        stopPolling();
        await loadReady();
      } else if (
        status.status === "error" &&
        (!batchInfo || (status.total_games || 1) === 1)
      ) {
        stopPolling();
        reportError(status.error);
      } else {
        renderProgress(status);
      }
    }, 800);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  async function loadReady() {
    const session = await api.session();
    const timeline = await api.timeline();
    if (session.empty) return;
    await applyReady(session, timeline);
    if (batchInfo) {
      const count = batchInfo.total;
      const who = batchInfo.self_handle ? ` as ${batchInfo.self_handle}` : "";
      batchInfo = null;
      bridge.activateLocalHistory();
      bridge.loadHistory(
        `Analyzed ${count} game${count === 1 ? "" : "s"}${who}. Showing the first below.`
      );
    } else if (bridge.isLocalHistory()) {
      bridge.loadHistory();
    }
  }

  function startSyncedBatch(info) {
    batchInfo = {
      total: info.new_games,
      self_handle: info.self_handle,
      lastDone: -1,
    };
    beginProvisional(
      info.first_pgn,
      info.first_side,
      `Syncing ${info.new_games} new chess.com game${info.new_games === 1 ? "" : "s"}… you can step through this one now.`
    );
    startPolling();
  }

  return { openGame, openBatch, startSyncedBatch };
}
