from __future__ import annotations

import copy
import unittest
from collections import deque
from typing import Any
from unittest.mock import patch

import chess

from server.core import lines
from server.core.agent.models import (
    AnalyzeMoveInput,
    AnalyzePositionInput,
    GetReviewContextInput,
)
from server.core.agent.tools import (
    ActiveReviewArtifact,
    AgentTools,
    ReviewArtifactScope,
)
from tests.backend.fixtures import CRITICAL_ID, GAME_ID, TACTICAL_FEN, analysis_artifact


START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4_FEN = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
CHECKMATE_FEN = "7k/6Q1/6K1/8/8/8/8/8 b - - 0 1"


def structured(
    fen: str,
    raw_lines: list[tuple[int | None, int | None, list[str], float]],
    *,
    depth: int = 18,
    multipv: int = 3,
    cache_hit: bool = False,
    engine_calls: int = 1,
) -> lines.StructuredAnalysis:
    return lines.StructuredAnalysis(
        fen=fen,
        depth=depth,
        multipv=multipv,
        lines=tuple(
            lines.StructuredEngineLine(
                cp=cp,
                mate=mate,
                pv_uci=tuple(pv),
                win_percent=win,
            )
            for cp, mate, pv, win in raw_lines
        ),
        engine_name="Stockfish",
        engine_version="Stockfish fixture-17",
        cache_key=f"cache-{len(raw_lines)}-{multipv}",
        cache_hit=cache_hit,
        engine_call_count=engine_calls,
    )


class RecordingProvider:
    def __init__(self, results: list[lines.StructuredAnalysis | Exception] | None = None) -> None:
        self.results = deque(results or [])
        self.calls: list[tuple[str, int, int]] = []

    def analyze(self, fen: str, *, depth: int, multipv: int) -> lines.StructuredAnalysis:
        self.calls.append((fen, depth, multipv))
        if not self.results:
            raise AssertionError("Unexpected Engine call")
        result = self.results.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def active_artifact(artifact: dict[str, Any] | None = None) -> ActiveReviewArtifact:
    return ActiveReviewArtifact.from_analysis(artifact or analysis_artifact(), CRITICAL_ID)


class AgentReviewToolTests(unittest.IsolatedAsyncioTestCase):
    def test_live_analysis_reference_resolves_only_matching_server_result(self) -> None:
        result = structured(
            START_FEN,
            [(25, None, ["e2e4", "e7e5"], 52.0)],
            depth=22,
            multipv=3,
        )
        tools = AgentTools(line_plies=2)

        with patch("server.core.agent.tools.lines.get_live_analysis", return_value=result):
            loaded = tools.load_live_analysis(START_FEN, result.cache_key)
            mismatched = tools.load_live_analysis(AFTER_E4_FEN, result.cache_key)

        self.assertIsNotNone(loaded)
        assert loaded is not None and loaded.data is not None
        self.assertEqual("e2e4", loaded.data.candidates[0].move.uci)
        self.assertEqual(["e2e4", "e7e5"], loaded.data.candidates[0].line_uci)
        self.assertIsNone(mismatched)

    async def test_get_review_context_is_owned_bounded_and_engine_free(self) -> None:
        provider = RecordingProvider()
        tools = AgentTools(
            active_review=active_artifact(),
            engine_provider=provider,
            line_plies=1,
        )
        request = GetReviewContextInput(
            game_id=GAME_ID,
            review_side="white",
            critical_id=CRITICAL_ID,
        )

        execution = await tools.execute("get_review_context", request)

        self.assertTrue(execution.result.ok)
        self.assertTrue(execution.cache_hit)
        self.assertEqual(0, execution.engine_calls)
        self.assertEqual([], provider.calls)
        data = execution.result.data
        assert data is not None
        self.assertEqual(TACTICAL_FEN, data.reference.fen)
        self.assertEqual(1, data.reference.ply)
        self.assertEqual("h1h2", data.position.selected_move_uci)
        self.assertEqual(3, len(data.candidates))
        self.assertTrue(all(len(candidate.line_uci) == 1 for candidate in data.candidates))
        self.assertEqual("white", data.candidates[0].score.pov)
        self.assertEqual(
            [f"review:{GAME_ID}:white:{CRITICAL_ID}"],
            execution.result.evidence_refs,
        )

        denied = await tools.get_review_context(
            GetReviewContextInput(
                game_id=GAME_ID,
                review_side="white",
                critical_id="ply-99",
            )
        )
        self.assertFalse(denied.ok)
        self.assertEqual("position_not_found", denied.error.code)

    async def test_review_context_can_load_only_its_declared_scope(self) -> None:
        artifact = analysis_artifact()
        calls: list[tuple[str, str | None]] = []

        def loader(game_id: str, review_side: str | None) -> dict[str, Any]:
            calls.append((game_id, review_side))
            return artifact

        scope = ReviewArtifactScope(GAME_ID, "white", CRITICAL_ID)
        tools = AgentTools(review_scope=scope, analysis_loader=loader, engine_provider=RecordingProvider())
        result = await tools.get_review_context(
            GetReviewContextInput(game_id=GAME_ID, review_side="white", critical_id=CRITICAL_ID)
        )

        self.assertTrue(result.ok)
        self.assertEqual([(GAME_ID, "white")], calls)
        self.assertFalse(
            tools.would_use_engine(
                "analyze_move",
                AnalyzeMoveInput(fen_before=TACTICAL_FEN, move_uci="d1d3"),
            )
        )


class AgentMoveToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_illegal_move_rejects_before_artifact_or_engine_access(self) -> None:
        provider = RecordingProvider()
        loader_calls: list[tuple[str, str | None]] = []

        def loader(game_id: str, review_side: str | None) -> dict[str, Any]:
            loader_calls.append((game_id, review_side))
            raise AssertionError("Illegal moves must not load artifacts")

        tools = AgentTools(
            review_scope=ReviewArtifactScope(GAME_ID, "white", CRITICAL_ID),
            analysis_loader=loader,
            engine_provider=provider,
        )
        request = AnalyzeMoveInput(fen_before=START_FEN, move_uci="d1d5")

        self.assertFalse(tools.would_use_engine("analyze_move", request))
        execution = await tools.execute("analyze_move", request)

        self.assertFalse(execution.result.ok)
        self.assertEqual("illegal_move", execution.result.error.code)
        self.assertEqual(0, execution.engine_calls)
        self.assertEqual([], loader_calls)
        self.assertEqual([], provider.calls)

    async def test_candidate_and_played_line_reuse_saved_artifact(self) -> None:
        provider = RecordingProvider()
        candidate_tools = AgentTools(active_review=active_artifact(), engine_provider=provider)
        candidate_request = AnalyzeMoveInput(fen_before=TACTICAL_FEN, move_uci="d1d3")

        self.assertFalse(candidate_tools.would_use_engine("analyze_move", candidate_request))
        candidate = await candidate_tools.execute("analyze_move", candidate_request)

        self.assertTrue(candidate.result.ok)
        self.assertTrue(candidate.cache_hit)
        self.assertEqual(0, candidate.engine_calls)
        candidate_data = candidate.result.data
        assert candidate_data is not None
        self.assertEqual("best", candidate_data.classification)
        self.assertEqual(["e8f7"], candidate_data.continuation_uci)
        self.assertEqual("white", candidate_data.score.pov)

        artifact = copy.deepcopy(analysis_artifact())
        critical = artifact["critical_positions"][0]
        critical["candidates"] = critical["candidates"][:2]
        played_tools = AgentTools(active_review=active_artifact(artifact), engine_provider=provider)
        played_request = AnalyzeMoveInput(fen_before=TACTICAL_FEN, move_uci="h1h2")

        self.assertFalse(played_tools.would_use_engine("analyze_move", played_request))
        played = await played_tools.execute("analyze_move", played_request)

        self.assertTrue(played.result.ok)
        self.assertEqual(0, played.engine_calls)
        played_data = played.result.data
        assert played_data is not None
        self.assertEqual("blunder", played_data.classification)
        self.assertEqual(["d3d1"], played_data.continuation_uci)
        self.assertEqual([], provider.calls)

    async def test_uncovered_active_move_uses_one_after_position_analysis(self) -> None:
        after = chess.Board(TACTICAL_FEN)
        after.push_uci("h1g1")
        provider = RecordingProvider(
            [
                structured(
                    after.fen(),
                    [(50, None, ["e8f8"], 55.0)],
                    multipv=1,
                )
            ]
        )
        tools = AgentTools(
            active_review=active_artifact(),
            engine_provider=provider,
            depth=18,
        )
        request = AnalyzeMoveInput(fen_before=TACTICAL_FEN, move_uci="h1g1")

        self.assertTrue(tools.would_use_engine("analyze_move", request))
        execution = await tools.execute("analyze_move", request)

        self.assertTrue(execution.result.ok)
        self.assertFalse(execution.cache_hit)
        self.assertEqual(1, execution.engine_calls)
        self.assertEqual([(after.fen(), 18, 1)], provider.calls)
        data = execution.result.data
        assert data is not None
        self.assertEqual("Rg1", data.move.san)
        self.assertEqual("white", data.score.pov)
        self.assertEqual(-50, data.score.value)
        self.assertEqual(["e8f8"], data.continuation_uci)
        self.assertEqual("d1d3", data.best_alternative.uci)

    async def test_standalone_move_reuses_selected_multipv_line(self) -> None:
        provider = RecordingProvider(
            [
                structured(
                    START_FEN,
                    [
                        (30, None, ["e2e4", "e7e5"], 53.0),
                        (20, None, ["d2d4", "d7d5"], 52.0),
                        (10, None, ["g1f3", "g8f6"], 51.0),
                    ],
                )
            ]
        )
        tools = AgentTools(engine_provider=provider, depth=18, line_plies=2)
        request = AnalyzeMoveInput(fen_before=START_FEN, move_uci="e2e4")

        execution = await tools.execute("analyze_move", request)

        self.assertTrue(execution.result.ok)
        self.assertEqual(1, execution.engine_calls)
        self.assertEqual([(START_FEN, 18, 3)], provider.calls)
        data = execution.result.data
        assert data is not None
        self.assertEqual("best", data.classification)
        self.assertEqual(30, data.score.value)
        self.assertEqual(["e7e5"], data.continuation_uci)
        self.assertEqual("d2d4", data.best_alternative.uci)


