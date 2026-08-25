# ADR-002: Deterministic chess truth

- Status: Accepted
- Date: 2026-08-25

## Decision

FEN and `python-chess` own board state, legal moves, and replay. Stockfish owns evaluations and
candidate lines. The facts pipeline owns deterministic classification evidence, and deterministic
aggregation owns training results and learning estimates. Model text owns none of these facts.

## Consequences

Agent and explanation output must cite verified context or successful tools. Invalid references,
moves, classifications, and actions are rejected before commit.
