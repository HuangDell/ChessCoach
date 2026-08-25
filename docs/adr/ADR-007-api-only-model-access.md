# ADR-007: API-only model access

- Status: Accepted
- Date: 2026-08-25

## Decision

Models are accessed only by backend APIs. There is no model CLI, subprocess, interactive login,
resume protocol, local chat transport, or implicit fallback. Bounded explanations remain a separate
OpenAI-compatible API provider outside the Agent loop.

Custom Responses endpoints are unavailable until the current portfolio fully passes and a local
certificate matches endpoint fingerprint, model, SDK, policy, response schema, dataset, and scorer.

## Consequences

Missing credentials and incompatible endpoints fail closed. Engine Review, history, learning, and
deterministic training remain available without any model.
