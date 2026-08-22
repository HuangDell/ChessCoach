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
  pollDelayMs = 800,
}) {
  let abortController = null;
  let pollTimer = null;
  let operationGeneration = 0;
  let batchInfo = null;

  function beginOperation() {
    stopPolling();
    operationGeneration += 1;
    if (abortController) abortController.abort();
    abortController = new AbortController();
    return { generation: operationGeneration, signal: abortController.signal };
  }

  function isCurrent(generation) {
    return generation === operationGeneration;
  }

  async function openGame(pgn, side, gameId = null) {
    const { generation, signal } = beginOperation();
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
    if (!isCurrent(generation)) return;
    if (status && status.status === "ready") {
      await loadReady(generation, signal);
      return;
    }
    if (status && status.error) {
      reportError(apiErrorMessage(status.error, "Could not start analysis."));
      return;
    }
    startPolling(generation, signal);
  }

  async function openBatch(pgnText, side, username) {
    const { generation, signal } = beginOperation();
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
    if (!isCurrent(generation)) return;
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
    startPolling(generation, signal);
  }

  function startPolling(generation, signal) {
    stopPolling();
    const poll = async () => {
      pollTimer = null;
      if (!isCurrent(generation)) return;
      let status;
      try {
        status = await api.analysisStatus({ signal });
      } catch (error) {
        if (!isCurrent(generation) || (error && error.name === "AbortError")) return;
        scheduleNext();
        return;
      }
      if (!isCurrent(generation)) return;
      if (batchInfo && status.done_games != null && status.done_games !== batchInfo.lastDone) {
        batchInfo.lastDone = status.done_games;
        if (bridge.isLocalHistory()) bridge.loadHistory();
      }
      if (status.status === "ready") {
        stopPolling();
        await loadReady(generation, signal);
      } else if (
        status.status === "error" &&
        (!batchInfo || (status.total_games || 1) === 1)
      ) {
        stopPolling();
        reportError(status.error);
      } else {
        renderProgress(status);
        scheduleNext();
      }
    };
    const scheduleNext = () => {
      if (isCurrent(generation)) pollTimer = setTimeout(poll, pollDelayMs);
    };
    scheduleNext();
  }

  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  async function loadReady(generation, signal) {
    let session;
    let timeline;
    try {
      session = await api.session({ signal });
      if (!isCurrent(generation)) return;
      timeline = await api.timeline({ signal });
    } catch (error) {
      if (error && error.name === "AbortError") return;
      throw error;
    }
    if (!isCurrent(generation) || session.empty) return;
    await applyReady(session, timeline, {
      signal,
      isCurrent: () => isCurrent(generation),
    });
    if (!isCurrent(generation)) return;
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
    const { generation, signal } = beginOperation();
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
    startPolling(generation, signal);
  }

  return { openGame, openBatch, startSyncedBatch };
}
