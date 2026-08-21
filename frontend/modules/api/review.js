import { http } from "../core/http.js";

export const reviewApi = {
  bestMoves: (body, options) => http.post("/api/best-moves", body, options),
  threats: (body, options) => http.post("/api/threats", body, options),
  bestMove: (body, options) => http.post("/api/best-move", body, options),
  evaluate: (body, options) => http.post("/api/evaluate", body, options),
  position: (ply, options) => http.get(`/api/position/${ply}`, options),
  trainingAttempt: (body, options) => http.post("/api/training/attempt", body, options),
  trainingHint: (query, options) => http.get("/api/training/hint", { ...options, query }),
  explanations: (gameId, query, options) =>
    http.get(`/api/games/${encodeURIComponent(gameId)}/explanations`, { ...options, query }),
  analysis: (gameId, reviewSide, options) =>
    http.get(`/api/games/${encodeURIComponent(gameId)}/analysis`, {
      ...options,
      query: { review_side: reviewSide },
    }),
  generateExplanations: (gameId, body, options) =>
    http.post(`/api/games/${encodeURIComponent(gameId)}/explanations`, body, options),
  chatHistory: (options) => http.get("/api/chat-history", options),
  chat: (body, options) => http.post("/api/chat", body, options),
  coach: (body, options) => http.post("/api/coach", body, options),
  resetChat: (options) => http.post("/api/chat-reset", undefined, options),
  session: (options) => http.get("/api/session", options),
  timeline: (options) => http.get("/api/timeline", options),
  analysisStatus: (options) => http.get("/api/analysis-status", options),
  analyze: (body, options) => http.post("/api/analyze", body, options),
  analyzeGame: (gameId, body, options) =>
    http.post(`/api/games/${encodeURIComponent(gameId)}/analyze`, body, options),
  analyzeBatch: (body, options) => http.post("/api/analyze-batch", body, options),
};
