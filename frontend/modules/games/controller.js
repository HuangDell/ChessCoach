import { gamesApi } from "../api/games.js";
import { byId, escapeHtml } from "../core/dom.js";
import { categoryLabel } from "../core/format.js";

export function createGamesController({ board, bridge }) {
  const $ = byId;

let historyMode = "normal"; // "normal" (local games) | "lichess" | "chesscom" | "paste"
let myPlayerId = ""; // configured user's id, for inferring side on lichess lookups
let lichessCount = 5; // how many recent lichess games to show ("Load more" grows it)
let lichessUser = ""; // the handle currently shown in lichess mode (for "Load more")
const LICHESS_PAGE = 5; // initial count + how many more each "Load more"
let chesscomCount = 5; // paging for the Chess.com tab, same scheme as lichess
let chesscomUser = "";
let pasteSourceType = "pgn_text";
// "My games": /api/history returns every analysed game; we render them in pages (inside the
// fixed-height scroll box) so the list starts short and grows on "Show more", not the page.
let historyGames = []; // all rows from the last /api/history fetch
let historyCount = 10; // how many to show now ("Show more" grows it)
const HISTORY_PAGE = 10; // initial count + how many more each "Show more"
let profileWeaknesses = [];

// App mode (double-click launcher): on open, auto-load the user's most recent game.
// `appUsername` is the Lichess handle (config.LICHESS_USERNAME); it drives "open my latest game"
// and the Lichess panel placeholder. `chesscomUsername` is the configured chess.com handle — it
// drives the automatic chess.com sync on launch (new games are fetched + analyzed into My games)
// and the chess.com autoload for users without a Lichess handle.
let appMode = false;
let appUsername = "";
let chesscomUsername = "";
// Auto-sync the configured chess.com user's newest games on launch (a Settings option, default on):
// check the most recent `chesscomSyncMax` games and analyze any not already in history.
let chesscomSync = true;
let chesscomSyncMax = 5;
// Rapid-refresh detection: if the user hammers Load/Sync on the Chess.com tab (a fresh game not
// showing yet), surface a small "chess.com is slow to publish — upload the PGN instead" hint.
let chesscomRefreshTimes = [];
const CHESSCOM_REFRESH_WINDOW_MS = 20000;
const CHESSCOM_REFRESH_TRIGGER = 3;

async function maybeAutoload() {
  if (chesscomSync && (await syncChesscom(true))) return true;
  if (appUsername) await autoOpenLatest(appUsername);
  else if (chesscomUsername) await autoOpenLatestChesscom(chesscomUsername);
  else showFirstRun("");
  return true;
}

// Auto-sync: ask the server to fetch the configured chess.com user's newest games and analyze the
// Record a manual Chess.com refresh (Load or Sync). When the user triggers it 3+ times inside a
// 20s window — the tell-tale of waiting on a just-finished game that chess.com hasn't published
// yet — reveal the "upload the PGN instead" hint.
function noteChesscomRefresh() {
  const now = Date.now();
  chesscomRefreshTimes.push(now);
  chesscomRefreshTimes = chesscomRefreshTimes.filter((t) => now - t <= CHESSCOM_REFRESH_WINDOW_MS);
  if (chesscomRefreshTimes.length >= CHESSCOM_REFRESH_TRIGGER) {
    const hint = $("chesscom-hint");
    if (hint) hint.hidden = false;
  }
}

// Hide the rapid-refresh hint and reset its counter (on tab switch, or once new games arrive).
function clearChesscomHint() {
  chesscomRefreshTimes = [];
  const hint = $("chesscom-hint");
  if (hint) hint.hidden = true;
}

// ones history hasn't seen. Returns true when a sync batch was started (the board shows its first
// game). `quiet` suppresses the "nothing new" message (used on app launch).
async function syncChesscom(quiet) {
  if (!chesscomUsername) return false;
  if (!quiet) $("history-status").textContent = "Syncing chess.com games…";
  let res;
  try {
    res = await gamesApi.syncChesscom({});
  } catch (_) {
    if (!quiet) $("history-status").textContent = "Could not reach Chess.com.";
    return false;
  }
  if (!res || res.error || !res.new_games) {
    if (!quiet)
      $("history-status").textContent =
        res && res.error ? res.error : "chess.com is up to date — no new games.";
    return false;
  }
  const n = res.new_games;
  clearChesscomHint(); // new games arrived — the "chess.com is slow" advice no longer applies
  $("history-status").textContent =
    `Syncing ${n} new chess.com game${n === 1 ? "" : "s"} → they'll appear in My games.`;
  bridge.review.startSyncedBatch(res);
  return true;
}

// Open the configured chess.com user's most recent game (autoload for chess.com-only users).
async function autoOpenLatestChesscom(username) {
  $("game-meta").textContent = `Loading ${username}'s most recent chess.com game…`;
  const q = new URLSearchParams({ username, max: "1" });
  let data;
  try {
    data = await gamesApi.chesscomGames(q);
  } catch (_) {
    $("game-meta").textContent = "Could not reach Chess.com — pick a game from the Games panel.";
    return;
  }
  if (data.error || !(data.games || []).length) {
    $("game-meta").textContent = data.error
      ? data.error
      : `No chess.com games found for ${username} — pick one from the Games panel.`;
    return;
  }
  const g = data.games[0];
  bridge.review.openGame(g.pgn, sideForUser(g, username));
}

// Persist both handles server-side (Lichess + chess.com) in one write and reflect them locally.
// Used by the first-run prompt, which offers both fields at once.
async function saveIdentity(lichess, chesscom) {
  appUsername = (lichess || "").trim();
  chesscomUsername = (chesscom || "").trim();
  if (appUsername) $("lichess-user").placeholder = appUsername;
  try {
    await gamesApi.saveSettings({ username: appUsername, chesscom_username: chesscomUsername });
  } catch (_) {}
}

// Persist the configured username server-side (unified identity) and reflect it locally.
async function saveUsername(username) {
  appUsername = (username || "").trim();
  if (appUsername) $("lichess-user").placeholder = appUsername;
  try {
    await gamesApi.saveSettings({ username: appUsername });
  } catch (_) {}
}

// Infer which side `who` played in a Lichess game (same rule as renderHistory's lichess branch).
function sideForUser(game, who) {
  const w = (game.white || "").toLowerCase();
  const b = (game.black || "").toLowerCase();
  const me = (who || "").toLowerCase();
  return me && w === me ? "white" : me && b === me ? "black" : "auto";
}

async function autoOpenLatest(username) {
  $("game-meta").textContent = `Loading ${username}'s most recent Lichess game…`;
  const q = new URLSearchParams({ username, max: "1" });
  let data;
  try {
    data = await gamesApi.lichessGames(q);
  } catch (_) {
    $("game-meta").textContent = "Could not reach Lichess — pick a game from the Games panel.";
    return;
  }
  if (data.error || !(data.games || []).length) {
    $("game-meta").textContent = data.error
      ? data.error
      : `No Lichess games found for ${username} — pick one from the Games panel.`;
    return;
  }
  const g = data.games[0];
  bridge.review.openGame(g.pgn, sideForUser(g, username));
}

function showFirstRun(defaultUsername) {
  const overlay = $("firstrun");
  if (!overlay) return;
  $("firstrun-user").value = defaultUsername || "";
  $("firstrun-chesscom-user").value = chesscomUsername || "";
  overlay.hidden = false;
  $("firstrun-user").focus();
}

// --- history / lichess panel ---------------------------------------------
// `resetPaging` collapses back to the first page + scrolls to top; callers pass it only on a
// genuine identity change. The default preserves how far the user paged/scrolled, so refreshing
// the list after opening/analyzing a game doesn't force them to press "Show more" and re-scroll
// to find an older game they were looking at.
async function loadHistory(doneMsg, { resetPaging = false } = {}) {
  $("history-status").textContent = "Loading…";
  let data;
  try {
    data = await gamesApi.history();
  } catch (_) {
    $("history-status").textContent = "Could not load history.";
    return;
  }
  myPlayerId = data.player_id || myPlayerId;
  if (myPlayerId) $("lichess-user").placeholder = myPlayerId;
  historyGames = data.games || [];
  if (resetPaging) {
    historyCount = HISTORY_PAGE;
    $("history-list").scrollTop = 0;
  } else {
    // Keep the current page count (clamped to the list) so an expanded list stays expanded.
    historyCount = Math.min(Math.max(historyCount, HISTORY_PAGE), Math.max(historyGames.length, HISTORY_PAGE));
  }
  renderMyGames();
  $("history-status").textContent = historyGames.length ? doneMsg || "" : "No analyzed games yet.";
  loadInsights(); // the aggregate reflects whatever just landed in history
}

// Render the current page of "My games" into the (fixed-height, scrollable) list, with a
// "Show more" row when there are extra games beyond what's shown. No refetch — pages a cached list.
function renderMyGames() {
  const box = $("history-list");
  const prevScroll = box.scrollTop; // rebuilding the list resets scroll; restore it below
  renderHistory(historyGames.slice(0, historyCount), "normal");
  const remaining = historyGames.length - historyCount;
  if (remaining > 0) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = `Show more (${remaining})`;
    li.addEventListener("click", () => {
      historyCount += HISTORY_PAGE;
      renderMyGames();
    });
    box.appendChild(li);
  }
  box.scrollTop = prevScroll;
}

