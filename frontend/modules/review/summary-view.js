import { escapeHtml } from "../core/dom.js";
import { pieceGlyph } from "./helpers.js";

export function createReviewSummaryView({ $, getSnapshot, onSelectMistake }) {
  function renderMistakes() {
    const { analyzing, mistakes } = getSnapshot();
    const list = $("mistakes");
    list.innerHTML = "";
    if (analyzing && !mistakes.length) {
      const item = document.createElement("li");
      item.className = "ph";
      item.textContent = "Analyzing… mistakes will appear here when the engine finishes.";
      list.appendChild(item);
      return;
    }
    mistakes.forEach((mistake, index) => {
      const item = document.createElement("li");
      item.dataset.index = index;
      const number = `${mistake.move_number}${mistake.color === "white" ? "." : "…"}`;
      item.innerHTML =
        `<span class="move"><span class="dot ${mistake.classification}"></span>` +
        `<span class="piece-glyph">${pieceGlyph(mistake.move_san)}</span>${number} ${mistake.move_san}</span>` +
        `<span class="muted">${mistake.classification} −${mistake.win_swing}</span>`;
      item.addEventListener("click", () => onSelectMistake(index));
      list.appendChild(item);
    });
  }

  function scoreChip(classification, count, label) {
    return (
      `<button type="button" class="chip ${classification}" data-cls="${classification}" title="${label}">` +
      `<span class="chip-n">${count}</span> <span class="chip-lbl">${label}</span></button>`
    );
  }

  function renderScoreboard(session) {
    const scoreboard = $("scoreboard");
    if (!scoreboard) return;
    const reviewed = session.player === "black" ? "black" : "white";
    const sideLabel = reviewed === "white" ? "White" : "Black";
    const accuracy = reviewed === "white" ? session.accuracy_white : session.accuracy_black;
    const opponentAccuracy = reviewed === "white" ? session.accuracy_black : session.accuracy_white;
    const counts = { blunder: 0, mistake: 0, inaccuracy: 0 };
    (session.mistakes || []).forEach((mistake) => {
      if (counts[mistake.classification] != null) counts[mistake.classification] += 1;
    });
    scoreboard.innerHTML =
      `<div class="sb-opening" title="Opening">${escapeHtml(session.opening || "—")}</div>` +
      `<div class="sb-acc">` +
      `<span class="sb-acc-main"><b>${accuracy}</b><span class="sb-acc-lbl">accuracy (${sideLabel})</span></span>` +
      `<span class="sb-acc-opp">opponent ${opponentAccuracy}</span>` +
      `</div><div class="sb-counts">` +
      scoreChip("blunder", counts.blunder, "Blunders") +
      scoreChip("mistake", counts.mistake, "Mistakes") +
      scoreChip("inaccuracy", counts.inaccuracy, "Inaccuracies") +
      `</div>`;
    scoreboard.hidden = false;
    scoreboard.querySelectorAll(".chip[data-cls]").forEach((element) =>
      element.addEventListener("click", () => {
        const index = (session.mistakes || []).findIndex(
          (mistake) => mistake.classification === element.dataset.cls
        );
        if (index >= 0) onSelectMistake(index);
      })
    );
  }

  return { renderMistakes, renderScoreboard };
}
