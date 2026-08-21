import { puzzleApi } from "../api/puzzles.js";
import { createLatestRequestScope } from "../core/async.js";
import { escapeHtml } from "../core/dom.js";
import {
  storageGet,
  storageJsonGet,
  storageJsonSet,
  storageSet,
} from "../core/storage.js";

export function createPuzzleProgress({ $, getConfig }) {
  const requests = createLatestRequestScope();
  let results = [];
  let streak = 0;
  let dailyStreak = 0;
  let bestDaily = 0;
  let curveExpanded = false;

  function loadSession() {
    results = storageJsonGet(sessionStorage, "pzResults", []);
    if (!Array.isArray(results)) results = [];
    renderPips();
  }

  function updateStats(rating, nextStreak) {
    if (rating != null) $("pz-rating").textContent = `Puzzle · ${rating}`;
    if (nextStreak != null) streak = nextStreak;
    renderPips();
  }

  function record(ok) {
    results.push(!!ok);
    if (results.length > 20) results = results.slice(-20);
    storageJsonSet(sessionStorage, "pzResults", results);
    renderPips();
  }

  function renderPips() {
    const element = $("pz-streak");
    if (!element) return;
    let html = results.slice(-5)
      .map((ok) => `<span class="pz-pip ${ok ? "ok" : "bad"}"></span>`)
      .join("");
    if (streak) html += `<span class="pz-streak-n">streak ${streak}</span>`;
    element.innerHTML = html;
  }

  function renderDaily(nextStreak, nextBest) {
    if (nextStreak != null) dailyStreak = nextStreak;
    if (nextBest != null) bestDaily = nextBest;
    const element = $("pz-daily");
    if (!element) return;
    if (dailyStreak < 1) {
      element.hidden = true;
      element.textContent = "";
      return;
    }
    element.classList.toggle("hot", dailyStreak >= 2);
    element.innerHTML = `🔥 <b>${dailyStreak}</b> day${dailyStreak === 1 ? "" : "s"}`;
    element.title =
      `Practised ${dailyStreak} day${dailyStreak === 1 ? "" : "s"} in a row` +
      (bestDaily > dailyStreak ? ` · best ${bestDaily}` : "");
    element.hidden = false;
  }

  function renderCurve(history) {
    const element = $("pz-progress");
    if (!element) return;
    const points = (history || [])
      .filter((item) => item && item.rated && typeof item.rating_after === "number")
      .map((item) => item.rating_after)
      .slice(-24);
    if (points.length < 2) {
      element.hidden = true;
      element.innerHTML = "";
      return;
    }
    const current = Math.round(points.at(-1));
    const minimum = Math.min(...points);
    const span = Math.max(1, Math.max(...points) - minimum);
    const net = Math.round(points.at(-1) - points[0]);
    const netClass = net > 0 ? "up" : net < 0 ? "down" : "flat";
    const width = 260;
    const height = 48;
    const padding = 4;
    const coordinates = points.map((rating, index) => {
      const x = (index / (points.length - 1)) * width;
      const y = height - padding - ((rating - minimum) / span) * (height - 2 * padding);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const line = coordinates.map((point, index) => `${index ? "L" : "M"}${point}`).join(" ");
    const area = `M0,${height} L${coordinates.join(" L")} L${width},${height} Z`;
    element.classList.toggle("open", curveExpanded);
    element.innerHTML =
      `<button type="button" class="pz-progress-head" aria-expanded="${curveExpanded}">` +
      `<span class="pz-progress-title">Rating <b>${current}</b></span>` +
      `<span class="pz-progress-caret">▸</span></button>` +
      `<div class="pz-progress-body"><div class="pz-progress-sub">` +
      `<span class="pz-progress-net ${netClass}">${net > 0 ? "+" : ""}${net} · last ${points.length}</span>` +
      `</div><svg class="pz-curve" viewBox="0 0 ${width} ${height}" ` +
      `preserveAspectRatio="none" aria-hidden="true">` +
      `<rect width="${width}" height="${height}" fill="#14130f"/>` +
      `<path d="${area}" fill="rgba(236,234,228,0.12)"/>` +
      `<path d="${line}" fill="none" stroke="#e8e6e3" stroke-width="1.5" ` +
      `vector-effect="non-scaling-stroke"/></svg></div>`;
    element.hidden = false;
    const head = element.querySelector(".pz-progress-head");
    head.onclick = () => {
      curveExpanded = !curveExpanded;
      element.classList.toggle("open", curveExpanded);
      head.setAttribute("aria-expanded", String(curveExpanded));
    };
  }

  async function loadCard() {
    const card = $("pz-statcard");
    if (!card) return;
    const request = requests.begin();
    let state;
    try {
      state = await puzzleApi.state({ signal: request.signal });
    } catch (_) {
      return;
    }
    if (!request.isCurrent()) return;
    renderDaily(state && state.daily_streak, state && state.best_daily_streak);
    renderCurve(state && state.history);
    const weakThemes = (state && state.weak_themes) || [];
    if (!weakThemes.length) {
      card.hidden = true;
      card.innerHTML = "";
      return;
    }
    const open = storageGet(localStorage, "pzWorkOnOpen") === "1";
    card.innerHTML =
      `<button type="button" class="pz-statcard-toggle" aria-expanded="${open}">` +
      `Work on <span class="pz-statcard-caret">${open ? "▾" : "▸"}</span></button>` +
      `<div class="pz-statcard-body"${open ? "" : " hidden"}>` +
      weakThemes.map((item) =>
        `<span class="pz-weak-theme">${escapeHtml(item.theme)} ` +
        `<b>${Math.round(item.rate * 100)}%</b></span>`
      ).join("") + `</div>`;
    card.hidden = false;
    card.querySelector(".pz-statcard-toggle").addEventListener("click", () => {
      const body = card.querySelector(".pz-statcard-body");
      const nowOpen = body.hidden;
      body.hidden = !nowOpen;
      card.querySelector(".pz-statcard-caret").textContent = nowOpen ? "▾" : "▸";
      card.querySelector(".pz-statcard-toggle").setAttribute("aria-expanded", String(nowOpen));
      storageSet(localStorage, "pzWorkOnOpen", nowOpen ? "1" : "0");
    });
  }

  function applyConfig() {
    const config = getConfig() || {};
    updateStats(config.your_rating, config.streak);
    renderDaily(config.daily_streak, config.best_daily_streak);
  }

  return {
    applyConfig,
    cancel: () => requests.cancel(),
    loadCard,
    loadSession,
    record,
    renderDaily,
    updateStats,
  };
}