async function loadLichess(username) {
  lichessUser = username;
  $("history-status").textContent = "Fetching from Lichess…";
  const q = new URLSearchParams();
  if (username) q.set("username", username);
  q.set("max", String(lichessCount));
  let data;
  try {
    data = await gamesApi.lichessGames(q);
  } catch (_) {
    $("history-status").textContent = "Could not reach Lichess.";
    return;
  }
  if (data.error) {
    $("history-status").textContent = data.error;
    $("history-list").innerHTML = "";
    return;
  }
  const games = data.games || [];
  const who = (username || myPlayerId || "").toLowerCase();
  reflectSetAsMe(who); // is the looked-up account already "me"?
  renderHistory(games, "lichess", who);
  $("history-status").textContent = games.length ? "" : "No games found.";
  // While the server keeps returning a full page, there are probably more to fetch.
  if (games.length >= lichessCount) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = "Load more";
    li.addEventListener("click", () => {
      lichessCount += LICHESS_PAGE;
      loadLichess(lichessUser);
    });
    $("history-list").appendChild(li);
  }
}

async function loadChesscom(username) {
  chesscomUser = username;
  $("history-status").textContent = "Fetching from Chess.com…";
  const q = new URLSearchParams();
  if (username) q.set("username", username);
  q.set("max", String(chesscomCount));
  let data;
  try {
    data = await gamesApi.chesscomGames(q);
  } catch (_) {
    $("history-status").textContent = "Could not reach Chess.com.";
    return;
  }
  if (data.error) {
    $("history-status").textContent = data.error;
    $("history-list").innerHTML = "";
    return;
  }
  const games = data.games || [];
  const who = (username || chesscomUsername || "").toLowerCase();
  renderHistory(games, "lichess", who); // same remote-games rendering as the Lichess tab
  $("history-status").textContent = games.length ? "" : "No games found.";
  if (games.length >= chesscomCount) {
    const li = document.createElement("li");
    li.className = "load-more";
    li.textContent = "Load more";
    li.addEventListener("click", () => {
      chesscomCount += LICHESS_PAGE;
      loadChesscom(chesscomUser);
    });
    $("history-list").appendChild(li);
  }
}

