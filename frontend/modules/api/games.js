import { http } from "../core/http.js";

export const gamesApi = {
  history: (options) => http.get("/api/history", options),
  lichessGames: (query, options) => http.get("/api/lichess/games", { ...options, query }),
  chesscomGames: (query, options) => http.get("/api/chesscom/games", { ...options, query }),
  syncChesscom: (body, options) => http.post("/api/sync/chesscom", body, options),
  deleteGame: (gameId, options) => http.delete(`/api/games/${encodeURIComponent(gameId)}`, options),
  profile: (days, options) => http.get("/api/profile", { ...options, query: { days } }),
  importPgn: (body, options) => http.post("/api/games/import", body, options),
};
