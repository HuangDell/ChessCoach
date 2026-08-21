# Frontend modules

The frontend is a browser-native ES module application. FastAPI still serves the static files and
JSON endpoints from the same origin; no bundler or separate Node service is required.

## Dependency direction

```text
main.js -> app.js -> feature controllers -> api modules -> core/http.js
                    feature controllers -> board/core helpers
```

- `app.js` is the composition root. It creates controllers and connects cross-feature ports.
- `api/` is the only place that knows endpoint paths and HTTP methods.
- `board/` owns Chess/Chessground integration and responsive board layout.
- `games/`, `review/`, `puzzles/`, `settings/`, and `system/` own their DOM events and private state.
- `core/` contains stateless shared infrastructure. It must not import feature controllers.

Feature controllers do not import each other. Cross-feature actions are injected as narrow `bridge`
or `lifecycle` ports by `app.js`; this keeps the import graph acyclic and makes ownership explicit.

## Extending the frontend

1. Add or change an endpoint in the matching `api/` module. Do not call `fetch` from a controller.
2. Keep feature state and event listeners inside the owning controller.
3. Add a port in `app.js` when one feature needs to trigger another feature.
4. Cancel superseded requests with `AbortController`; retain a generation check when work continues
   after timers, animations, or a server response.
5. Run `npm run test:frontend` to verify the request contract and module boundaries.
