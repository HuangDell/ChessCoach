from __future__ import annotations

import copy
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_BOOTSTRAP = tempfile.TemporaryDirectory(prefix="chesscoach-positive-bootstrap-")
_OLD_DATA = os.environ.get("CHESSCOACH_DATA_DIR")
os.environ["CHESSCOACH_DATA_DIR"] = _BOOTSTRAP.name

import chess

from server import config
from server.core import analysis_cache, engine, game_analysis, training
from server.core.critical import select_critical_moves
from server.core.evaluation import CLASSIFICATIONS, classify
from server.core.explanation.builder import build_request
from server.core.facts_projection import project_facts
from server.core.learning.taxonomy import map_analysis_position
from server.core.lines import _settle_leaf
from server.core.positive_moves import classify_positive
from server.core.storage import load_analysis, store_analysis


def tearDownModule():
    if _OLD_DATA is None:
        os.environ.pop("CHESSCOACH_DATA_DIR", None)
    else:
        os.environ["CHESSCOACH_DATA_DIR"] = _OLD_DATA
    _BOOTSTRAP.cleanup()


PGN = '[White "Fixture"]\n[Black "Opponent"]\n\n1. e4 e5 2. Nf3 *'


class FakeEngine:
    def __init__(self):
        board = chess.Board()
        self.choices = {}
        self.calls = []
        for san in ("e4", "e5", "Nf3", "Nc6"):
            move = board.parse_san(san)
            self.choices[board.fen()] = move
            board.push(move)

    def __call__(self, fen, *, depth, multipv=1):
        board = chess.Board(fen)
        self.calls.append((fen, depth, multipv))
        legal = list(board.legal_moves)
        best = self.choices.get(fen, legal[0])
        choices = [best, *(m for m in legal if m != best)][:multipv]
        return engine.AnalysisResult(fen, depth, [
            engine.EngineLine(0 if rank == 0 else -200, None, [move.uci()])
            for rank, move in enumerate(choices)
        ])


