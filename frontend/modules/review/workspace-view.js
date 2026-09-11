import { escapeHtml } from "../core/dom.js";
import { categoryLabel } from "../core/format.js";
import {
  criticalSwingLabel,
  reviewMoveLabel,
  scoreLabel,
} from "./helpers.js";

export function createWorkspaceView({
  $,
  setWorkflowState,
  getSnapshot,
  onSelectCritical,
  onSelectEngineMove,
  wireVariationLinks,
}) {
  let view = "key";

  function explanationFor(snapshot, criticalId) {
    return ((snapshot.explanationArtifact && snapshot.explanationArtifact.positions) || []).find(
      (item) => item.critical_id === criticalId
    ) || null;
  }

  function mistakeItems(snapshot) {
    if (snapshot.engineReview && snapshot.engineReview.moves) {
      return snapshot.engineReview.moves.filter(
        (move) =>
          move.side === snapshot.player &&
          ["inaccuracy", "mistake", "blunder"].includes(move.classification)
      );
    }
    return snapshot.mistakes;
  }

  function setView(nextView) {
    view = ["key", "mistakes", "all"].includes(nextView) ? nextView : "key";
    document.querySelectorAll(".review-tabs button").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    });
    $("movelist-panel").hidden = view !== "all";
    $("review-position-list").hidden = view === "all";
    renderList();
  }

  function renderList() {
    const snapshot = getSnapshot();
    const list = $("review-position-list");
    if (!list) return;
    list.innerHTML = "";
    if (view === "all") return;
    const items = view === "key" ? snapshot.criticalPositions : mistakeItems(snapshot);
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "position-meta";
      empty.textContent = snapshot.analyzing
        ? "Key positions will appear after analysis."
        : "No positions in this view.";
      list.appendChild(empty);
      return;
    }
    for (const item of items) {
      const critical = item.critical_id
        ? item
        : snapshot.criticalPositions.find((entry) => Number(entry.ply) === Number(item.ply));
      const button = document.createElement("button");
      button.type = "button";
      button.className = "position-item";
      button.dataset.ply = String(item.ply);
      if (critical) button.dataset.criticalId = critical.critical_id;
      const facts = critical && critical.facts;
      const category = facts && facts.primary_category
        ? categoryLabel(facts.primary_category)
        : "Engine review";
      const swing = critical
        ? criticalSwingLabel(critical)
        : `−${Number(item.win_percent_loss ?? item.win_swing ?? 0).toFixed(1)}%`;
      button.innerHTML =
        `<span class="position-dot ${escapeHtml(item.classification || "")}"></span>` +
        `<span class="position-copy"><span class="position-move">${escapeHtml(reviewMoveLabel(item))}</span>` +
        `<span class="position-meta">${escapeHtml(item.classification || "move")} · ${escapeHtml(swing)}<br>${escapeHtml(category)}</span></span>`;
      button.classList.toggle(
        "active",
        critical
          ? critical.critical_id === snapshot.activeCriticalId
          : Number(item.ply) === snapshot.reviewedMoveNode + 1
      );
      button.addEventListener("click", () => {
        if (critical) onSelectCritical(critical.critical_id);
        else onSelectEngineMove(Number(item.ply));
      });
      list.appendChild(button);
    }
  }

  function variationBlockHtml(line, kind, name, summary) {
    const sans = (line && line.san) || [];
    const moves = sans.length
      ? `<span class="san-line">${sans
          .map(
            (san, index) =>
              `<button type="button" class="san-move" data-variation="${kind}" data-step="${index + 1}">${escapeHtml(san)}</button>`
          )
          .join("")}</span>`
      : `<span class="muted">No legal variation stored.</span>`;
    return (
      `<section class="explanation-section"><h3>${escapeHtml(name)}</h3>` +
      `<div class="variation-row"><button type="button" class="san-move" data-play-line="${kind}" title="Play ${escapeHtml(name)}" aria-label="Play ${escapeHtml(name)}">▶</button>` +
      `${moves}</div>` +
      (summary ? `<div class="variation-summary">${escapeHtml(summary)}</div>` : "") +
      `</section>`
    );
  }

  function factsProblemHtml(critical) {
    const motifs = (critical.facts && critical.facts.motifs) || [];
    if (motifs.length) {
      return motifs
        .flatMap((motif) => motif.evidence || [])
        .slice(0, 3)
        .map((text) => escapeHtml(text))
        .join(" ");
    }
    return `The move was classified ${escapeHtml(critical.classification || "critical")} and lost ${Number(critical.win_loss || 0).toFixed(1)} percentage points of win chance.`;
  }

  function factReasons(critical) {
    const facts = critical.facts || {};
    const effects = (((facts.move_effects || {}).best || {}).effects || []).map(categoryLabel);
    const delta = ((facts.deltas || {}).material_delta || {}).best_minus_played;
    const reasons = effects.slice(0, 4);
    if (delta != null && Number(delta) !== 0) {
      reasons.push(`Material outcome improves by ${Number(delta)} point(s) in the stored lines.`);
    }
    return reasons.length
      ? reasons
      : ["It preserves the best Engine evaluation in the supplied legal line."];
  }

  function renderCursor() {
    const snapshot = getSnapshot();
    if (!snapshot.engineReview || !snapshot.engineReview.moves || !snapshot.timeline.length) return;
    const move = snapshot.engineReview.moves.find(
      (item) => Number(item.ply) === snapshot.reviewedMoveNode + 1
    );
    if (!move) return;
    $("review-empty").hidden = true;
    $("critical-review").hidden = false;
    $("critical-class").className = `review-class ${move.classification || ""}`;
    $("critical-class").textContent = move.classification || "move";
    $("critical-swing").textContent = `−${Number(move.win_percent_loss || 0).toFixed(1)}% win chance`;
    $("critical-title").textContent = reviewMoveLabel(move);
    $("critical-count").textContent = "All moves";
    $("critical-prev").disabled = true;
    $("critical-next").disabled = true;
    const best = move.best_move || {};
    $("explanation-content").innerHTML =
      `<section class="explanation-section"><h3>You played</h3><p><strong>${escapeHtml(reviewMoveLabel(move))}</strong></p></section>` +
      `<section class="explanation-section"><h3>Engine result</h3><div class="engine-fallback">` +
      `White eval ${escapeHtml(scoreLabel(move.eval_before))} → ${escapeHtml(scoreLabel(move.eval_after))}; ` +
      `the move lost ${Number(move.win_percent_loss || 0).toFixed(1)} percentage points for the mover.</div></section>` +
      `<section class="explanation-section"><h3>Better move</h3><p><strong>${escapeHtml(best.san || "—")}</strong></p></section>` +
      variationBlockHtml(move.best_pv || { uci: [], san: [] }, "best", "Best line", "");
    $("explanation-action").hidden = true;
    $("variation-controls").hidden = true;
  }

  function renderCritical(critical) {
    if (!critical) return;
    const snapshot = getSnapshot();
    const index = snapshot.criticalPositions.indexOf(critical);
    const explanation = explanationFor(snapshot, critical.critical_id);
    const bestMove = ((critical.best_line || {}).san || ["—"])[0];
    const categories = [
      critical.facts && critical.facts.primary_category,
      ...((critical.facts && critical.facts.secondary_categories) || []),
    ].filter(Boolean);
    $("review-empty").hidden = true;
    $("critical-review").hidden = false;
    $("critical-class").className = `review-class ${critical.classification || ""}`;
    $("critical-class").textContent = critical.classification || "critical";
    $("critical-swing").textContent = criticalSwingLabel(critical);
    $("critical-title").textContent = reviewMoveLabel(critical);
    $("critical-count").textContent = `${index + 1} / ${snapshot.criticalPositions.length}`;
    $("critical-prev").disabled = index <= 0;
    $("critical-next").disabled = index < 0 || index >= snapshot.criticalPositions.length - 1;

    let html = "";
    if (explanation) {
      html += `<section class="explanation-section"><h3>You played</h3><p><strong>${escapeHtml(explanation.played_move)}</strong></p></section>`;
      html += `<section class="explanation-section"><h3>Why it looked reasonable</h3><p>${escapeHtml(explanation.why_it_looked_reasonable)}</p></section>`;
      html += `<section class="explanation-section"><h3>Core problem</h3><p>${escapeHtml(explanation.core_problem)}</p></section>`;
      html += `<section class="explanation-section"><h3>Engine recommendation</h3><p><strong>${escapeHtml(explanation.recommended_move)}</strong></p></section>`;
      html += `<section class="explanation-section"><h3>Why it works</h3><ul class="explanation-list">${explanation.why_recommended.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
      html += variationBlockHtml(critical.played_line, "played", "Played line", explanation.played_line_summary);
      html += variationBlockHtml(critical.best_line, "best", "Best line", explanation.best_line_summary);
      html += `<section class="explanation-section"><h3>Error category</h3><div class="category-row">${[explanation.primary_category, ...explanation.secondary_categories].filter(Boolean).map((item) => `<span class="category-chip">${escapeHtml(categoryLabel(item))}</span>`).join("") || '<span class="muted">Uncategorized</span>'}</div></section>`;
      html += `<section class="explanation-section"><h3>Transferable principle</h3><p>${escapeHtml(explanation.transferable_principle)}</p></section>`;
      html += `<section class="explanation-section"><h3>Next-time checklist</h3><ul class="explanation-list">${explanation.next_time_checklist.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
    } else {
      html += `<div class="engine-fallback"><strong>Engine review is ready.</strong> AI explanation is optional; every statement below comes from the stored analysis and deterministic facts.</div>`;
      html += `<section class="explanation-section"><h3>You played</h3><p><strong>${escapeHtml(reviewMoveLabel(critical))}</strong></p></section>`;
      html += `<section class="explanation-section"><h3>Core problem</h3><p>${factsProblemHtml(critical)}</p></section>`;
      html += `<section class="explanation-section"><h3>Engine recommendation</h3><p><strong>${escapeHtml(bestMove)}</strong> · ${escapeHtml(critical.criticality || "critical")}</p></section>`;
      html += `<section class="explanation-section"><h3>Why it works</h3><ul class="explanation-list">${factReasons(critical).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section>`;
      html += variationBlockHtml(critical.played_line, "played", "Played line", "");
      html += variationBlockHtml(critical.best_line, "best", "Best line", "");
      html += `<section class="explanation-section"><h3>Error category</h3><div class="category-row">${categories.map((item) => `<span class="category-chip">${escapeHtml(categoryLabel(item))}</span>`).join("") || '<span class="muted">No deterministic motif was assigned.</span>'}</div></section>`;
    }
    $("explanation-content").innerHTML = html;
    $("explanation-action").hidden = false;
    $("generate-explanation").textContent = explanation
      ? "Regenerate this explanation"
      : "Generate AI explanations";
    $("explanation-status").textContent = explanation
      ? `${snapshot.explanationArtifact.provider} · ${snapshot.explanationArtifact.model} · ${snapshot.explanationArtifact.language}`
      : "Engine review remains available if generation fails.";
    wireVariationLinks();
  }

  function renderFreeAnalysis(line, ply, details = {}) {
    $("review-empty").hidden = false;
    $("review-empty-title").textContent = "Free analysis";
    $("review-empty-detail").textContent = "Temporary · not saved";
    $("free-analysis-line").textContent = line;
    $("timeline-readout").textContent = ply ? `Ply ${ply}` : "Start position";

    const sideToMove = details.sideToMove === "black" ? "Black" : "White";
    setWorkflowState("free_analysis", "Free analysis", `Temporary position · ${sideToMove} to move`);

    const verdict = details.verdict;
    const mover = details.moverColor === "black" ? "Black" : "White";
    const moveNumber = Math.max(1, Math.ceil(Number(ply || 1) / 2));
    const movePrefix = mover === "White" ? `${moveNumber}.` : `${moveNumber}...`;
    const moveSan = details.moveSan ? `${movePrefix}${escapeHtml(details.moveSan)}` : "";
    if (!verdict || !moveSan) return;

    const analysis = $("workflow-analysis");
    analysis.hidden = false;
    if (verdict === "pending" || verdict.error) {
      const result = verdict === "pending" ? "Evaluating…" : "Couldn't evaluate that move.";
      analysis.innerHTML =
        `<span class="workflow-move"><b>${mover} played ${moveSan}</b></span>` +
        `<span class="workflow-eval">${result}</span>`;
      return;
    }

    const label = verdict.classification === "best" && !verdict.is_engine_best
      ? "good"
      : verdict.classification;
    const toWhiteWinChance = (value) => {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return value;
      return mover === "White" ? numeric : Math.round((100 - numeric) * 10) / 10;
    };
    const whiteBefore = toWhiteWinChance(verdict.win_before);
    const whiteAfter = toWhiteWinChance(verdict.win_after);
    const bestMove = verdict.is_engine_best || !verdict.better_move_san
      ? "Engine's top choice"
      : `Best was <b>${movePrefix}${escapeHtml(verdict.better_move_san)}</b>`;
    analysis.innerHTML =
      `<span class="workflow-move"><span class="tag ${label}">${label}</span>` +
        `<b>${mover} played ${moveSan}</b></span>` +
      `<span class="workflow-eval">White win chance ${whiteBefore}% → ${whiteAfter}% · ${bestMove}</span>`;
  }

  return {
    setView,
    refreshView() { setView(view); },
    renderList,
    renderCursor,
    renderCritical,
    renderFreeAnalysis,
  };
}
