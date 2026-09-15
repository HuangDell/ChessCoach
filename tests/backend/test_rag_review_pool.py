import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.evals.rag.review_pool import merge, prepare


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class RagReviewPoolTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path, Path]:
        dataset = root / "dataset"
        retrieval = root / "retrieval"
        corpus = dataset / "corpus-v3.sqlite3"
        dataset.mkdir()
        retrieval.mkdir()
        query_rows = [
            {"query_id": query_id, "query_zh": text, "query_en_reference": f"reference {query_id}", "context": None}
            for query_id, text in (("Q3", "第三个问题"), ("Q1", "第一个问题"), ("Q2", "第二个问题"))
        ]
        query_text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in query_rows)
        (dataset / "queries.jsonl").write_text(query_text, encoding="utf-8")
        (dataset / "translation_prompt.md").write_text("```text\nTranslate.\n```\n", encoding="utf-8")

        with sqlite3.connect(corpus) as connection:
            connection.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
                CREATE TABLE books (
                    book_id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL,
                    language TEXT NOT NULL, format TEXT NOT NULL, source_name TEXT NOT NULL,
                    source_size INTEGER NOT NULL, source TEXT NOT NULL, source_uri TEXT NOT NULL,
                    rights TEXT NOT NULL, chunk_count INTEGER NOT NULL
                );
                CREATE TABLE chunks (
                    chunk_id TEXT PRIMARY KEY, book_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    heading_path TEXT NOT NULL, source_locator TEXT NOT NULL, text TEXT NOT NULL,
                    text_hash TEXT NOT NULL, unit_count INTEGER NOT NULL
                );
            """)
            connection.executemany("INSERT INTO meta VALUES (?, ?)", [
                ("corpus_version", "fixture-v3"), ("schema_version", "3"),
                ("source_collection_hash", "source-hash"),
            ])
            connection.execute(
                "INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("book", "Fixture Book", "Author", "en", "txt", "fixture.txt", 1,
                 "fixture", "fixture", "test", 4),
            )
            chunks = []
            for ordinal, chunk_id in enumerate(("old", "new-Q1", "new-Q2", "new-Q3")):
                text = f"passage {chunk_id}"
                chunks.append((chunk_id, "book", ordinal, '["Chapter"]', f"loc-{ordinal}", text,
                               hashlib.sha256(text.encode()).hexdigest(), 2))
            connection.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)", chunks)

        with sqlite3.connect(corpus) as connection:
            rows = connection.execute("SELECT chunk_id, text_hash FROM chunks ORDER BY book_id, ordinal").fetchall()
        fingerprint = hashlib.sha256("\0".join([
            "fixture-v3", "source-hash", *(f"{chunk_id}:{text_hash}" for chunk_id, text_hash in rows),
        ]).encode()).hexdigest()
        labels = {}
        old_hash = rows[0][1]
        for query_id in ("Q1", "Q2", "Q3"):
            labels[query_id] = {
                "kind": "general", "origin": "fixture", "group_id": query_id,
                "split": "unassigned_draft", "answerability": "supported",
                "review_status": "model_reviewed", "qrels_complete": False,
                "judgments": [{
                    "chunk_id": "old", "book_id": "book", "ordinal": 0,
                    "text_hash": old_hash, "source_locator": "loc-0", "title": "Fixture Book",
                    "author": "Author", "heading_path": ["Chapter"], "reason_zh": "已有直接证据",
                    "grade": 2,
                }],
                "diagnostic_sources": [], "expected_points_zh": ["要点"],
                "prohibited_claims_zh": [], "engine_evaluation_available": False,
                "notes_zh": "", "independent_human_review": False,
                "reviewer": {"type": "model_assisted_review", "model": "fixture",
                             "agent": "fixture", "method": "fixture"},
                "review_notes_zh": "fixture model review",
            }
        write_json(dataset / "labels.json", labels)
        write_json(dataset / "manifest.json", {
            "dataset_version": "fixture-v2", "status": "model_reviewed", "query_count": 3,
            "corpus_fingerprint": fingerprint, "corpus_version": "fixture-v3",
            "corpus_schema_version": 3, "corpus_chunk_count": 4, "books": [],
            "translation": {}, "judgments": {"complete": False},
        })

        translations = []
        results = []
        pool = []
        hashes = {chunk_id: text_hash for chunk_id, text_hash in rows}
        for query_id in ("Q1", "Q2", "Q3"):
            query_en = f"translated {query_id}"
            translations.append({"query_id": query_id, "status": "ok", "query_en": query_en})
            old = {"chunk_id": "old", "text_hash": hashes["old"]}
            new_id = f"new-{query_id}"
            new = {"chunk_id": new_id, "text_hash": hashes[new_id]}
            results.append({
                "query_id": query_id, "status": "ok", "query_text": query_en,
                "routes": {"bm25": [old], "dense": [new], "hybrid": [new, old]},
            })
            query = next(row for row in query_rows if row["query_id"] == query_id)
            pool.append({
                "query_id": query_id, "query_zh": query["query_zh"], "context": None,
                "candidates": [
                    {**old, "title": "Fixture Book", "source_locator": "loc-0", "text": "passage old"},
                    {**new, "title": "Fixture Book", "source_locator": f"loc-{query_id[-1]}",
                     "text": f"passage {new_id}"},
                ],
            })
        write_json(retrieval / "translations.json", translations)
        write_json(retrieval / "translation-run.json", {"model": "translator", "status": "complete"})
        write_json(retrieval / "results.json", results)
        write_json(retrieval / "review-pool.json", pool)
        write_json(retrieval / "run.json", {
            "translation": "frozen_live_outputs",
            "translation_sha256": hashlib.sha256((retrieval / "translations.json").read_bytes()).hexdigest(),
            "query_sha256": hashlib.sha256((dataset / "queries.jsonl").read_bytes()).hexdigest(),
            "integrity": {"corpus_fingerprint": fingerprint},
        })
        return dataset, retrieval, corpus

    def complete_reviews(self, review_dir: Path) -> list[Path]:
        paths = []
        for shard in ("A", "B", "C"):
            path = review_dir / f"shard-{shard}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["reviewer"] = {
                "type": "model_assisted_review", "model": "gpt-5.6-sol",
                "reasoning_effort": "high", "agent": f"review-{shard}", "method": "blind pool review",
            }
            value["translation_reviews"] = [
                {"query_id": row["query_id"], "verdict": "faithful", "reason_zh": "含义完整"}
                for row in value["translations"]
            ]
            value["decisions"] = [
                {"query_id": item["query_id"], "chunk_id": item["candidate"]["chunk_id"],
                 "text_hash": item["candidate"]["text_hash"], "grade": 1,
                 "reason_zh": "提供相关背景", "uncertain": True}
                for item in value["items"]
            ]
            write_json(path, value)
            paths.append(path)
        return paths

    def test_prepare_blinds_existing_labels_and_merge_preserves_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset, retrieval, corpus = self.make_fixture(root)
            review_dir = root / "review"
            summary = prepare(dataset, retrieval, review_dir)
            self.assertEqual(summary["new_candidate_pairs"], 3)
            self.assertEqual(summary["pairs_by_shard"], {"A": 1, "B": 1, "C": 1})
            raw = "".join((review_dir / f"shard-{shard}.json").read_text() for shard in ("A", "B", "C"))
            self.assertNotIn("reference Q", raw)
            self.assertNotIn("已有直接证据", raw)
            reviews = self.complete_reviews(review_dir)
            result = merge(dataset, corpus, reviews, root / "merged")
            self.assertEqual(result["new_judgments"], 3)
            self.assertEqual(result["translation_reviews"], 3)
            merged = json.loads((root / "merged" / "labels.json").read_text())
            self.assertEqual([row["chunk_id"] for row in merged["Q1"]["judgments"]], ["old", "new-Q1"])
            self.assertFalse(merged["Q1"]["qrels_complete"])
            self.assertFalse(merged["Q1"]["independent_human_review"])
            self.assertTrue((root / "merged" / "reviews" / "shard-A.json").exists())

    def test_merge_rejects_incomplete_candidate_review(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset, retrieval, corpus = self.make_fixture(root)
            review_dir = root / "review"
            prepare(dataset, retrieval, review_dir)
            reviews = self.complete_reviews(review_dir)
            value = json.loads(reviews[0].read_text())
            value["decisions"] = []
            write_json(reviews[0], value)
            with self.assertRaisesRegex(ValueError, "exactly cover"):
                merge(dataset, corpus, reviews, root / "merged")

    def test_merge_rejects_changed_blind_candidate_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset, retrieval, corpus = self.make_fixture(root)
            review_dir = root / "review"
            prepare(dataset, retrieval, review_dir)
            reviews = self.complete_reviews(review_dir)
            value = json.loads(reviews[0].read_text())
            value["items"][0]["candidate"]["text"] = "changed"
            write_json(reviews[0], value)
            with self.assertRaisesRegex(ValueError, "changed after preparation"):
                merge(dataset, corpus, reviews, root / "merged")


if __name__ == "__main__":
    unittest.main()
