from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server import config
from server.core import puzzle_flow, puzzle_session, puzzle_storm, puzzles
from server.core.learning import ObservationStore, initialize_learning


class CuratedPuzzleValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="chesscoach-curated-validation-")
        self.old_data_dir = config.DATA_DIR
        config.DATA_DIR = self.temporary.name
        puzzle_session.clear_current()
        puzzle_storm.clear()
        status = initialize_learning(self.temporary.name)
        self.assertTrue(status["available"], status.get("error"))

    def tearDown(self) -> None:
        puzzle_session.clear_current()
        puzzle_storm.clear()
        try:
            status = initialize_learning(self.temporary.name)
            self.assertTrue(status["available"], status.get("error"))
        finally:
            config.DATA_DIR = self.old_data_dir
            self.temporary.cleanup()

    @staticmethod
    def _valid_puzzle(*, puzzle_id: str = "curated-valid") -> dict:
        return {
            "id": puzzle_id,
            "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            "solve_fen": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
            "moves": ["e2e4", "e7e5"],
            "themes": ["fork", "opening"],
            "rating": 1200,
            "rd": 50,
        }

    @staticmethod
    def _invalid_fen_puzzle(*, puzzle_id: str) -> dict:
        return {
            **CuratedPuzzleValidationTests._valid_puzzle(puzzle_id=puzzle_id),
            "fen": "8/8/8/8/8/8/8/8 w - - 0 1",
            "solve_fen": "8/8/8/8/8/8/8/8 b - - 0 1",
        }

    def _attempt_path(self) -> Path:
        return Path(self.temporary.name) / "history" / "puzzle_attempts.jsonl"

    def test_shard_loader_filters_invalid_puzzles_before_serving(self) -> None:
        shard = Path(self.temporary.name) / "band_1200.jsonl.gz"
        rows = [
            self._invalid_fen_puzzle(puzzle_id="invalid-owner"),
            {**self._valid_puzzle(puzzle_id="invalid-themes"), "themes": ["fork", 7]},
            self._valid_puzzle(),
        ]
        with gzip.open(shard, "wt", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

        loaded = puzzles._load_jsonl_gz(str(shard))

        self.assertEqual(["curated-valid"], [item["id"] for item in loaded])
        self.assertEqual(self._valid_puzzle()["solve_fen"], loaded[0]["solve_fen"])
        self.assertEqual("black", loaded[0]["side_to_move"])

    def test_give_up_rejects_bad_fen_before_attempt_rating_or_scored_state(self) -> None:
        invalid = puzzle_session.set_current(
            self._invalid_fen_puzzle(puzzle_id="invalid-giveup")
        )
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state") as load_state,
            patch("server.core.puzzle_flow.puzzle_rating.record_result") as record_result,
            patch("server.core.puzzle_flow.puzzle_rating.save_state") as save_state,
        ):
            with self.assertRaisesRegex(ValueError, "invalid owning FEN"):
                puzzle_flow.give_up(invalid)

        self.assertFalse(invalid.scored)
        self.assertFalse(invalid.finished)
        self.assertFalse(self._attempt_path().exists())
        load_state.assert_not_called()
        record_result.assert_not_called()
        save_state.assert_not_called()
        self.assertTrue(initialize_learning(self.temporary.name)["available"])
        self.assertEqual([], ObservationStore(self.temporary.name).load())

        valid = puzzle_session.set_current(self._valid_puzzle(puzzle_id="valid-after-rejection"))
        with (
            patch("server.core.puzzle_flow.puzzle_rating.load_state", return_value={}),
            patch("server.core.puzzle_flow.puzzle_rating.record_result", return_value={}),
            patch("server.core.puzzle_flow.puzzle_rating.save_state"),
        ):
            puzzle_flow.give_up(valid)

        self.assertTrue(valid.scored)
        self.assertEqual(1, len(self._attempt_path().read_text(encoding="utf-8").splitlines()))
        self.assertTrue(initialize_learning(self.temporary.name)["available"])

    def test_storm_finish_rejects_bad_fen_without_committing_run(self) -> None:
        progress = puzzle_session.set_current(
            self._invalid_fen_puzzle(puzzle_id="invalid-storm-finish")
        )
        run = puzzle_storm.StormRun(1200, 1, 100.0, 180)
        puzzle_storm._RUN = run
        with patch("server.core.puzzle_storm.puzzle_rating.save_state") as save_state:
            with self.assertRaisesRegex(ValueError, "invalid owning FEN"):
                puzzle_storm.end({}, now=101.0)

        self.assertFalse(progress.scored)
        self.assertFalse(progress.finished)
        self.assertFalse(run.ended)
        self.assertIs(progress, puzzle_session.get_current())
        self.assertIs(run, puzzle_storm.get_run())
        self.assertFalse(self._attempt_path().exists())
        save_state.assert_not_called()
        self.assertTrue(initialize_learning(self.temporary.name)["available"])
        self.assertEqual([], ObservationStore(self.temporary.name).load())

    def test_validator_rejects_mismatched_owner_illegal_line_and_unverified_themes(self) -> None:
        invalid_cases = (
            {**self._valid_puzzle(), "solve_fen": self._valid_puzzle()["fen"]},
            {**self._valid_puzzle(), "moves": ["e2e5", "e7e5"]},
            {**self._valid_puzzle(), "themes": []},
            {**self._valid_puzzle(), "themes": ["fork", "fork"]},
        )
        for puzzle in invalid_cases:
            with self.subTest(puzzle=puzzle):
                with self.assertRaises(ValueError):
                    puzzles.validate_curated_puzzle(puzzle)


if __name__ == "__main__":
    unittest.main()
