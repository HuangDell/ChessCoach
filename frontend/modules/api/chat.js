import { http } from "../core/http.js";

export const chatApi = {
  history: (options) => http.get("/api/chat-history", options),
  send: (body, options) => http.post("/api/chat", body, options),
  reset: (options) => http.post("/api/chat-reset", undefined, options),
};
