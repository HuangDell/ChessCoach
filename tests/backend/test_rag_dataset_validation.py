"""Dataset safeguards use synthetic files, never the user's local benchmark."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import chess

from tests.evals.rag.validate_dataset import validate


class RagDatasetValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-dataset-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.corpus = self.root / "corpus.sqlite3"
        with sqlite3.connect(self.corpus) as connection:
            connection.executescript("""
                CREATE TABLE meta (key TEXT, value TEXT);
                INSERT INTO meta VALUES ('corpus_version', 'fixture-v1');
                INSERT INTO meta VALUES ('source_collection_hash', 'fixture-source');
                CREATE TABLE chunks (
                    chunk_id TEXT, book_id TEXT, ordinal INTEGER,
                    text_hash TEXT, source_locator TEXT
                );
                INSERT INTO chunks VALUES ('c1', 'b1', 0, 'text-hash', 'fixture#1');
            """)
        self.query = {
            "query_id": "q1", "query_zh": "什么是对王？",
            "query_en_reference": "What is opposition?", "context": None,
        }
        self.labels = {"q1": {
            "kind": "general", "answerability": "supported",
            "review_status": "draft_pending_human_review", "qrels_complete": False,
            "group_id": "opposition", "expected_points_zh": ["控制关键格。"],
            "judgments": [{
                "chunk_id": "c1", "book_id": "b1", "ordinal": 0,
                "text_hash": "text-hash", "source_locator": "fixture#1",
                "grade": 2, "reason_zh": "合成标注。",
            }], "diagnostic_sources": [],
        }}
        self.manifest = {
            "query_count": 1,
            "corpus_fingerprint": hashlib.sha256(
                b"fixture-v1\0fixture-source\0c1:text-hash"
            ).hexdigest(),
        }

    def write_dataset(self) -> None:
        (self.root / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (self.root / "queries.jsonl").write_text(json.dumps(self.query) + "\n", encoding="utf-8")
        (self.root / "labels.json").write_text(json.dumps(self.labels), encoding="utf-8")

    def test_valid_draft_does_not_claim_quality_metrics(self) -> None:
        self.write_dataset()
        result = validate(self.root, self.corpus)
        self.assertEqual(result["queries"], 1)
        self.assertEqual(result["retrieval_metrics"], "not_run")
        self.assertEqual(result["semantic_review"], "pending")

    def test_changed_corpus_rejects_stale_dataset(self) -> None:
        self.write_dataset()
        with sqlite3.connect(self.corpus) as connection:
            connection.execute("UPDATE chunks SET text_hash='changed'")
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            validate(self.root, self.corpus)

    def test_model_review_requires_provenance_and_is_not_human_approval(self) -> None:
        self.labels["q1"].update(review_status="model_reviewed", independent_human_review=False,
                                 reviewer={"type": "model_assisted_review", "agent": "reviewer", "model": "test-model"},
                                 review_notes_zh="Checked the synthetic passage.")
        self.write_dataset()
        self.assertEqual(validate(self.root, self.corpus)["semantic_review"], "model_reviewed_not_human_approved")
        self.labels["q1"]["independent_human_review"] = True
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "not human approval"):
            validate(self.root, self.corpus)

    def test_model_review_without_provenance_is_rejected(self) -> None:
        self.labels["q1"].update(review_status="model_reviewed", independent_human_review=False)
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "provenance"):
            validate(self.root, self.corpus)

    def test_gold_fields_cannot_enter_query_input(self) -> None:
        self.query["expected_answer"] = "Hidden answer"
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "model-input fields"):
            validate(self.root, self.corpus)

    def test_unsupported_label_cannot_claim_positive_evidence(self) -> None:
        self.labels["q1"]["judgments"][0]["grade"] = 0
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "no direct supporting passage"):
            validate(self.root, self.corpus)

    def test_source_hash_must_match(self) -> None:
        self.labels["q1"]["judgments"][0]["text_hash"] = "wrong"
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "stale source hash"):
            validate(self.root, self.corpus)

    def test_real_position_preserves_fen_without_inventing_history(self) -> None:
        board = chess.Board()
        self.query["context"] = {
            "origin": "user_provided_fen", "initial_fen": board.fen(),
            "fen": board.fen(), "moves_san": [], "side_to_move": "white",
            "in_check": False, "candidate_moves_uci": [],
            "piece_map": {chess.square_name(s): p.symbol() for s, p in sorted(board.piece_map().items())},
        }
        self.labels["q1"]["kind"] = "real_position"
        self.write_dataset()
        self.assertEqual(validate(self.root, self.corpus)["verified_positions"], 1)
        # Even a legal cycle back to the same piece placement is invented history.
        self.query["context"]["moves_san"] = ["Nf3", "Nf6", "Ng1", "Ng8"]
        for san in self.query["context"]["moves_san"]:
            board.push_san(san)
        self.query["context"]["fen"] = board.fen()
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "must not invent move history"):
            validate(self.root, self.corpus)

    def test_real_position_requires_context(self) -> None:
        self.labels["q1"]["kind"] = "real_position"
        self.write_dataset()
        with self.assertRaisesRegex(ValueError, "missing position"):
            validate(self.root, self.corpus)


if __name__ == "__main__":
    unittest.main()
