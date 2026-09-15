"""Known-evidence metric contracts; no real corpus, model, or user data."""
import unittest
import hashlib
import json
from pathlib import Path
import tempfile
import chess

from tests.evals.rag.runner import compare, diagnose, load_translations, report, score, unique_candidates, verified_context


class RagRunnerTests(unittest.TestCase):
    def test_context_replays_facts_without_curated_hints_or_invented_history(self):
        board = chess.Board()
        board.push_san("e4")
        context = {"initial_fen": chess.STARTING_FEN, "moves_san": ["e4"], "fen": board.fen(),
                   "candidate_moves_uci": ["secret_hint"], "opening": "secret_label", "side_to_move": "wrong"}
        text = verified_context(context)
        self.assertIn("Side to move: Black", text)
        self.assertIn("pawn e4", text)
        self.assertIn("1.e4", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("wrong", text)
        self.assertEqual(verified_context(None), "")
        self.assertNotIn("move history", verified_context(dict(context, initial_fen=board.fen(), moves_san=[])))
        with self.assertRaises(ValueError):
            verified_context(dict(context, fen=chess.STARTING_FEN))
        with self.assertRaises(ValueError):
            verified_context(dict(context, moves_san=["e5"]))

    def test_unknown_is_not_negative_and_background_is_not_direct(self):
        labels = {"q": {"kind": "general", "answerability": "supported",
                        "judgments": [{"chunk_id": "background", "grade": 1},
                                      {"chunk_id": "direct", "grade": 2}]}}
        rows = [{"chunk_id": cid} for cid in ("unknown", "background", "direct")]
        result = score([{"query_id": "q", "status": "ok",
                         "routes": {route: rows for route in ("bm25", "dense", "hybrid")}}], labels)
        metric = result["aggregate"]["hybrid"]["3"]
        self.assertEqual(metric["known_direct_hit"], 1)
        self.assertAlmostEqual(metric["known_direct_reciprocal_rank"], 1 / 3)
        self.assertAlmostEqual(metric["unjudged_fraction"], 1 / 3)

    def test_failure_counts_as_miss_and_unsupported_is_excluded(self):
        labels = {qid: {"kind": "general", "answerability": answerability, "judgments": []}
                  for qid, answerability in (("fail", "supported"), ("out", "requires_engine"))}
        result = score([{"query_id": qid, "status": "error"} for qid in labels], labels)
        self.assertEqual(result["failed_queries"], 2)
        metric = result["aggregate"]["dense"]["5"]
        self.assertEqual(metric["scored_supported_queries"], 1)
        self.assertEqual(metric["known_direct_hit"], 0)
        self.assertIsNone(metric["unjudged_fraction"])

    def test_missing_cases_cannot_be_reported_as_complete(self):
        with self.assertRaisesRegex(ValueError, "exactly once"):
            score([], {"missing": {}})

    def test_deduplication_keeps_first_rank_and_fills_limit(self):
        rows = [{"chunk_id": cid, "text_hash": text_hash}
                for cid, text_hash in (("a", "x"), ("b", "x"), ("c", "y"), ("d", "z"))]
        self.assertEqual([r["chunk_id"] for r in unique_candidates(rows, 2)], ["a", "c"])

    def test_rescoring_rejects_changed_queries_or_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "queries.jsonl").write_text("original\n")
            (root / "run.json").write_text(json.dumps({
                "integrity": {"corpus_fingerprint": "original"},
                "query_sha256": hashlib.sha256(b"original\n").hexdigest(),
            }))
            (root / "manifest.json").write_text(json.dumps({"corpus_fingerprint": "changed"}))
            with self.assertRaisesRegex(ValueError, "different corpus"):
                report(root, root)
            (root / "manifest.json").write_text(json.dumps({"corpus_fingerprint": "original"}))
            (root / "queries.jsonl").write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "queries changed"):
                report(root, root)

    def test_frozen_translation_requires_matching_queries_and_complete_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "queries.jsonl").write_text("original\n")
            metadata = {"query_sha256": hashlib.sha256(b"original\n").hexdigest()}
            (root / "run.json").write_text(json.dumps(metadata))
            rows = [{"query_id": "a", "status": "ok", "query_en": "Why develop?"},
                    {"query_id": "b", "status": "error"}]
            (root / "translations.json").write_text(json.dumps(rows))
            queries = [{"query_id": "a"}, {"query_id": "b"}]
            translations, _ = load_translations(root, root, queries)
            self.assertEqual(translations["b"]["status"], "error")
            for invalid in (rows[:1], [rows[0], rows[0]],
                            [dict(rows[0], query_en=" "), rows[1]]):
                (root / "translations.json").write_text(json.dumps(invalid))
                with self.assertRaises(ValueError):
                    load_translations(root, root, queries)
            (root / "queries.jsonl").write_text("changed\n")
            with self.assertRaisesRegex(ValueError, "do not match"):
                load_translations(root, root, queries)

    def test_comparison_rescores_with_shared_labels_and_rejects_index_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "queries.jsonl").write_text("same\n")
            (root / "manifest.json").write_text(json.dumps({"corpus_fingerprint": "corpus"}))
            (root / "labels.json").write_text(json.dumps({"q": {
                "kind": "general", "answerability": "supported", "judgments": [{"chunk_id": "c", "grade": 2}]}}))
            metadata = {"query_sha256": hashlib.sha256(b"same\n").hexdigest(),
                        "index_fingerprint": "index", "embedding_fingerprint": "model",
                        "context_mode": "question_only", "integrity": {"corpus_fingerprint": "corpus"},
                        "query_field": "query_zh"}
            runs = [root / name for name in ("a", "b")]
            for directory in runs:
                directory.mkdir()
                (directory / "run.json").write_text(json.dumps(metadata))
                (directory / "results.json").write_text(json.dumps([{"query_id": "q", "status": "ok",
                    "routes": {route: [{"chunk_id": "c"}] for route in ("bm25", "dense", "hybrid")}}]))
            compare(root, runs, root / "comparison.json")
            comparison = json.loads((root / "comparison.json").read_text())
            self.assertEqual(comparison["runs"][0]["score"]["aggregate"]["hybrid"]["5"]["known_direct_hit"], 1)
            for directory in runs:
                (directory / "q-trace.json").write_text(json.dumps({"query": "fixture", "dense": [
                    {"chunk_id": "unknown", "text": "Unknown"}, {"chunk_id": "c", "text": "Evidence"}], "lexical": []}))
            diagnose(root, runs, ["q"], root / "diagnosis.json")
            detail = json.loads((root / "diagnosis.json").read_text())["diagnostics"][0]
            self.assertEqual(detail["known_direct_ranks"]["c"], {"dense": 2, "bm25": None})
            self.assertIsNone(detail["candidates"]["dense"][0]["grade"])
            (runs[1] / "run.json").write_text(json.dumps(dict(metadata, context_mode="verified_board_v1")))
            with self.assertRaisesRegex(ValueError, "identical"):
                compare(root, runs, root / "context-rejected.json")
            compare(root, runs, root / "context-comparison.json", allow_context_change=True)
            (runs[1] / "run.json").write_text(json.dumps(dict(metadata, query_field="query_en_reference")))
            with self.assertRaisesRegex(ValueError, "identical"):
                compare(root, runs, root / "language-rejected.json", allow_context_change=True)
            (runs[1] / "run.json").write_text(json.dumps(dict(metadata, index_fingerprint="changed")))
            with self.assertRaisesRegex(ValueError, "identical"):
                compare(root, runs, root / "rejected.json")


if __name__ == "__main__":
    unittest.main()
