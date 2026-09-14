import { escapeHtml } from "../core/dom.js";

function safeSourceUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

export function knowledgeSourcesHtml(citations = []) {
  const bounded = Array.isArray(citations) ? citations.slice(0, 5) : [];
  if (!bounded.length) return "";
  const items = bounded.map((citation) => {
    const title = escapeHtml(citation.title || "Untitled source");
    const author = citation.author ? ` · ${escapeHtml(citation.author)}` : "";
    const location = [citation.heading, citation.source_locator].filter(Boolean).join(" · ");
    const url = safeSourceUrl(citation.source_url);
    const label = url
      ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${title}</a>`
      : `<strong>${title}</strong>`;
    return `<li>${label}${author}${location ? `<span>${escapeHtml(location)}</span>` : ""}</li>`;
  }).join("");
  return `<section class="knowledge-sources"><h3>Sources</h3><ol>${items}</ol></section>`;
}
