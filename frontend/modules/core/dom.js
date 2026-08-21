export const byId = (id) => document.getElementById(id);
export const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export function storageGet(storage, key) {
  try {
    return storage.getItem(key);
  } catch (_) {
    return null;
  }
}

export function storageSet(storage, key, value) {
  try {
    storage.setItem(key, value);
  } catch (_) {}
}

export const escapeHtml = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
