"""Known-evidence metric contracts; no real corpus, model, or user data."""
import unittest
import hashlib
import json
from pathlib import Path
import tempfile

from tests.evals.rag.runner import report, score, unique_candidates


class RagRunnerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
