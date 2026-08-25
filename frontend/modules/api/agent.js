import { http } from "../core/http.js";

const sessionPath = (sessionId) =>
  `/api/agent/sessions/${encodeURIComponent(sessionId)}`;

export const agentApi = {
  metrics: (limit = 100, options) =>
    http.get("/api/agent/metrics", { ...options, query: { limit } }),
  clearRuns: (options) => http.delete("/api/agent/runs", options),
  createSession: (body = {}, options) => http.post("/api/agent/sessions", body, options),
  getSession: (sessionId, options) => http.get(sessionPath(sessionId), options),
  deleteSession: (sessionId, options) => http.delete(sessionPath(sessionId), options),
  updateContext: (sessionId, body, options) =>
    http.post(`${sessionPath(sessionId)}/context`, body, options),
  sendMessage: (sessionId, body, options) =>
    http.post(`${sessionPath(sessionId)}/messages`, body, options),
  startTraining: (sessionId, body, options) =>
    http.post(`${sessionPath(sessionId)}/actions/start-training`, body, options),
};