class AgentPositionToolTests(unittest.IsolatedAsyncioTestCase):
    def test_terminal_structured_analysis_does_not_start_engine(self) -> None:
        with patch("server.core.lines.engine.info") as info, patch(
            "server.core.lines.engine.analyse"
        ) as analyse:
            result = lines.structured_analysis(CHECKMATE_FEN, depth=18, multipv=3)

        info.assert_not_called()
        analyse.assert_not_called()
        self.assertEqual((), result.lines)
        self.assertEqual(0, result.engine_call_count)
        self.assertEqual("python-chess", result.engine_name)
        lines.remember_live_analysis(result)
        self.assertEqual(result, lines.get_live_analysis(result.cache_key))
        self.assertEqual(
            0,
            AgentTools().estimated_engine_calls(
                "analyze_position",
                AnalyzePositionInput(fen=CHECKMATE_FEN, purpose="explain_position"),
            ),
        )

    async def test_position_analysis_bounds_candidates_and_uses_white_pov(self) -> None:
        provider = RecordingProvider(
            [
                structured(
                    AFTER_E4_FEN,
                    [
                        (100, None, ["e7e5", "g1f3"], 60.0),
                        (None, -3, ["g8f6", "b1c3"], 0.0),
                        (20, None, ["d7d5", "e4d5"], 52.0),
                        (10, None, ["b8c6"], 51.0),
                    ],
                    multipv=3,
                )
            ]
        )
        tools = AgentTools(engine_provider=provider, depth=17, line_plies=1)
        request = AnalyzePositionInput(fen=AFTER_E4_FEN, purpose="compare_candidates")

        execution = await tools.execute("analyze_position", request)

        self.assertTrue(execution.result.ok)
        self.assertEqual(1, execution.engine_calls)
        self.assertEqual([(AFTER_E4_FEN, 17, 3)], provider.calls)
        data = execution.result.data
        assert data is not None
        self.assertEqual(3, len(data.candidates))
        self.assertTrue(all(len(item.line_uci) == 1 for item in data.candidates))
        self.assertEqual("white", data.candidates[0].score.pov)
        self.assertEqual(-100, data.candidates[0].score.value)
        self.assertEqual("mate", data.candidates[1].score.kind)
        self.assertEqual(3, data.candidates[1].score.value)
        self.assertEqual(3, data.provenance.multipv)

    async def test_engine_timeout_has_stable_error_and_attempt_metadata(self) -> None:
        provider = RecordingProvider([TimeoutError("sensitive provider detail")])
        tools = AgentTools(engine_provider=provider, depth=18)

        execution = await tools.execute(
            "analyze_position",
            AnalyzePositionInput(fen=START_FEN, purpose="compare_candidates"),
        )

        self.assertFalse(execution.result.ok)
        self.assertEqual("engine_timeout", execution.result.error.code)
        self.assertNotIn("sensitive", execution.result.error.message)
        self.assertEqual(1, execution.engine_calls)


if __name__ == "__main__":
    unittest.main()
