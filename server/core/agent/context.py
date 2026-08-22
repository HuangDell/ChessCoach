"""Resolve canonical chess state and build the bounded model-visible context."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import chess

from server.core.agent.models import (
    AgentSessionState,
    EngineFactsContext,
    GameContext,
    GetReviewContextInput,
    GetReviewContextResult,
    ModelVisibleContext,
    PositionContext,
    PositionReference,
    ResolvedChessContext,
    ReviewContext,
    TaskContext,
    ToolResult,
)
from server.core.storage import games


class ChessContextError(ValueError):
    """A checkpoint cannot be resolved to one authoritative position."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedContextBundle:
    context: ResolvedChessContext
    analysis: dict[str, Any] | None = None
    critical: dict[str, Any] | None = None


ReviewContextLoader = Callable[
    [GetReviewContextInput], Awaitable[ToolResult[GetReviewContextResult]]
]


def _move_notation(item: dict[str, Any]) -> tuple[str, str]:
    played = item.get("played_move") or item
    return str(played.get("uci") or ""), str(played.get("san") or "")


def _recent_moves(
    moves: list[dict[str, Any]], active_ply: int
) -> tuple[list[str], list[str]]:
    selected = [item for item in moves if int(item.get("ply") or 0) <= active_ply][-8:]
    pairs = [_move_notation(item) for item in selected]
    if any(not uci or not san for uci, san in pairs):
        raise ChessContextError("invalid_session_context", "Game artifact has incomplete move notation.")
    return [uci for uci, _san in pairs], [san for _uci, san in pairs]


def _replay_mainline(moves: list[dict[str, Any]], active_ply: int) -> chess.Board:
    if not moves:
        raise ChessContextError("invalid_session_context", "Saved game has no replayable moves.")
    try:
        board = chess.Board(str(moves[0].get("fen_before") or ""))
    except ValueError as exc:
        raise ChessContextError(
            "invalid_session_context", "Saved game position has an invalid FEN."
        ) from exc
    if not board.is_valid():
        raise ChessContextError(
            "invalid_session_context", "Saved game position has an invalid FEN."
        )

    for expected_ply, item in enumerate(moves[:active_ply], start=1):
        if int(item.get("ply") or 0) != expected_ply or item.get("fen_before") != board.fen():
            raise ChessContextError(
                "invalid_session_context", "Saved game mainline is not continuous."
            )
        uci, san = _move_notation(item)
        try:
            move = chess.Move.from_uci(uci)
        except ValueError as exc:
            raise ChessContextError(
                "invalid_session_context", "Saved game contains invalid move notation."
            ) from exc
        if move not in board.legal_moves or board.san(move) != san:
            raise ChessContextError(
                "invalid_session_context", "Saved game contains an illegal or mismatched move."
            )
        board.push(move)
        if item.get("fen_after") != board.fen():
            raise ChessContextError(
                "invalid_session_context", "Saved game mainline has a mismatched result FEN."
            )
    return board


def _position_at_ply(
    game_id: str,
    review_side: str | None,
    moves: list[dict[str, Any]],
    active_ply: int,
) -> PositionContext:
    if active_ply < 0 or active_ply > len(moves):
        raise ChessContextError("invalid_session_context", "Active ply is outside the saved game.")
    board = _replay_mainline(moves, active_ply)
    fen = board.fen()
    recent_uci, recent_san = _recent_moves(moves, active_ply)
    return PositionContext(
        fen=fen,
        recent_moves_uci=recent_uci,
        recent_moves_san=recent_san,
        reference=PositionReference(
            game_id=game_id,
            review_side=review_side,
            ply=active_ply,
            fen=fen,
        ),
    )


