import { gamesApi } from "../api/games.js";
import { createLatestRequestScope } from "../core/async.js";
import { errorMessage } from "../core/errors.js";
import { escapeHtml } from "../core/dom.js";
import { historyRows, resultClass, resultWord, sideForUser } from "./helpers.js";

const HISTORY_PAGE = 10;
const REMOTE_PAGE = 5;

export function createGamesLibrary({ $, bridge, getIdentity, onHistoryChanged, onRemoteAccount }) {
  let historyGames = [];
  let historyCount = HISTORY_PAGE;
  let playerId = "";
  const historyRequests = createLatestRequestScope();
  const remoteRequests = createLatestRequestScope();
  const remote = {
    lichess: { count: REMOTE_PAGE, username: "" },
    chesscom: { count: REMOTE_PAGE, username: "" },
  };

  function provider(source) {
    const chesscom = source === "chesscom";
    return {
      source: chesscom ? "chesscom" : "lichess",
      label: chesscom ? "Chess.com" : "Lichess",
      loadGames: chesscom ? gamesApi.chesscomGames : gamesApi.lichessGames,
      state: remote[chesscom ? "chesscom" : "lichess"],
      fallback: chesscom ? getIdentity().chesscomUsername : playerId,
    };
  }

  async function loadHistory(doneMessage, { resetPaging = false } = {}) {
    remoteRequests.cancel();
    const request = historyRequests.begin();
    $("history-status").textContent = "Loading…";
    let data;
    try {
      data = await gamesApi.history({ signal: request.signal });
    } catch (_) {
      if (!request.isCurrent()) return;
      $("history-status").textContent = "Could not load history.";
      return;
    }
    if (!request.isCurrent()) return;
    data ||= {};
    playerId = data.player_id || playerId;
    if (playerId) $("lichess-user").placeholder = playerId;
    historyGames = historyRows(data);
    if (resetPaging) {
      historyCount = HISTORY_PAGE;
      $("history-list").scrollTop = 0;
    } else {
      historyCount = Math.min(
        Math.max(historyCount, HISTORY_PAGE),
        Math.max(historyGames.length, HISTORY_PAGE)
      );
    }
    renderPage();
    $("history-status").textContent = historyGames.length
      ? doneMessage || ""
      : "No analyzed games yet.";
    onHistoryChanged();
  }

  function renderPage() {
    const list = $("history-list");
    const scrollTop = list.scrollTop;
    renderHistory(historyGames.slice(0, historyCount), "normal");
    const remaining = historyGames.length - historyCount;
    if (remaining > 0) {
      const item = document.createElement("li");
      item.className = "load-more";
      item.textContent = `Show more (${remaining})`;
      item.addEventListener("click", () => {
        historyCount += HISTORY_PAGE;
        renderPage();
      });
      list.appendChild(item);
    }
    list.scrollTop = scrollTop;
  }

  async function loadRemote(source, username) {
    historyRequests.cancel();
    const request = remoteRequests.begin();
    const remoteProvider = provider(source);
    remoteProvider.state.username = username;
    $("history-status").textContent = `Fetching from ${remoteProvider.label}…`;
    const query = new URLSearchParams();
    if (username) query.set("username", username);
    query.set("max", String(remoteProvider.state.count));
    let data;
    try {
      data = await remoteProvider.loadGames(query, { signal: request.signal });
    } catch (_) {
      if (!request.isCurrent()) return;
      $("history-status").textContent = `Could not reach ${remoteProvider.label}.`;
      return;
    }
    if (!request.isCurrent()) return;
    data ||= {};
    if (data.error) {
      $("history-status").textContent = errorMessage(data.error, `Could not load ${remoteProvider.label}.`);
      $("history-list").innerHTML = "";
      return;
    }
    const games = historyRows(data);
    const who = (username || remoteProvider.fallback || "").toLowerCase();
    if (source === "lichess") onRemoteAccount(who);
    renderHistory(games, "remote", who);
    $("history-status").textContent = games.length ? "" : "No games found.";
    if (games.length >= remoteProvider.state.count) {
      const item = document.createElement("li");
      item.className = "load-more";
      item.textContent = "Load more";
      item.addEventListener("click", () => {
        remoteProvider.state.count += REMOTE_PAGE;
        loadRemote(remoteProvider.source, remoteProvider.state.username);
      });
      $("history-list").appendChild(item);
    }
  }

  function resetAndLoadRemote(source) {
    const remoteProvider = provider(source);
    remoteProvider.state.count = REMOTE_PAGE;
    return loadRemote(source, $(`${remoteProvider.source}-user`).value.trim());
  }

  function renderHistory(games, mode, who) {
    const list = $("history-list");
    list.innerHTML = "";
    for (const game of games) {
      const item = document.createElement("li");
      let side;
      let title;
      let subtitle;
      let blunders;
      let disabled;
      let className;
      if (mode === "normal") {
        side = game.reviewed_side;
        const opponent = side === "white" ? game.black : game.white;
        className = resultClass(game.player_result);
        const accuracy = game.accuracy != null ? `${game.accuracy}%` : "?";
        title = `${resultWord(game.player_result) || "vs"} ${opponent || "?"}`;
        subtitle = `${game.date || ""} · ${game.opening || "—"} · ${accuracy} · ${game.speed}`;
        blunders = (game.counts && game.counts.blunder) || 0;
        disabled = !game.has_pgn;
        if (game.is_me || (playerId && game.player_id && game.player_id === playerId)) {
          className += " mine";
        }
      } else {
        side = sideForUser(game, who);
        className = "";
        title = `${game.white || "?"} vs ${game.black || "?"}`;
        subtitle = `${game.date || ""} · ${game.opening || "—"} · ${game.speed} · ${game.result || ""}`;
        blunders = null;
        disabled = !game.pgn;
      }
      item.className = className + (disabled ? " disabled" : "");
      item.innerHTML =
        `<div class="h-title"><span>${escapeHtml(title)}</span><span class="h-actions">` +
        (blunders ? `<span class="h-blunders">●${blunders}</span>` : "") +
        (mode === "normal"
          ? `<button type="button" class="h-delete" title="Delete this local game">Delete</button>`
          : "") +
        `</span></div><div class="h-sub">${escapeHtml(subtitle)}</div>`;
      const deleteButton = item.querySelector(".h-delete");
      if (deleteButton) {
        deleteButton.addEventListener("click", (event) => {
          event.stopPropagation();
          deleteGame(game);
        });
      }
      if (disabled) {
        item.title = mode === "normal"
          ? "Can't reopen — this game was analyzed before PGNs were stored. Re-analyze it from Lichess."
          : "No PGN available for this game.";
      } else {
        item.addEventListener("click", () =>
          bridge.review.openGame(game.pgn, side, mode === "normal" ? game.game_id : null)
        );
      }
      list.appendChild(item);
    }
  }

  async function deleteGame(game) {
    const opponent = game.reviewed_side === "white" ? game.black : game.white;
    if (!window.confirm(
      `Delete the local game against ${opponent || "this opponent"}? Analysis, explanations, and linked training attempts will also be removed.`
    )) return;
    const status = $("history-status");
    status.textContent = "Deleting local game…";
    try {
      const data = await gamesApi.deleteGame(game.game_id);
      if (data.error) throw new Error(errorMessage(data.error, "Delete failed."));
      historyGames = historyGames.filter((item) => item.game_id !== game.game_id);
      renderPage();
      status.textContent =
        `Deleted game and ${data.attempts_removed || 0} linked training attempt(s).`;
      onHistoryChanged();
    } catch (error) {
      status.textContent = errorMessage(error, "Could not delete the local game.");
    }
  }

  async function autoOpenLatest(source, username) {
    const remoteProvider = provider(source);
    const request = remoteRequests.begin();
    $("game-meta").textContent =
      `Loading ${username}'s most recent ${remoteProvider.label} game…`;
    try {
      const data = await remoteProvider.loadGames(
        new URLSearchParams({ username, max: "1" }),
        { signal: request.signal }
      );
      if (!request.isCurrent()) return;
      const games = historyRows(data);
      if (data.error || !games.length) {
        $("game-meta").textContent = data.error
          ? errorMessage(data.error)
          : `No ${remoteProvider.label} games found for ${username} — pick one from the Games panel.`;
        return;
      }
      bridge.review.openGame(games[0].pgn, sideForUser(games[0], username));
    } catch (_) {
      if (!request.isCurrent()) return;
      $("game-meta").textContent =
        `Could not reach ${remoteProvider.label} — pick a game from the Games panel.`;
    }
  }

  return {
    autoOpenLatest,
    cancelRequests() {
      historyRequests.cancel();
      remoteRequests.cancel();
    },
    findGame: (gameId, reviewedSide) => historyGames.find(
      (game) => game.game_id === gameId && (!reviewedSide || game.reviewed_side === reviewedSide)
    ),
    loadHistory,
    loadRemote,
    resetAndLoadRemote,
  };
}
