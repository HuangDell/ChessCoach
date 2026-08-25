"""Runtime protocol for the Chess Coach Agent."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from server.core.agent.models import AgentError, AgentRunRequest, AgentRunResult, ToolCallRecord


@dataclass(frozen=True)
class AgentRuntimeAvailability:
    enabled: bool
    available: bool
    model: str
    endpoint_type: str
    reason: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class AgentRuntimeTelemetry:
    tool_calls: list[ToolCallRecord]
    usage: dict[str, int | float]


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
        code = self.availability.error_code or "agent_unavailable"
        raise AgentRuntimeFailure(
            AgentError(
                code=code,
                message=self.availability.reason or "Chess Coach Agent is not available.",
                recoverable=code not in {"agent_endpoint_incompatible", "agent_authentication_failed"},
            )
        )
