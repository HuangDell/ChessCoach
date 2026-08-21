# Frontend modules

The frontend is a browser-native ES module application. FastAPI still serves the static files and
JSON endpoints from the same origin; no bundler or separate Node service is required.

## Dependency direction

```text
main.js -> app.js -> feature controllers -> feature modules -> api modules -> core/http.js
                    feature controllers -> board/core infrastructure
```

- `app.js` is the composition root. It creates controllers and connects cross-feature ports.
- `api/` is the only place that knows endpoint paths and HTTP methods.
- `board/` owns Chess/Chessground integration and responsive board layout.
- `games/controller.js` owns tabs, account identity, and startup sync. `library.js`, `importer.js`,
  and `insights.js` own their lists, PGN workflow, and profile state respectively.
- `puzzles/controller.js` owns only Analyze/Puzzles and Solve/Storm lifecycle coordination. The
  trainer, Storm, board view, chat, progress, and solution playback modules each own their state and
  cancellation scope.
- `review/`, `settings/`, and `system/` own their feature-specific DOM events and private state.
- `core/` contains shared infrastructure for DOM access, async scopes, storage, errors, formatting,
  and HTTP. It must not import feature modules or controllers.

Feature controllers do not import each other. Cross-feature actions are injected as narrow `bridge`
or `lifecycle` ports by `app.js`; feature modules may only call those ports through their owning
controller. API modules are the sole owners of endpoint paths. This keeps the graph acyclic and
makes state and endpoint ownership explicit.

## Extending the frontend

1. Add or change an endpoint in the matching `api/` module. Do not call `fetch` from a controller.
2. Keep state in the narrowest owning feature module; controllers should coordinate, not render.
3. Add a narrow port in `app.js` when one feature must trigger another. Never import another feature
   controller directly.
4. Use `createLatestRequestScope` for superseded requests and retain a generation check when work
   continues after timers, animations, or a server response.
5. Run `npm run test:frontend` to verify the request contract and module boundaries.