class PositiveClassificationTests(unittest.TestCase):
    def position(self, board, played, gap=13, after=50):
        other = next(m for m in board.legal_moves if m.uci() != played)
        return {
            "played_move": {"uci": played}, "deep_depth": 4,
            "played_move_scores": {"mover": {"type": "cp", "value": 0}},
            "verified_win_after": after,
            "candidates": [
                {"move": {"uci": played}, "win_percent": {"mover": 50}, "win_gap_from_best": 0},
                {"move": {"uci": other.uci()}, "win_percent": {"mover": 50-gap}, "win_gap_from_best": gap},
            ],
        }

    def test_basic_boundaries_and_score_improvement(self):
        for before, after, best, expected in [
            (50, 51, False, "excellent"), (50, 50, True, "best"),
            (50, 48.01, False, "excellent"), (50, 48, False, "good"),
            (50, 45, False, "inaccuracy"), (50, 40, False, "mistake"),
            (50, 35, True, "blunder"),
        ]:
            self.assertEqual(expected, classify(before, after, is_best=best))
        self.assertEqual("inaccuracy", classify(50, 49, thresholds=(1, 2, 3)))

    def test_great_requires_alternatives_gap_and_viable_position(self):
        board = chess.Board()
        position = self.position(board, "e2e4")
        self.assertEqual("great", classify_positive(board, position, (5, 10, 15))[0])
        position["candidates"][1]["win_gap_from_best"] = 11.9
        self.assertEqual("best", classify_positive(board, position, (5, 10, 15))[0])
        position["candidates"] = position["candidates"][:1]
        self.assertEqual("best", classify_positive(board, position, (5, 10, 15))[0])
        position["verified_win_after"] = 49
        self.assertEqual("best", classify_positive(board, position, (5, 10, 15))[0])

    def test_verified_sacrifice_and_equal_trade_for_both_colors(self):
        for trade in (False, True):
            original = chess.Board("4k3/8/7p/" + ("6b1" if trade else "8") + "/8/5N2/8/4K3 w - - 0 1")
            for mirrored in (False, True):
                board = original.mirror() if mirrored else original
                played = "f6g4" if mirrored else "f3g5"
                reply = "e8f7" if mirrored else "e1f2"
                response = engine.AnalysisResult("unused", 4, [engine.EngineLine(0, None, [reply])])
                with patch.object(engine, "analyse", return_value=response):
                    label, evidence = classify_positive(board, self.position(board, played), (5, 10, 15))
                self.assertEqual("great" if trade else "brilliant", label)
                if not trade:
                    sacrifice = evidence["sacrifice"]
                    self.assertEqual(3, sacrifice["material_invested"])
                    replay = board.copy()
                    for uci in sacrifice["line"]["uci"]:
                        move = chess.Move.from_uci(uci)
                        self.assertIn(move, replay.legal_moves)
                        replay.push(move)
                    self.assertEqual(replay.fen(), sacrifice["settled_fen"])

    def test_losing_capture_response_is_not_brilliant(self):
        board = chess.Board("4k3/8/7p/8/8/5N2/8/4K3 w - - 0 1")
        response = engine.AnalysisResult("unused", 4, [engine.EngineLine(-400, None, ["e1f2"])])
        with patch.object(engine, "analyse", return_value=response):
            self.assertNotEqual("brilliant", classify_positive(board, self.position(board, "f3g5"), (5, 10, 15))[0])

    def test_immediate_recapture_is_not_a_sacrifice(self):
        board = chess.Board("4k1r1/8/8/8/8/5R2/7P/4K3 w - - 0 1")
        response = engine.AnalysisResult("unused", 4, [engine.EngineLine(0, None, ["h2g3"])])
        with patch.object(engine, "analyse", return_value=response):
            label, evidence = classify_positive(board, self.position(board, "f3g3"), (5, 10, 15))
        self.assertEqual("great", label)
        self.assertNotIn("sacrifice", evidence)

    def test_single_legal_reply_and_already_winning_alternatives_are_not_awards(self):
        board = chess.Board("R6k/8/5K2/8/8/8/8/8 b - - 0 1")
        self.assertEqual(1, board.legal_moves.count())
        position = self.position(chess.Board(), "e2e4")
        position["played_move"]["uci"] = "h8h7"
        position["candidates"] = [{"move": {"uci": "h8h7"}, "win_percent": {"mover": 50}}]
        self.assertEqual("best", classify_positive(board, position, (5, 10, 15))[0])
        board = chess.Board("4k3/8/7p/8/8/5N2/8/4K3 w - - 0 1")
        position = self.position(board, "f3g5", gap=1, after=90)
        for candidate in position["candidates"]:
            candidate["win_percent"]["mover"] = 90
        with patch.object(engine, "analyse") as analyse:
            self.assertEqual("best", classify_positive(board, position, (5, 10, 15))[0])
        analyse.assert_not_called()

    def test_mating_sacrifice_settles_without_searching_a_terminal_board(self):
        board = chess.Board("6rk/6pp/7N/8/8/8/8/6K1 w - - 0 1")
        with patch.object(engine, "analyse") as analyse:
            leaf = _settle_leaf(board, ["h6f7"], 4)
        self.assertTrue(leaf.is_checkmate())
        analyse.assert_not_called()

    def test_highlights_do_not_displace_errors_or_fill_with_ordinary_good_moves(self):
        errors = [{"ply": i * 2 + 1, "side": "white", "classification": "mistake",
                   "win_percent_loss": 10, "win_percent_before": {"mover": 50}}
                  for i in range(config.CRITICAL_MAX)]
        highlights = [{"ply": 101 + i*2, "side": "white", "classification": "great"} for i in range(5)]
        good = {"ply": 200, "side": "white", "classification": "good", "centipawn_loss": 100}
        result = select_critical_moves([*errors, *highlights, good], "white", (5, 10, 15))
        self.assertEqual(config.CRITICAL_MAX + config.HIGHLIGHT_MAX, len(result))
        self.assertNotIn(good, result)
        self.assertTrue(all(m in result for m in errors))


class PositivePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chesscoach-positive-test-")
        self.addCleanup(self.temp.cleanup)
        for name, value in (("DATA_DIR", self.temp.name), ("DEEP_ANALYSIS_DEPTH", 4),
                            ("SWEEP_DEPTH", 4), ("PERSONALIZE_HISTORY", False)):
            override = patch.object(config, name, value)
            override.start()
            self.addCleanup(override.stop)
        self.fake = FakeEngine()
        for override in (patch.object(engine, "analyse", side_effect=self.fake),
                         patch.object(engine, "info", return_value={"name": "Fake", "options": {}})):
            override.start()
            self.addCleanup(override.stop)

    def test_final_labels_stats_facts_storage_and_cache_agree(self):
        session = game_analysis.analyze_game(PGN, player="white", depth=4)
        analysis = session.engine_analysis
        self.assertEqual(["great"] * 3, [m["classification"] for m in analysis["moves"]])
        self.assertEqual(["great"] * 3, [n["classification"] for n in session.timeline[:-1]])
        self.assertEqual([], session.mistakes)
        self.assertEqual(2, analysis["summary"]["classifications_by_side"]["white"]["great"])
        self.assertEqual(1, analysis["summary"]["classifications_by_side"]["black"]["great"])
        for side in ("white", "black"):
            self.assertEqual(set(CLASSIFICATIONS), set(analysis["summary"]["classifications_by_side"][side]))
            self.assertEqual(sum(m["side"] == side for m in analysis["moves"]),
                             sum(analysis["summary"]["classifications_by_side"][side].values()))
        for critical in analysis["critical_positions"]:
            self.assertEqual([], map_analysis_position(analysis, critical))
            self.assertEqual([], critical["facts"]["motifs"])
            facts = project_facts(critical["facts"], critical["signals"])
            self.assertEqual(critical["classification_reason"], facts["classification_reason"])
            with patch("server.core.explanation.builder._relevant_memory", return_value=[]):
                request = build_request(analysis, critical)
            self.assertIn("facts.classification_reason", request.allowed_evidence_refs)
        store_analysis(analysis["game_id"], "white", analysis)
        self.assertEqual(analysis, load_analysis(analysis["game_id"], "white"))
        self.assertEqual([], training.list_training_positions(data_dir=self.temp.name))
        analysis_cache.store(session)
        loaded = analysis_cache.load(PGN, "white")
        self.assertIsNotNone(loaded)
        self.assertEqual(session.engine_analysis, loaded.engine_analysis)
        with patch.object(game_analysis, "CLASSIFICATION_VERSION", 999):
            self.assertIsNone(analysis_cache.load(PGN, "white"))

    def test_verification_failure_keeps_basic_review_and_reports_incomplete(self):
        def failing(fen, *, depth, multipv=1):
            if multipv > 1:
                raise RuntimeError("Stockfish engine failed")
            return self.fake(fen, depth=depth, multipv=multipv)
        phases = []
        with patch.object(engine, "analyse", side_effect=failing):
            session = game_analysis.analyze_game(PGN, player="white", depth=4,
                                                on_progress=lambda p: phases.append(p["phase"]))
        self.assertEqual("incomplete", session.engine_analysis["summary"]["positive_verification"])
        self.assertEqual(["best"] * 3, [m["classification"] for m in session.engine_analysis["moves"]])
        self.assertIn("verifying_positive", phases)
        analysis_cache.store(session)
        self.assertIsNone(analysis_cache.load(PGN, "white"))

    def test_deep_downgrade_is_reflected_in_mistakes_signals_and_counts(self):
        def changed_score(fen, *, depth, multipv=1):
            result = self.fake(fen, depth=depth, multipv=multipv)
            if multipv > 1:
                result.lines[0].cp = 200
            return result
        with patch.object(engine, "analyse", side_effect=changed_score):
            session = game_analysis.analyze_game(PGN, player="white", depth=4)
        self.assertEqual(2, len(session.mistakes))
        for move in session.mistakes:
            self.assertEqual("blunder", move.classification)
            self.assertTrue(move.comment)
        for move in session.engine_analysis["moves"]:
            self.assertIn("winning_to_equal", move["signals"])
        self.assertEqual(2, session.engine_analysis["summary"]["classifications"]["blunder"])

    def test_analysis_job_api_reopen_and_other_side(self):
        import httpx
        from server.web import jobs
        from server.web.app import create_app
        from server.core import session as session_mod

        self.addCleanup(session_mod.clear_session)
        app = create_app()

        async def inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        def thread(*, target, args, **kwargs):
            return SimpleNamespace(start=lambda: target(*args))

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://testserver") as client:
                response = await client.post("/api/analyze", json={"pgn": PGN, "player": "white"})
                self.assertEqual(200, response.status_code)
                self.assertEqual("ready", response.json()["status"])
                game_id = response.json()["game_id"]
                summary = (await client.get("/api/session")).json()
                self.assertEqual(2, summary["classification_summary"]["classifications_by_side"]["white"]["great"])
                timeline = (await client.get("/api/timeline")).json()["nodes"]
                self.assertEqual(["great"] * 3, [n["classification"] for n in timeline[:-1]])
                saved = await client.get(f"/api/games/{game_id}/analysis?review_side=white")
                self.assertEqual(200, saved.status_code)
                self.assertEqual(2, len(saved.json()["critical_positions"]))
                before_calls = len(self.fake.calls)
                reopened = await client.post("/api/analyze", json={"pgn": PGN, "player": "white"})
                self.assertEqual("ready", reopened.json()["status"])
                self.assertEqual(before_calls, len(self.fake.calls))
                other = await client.post("/api/analyze", json={"pgn": PGN, "player": "black"})
                self.assertEqual("ready", other.json()["status"])
                summary = (await client.get("/api/session")).json()
                self.assertEqual("black", summary["player"])
                self.assertEqual(1, summary["num_critical_positions"])

        old_state, old_records = copy.deepcopy(jobs._state), copy.deepcopy(jobs._records)
        try:
            with patch("fastapi.routing.run_in_threadpool", new=inline), \
                    patch.object(jobs.threading, "Thread", side_effect=thread), \
                    patch.object(config, "HISTORY_ENABLED", False):
                asyncio.run(check())
        finally:
            jobs._state.clear()
            jobs._state.update(old_state)
            jobs._records.clear()
            jobs._records.update(old_records)


if __name__ == "__main__":
    unittest.main()
