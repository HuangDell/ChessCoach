export function errorMessage(value, fallback = "Something went wrong.") {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (!value || typeof value !== "object") return fallback;
  if (Array.isArray(value)) {
    const messages = value
      .map((item) => errorMessage(item, ""))
      .filter(Boolean);
    return messages.length ? messages.join("; ") : fallback;
  }
  const payload = value.payload && typeof value.payload === "object" ? value.payload : null;
  const candidate =
    value.detail || value.error || value.message || value.msg ||
    (payload && (payload.detail || payload.error || payload.message));
  if (candidate && typeof candidate === "object") return errorMessage(candidate, fallback);
  return typeof candidate === "string" && candidate.trim() ? candidate.trim() : fallback;
}
