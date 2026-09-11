"""Resolve canonical chess state and build the bounded model-visible context."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Sequence

import chess

from server.core.agent.models import (
    AgentSessionState,
    AnalyzePositionResult,
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


FollowUpResolutionStatus = Literal["resolved", "ambiguous", "unresolved"]
FollowUpReferenceSource = Literal[
    "explicit",
    "checkpoint",
    "discussed",
    "recent_turn",
    "summary",
]


@dataclass(frozen=True)
class FollowUpResolution:
    """Result of resolving a follow-up without guessing between equal candidates."""

    status: FollowUpResolutionStatus
    reference: PositionReference | None = None
    source: FollowUpReferenceSource | None = None
    candidates: tuple[PositionReference, ...] = ()
    message: str = ""


ReviewContextLoader = Callable[
    [GetReviewContextInput], Awaitable[ToolResult[GetReviewContextResult]]
]
LiveAnalysisLoader = Callable[[str, str], ToolResult[AnalyzePositionResult] | None]


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
            selected_uci, selected_san = _validated_selected_move(supplied)
            if supplied.exploration_moves_uci:
                if supplied.reference is None:
                    raise ChessContextError(
                        "invalid_session_context",
                        "Exploration is missing its canonical base position.",
                    )
                canonical_base = self.canonicalize_reference(supplied.reference)
                if (
                    position is not None
                    and canonical_base != position.reference
                ):
                    raise ChessContextError(
                        "invalid_session_context",
                        "Exploration does not start from the active canonical position.",
                    )
                values = supplied.model_dump(mode="python")
                values["reference"] = canonical_base
                if position is not None:
                    values["recent_moves_uci"] = position.recent_moves_uci
                    values["recent_moves_san"] = position.recent_moves_san
                values["selected_move_uci"] = selected_uci
                values["selected_move_san"] = selected_san
                position = PositionContext.model_validate(values)
            elif position is not None and supplied.fen != position.fen:
                raise ChessContextError(
                    "invalid_session_context",
                    "Submitted FEN does not match the canonical game ply.",
                )
            elif position is None:
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

    def canonicalize_reference(
        self,
        reference: PositionReference,
        *,
        session: AgentSessionState | None = None,
    ) -> PositionReference:
        """Validate a position pointer against its artifact and fill canonical fields."""

        if reference.game_id is None:
            if reference.fen is None:
                raise ChessContextError(
                    "position_not_found", "The position reference has no usable identity."
                )
            return PositionReference(fen=chess.Board(reference.fen).fen())

        game_id = reference.game_id
        review_side = reference.review_side
        if (
            review_side is None
            and session is not None
            and session.active_game_id == game_id
        ):
            review_side = session.review_side

        trusted_exploration = self._trusted_exploration_reference(
            reference,
            session=session,
            review_side=review_side,
        )
        if trusted_exploration is not None:
            return trusted_exploration

        if reference.critical_id is not None:
            try:
                analysis = games.load_analysis(game_id, review_side)
            except games.GameNotFoundError as exc:
                raise ChessContextError("position_not_found", str(exc)) from exc
            artifact_side = analysis.get("review_side")
            if artifact_side not in ("white", "black"):
                raise ChessContextError(
                    "invalid_session_context", "Analysis artifact has no valid review side."
                )
            critical = next(
                (
                    item
                    for item in list(analysis.get("critical_positions") or [])
                    if item.get("critical_id") == reference.critical_id
                ),
                None,
            )
            if critical is None:
                raise ChessContextError(
                    "position_not_found", "The referenced critical position is not available."
                )
            ply = int(critical.get("ply") or 0)
            fen = str(critical.get("fen_before") or "")
            canonical_position = _position_at_ply(
                game_id,
                str(artifact_side),
                list(analysis.get("moves") or []),
                ply - 1,
            )
            if ply < 1 or canonical_position.fen != fen:
                raise ChessContextError(
                    "invalid_session_context",
                    "The referenced critical position does not match its saved mainline.",
                )
            if reference.ply not in (None, ply) or reference.fen not in (None, fen):
                raise ChessContextError(
                    "position_not_found", "The reference does not match the critical position."
                )
            return PositionReference(
                game_id=game_id,
                review_side=artifact_side,
                critical_id=reference.critical_id,
                ply=ply,
                fen=fen,
            )

        try:
            if review_side is not None:
                try:
                    artifact = games.load_analysis(game_id, review_side)
                except games.GameNotFoundError:
                    artifact = games.load_game(game_id)
            else:
                artifact = games.load_game(game_id)
        except games.GameNotFoundError as exc:
            raise ChessContextError("position_not_found", str(exc)) from exc

        moves = list(artifact.get("moves") or [])
        if reference.ply is not None:
            position = _position_at_ply(game_id, review_side, moves, reference.ply)
            if reference.fen not in (None, position.fen):
                raise ChessContextError(
                    "position_not_found", "The reference FEN does not match its saved ply."
                )
            return position.reference  # type: ignore[return-value]

        if reference.fen is not None:
            normalized_fen = chess.Board(reference.fen).fen()
            matching_plies = [
                ply
                for ply in range(len(moves) + 1)
                if _position_at_ply(game_id, review_side, moves, ply).fen == normalized_fen
            ]
            if len(matching_plies) != 1:
                raise ChessContextError(
                    "position_not_found",
                    "The reference FEN does not identify one saved game position.",
                )
            position = _position_at_ply(game_id, review_side, moves, matching_plies[0])
            return position.reference  # type: ignore[return-value]

        if session is not None and session.active_game_id == game_id:
            current = self.resolve(session).context.position
            if current is not None and current.reference is not None:
                return current.reference
        raise ChessContextError(
            "position_not_found", "The game reference does not identify a position."
        )

    def _trusted_exploration_reference(
        self,
        reference: PositionReference,
        *,
        session: AgentSessionState | None,
        review_side: str | None,
    ) -> PositionReference | None:
        """Accept off-mainline FENs only after checkpoint-backed validation."""

        if (
            session is None
            or reference.fen is None
            or reference.critical_id is not None
            or reference.ply is not None
        ):
            return None
        normalized = PositionReference(
            game_id=reference.game_id,
            review_side=review_side,
            fen=chess.Board(reference.fen).fen(),
        )
        trusted = False
        supplied = session.position
        if (
            supplied is not None
            and supplied.exploration_moves_uci
            and supplied.fen == normalized.fen
            and session.active_game_id == normalized.game_id
            and session.review_side == normalized.review_side
        ):
            # resolve() replays and validates the exploration from its canonical base.
            current = self.resolve(session).context.position
            trusted = current is not None and current.fen == normalized.fen
        if not trusted:
            trusted = any(existing == normalized for existing in session.discussed_positions)
        if not trusted:
            return None

        try:
            if review_side is not None:
                games.load_analysis(str(reference.game_id), review_side)
            else:
                games.load_game(str(reference.game_id))
        except games.GameNotFoundError as exc:
            raise ChessContextError("position_not_found", str(exc)) from exc
        return normalized

    @staticmethod
    def _current_reference(
        session: AgentSessionState,
        position: PositionContext,
    ) -> PositionReference | None:
        if position.exploration_moves_uci:
            if session.active_game_id is None:
                return PositionReference(fen=position.fen)
            return PositionReference(
                game_id=session.active_game_id,
                review_side=session.review_side,
                fen=position.fen,
            )
        return position.reference

    def resolve_follow_up_reference(
        self,
        session: AgentSessionState,
        *,
        explicit_references: Sequence[PositionReference] = (),
        recent_turn_references: Sequence[PositionReference] = (),
        summary_references: Sequence[PositionReference] | None = None,
    ) -> FollowUpResolution:
        """Resolve one canonical antecedent in the Phase 2 priority order."""

        if explicit_references:
            resolved = self._resolve_reference_tier(
                session, "explicit", explicit_references
            )
            if resolved.status != "unresolved":
                return resolved
            return FollowUpResolution(
                status="unresolved",
                source="explicit",
                message="The explicit position reference is unavailable or expired.",
            )

        try:
            current = self.resolve(session).context.position
        except ChessContextError:
            current = None
        current_reference = (
            self._current_reference(session, current) if current is not None else None
        )
        if current_reference is not None:
            return FollowUpResolution(
                status="resolved",
                reference=current_reference,
                source="checkpoint",
                candidates=(current_reference,),
            )

        for reference in reversed(session.discussed_positions):
            resolved = self._resolve_reference_tier(session, "discussed", [reference])
            if resolved.status == "resolved":
                return resolved

        resolved = self._resolve_reference_tier(
            session, "recent_turn", recent_turn_references
        )
        if resolved.status != "unresolved":
            return resolved

        resolved = self._resolve_reference_tier(
            session,
            "summary",
            (
                session.conversation_summary_references
                if summary_references is None
                else summary_references
            ),
        )
        if resolved.status != "unresolved":
            return resolved
        return FollowUpResolution(
            status="unresolved",
            message="No validated position reference is available for this follow-up.",
        )

    def _resolve_reference_tier(
        self,
        session: AgentSessionState,
        source: FollowUpReferenceSource,
        references: Sequence[PositionReference],
    ) -> FollowUpResolution:
        candidates: list[PositionReference] = []
        identities: set[str] = set()
        for reference in references:
            try:
                canonical = self.canonicalize_reference(reference, session=session)
            except (ChessContextError, ValueError):
                continue
            identity = canonical.model_dump_json(exclude_none=True)
            if identity in identities:
                continue
            identities.add(identity)
            candidates.append(canonical)
        if len(candidates) == 1:
            return FollowUpResolution(
                status="resolved",
                reference=candidates[0],
                source=source,
                candidates=(candidates[0],),
            )
        if len(candidates) > 1:
            return FollowUpResolution(
                status="ambiguous",
                source=source,
                candidates=tuple(candidates),
                message="Multiple equally likely positions require explicit clarification.",
            )
        return FollowUpResolution(status="unresolved", source=source)

    def validated_discussed_positions(
        self,
        session: AgentSessionState,
        *,
        limit: int = 5,
    ) -> list[PositionReference]:
        """Return only still-valid, unique recent references for model context."""

        if limit < 0:
            raise ValueError("limit must not be negative")
        if limit == 0:
            return []
        recent: list[PositionReference] = []
        identities: set[str] = set()
        for reference in reversed(session.discussed_positions):
            try:
                canonical = self.canonicalize_reference(reference, session=session)
            except (ChessContextError, ValueError):
                continue
            identity = canonical.model_dump_json(exclude_none=True)
            if identity in identities:
                continue
            identities.add(identity)
            recent.append(canonical)
            if len(recent) == limit:
                break
        recent.reverse()
        return recent

    async def build_model_context(
        self,
        bundle: ResolvedContextBundle,
        question: str,
        review_loader: ReviewContextLoader | None = None,
        live_analysis_loader: LiveAnalysisLoader | None = None,
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
        elif (
            resolved.position is not None
            and resolved.position.live_analysis_ref is not None
            and live_analysis_loader is not None
        ):
            loaded = live_analysis_loader(
                resolved.position.fen,
                resolved.position.live_analysis_ref,
            )
            if loaded is not None and loaded.ok and loaded.data is not None:
                analysis = loaded.data
                allowed_evidence_refs = list(loaded.evidence_refs)
                engine_facts = EngineFactsContext(
                    reference=PositionReference(fen=resolved.position.fen),
                    best_move=analysis.candidates[0].move if analysis.candidates else None,
                    candidates=analysis.candidates,
                    provenance=analysis.provenance,
                    facts={"source": "live_best_moves"},
                    evidence_refs=allowed_evidence_refs,
                )

        return ModelVisibleContext(
            task=TaskContext(
                activity=resolved.session.activity,
                user_goal=question,
                review_side=resolved.session.review_side,
            ),
            position=(
                resolved.position.model_copy(update={"live_analysis_ref": None})
                if resolved.position is not None
                else None
            ),
            engine_facts=engine_facts,
            relevant_profile=None,
            relevant_memory=[],
            conversation_summary=resolved.session.conversation_summary,
            discussed_positions=self.validated_discussed_positions(resolved.session),
            allowed_evidence_refs=allowed_evidence_refs,
        )
