import { http } from "../core/http.js";

export const gamesApi = {
  history: (options) => http.get("/api/history", options),
  lichessGames: (query, options) => http.get("/api/lichess/games", { ...options, query }),
  chesscomGames: (query, options) => http.get("/api/chesscom/games", { ...options, query }),
  syncChesscom: (body, options) => http.post("/api/sync/chesscom", body, options),
  saveSettings: (body, options) => http.post("/api/settings", body, options),
  settings: (options) => http.get("/api/settings", options),
  deleteGame: (gameId, options) => http.delete(`/api/games/${encodeURIComponent(gameId)}`, options),
  profile: (days, options) => http.get("/api/profile", { ...options, query: { days } }),
  clearEngineCache: (options) => http.post("/api/data/engine-cache/clear", undefined, options),
  ollamaModels: (url, options) => http.get("/api/ollama/models", { ...options, query: { url } }),
  importPgn: (body, options) => http.post("/api/games/import", body, options),
};
