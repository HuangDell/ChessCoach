import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, createHttpClient } from "../../frontend/modules/core/http.js";

test("serializes query parameters and JSON request bodies", async () => {
  const calls = [];
  const client = createHttpClient(async (url, options) => {
    calls.push({ url, options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });

  const result = await client.post("/api/example", { move: "e2e4" }, { query: { depth: 18 } });
  assert.deepEqual(result, { ok: true });
  assert.equal(calls[0].url, "/api/example?depth=18");
  assert.equal(calls[0].options.headers["Content-Type"], "application/json");
  assert.equal(calls[0].options.body, JSON.stringify({ move: "e2e4" }));
});

test("returns null for an empty 204 response", async () => {
  const client = createHttpClient(async () => new Response(null, { status: 204 }));
  assert.equal(await client.delete("/api/example"), null);
});

test("maps structured non-2xx responses to ApiError", async () => {
  const client = createHttpClient(async () =>
    new Response(JSON.stringify({ detail: "Invalid PGN", code: "invalid_pgn" }), {
      status: 422,
      headers: { "content-type": "application/json" },
    })
  );

  await assert.rejects(
    client.post("/api/games/import", { pgn: "bad" }),
    (error) =>
      error instanceof ApiError &&
      error.status === 422 &&
      error.message === "Invalid PGN" &&
      error.payload.code === "invalid_pgn"
  );
});

test("preserves AbortError so controllers can silently discard stale work", async () => {
  const client = createHttpClient(async (_url, options) =>
    new Promise((_resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    })
  );
  const controller = new AbortController();
  const pending = client.get("/api/slow", { signal: controller.signal });
  controller.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError");
});
