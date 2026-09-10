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

## Provider schema adapters

`CHESS_AGENT_PROVIDER` selects `openai`, `generic`, or `deepseek` within the same runtime.
When omitted, the effective custom URL selects generic; otherwise OpenAI is used. Unknown values
are configuration errors. This setting never selects credentials, models, URLs, or another protocol.

`schema_adapter.py` transforms wire schemas only. OpenAI/generic are identity adapters. DeepSeek v1
inlines local references in `anyOf` branches and removes `minLength`, `maxLength`, `minItems`, and
`maxItems`; types, nullability, unions, required fields and additional-property restrictions remain.
The runtime supplies adapted output via SDK `AgentOutputSchemaBase`, delegating validation to the
original SDK output schema. Tool parameter schemas receive the same transformation while SDK tool
invocation and domain validation remain intact. No SDK dependency is added to domain models.

Live reports and certificates bind adapter name and version. Missing adapter fields in legacy
schema-v1 certificates mean generic v1 only. Production and live eval share configuration resolution.
The DeepSeek reference expansion is a compatibility hypothesis pending explicit live portfolio
verification; offline checks do not certify an endpoint. No dedicated adapter tests were added in
this change, so automated coverage of provider differences is limited.