const resultClass = (r) => (r === "win" ? "win" : r === "loss" ? "loss" : r === "draw" ? "draw" : "");
const resultWord = (r) => ({ win: "Won", loss: "Lost", draw: "Drew" }[r] || "");

function renderHistory(games, mode, who) {
  const ol = $("history-list");
  ol.innerHTML = "";
  for (const g of games) {
    const li = document.createElement("li");
    let side, title, sub, blunders, disabled, cls;
    if (mode === "normal") {
      side = g.reviewed_side;
      const opp = side === "white" ? g.black : g.white;
      cls = resultClass(g.player_result);
      const acc = g.accuracy != null ? `${g.accuracy}%` : "?";
      title = `${resultWord(g.player_result) || "vs"} ${opp || "?"}`;
      sub = `${g.date || ""} · ${g.opening || "—"} · ${acc} · ${g.speed}`;
      blunders = (g.counts && g.counts.blunder) || 0;
      disabled = !g.has_pgn;
      // Tint games recorded under the configured user ("you") so they stand out from games
      // analysed for someone else (e.g. an opponent-side review, or another account's PGN).
      // The backend computes `is_me` against the CURRENT identity (so a chess.com game recorded
      // before that handle was added to "me" still tints); fall back to the frozen id match.
      if (g.is_me || (myPlayerId && g.player_id && g.player_id === myPlayerId)) cls += " mine";
    } else {
      const w = (g.white || "").toLowerCase();
      const b = (g.black || "").toLowerCase();
      side = who && w === who ? "white" : who && b === who ? "black" : "auto";
      cls = "";
      title = `${g.white || "?"} vs ${g.black || "?"}`;
      sub = `${g.date || ""} · ${g.opening || "—"} · ${g.speed} · ${g.result || ""}`;
      blunders = null;
      disabled = !g.pgn;
    }
    li.className = cls + (disabled ? " disabled" : "");
    li.innerHTML =
      `<div class="h-title"><span>${escapeHtml(title)}</span><span class="h-actions">` +
      (blunders ? `<span class="h-blunders">●${blunders}</span>` : "") +
      (mode === "normal" ? `<button type="button" class="h-delete" title="Delete this local game">Delete</button>` : "") +
      `</span>` +
      `</div><div class="h-sub">${escapeHtml(sub)}</div>`;
    const deleteButton = li.querySelector(".h-delete");
    if (deleteButton) {
      deleteButton.addEventListener("click", (event) => {
        event.stopPropagation();
        deleteHistoryGame(g);
      });
    }
    if (disabled) {
      li.title =
        mode === "normal"
          ? "Can't reopen — this game was analyzed before PGNs were stored. Re-analyze it from Lichess."
          : "No PGN available for this game.";
    } else {
      li.addEventListener("click", () => bridge.review.openGame(g.pgn, side, mode === "normal" ? g.game_id : null));
    }
    ol.appendChild(li);
  }
}

