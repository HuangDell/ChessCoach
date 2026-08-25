from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

import chess

from server import config
from server.core import engine
from server.core.game_analysis import analyze_game


STOCKFISH = Path(__file__).parents[2] / ".chess-review" / "engine" / "stockfish"
FOOLS_MATE = """[Event "Phase 5 system fixture"]
[Site "local"]
[Date "2026.08.25"]
[Round "1"]
[White "Fixture White"]
[Black "Fixture Black"]
[Result "0-1"]

1. f3 e5 2. g4 Qh4# 0-1
"""


@unittest.skipUnless(
    os.environ.get("CHESSCOACH_RUN_ENGINE_SYSTEM") == "1" and STOCKFISH.is_file(),
    "set CHESSCOACH_RUN_ENGINE_SYSTEM=1 with the fixture Stockfish installed",
)
class RealEngineSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="chesscoach-engine-system-")
        self.previous = {
            "DATA_DIR": config.DATA_DIR,
            "STOCKFISH_PATH": config.STOCKFISH_PATH,
            "ENGINE_POOL_SIZE": config.ENGINE_POOL_SIZE,
            "ENGINE_THREADS": config.ENGINE_THREADS,
            "ENGINE_HASH_MB": config.ENGINE_HASH_MB,
            "DEEP_ANALYSIS_DEPTH": config.DEEP_ANALYSIS_DEPTH,
            "DEEP_ANALYSIS_MULTIPV": config.DEEP_ANALYSIS_MULTIPV,
            "ENGINE_CACHE_ENABLED": config.ENGINE_CACHE_ENABLED,
        }
        engine.restart()
        config.DATA_DIR = self.temporary.name
        config.STOCKFISH_PATH = str(STOCKFISH)
        config.ENGINE_POOL_SIZE = 1
        config.ENGINE_THREADS = 1
        config.ENGINE_HASH_MB = 16
        config.DEEP_ANALYSIS_DEPTH = 4
        config.DEEP_ANALYSIS_MULTIPV = 3
        config.ENGINE_CACHE_ENABLED = True

    def tearDown(self) -> None:
        engine.shutdown()
        for name, value in self.previous.items():
            setattr(config, name, value)
        self.temporary.cleanup()

    def test_short_game_analysis_is_legal_versioned_and_closes_engine_pool(self) -> None:
        session = analyze_game(FOOLS_MATE, player="white", depth=4)
        artifact = session.engine_analysis

        self.assertEqual(2, artifact["schema_version"])
        self.assertEqual("white", artifact["review_side"])
        self.assertEqual(4, artifact["profile"]["scan"]["depth"])
        self.assertEqual(4, artifact["profile"]["deep"]["depth"])
        self.assertIn("Stockfish", artifact["engine"]["name"])
        self.assertEqual({"Threads": 1, "Hash": 16}, artifact["engine"]["options"])
        self.assertEqual(4, artifact["summary"]["plies"])
        self.assertGreaterEqual(len(artifact["critical_positions"]), 1)

        board = chess.Board()
        for move in artifact["moves"]:
            played = chess.Move.from_uci(move["played_move"]["uci"])
            self.assertIn(played, board.legal_moves)
            self.assertEqual(board.fen(), move["fen_before"])
            board.push(played)
            self.assertEqual(board.fen(), move["fen_after"])

        for critical in artifact["critical_positions"]:
            position = chess.Board(critical["fen_before"])
            self.assertEqual(1, critical["facts"]["facts_version"])
            self.assertEqual(critical["critical_id"], critical["facts"]["critical_id"])
            self.assertEqual(4, critical["deep_depth"])
            self.assertLessEqual(len(critical["candidates"]), 3)
            for candidate in critical["candidates"]:
                replay = position.copy(stack=False)
                for raw_uci in candidate["line"]["uci"]:
                    move = chess.Move.from_uci(raw_uci)
                    self.assertIn(move, replay.legal_moves)
                    replay.push(move)

        engine.shutdown()
        self.assertFalse(engine._POOL._started)
        self.assertTrue(engine._POOL._pool.empty())


if __name__ == "__main__":
    unittest.main()
