"""Grounded Phase 1 domain tools backed by saved review facts and Stockfish.

The public methods use the frozen Phase 0 DTOs. Runtime adapters can call ``execute`` to retain
cache and actual Engine-I/O metadata without adding those internal details to model-visible data.
"""
from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, cast

import chess
from pydantic import BaseModel, ValidationError

from server import config
from server.core import history, lines, openings
from server.core.agent.models import (
    AgentToolName,
    AnalyzeMoveInput,
    AnalyzeMoveResult,
    AnalyzePositionInput,
    AnalyzePositionResult,
    CandidateLine,
    ChessReference,
    EngineProvenance,
    EngineScore,
    GetPlayerProfileInput,
    GetPlayerProfileResult,
    GetReviewContextInput,
    GetReviewContextResult,
    LookupOpeningInput,
    LookupOpeningResult,
    LearningMemoryItem,
    MemoryQuery,
    MoveReference,
    PositionContext,
    PositionReference,
    ReviewSide,
    SkillEstimate,
    ToolError,
    ToolResult,
)
from server.core.evaluation import classify
from server.core.learning import memory, taxonomy
from server.core.storage import games


ResultT = TypeVar("ResultT", bound=BaseModel)
AnalysisLoader = Callable[[str, str | None], dict[str, Any]]
OpeningClassifier = Callable[[list[str]], tuple[str | None, str | None]]
ProfileLoader = Callable[[], dict[str, Any]]


class ArtifactConsistencyError(ValueError):
    """Raised when a saved analysis cannot ground a typed tool result."""


class EngineAnalysisFailure(RuntimeError):
    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


@dataclass(frozen=True)
class ReviewArtifactScope:
    """The one saved critical position this tool set is allowed to expose and reuse."""

    game_id: str
    review_side: ReviewSide
    critical_id: str


@dataclass(frozen=True)
class ActiveReviewArtifact:
    scope: ReviewArtifactScope
    analysis: dict[str, Any]
    critical: dict[str, Any]

    @classmethod
    def from_analysis(
        cls,
        analysis: dict[str, Any],
        critical_id: str,
    ) -> "ActiveReviewArtifact":
        game_id = str(analysis.get("game_id") or "").strip()
        review_side = str(analysis.get("review_side") or "").strip()
        if not game_id or review_side not in {"white", "black"}:
            raise ArtifactConsistencyError("Saved analysis has invalid ownership metadata.")
        critical = next(
            (
                item
                for item in analysis.get("critical_positions", []) or []
                if isinstance(item, dict) and item.get("critical_id") == critical_id
            ),
            None,
        )
        if critical is None:
            raise ArtifactConsistencyError("Saved analysis does not contain the active position.")
        return cls(
            scope=ReviewArtifactScope(
                game_id=game_id,
                review_side=cast(ReviewSide, review_side),
                critical_id=critical_id,
            ),
            analysis=analysis,
            critical=critical,
        )


class PositionAnalysisProvider(Protocol):
    """Narrow, synchronous boundary suitable for a recording fake."""

    def analyze(self, fen: str, *, depth: int, multipv: int) -> lines.StructuredAnalysis:
        ...


class CorePositionAnalysisProvider:
    run_in_thread = True

    def analyze(self, fen: str, *, depth: int, multipv: int) -> lines.StructuredAnalysis:
        return lines.structured_analysis(fen, depth=depth, multipv=multipv)


@dataclass(frozen=True)
class ToolExecution(Generic[ResultT]):
    name: AgentToolName
    result: ToolResult[ResultT]
    cache_hit: bool
    engine_call_count: int

    @property
    def engine_calls(self) -> int:
        """Compatibility name used by runtime budget accounting."""
        return self.engine_call_count


def _tool_error(code: str, message: str, *, recoverable: bool) -> ToolError:
    return ToolError.model_validate(
        {"code": code, "message": message, "recoverable": recoverable}
    )


def _failure(
    name: AgentToolName,
    error: ToolError,
    *,
    cache_hit: bool = False,
    engine_call_count: int = 0,
) -> ToolExecution[Any]:
    return ToolExecution(
        name=name,
        result=ToolResult[Any](ok=False, error=error),
        cache_hit=cache_hit,
        engine_call_count=engine_call_count,
    )


def _success(
    name: AgentToolName,
    data: ResultT,
    evidence_refs: list[str],
    *,
    cache_hit: bool,
    engine_call_count: int,
) -> ToolExecution[ResultT]:
    return ToolExecution(
        name=name,
        result=ToolResult[ResultT](ok=True, data=data, evidence_refs=evidence_refs),
        cache_hit=cache_hit,
        engine_call_count=engine_call_count,
    )


def _analysis_depth() -> int:
    if config.ANALYSIS_PRESET == "fast":
        return max(10, config.DEEP_ANALYSIS_DEPTH - 4)
    if config.ANALYSIS_PRESET == "deep":
        return config.DEEP_ANALYSIS_DEPTH + 4
    return config.DEEP_ANALYSIS_DEPTH


def _profile_id(depth: int, multipv: int) -> str:
    return (
        f"{config.ANALYSIS_PROFILE_VERSION}:agent:{config.ANALYSIS_PRESET}:"
        f"d{depth}:m{multipv}"
    )


def _evidence_ref(kind: str, *parts: str) -> str:
    encoded = "\x00".join(parts).encode("utf-8")
    return f"{kind}:{hashlib.sha256(encoded).hexdigest()[:24]}"