async function deleteHistoryGame(game) {
  const opponent = game.reviewed_side === "white" ? game.black : game.white;
  if (!window.confirm(`Delete the local game against ${opponent || "this opponent"}? Analysis, explanations, and linked training attempts will also be removed.`)) return;
  const status = $("history-status");
  status.textContent = "Deleting local game…";
  try {
    const data = await gamesApi.deleteGame(game.game_id);
    if (data.error) throw new Error(data.error || "Delete failed.");
    historyGames = historyGames.filter((item) => item.game_id !== game.game_id);
    renderHistoryPage();
    status.textContent = `Deleted game and ${data.attempts_removed || 0} linked training attempt(s).`;
    loadInsights();
  } catch (error) {
    status.textContent = error.message || "Could not delete the local game.";
  }
}

// Build a small "open this game on the source site" ↗ anchor (Lichess / Chess.com), or null if
// there's no usable URL. Opens in a new tab; stops click-propagation so it never triggers a
// surrounding row/handler.
function gameLink(url, className) {
  if (!url || !/^https?:\/\//i.test(url)) return null;
  let host = "the source site";
  try {
    host = new URL(url).hostname.replace(/^www\./, "");
  } catch (_) {}
  const a = document.createElement("a");
  a.className = className || "game-open";
  a.href = url;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = "↗";
  a.title = `Open this game on ${host}`;
  a.setAttribute("aria-label", `Open this game on ${host}`);
  a.addEventListener("click", (e) => e.stopPropagation());
  return a;
}

// Render the board-header line: "White vs Black" + an optional ↗ source-site link + a details tail.
// Built via DOM (not innerHTML) so the URL/names can't break out of an attribute or inject markup.
function setGameMeta(white, black, url, tail) {
  const gm = $("game-meta");
  if (!gm) return;
  gm.textContent = "";
  const names = document.createElement("span");
  names.textContent = `${white || "White"} vs ${black || "Black"}`;
  gm.appendChild(names);
  const link = gameLink(url, "meta-open");
  if (link) gm.appendChild(link);
  if (tail) gm.appendChild(document.createTextNode(tail));
}

// The original game's URL from PGN Site/Link headers (mirrors history.game_url_from_headers).
function gameUrlFromHeaders(h) {
  for (const k of ["Site", "Link"]) {
    const v = (h && h[k] != null ? String(h[k]) : "").trim();
    if (/^https?:\/\//i.test(v)) return v;
  }
  return null;
}

// Parse just the White/Black/URL out of a PGN's headers (chess.js), best-effort.
function pgnHeaders(pgn) {
  try {
    const c = board.createGame();
    c.loadPgn(pgn);
    const h = c.header();
    return { white: h.White, black: h.Black, url: gameUrlFromHeaders(h) };
  } catch (_) {
    return {};
  }
}

// --- insights panel --------------------------------------------------------
// Cross-game themes + stats for the configured user over a chosen time window, aggregated
// server-side from the analyzed-games history (GET /api/insights). Refreshed whenever the
// history list reloads (i.e. as new games are analyzed) and when the period changes.
let insightsDays = 30;

async function loadInsights() {
  const body = $("insights-body");
  if (!body) return;
  let data;
  try {
    data = await gamesApi.profile(insightsDays);
  } catch (_) {
    body.innerHTML = `<p class="muted">Could not load insights.</p>`;
    return;
  }
  renderInsights(data);
}

function renderInsights(d) {
  const body = $("insights-body");
  if (!d || d.error) {
    body.innerHTML = `<p class="muted">${escapeHtml((d && d.error) || "No data.")}</p>`;
    return;
  }
  if (!d.games) {
    const training = d.training || {};
    body.innerHTML = `<p class="muted">No analyzed games in this period yet.</p>` +
      (training.total
        ? `<h3>Training</h3><div class="training-stat"><strong>${training.solve_rate}%</strong>` +
          `<span>${training.solved} solved from ${training.total} recent attempt(s)</span></div>`
        : "");
    profileWeaknesses = [];
    return;
  }
  const r = d.results || {};
  const mt = d.mistake_totals || {};
  const parts = [];
  profileWeaknesses = d.weaknesses || [];
  parts.push(
    `<div class="ins-stat"><b>${d.games}</b> game${d.games === 1 ? "" : "s"} · ` +
      `${r.win || 0}W–${r.loss || 0}L–${r.draw || 0}D` +
      (d.avg_accuracy != null ? ` · <b>${d.avg_accuracy}%</b> avg accuracy` : "") +
      `</div>`
  );
  parts.push(
    `<div class="ins-stat muted">${mt.blunder || 0} blunders · ${mt.mistake || 0} mistakes · ` +
      `${mt.inaccuracy || 0} inaccuracies</div>`
  );
  const coach = d.coach_summary || {};
  if (coach.headline) {
    parts.push(`<p class="coach-headline">${escapeHtml(coach.headline)}</p>`);
  }
  const phases = d.phase_error_counts || {};
  const phaseTotal = (phases.opening || 0) + (phases.middlegame || 0) + (phases.endgame || 0);
  if (phaseTotal) {
    parts.push(
      `<h3>Losses by phase</h3><div class="phase-split">` +
      ["opening", "middlegame", "endgame"].map((phase) =>
        `<span><b>${phases[phase] || 0}</b>${escapeHtml(phase)}</span>`
      ).join("") + `</div>`
    );
  }
  if (profileWeaknesses.length) {
    parts.push(
      `<h3>Established weaknesses</h3><ul class="ins-list weakness-list">` +
      profileWeaknesses.map((item, index) => {
        const typical = item.typical_position || {};
        return `<li><div class="weakness-copy"><b>${escapeHtml(categoryLabel(item.category))}</b>` +
          `<span class="muted">${item.count} times · ${Number(item.average_severity || 0).toFixed(1)}% avg loss · ${escapeHtml(item.primary_phase)}</span></div>` +
          `<div class="weakness-actions">` +
          (typical.game_id && typical.critical_id ? `<button type="button" data-profile-review="${index}">Review</button>` : "") +
          `<button type="button" data-profile-train="${index}">Train</button></div></li>`;
      }).join("") + `</ul>`
    );
  } else if (coach.headline) {
    parts.push(`<div class="profile-threshold muted">No repeated weakness has crossed the evidence threshold yet.</div>`);
  }
  const training = d.training || {};
  if (training.total) {
    parts.push(
      `<h3>Training</h3><div class="training-stat"><strong>${training.solve_rate}%</strong>` +
      `<span>${training.solved} solved from ${training.total} Retry / personal puzzle attempts</span></div>`
    );
  }
  if ((coach.checklist || []).length) {
    parts.push(`<h3>Next-game checklist</h3><ol class="coach-checklist">` +
      coach.checklist.map((item) => `<li>${escapeHtml(item)}</li>`).join("") + `</ol>`);
  }
  const ops = (d.openings || []).slice(0, 3);
  if (ops.length) {
    parts.push(
      `<h3>Most played openings</h3><ul class="ins-list">` +
        ops
          .map(
            (o) =>
              `<li>${escapeHtml(o.opening)} <span class="muted">×${o.games}` +
              (o.avg_accuracy != null ? ` · ${o.avg_accuracy}%` : "") +
              `</span></li>`
          )
          .join("") +
        `</ul>`
    );
  }
  body.innerHTML = parts.join("");
  body.querySelectorAll("[data-profile-review]").forEach((button) =>
    button.addEventListener("click", () => openProfilePosition(profileWeaknesses[Number(button.dataset.profileReview)]))
  );
  body.querySelectorAll("[data-profile-train]").forEach((button) =>
    button.addEventListener("click", () => trainProfileWeakness(profileWeaknesses[Number(button.dataset.profileTrain)]))
  );
}

async function openProfilePosition(weakness) {
  const target = weakness && weakness.typical_position;
  if (!target) return;
  const row = historyGames.find(
    (item) => item.game_id === target.game_id && item.reviewed_side === target.reviewed_side
  );
  if (!row || !row.pgn) {
    $("history-status").textContent = "The source PGN for that position is unavailable.";
    return;
  }
  bridge.review.setPendingCritical(target.critical_id);
  if (bridge.puzzles.active) await bridge.puzzles.setMode(false);
  bridge.review.openGame(row.pgn, target.reviewed_side, target.game_id);
}

async function trainProfileWeakness(weakness) {
  if (!weakness) return;
  const target = weakness.typical_position || {};
  await bridge.puzzles.train({
    category: weakness.category || "",
    gameId: target.game_id,
    criticalId: target.critical_id,
  });
}

async function clearEngineCache() {
  const status = $("data-action-status");
  status.textContent = "Clearing…";
  try {
    const data = await gamesApi.clearEngineCache();
    if (data.error) throw new Error(data.error || "Cache cleanup failed.");
    status.textContent = `${data.files_removed || 0} cached file(s) removed.`;
  } catch (error) {
    status.textContent = error.message || "Cache cleanup failed.";
  }
}

// Just the tab chrome (active button + which form/list is shown), no data fetch.
function activateTab(mode) {
  historyMode = mode;
  clearChesscomHint(); // leaving/returning to a tab resets the rapid-refresh detector
  $("mode-normal").classList.toggle("active", mode === "normal");
  $("mode-lichess").classList.toggle("active", mode === "lichess");
  $("mode-chesscom").classList.toggle("active", mode === "chesscom");
  $("mode-paste").classList.toggle("active", mode === "paste");
  $("lichess-form").style.display = mode === "lichess" ? "flex" : "none";
  $("chesscom-form").style.display = mode === "chesscom" ? "flex" : "none";
  $("paste-form").style.display = mode === "paste" ? "flex" : "none";
  $("history-list").style.display = mode === "paste" ? "none" : "";
  if (mode !== "paste") {
    $("history-status").classList.remove("import-error", "import-success", "import-action");
  }
}

// Update the "Set as my account" button to reflect whether `who` (lowercased) is already you.
function reflectSetAsMe(who) {
  const btn = $("set-as-me");
  if (!btn) return;
  const isMe = !!appUsername && appUsername.toLowerCase() === who && !!who;
  btn.disabled = isMe;
  btn.textContent = isMe ? "✓ This is your account" : "Set as my account";
}

function setMode(mode) {
  activateTab(mode);
  if (mode === "normal") {
    loadHistory();
  } else if (mode === "lichess") {
    lichessCount = LICHESS_PAGE; // fresh search starts at the first page
    loadLichess($("lichess-user").value.trim());
  } else if (mode === "chesscom") {
    chesscomCount = LICHESS_PAGE;
    loadChesscom($("chesscom-user").value.trim());
  } else {
    // paste: nothing to fetch; just a hint until they submit.
    updatePasteHint();
  }
}

// Count games in a PGN by its [Event headers (>=1: a header-less PGN is still one game).
const countGames = (pgn) => Math.max(1, (pgn.match(/^\s*\[Event\b/gm) || []).length);

function updatePasteHint() {
  if (historyMode !== "paste") return;
  const pgn = $("paste-pgn").value.trim();
  const status = $("history-status");
  status.classList.remove("import-error", "import-success", "import-action");
  if (!pgn) {
    status.textContent = "Paste or upload a PGN (one or many games).";
    return;
  }
  if (/^https?:\/\//i.test(pgn)) {
    status.classList.add("import-action");
    status.textContent = "Download or copy the game's PGN, then paste it here.";
    return;
  }
  const n = countGames(pgn);
  status.textContent =
    n > 1 ? `${n} games detected — all will be imported.` : "1 game ready to import.";
}

const apiErrorMessage = (value, fallback) => {
  if (value && typeof value === "object") return value.message || fallback;
  return value || fallback;
};

// Import first, then hand only the normalized artifact to the existing engine-analysis flow.
async function startPasteAnalysis(pgn, side, username, sourceType = pasteSourceType) {
  $("firstrun").hidden = true; // in case the first-run prompt was still up
  const status = $("history-status");
  const submit = $("paste-submit");
  status.classList.remove("import-error", "import-success", "import-action");
  if (/^https?:\/\//i.test(pgn.trim())) {
    status.classList.add("import-action");
    status.textContent = "Single-game URLs aren't supported. Download or copy the PGN first.";
    return;
  }

  status.textContent = "Importing PGN…";
  bridge.review.setWorkflowState("importing", "Importing PGN", "Normalizing headers and replaying legal moves.");
  submit.disabled = true;
  let data;
  try {
    data = await gamesApi.importPgn({
      pgn,
      source_type: sourceType || "pgn_text",
      review_side: side || "auto",
      username: username || "",
    });
  } catch (error) {
    data = error && error.payload;
    if (data) {
      // Validation errors retain their structured payload for the actionable message below.
    } else {
    status.classList.add("import-error");
    status.textContent = "Could not reach the import service.";
    bridge.review.setWorkflowState("failed", "Import failed", "Could not reach the local import service.");
    submit.disabled = false;
    return;
    }
  }
  submit.disabled = false;
  if (data.error) {
    const urlHint = data.error && data.error.code === "url_not_supported";
    status.classList.add(urlHint ? "import-action" : "import-error");
    status.textContent = apiErrorMessage(data.error, "PGN import failed.");
    bridge.review.setWorkflowState("failed", "Import failed", status.textContent);
    return;
  }

  const imported = data.games || (data.game_id ? [data] : []);
  if (!imported.length) {
    status.classList.add("import-error");
    status.textContent = "The importer returned no games.";
    bridge.review.setWorkflowState("failed", "Import failed", status.textContent);
    return;
  }
  const unresolved = imported.filter((game) => !game.review_side);
  if (unresolved.length && side === "auto") {
    status.classList.add("import-action");
    status.textContent =
      imported.length === 1
        ? "Imported. Choose White or Black, then continue."
        : "Imported. Enter your username or choose a review side, then continue.";
    $("paste-side").focus();
    bridge.review.setWorkflowState("ready_to_analyze", "Game imported", "Choose a review side to continue.");
    return;
  }

  const normalized = imported.map((game) => game.pgn).join("\n");
  if (imported.length > 1) {
    status.classList.add("import-success");
    status.textContent = `Imported ${imported.length} games. Starting analysis…`;
    bridge.review.openBatch(normalized, side, username);
    return;
  }

  const game = imported[0];
  const selected = game.review_side || side;
  bridge.review.setWorkflowState("ready_to_analyze", "Game ready to analyze", `Reviewing ${selected}.`);
  status.classList.add("import-success");
  status.textContent = game.analysis_cached
    ? "Imported. Cached analysis found — opening it now."
    : game.already_imported
    ? "Game already imported. Starting analysis…"
    : "Imported and ready. Starting analysis…";
  bridge.review.openGame(game.pgn, selected, game.game_id);
}

// Read a dropped/picked .pgn file, mirror it into the Paste textarea (so the user sees what loaded
// and can still pick a side), and — when `analyze` — start the sweep straight away. Dropping a file
// anywhere on the Games panel uses analyze=true so people don't have to click Upload then Analyze.
function loadPgnFile(file, analyze) {
  if (!file) return;
  pasteSourceType = "pgn_file";
  const reader = new FileReader();
  reader.onload = () => {
    setMode("paste"); // reveal the Paste panel so the loaded PGN is visible/editable
    $("paste-pgn").value = reader.result || "";
    updatePasteHint();
    if (!analyze) return;
    const pgn = ($("paste-pgn").value || "").trim();
    if (!pgn) {
      $("history-status").textContent = "That file had no PGN text.";
      return;
    }
    startPasteAnalysis(
      pgn,
      $("paste-side").value || "auto",
      ($("paste-username").value || "").trim(),
      "pgn_file"
    );
  };
  reader.onerror = () => {
    $("history-status").textContent = "Could not read that file.";
  };
  reader.readAsText(file);
}

// True when a drag carries files (vs. selected text), so we only hijack file drags.
function dragHasFiles(e) {
  return !!e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
}

// First dropped file that looks like a PGN (by extension — many sources set no MIME type).
function firstPgnFile(dataTransfer) {
  const files = dataTransfer && dataTransfer.files ? Array.from(dataTransfer.files) : [];
  return files.find((f) => /\.(pgn|txt)$/i.test(f.name || "")) || null;
}

// Drag-and-drop a .pgn onto the Games panel (any tab) to load + analyze it. dragenter/leave use a
// depth counter so the highlight doesn't flicker as the cursor crosses child elements.
function initPgnDrop() {
  const col = $("history-col");
  if (!col) return;
  let depth = 0;
  const clear = () => {
    depth = 0;
    col.classList.remove("drag-over");
  };
  col.addEventListener("dragenter", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth++;
    col.classList.add("drag-over");
  });
  col.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  col.addEventListener("dragleave", (e) => {
    if (!dragHasFiles(e)) return;
    if (--depth <= 0) clear();
  });
  col.addEventListener("drop", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    clear();
    const file = firstPgnFile(e.dataTransfer);
    if (!file) {
      setMode("paste");
      $("history-status").textContent = "Drop a .pgn file to analyze it.";
      return;
    }
    loadPgnFile(file, true);
  });
  // Stop the browser from navigating away if a file is dropped outside the panel (only intercept
  // file drags, so dragging selected text into inputs/textarea still works normally).
  ["dragover", "drop"].forEach((ev) =>
    document.addEventListener(ev, (e) => {
      if (dragHasFiles(e)) e.preventDefault();
    })
  );
}

// Below this width the Games panel is an off-canvas drawer (see styles.css) rather than a third
// column, so it must start closed and auto-close when a game is opened to reveal the board.

  function mount() {
    $("history-toggle").addEventListener("click", bridge.toggleHistory);
    $("history-collapse").addEventListener("click", bridge.toggleHistory);
    $("mode-normal").addEventListener("click", () => setMode("normal"));
    $("mode-lichess").addEventListener("click", () => setMode("lichess"));
    $("mode-chesscom").addEventListener("click", () => setMode("chesscom"));
    $("mode-paste").addEventListener("click", () => setMode("paste"));
    $("paste-upload").addEventListener("click", () => $("paste-file").click());
    $("paste-file").addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      pasteSourceType = "pgn_file";
      loadPgnFile(file, false);
      event.target.value = "";
    });
    initPgnDrop();
    $("paste-pgn").addEventListener("input", () => {
      pasteSourceType = "pgn_text";
      updatePasteHint();
    });
    $("paste-form").addEventListener("submit", (event) => {
      event.preventDefault();
      const pgn = $("paste-pgn").value.trim();
      if (!pgn) {
        $("history-status").textContent = "Paste or upload a PGN first.";
        return;
      }
      startPasteAnalysis(
        pgn,
        $("paste-side").value || "auto",
        ($("paste-username").value || "").trim(),
        pasteSourceType
      );
    });
    $("lichess-form").addEventListener("submit", (event) => {
      event.preventDefault();
      lichessCount = LICHESS_PAGE;
      loadLichess($("lichess-user").value.trim());
    });
    $("chesscom-form").addEventListener("submit", (event) => {
      event.preventDefault();
      noteChesscomRefresh();
      chesscomCount = LICHESS_PAGE;
      loadChesscom($("chesscom-user").value.trim());
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
      const username = ($("lichess-user").value.trim() || lichessUser || "").trim();
      if (!username) return;
      await saveUsername(username);
      reflectSetAsMe(username.toLowerCase());
      loadHistory(undefined, { resetPaging: true });
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
    $("insights-period").addEventListener("change", (event) => {
      insightsDays = Number(event.target.value) || 0;
      loadInsights();
    });
    $("clear-engine-cache").addEventListener("click", clearEngineCache);
  }

  function setConfig(config = {}) {
    appMode = !!config.app_mode;
    appUsername = (config.lichess_username || "").trim();
    chesscomUsername = (config.chesscom_username || "").trim();
    chesscomSync = config.chesscom_sync !== false;
    chesscomSyncMax = Number(config.chesscom_sync_max) || 5;
    if (appUsername) $("lichess-user").placeholder = appUsername;
    if (chesscomUsername) $("chesscom-user").placeholder = chesscomUsername;
  }

  function applySavedSettings(settings = {}) {
    appUsername = settings.username || "";
    chesscomUsername = settings.chesscom_username || "";
    chesscomSync = settings.chesscom_sync !== false;
    chesscomSyncMax = Number(settings.chesscom_sync_max) || 5;
    if (appUsername) $("lichess-user").placeholder = appUsername;
    if (historyMode === "normal") loadHistory();
  }

  return {
    mount,
    loadHistory,
    maybeAutoload,
    setConfig,
    applySavedSettings,
    isLocalHistory: () => historyMode === "normal",
    activateLocal() { activateTab("normal"); },
    get appMode() { return appMode; },
  };
}
