import { escapeHtml } from "../core/dom.js";
import { classColor } from "./helpers.js";

const GRAPH_WIDTH = 1000;
const GRAPH_HEIGHT = 100;

export function createReviewGraph({ $, getSnapshot, onGotoNode, onSelectCritical, onSelectMistake }) {
  function updateReadout(snapshot) {
    const output = $("timeline-readout");
    const node = snapshot.timeline[snapshot.cur];
    if (!output || !node) return;
    let score = null;
    if (snapshot.engineReview && snapshot.engineReview.moves) {
      const move = snapshot.engineReview.moves.find((item) => Number(item.ply) === snapshot.cur + 1);
      const previous = snapshot.engineReview.moves.find((item) => Number(item.ply) === snapshot.cur);
      score = (move && move.eval_before) || (previous && previous.eval_after) || null;
    }
    let scoreText = "";
    if (score && score.type === "mate") {
      const value = Number(score.value) || 0;
      scoreText = ` · ${value < 0 ? "-" : ""}M${Math.abs(value)}`;
    } else if (score && score.type === "cp") {
      const value = Number(score.value) / 100;
      scoreText = ` · ${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
    }
    output.textContent = `Ply ${snapshot.cur} · White ${
      node.win_white == null ? "—" : `${node.win_white}%`
    }${scoreText}`;
  }

  function render() {
    const snapshot = getSnapshot();
    const { timeline, cur, orient, engineReview, criticalPositions } = snapshot;
    const svg = $("graph");
    const count = timeline.length;
    updateReadout(snapshot);
    if (count < 2) {
      svg.innerHTML = "";
      return;
    }
    svg.setAttribute("viewBox", `0 0 ${GRAPH_WIDTH} ${GRAPH_HEIGHT}`);
    const x = (index) => (index / (count - 1)) * GRAPH_WIDTH;
    const y = (winPercent) => GRAPH_HEIGHT - (winPercent / 100) * GRAPH_HEIGHT;
    const hasEval = timeline.some((node) => node.win_white != null);
    const value = (node) => {
      const white = node.win_white == null ? 50 : node.win_white;
      return orient === "white" ? white : 100 - white;
    };
    const points = timeline
      .map((node, index) => `${x(index).toFixed(1)},${y(value(node)).toFixed(1)}`)
      .join(" L");
    const belowArea = `M0,${GRAPH_HEIGHT} L${points} L${GRAPH_WIDTH},${GRAPH_HEIGHT} Z`;
    const aboveArea = `M0,0 L${points} L${GRAPH_WIDTH},0 Z`;
    const light = "rgba(236,234,228,0.22)";
    const dark = "rgba(0,0,0,0.45)";
    const bottomFill = orient === "white" ? light : dark;
    const topFill = orient === "white" ? dark : light;
    const line = timeline
      .map((node, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(value(node)).toFixed(1)}`)
      .join(" ");
    const flagged = criticalPositions.length
      ? criticalPositions
          .map((critical) => ({ node: timeline[Number(critical.ply) - 1], critical }))
          .filter((item) => item.node)
      : timeline
          .filter((node) => node.mistake_index != null)
          .map((node) => ({ node, critical: null }));
    const mistakeDots = flagged
      .map(
        ({ node, critical }) =>
          `<circle cx="${x(node.node).toFixed(1)}" cy="${y(value(node)).toFixed(1)}" r="3.5" ` +
          `fill="${classColor((critical && critical.classification) || node.classification)}" vector-effect="non-scaling-stroke"/>`
      )
      .join("");
    const half = GRAPH_WIDTH / (count - 1) / 2;
    const mistakeHits = flagged
      .map(
        ({ node, critical }) =>
          `<rect class="mdot-hit" ${
            critical
              ? `data-critical="${escapeHtml(critical.critical_id)}"`
              : `data-mi="${node.mistake_index}"`
          } pointer-events="all" ` +
          `x="${(x(node.node) - half).toFixed(1)}" y="0" width="${(half * 2).toFixed(1)}" ` +
          `height="${GRAPH_HEIGHT}" fill="transparent"/>`
      )
      .join("");
    const mateLabels = (engineReview && engineReview.moves ? engineReview.moves : [])
      .filter((move) => move.eval_after && move.eval_after.type === "mate")
      .map((move) => {
        const node = timeline[Number(move.ply)];
        if (!node) return "";
        const raw = Number(move.eval_after.value) || 0;
        const label = `${raw < 0 ? "-" : ""}M${Math.abs(raw)}`;
        return `<text x="${x(node.node).toFixed(1)}" y="${Math.max(9, y(value(node)) - 6).toFixed(1)}" ` +
          `fill="#f3c9c9" font-size="8" text-anchor="middle" vector-effect="non-scaling-stroke">${label}</text>`;
      })
      .join("");
    const currentX = x(cur).toFixed(1);
    const currentY = y(value(timeline[cur])).toFixed(1);
    const marker =
      `<line x1="${currentX}" y1="0" x2="${currentX}" y2="${GRAPH_HEIGHT}" stroke="#629924" stroke-width="1" vector-effect="non-scaling-stroke"/>` +
      `<circle cx="${currentX}" cy="${currentY}" r="4" fill="#629924" vector-effect="non-scaling-stroke"/>`;
    const analyzingNote = hasEval
      ? ""
      : `<text x="${GRAPH_WIDTH / 2}" y="${GRAPH_HEIGHT / 2 - 4}" fill="#9c9890" font-size="9" text-anchor="middle" ` +
        `vector-effect="non-scaling-stroke">analyzing… moves are navigable now</text>`;

    svg.innerHTML =
      `<rect x="0" y="0" width="${GRAPH_WIDTH}" height="${GRAPH_HEIGHT}" fill="#14130f"/>` +
      `<path d="${aboveArea}" fill="${topFill}"/>` +
      `<path d="${belowArea}" fill="${bottomFill}"/>` +
      `<line x1="0" y1="${GRAPH_HEIGHT / 2}" x2="${GRAPH_WIDTH}" y2="${GRAPH_HEIGHT / 2}" stroke="#4a4843" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>` +
      (hasEval
        ? `<path d="${line}" fill="none" stroke="#e8e6e3" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`
        : "") +
      mistakeDots + mateLabels + marker + analyzingNote + mistakeHits;
  }

  function handleClick(event) {
    const criticalTarget = event.target?.closest?.("[data-critical]");
    if (criticalTarget) return onSelectCritical(criticalTarget.getAttribute("data-critical"));
    const mistakeTarget = event.target?.closest?.("[data-mi]");
    if (mistakeTarget) return onSelectMistake(Number(mistakeTarget.getAttribute("data-mi")));
    const { timeline } = getSnapshot();
    if (timeline.length < 2) return;
    const rect = $("graph").getBoundingClientRect();
    const fraction = (event.clientX - rect.left) / rect.width;
    onGotoNode(Math.round(fraction * (timeline.length - 1)));
  }

  return { render, handleClick };
}
