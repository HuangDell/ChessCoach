import { http } from "../core/http.js";

const sessionPath = (sessionId) =>
  `/api/agent/sessions/${encodeURIComponent(sessionId)}`;

export const agentApi = {
  createSession: (body = {}, options) => http.post("/api/agent/sessions", body, options),
  getSession: (sessionId, options) => http.get(sessionPath(sessionId), options),
  deleteSession: (sessionId, options) => http.delete(sessionPath(sessionId), options),
  updateContext: (sessionId, body, options) =>
    http.post(`${sessionPath(sessionId)}/context`, body, options),
  sendMessage: (sessionId, body, options) =>
    http.post(`${sessionPath(sessionId)}/messages`, body, options),
};
