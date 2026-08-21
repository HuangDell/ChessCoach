export const byId = (id) => document.getElementById(id);
export const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
