import { escapeHtml } from "./dom.js";

export function categoryLabel(value) {
  const labels = {
    allowed_mate: "Allowed mate",
    missed_mate: "Missed mate",
    wrong_exchange_sequence: "Exchange sequence",
    missed_opponent_threat: "Opponent threat",
    hanging_piece: "Hanging piece",
    missed_capture: "Missed capture",
    fork: "Fork",
  };
  return labels[value] || String(value || "Uncategorized").replaceAll("_", " ");
}

export function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "";
  let inList = false;
  const inline = (value) =>
    value
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  for (const raw of lines) {
    const line = raw.trim();
    const item = line.match(/^[-*]\s+(.*)/);
    if (item) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${inline(item[1])}</li>`;
    } else {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      if (line) html += `<p>${inline(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html || "<p></p>";
}
