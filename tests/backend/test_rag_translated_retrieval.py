"""Translated-query retrieval contracts with fake index boundaries."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
import chess
from unittest.mock import patch

from tests.evals.rag import runner


class _FakeEmbedder:
    fingerprint = "fake-embedding"

    def __init__(self, model_path, *, device, batch_size):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeRetriever:
    search_queries = []

    def __init__(self, data_dir, embedder, *, trace_store):
        self.trace_store = trace_store

    def search(self, query, *, limit):
        self.search_queries.append(query)
        if query == "Raise during retrieval":
            raise RuntimeError("fixture retrieval failure")
        row = {
            "chunk_id": "direct",
            "text_hash": "direct-hash",
            "text": "Direct supporting passage.",
            "title": "Fixture",
            "source_locator": "fixture#direct",
        }
        self.trace_store.save({
            "status": "ok",
            "dense": [row],
            "lexical": [row],
            "timings_ms": {"total": 12.0},
        })
        return SimpleNamespace(passages=[SimpleNamespace(passage_id="direct")])

    def close(self):
        pass


class RagTranslatedRetrievalTests(unittest.TestCase):
    def test_translation_and_retrieval_failures_are_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            translations = root / "translations"
            data_dir = root / "index"
            output = root / "output"
            dataset.mkdir()
            translations.mkdir()
            (data_dir / "knowledge").mkdir(parents=True)
            (data_dir / "knowledge" / "corpus.sqlite3").touch()

            queries = [
                {"query_id": "translation_failed", "query_zh": "原问题一", "query_en_reference": "Reference one", "context": None},
                {"query_id": "ok", "query_zh": "原问题二", "query_en_reference": "Reference two", "context": {
                    "initial_fen": chess.STARTING_FEN, "fen": chess.STARTING_FEN, "moves_san": [],
                    "candidate_moves_uci": ["excluded_hint"]}},
                {"query_id": "retrieval_failed", "query_zh": "原问题三", "query_en_reference": "Reference three", "context": None},
            ]
            query_bytes = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in queries).encode()
            (dataset / "queries.jsonl").write_bytes(query_bytes)
            (dataset / "manifest.json").write_text(json.dumps({"corpus_fingerprint": "fixture-corpus"}))
            labels = {
                query_id: {
                    "kind": "general",
                    "answerability": "supported",
                    "review_status": "model_reviewed",
                    "judgments": ([{"chunk_id": "direct", "grade": 2}] if query_id == "ok" else []),
                }
                for query_id in ("translation_failed", "ok", "retrieval_failed")
            }
            (dataset / "labels.json").write_text(json.dumps(labels))
            translation_rows = [
                {"query_id": "translation_failed", "status": "error", "error_type": "ProviderError", "duration_ms": 9.0},
                {"query_id": "ok", "status": "ok", "query_en": "Exact emitted translation", "duration_ms": 7.0},
                {"query_id": "retrieval_failed", "status": "ok", "query_en": "Raise during retrieval", "duration_ms": 8.0},
            ]
            (translations / "translations.json").write_text(json.dumps(translation_rows))
            (translations / "run.json").write_text(json.dumps({
                "query_sha256": hashlib.sha256(query_bytes).hexdigest(),
                "model": "fake-translator",
            }))

            fake_index = ModuleType("server.core.knowledge.index")
            fake_index.QwenEmbedder = _FakeEmbedder
            fake_index.LanceDBKnowledgeRetriever = _FakeRetriever
            fake_index.get_index_status = lambda *args, **kwargs: SimpleNamespace(
                available=True,
                error=None,
                index_fingerprint="fixture-index",
            )
            _FakeRetriever.search_queries = []
            with (
                patch.dict(sys.modules, {"server.core.knowledge.index": fake_index}),
                patch.object(runner, "validate", return_value={"corpus_fingerprint": "fixture-corpus"}),
                patch.object(runner.subprocess, "check_output", return_value="fixture-revision\n"),
            ):
                runner.run(
                    dataset,
                    data_dir,
                    output,
                    Path("fake-model"),
                    "cpu",
                    1,
                    "query_zh",
                    translations,
                    context_mode="verified_board_v1",
                )

            self.assertEqual(len(_FakeRetriever.search_queries), 2)
            self.assertTrue(_FakeRetriever.search_queries[0].startswith("Exact emitted translation\n\nVerified board context:"))
            self.assertIn("Side to move: White", _FakeRetriever.search_queries[0])
            self.assertNotIn("excluded_hint", _FakeRetriever.search_queries[0])
            self.assertEqual(_FakeRetriever.search_queries[1], "Raise during retrieval")
            results = json.loads((output / "results.json").read_text())
            self.assertEqual(results[0]["error_type"], "TranslationFailed")
            self.assertNotIn("query_text", results[0])
            self.assertEqual(results[1]["query_text"], _FakeRetriever.search_queries[0])
            self.assertEqual(results[2]["error_type"], "RuntimeError")
            translation_trace = json.loads((output / "translation_failed-trace.json").read_text())
            self.assertEqual(translation_trace, {"status": "error", "failure_stage": "translation"})
            retrieval_trace = json.loads((output / "retrieval_failed-trace.json").read_text())
            self.assertEqual(retrieval_trace, {"status": "error", "query_id": "retrieval_failed"})

            report = json.loads((output / "report.json").read_text())
            self.assertEqual(report["failed_queries"], 2)
            self.assertEqual(report["aggregate"]["hybrid"]["5"]["scored_supported_queries"], 3)
            self.assertAlmostEqual(report["aggregate"]["hybrid"]["5"]["known_direct_hit"], 1 / 3)
            self.assertEqual(report["latency_ms"]["first_successful_query_id"], "ok")
            self.assertEqual(report["latency_ms"]["first_query_including_model_load"], 12.0)
            self.assertEqual(report["latency_ms"]["warm_query_count"], 0)
            self.assertEqual(report["translation"]["failed_queries"], 1)


if __name__ == "__main__":
    unittest.main()