def _bounded_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Keep deterministic teaching evidence while excluding large unrelated snapshots."""

    snapshots: dict[str, Any] = {}
    for name in ("before", "after_played", "after_best"):
        source = (facts.get("snapshots") or {}).get(name) or {}
        snapshots[name] = {
            key: source.get(key)
            for key in (
                "fen",
                "turn",
                "in_check",
                "material",
                "safety",
                "structure",
                "mobility",
                "king_safety",
                "phase",
            )
            if key in source
        }
    replies = facts.get("opponent_direct_replies") or {}
    return {
        key: value
        for key, value in {
            "facts_version": facts.get("facts_version"),
            "snapshots": snapshots,
            "move_effects": facts.get("move_effects"),
            "played_line_result": facts.get("played_line_result"),
            "best_line_result": facts.get("best_line_result"),
            "deltas": facts.get("deltas"),
            "opponent_direct_replies": {
                "checks": list(replies.get("checks") or [])[:5],
                "captures": list(replies.get("captures") or [])[:5],
            },
            "motifs": list(facts.get("motifs") or [])[:5],
            "primary_category": facts.get("primary_category"),
            "secondary_categories": list(facts.get("secondary_categories") or [])[:5],
            "classification_evidence": list(facts.get("classification_evidence") or [])[:8],
        }.items()
        if value not in (None, {}, [])
    }


def _validated_selected_move(position: PositionContext) -> tuple[str | None, str | None]:
    uci = position.selected_move_uci
    san = position.selected_move_san
    if uci is None or san is None:
        return None, None
    board = chess.Board(position.fen)
    try:
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise ChessContextError(
            "invalid_session_context", "Selected move has invalid UCI notation."
        ) from exc
    if move not in board.legal_moves or board.san(move) != san:
        raise ChessContextError(
            "invalid_session_context", "Selected move is illegal or has mismatched SAN."
        )
    return move.uci(), san


class ChessContextBuilder:
    """Resolve a checkpoint without exposing raw artifacts to the model."""

    def resolve(self, session: AgentSessionState) -> ResolvedContextBundle:
        analysis: dict[str, Any] | None = None
        critical: dict[str, Any] | None = None
        game_context: GameContext | None = None
        review_context: ReviewContext | None = None
        position: PositionContext | None = None

        if session.active_game_id is not None:
            if session.review_side is not None:
                try:
                    analysis = games.load_analysis(session.active_game_id, session.review_side)
                    game_source = analysis
                except games.GameNotFoundError:
                    try:
                        game_source = games.load_game(session.active_game_id)
                    except games.GameNotFoundError as exc:
                        raise ChessContextError("game_not_found", str(exc)) from exc
                headers = {
                    str(key): str(value)
                    for key, value in (game_source.get("headers") or {}).items()
                }
                game_context = GameContext(
                    game_id=session.active_game_id,
                    review_side=session.review_side,
                    headers=headers,
                    result=(
                        str(game_source.get("result"))
                        if game_source.get("result") is not None
                        else None
                    ),
                )
                critical_positions = (
                    list(analysis.get("critical_positions") or [])
                    if analysis is not None
                    else []
                )
                if analysis is not None:
                    review_context = ReviewContext(
                        game_id=session.active_game_id,
                        review_side=session.review_side,
                        critical_ids=[
                            str(item["critical_id"])
                            for item in critical_positions
                            if item.get("critical_id")
                        ],
                        active_critical_id=session.active_critical_id,
                    )
                if session.active_critical_id is not None:
                    critical = next(
                        (
                            item
                            for item in critical_positions
                            if item.get("critical_id") == session.active_critical_id
                        ),
                        None,
                    )
                    if critical is None:
                        raise ChessContextError(
                            "position_not_found", "The active critical position is not available."
                        )
                    critical_ply = int(critical.get("ply") or 0)
                    if critical_ply < 1 or session.active_ply not in (None, critical_ply - 1):
                        raise ChessContextError(
                            "invalid_session_context",
                            "Active ply does not match the selected critical position.",
                        )
                    recent_uci, recent_san = _recent_moves(
                        list(analysis.get("moves") or []), critical_ply - 1
                    )
                    fen = str(critical.get("fen_before") or "")
                    mainline_moves = list(analysis.get("moves") or [])
                    replayed = _replay_mainline(mainline_moves, critical_ply - 1)
                    if replayed.fen() != fen:
                        raise ChessContextError(
                            "invalid_session_context",
                            "Critical position does not match the saved mainline.",
                        )
                    _replay_mainline(mainline_moves, critical_ply)
                    stage_move = mainline_moves[critical_ply - 1]
                    if (
                        _move_notation(stage_move)
                        != _move_notation(dict(critical.get("played_move") or {}))
                        or stage_move.get("fen_after") != critical.get("fen_after")
                    ):
                        raise ChessContextError(
                            "invalid_session_context",
                            "Critical move does not match the saved mainline.",
                        )
                    reference = PositionReference(
                        game_id=session.active_game_id,
                        review_side=session.review_side,
                        critical_id=session.active_critical_id,
                        ply=critical_ply,
                        fen=fen,
                    )
                    position = PositionContext(
                        fen=fen,
                        recent_moves_uci=recent_uci,
                        recent_moves_san=recent_san,
                        reference=reference,
                    )
                elif session.active_ply is not None:
                    position = _position_at_ply(
                        session.active_game_id,
                        session.review_side,
                        list(game_source.get("moves") or []),
                        session.active_ply,
                    )
            else:
                try:
                    game = games.load_game(session.active_game_id)
                except games.GameNotFoundError as exc:
                    raise ChessContextError("game_not_found", str(exc)) from exc
                game_context = GameContext(
                    game_id=session.active_game_id,
                    headers={str(k): str(v) for k, v in (game.get("headers") or {}).items()},
                )
                if session.active_ply is not None:
                    position = _position_at_ply(
                        session.active_game_id,
                        None,
                        list(game.get("moves") or []),
                        session.active_ply,
                    )

        supplied = session.position
        if supplied is not None:
            if supplied.reference and supplied.reference.game_id not in (
                None,
                session.active_game_id,
            ):
                raise ChessContextError(
                    "invalid_session_context", "Position reference belongs to another game."
                )
            if position is not None and supplied.fen != position.fen:
                raise ChessContextError(
                    "invalid_session_context",
                    "Submitted FEN does not match the canonical game ply.",
                )
            selected_uci, selected_san = _validated_selected_move(supplied)
            if position is None:
                position = supplied
            elif selected_uci is not None:
                position = position.model_copy(
                    update={
                        "selected_move_uci": selected_uci,
                        "selected_move_san": selected_san,
                    }
                )

        return ResolvedContextBundle(
            context=ResolvedChessContext(
                session=session,
                position=position,
                game=game_context,
                review=review_context,
                profile=None,
                training=None,
            ),
            analysis=analysis,
            critical=critical,
        )

    async def build_model_context(
        self,
        bundle: ResolvedContextBundle,
        question: str,
        review_loader: ReviewContextLoader | None = None,
    ) -> ModelVisibleContext:
        resolved = bundle.context
        engine_facts: EngineFactsContext | None = None
        allowed_evidence_refs: list[str] = []
        if (
            bundle.critical is not None
            and resolved.review is not None
            and review_loader is not None
        ):
            request = GetReviewContextInput(
                game_id=resolved.review.game_id,
                review_side=resolved.review.review_side,
                critical_id=str(bundle.critical["critical_id"]),
            )
            loaded = await review_loader(request)
            if loaded.ok and loaded.data is not None:
                review = loaded.data
                allowed_evidence_refs = list(loaded.evidence_refs)
                engine_facts = EngineFactsContext(
                    reference=review.reference,
                    played_move=review.played_move,
                    best_move=review.best_move,
                    classification=review.classification,
                    criticality=review.criticality,
                    candidates=review.candidates,
                    provenance=review.provenance,
                    facts=_bounded_facts(review.facts),
                    evidence_refs=allowed_evidence_refs,
                )

        return ModelVisibleContext(
            task=TaskContext(
                activity=resolved.session.activity,
                user_goal=question,
                review_side=resolved.session.review_side,
            ),
            position=resolved.position,
            engine_facts=engine_facts,
            relevant_profile=None,
            relevant_memory=[],
            conversation_summary="",
            allowed_evidence_refs=allowed_evidence_refs,
        )
