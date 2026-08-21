import { gamesApi } from "../api/games.js";
import { createLatestRequestScope } from "../core/async.js";
import { escapeHtml } from "../core/dom.js";
import { categoryLabel } from "../core/format.js";

export function createGamesInsights({ $, bridge, library }) {
  const requests = createLatestRequestScope();
  let days = 30;
  let weaknesses = [];

  async function load() {
    const body = $("insights-body");
    if (!body) return;
    const request = requests.begin();
    try {
      const data = await gamesApi.profile(days, { signal: request.signal });
      if (request.isCurrent()) render(data);
    } catch (_) {
      if (!request.isCurrent()) return;
      body.innerHTML = `<p class="muted">Could not load insights.</p>`;
    }
  }

  function render(data) {
    const body = $("insights-body");
    if (!data || data.error) {
      body.innerHTML = `<p class="muted">${escapeHtml((data && data.error) || "No data.")}</p>`;
      return;
    }
    const training = data.training || {};
    if (!data.games) {
      body.innerHTML = `<p class="muted">No analyzed games in this period yet.</p>` +
        (training.total
          ? `<h3>Training</h3><div class="training-stat"><strong>${training.solve_rate}%</strong>` +
            `<span>${training.solved} solved from ${training.total} recent attempt(s)</span></div>`
          : "");
      weaknesses = [];
      return;
    }

    const results = data.results || {};
    const totals = data.mistake_totals || {};
    const coach = data.coach_summary || {};
    const parts = [];
    weaknesses = data.weaknesses || [];
    parts.push(
      `<div class="ins-stat"><b>${data.games}</b> game${data.games === 1 ? "" : "s"} · ` +
      `${results.win || 0}W–${results.loss || 0}L–${results.draw || 0}D` +
      (data.avg_accuracy != null ? ` · <b>${data.avg_accuracy}%</b> avg accuracy` : "") +
      `</div>`
    );
    parts.push(
      `<div class="ins-stat muted">${totals.blunder || 0} blunders · ` +
      `${totals.mistake || 0} mistakes · ${totals.inaccuracy || 0} inaccuracies</div>`
    );
    if (coach.headline) parts.push(`<p class="coach-headline">${escapeHtml(coach.headline)}</p>`);
    const phases = data.phase_error_counts || {};
    if ((phases.opening || 0) + (phases.middlegame || 0) + (phases.endgame || 0)) {
      parts.push(
        `<h3>Losses by phase</h3><div class="phase-split">` +
        ["opening", "middlegame", "endgame"].map((phase) =>
          `<span><b>${phases[phase] || 0}</b>${escapeHtml(phase)}</span>`
        ).join("") + `</div>`
      );
    }
    if (weaknesses.length) {
      parts.push(
        `<h3>Established weaknesses</h3><ul class="ins-list weakness-list">` +
        weaknesses.map((item, index) => {
          const position = item.typical_position || {};
          return `<li><div class="weakness-copy"><b>${escapeHtml(categoryLabel(item.category))}</b>` +
            `<span class="muted">${item.count} times · ` +
            `${Number(item.average_severity || 0).toFixed(1)}% avg loss · ` +
            `${escapeHtml(item.primary_phase)}</span></div><div class="weakness-actions">` +
            (position.game_id && position.critical_id
              ? `<button type="button" data-profile-review="${index}">Review</button>`
              : "") +
            `<button type="button" data-profile-train="${index}">Train</button></div></li>`;
        }).join("") + `</ul>`
      );
    } else if (coach.headline) {
      parts.push(
        `<div class="profile-threshold muted">No repeated weakness has crossed the evidence threshold yet.</div>`
      );
    }
    if (training.total) {
      parts.push(
        `<h3>Training</h3><div class="training-stat"><strong>${training.solve_rate}%</strong>` +
        `<span>${training.solved} solved from ${training.total} Retry / personal puzzle attempts</span></div>`
      );
    }
    if ((coach.checklist || []).length) {
      parts.push(
        `<h3>Next-game checklist</h3><ol class="coach-checklist">` +
        coach.checklist.map((item) => `<li>${escapeHtml(item)}</li>`).join("") + `</ol>`
      );
    }
    const openings = (data.openings || []).slice(0, 3);
    if (openings.length) {
      parts.push(
        `<h3>Most played openings</h3><ul class="ins-list">` +
        openings.map((opening) =>
          `<li>${escapeHtml(opening.opening)} <span class="muted">×${opening.games}` +
          (opening.avg_accuracy != null ? ` · ${opening.avg_accuracy}%` : "") +
          `</span></li>`
        ).join("") + `</ul>`
      );
    }
    body.innerHTML = parts.join("");
    body.querySelectorAll("[data-profile-review]").forEach((button) =>
      button.addEventListener("click", () => review(weaknesses[Number(button.dataset.profileReview)]))
    );
    body.querySelectorAll("[data-profile-train]").forEach((button) =>
      button.addEventListener("click", () => train(weaknesses[Number(button.dataset.profileTrain)]))
    );
  }

  async function review(weakness) {
    const position = weakness && weakness.typical_position;
    if (!position) return;
    const game = library.findGame(position.game_id, position.reviewed_side);
    if (!game || !game.pgn) {
      $("history-status").textContent = "The source PGN for that position is unavailable.";
      return;
    }
    bridge.review.setPendingCritical(position.critical_id);
    if (bridge.puzzles.active) await bridge.puzzles.setMode(false);
    bridge.review.openGame(game.pgn, position.reviewed_side, position.game_id);
  }

  async function train(weakness) {
    if (!weakness) return;
    const position = weakness.typical_position || {};
    await bridge.puzzles.train({
      category: weakness.category || "",
      gameId: position.game_id,
      criticalId: position.critical_id,
    });
  }

  function mount() {
    $("insights-period").addEventListener("change", (event) => {
      days = Number(event.target.value) || 0;
      load();
    });
  }

  return { load, mount };
}
