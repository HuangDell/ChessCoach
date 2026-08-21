import { http } from "../core/http.js";

export const puzzleApi = {
  config: (options) => http.get("/api/puzzle/config", options),
  current: (options) => http.get("/api/puzzle/current", options),
  next: (query, options) => http.get("/api/puzzle/next", { ...options, query }),
  chat: (body, options) => http.post("/api/chat", body, options),
  move: (body, options) => http.post("/api/puzzle/move", body, options),
  stormStart: (options) => http.post("/api/puzzle/storm/start", undefined, options),
  stormNext: (options) => http.get("/api/puzzle/storm/next", options),
  stormMove: (body, options) => http.post("/api/puzzle/storm/move", body, options),
  stormReview: (options) => http.get("/api/puzzle/storm/review", options),
  solution: (id, options) => http.get("/api/puzzle/solution", { ...options, query: { id } }),
  stormSummary: (options) => http.post("/api/puzzle/storm/summary", undefined, options),
  stormEnd: (options) => http.post("/api/puzzle/storm/end", undefined, options),
  state: (options) => http.get("/api/puzzle/state", options),
  giveUp: (id, options) => http.post("/api/puzzle/giveup", { id }, options),
  explain: (body, options) => http.post("/api/puzzle/explain", body, options),
  hint: (id, options) => http.post("/api/puzzle/hint", { id }, options),
  categories: (options) => http.get("/api/puzzle/categories", options),
};
