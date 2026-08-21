import { classGlyph } from "./helpers.js";

export function createReviewNotation({ $, getSnapshot, onGotoNode, onSelectCritical, onSelectEngineMove, onSelectMistake }) {
  function render() {
    const list = $("movelist");
    if (!list) return;
    list.innerHTML = "";
    const plies = getSnapshot().timeline.filter((node) => node.move_san);
    if (!plies.length) return;
    const rows = new Map();
    for (const node of plies) {
      if (!rows.has(node.move_number)) rows.set(node.move_number, { white: null, black: null });
      rows.get(node.move_number)[node.color] = node;
    }
    for (const [moveNumber, pair] of rows) {
      const item = document.createElement("li");
      item.className = "move-row";
      item.innerHTML = `<span class="moveno">${moveNumber}.</span>${plyCell(pair.white)}${plyCell(pair.black)}`;
      list.appendChild(item);
    }
    list.querySelectorAll(".ply[data-node]").forEach((element) =>
      element.addEventListener("click", () => selectMove(Number(element.dataset.node)))
    );
    highlightCurrent();
  }

  function plyCell(node) {
    if (!node) return `<span class="ply empty"></span>`;
    return `<span class="ply" data-node="${node.node}">${classGlyph(node.classification)}${node.move_san}</span>`;
  }

  function selectMove(index) {
    const { timeline, criticalPositions, engineReview } = getSnapshot();
    const node = timeline[index];
    const critical = node && criticalPositions.find((item) => Number(item.ply) === Number(node.ply));
    if (critical) onSelectCritical(critical.critical_id);
    else if (node && engineReview) onSelectEngineMove(node.ply);
    else if (node && node.mistake_index != null) onSelectMistake(node.mistake_index);
    else onGotoNode(index + 1);
  }

  function highlightCurrent() {
    const list = $("movelist");
    if (!list) return;
    const reviewedNode = getSnapshot().reviewedMoveNode;
    let active = null;
    list.querySelectorAll(".ply[data-node]").forEach((element) => {
      const selected = Number(element.dataset.node) === reviewedNode;
      element.classList.toggle("active", selected);
      if (selected) active = element;
    });
    if (active) active.scrollIntoView({ block: "nearest" });
  }

  function toggleExpanded() {
    const list = $("movelist");
    if (!list) return;
    const expanded = list.classList.toggle("expanded");
    list.classList.toggle("compact", !expanded);
    $("movelist-expand").textContent = expanded ? "Collapse ▴" : "Show all ▾";
    highlightCurrent();
  }

  return { render, highlightCurrent, toggleExpanded };
}
