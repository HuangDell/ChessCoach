import { http } from "../core/http.js";

export const settingsApi = {
  get: (options) => http.get("/api/settings", options),
  save: (body, options) => http.post("/api/settings", body, options),
  ollamaModels: (url, options) => http.get("/api/ollama/models", { ...options, query: { url } }),
};
