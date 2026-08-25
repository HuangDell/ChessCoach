# ADR-004: Session and memory separation

- Status: Accepted
- Date: 2026-08-25

## Decision

SDK conversation items, chess checkpoints, canonical observations, and deterministic estimates use
separate schemas and lifecycles. Conversation continuity is never chess truth or long-term evidence.

## Consequences

Stale or cancelled runs discard staged conversation. Deleting a conversation does not delete
learning data, and rebuilding learning data does not rewrite conversation history.