def _bounded_legal_line(
    board: chess.Board,
    ucis: list[str] | tuple[str, ...],
    *,
    max_plies: int,
    expected_sans: list[str] | tuple[str, ...] | None = None,
) -> tuple[list[str], list[str]]:
    replay = board.copy(stack=False)
    bounded_ucis: list[str] = []
    bounded_sans: list[str] = []
    for index, raw_uci in enumerate(list(ucis)[:max_plies]):
        try:
            move = chess.Move.from_uci(str(raw_uci))
        except ValueError as exc:
            raise ArtifactConsistencyError("Saved Engine line contains invalid UCI.") from exc
        if move not in replay.legal_moves:
            raise ArtifactConsistencyError("Saved Engine line contains an illegal move.")
        san = replay.san(move)
        if expected_sans is not None:
            if index >= len(expected_sans) or str(expected_sans[index]) != san:
                raise ArtifactConsistencyError("Saved Engine line has mismatched SAN.")
        bounded_ucis.append(move.uci())
        bounded_sans.append(san)
        replay.push(move)
    return bounded_ucis, bounded_sans


def _move_reference(board: chess.Board, raw: dict[str, Any]) -> MoveReference:
    try:
        move = chess.Move.from_uci(str(raw["uci"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactConsistencyError("Saved move reference is invalid.") from exc
    if move not in board.legal_moves:
        raise ArtifactConsistencyError("Saved move reference is illegal.")
    san = board.san(move)
    if raw.get("san") not in (None, san):
        raise ArtifactConsistencyError("Saved move reference has mismatched SAN.")
    return MoveReference(uci=move.uci(), san=san)


def _white_score_from_raw(
    *,
    cp: int | None,
    mate: int | None,
    side_to_move: chess.Color,
) -> EngineScore:
    sign = 1 if side_to_move == chess.WHITE else -1
    if mate is not None:
        return EngineScore(kind="mate", value=sign * int(mate), pov="white")
    return EngineScore(kind="cp", value=sign * int(cp or 0), pov="white")


def _artifact_score(
    raw: dict[str, Any],
    *,
    board: chess.Board,
    review_side: ReviewSide,
) -> EngineScore:
    kind = str(raw.get("type") or raw.get("kind") or "")
    if kind not in {"cp", "mate"}:
        raise ArtifactConsistencyError("Saved Engine score has no supported type.")
    value = int(raw.get("value") or 0)
    pov = str(raw.get("pov") or "")
    if pov == "mover" or pov == "side_to_move":
        value *= 1 if board.turn == chess.WHITE else -1
        pov = "white"
    elif pov == "review_side":
        value *= 1 if review_side == "white" else -1
        pov = "white"
    if pov not in {"white", "black"}:
        raise ArtifactConsistencyError("Saved Engine score has no explicit color POV.")
    return EngineScore(kind=cast(Any, kind), value=value, pov=cast(Any, pov))


def _artifact_provenance(
    active: ActiveReviewArtifact,
    *,
    cache_suffix: str | None = None,
) -> EngineProvenance:
    analysis = active.analysis
    critical = active.critical
    raw_name = str((analysis.get("engine") or {}).get("name") or "Stockfish")
    profile = analysis.get("profile") or {}
    cache_key = str(analysis.get("cache_key") or "").strip() or None
    if cache_key and cache_suffix:
        cache_key = f"{cache_key}:{cache_suffix}"
    return EngineProvenance(
        engine_name="Stockfish" if raw_name.casefold().startswith("stockfish") else raw_name,
        engine_version=raw_name,
        depth=max(
            1,
            int(
                critical.get("deep_depth")
                or (profile.get("deep") or {}).get("depth")
                or 1
            ),
        ),
        multipv=max(
            1,
            int(
                critical.get("multipv")
                or (profile.get("deep") or {}).get("multipv")
                or 1
            ),
        ),
        analysis_profile_id=str(
            profile.get("id") or f"analysis-v{analysis.get('schema_version', 1)}"
        ),
        cache_key=cache_key,
    )


def _artifact_thresholds(active: ActiveReviewArtifact) -> tuple[float, float, float] | None:
    raw = (((active.analysis.get("profile") or {}).get("critical") or {}).get("thresholds") or [])
    values = tuple(float(value) for value in raw[:3])
    return cast(tuple[float, float, float], values) if len(values) == 3 else None


def _engine_provenance(result: lines.StructuredAnalysis) -> EngineProvenance:
    return EngineProvenance(
        engine_name=result.engine_name,
        engine_version=result.engine_version,
        depth=result.depth,
        multipv=result.multipv,
        analysis_profile_id=_profile_id(result.depth, result.multipv),
        cache_key=result.cache_key,
    )


def _stored_candidate(
    raw: dict[str, Any],
    *,
    board: chess.Board,
    review_side: ReviewSide,
    max_plies: int,
) -> CandidateLine:
    move = _move_reference(board, dict(raw.get("move") or {}))
    line = raw.get("line") or {}
    ucis, sans = _bounded_legal_line(
        board,
        list(line.get("uci") or []),
        max_plies=max_plies,
        expected_sans=list(line.get("san") or []),
    )
    if not ucis or ucis[0] != move.uci:
        raise ArtifactConsistencyError("Saved candidate line does not start with its move.")
    scores = raw.get("scores") or {}
    score_raw = scores.get("review_side") or scores.get("white") or raw.get("eval")
    if not isinstance(score_raw, dict):
        raise ArtifactConsistencyError("Saved candidate has no typed score.")
    return CandidateLine(
        rank=max(1, int(raw.get("rank") or 1)),
        move=move,
        score=_artifact_score(score_raw, board=board, review_side=review_side),
        win_percent_for_review_side=(
            float((raw.get("win_percent") or {}).get("review_side"))
            if (raw.get("win_percent") or {}).get("review_side") is not None
            else None
        ),
        line_uci=ucis,
        line_san=sans,
    )


def _engine_candidate(
    raw: lines.StructuredEngineLine,
    *,
    board: chess.Board,
    rank: int,
    max_plies: int,
) -> CandidateLine:
    ucis, sans = _bounded_legal_line(board, raw.pv_uci, max_plies=max_plies)
    if not ucis:
        raise ArtifactConsistencyError("Engine candidate has no legal principal variation.")
    return CandidateLine(
        rank=rank,
        move=MoveReference(uci=ucis[0], san=sans[0]),
        score=_white_score_from_raw(cp=raw.cp, mate=raw.mate, side_to_move=board.turn),
        win_percent_for_review_side=None,
        line_uci=ucis,
        line_san=sans,
    )


def _recent_moves(
    analysis: dict[str, Any], critical: dict[str, Any]
) -> tuple[list[str], list[str]]:
    try:
        critical_ply = int(critical["ply"])
        critical_fen = str(critical["fen_before"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArtifactConsistencyError("Saved critical position has no valid ply or FEN.") from exc
    moves = [item for item in analysis.get("moves", []) or [] if isinstance(item, dict)]
    stage = next((item for item in moves if int(item.get("ply") or -1) == critical_ply), None)
    if stage is not None:
        if (
            stage.get("fen_before") != critical_fen
            or stage.get("played_move") != critical.get("played_move")
        ):
            raise ArtifactConsistencyError("Saved critical position does not match the mainline.")
    preceding = sorted(
        (item for item in moves if int(item.get("ply") or -1) < critical_ply),
        key=lambda item: int(item.get("ply") or 0),
    )[-8:]
    ucis: list[str] = []
    sans: list[str] = []
    previous_after: str | None = None
    for item in preceding:
        fen_before = str(item.get("fen_before") or "")
        if previous_after is not None and previous_after != fen_before:
            raise ArtifactConsistencyError("Saved mainline FEN chain is inconsistent.")
        board = chess.Board(fen_before)
        move = _move_reference(board, dict(item.get("played_move") or {}))
        board.push(chess.Move.from_uci(move.uci))
        generated_after = board.fen()
        if item.get("fen_after") not in (None, generated_after):
            raise ArtifactConsistencyError("Saved mainline after-FEN is inconsistent.")
        previous_after = generated_after
        ucis.append(move.uci)
        sans.append(move.san)
    if preceding and previous_after != critical_fen:
        raise ArtifactConsistencyError("Saved recent moves do not reach the critical FEN.")
    return ucis, sans


def _opening_lookup_fens(request: LookupOpeningInput) -> tuple[list[str], str]:
    if request.fen is not None:
        return [request.fen], "fen"
    board = chess.Board()
    fens: list[str] = []
    for raw_uci in request.recent_moves_uci:
        move = chess.Move.from_uci(raw_uci)
        if move not in board.legal_moves:
            raise ArtifactConsistencyError("Recent opening moves contain an illegal move.")
        board.push(move)
        fens.append(board.fen())
    return fens, "recent_moves"


def _load_local_player_profile() -> dict[str, Any]:
    data_dir = config.DATA_DIR
    return history.get_profile(history.my_player_id(data_dir), data_dir)


def _compact_profile_stats(raw: dict[str, Any]) -> dict[str, int | float]:
    out: dict[str, int | float] = {}
    for source, target in (("games", "games"), ("avg_accuracy", "avg_accuracy")):
        value = raw.get(source)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[target] = value
    training = raw.get("training") or {}
    if isinstance(training, dict):
        for source, target in (
            ("total", "training_attempts"),
            ("solved", "training_solved"),
            ("solve_rate", "training_solve_rate"),
        ):
            value = training.get(source)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[target] = value
    return out


def _profile_evidence_refs(estimates: list[SkillEstimate]) -> list[str]:
    refs: list[str] = []
    for estimate in estimates:
        for ref in memory.evidence_refs_for_estimate(estimate):
            if ref not in refs:
                refs.append(ref)
    return refs


class AgentTools:
    """Per-run domain tools with exact ownership and fakeable external boundaries."""

    def __init__(
        self,
        *,
        review_scope: ReviewArtifactScope | None = None,
        active_review: ActiveReviewArtifact | None = None,
        analysis_loader: AnalysisLoader = games.load_analysis,
        engine_provider: PositionAnalysisProvider | None = None,
        opening_classifier: OpeningClassifier = openings.classify_from_fens,
        opening_history_fens: list[str] | None = None,
        profile_loader: ProfileLoader | None = None,
        estimate_loader: memory.EstimateLoader | None = None,
        personalization_enabled: bool | None = None,
        depth: int | None = None,
        multipv: int = 3,
        line_plies: int | None = None,
    ) -> None:
        if active_review is not None:
            if review_scope is not None and review_scope != active_review.scope:
                raise ValueError("active_review does not match review_scope")
            review_scope = active_review.scope
        self.review_scope = review_scope
        self._active_review = active_review
        self._analysis_loader = analysis_loader
        self._engine_provider = engine_provider or CorePositionAnalysisProvider()
        self._opening_classifier = opening_classifier
        self._opening_history_fens = list(opening_history_fens or [])
        self._profile_loader = profile_loader or _load_local_player_profile
        self._estimate_loader = estimate_loader
        self.personalization_enabled = (
            config.PERSONALIZE_HISTORY
            if personalization_enabled is None
            else bool(personalization_enabled)
        )
        self.depth = max(1, int(depth if depth is not None else _analysis_depth()))
        self.multipv = max(1, min(3, int(multipv)))
        self.line_plies = max(
            1,
            int(line_plies if line_plies is not None else config.FACT_LINE_PLIES),
        )
        self.executions: list[ToolExecution[Any]] = []

    @property
    def last_execution(self) -> ToolExecution[Any] | None:
        return self.executions[-1] if self.executions else None

    def personalization_available(self) -> bool:
        """Check settings and process health without reading profile or estimate files."""

        return self.personalization_enabled and memory.is_available()

    def learning_memory(self, query: MemoryQuery) -> list[LearningMemoryItem]:
        if not self.personalization_available():
            return []
        return memory.retrieve_memory(
            query,
            personalization_enabled=True,
            estimate_loader=self._estimate_loader,
        )

    def review_recurrence_evidence(self) -> dict[str, dict[str, Any]]:
        """Return bounded, game-backed category recurrence for deterministic prioritization.

        This is a Core-side helper, not a model tool.  Disabled personalization and any profile
        failure both degrade to no recurrence signal without reading unrelated history.
        """
        if not self.personalization_available():
            return {}
        try:
            items = self.learning_memory(
                MemoryQuery(activity="game_review", window="recent", limit=5)
            )
            evidence: dict[str, dict[str, Any]] = {}
            for item in items:
                if item.status != "weakness":
                    continue
                definition = taxonomy.get_skill_definition(item.skill_id)
                aliases = (
                    [item.skill_id, *definition.aliases]
                    if definition is not None
                    else [item.skill_id]
                )
                value = {
                    "count": item.evidence_count,
                    "evidence_refs": list(item.evidence_refs),
                }
                for alias in aliases:
                    evidence[alias] = value
            return evidence
        except Exception:  # noqa: BLE001 - recurrence is an optional prioritization feature
            return {}

    def would_use_engine(self, name: AgentToolName, request: BaseModel) -> bool:
        """Conservatively predict Engine use without performing storage or Engine I/O."""
        return self.estimated_engine_calls(name, request) > 0

    def estimated_engine_calls(self, name: AgentToolName, request: BaseModel) -> int:
        """Reserve the maximum UCI analyses this invocation can perform."""
        if name != "analyze_position" and name != "analyze_move":
            return 0
        if name == "analyze_position":
            if not isinstance(request, AnalyzePositionInput):
                raise ValueError("analyze_position requires AnalyzePositionInput")
            return int(not chess.Board(request.fen).is_game_over(claim_draw=True))
        if not isinstance(request, AnalyzeMoveInput):
            raise ValueError("analyze_move requires AnalyzeMoveInput")
        board = chess.Board(request.fen_before)
        move = chess.Move.from_uci(request.move_uci)
        if move not in board.legal_moves:
            return 0
        active = self._active_review
        if active is None or str(active.critical.get("fen_before") or "") != request.fen_before:
            return 2
        candidates = active.critical.get("candidates", []) or []
        covered = any(
            isinstance(item, dict) and (item.get("move") or {}).get("uci") == move.uci()
            for item in candidates
        )
        if covered or (active.critical.get("played_move") or {}).get("uci") == move.uci():
            return 0
        after = board.copy(stack=False)
        after.push(move)
        return 0 if after.is_game_over(claim_draw=True) else 1

    async def _load_active_review(self) -> tuple[ActiveReviewArtifact | None, ToolError | None]:
        if self._active_review is not None:
            return self._active_review, None
        if self.review_scope is None:
            return None, None
        try:
            analysis = self._analysis_loader(
                self.review_scope.game_id, self.review_scope.review_side
            )
            active = ActiveReviewArtifact.from_analysis(
                analysis,
                self.review_scope.critical_id,
            )
            if active.scope != self.review_scope:
                raise ArtifactConsistencyError("Saved analysis does not match the active scope.")
            self._active_review = active
            return active, None
        except games.GameNotFoundError:
            return None, _tool_error(
                "game_not_found",
                "The saved review is not available for this game and side.",
                recoverable=True,
            )
        except (ArtifactConsistencyError, KeyError, TypeError, ValueError, ValidationError):
            return None, _tool_error(
                "position_not_found",
                "The saved critical position is unavailable or inconsistent.",
                recoverable=False,
            )

    async def _engine_analysis(
        self,
        fen: str,
        *,
        multipv: int,
    ) -> lines.StructuredAnalysis:
        try:
            if getattr(self._engine_provider, "run_in_thread", False):
                return await asyncio.to_thread(
                    self._engine_provider.analyze,
                    fen,
                    depth=self.depth,
                    multipv=multipv,
                )
            return self._engine_provider.analyze(
                fen,
                depth=self.depth,
                multipv=multipv,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise EngineAnalysisFailure(exc) from exc

    @staticmethod
    def _engine_failure(
        name: AgentToolName,
        exc: BaseException,
        *,
        engine_call_count: int = 0,
    ) -> ToolExecution[Any]:
        cause = exc.cause if isinstance(exc, EngineAnalysisFailure) else exc
        if isinstance(cause, (TimeoutError, asyncio.TimeoutError)):
            error = _tool_error(
                "engine_timeout",
                "Stockfish did not finish before the analysis deadline.",
                recoverable=True,
            )
        elif isinstance(cause, ValueError):
            error = _tool_error(
                "invalid_fen",
                "The supplied FEN cannot be analyzed.",
                recoverable=False,
            )
        else:
            error = _tool_error(
                "engine_unavailable",
                "Stockfish is unavailable for this analysis.",
                recoverable=True,
            )
        return _failure(name, error, engine_call_count=engine_call_count)

    async def execute(
        self,
        name: AgentToolName,
        request: BaseModel,
    ) -> ToolExecution[Any]:
        if name == "get_review_context" and isinstance(request, GetReviewContextInput):
            execution = await self._get_review_context(request)
        elif name == "analyze_position" and isinstance(request, AnalyzePositionInput):
            execution = await self._analyze_position(request)
        elif name == "analyze_move" and isinstance(request, AnalyzeMoveInput):
            execution = await self._analyze_move(request)
        elif name == "lookup_opening" and isinstance(request, LookupOpeningInput):
            execution = await self._lookup_opening(request)
        elif name == "get_player_profile" and isinstance(request, GetPlayerProfileInput):
            execution = await self._get_player_profile(request)
        else:
            raise ValueError(f"Unsupported Agent tool or input type: {name}")
        self.executions.append(execution)
        return execution

    async def get_review_context(
        self,
        request: GetReviewContextInput,
    ) -> ToolResult[GetReviewContextResult]:
        execution = await self.execute("get_review_context", request)
        return cast(ToolResult[GetReviewContextResult], execution.result)

    async def analyze_position(
        self,
        request: AnalyzePositionInput,
    ) -> ToolResult[AnalyzePositionResult]:
        execution = await self.execute("analyze_position", request)
        return cast(ToolResult[AnalyzePositionResult], execution.result)

    async def analyze_move(
        self,
        request: AnalyzeMoveInput,
    ) -> ToolResult[AnalyzeMoveResult]:
        execution = await self.execute("analyze_move", request)
        return cast(ToolResult[AnalyzeMoveResult], execution.result)

    async def lookup_opening(
        self,
        request: LookupOpeningInput,
    ) -> ToolResult[LookupOpeningResult]:
        execution = await self.execute("lookup_opening", request)
        return cast(ToolResult[LookupOpeningResult], execution.result)

    async def get_player_profile(
        self,
        request: GetPlayerProfileInput,
    ) -> ToolResult[GetPlayerProfileResult]:
        execution = await self.execute("get_player_profile", request)
        return cast(ToolResult[GetPlayerProfileResult], execution.result)

    async def _lookup_opening(
        self,
        request: LookupOpeningInput,
    ) -> ToolExecution[LookupOpeningResult]:
        try:
            fens, lookup_kind = _opening_lookup_fens(request)
            if (
                request.fen is not None
                and self._opening_history_fens
                and self._opening_history_fens[-1] == request.fen
            ):
                fens = list(self._opening_history_fens)
                lookup_kind = "validated_game_history"
            eco, name = self._opening_classifier(fens)
            recognized = bool(eco or name)
            data = LookupOpeningResult(
                eco=eco,
                name=name,
                classification="recognized" if recognized else "unrecognized",
                metadata={
                    "source": "local_eco",
                    "lookup": lookup_kind,
                    "positions_checked": len(fens),
                },
            )
        except ArtifactConsistencyError:
            return cast(
                ToolExecution[LookupOpeningResult],
                _failure(
                    "lookup_opening",
                    _tool_error(
                        "illegal_move",
                        "The recent moves do not form a legal opening sequence.",
                        recoverable=False,
                    ),
                ),
            )
        except Exception:  # noqa: BLE001 - local boundary degrades to a stable tool error
            return cast(
                ToolExecution[LookupOpeningResult],
                _failure(
                    "lookup_opening",
                    _tool_error(
                        "position_not_found",
                        "The local opening book could not be read for this position.",
                        recoverable=True,
                    ),
                ),
            )
        evidence = (
            [_evidence_ref("opening", fens[-1], str(eco or ""), str(name or ""))]
            if recognized and fens
            else []
        )
        return _success(
            "lookup_opening",
            data,
            evidence,
            cache_hit=True,
            engine_call_count=0,
        )

    async def _get_player_profile(
        self,
        request: GetPlayerProfileInput,
    ) -> ToolExecution[GetPlayerProfileResult]:
        if not self.personalization_enabled:
            return cast(
                ToolExecution[GetPlayerProfileResult],
                _failure(
                    "get_player_profile",
                    _tool_error(
                        "profile_unavailable",
                        "Personalized coaching is disabled in local settings.",
                        recoverable=False,
                    ),
                ),
            )
        if not memory.is_available():
            status = memory.health_status()
            operation = str(status.get("operation") or "startup synchronization")
            return cast(
                ToolExecution[GetPlayerProfileResult],
                _failure(
                    "get_player_profile",
                    _tool_error(
                        "profile_unavailable",
                        f"Canonical learning memory is unavailable after {operation}; "
                        "Engine Review remains available.",
                        recoverable=True,
                    ),
                ),
            )
        try:
            raw_focus = [*request.focus_skill_ids, *request.focus_categories]
            resolved_focus = [taxonomy.resolve_skill_id(value) for value in raw_focus]
            if any(value is None for value in resolved_focus):
                estimates: list[SkillEstimate] = []
            elif resolved_focus:
                estimates = []
                for skill_id in dict.fromkeys(cast(list[str], resolved_focus)):
                    estimates.extend(
                        memory.retrieve_estimates(
                            MemoryQuery(
                                activity="training_planning",
                                focus_skill_id=skill_id,
                                window="recent",
                                limit=1,
                            ),
                            personalization_enabled=True,
                            estimate_loader=self._estimate_loader,
                        )
                    )
                estimates = estimates[: request.limit]
            else:
                estimates = memory.retrieve_estimates(
                    MemoryQuery(
                        activity="training_planning",
                        window="recent",
                        limit=request.limit,
                    ),
                    personalization_enabled=True,
                    estimate_loader=self._estimate_loader,
                )

            try:
                profile = self._profile_loader()
            except Exception:  # noqa: BLE001 - canonical estimates remain usable without legacy stats
                profile = {}
            if not isinstance(profile, dict):
                profile = {}
            analyzed_games = max(
                max((estimate.distinct_games for estimate in estimates), default=0),
                max(0, int(profile.get("games_analyzed") or 0)),
            )
            recent = profile.get("recent") or {}
            lifetime = profile.get("lifetime") or {}
            if not isinstance(recent, dict):
                recent = {}
            if not isinstance(lifetime, dict):
                lifetime = {}
            training = recent.get("training") or {}
            training_rate = (
                float(training["solve_rate"])
                if isinstance(training, dict) and training.get("solve_rate") is not None
                else None
            )
            data = GetPlayerProfileResult(
                analyzed_games=analyzed_games,
                relevant_estimates=estimates,
                training_success_rate=training_rate,
                recent=_compact_profile_stats(recent),
                lifetime=_compact_profile_stats(lifetime),
            )
        except Exception:  # noqa: BLE001 - storage/profile failures use the typed degradation path
            return cast(
                ToolExecution[GetPlayerProfileResult],
                _failure(
                    "get_player_profile",
                    _tool_error(
                        "profile_unavailable",
                        "The local player profile could not be loaded.",
                        recoverable=True,
                    ),
                ),
            )
        return _success(
            "get_player_profile",
            data,
            _profile_evidence_refs(estimates),
            cache_hit=True,
            engine_call_count=0,
        )

    async def _get_review_context(
        self,
        request: GetReviewContextInput,
    ) -> ToolExecution[GetReviewContextResult]:
        scope = self.review_scope
        if (
            scope is None
            or request.game_id != scope.game_id
            or request.review_side != scope.review_side
        ):
            return cast(
                ToolExecution[GetReviewContextResult],
                _failure(
                    "get_review_context",
                    _tool_error(
                        "game_not_found",
                        "The requested review is outside the active Agent session.",
                        recoverable=False,
                    ),
                ),
            )
        if request.critical_id != scope.critical_id:
            return cast(
                ToolExecution[GetReviewContextResult],
                _failure(
                    "get_review_context",
                    _tool_error(
                        "position_not_found",
                        "The requested position is not the active review position.",
                        recoverable=False,
                    ),
                ),
            )
        active, error = await self._load_active_review()
        if error is not None or active is None:
            return cast(
                ToolExecution[GetReviewContextResult],
                _failure(
                    "get_review_context",
                    error
                    or _tool_error(
                        "position_not_found",
                        "The active review position is unavailable.",
                        recoverable=True,
                    ),
                ),
            )
        try:
            critical = active.critical
            analysis = active.analysis
            board = chess.Board(str(critical["fen_before"]))
            reference = PositionReference(
                game_id=scope.game_id,
                review_side=scope.review_side,
                critical_id=scope.critical_id,
                ply=int(critical["ply"]),
                fen=board.fen(),
            )
            recent_uci, recent_san = _recent_moves(analysis, critical)
            played_move = _move_reference(board, dict(critical.get("played_move") or {}))
            raw_candidates = [
                item for item in critical.get("candidates", []) or [] if isinstance(item, dict)
            ][:3]
            candidates = [
                _stored_candidate(
                    item,
                    board=board,
                    review_side=scope.review_side,
                    max_plies=self.line_plies,
                )
                for item in raw_candidates
            ]
            if not candidates:
                raise ArtifactConsistencyError("Saved critical position has no candidate moves.")
            facts = critical.get("facts") or {}
            if not isinstance(facts, dict):
                raise ArtifactConsistencyError("Saved critical position has invalid facts.")
            data = GetReviewContextResult(
                reference=reference,
                position=PositionContext(
                    fen=board.fen(),
                    recent_moves_uci=recent_uci,
                    recent_moves_san=recent_san,
                    selected_move_uci=played_move.uci,
                    selected_move_san=played_move.san,
                    reference=reference,
                ),
                played_move=played_move,
                best_move=candidates[0].move,
                classification=str(critical.get("classification") or "unclassified"),
                criticality=str(critical.get("criticality") or "critical"),
                candidates=candidates,
                facts=facts,
                provenance=_artifact_provenance(active),
            )
        except (ArtifactConsistencyError, KeyError, TypeError, ValueError, ValidationError):
            return cast(
                ToolExecution[GetReviewContextResult],
                _failure(
                    "get_review_context",
                    _tool_error(
                        "position_not_found",
                        "The saved critical position is unavailable or inconsistent.",
                        recoverable=False,
                    ),
                    cache_hit=True,
                ),
            )
        evidence = f"review:{scope.game_id}:{scope.review_side}:{scope.critical_id}"
        return _success(
            "get_review_context",
            data,
            [evidence],
            cache_hit=True,
            engine_call_count=0,
        )

    async def _analyze_position(
        self,
        request: AnalyzePositionInput,
    ) -> ToolExecution[AnalyzePositionResult]:
        try:
            board = chess.Board(request.fen)
            result = await self._engine_analysis(request.fen, multipv=self.multipv)
            candidates = [
                _engine_candidate(
                    raw,
                    board=board,
                    rank=rank,
                    max_plies=self.line_plies,
                )
                for rank, raw in enumerate(result.lines[:3], start=1)
            ]
            data = AnalyzePositionResult(
                fen=request.fen,
                candidates=candidates,
                provenance=_engine_provenance(result),
            )
        except (ArtifactConsistencyError, ValidationError):
            return cast(
                ToolExecution[AnalyzePositionResult],
                _failure(
                    "analyze_position",
                    _tool_error(
                        "engine_unavailable",
                        "Stockfish returned an invalid analysis result.",
                        recoverable=True,
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - mapped to the stable tool error surface
            return cast(
                ToolExecution[AnalyzePositionResult],
                self._engine_failure(
                    "analyze_position",
                    exc,
                    engine_call_count=1 if isinstance(exc, EngineAnalysisFailure) else 0,
                ),
            )
        evidence = _evidence_ref("engine-position", request.fen, str(self.depth), str(self.multipv))
        return _success(
            "analyze_position",
            data,
            [evidence],
            cache_hit=result.cache_hit,
            engine_call_count=result.engine_call_count,
        )

    def _artifact_move_result(
        self,
        active: ActiveReviewArtifact,
        board: chess.Board,
        move: chess.Move,
    ) -> AnalyzeMoveResult | None:
        critical = active.critical
        raw_candidates = [
            item for item in critical.get("candidates", []) or [] if isinstance(item, dict)
        ]
        selected = next(
            (item for item in raw_candidates if (item.get("move") or {}).get("uci") == move.uci()),
            None,
        )
        played = dict(critical.get("played_move") or {})
        is_played = played.get("uci") == move.uci()
        if selected is None and not is_played:
            return None

        move_ref = MoveReference(uci=move.uci(), san=board.san(move))
        best_alternative = next(
            (
                _move_reference(board, dict(item.get("move") or {}))
                for item in raw_candidates
                if (item.get("move") or {}).get("uci") != move.uci()
            ),
            None,
        )
        if selected is not None:
            stored = _stored_candidate(
                selected,
                board=board,
                review_side=active.scope.review_side,
                max_plies=self.line_plies,
            )
            score_raw = (selected.get("scores") or {}).get("white") or selected.get("eval")
            if not isinstance(score_raw, dict):
                raise ArtifactConsistencyError("Saved candidate has no white-POV score.")
            score = _artifact_score(score_raw, board=board, review_side=active.scope.review_side)
            continuation_uci = stored.line_uci[1:]
            continuation_san = stored.line_san[1:]
            top_value = (raw_candidates[0].get("win_percent") or {}).get("mover")
            selected_value = (selected.get("win_percent") or {}).get("mover")
            top_win = 50.0 if top_value is None else float(top_value)
            selected_win = 50.0 if selected_value is None else float(selected_value)
            classification = classify(
                top_win,
                selected_win,
                is_best=int(selected.get("rank") or 0) == 1,
                thresholds=_artifact_thresholds(active),
            )
        else:
            played_scores = critical.get("played_move_scores") or {}
            score_raw = played_scores.get("white") or critical.get("played_move_eval")
            if not isinstance(score_raw, dict):
                raise ArtifactConsistencyError("Saved played move has no white-POV score.")
            score = _artifact_score(score_raw, board=board, review_side=active.scope.review_side)
            line = critical.get("played_line") or {}
            ucis, sans = _bounded_legal_line(
                board,
                list(line.get("uci") or []),
                max_plies=self.line_plies,
                expected_sans=list(line.get("san") or []),
            )
            if not ucis or ucis[0] != move.uci():
                raise ArtifactConsistencyError("Saved played line does not start with its move.")
            continuation_uci = ucis[1:]
            continuation_san = sans[1:]
            classification = str(critical.get("classification") or "unclassified")

        return AnalyzeMoveResult(
            fen_before=board.fen(),
            legal=True,
            move=move_ref,
            score=score,
            best_alternative=best_alternative,
            classification=classification,
            continuation_uci=continuation_uci,
            continuation_san=continuation_san,
            provenance=_artifact_provenance(active, cache_suffix=move.uci()),
        )

    async def _analyze_move(
        self,
        request: AnalyzeMoveInput,
    ) -> ToolExecution[AnalyzeMoveResult]:
        name: AgentToolName = "analyze_move"
        board = chess.Board(request.fen_before)
        move = chess.Move.from_uci(request.move_uci)
        # Legality is always resolved before storage or Engine access.
        if move not in board.legal_moves:
            return cast(
                ToolExecution[AnalyzeMoveResult],
                _failure(
                    name,
                    _tool_error(
                        "illegal_move",
                        "The move is not legal in the supplied position.",
                        recoverable=False,
                    ),
                ),
            )

        active, _load_error = await self._load_active_review()
        exact_active = (
            active
            if active is not None
            and str(active.critical.get("fen_before") or "") == request.fen_before
            else None
        )
        if exact_active is not None:
            try:
                cached = self._artifact_move_result(exact_active, board, move)
            except (ArtifactConsistencyError, KeyError, TypeError, ValueError, ValidationError):
                return cast(
                    ToolExecution[AnalyzeMoveResult],
                    _failure(
                        name,
                        _tool_error(
                            "position_not_found",
                            "The saved position is inconsistent and cannot be reused.",
                            recoverable=False,
                        ),
                        cache_hit=True,
                    ),
                )
            if cached is not None:
                evidence = (
                    f"review:{exact_active.scope.game_id}:{exact_active.scope.review_side}:"
                    f"{exact_active.scope.critical_id}:move:{move.uci()}"
                )
                return _success(
                    name,
                    cached,
                    [evidence],
                    cache_hit=True,
                    engine_call_count=0,
                )

        engine_results: list[lines.StructuredAnalysis] = []
        base: lines.StructuredAnalysis | None = None
        base_lines: tuple[lines.StructuredEngineLine, ...] = ()
        best_alternative: MoveReference | None = None
        win_before: float
        provenance: EngineProvenance

        try:
            if exact_active is not None:
                raw_candidates = [
                    item
                    for item in exact_active.critical.get("candidates", []) or []
                    if isinstance(item, dict)
                ]
                if not raw_candidates:
                    raise ArtifactConsistencyError("Saved position has no best candidate.")
                best_raw = raw_candidates[0]
                best_alternative = _move_reference(board, dict(best_raw.get("move") or {}))
                best_value = (best_raw.get("win_percent") or {}).get("mover")
                win_before = 50.0 if best_value is None else float(best_value)
                provenance = _artifact_provenance(exact_active)
            else:
                base = await self._engine_analysis(request.fen_before, multipv=self.multipv)
                engine_results.append(base)
                base_lines = base.lines[:3]
                if not base_lines:
                    raise RuntimeError("Engine returned no candidate lines.")
                best_candidate = _engine_candidate(
                    base_lines[0], board=board, rank=1, max_plies=self.line_plies
                )
                best_alternative = best_candidate.move
                win_before = float(base_lines[0].win_percent)
                provenance = _engine_provenance(base)

                selected_rank = next(
                    (
                        rank
                        for rank, line in enumerate(base_lines, start=1)
                        if line.pv_uci and line.pv_uci[0] == move.uci()
                    ),
                    None,
                )
                if selected_rank is not None:
                    selected_line = base_lines[selected_rank - 1]
                    selected_candidate = _engine_candidate(
                        selected_line,
                        board=board,
                        rank=selected_rank,
                        max_plies=self.line_plies,
                    )
                    alternative = next(
                        (
                            _engine_candidate(
                                raw,
                                board=board,
                                rank=rank,
                                max_plies=self.line_plies,
                            ).move
                            for rank, raw in enumerate(base_lines, start=1)
                            if raw.pv_uci and raw.pv_uci[0] != move.uci()
                        ),
                        None,
                    )
                    data = AnalyzeMoveResult(
                        fen_before=request.fen_before,
                        legal=True,
                        move=MoveReference(uci=move.uci(), san=board.san(move)),
                        score=selected_candidate.score,
                        best_alternative=alternative,
                        classification=classify(
                            win_before,
                            float(selected_line.win_percent),
                            is_best=selected_rank == 1,
                        ),
                        continuation_uci=selected_candidate.line_uci[1:],
                        continuation_san=selected_candidate.line_san[1:],
                        provenance=provenance,
                    )
                    evidence = _evidence_ref(
                        "engine-move", request.fen_before, move.uci(), str(self.depth)
                    )
                    return _success(
                        name,
                        data,
                        [evidence],
                        cache_hit=all(item.cache_hit for item in engine_results),
                        engine_call_count=sum(item.engine_call_count for item in engine_results),
                    )

            after_board = board.copy(stack=False)
            after_board.push(move)
            if after_board.is_game_over(claim_draw=True):
                outcome = after_board.outcome(claim_draw=True)
                if outcome is None or outcome.winner is None:
                    score = EngineScore(kind="cp", value=0, pov="white")
                    win_after = 50.0
                else:
                    score = EngineScore(
                        kind="mate",
                        value=1 if outcome.winner == chess.WHITE else -1,
                        pov="white",
                    )
                    win_after = 100.0 if outcome.winner == board.turn else 0.0
                continuation_uci: list[str] = []
                continuation_san: list[str] = []
            else:
                after = await self._engine_analysis(after_board.fen(), multipv=1)
                engine_results.append(after)
                if not after.lines:
                    raise RuntimeError("Engine returned no continuation line.")
                response = after.lines[0]
                continuation_uci, continuation_san = _bounded_legal_line(
                    after_board,
                    response.pv_uci,
                    max_plies=self.line_plies,
                )
                score = _white_score_from_raw(
                    cp=response.cp,
                    mate=response.mate,
                    side_to_move=after_board.turn,
                )
                win_after = 100.0 - float(response.win_percent)
                provenance = _engine_provenance(after)

            is_best = best_alternative is not None and best_alternative.uci == move.uci()
            data = AnalyzeMoveResult(
                fen_before=request.fen_before,
                legal=True,
                move=MoveReference(uci=move.uci(), san=board.san(move)),
                score=score,
                best_alternative=None if is_best else best_alternative,
                classification=classify(
                    win_before,
                    win_after,
                    is_best=is_best,
                    thresholds=(
                        _artifact_thresholds(exact_active)
                        if exact_active is not None
                        else None
                    ),
                ),
                continuation_uci=continuation_uci,
                continuation_san=continuation_san,
                provenance=provenance,
            )
        except (ArtifactConsistencyError, ValidationError):
            error = (
                _tool_error(
                    "position_not_found",
                    "The active review position is inconsistent and cannot be analyzed.",
                    recoverable=False,
                )
                if exact_active is not None
                else _tool_error(
                    "engine_unavailable",
                    "Stockfish returned an invalid analysis result.",
                    recoverable=True,
                )
            )
            return cast(
                ToolExecution[AnalyzeMoveResult],
                _failure(
                    name,
                    error,
                    cache_hit=bool(exact_active),
                    engine_call_count=sum(item.engine_call_count for item in engine_results),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - mapped to the stable tool error surface
            return cast(
                ToolExecution[AnalyzeMoveResult],
                self._engine_failure(
                    name,
                    exc,
                    engine_call_count=(
                        sum(item.engine_call_count for item in engine_results)
                        + (1 if isinstance(exc, EngineAnalysisFailure) else 0)
                    ),
                ),
            )

        evidence = _evidence_ref("engine-move", request.fen_before, move.uci(), str(self.depth))
        return _success(
            name,
            data,
            [evidence],
            cache_hit=bool(engine_results) and all(item.cache_hit for item in engine_results),
            engine_call_count=sum(item.engine_call_count for item in engine_results),
        )


__all__ = [
    "ActiveReviewArtifact",
    "AgentTools",
    "CorePositionAnalysisProvider",
    "PositionAnalysisProvider",
    "ReviewArtifactScope",
    "ToolExecution",
]
