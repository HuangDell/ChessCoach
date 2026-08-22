"""Runtime protocol for the Chess Coach Agent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from server.core.agent.models import AgentError, AgentRunRequest, AgentRunResult


@dataclass(frozen=True)
class AgentRuntimeAvailability:
    enabled: bool
    available: bool
    model: str
    endpoint_type: str
    reason: str | None = None


class AgentRuntimeFailure(RuntimeError):
    """Stable framework-neutral failure raised by a runtime adapter."""

    def __init__(self, error: AgentError):
        super().__init__(error.message)
        self.error = error


@runtime_checkable
class AgentRuntime(Protocol):
    """Framework-neutral, non-streaming Agent execution boundary."""

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        ...


class UnavailableAgentRuntime:
    def __init__(self, availability: AgentRuntimeAvailability):
        self.availability = availability

    async def run(self, request: AgentRunRequest) -> AgentRunResult:
        del request
        raise AgentRuntimeFailure(
            AgentError(
                code="agent_unavailable",
                message=self.availability.reason or "Chess Coach Agent is not available.",
                recoverable=True,
            )
        )
