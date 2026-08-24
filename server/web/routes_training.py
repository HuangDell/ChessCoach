"""Retry and personal-training endpoints backed by stored critical-position artifacts."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server.core import training


router = APIRouter()


class AttemptBody(BaseModel):
    game_id: str
    critical_id: str
    selected_move: str
    review_side: str | None = None
    hints_used: int = Field(default=0, ge=0, le=4)
    source: str = "retry"


@router.post("/training/attempt")
def submit_attempt(body: AttemptBody) -> JSONResponse:
    try:
        result = training.evaluate_attempt(
            game_id=body.game_id,
            critical_id=body.critical_id,
            selected_move=body.selected_move,
            review_side=body.review_side,
            hints_used=body.hints_used,
            source="puzzle" if body.source == "puzzle" else "retry",
        )
    except training.TrainingGameDeletedError as exc:
        return JSONResponse(
            {"error": {"code": exc.code, "message": str(exc)}},
            status_code=409,
        )
    except training.TrainingPositionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except training.LearningProjectionError as exc:
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "attempt_id": exc.attempt_id,
                }
            },
            status_code=500,
        )
    return JSONResponse(result)


@router.get("/training/hint")
def get_hint(
    game_id: str,
    critical_id: str,
    level: int = 1,
    review_side: str | None = None,
) -> JSONResponse:
    try:
        return JSONResponse(
            training.hint_for(
                game_id=game_id,
                critical_id=critical_id,
                level=level,
                review_side=review_side,
            )
        )
    except training.TrainingPositionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@router.get("/training/attempts")
def get_attempts(game_id: str | None = None, critical_id: str | None = None) -> dict:
    attempts = training.load_attempts(game_id=game_id, critical_id=critical_id)
    return {"attempts": attempts, "count": len(attempts)}
