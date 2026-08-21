export class ApiError extends Error {
  constructor(message, { status = 0, payload = null, cause = null } = {}) {
    super(message, cause ? { cause } : undefined);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function withQuery(path, query) {
  if (!query) return path;
  const params = query instanceof URLSearchParams ? query : new URLSearchParams();
  if (!(query instanceof URLSearchParams)) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
    }
  }
  const encoded = params.toString();
  return encoded ? `${path}${path.includes("?") ? "&" : "?"}${encoded}` : path;
}

function errorMessage(payload, status) {
  if (payload && typeof payload === "object") {
    return payload.detail || payload.error || payload.message || `Request failed (${status})`;
  }
  return typeof payload === "string" && payload.trim() ? payload.trim() : `Request failed (${status})`;
}

export function createHttpClient(fetchImpl = globalThis.fetch.bind(globalThis)) {
  async function request(path, { method = "GET", query, body, headers, signal, keepalive } = {}) {
    const options = { method, signal, keepalive, headers: { ...(headers || {}) } };
    if (body !== undefined) {
      options.headers["Content-Type"] ||= "application/json";
      options.body = options.headers["Content-Type"] === "application/json" ? JSON.stringify(body) : body;
    }

    let response;
    try {
      response = await fetchImpl(withQuery(path, query), options);
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      throw new ApiError("Unable to reach the local server.", { cause: error });
    }

    if (response.status === 204) return null;
    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    try {
      payload = contentType.includes("json") ? await response.json() : await response.text();
    } catch (error) {
      if (response.ok) throw new ApiError("The server returned an invalid response.", { status: response.status, cause: error });
    }
    if (!response.ok) {
      throw new ApiError(errorMessage(payload, response.status), { status: response.status, payload });
    }
    return payload;
  }

  return {
    request,
    get: (path, options = {}) => request(path, { ...options, method: "GET" }),
    post: (path, body, options = {}) => request(path, { ...options, method: "POST", body }),
    delete: (path, options = {}) => request(path, { ...options, method: "DELETE" }),
    beacon(path, body) {
      return globalThis.navigator && typeof globalThis.navigator.sendBeacon === "function"
        ? globalThis.navigator.sendBeacon(path, body)
        : false;
    },
  };
}

export const http = createHttpClient();
