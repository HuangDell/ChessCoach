"""Game import and artifact-backed analysis entry points."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.core import analysis_cache
from server.core.importers import PgnImportError, import_pgn
from server.core.importers.pgn import resolve_review_side
from server.core.storage import GameNotFoundError, load_analysis, load_game, store_game
from server.web import jobs

router = APIRouter()


class ImportBody(BaseModel):
    pgn: str
    source_type: str = "pgn_text"
    source_url: str | None = None
    review_side: str = "auto"
    username: str = ""


class AnalyzeImportedBody(BaseModel):
    review_side: str = "auto"


def _error(code: str, message: str, status_code: int, **extra) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, **extra}},
        status_code=status_code,
    )


def _cache_state(game) -> None:
    cached_sides = [
        side for side in ("white", "black") if analysis_cache.load(game.pgn, side) is not None
    ]
    game.cached_sides = cached_sides
    game.analysis_cached = bool(game.review_side and game.review_side in cached_sides)


@router.post("/games/import")
def post_game_import(body: ImportBody) -> JSONResponse:
    """Normalize, validate and persist one or more PGNs without running Stockfish."""
    try:
        imported = import_pgn(
            body.pgn,
            source_type=body.source_type,
            source_url=body.source_url,
            review_side=body.review_side,
            username=body.username,
        )
        for game in imported:
            store_game(game)
            _cache_state(game)
    except PgnImportError as exc:
        extra = {"game_index": exc.game_index} if exc.game_index is not None else {}
        return _error(exc.code, exc.message, 400, **extra)
    except OSError as exc:
        return _error("storage_failed", f"Could not save the imported game: {exc}", 500)

    games = [game.to_dict() for game in imported]
    if len(games) == 1:
        return JSONResponse({"count": 1, "games": games, **games[0]})
    return JSONResponse({"count": len(games), "games": games})


@router.get("/games/{game_id}")
def get_imported_game(game_id: str) -> JSONResponse:
    """Return a stored normalized game plus its structured, legally replayed move list."""
    try:
        game = load_game(game_id)
    except GameNotFoundError as exc:
        return _error("game_not_found", str(exc), 404)
    return JSONResponse(game)


@router.get("/games/{game_id}/analysis")
def get_game_analysis(game_id: str, review_side: str | None = None) -> JSONResponse:
    """Return the stable, LLM-free two-stage Engine artifact for a stored game."""
    try:
        analysis = load_analysis(game_id, review_side=review_side)
    except GameNotFoundError as exc:
        return _error("analysis_not_found", str(exc), 404)
    return JSONResponse(analysis)


@router.get("/jobs/{job_id}")
def get_analysis_job(job_id: str) -> JSONResponse:
    """Poll one analysis by id, including terminal cancelled/failed records."""
    result = jobs.job_status(job_id)
    if result is None:
        return _error("job_not_found", "Unknown analysis job.", 404)
    return JSONResponse(result)


@router.post("/games/{game_id}/analyze")
def post_analyze_imported(
    game_id: str, body: AnalyzeImportedBody | None = None
) -> JSONResponse:
    """Start analysis from the stored normalized PGN, never from unvalidated user text."""
    try:
        game = load_game(game_id)
        requested = (body.review_side if body else "auto") or "auto"
        selected = resolve_review_side(game.get("headers") or {}, requested)
    except GameNotFoundError as exc:
        return _error("game_not_found", str(exc), 404)
    except PgnImportError as exc:
        return _error(exc.code, exc.message, 400)

    if selected is None:
        return _error(
            "review_side_required",
            "Choose White or Black before starting analysis.",
            409,
        )
    result = jobs.start(game["pgn"], player=selected, game_id=game_id)
    return JSONResponse(
        {
            **result,
            "review_side": selected,
            "job_url": f"/api/jobs/{result['job_id']}",
            "analysis_url": f"/api/games/{game_id}/analysis?review_side={selected}",
        }
    )
