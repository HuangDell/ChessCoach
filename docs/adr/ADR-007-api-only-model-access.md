# ADR-007: API-only model access

- Status: Accepted; amended 2026-09-10
- Date: 2026-08-25

## Decision

Models are accessed only by backend APIs. There is no model CLI, subprocess, interactive login,
resume protocol, local chat transport, or implicit fallback. Bounded explanations remain a separate
OpenAI-compatible API provider outside the Agent loop.

Custom Responses endpoints use the configured model, base URL, API key, and schema adapter directly.
The live portfolio remains available as an explicit model benchmark and does not gate runtime use.

## Consequences

Missing credentials and provider failures return typed errors. Engine Review, history, learning,
and deterministic training remain available without any model.
