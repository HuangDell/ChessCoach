# ADR-001: OpenAI Agents SDK runtime

- Status: Accepted
- Date: 2026-08-25

## Decision

V1 uses `openai-agents==0.22.0` behind `server/core/agent/runtime_openai.py`. Domain and Web code
depend on the local `AgentRuntime` protocol and project DTOs, never SDK types. The adapter uses the
Responses path, typed function tools, structured `AgentResponse`, bounded turns, non-streaming runs,
SQLite conversation sessions, and disabled sensitive tracing.

## Consequences

SDK upgrades require compatibility tests, an eval report, and custom endpoint recertification. The
application does not maintain a second tool loop or fall back to Chat Completions.
