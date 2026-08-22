"""Thin HTTP adapter for the optional Phase 1 Chess Coach Agent service."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from server.core.agent.models import (
    AgentError,
    AgentErrorResponse,
    AgentMessageRequest,
    AgentMessageResponse,
    AgentSessionContextRequest,
    AgentSessionCreateRequest,
    AgentSessionResponse,
)
from server.core.agent.service import AgentServiceFailure, ChessAgentService


router = APIRouter(prefix="/agent")

_ERROR_STATUS = {
    "session_not_found": 404,
    "stale_agent_context": 409,
    "session_busy": 409,
    "invalid_session_context": 400,
    "agent_provider_error": 502,
    "invalid_agent_response": 502,
    "max_turns_exceeded": 502,
    "agent_unavailable": 503,
    "agent_authentication_failed": 503,
    "agent_rate_limited": 503,
    "agent_timeout": 504,
}
_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": AgentErrorResponse}
    for status in (400, 404, 409, 502, 503, 504)
}


def _service(request: Request) -> ChessAgentService:
    service = getattr(request.app.state, "agent_service", None)
    if service is None:
        raise AgentServiceFailure(
            AgentError(
                code="agent_unavailable",
                message="Chess Coach Agent is not available.",
                recoverable=True,
            )
        )
    return service


def _error_response(failure: AgentServiceFailure) -> JSONResponse:
    error = failure.error
    status = _ERROR_STATUS.get(error.code, 502)
    payload = AgentErrorResponse(error=error).model_dump(mode="json")
    return JSONResponse(payload, status_code=status)


@router.post(
    "/sessions",
    response_model=AgentSessionResponse,
    responses=_ERROR_RESPONSES,
)
async def create_agent_session(
    body: AgentSessionCreateRequest,
    request: Request,
) -> AgentSessionResponse | JSONResponse:
    try:
        return _service(request).create_session(body)
    except AgentServiceFailure as exc:
        return _error_response(exc)


@router.get(
    "/sessions/{session_id}",
    response_model=AgentSessionResponse,
    responses=_ERROR_RESPONSES,
)
async def get_agent_session(
    session_id: str,
    request: Request,
) -> AgentSessionResponse | JSONResponse:
    try:
        return _service(request).get_session(session_id)
    except AgentServiceFailure as exc:
        return _error_response(exc)


@router.delete(
    "/sessions/{session_id}",
    status_code=204,
    responses=_ERROR_RESPONSES,
)
async def delete_agent_session(session_id: str, request: Request) -> Response:
    try:
        await _service(request).delete_session(session_id)
    except AgentServiceFailure as exc:
        return _error_response(exc)
    return Response(status_code=204)


@router.post(
    "/sessions/{session_id}/context",
    response_model=AgentSessionResponse,
    responses=_ERROR_RESPONSES,
)
async def update_agent_context(
    session_id: str,
    body: AgentSessionContextRequest,
    request: Request,
) -> AgentSessionResponse | JSONResponse:
    try:
        return await _service(request).update_context(session_id, body)
    except AgentServiceFailure as exc:
        return _error_response(exc)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=AgentMessageResponse,
    responses=_ERROR_RESPONSES,
)
async def send_agent_message(
    session_id: str,
    body: AgentMessageRequest,
    request: Request,
) -> AgentMessageResponse | JSONResponse:
    try:
        return await _service(request).send_message(session_id, body)
    except AgentServiceFailure as exc:
        return _error_response(exc)
