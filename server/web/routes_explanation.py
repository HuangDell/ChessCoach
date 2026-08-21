"""Artifact-backed endpoints for structured AI coaching explanations."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.core.explanation import (
    ExplanationError,
    ExplanationNotFoundError,
    generate_explanations,
)
from server.core.storage import GameNotFoundError, load_explanations

router = APIRouter()


class GenerateExplanationsBody(BaseModel):
    review_side: str | None = None
    critical_id: str | None = None
    force: bool = False


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}}, status_code=status_code
    )


@router.get("/games/{game_id}/explanations")
def get_game_explanations(game_id: str, review_side: str | None = None) -> JSONResponse:
    """Return only previously validated explanations; this endpoint never calls a model."""
    try:
        artifact = load_explanations(game_id, review_side=review_side)
    except GameNotFoundError as exc:
        return _error("explanations_not_found", str(exc), 404)
    return JSONResponse(artifact)


@router.post("/games/{game_id}/explanations")
def post_game_explanations(
    game_id: str, body: GenerateExplanationsBody | None = None
) -> JSONResponse:
    """Generate one critical position or all missing positions, with input-hash caching."""
    request = body or GenerateExplanationsBody()
    try:
        result = generate_explanations(
            game_id,
            review_side=request.review_side,
            critical_id=(request.critical_id or "").strip() or None,
            force=request.force,
        )
    except ExplanationNotFoundError as exc:
        return _error("explanation_input_not_found", str(exc), 404)
    except ExplanationError as exc:
        return _error("explanation_failed", str(exc), 503)
    return JSONResponse(result)
