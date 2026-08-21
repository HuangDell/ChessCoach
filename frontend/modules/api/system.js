import { http } from "../core/http.js";

export const systemApi = {
  appConfig: (options) => http.get("/api/app-config", options),
  doctor: (options) => http.get("/api/doctor", options),
  fixStockfishArch: (options) => http.post("/api/fix-stockfish-arch", undefined, options),
  connectivity: (options) => http.get("/api/connectivity", options),
  updateCheck: (options) => http.get("/api/update-check", options),
  applyUpdate: (options) => http.post("/api/apply-update", undefined, options),
  ping: (options = {}) => http.post("/api/ping", undefined, { ...options, keepalive: true }),
  closing: () => http.beacon("/api/closing"),
};
