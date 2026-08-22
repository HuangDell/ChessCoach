"""Runtime protocol for the Chess Coach Agent."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from server.core.agent.models import AgentRunRequest, AgentRunResult


@runtime_checkable
class AgentRuntime(Protocol):
    """Framework-neutral, non-streaming Agent execution boundary."""

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        ...
