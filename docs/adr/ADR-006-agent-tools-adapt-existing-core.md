# ADR-006: Agent tools adapt existing Core

- Status: Accepted
- Date: 2026-08-25

## Decision

Function tools are narrow adapters over existing review, Engine, opening, profile, and training
Core boundaries. They return project DTOs and expose explicit READ or COMPUTE permission.

## Consequences

Tools do not copy legality, scoring, ranking, storage, or training rules. New tools are justified by
a stable Core capability, not by one prompt.
